"""Pinned CLI exit-zero write failures must not open the bootstrap gate."""
from pathlib import Path
import shlex
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from selfsni.core import InstallError
from selfsni.provision import panel, verify_panel_bootstrap, xui_unit_override


CERT = '/etc/letsencrypt/live/panel.test/'
VALID = 'listenIP: 127.0.0.1\ncert: '+CERT+'fullchain.pem\nkey: '+CERT+'privkey.pem\nport: 21000\nwebBasePath: /'+'a'*48+'/\nhasDefaultCredential: false\n'


class BootstrapChecks(unittest.TestCase):
    def test_missing_wrong_or_duplicate_settings_are_rejected_before_tls_load(self):
        for output in ('database error', VALID.replace('127.0.0.1', '0.0.0.0'),
                       VALID.replace('cert: '+CERT+'fullchain.pem', 'cert: /wrong.pem'),
                       VALID.replace('key: '+CERT+'privkey.pem', 'key: '),
                       VALID+'listenIP: 127.0.0.1\n',
                       VALID.replace('port: 21000', 'port: 54321'),
                       VALID.replace('/'+'a'*48+'/', '/unexpected/'),
                       VALID.replace('hasDefaultCredential: false', 'hasDefaultCredential: true')):
            with self.subTest(output=output), patch('selfsni.provision.run', return_value=output), \
                 patch('selfsni.provision.ssl.SSLContext') as ctx:
                with self.assertRaises(InstallError):
                    verify_panel_bootstrap({'domain': 'panel.test', 'panel_port': 21000, 'panel_path': '/'+'a'*48+'/'})
                ctx.assert_not_called()

    def test_readback_is_followed_by_real_certificate_pair_validation(self):
        with patch('selfsni.provision.run', return_value=VALID), \
             patch('selfsni.provision.ssl.SSLContext') as ctx:
            verify_panel_bootstrap({'domain': 'panel.test', 'panel_port': 21000, 'panel_path': '/'+'a'*48+'/'})
            ctx.return_value.load_cert_chain.assert_called_once_with(CERT+'fullchain.pem', CERT+'privkey.pem')
            ctx.return_value.load_cert_chain.side_effect = ssl.SSLError('private key detail')
            with self.assertRaisesRegex(InstallError, '^Panel bootstrap TLS certificate/key validation failed$'):
                verify_panel_bootstrap({'domain': 'panel.test', 'panel_port': 21000, 'panel_path': '/'+'a'*48+'/'})

    def test_zero_exit_cli_write_failure_does_not_create_marker_or_start_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = dict(domain='panel.test', arch='amd64', ipv4='192.0.2.1',
                         panel_port=21000, panel_path='/'+'a'*48+'/',
                         username='user', password='password', warp_mode='skip')
            lock = {'xui_version': 'v3.10.2', 'downloads': {'x-ui-linux-amd64.tar.gz':
                    {'url': 'https://example.com/archive', 'sha256': '0'*64}}}
            writes, calls = [], []
            def run(*args, **kwargs):
                calls.append(args)
                if '-getListen' in args:
                    return VALID.replace('127.0.0.1', '0.0.0.0')
                return ''  # Including the settings write that silently failed.
            with patch('selfsni.provision.STATE', root/'state'), \
                 patch('selfsni.provision.download', return_value=root/'archive'), \
                 patch('selfsni.provision.write', side_effect=lambda p, *_: writes.append(str(p))), \
                 patch('selfsni.provision.save'), patch('selfsni.provision.run', side_effect=run), \
                 patch('selfsni.provision.install_pinned_xray', return_value='verified'):
                with self.assertRaises(InstallError):
                    panel(state, lock, root)
            self.assertNotIn(str(root/'state/panel-ready'), writes)
            self.assertNotIn(('systemctl', 'restart', 'x-ui'), calls)

    def test_restart_readback_shell_blocks_silent_cli_failures(self):
        line = next(line for line in xui_unit_override('skip').splitlines()
                    if line.startswith('ExecStartPre=/bin/sh'))
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory)/'x-ui'
            for output, success in (('listenIP: 127.0.0.1', True), ('listenIP: 0.0.0.0', False), ('', False)):
                cli.write_text('#!/bin/sh\nprintf "%s\\n" '+shlex.quote(output)+'\nexit 0\n')
                cli.chmod(0o755)
                command = line.removeprefix('ExecStartPre=').replace('/usr/local/x-ui/x-ui', str(cli))
                result = subprocess.run(shlex.split(command), capture_output=True, timeout=5)
                self.assertEqual(result.returncode == 0, success)
