import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selfsni.core import InstallError, configure_diagnostics
from selfsni import menu_upstream, menu_warp, provision


PIN = menu_upstream.SUPPORTED_COMMIT
SHA = menu_upstream.SUPPORTED_SHA256
LOCK = {'menu_repository': menu_upstream.OFFICIAL_REPOSITORY, 'menu_commit': PIN,
        'menu_archive_sha256': SHA, 'downloads': {}}


def source_at(path):
    path.mkdir(parents=True)
    for name in ('install.sh', 'server-menu', 'menu_warp.sh'):
        (path / name).write_text(name + '\n')
    return path


def git(*args):
    return subprocess.check_output(['git', *map(str, args)], text=True)


def git_command(*args, **kwargs):
    assert args[0] == 'git'
    return git(*args[1:])


class OfficialMenuTests(unittest.TestCase):
    def test_official_pin_and_archive_url(self):
        self.assertEqual(menu_upstream.pinned_menu_archive(LOCK),
                         (f'https://codeload.github.com/Diman6267/VPS-Server-Menu/tar.gz/{PIN}', SHA))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = source_at(root / 'source')
            with patch.object(menu_upstream, '_archive_source', return_value=source), \
                 patch.object(menu_upstream, 'BIN_DIR', root / 'bin'):
                self.assertEqual(menu_upstream.check_official_menu(LOCK, target=root / 'absent'), PIN)

    def test_fork_floating_commit_and_checksum_rejected_without_network(self):
        changes = ({'menu_repository': 'https://github.com/other/menu.git'},
                   {'menu_branch': 'main'}, {'menu_installer_url': 'https://example.com/install.sh'},
                   {'downloads': {'menu.tar.gz': {}}}, {'menu_commit': '0' * 40},
                   {'menu_archive_sha256': '0' * 64}, {'menu_commit': None})
        for change in changes:
            with self.subTest(change=change), \
                 patch.object(menu_upstream, '_archive_source') as fetch:
                with self.assertRaises(InstallError):
                    menu_upstream.check_official_menu(dict(LOCK, **change))
                fetch.assert_not_called()

    def test_wrong_download_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            def fetch(url, path, expected):
                self.assertEqual(expected, SHA)
                Path(path).write_bytes(b'not official archive')
            with self.assertRaisesRegex(InstallError, 'SHA256'):
                menu_upstream.check_official_menu(LOCK, fetch=fetch,
                                                   target=Path(directory) / 'absent')

    def _checkout(self, root):
        target = source_at(root / 'menu')
        git('init', target)
        git('-C', target, 'remote', 'add', 'origin', menu_upstream.OFFICIAL_REPOSITORY)
        git('-C', target, 'add', '.')
        git('-C', target, '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
            'commit', '-m', 'fixture')
        return target, git('-C', target, 'rev-parse', 'HEAD').strip()

    def test_existing_checkout_origin_head_dirty_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, commit = self._checkout(root)
            with patch.object(menu_upstream, 'SUPPORTED_COMMIT', commit):
                lock = dict(LOCK, menu_commit=commit)
                calls = []
                def inspect_command(*args, **kwargs):
                    calls.append(args)
                    return git_command(*args, **kwargs)
                self.assertTrue(menu_upstream.inspect_existing_menu(lock, target, inspect_command))
                self.assertTrue(any(x[3:6] == ('config', '--local', '--get-all') for x in calls))
                self.assertTrue(any('--no-optional-locks' in x and
                                    'core.fsmonitor=false' in x for x in calls))
                git('-C', target, 'remote', 'set-url', 'origin', 'https://github.com/fork/menu.git')
                with self.assertRaisesRegex(InstallError, 'origin'):
                    menu_upstream.inspect_existing_menu(lock, target, git_command)
                git('-C', target, 'remote', 'set-url', 'origin', menu_upstream.OFFICIAL_REPOSITORY)
                with patch.object(menu_upstream, 'SUPPORTED_COMMIT', '0' * 40), \
                     self.assertRaises(InstallError):
                    menu_upstream.inspect_existing_menu(dict(lock, menu_commit='0' * 40),
                                                        target, git_command)
                (target / 'menu_warp.sh').write_text('dirty\n')
                with self.assertRaisesRegex(InstallError, 'дерево изменено'):
                    menu_upstream.inspect_existing_menu(lock, target, git_command)
                git('-C', target, 'checkout', '--', 'menu_warp.sh')
                (target / 'extra').symlink_to('install.sh')
                with self.assertRaises(InstallError):
                    menu_upstream.inspect_existing_menu(lock, target, git_command)
                (target / 'extra').unlink()
                alternate = root / 'link'
                alternate.symlink_to(target)
                with self.assertRaisesRegex(InstallError, 'symlink'):
                    menu_upstream.inspect_existing_menu(lock, alternate, git_command)

    def test_existing_checkout_is_not_replaced_and_bashrc_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, commit = self._checkout(root)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            bashrc = root / '.bashrc'
            bashrc.write_text('existing user settings\n')
            calls = []
            def command(*args, **kwargs):
                calls.append(tuple(map(str, args)))
                if args[0] == 'bash':
                    self.assertEqual(list(map(str, args[2:])),
                                     ['--non-interactive', '--no-autostart', '--source-dir',
                                      str(target), '--pinned-commit', commit])
                    for name in ('install.sh', 'server-menu', 'menu_warp.sh'):
                        (bin_dir / name).symlink_to(target / name)
                    return ''
                return git_command(*args, **kwargs)
            with patch.object(menu_upstream, 'SUPPORTED_COMMIT', commit), \
                 patch.object(menu_upstream, '_archive_source', return_value=target):
                lock = dict(LOCK, menu_commit=commit)
                menu_upstream.install_menu(lock, root, target, bin_dir, command)
                menu_upstream.install_menu(lock, root, target, bin_dir, command)
            self.assertEqual(sum(row[0] == 'bash' for row in calls), 1)
            self.assertEqual(bashrc.read_text(), 'existing user settings\n')
            self.assertTrue((target / '.git').is_dir())

    def test_conflicting_command_does_not_touch_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, commit = self._checkout(root)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            (bin_dir / 'server-menu').write_text('foreign')
            with patch.object(menu_upstream, 'SUPPORTED_COMMIT', commit), \
                 patch.object(menu_upstream, '_archive_source', return_value=target), \
                 self.assertRaisesRegex(InstallError, 'Посторонняя команда'):
                menu_upstream.install_menu(dict(LOCK, menu_commit=commit), root, target,
                                           bin_dir, git_command)
            self.assertEqual((bin_dir / 'server-menu').read_text(), 'foreign')

    def test_fresh_checkout_fetches_pin_and_installs_official_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, commit = self._checkout(root)
            target = root / 'newmenu'
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            calls = []
            def command(*args, **kwargs):
                calls.append(tuple(map(str, args)))
                if args[:2] == ('git', '-C') and 'fetch' in args:
                    return git('-C', args[2], 'fetch', '--depth=1', source, commit)
                if args[0] == 'bash':
                    for name in ('install.sh', 'server-menu', 'menu_warp.sh'):
                        (bin_dir / name).symlink_to(target / name)
                    return ''
                return git_command(*args, **kwargs)
            with patch.object(menu_upstream, 'SUPPORTED_COMMIT', commit), \
                 patch.object(menu_upstream, '_archive_source', return_value=source):
                menu_upstream.install_menu(dict(LOCK, menu_commit=commit), root, target,
                                           bin_dir, command)
            self.assertEqual(git('-C', target, 'rev-parse', 'HEAD').strip(), commit)
            self.assertEqual(git('-C', target, 'remote', 'get-url', 'origin').strip(),
                             menu_upstream.OFFICIAL_REPOSITORY)
            self.assertTrue((target / '.git').is_dir())
            self.assertEqual([x for x in calls if x[0] == 'bash'][0][1],
                             str(target / 'install.sh'))

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
