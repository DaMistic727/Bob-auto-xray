"""Acceptance of the public proxy, using real TLS and the stock panel protocol."""
import base64
import hashlib
import json
import re
import ssl
from pathlib import Path
from http.cookies import SimpleCookie

from .api import LocalTLS, request
from .core import require, run


def panel_network(state):
    lines = run('ss', '-H', '-lntp').splitlines()
    panel = [line for line in lines if line.split()[3].endswith(':'+str(state['panel_port']))]
    pid = run('systemctl', 'show', 'x-ui', '--property=MainPID', '--value').strip()
    require(pid.isdigit() and int(pid) > 0, 'Panel has no running service PID')
    require(len(panel) == 1 and panel[0].split()[3] == '127.0.0.1:'+str(state['panel_port'])
            and ('pid='+pid+',') in panel[0], 'Panel is not exclusively owned loopback TLS')
    public = [line for line in lines if line.split()[3].endswith(':443')]
    require(public and all('xray' in line for line in public), 'Public TCP/443 must belong to Xray')
    for name in ('user.rules', 'user6.rules'):
        rules = Path('/etc/ufw', name).read_text()
        require(not re.search(r'--dport\s+'+str(state['panel_port'])+r'\b', rules),
                'Internal panel port has a UFW rule')


def panel_routes(state, send=request):
    domain, path = state['domain'], state['panel_path']
    require(re.fullmatch(r'/[a-f0-9]{48}/', path) is not None, 'Weak panel path')
    status, headers, body = send(domain, 443, path)
    require(status == 200 and b'X_UI_BASE_PATH' in body, 'Public panel login page unavailable')
    require(headers.get('referrer-policy') == 'no-referrer', 'Panel may leak its URL via Referer')
    require(headers.get('cache-control') == 'no-store', 'Panel can be cached')
    status, headers, _ = send(domain, 443, path[:-1])
    require(status == 308 and headers.get('location') == path, 'Unsafe panel slash redirect')
    for missing in ('/not-a-real-route', '/panel/api/inbounds/list', '/api/inbounds/list',
                    '/login', '/csrf-token', '/ws', '/assets/missing.js', path[:-1]+'extra/'):
        status, headers, body = send(domain, 443, missing)
        require(status == 404 and 'location' not in headers and path.encode() not in body,
                'Route outside secret prefix exposed panel information')
    require(send(domain, 443, path, headers={'Host': 'wrong.invalid'})[0] == 404,
            'Foreign Host accepted by proxy')
    # The embedded SPA rewrites Vite assets into the configured base path.
    _, _, body = send(domain, 443, path)
    assets = re.findall(rb'(?:src|href)=["\']([^"\']+\.(?:js|css))["\']', body)
    require(bool(assets), 'Panel HTML contains no testable assets')
    for raw in assets:
        asset = raw.decode()
        require(asset.startswith(path) and '..' not in asset, 'Panel asset escaped the secret prefix')
        require(send(domain, 443, asset)[0] == 200, 'Panel asset unavailable')
    status, _, _ = send(domain, 443, path+'panel/api/inbounds/list',
                         headers={'X-Requested-With': 'XMLHttpRequest'})
    require(status == 401, 'Panel API accepted an unauthenticated session')


def panel_websocket(state, cookie):
    """Check a genuine authenticated upgrade and receive a server WS frame."""
    connection = LocalTLS(state['domain'], 443, timeout=15, context=ssl.create_default_context())
    key = base64.b64encode(b'bob-panel-ws-key').decode()
    try:
        connection.request('GET', state['panel_path']+'ws', headers={
            'Cookie': cookie, 'Origin': 'https://'+state['domain'],
            'Upgrade': 'websocket', 'Connection': 'Upgrade',
            'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': key})
        response = connection.getresponse()
        expected = base64.b64encode(hashlib.sha1(
            (key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        require(response.status == 101 and response.getheader('Sec-WebSocket-Accept') == expected,
                'Authenticated WebSocket upgrade failed')
        require(bool(response.fp.read(2)), 'WebSocket returned no frame')
    finally:
        connection.close()


def panel_login(state, send=request, websocket=panel_websocket):
    domain, path = state['domain'], state['panel_path']
    status, headers, body = send(domain, 443, path+'csrf-token')
    require(status == 200, 'Public CSRF endpoint unavailable')
    cookie = headers['set-cookie'].split(';')[0]
    csrf = json.loads(body)['obj']
    login = json.dumps({'username': state['username'], 'password': state['password']}).encode()
    base_headers = {'Content-Type': 'application/json', 'Cookie': cookie}
    require(send(domain, 443, path+'login', 'POST', login, base_headers)[0] == 403,
            'Login accepted missing CSRF token')
    auth_headers = dict(base_headers, **{'X-CSRF-Token': csrf})
    bad = json.dumps({'username': state['username'], 'password': 'wrong-password'}).encode()
    status, _, body = send(domain, 443, path+'login', 'POST', bad, auth_headers)
    require(status == 200 and json.loads(body).get('success') is False,
            'Wrong credentials were accepted')
    status, headers, body = send(domain, 443, path+'login', 'POST', login, auth_headers)
    require(status == 200 and json.loads(body).get('success') is True,
            'Public login failed')
    jar = SimpleCookie(headers['set-cookie'])
    session = jar['3x-ui']
    require(session['path'] == path and session['secure'] and session['httponly']
            and session['samesite'].lower() == 'lax', 'Unsafe panel session cookie')
    cookie = '3x-ui='+session.coded_value
    session_headers = {'Cookie': cookie, 'X-Requested-With': 'XMLHttpRequest'}
    status, _, body = send(domain, 443, path+'panel/api/inbounds/list', headers=session_headers)
    require(status == 200 and json.loads(body).get('success') is True, 'Public session API failed')
    status, headers, _ = send(domain, 443, path, headers={'Cookie': cookie})
    require(status == 307 and headers.get('location') == path+'panel/', 'Login redirect leaked authority')
    # State-changing API request without CSRF must fail before doing any work.
    require(send(domain, 443, path+'panel/api/setting/all', 'POST', b'{}', session_headers)[0] == 403,
            'Session API accepted missing CSRF token')
    # A read-only API POST over 8 KiB proves the old global Nginx limit is overridden.
    status, headers, body = send(domain, 443, path+'csrf-token', headers={'Cookie': cookie})
    require(status == 200, 'Authenticated CSRF refresh failed')
    cookie = headers.get('set-cookie', cookie).split(';')[0]
    csrf_headers = {'Cookie': cookie, 'X-CSRF-Token': json.loads(body)['obj'],
                    'Content-Type': 'application/json'}
    status, _, body = send(domain, 443, path+'panel/api/setting/all', 'POST', b' '*9000+b'{}', csrf_headers)
    require(status == 200 and json.loads(body).get('success') is True, 'Panel request over 8 KiB failed')
    websocket(state, cookie)
    status, _, body = send(domain, 443, path+'logout', 'POST', b'', csrf_headers)
    require(status == 200 and json.loads(body).get('success') is True, 'Public logout failed')
    # Stock stateless signed cookies are cleared in the browser on logout. A
    # previously copied cookie is not server-side revoked; do not claim otherwise.
    require(send(domain, 443, path+'panel/api/inbounds/list',
                 headers={'X-Requested-With': 'XMLHttpRequest'})[0] == 401,
            'Logged-out browser can access protected API')
