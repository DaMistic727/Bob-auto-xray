import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from selfsni.core import InstallError, verify_bundle
from selfsni.xray_pin import XRAY_ARCHIVES, install_pinned_xray, pinned_xray_spec


class XrayPinTests(unittest.TestCase):
    def test_official_lock_keeps_panel_at_3102(self):
        lock = verify_bundle('skip')
        self.assertEqual(lock['xui_version'], 'v3.10.2')
        self.assertEqual(lock['xray_version'], 'v26.10.21')
        for arch in XRAY_ARCHIVES:
            self.assertIn('XTLS/Xray-core/releases/download/v26.10.21/',
                          pinned_xray_spec(lock, arch)['url'])

    def test_verified_binary_replaces_stopped_panel_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root/'official.zip'
            payload = b'verified xray binary'
            member = zipfile.ZipInfo('xray')
            member.external_attr = (0o100755 << 16)
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr(member, payload)
            lock = {'xray_version': 'v26.10.21', 'xray_downloads': {'amd64': {
                'url': 'https://github.com/XTLS/Xray-core/releases/download/v26.10.21/Xray-linux-64.zip',
                'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'binary_sha256': hashlib.sha256(payload).hexdigest(),
            }}}
            destination = root/'xray-linux-amd64'
            destination.write_bytes(b'withdrawn binary')
            with patch('selfsni.xray_pin.download', return_value=archive) as fetch, \
                 patch('selfsni.xray_pin.run', side_effect=['', 'Xray 26.10.21 1b1647c']) as command:
                result = install_pinned_xray(lock, 'amd64', root, destination)
            self.assertEqual(result, lock['xray_downloads']['amd64']['binary_sha256'])
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o755)
            fetch.assert_called_once_with(lock['xray_downloads']['amd64']['url'],
                                          root/'Xray-linux-64.zip',
                                          lock['xray_downloads']['amd64']['sha256'])
            self.assertEqual(command.call_args_list[0].args, ('systemctl', 'stop', 'x-ui'))
            self.assertEqual(command.call_args_list[1].args, (str(destination), 'version'))

    def test_fork_or_wrong_binary_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root/'official.zip'
            member = zipfile.ZipInfo('xray')
            member.external_attr = (0o100755 << 16)
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr(member, b'actual')
            spec = {'url': 'https://github.com/XTLS/Xray-core/releases/download/v26.10.21/Xray-linux-64.zip',
                    'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                    'binary_sha256': hashlib.sha256(b'expected').hexdigest()}
            lock = {'xray_version': 'v26.10.21', 'xray_downloads': {'amd64': spec}}
            destination = root/'xray-linux-amd64'
            destination.write_bytes(b'withdrawn binary')
            with patch('selfsni.xray_pin.download', return_value=archive), \
                 patch('selfsni.xray_pin.run', return_value='') as command:
                with self.assertRaisesRegex(InstallError, 'binary SHA256'):
                    install_pinned_xray(lock, 'amd64', root, destination)
            self.assertEqual(destination.read_bytes(), b'withdrawn binary')
            self.assertEqual([call.args for call in command.call_args_list],
                             [('systemctl', 'stop', 'x-ui')])
            self.assertFalse(list(root.glob('.xray-*')))
            spec['url'] = 'https://github.com/other/Xray-core/releases/download/v26.10.21/Xray-linux-64.zip'
            with self.assertRaisesRegex(InstallError, 'pinned official release'):
                pinned_xray_spec(lock, 'amd64')


if __name__ == '__main__':
    unittest.main()
