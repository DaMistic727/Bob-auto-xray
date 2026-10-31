"""Harness orchestration/owned cleanup regressions, not live external E2E."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from selfsni.core import InstallError
from tools import external_acceptance as external
from tools.remote_fixture import cleanup_fixture, create_fixture

ADMIN = {'email': 'admin', 'id': 'bc026b34-8565-4320-93eb-efad05e288f5',
         'subId': 'b'*48, 'flow': 'xtls-rprx-vision', 'enable': True,
         'limitIp': 0, 'limitHwid': 0, 'totalGB': 0, 'expiryTime': 0, 'reset': 0}


def state():
    return dict(domain='panel.test', ipv4='192.0.2.1', panel_port=21000, sub_port=22000,
                panel_path='/secret-path/', sub_path='/subscription/', panel_access='https-443',
                warp_mode='skip', admin_client=dict(ADMIN), client={'id': 'client-id', 'subId': 'sub-id'},
                reality_public='public-key', short_id='short-id')


class RunnerTests(unittest.TestCase):
    def exercise(self, port_status='INCONCLUSIVE', create_error=None, vision_error=None, cleanup_error=None,
                 probe_error=None):
        calls = []
        def remote(args, action, owner):
            calls.append((action, owner))
            if action == 'create':
                if create_error:
                    raise create_error
                return json.dumps(state())
            if action == 'cleanup' and cleanup_error:
                raise cleanup_error
            return ''
        def vision(args, s, report, remote_call):
            calls.append(('vision', None))
            if vision_error:
                raise vision_error
            report['checks'].append({'name': 'VLESS REALITY Vision', 'status': 'PASS'})
        with patch.object(external, 'remote', side_effect=remote), \
             patch.object(external, 'http', return_value=(200, {}, b'')), \
             patch.object(external, 'verify_sub_response'), \
             patch.object(external, 'probe_panel_port', return_value={'status': port_status}, side_effect=probe_error), \
             patch.object(external, 'vision_checks', side_effect=vision):
            return external.run_acceptance(SimpleNamespace()), calls

    def test_port_outcomes_keep_vision_and_cleanup(self):
        for status in ('PASS', 'FAIL', 'INCONCLUSIVE'):
            with self.subTest(status=status):
                report, calls = self.exercise(status)
                self.assertEqual(report['status'], status)
                self.assertIn(('vision', None), calls)
                self.assertEqual(calls[0][0], 'create')
                self.assertEqual(calls[-1], ('cleanup', calls[0][1]))
                self.assertEqual(report['cleanup'], 'CONFIRMED')
                self.assertTrue(all(item['status'] == 'PASS' for item in report['checks']
                                    if 'subscription' in item['name']))

    def test_probe_exception_is_inconclusive_and_does_not_skip_vision(self):
        report, calls = self.exercise(probe_error=RuntimeError('secret-path'))
        self.assertEqual(report['status'], 'INCONCLUSIVE')
        self.assertIn(('vision', None), calls)
        self.assertEqual(calls[-1][0], 'cleanup')
        self.assertNotIn('secret-path', json.dumps(report))

    def test_malformed_fixture_response_still_attempts_cleanup(self):
        with patch.object(external, 'remote', side_effect=['not-json /secret-path/', '']) as remote:
            report = external.run_acceptance(SimpleNamespace())
        self.assertEqual([call.args[1] for call in remote.call_args_list], ['create', 'cleanup'])
        self.assertEqual(report['cleanup'], 'CONFIRMED')
        self.assertEqual(report['status'], 'FAIL')
        self.assertNotIn('/secret-path/', json.dumps(report))

    def test_partial_create_exception_still_cleans_its_owner(self):
        report, calls = self.exercise(create_error=InstallError('password api-token private-key /secret-path/'))
        self.assertEqual([action for action, _ in calls], ['create', 'cleanup'])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(report['status'], 'FAIL')
        for secret in ('password', 'api-token', 'private-key', '/secret-path/'):
            self.assertNotIn(secret, json.dumps(report))

    def test_vision_exception_does_not_skip_cleanup(self):
        report, calls = self.exercise(vision_error=RuntimeError('private-key'))
        self.assertEqual(calls[-1][0], 'cleanup')
        self.assertEqual(report['status'], 'FAIL')
        self.assertNotIn('private-key', json.dumps(report))

    def test_cleanup_failure_is_never_reported_confirmed(self):
        report, _ = self.exercise(cleanup_error=InstallError('secret-url'))
        self.assertEqual((report['status'], report['cleanup']), ('FAIL', 'NOT VERIFIED'))
        self.assertNotIn('secret-url', json.dumps(report))

    def test_inconclusive_is_written_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'report.json'
            report = {'status': 'INCONCLUSIVE', 'checks': [], 'cleanup': 'CONFIRMED'}
            with patch.object(external, 'run_acceptance', return_value=report), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = external.main(['--host', 'test-host', '--control-path', 'test-socket',
                                      '--xray', 'test-xray', '--output', str(path)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(path.read_text())['status'], 'INCONCLUSIVE')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_local_xray_is_killed_after_failed_startup_check(self):
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired('xray', 5), 0]
        report = {'checks': []}
        def wait(check, *args, **kwargs):
            check()
        with patch.object(external.subprocess, 'Popen', return_value=proc), \
             patch.object(external, 'free_port', return_value=12345), \
             patch.object(external, 'wait_for', side_effect=wait), \
             patch.object(external, 'run', side_effect=InstallError('secret-url')):
            external.vision_checks(SimpleNamespace(xray='test-xray'), state(), report, lambda _: '')
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)
        self.assertTrue(any(x['status'] == 'NOT TESTED' for x in report['checks']))
        self.assertNotIn('secret-url', json.dumps(report))


class FixtureAPI:
    def __init__(self):
        self.clients = [dict(ADMIN)]
        self.catalog = [dict(ADMIN)]
        self.lose_add_response = False
        self.partial_delete = False
        self.available = True

    def call(self, endpoint, data=None):
        if not self.available:
            raise InstallError('API unavailable')
        if endpoint == 'inbounds/list':
            return [{'id': 1, 'port': 443, 'protocol': 'vless', 'remark': 'vless by bob',
                     'settings': json.dumps({'clients': self.clients})}]
        if endpoint == 'clients/list':
            return [dict(c, uuid=c['id'], id=index+1, inboundIds=[1]) for index, c in enumerate(self.catalog)]
        if endpoint == 'clients/add':
            self.clients.append(data['client'])
            self.catalog.append(dict(data['client']))
            if self.lose_add_response:
                raise InstallError('Lost response after DB commit')
        elif endpoint.startswith('clients/del/'):
            self.clients = [c for c in self.clients if c['email'] != endpoint[len('clients/del/'):]]
            if self.partial_delete:
                self.partial_delete = False
                raise InstallError('Attachments deleted; catalog transaction failed')
            self.catalog = [c for c in self.catalog if c['email'] != endpoint[len('clients/del/'):]]


class OwnedFixtureTests(unittest.TestCase):
    def create(self, api, path):
        with patch('tools.remote_fixture.run'), patch('tools.remote_fixture.wait_for'), \
             patch('tools.remote_fixture.panel_certificate_fingerprint', return_value='0'*64):
            return create_fixture(state(), api, path, 'a'*32)

    def test_lost_add_response_removes_created_client_and_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/'fixture.json'
            api = FixtureAPI(); api.lose_add_response = True
            with self.assertRaises(InstallError):
                self.create(api, fixture)
            self.assertEqual(api.clients, [ADMIN])
            self.assertFalse(fixture.exists())

    def test_restart_failure_also_cleans_created_client(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/'fixture.json'; api = FixtureAPI()
            with patch('tools.remote_fixture.run', side_effect=InstallError('restart failed')), \
                 patch('tools.remote_fixture.panel_certificate_fingerprint', return_value='0'*64):
                with self.assertRaises(InstallError):
                    create_fixture(state(), api, fixture, 'a'*32)
            self.assertEqual(api.clients, [ADMIN])
            self.assertFalse(fixture.exists())

    def test_other_run_cannot_delete_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/'fixture.json'; api = FixtureAPI()
            self.create(api, fixture)
            before = fixture.read_bytes()
            with self.assertRaises(InstallError):
                cleanup_fixture(api, fixture, 'b'*32)
            self.assertEqual(fixture.read_bytes(), before)
            self.assertEqual(len(api.clients), 2)

    def test_cleanup_is_idempotent_and_keeps_other_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/'fixture.json'; api = FixtureAPI()
            api.clients.append({'id': 'other-id', 'email': 'other-user'})
            self.create(api, fixture)
            cleanup_fixture(api, fixture, 'a'*32)
            cleanup_fixture(api, fixture, 'a'*32)
            self.assertEqual(api.clients, [ADMIN, {'id': 'other-id', 'email': 'other-user'}])
            self.assertFalse(fixture.exists())

    def test_unavailable_api_retains_recovery_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/'fixture.json'; api = FixtureAPI()
            self.create(api, fixture)
            api.available = False
            with self.assertRaises(InstallError):
                cleanup_fixture(api, fixture, 'a'*32)
            self.assertTrue(fixture.exists())
            self.assertEqual(len(api.clients), 2)

    def test_replaced_client_identity_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/'fixture.json'; api = FixtureAPI()
            self.create(api, fixture)
            api.clients[-1]['id'] = 'changed-id'
            with self.assertRaises(InstallError):
                cleanup_fixture(api, fixture, 'a'*32)
            self.assertTrue(fixture.exists())
            self.assertEqual(api.clients[-1]['id'], 'changed-id')

    def test_catalog_only_client_is_removed_before_cleanup_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/'fixture.json'; api = FixtureAPI()
            self.create(api, fixture)
            api.clients[:] = [c for c in api.clients if c['email'] == 'admin']
            cleanup_fixture(api, fixture, 'a'*32)
            self.assertEqual(api.catalog, [ADMIN])
            self.assertFalse(fixture.exists())

    def test_retry_after_partial_delete_also_removes_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/'fixture.json'; api = FixtureAPI()
            self.create(api, fixture)
            api.partial_delete = True
            with self.assertRaises(InstallError):
                cleanup_fixture(api, fixture, 'a'*32)
            self.assertEqual(api.clients, [ADMIN])
            self.assertEqual(len(api.catalog), 2)
            self.assertTrue(fixture.exists())
            cleanup_fixture(api, fixture, 'a'*32)
            self.assertEqual(api.catalog, [ADMIN])
            self.assertFalse(fixture.exists())
