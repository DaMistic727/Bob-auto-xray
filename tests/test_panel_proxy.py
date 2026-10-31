"""Isolation and bootstrap regression tests; live TLS checks are in panel_checks."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from selfsni.api import Panel, public_panel_port, public_panel_url
from selfsni.provision import firewall, panel, render, xui_unit_override
from selfsni.ui import InstallerUI
from selfsni.core import InstallError


def state():
    return dict(domain='vpn.example.com', panel_port=21000, sub_port=22000,
                panel_path='/'+'a'*48+'/', panel_access='https-443',
                ssh_ports=[22, 2222], arch='amd64', ipv4='192.0.2.1',
                username='user', password='password', warp_mode='skip')


class PanelProxyTests(unittest.TestCase):
    def test_public_url_and_internal_api_are_distinct(self):
        s = state()
        self.assertEqual(public_panel_url(s), 'https://vpn.example.com'+s['panel_path'])
        self.assertEqual(public_panel_port(s), 443)
        s['api_token'] = 'secret'
        with patch('selfsni.api.request', return_value=(200, {}, b'{"success":true,"obj":[]}')) as send:
            self.assertEqual(Panel(s).call('inbounds/list'), [])
        self.assertEqual(send.call_args.args[:3],
                         (s['domain'], 21000, s['panel_path']+'panel/api/inbounds/list'))

    def test_legacy_state_keeps_old_url(self):
        s = state(); del s['panel_access']
        self.assertEqual(public_panel_port(s), 21000)
        self.assertIn(':21000/', public_panel_url(s))
        self.assertNotIn('panel_access', s)

    def test_result_uses_443_without_internal_port(self):
        output = io.StringIO()
        InstallerUI(output, tty=False).success(state())
        self.assertIn(public_panel_url(state()), output.getvalue())
        self.assertNotIn(':21000', output.getvalue())

    def test_firewall_never_allows_internal_port(self):
        for public in (False, True):
            with self.subTest(public=public), patch('selfsni.provision.run') as run:
                firewall(state(), public=public)
                calls = [call.args for call in run.call_args_list]
                self.assertNotIn(('ufw', 'allow', '21000/tcp'), calls)
                self.assertEqual(('ufw', 'allow', '22000/tcp') in calls, public)
                self.assertIn(('ufw', 'default', 'deny', 'incoming'), calls)

    def test_proxy_retains_uri_and_verifies_tls(self):
        conf = render('nginx-panel.conf', {'DOMAIN': 'vpn.example.com', 'PANEL_PORT': 21000})
        self.assertIn('proxy_pass https://127.0.0.1:21000;', conf)
        self.assertIn('proxy_ssl_verify on;', conf)
        self.assertIn('proxy_ssl_name vpn.example.com;', conf)
        self.assertIn('proxy_ssl_server_name on;', conf)
        self.assertNotIn('proxy_ssl_verify off', conf)
        self.assertIn('proxy_set_header X-Forwarded-For $remote_addr;', conf)
        self.assertNotIn('$proxy_add_x_forwarded_for', conf)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', conf)
        self.assertIn('client_max_body_size 64m;', conf)

    def test_only_secret_prefix_routes_to_proxy(self):
        s = state()
        values = dict(DOMAIN=s['domain'], PANEL_PATH=s['panel_path'],
                      PANEL_NO_SLASH=s['panel_path'][:-1], AUTH='auth', STYLE='style', ICON='icon')
        conf = render('nginx-tls.conf', values)
        self.assertEqual(conf.count('listen '), 1)
        self.assertIn('listen unix:/run/selfsni/nginx.sock ssl http2 proxy_protocol;', conf)
        self.assertIn('location ^~ '+s['panel_path'], conf)
        self.assertIn('location = '+s['panel_path']+'login', conf)
        self.assertIn('location / { return 404; }', conf)
        for public in ('/assets/', '/api/', '/login'):
            self.assertNotIn('location '+public, conf)
        self.assertIn('error_log /dev/null crit;', conf)
        self.assertIn('absolute_redirect off;', conf)

    def test_every_restart_enforces_loopback_and_bootstrap_gate(self):
        for mode in ('install', 'adopt', 'skip'):
            conf = xui_unit_override(mode)
            self.assertIn('ConditionPathExists=/var/lib/selfsni/panel-ready', conf)
            self.assertIn('ExecStartPre=/usr/local/x-ui/x-ui setting -listenIP 127.0.0.1', conf)
            self.assertEqual('warp-svc.service' in conf, mode != 'skip')

    def test_bootstrap_cannot_start_before_loopback_tls_and_pinned_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s = state()
            lock = dict(xui_version='v3.10.2', downloads={
                'x-ui-linux-amd64.tar.gz': dict(url='https://example.com/xui.tar.gz', sha256='0'*64)})
            events = []
            def write(path, content, *args):
                events.append(('write', str(path), content))
            def run(*args, **kwargs):
                events.append(('run', *map(str, args)))
                if '-getApiToken' in args:
                    return 'apiToken: abcdef\n'
                if '-getListen' in args:
                    cert = '/etc/letsencrypt/live/vpn.example.com/'
                    return 'listenIP: 127.0.0.1\ncert: '+cert+'fullchain.pem\nkey: '+cert+'privkey.pem\nport: '+str(s['panel_port'])+'\nwebBasePath: '+s['panel_path']+'\nhasDefaultCredential: false\n'
                return ''
            def pinned(*args):
                events.append(('pinned',))
                return 'verified'
            with patch('selfsni.provision.STATE', root/'state'), \
                 patch('selfsni.provision.write', side_effect=write), \
                 patch('selfsni.provision.run', side_effect=run), \
                 patch('selfsni.provision.download', return_value=root/'archive'), \
                 patch('selfsni.provision.install_pinned_xray', side_effect=pinned), \
                 patch('selfsni.provision.ssl.SSLContext'), \
                 patch('selfsni.provision.wait_for'), patch('selfsni.provision.save'), \
                 patch('selfsni.provision.configure_panel'):
                panel(s, lock, root)
            gate = next(i for i, e in enumerate(events) if e[:2] == ('write', '/etc/systemd/system/x-ui.service.d/20-selfsni.conf'))
            vendor = next(i for i, e in enumerate(events) if e[:2] == ('run', 'bash'))
            cert = next(i for i, e in enumerate(events) if e[:3] == ('run', '/usr/local/x-ui/x-ui', 'setting') and '-webCert' in e)
            marker = next(i for i, e in enumerate(events) if e[:2] == ('write', str(root/'state/panel-ready')))
            start = next(i for i, e in enumerate(events) if e == ('run', 'systemctl', 'restart', 'x-ui'))
            self.assertLess(gate, vendor)
            self.assertLess(vendor, events.index(('pinned',)))
            self.assertLess(events.index(('pinned',)), cert)
            self.assertLess(cert, marker)
            self.assertLess(marker, start)
            self.assertIn('127.0.0.1', events[cert])
            self.assertNotIn('0.0.0.0', events[cert])

    def test_failed_bootstrap_never_creates_ready_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            lock = dict(xui_version='v3.10.2', downloads={
                'x-ui-linux-amd64.tar.gz': dict(url='https://example.com/xui.tar.gz', sha256='0'*64)})
            def run(*args, **kwargs):
                if args[0] == 'bash':
                    raise InstallError('bootstrap failed')
            with patch('selfsni.provision.STATE', root/'state'), \
                 patch('selfsni.provision.write', side_effect=lambda p, *_: events.append(str(p))), \
                 patch('selfsni.provision.run', side_effect=run), \
                 patch('selfsni.provision.download', return_value=root/'archive'), \
                 patch('selfsni.provision.save'):
                with self.assertRaises(InstallError):
                    panel(state(), lock, root)
            self.assertNotIn(str(root/'state/panel-ready'), events)
