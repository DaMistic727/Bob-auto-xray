import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selfsni.core import InstallError, configure_diagnostics
from selfsni import menu_upstream, menu_warp, provision


LOCK = {'menu_repository': menu_upstream.OFFICIAL_REPOSITORY}


class OfficialMenuTests(unittest.TestCase):
    def test_preflight_rejects_dependency_before_state_and_apt(self):
        for mode in ((), ('--adopt-warp',)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / 'state'
                with patch.object(provision, 'STATE', state), \
                     patch.object(provision, 'verify_bundle', return_value=LOCK), \
                     patch.object(provision, 'check_official_menu',
                                  side_effect=InstallError('archive mismatch')), \
                     patch.object(provision, 'AptRunner') as apt, \
                     patch.object(provision, 'InstallerLock') as installer_lock, \
                     patch('sys.argv', ['install', '--domain', 'vpn.example.com', *mode]), \
                     patch('sys.stdout', io.StringIO()):
                    with self.assertRaises(SystemExit):
                        provision.main()
                self.assertFalse(state.exists())
                apt.assert_not_called()
                installer_lock.assert_not_called()


def health(status='HEALTHY', mode='proxy', port=40000, operation=None):
    data = {'status': status, 'reason': 'verified', 'mode': mode, 'port': port}
    if operation is not None:
        data['operation'] = operation
    return json.dumps(data)


class WarpCliTests(unittest.TestCase):
    def test_fresh_install_uses_official_cli_and_independent_health(self):
        outbound = json.dumps({'tag':'warp-outbound', 'protocol':'socks',
                               'settings':{'servers':[{'address':'127.0.0.1',
                                                       'port':40000,'users':[]}]}})
        responses = [(0, health(operation={'success': True, 'exitCode': 0}), ''),
                     (0, health(), ''), (0, outbound, '')]
        with patch.object(menu_warp, '_execute', side_effect=responses) as execute, \
             patch('selfsni.acceptance.check_warp') as check:
            self.assertEqual(menu_warp.install()['status'], 'HEALTHY')
        self.assertEqual([c.args[0] for c in execute.call_args_list],
                         ['install', 'health', 'xray-outbound'])
        check.assert_called_once_with()

    def test_adopt_calls_only_health(self):
        with patch.object(menu_warp, '_execute', return_value=(0, health(), '')) as execute, \
             patch('selfsni.acceptance.check_warp'):
            menu_warp.health()
        execute.assert_called_once_with('health')

    def test_install_operation_failure_even_with_healthy_old_tunnel(self):
        for code, operation in [(1, {'success':False, 'exitCode':1}),
                                (0, {'success':False, 'exitCode':0}),
                                (1, {'success':True, 'exitCode':0})]:
            with self.subTest(code=code, operation=operation), \
                 patch.object(menu_warp, '_execute',
                              return_value=(code, health(operation=operation), '')):
                with self.assertRaises(InstallError):
                    menu_warp.install()

    def test_install_rejects_missing_or_wrong_operation_fields(self):
        for operation in (None, {}, {'success': True, 'exitCode': 1},
                          {'success': 1, 'exitCode': 0}):
            with self.subTest(operation=operation), \
                 patch.object(menu_warp, '_execute',
                              return_value=(0, health(operation=operation), '')):
                with self.assertRaises(InstallError):
                    menu_warp.install()

    def test_provision_install_uses_menu_then_warp_cli(self):
        s = {'warp_mode': 'install'}
        with patch.object(provision, 'install_menu') as menu, \
             patch.object(provision.menu_warp, 'install',
                          return_value={'status': 'HEALTHY'}) as install:
            provision.install_warp(s, LOCK, Path('/tmp'))
        menu.assert_called_once_with(LOCK, Path('/tmp'))
        install.assert_called_once_with()
        self.assertEqual(s['warp_health']['status'], 'HEALTHY')

    def test_exit_codes_and_incompatible_health(self):
        for code, status in [(0, 'HEALTHY'), (1, 'HEALTHY'), (2, 'DEGRADED'),
                             (3, 'DOWN'), (64, 'HEALTHY')]:
            with self.subTest(code=code, status=status), \
                 patch.object(menu_warp, '_execute', return_value=(code, health(status), '')), \
                 patch('selfsni.acceptance.check_warp') as check:
                if code == 0:
                    self.assertEqual(menu_warp.health()['status'], 'HEALTHY')
                    check.assert_called_once()
                else:
                    with self.assertRaises(InstallError):
                        menu_warp.health()
                    check.assert_not_called()
        for mode, port in [('warp', 40000), ('proxy', 40001)]:
            with patch.object(menu_warp, '_execute', return_value=(0, health(mode=mode, port=port), '')):
                with self.assertRaises(InstallError):
                    menu_warp.health()

    def test_malformed_and_extra_stdout_never_succeed(self):
        for output in ('', '{}', 'notice\n' + health(), '[]',
                       '{"status":"HEALTHY","reason":"x","mode":"proxy","port":true}'):
            with self.subTest(output=output), \
                 patch.object(menu_warp, '_execute', return_value=(0, output, '')):
                with self.assertRaises(InstallError):
                    menu_warp.health()

    def test_cli_separates_stdout_and_redacts_diagnostic_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'server-menu'
            script.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"status":"HEALTHY"}\'\n'
                              'printf \'password=supersecret\\n\' >&2\n')
            script.chmod(0o755)
            log = root / 'install.log'
            configure_diagnostics(log)
            try:
                with patch.object(menu_warp, 'CLI', str(script)):
                    code, output, errors = menu_warp._execute('health', timeout=5)
            finally:
                configure_diagnostics()
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)['status'], 'HEALTHY')
            self.assertIn('supersecret', errors)
            self.assertNotIn('supersecret', log.read_text())
            self.assertIn('[REDACTED]', log.read_text())

    def test_cli_timeout_stops_child(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'server-menu'
            script.write_text('#!/bin/sh\nsleep 5\n')
            script.chmod(0o755)
            with patch.object(menu_warp, 'CLI', str(script)), \
                 self.assertRaisesRegex(InstallError, 'тайм-аут'):
                menu_warp._execute('health', timeout=0.1)


if __name__ == '__main__':
    unittest.main()
