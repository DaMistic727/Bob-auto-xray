import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selfsni.core import InstallError
from selfsni.menu_upstream import (OFFICIAL_REPOSITORY, UNSAFE_MAIN,
                                   check_official_menu, pinned_menu_archive)


PIN = 'a' * 40
LOCK = {'menu_repository': OFFICIAL_REPOSITORY, 'menu_commit': PIN,
        'menu_archive_sha256': 'b' * 64, 'downloads': {}}


class OfficialMenuTests(unittest.TestCase):
    def test_unmerged_upstream_is_rejected_before_mutation(self):
        calls = []
        def command(*args, **kwargs):
            calls.append((args, kwargs))
            return UNSAFE_MAIN + '\trefs/heads/main\n'
        with self.assertRaisesRegex(InstallError, 'pinned commit'):
            check_official_menu(LOCK, command)
        self.assertEqual(calls[0][0], ('git', 'ls-remote', OFFICIAL_REPOSITORY,
                                       'refs/heads/main'))
        self.assertEqual(calls[0][1]['timeout'], 45)

    def test_floating_or_forked_source_is_rejected(self):
        for changed in ({'menu_repository': 'https://github.com/other/menu.git'},
                        {'menu_branch': 'main'},
                        {'menu_installer_url': 'https://raw.githubusercontent.com/Diman6267/VPS-Server-Menu/main/install.sh'},
                        {'downloads': {'menu.tar.gz': {'url': 'https://example.com'}}}):
            with self.subTest(changed=changed):
                lock = dict(LOCK, **changed)
                with patch('selfsni.menu_upstream.run') as command, \
                     self.assertRaisesRegex(InstallError, 'официальный VPS-Server-Menu'):
                    check_official_menu(lock, command)
                command.assert_not_called()

    def test_pin_and_digest_must_be_reviewed_after_merge(self):
        for changed in ({'menu_commit': None}, {'menu_archive_sha256': None},
                        {'menu_commit': UNSAFE_MAIN}, {'menu_archive_sha256': 'wrong'}):
            with self.subTest(changed=changed):
                with self.assertRaises(InstallError):
                    check_official_menu(dict(LOCK, **changed), lambda *args, **kwargs: self.fail('network called'))
        self.assertEqual(pinned_menu_archive(LOCK),
                         (f'https://github.com/Diman6267/VPS-Server-Menu/archive/{PIN}.tar.gz', 'b' * 64))
        fetched = []
        self.assertEqual(check_official_menu(
            LOCK, lambda *args, **kwargs: PIN + '\trefs/heads/main\n',
            lambda url, path, checksum: fetched.append((url, checksum))), PIN)
        self.assertEqual(fetched, [(pinned_menu_archive(LOCK)[0], 'b' * 64)])
        def bad_archive(*_):
            raise InstallError('SHA256 mismatch')
        with self.assertRaisesRegex(InstallError, 'SHA256 mismatch'):
            check_official_menu(LOCK, lambda *args, **kwargs: PIN + '\trefs/heads/main\n', bad_archive)

    def test_provisioning_stops_before_state_or_apt(self):
        import selfsni.provision as provision
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            output = io.StringIO()
            with patch.object(provision, 'STATE', state), \
                 patch.object(provision, 'verify_bundle', return_value=LOCK), \
                 patch.object(provision, 'check_official_menu',
                              side_effect=InstallError('pinned revision отсутствует')), \
                 patch.object(provision, 'AptRunner') as apt, \
                 patch.object(provision, 'InstallerLock') as installer_lock, \
                 patch('sys.argv', ['install', '--domain', 'vpn.example.com']), \
                 patch('sys.stdout', output):
                with self.assertRaises(SystemExit):
                    provision.main()
            self.assertFalse(state.exists())
            apt.assert_not_called()
            installer_lock.assert_not_called()
            self.assertIn('pinned revision отсутствует', output.getvalue())


if __name__ == '__main__':
    unittest.main()
