import base64
import copy
import json
import unittest
import uuid
from unittest.mock import patch

from selfsni import acceptance, clients
from selfsni.core import InstallError, configure_diagnostics, redact
from selfsni.routing import inbound, server_template, BLOCKED_DOMAINS, BLOCKED_IPS, WARP_DOMAINS
from test_external_acceptance import ADMIN, FixtureAPI, state


class BootstrapAPI(FixtureAPI):
    def __init__(self):
        super().__init__()
        self.clients = []
        self.catalog = []
        self.adds = 0
        self.no_write = False
        self.bad_readback = False

    def call(self, endpoint, data=None):
        if endpoint == 'clients/add':
            self.adds += 1
            if self.no_write:
                return None
        result = super().call(endpoint, data)
        if endpoint == 'clients/add' and self.bad_readback:
            self.catalog[0]['limitHwid'] = 1
        return result


class PermanentClientTests(unittest.TestCase):
    def empty_state(self):
        result = state()
        result.pop('admin_client')
        return result

    def test_create_persists_identity_before_api_and_is_idempotent(self):
        api, s = BootstrapAPI(), self.empty_state()
        saved = []
        def persist(value):
            self.assertEqual(api.adds, 0)
            saved.append(copy.deepcopy(value))
        client = clients.ensure_admin(s, api, persist)
        self.assertEqual(uuid.UUID(client['id']).version, 4)
        self.assertRegex(client['subId'], r'^[a-f0-9]{48}$')
        self.assertEqual(saved[0]['admin_client'], client)
        before = clients.snapshot_clients(api)
        self.assertEqual(clients.ensure_admin(s, api, persist), client)
        self.assertEqual(clients.snapshot_clients(api), before)
        self.assertEqual(api.adds, 1)
        clients.validate_admin(s, *clients.read_clients(api), only_admin=True)
        self.assertNotIn(client['id'], redact(client['id']))
        self.assertNotIn(client['subId'], redact(client['subId']))
        configure_diagnostics()

    def test_timeout_after_commit_is_confirmed_without_second_add(self):
        api, s = BootstrapAPI(), self.empty_state()
        api.lose_add_response = True
        client = clients.ensure_admin(s, api, lambda _: None)
        self.assertEqual(client, api.clients[0])
        clients.ensure_admin(s, api, lambda _: None)
        self.assertEqual(api.adds, 1)

    def test_rejected_or_missing_write_never_reports_success(self):
        for raises in (False, True):
            api, s = BootstrapAPI(), self.empty_state()
            api.no_write = True
            original = api.call
            def call(endpoint, data=None):
                if raises and endpoint == 'clients/add':
                    raise InstallError('secret API response')
                return original(endpoint, data)
            api.call = call
            with self.assertRaisesRegex(InstallError, '^Permanent admin creation was not confirmed'):
                clients.ensure_admin(s, api, lambda _: None)
            self.assertIn('admin_client', s)
            self.assertEqual(api.clients, [])

    def test_bad_record_or_partial_attachment_fails_without_repair(self):
        api, s = BootstrapAPI(), self.empty_state()
        api.bad_readback = True
        with self.assertRaises(InstallError):
            clients.ensure_admin(s, api, lambda _: None)
        before = copy.deepcopy(api.catalog)
        with self.assertRaises(InstallError):
            clients.ensure_admin(s, api, lambda _: None)
        self.assertEqual(api.adds, 1)
        self.assertEqual(api.catalog, before)
        api = FixtureAPI(); api.clients = []
        with self.assertRaises(InstallError):
            clients.ensure_admin(state(), api, lambda _: None)
        self.assertEqual(api.clients, [])

    def test_unowned_existing_admin_is_not_adopted_or_replaced(self):
        api = FixtureAPI(); before = clients.snapshot_clients(api)
        with self.assertRaisesRegex(InstallError, 'Unowned admin'):
            clients.ensure_admin(self.empty_state(), api, lambda _: None)
        self.assertEqual(before, clients.snapshot_clients(api))

    def test_invalid_limits_identity_duplicates_and_inbound_remark_are_rejected(self):
        for field, value in [('enable', False), ('flow', ''), ('totalGB', 1),
                             ('limitIp', 1), ('expiryTime', 1), ('reset', 1),
                             ('id', str(uuid.uuid1())), ('subId', 'short')]:
            api = FixtureAPI(); api.clients[0][field] = value
            with self.subTest(field=field), self.assertRaises(InstallError):
                clients.validate_admin(state(), *clients.read_clients(api))
        api = FixtureAPI(); api.clients.append(dict(ADMIN, email='other'))
        with self.assertRaisesRegex(InstallError, 'unique'):
            clients.validate_admin(state(), *clients.read_clients(api))
        rows, catalog = clients.read_clients(FixtureAPI()); rows[0]['remark'] = 'old name'
        with self.assertRaisesRegex(InstallError, 'remark'):
            clients.validate_admin(state(), rows, catalog)

    def test_catalog_flow_can_be_empty_but_attachment_vision_is_required(self):
        api = FixtureAPI(); api.catalog[0]['flow'] = ''
        clients.validate_admin(state(), *clients.read_clients(api))
        api.clients[0]['flow'] = ''
        with self.assertRaises(InstallError):
            clients.validate_admin(state(), *clients.read_clients(api))

    def test_api_save_preserves_admin_and_remark(self):
        api = FixtureAPI()
        original_call = api.call
        def call(endpoint, data=None):
            if endpoint == 'inbounds/update/1':
                self.assertEqual(data['remark'], 'vless by bob')
                api.clients = json.loads(json.dumps(data['settings']))['clients']
                return None
            return original_call(endpoint, data)
        api.call = call
        rows, catalog = clients.read_clients(api)
        row = rows[0]; row['settings'] = json.loads(row['settings'])
        api.call('inbounds/update/1', row)
        clients.validate_admin(state(), *clients.read_clients(api), only_admin=True)

    def test_runtime_and_both_subscription_identities_are_checked(self):
        config = {'inbounds': [{'port': 443, 'protocol': 'vless', 'settings': {'clients': [dict(ADMIN)]}}]}
        clients.verify_admin_runtime(state(), config)
        config['inbounds'][0]['settings']['clients'][0]['flow'] = ''
        with self.assertRaises(InstallError):
            clients.verify_admin_runtime(state(), config)
        calls = []
        link = ('vless://'+ADMIN['id']+'@panel.test:443?flow=xtls-rprx-vision&fp=firefox').encode()
        def send(*args, **kwargs):
            calls.append((args, kwargs))
            return 200, {}, base64.b64encode(link)
        clients.verify_admin_subscriptions(state(), send)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[0][-1].endswith(ADMIN['subId']) for call in calls))
        for result in ((403, {}, b''), (200, {}, b'vless://wrong@host?flow=xtls-rprx-vision')):
            with self.assertRaises(InstallError):
                clients.verify_admin_subscriptions(state(), lambda *a, **kw: result)

    def test_onhost_e2e_failures_delete_only_owned_temporary_client(self):
        for lost_response, restart_failure in ((True, False), (False, True), (False, False)):
            api = FixtureAPI(); api.lose_add_response = lost_response
            protected = clients.snapshot_clients(api)
            with self.subTest(lost=lost_response, restart=restart_failure), \
                 patch.object(acceptance, 'Panel', return_value=api), \
                 patch.object(acceptance, 'run', side_effect=InstallError('restart failed') if restart_failure else None), \
                 patch.object(acceptance, 'wait_for'), \
                 patch.object(acceptance, 'request', return_value=(503, {}, b'')):
                with self.assertRaises(InstallError):
                    acceptance.e2e(state())
            self.assertEqual(clients.snapshot_clients(api), protected)

    def test_cleanup_detects_admin_mutation_and_never_deletes_admin(self):
        api = FixtureAPI(); protected = clients.snapshot_clients(api)
        temporary = dict(ADMIN, email='selfsni-e2e-test', id=str(uuid.uuid4()), subId='c'*48)
        api.call('clients/add', {'client': temporary})
        api.clients[0]['enable'] = False
        with self.assertRaisesRegex(InstallError, 'Permanent clients changed'):
            clients.cleanup_owned_client(api, temporary, protected)
        self.assertEqual(len(api.clients), 1)
        with self.assertRaisesRegex(InstallError, 'non-test'):
            clients.cleanup_owned_client(api, ADMIN)


