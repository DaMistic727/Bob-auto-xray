import json
from pathlib import Path
import tempfile
import unittest

from selfsni.core import InstallError
from selfsni.install_state import (InstallerLock, fail, initialize, load,
                                   upgrade_legacy_apt_failure, validate_resume)


def values():
    return {'domain':'vpn.example.com','adopt_warp':False,'username':'user',
            'password':'secret','panel_port':21000,'sub_port':22000,
            'panel_path':'/panel/','sub_path':'/sub/'}


class StateTests(unittest.TestCase):
    def test_credentials_survive_apt_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            state = initialize(values(), ['/etc/nginx'])
            state['stage'] = 'APT_INSTALL'
            fail(directory, state, 'apt failed')
            resumed = validate_resume(load(directory), 'vpn.example.com', False)
            self.assertEqual(resumed['password'], 'secret')
            self.assertEqual(resumed['panel_path'], '/panel/')
            self.assertEqual(resumed['resume_count'], 1)

    def test_healthy_and_late_failure_are_not_resumed(self):
        healthy = initialize(values(), [])
        healthy['status'] = 'HEALTHY_LOCAL'
        with self.assertRaises(InstallError):
            validate_resume(healthy, 'vpn.example.com', False)
        late = initialize(values(), [])
        late.update(status='FAILED', stage='PANEL')
        with self.assertRaises(InstallError):
            validate_resume(late, 'vpn.example.com', False)

    def test_warp_reconciliation_is_resumable(self):
        state = initialize(values(), ['/root/VPS-Server-Menu'])
        state.update(status='FAILED', stage='WARP')
        self.assertEqual(validate_resume(state, 'vpn.example.com', False)['stage'], 'WARP')

    def test_certificate_retry_is_resumable(self):
        state = initialize(values(), ['/etc/letsencrypt'])
        state.update(status='FAILED', stage='CERTIFICATE', last_completed_stage='WARP')
        self.assertEqual(validate_resume(state, 'vpn.example.com', False)['stage'], 'CERTIFICATE')

    def test_exact_legacy_v033_apt_failure_is_migrated_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = values()
            legacy['status'] = 'INSTALLING'
            (root/'FAILED').write_text('apt-get exited 100\n')
            (root/'progress.log').write_text(
                '2026-10-19T05:01:31Z Install distribution packages\n'
                '2026-10-19T05:01:36Z FAILED: apt-get exited 100\n')
            managed = root/'managed'
            migrated = upgrade_legacy_apt_failure(legacy, root, [managed])
            self.assertEqual(migrated['stage'], 'APT_UPDATE')
            self.assertEqual(migrated['legacy_resume'], 'v0.3.3-apt-exit-100')
            self.assertEqual(migrated['password'], 'secret')
            self.assertFalse((root/'state.json').exists())

    def test_legacy_migration_rejects_any_managed_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); managed = root/'managed'; managed.mkdir()
            legacy = values(); legacy['status'] = 'INSTALLING'
            (root/'FAILED').write_text('apt-get exited 100\n')
            (root/'progress.log').write_text(
                '2026-10-19T05:01:31Z Install distribution packages\n'
                '2026-10-19T05:01:36Z FAILED: apt-get exited 100\n')
            with self.assertRaises(InstallError):
                upgrade_legacy_apt_failure(legacy, root, [managed])

    def test_different_invocation_is_rejected(self):
        state = initialize(values(), [])
        state.update(status='FAILED', stage='APT_UPDATE')
        with self.assertRaises(InstallError):
            validate_resume(state, 'other.example.com', False)

    def test_concurrent_installer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'install.lock'
            with InstallerLock(path):
                with self.assertRaises(InstallError):
                    with InstallerLock(path):
                        pass


if __name__ == '__main__':
    unittest.main()
