"""Regression tests for the acceptance probes, including errors found in real E2E."""
import base64
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

from selfsni.core import InstallError
from selfsni.panel_checks import panel_routes, panel_websocket, panel_network


class PanelChecksTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(domain='vpn.example.com', panel_port=21000, panel_path='/'+'a'*48+'/')

    def routes(self, asset=None):
        prefix=self.state['panel_path']
        asset=asset or prefix+'pwa-register.js'
        def send(domain, port, path, method='GET', data=None, headers=None):
            if headers and headers.get('Host') == 'wrong.invalid':
                return 404, {}, b''
            if path == prefix:
                return 200, {'referrer-policy':'no-referrer','cache-control':'no-store'}, (
                    'X_UI_BASE_PATH <script src="'+prefix+'assets/main.js"></script><script src="'+asset+'"></script>').encode()
            if path == prefix[:-1]:
                return 308, {'location':prefix}, b''
            if path == prefix+'panel/api/inbounds/list':
                return 401, {}, b''
            if path in (prefix+'assets/main.js', prefix+'pwa-register.js'):
                return 200, {}, b'javascript'
            return 404, {}, b''
        return send

    def test_pwa_and_vite_assets_under_secret_prefix_are_valid(self):
        panel_routes(self.state, self.routes())

    def test_asset_outside_prefix_fails(self):
        with self.assertRaisesRegex(InstallError, 'escaped'):
            panel_routes(self.state, self.routes('/assets/public.js'))

    def test_unauthenticated_api_must_fail(self):
        send=self.routes()
        def broken(*args, **kwargs):
            if args[2].endswith('panel/api/inbounds/list') and args[2].startswith(self.state['panel_path']):
                return 200, {}, b'{}'
            return send(*args, **kwargs)
        with self.assertRaisesRegex(InstallError, 'unauthenticated'):
            panel_routes(self.state, broken)

    def test_websocket_uses_16_byte_key_and_checks_accept_digest(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status=101
        def header(name):
            key=conn.request.call_args.kwargs['headers']['Sec-WebSocket-Key']
            self.assertEqual(len(base64.b64decode(key)),16)
            return base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        response.getheader.side_effect=header
        response.fp.read.return_value=b'\x81\x02'
        with patch('selfsni.panel_checks.LocalTLS', return_value=conn):
            panel_websocket(self.state, 'session')
        conn.close.assert_called_once()

    def test_websocket_rejection_fails_and_closes(self):
        conn=MagicMock(); conn.getresponse.return_value.status=401
        with patch('selfsni.panel_checks.LocalTLS', return_value=conn):
            with self.assertRaises(InstallError):
                panel_websocket(self.state, 'session')
        conn.close.assert_called_once()

    def test_public_or_wrong_owner_panel_socket_fails(self):
        for address, pid in (('0.0.0.0', 42), ('[::]',42), ('127.0.0.1',43)):
            listeners=f'LISTEN 0 100 {address}:21000 0.0.0.0:* users:(("x-ui",pid={pid},fd=8))'
            with self.subTest(address=address,pid=pid), patch('selfsni.panel_checks.run',side_effect=[listeners,'42']):
                with self.assertRaises(InstallError):
                    panel_network(self.state)
