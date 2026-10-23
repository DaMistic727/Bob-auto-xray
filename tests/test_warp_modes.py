import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import DEFAULT, MagicMock, patch

from selfsni.core import InstallError
from selfsni.install_state import initialize, validate_resume, warp_mode
from selfsni.routing import server_template
from selfsni import provision
from selfsni.ui import interactive_wizard


def state(mode):
    return dict(domain='vpn.example.com', ipv4='192.0.2.1', ubuntu='26.04',
                codename='resolute', arch='amd64', ssh_ports=[22],
                panel_port=21000, sub_port=22000, panel_path='/panel/',
                sub_path='/sub/', username='user', password='password',
                warp_mode=mode, adopt_warp=mode == 'adopt', bbr_preflight={},
                status='INSTALLING')


def stock_xray():
    return {'outbounds': [{'tag': 'direct', 'protocol': 'freedom'},
                          {'tag': 'blocked', 'protocol': 'blackhole'}],
            'routing': {'rules': [{'inboundTag': ['api'], 'outboundTag': 'api'}]}}


class WarpModeTests(unittest.TestCase):
    def test_skip_bundle_ignores_only_warp_keyring(self):
        from selfsni import core
        original_digest = core.digest
        def digest(path):
            if str(path).endswith('vendor/cloudflare-pubkey.gpg'):
                return '0' * 64
            return original_digest(path)
        with patch.object(core, 'digest', side_effect=digest):
            self.assertEqual(core.verify_bundle('skip')['xui_version'], 'v3.10.2')
            self.assertEqual(core.verify_bundle('adopt')['xui_version'], 'v3.10.2')
            with self.assertRaisesRegex(InstallError, 'cloudflare-pubkey.gpg'):
                core.verify_bundle('install')

    def test_routing_modes_and_idempotence(self):
        for mode in ('install', 'adopt', 'skip'):
            with self.subTest(mode=mode):
                original = stock_xray()
                result = server_template(original, mode)
                self.assertEqual(original, stock_xray())
                self.assertEqual(server_template(result, mode), result)
                self.assertEqual(result['routing']['domainStrategy'], 'IPOnDemand')
                tags = [item['tag'] for item in result['outbounds']]
                rule_tags = [item['outboundTag'] for item in result['routing']['rules']]
                self.assertEqual(tags, ['direct', 'blocked'] +
                                 ([] if mode == 'skip' else ['warp-outbound']))
                self.assertEqual(rule_tags[:4], ['api', 'blocked', 'blocked', 'blocked'])
                self.assertEqual('warp-outbound' in rule_tags, mode != 'skip')
                self.assertNotIn('warp-outbound', json.dumps(result) if mode == 'skip' else '')

    def test_state_modes_and_legacy_resume(self):
        for mode in ('install', 'adopt', 'skip'):
            saved = initialize(state(mode), [])
            saved.update(status='FAILED', stage='CERTIFICATE', last_completed_stage='WARP')
            installation_id = saved['installation_id']
            result = validate_resume(saved, 'vpn.example.com', mode)
            self.assertEqual(result['installation_id'], installation_id)
            self.assertEqual(result['password'], 'password')
            for changed in set(('install', 'adopt', 'skip')) - {mode}:
                with self.assertRaisesRegex(InstallError, 'режим WARP'):
                    validate_resume(copy.deepcopy(saved), 'vpn.example.com', changed)
        old = initialize(state('adopt'), [])
        old.pop('warp_mode')
        old.update(status='FAILED', stage='WARP')
        self.assertEqual(warp_mode(old), 'adopt')
        self.assertEqual(validate_resume(old, 'vpn.example.com', 'adopt')['warp_mode'], 'adopt')

    def test_wizard_all_three_modes(self):
        from argparse import Namespace
        for answer, mode in [('\n', 'install'), ('2\n', 'adopt'), ('3\n', 'skip')]:
            args = Namespace(domain=None, email='admin@example.com',
                             warp_mode=None, adopt_warp=False)
            interactive_wizard(args, 'test', io.StringIO('vpn.example.com\n' + answer + '\n'), io.StringIO())
            self.assertEqual(args.warp_mode, mode)

    def test_cli_flags_are_mutually_exclusive(self):
        with patch('sys.argv', ['install', '--adopt-warp', '--no-warp']), \
             self.assertRaises(SystemExit) as error:
            provision.main()
        self.assertEqual(error.exception.code, 2)

    def test_adopt_is_read_only_and_requires_healthy_proxy(self):
        outputs = iter(['active', '{"status":"Connected"}',
                        '{"settings":{"operation_mode":"proxy","proxy_port":40000}}'])
        with patch.object(provision, 'run', side_effect=lambda *a: next(outputs)) as command, \
             patch('selfsni.acceptance.check_warp') as trace:
            provision.adopt_warp(state('adopt'))
        trace.assert_called_once_with()
        self.assertEqual([call.args for call in command.call_args_list],
                         [('systemctl', 'is-active', 'warp-svc'),
                          ('warp-cli', '--json', 'status'), ('warp-cli', '--json', 'settings')])
        with patch.object(provision, 'run', side_effect=['active', '{"status":"Connected"}',
                                                        '{"settings":{"operation_mode":"proxy","proxy_port":40001}}']), \
             patch('selfsni.acceptance.check_warp') as trace:
            with self.assertRaisesRegex(InstallError, '40000'):
                provision.adopt_warp(state('adopt'))
            trace.assert_not_called()

    def test_skip_has_no_systemd_warp_dependency(self):
        self.assertNotIn('warp-svc', provision.xui_unit_override('skip'))
        self.assertIn('warp-svc.service', provision.xui_unit_override('install'))
        self.assertIn('warp-svc.service', provision.xui_unit_override('adopt'))
        self.assertNotIn('cloudflare-warp', provision.version_packages('skip'))
        self.assertIn('cloudflare-warp', provision.version_packages('install'))

    def test_skip_preflight_accepts_existing_warp_without_tun_or_mutation(self):
        from argparse import Namespace
        args = Namespace(domain='vpn.example.com', ssh_port=None,
                         warp_mode='skip', adopt_warp=False)
        listeners = ('LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd"))\n'
                     'LISTEN 0 128 127.0.0.1:40001 0.0.0.0:* users:(("warp-svc"))')
        def command(*parts, **kwargs):
            if parts[:2] == ('/usr/sbin/sshd', '-T'):
                return 'port 22\n'
            if parts[:3] == ('ss', '-H', '-lntp'):
                return listeners
            if parts[0] == 'curl':
                return '8.8.8.8'
            raise AssertionError(parts)
        with patch.object(provision.os, 'geteuid', return_value=0), \
             patch.object(provision, 'os_info', return_value={'VERSION_ID':'26.04', 'VERSION_CODENAME':'resolute'}), \
             patch.object(provision.platform, 'machine', return_value='x86_64'), \
             patch.object(provision.shutil, 'disk_usage', return_value=type('D', (), {'free':3*1024**3})()), \
             patch.object(provision.shutil, 'which', side_effect=lambda name: '/usr/bin/warp-cli' if name == 'warp-cli' else None), \
             patch.object(provision.Path, 'exists', lambda path: str(path) in
                          ('/run/systemd/system', '/root/VPS-Server-Menu',
                           '/etc/apt/sources.list.d/cloudflare-client.list')), \
             patch.object(provision, 'run', side_effect=command) as commands, \
             patch.object(provision.socket, 'getaddrinfo', return_value=[(None,None,None,None,('8.8.8.8', 80))]), \
             patch.object(provision.socket, 'socket') as sock, \
             patch.object(provision, 'free_port', side_effect=[21000,22000]), \
             patch.object(provision.bbr, 'preflight', return_value={}):
            result = provision.preflight(args)
        self.assertEqual(result['warp_mode'], 'skip')
        self.assertFalse(any(call.args[0][0] in ('warp-cli', 'systemctl') for call in commands.call_args_list))
        sock.return_value.__enter__.return_value.bind.assert_called()

    def test_skip_reaches_certbot_without_warp_calls_and_resumes(self):
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                saved = initialize(state('skip'), []) if resume else None
                if saved:
                    saved.update(status='FAILED', stage='CERTIFICATE', last_completed_stage='WARP', email='')
                    (root / 'state').mkdir()
                    (root / 'state/state.json').write_text(json.dumps(saved))
                apt = MagicMock()
                with patch.object(provision, 'STATE', root/'state'), \
                     patch.object(provision, 'DIAGNOSTIC_LOG', root/'install.log'), \
                     patch.object(provision, 'INSTALL_LOCK', root/'install.lock'), \
                     patch.object(provision, 'CONTROLLED_PATHS', []), \
                     patch.object(provision, 'verify_bundle', return_value={}), \
                     patch.object(provision, 'check_official_menu') as menu, \
                     patch.object(provision, 'preflight', side_effect=lambda args, previous=None:
                                  dict(state('skip'), **({key: previous[key] for key in
                                      ('installation_id', 'schema', 'absent_paths', 'resume_count',
                                       'stage', 'last_completed_stage') if key in previous}
                                      if previous else {}))), \
                     patch.object(provision, 'AptRunner', return_value=apt), \
                     patch.object(provision, 'run'), \
                     patch.object(provision, 'firewall'), \
                     patch.object(provision.bbr, 'configure', return_value={}), \
                     patch.object(provision, 'install_warp') as install, \
                     patch.object(provision, 'adopt_warp') as adopt, \
                     patch.object(provision, 'certificate', side_effect=InstallError('Certbot failed')), \
                     patch('sys.argv', ['install', '--domain', 'vpn.example.com', '--no-warp']), \
                     patch('sys.stdout', io.StringIO()):
                    with self.assertRaises(SystemExit):
                        provision.main()
                menu.assert_not_called()
                install.assert_not_called()
                adopt.assert_not_called()
                result = json.loads((root/'state/state.json').read_text())
                self.assertEqual(result['warp_mode'], 'skip')
                self.assertEqual(result['stage'], 'CERTIFICATE')
                self.assertEqual(result['last_completed_stage'], 'WARP')
                if resume:
                    self.assertEqual(result['installation_id'], saved['installation_id'])
                    self.assertEqual(result['password'], saved['password'])

    def test_clean_skip_completes_without_warp_package_or_menu(self):
        import selfsni.acceptance as acceptance
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(provision, 'STATE', root/'state'), \
                 patch.object(provision, 'DIAGNOSTIC_LOG', root/'install.log'), \
                 patch.object(provision, 'INSTALL_LOCK', root/'install.lock'), \
                 patch.object(provision, 'CONTROLLED_PATHS', []), \
                 patch.object(provision, 'check_official_menu',
                              side_effect=InstallError('несовместимый upstream WARP API')) as menu, \
                 patch.object(provision, 'preflight', return_value=state('skip')), \
                 patch.object(provision, 'AptRunner'), \
                 patch.object(provision, 'run', return_value='versions') as commands, \
                 patch.object(provision, 'firewall'), \
                 patch.object(provision.bbr, 'configure', return_value={}), \
                 patch.object(provision, 'install_warp') as install, \
                 patch.object(provision, 'adopt_warp') as adopt, \
                 patch.multiple(provision, certificate=DEFAULT, decoy=DEFAULT,
                                panel=DEFAULT, security=DEFAULT, renew_hook=DEFAULT), \
                 patch.object(acceptance, 'verify', return_value={'checks':[{'passed':True}]}), \
                 patch('sys.argv', ['install', '--domain', 'vpn.example.com', '--no-warp']), \
                 patch('sys.stdout', io.StringIO()):
                provision.main()
            menu.assert_not_called()
            install.assert_not_called()
            adopt.assert_not_called()
            self.assertEqual([call.args for call in commands.call_args_list],
                             [('dpkg-query', '-W', 'nginx', 'php-fpm', 'certbot', 'fail2ban', 'ufw')])
            saved = json.loads((root/'state/state.json').read_text())
            self.assertEqual(saved['status'], 'HEALTHY_LOCAL')
            self.assertEqual(saved['warp_mode'], 'skip')


if __name__ == '__main__':
    unittest.main()
