import http.client
import json
import socket
import ssl
import urllib.parse
from .core import InstallError, require

PANEL_ACCESS = 'https-443'


def public_panel_port(state):
    """Missing marker denotes an older installation, never silently migrate it."""
    return 443 if state.get('panel_access') == PANEL_ACCESS else state['panel_port']


def public_panel_url(state):
    port = public_panel_port(state)
    authority = state['domain'] + ('' if port == 443 else ':' + str(port))
    return 'https://' + authority + state['panel_path']


class LocalTLS(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(('127.0.0.1', self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)

def request(domain, port, path, method='GET', data=None, headers=None):
    conn = LocalTLS(domain, port, timeout=30, context=ssl.create_default_context())
    try:
        conn.request(method, path, body=data, headers=headers or {})
        response = conn.getresponse()
        body = response.read(4 * 1024 * 1024)
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), body
    finally:
        conn.close()

class Panel:
    """Provisioning always uses the internal TLS endpoint, including before Xray."""
    def __init__(self, state):
        self.s = state

    def call(self, endpoint, data=None, method=None, form=False):
        headers = {'Authorization': 'Bearer ' + self.s['api_token']}
        if data is not None:
            if form:
                data = urllib.parse.urlencode(data).encode()
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            else:
                data = json.dumps(data).encode()
                headers['Content-Type'] = 'application/json'
        status, _, body = request(self.s['domain'], self.s['panel_port'],
            self.s['panel_path'] + 'panel/api/' + endpoint,
            method or ('POST' if data is not None else 'GET'), data, headers)
        require(status == 200, f'Panel API {endpoint}: HTTP {status}')
        try:
            result = json.loads(body)
        except ValueError as e:
            raise InstallError('Panel returned invalid JSON: ' + endpoint) from e
        require(result.get('success') is True, 'Panel API rejected: ' + endpoint)
        return result.get('obj')
