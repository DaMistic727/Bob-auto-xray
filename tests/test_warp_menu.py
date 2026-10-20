import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selfsni.core import InstallError
from selfsni.menu_upstream import (OFFICIAL_INSTALLER, OFFICIAL_REPOSITORY,
                                   UNSAFE_MAIN, check_official_menu)


LOCK = {'menu_repository': OFFICIAL_REPOSITORY, 'menu_branch': 'main',
        'menu_installer_url': OFFICIAL_INSTALLER, 'downloads': {}}


class OfficialMenuTests(unittest.TestCase):
    def test_current_official_main_is_rejected_before_mutation(self):
        calls = []
        def command(*args, **kwargs):
            calls.append((args, kwargs))
            return UNSAFE_MAIN + '\trefs/heads/main\n'
        with self.assertRaisesRegex(InstallError, r'cron restart.*интерактивное'):
            check_official_menu(LOCK, command)
        self.assertEqual(calls[0][0], ('git', 'ls-remote', OFFICIAL_REPOSITORY,
                                       'refs/heads/main'))
        self.assertEqual(calls[0][1]['timeout'], 45)

    def test_fork_or_pinned_archive_is_rejected(self):
        for changed in ({'menu_repository': 'https://github.com/other/menu.git'},
                        {'menu_commit': 'a' * 40},
                        {'downloads': {'menu.tar.gz': {'url': 'https://example.com'}}}):
            with self.subTest(changed=changed):
                lock = dict(LOCK, **changed)
                with patch('selfsni.menu_upstream.run') as command, \
                     self.assertRaisesRegex(InstallError, 'официальный main'):
                    check_official_menu(lock, command)
                command.assert_not_called()

    def test_new_unknown_main_is_not_assumed_compatible(self):
        with self.assertRaisesRegex(InstallError, 'ещё не проверен'):
            check_official_menu(LOCK, lambda *args, **kwargs: 'b' * 40 + '\trefs/heads/main\n')

    def test_provisioning_stops_before_state_or_apt(self):
        import selfsni.provision as provision
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            output = io.StringIO()
            with patch.object(provision, 'STATE', state), \
                 patch.object(provision, 'verify_bundle', return_value=LOCK), \
                 patch.object(provision, 'check_official_menu',
                              side_effect=InstallError('официальный main несовместим')), \
                 patch.object(provision, 'AptRunner') as apt, \
                 patch.object(provision, 'InstallerLock') as installer_lock, \
                 patch('sys.argv', ['install', '--domain', 'vpn.example.com']), \
                 patch('sys.stdout', output):
                with self.assertRaises(SystemExit):
                    provision.main()
            self.assertFalse(state.exists())
            apt.assert_not_called()
            installer_lock.assert_not_called()
            self.assertIn('официальный main несовместим', output.getvalue())


if __name__ == '__main__':
    unittest.main()