class RoutingRegressionTests(unittest.TestCase):
    def test_only_inbound_remark_changes_against_v035_contract(self):
        row = inbound('test.example', 'private', 'public', 'abcd')
        self.assertEqual(row['remark'], 'vless by bob')
        self.assertEqual(row['tag'], 'inbound-443')
        self.assertEqual(row['port'], 443)
        reality = json.loads(row['streamSettings'])['realitySettings']
        self.assertEqual((reality['target'], reality['xver']), ('/run/selfsni/nginx.sock', 1))
        self.assertEqual(reality['settings']['fingerprint'], 'firefox')

    def test_global_and_ru_ads_precede_warp_for_all_modes_and_survive_save(self):
        source = {'outbounds': [{'tag': 'direct', 'protocol': 'freedom'},
                                {'tag': 'blocked', 'protocol': 'blackhole'}],
                  'routing': {'rules': [{'inboundTag': ['api'], 'outboundTag': 'api'}]}}
        for mode in ('install', 'adopt', 'skip'):
            result = server_template(source, mode)
            rules = result['routing']['rules']
            block = next(r for r in rules if r.get('domain') == BLOCKED_DOMAINS)
            self.assertIn('geosite:category-ads-all', block['domain'])
            self.assertIn('ext:geosite_RU.dat:category-ads-all', block['domain'])
            self.assertEqual(block['outboundTag'], 'blocked')
            self.assertEqual(rules[1]['ip'], BLOCKED_IPS)
            self.assertEqual(rules[2]['protocol'], ['bittorrent'])
            self.assertEqual(result['routing']['domainStrategy'], 'IPOnDemand')
            if mode != 'skip':
                warp = next(r for r in rules if r.get('domain') == WARP_DOMAINS)
                self.assertLess(rules.index(block), rules.index(warp))
            # Same serialization boundary as the 3x-ui xray/update API.
            persisted = json.loads(json.dumps({'xraySetting': json.dumps(result)}))
            self.assertEqual(server_template(json.loads(persisted['xraySetting']), mode), result)
