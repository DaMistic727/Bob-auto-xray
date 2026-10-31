#!/usr/bin/env python3
"""Real Nginx + HTTPS upstream regression harness. Linux, nginx and openssl required.
Uses a private Unix socket and a loopback test upstream; no public TCP listener.
Run: PYTHONPATH=src python3 tools/test_panel_proxy.py
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time

from selfsni.provision import render


def main():
    if not all(shutil.which(name) for name in ('nginx', 'openssl')):
        raise SystemExit('nginx and openssl required; no integration test was run')
    secret = '/'+'a'*48+'/'
    sub_secret = '/'+'b'*48+'/'
    checks = []
    with tempfile.TemporaryDirectory(prefix='bob-proxy-test-') as directory:
        root = Path(directory)
        root.chmod(0o755)
        def cert(name, hostname):
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-days', '1', '-subj', '/CN='+hostname, '-addext',
                            'subjectAltName=DNS:'+hostname, '-keyout', str(root/(name+'.key')),
                            '-out', str(root/(name+'.crt'))], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cert('server', 'panel.test')
        cert('other', 'other.test')
        class Upstream(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.reply()
            def do_HEAD(self):
                self.reply()
            def do_POST(self):
                self.reply()
            def reply(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if self.headers.get('Upgrade', '').lower() == 'websocket':
                    self.send_response(101)
                    self.send_header('Upgrade', 'websocket')
                    self.send_header('Connection', 'Upgrade')
                    self.end_headers()
                    self.wfile.write(b'\x81\x02ok')
                    self.wfile.flush()
                    return
                data = json.dumps(dict(path=self.path, method=self.command,
                                       headers=dict(self.headers), size=len(body))).encode()
                self.send_response(200)
                if self.path.endswith('redirect'):
                    self.send_header('Location', 'https://panel.test:'+str(self.server.server_port)+secret+'panel/')
                for k,v in {'routing':'happ-preset','routing-enable':'true','tun-type':'xray','Subscription-Userinfo':'upload=0; download=0','Profile-Update-Interval':'12','Content-Type':'text/plain','Content-Disposition':'attachment'}.items():
                    self.send_header(k,v)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(root/'server.crt', root/'server.key')
        upstream.socket = tls.wrap_socket(upstream.socket, server_side=True)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        sub = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        sub.socket = tls.wrap_socket(sub.socket, server_side=True)
        threading.Thread(target=sub.serve_forever, daemon=True).start()
        values = dict(SUB_PATH=sub_secret, SUB_PORT=sub.server_port, DOMAIN='panel.test', PANEL_PORT=upstream.server_port,
                      PANEL_PATH=secret, PANEL_NO_SLASH=secret[:-1], AUTH='auth', STYLE='style', ICON='icon')
        snippet = render('nginx-panel.conf', values).replace('/etc/ssl/certs/ca-certificates.crt', str(root/'server.crt'))
        sub_snippet = render('nginx-subscription.conf', values).replace('/etc/ssl/certs/ca-certificates.crt', str(root/'server.crt'))
        server = render('nginx-tls.conf', values)
        server = server.replace('/run/selfsni/nginx.sock', str(root/'nginx.sock'))
        server = server.replace('/etc/letsencrypt/live/panel.test/fullchain.pem', str(root/'server.crt'))
        server = server.replace('/etc/letsencrypt/live/panel.test/privkey.pem', str(root/'server.key'))
        server = server.replace('/etc/nginx/snippets/selfsni-subscription.conf', str(root/'sub.conf'))
        server = server.replace('/etc/nginx/snippets/selfsni-panel.conf', str(root/'panel.conf'))
        server = server.replace('/etc/nginx/snippets/selfsni-fastcgi.conf', str(root/'decoy.conf'))
        (root/'decoy.conf').write_text('return 200 "decoy";\n')
        config = ('pid '+str(root/'nginx.pid')+';\nerror_log '+str(root/'global.log')+' crit;\n'
                  'events {}\nhttp { access_log off; client_body_temp_path '+str(root/'body')+';\n'
                  'proxy_temp_path '+str(root/'proxy')+';\n'+server+'\n}\n')
        (root/'nginx.conf').write_text(config)
        process = None
        def start(proxy, subscription=None):
            nonlocal process
            (root/'panel.conf').write_text(proxy)
            (root/'sub.conf').write_text(sub_snippet if subscription is None else subscription)
            subprocess.run(['nginx', '-t', '-p', str(root), '-c', str(root/'nginx.conf')],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process = subprocess.Popen(['nginx', '-p', str(root), '-c', str(root/'nginx.conf'), '-g', 'daemon off;'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                if (root/'nginx.sock').exists():
                    return
                if process.poll() is not None:
                    raise RuntimeError('Test Nginx exited')
                time.sleep(0.05)
            raise RuntimeError('Test Nginx socket timeout')
        def stop():
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            (root/'nginx.sock').unlink(missing_ok=True)
        def send(path, method='GET', data=None, headers=None, ip='198.51.100.44'):
            raw = socket.socket(socket.AF_UNIX)
            raw.settimeout(5)
            raw.connect(str(root/'nginx.sock'))
            raw.sendall(('PROXY TCP4 '+ip+' 203.0.113.1 54321 443\r\n').encode())
            context = ssl.create_default_context(cafile=str(root/'server.crt'))
            sock = context.wrap_socket(raw, server_hostname='panel.test')
            conn = http.client.HTTPConnection('panel.test')
            conn.sock = sock
            try:
                conn.request(method, path, data, headers or {})
                res = conn.getresponse()
                return res.status, dict((k.lower(), v) for k, v in res.getheaders()), res.read() if res.status != 101 else res.fp.read(4)
            finally:
                conn.close()
        try:
            start(snippet)
            assert send('/')[2] == b'decoy'
            for missing in ('/wrong', '/assets/x.js', '/login', '/api/inbounds/list', '/panel/api/inbounds/list', secret[:-1]+'extra/'):
                assert send(missing)[0] == 404, missing
            assert send(secret[:-1])[:2][1]['location'] == secret
            assert send(secret, headers={'Host': 'evil.test'})[0] == 404
            checks.append('Nginx syntax, decoy, exact prefix, relative slash redirect, foreign Host and unknown routes')
            forged = {'X-Real-IP': '1.2.3.4', 'X-Forwarded-For': '1.2.3.4, 127.0.0.1',
                      'Forwarded': 'for=1.2.3.4;proto=http', 'X-Forwarded-Proto': 'http',
                      'X-Forwarded-Host': 'evil.test', 'X-Forwarded-Port': '21000'}
            status, headers, body = send(secret+'panel/api/test?x=1', headers=forged)
            result = json.loads(body)
            assert status == 200 and result['path'] == secret+'panel/api/test?x=1'
            assert result['headers']['Host'] == 'panel.test'
            for key in ('X-Real-IP', 'X-Forwarded-For'):
                assert result['headers'][key] == '198.51.100.44'
            assert 'Forwarded' not in result['headers']
            assert result['headers']['X-Forwarded-Proto'] == 'https'
            assert result['headers']['X-Forwarded-Port'] == '443'
            assert headers['referrer-policy'] == 'no-referrer' and headers['cache-control'] == 'no-store'
            assert send(secret+'redirect')[1]['location'] == 'https://panel.test'+secret+'panel/'
            checks.append('PROXY client IP wins over forged headers; URI/query and public redirect authority preserved')
            assert json.loads(send(secret+'api/test', 'POST', b'x'*9000)[2])['size'] == 9000
            checks.append('Panel body exceeds decoy 8 KiB limit')
            status, _, frame = send(secret+'ws', headers={'Upgrade': 'websocket', 'Connection': 'Upgrade'})
            assert status == 101 and frame == b'\x81\x02ok'
            checks.append('WebSocket upgrade and server frame traverse real TLS proxy')
            statuses = [send(secret+'login', 'POST', b'{}', ip='198.51.100.99')[0] for _ in range(8)]
            assert 200 in statuses and 429 in statuses
            assert send(secret+'api/test', 'POST', b'{}', ip='198.51.100.99')[0] == 200
            checks.append('Exact login POST rate limit returns 429 without limiting other APIs')
            for agent in ('Happ/3.0.0', 'INCY/1.0.0'):
                path = sub_secret+'test-id?q=a%20b'
                status, h, body = send(path, headers=dict(forged, **{'User-Agent':agent}))
                result = json.loads(body)
                assert status == 200 and result['path'] == path
                assert result['headers']['User-Agent'] == agent
                assert result['headers']['Host'] == 'panel.test'
                assert result['headers']['X-Forwarded-For'] == '198.51.100.44'
                assert h['routing'] == 'happ-preset' and h['subscription-userinfo'] == 'upload=0; download=0'
                assert h['profile-update-interval'] == '12' and h['cache-control'] == 'no-store'
                assert send(path, 'HEAD')[2] == b''
            assert send(sub_secret+'id', headers={'Host':'evil.test'})[0] == 404
            for path in ('/assets/x.js','/api/x',sub_secret[:-1]+'extra/id',sub_secret+'%2e%2e/id',sub_secret+'../',sub_secret+'id%2felse'):
                assert send(path)[0] == 404
            checks.append('Subscription GET/HEAD, encoded queries, User-Agent/custom headers, spoofing, exact prefix and traversal isolation')
            stop()
            start(snippet, sub_snippet.replace('proxy_ssl_name panel.test;', 'proxy_ssl_name wrong.test;'))
            assert send(sub_secret+'id')[0] == 502 and send(secret)[0] == 200
            stop()
            start(snippet, sub_snippet.replace(str(root/'server.crt'), str(root/'other.crt')))
            assert send(sub_secret+'id')[0] == 502 and send(secret)[0] == 200
            stop()
            sub.shutdown(); sub.server_close()
            start(snippet)
            assert send(sub_secret+'id')[0] == 502 and send(secret)[0] == 200
            assert sub_secret not in (root/'global.log').read_text()
            checks.append('Subscription TLS hostname/trust and unavailable backend fail closed; panel survives; no URI logs')
            stop()
            start(snippet.replace('proxy_ssl_name panel.test;', 'proxy_ssl_name wrong.test;'))
            assert send(secret)[0] == 502
            checks.append('Wrong upstream TLS hostname fails closed with 502')
            stop()
            start(snippet.replace(str(root/'server.crt'), str(root/'other.crt')))
            assert send(secret)[0] == 502
            checks.append('Untrusted upstream TLS certificate fails closed with 502')
        finally:
            stop()
            upstream.shutdown()
            upstream.server_close()
            sub.server_close()
        print(json.dumps({'scope': 'isolated real Nginx/HTTPS upstream, no public listener',
                          'passed': len(checks), 'checks': checks}, indent=2))


if __name__ == '__main__':
    main()
