#!/usr/bin/env python3
"""Linux CI only: pinned 3x-ui API in a temporary directory on loopback.
No installer, systemd, network/firewall configuration or VPS is used.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tarfile
import tempfile
import uuid

from selfsni.clients import (ensure_admin, read_clients, validate_admin,
                             snapshot_clients, cleanup_owned_client)
from selfsni.core import ROOT, download, free_port, json_obj, require, save, token, wait_for
from selfsni.geodata import verify_server_geodata
from selfsni.routing import inbound, server_template


def main():
    lock = json.loads((ROOT/'versions.lock.json').read_text())
    with tempfile.TemporaryDirectory(prefix='bob-client-api-') as directory:
        root = Path(directory)
        archive_spec = lock['downloads']['x-ui-linux-amd64.tar.gz']
        archive = download(archive_spec['url'], root/'xui.tar.gz', archive_spec['sha256'])
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                require((member.name == 'x-ui' or member.name.startswith('x-ui/')) and '..' not in Path(member.name).parts
                        and (member.isdir() or member.isfile()), 'Unsafe pinned archive member')
            tf.extractall(root)
        app = root/'x-ui'
        for name, expected in lock['server_geodata']['files'].items():
            require(hashlib.sha256((app/'bin'/name).read_bytes()).hexdigest() == expected,
                    'Pinned server GeoData checksum mismatch')
        categories = verify_server_geodata(app/'bin')
        require('category-ads-all' in categories['geosite.dat'], 'Global advertising category missing')
        env = dict(os.environ, XUI_DB_FOLDER=str(root/'db'), XUI_BIN_FOLDER=str(app/'bin'),
                   XUI_LOG_FOLDER=str(root/'logs'))
        binary = str(app/'x-ui')
        port, base = free_port({443, 62789}), '/'+token(24)+'/'
        def cli(*args):
            return subprocess.check_output([binary, 'setting', *args], env=env, cwd=app,
                                           stderr=subprocess.STDOUT, text=True)
        cli('-port', str(port), '-listenIP', '127.0.0.1', '-webBasePath', base,
            '-username', 'test-'+token(6), '-password', token(24))
        output = cli('-getApiToken')
        match = re.search(r'^apiToken:\s*(\S+)', output, re.M)
        require(match is not None, 'Pinned CLI token contract changed')
        bearer = match.group(1)
        class API:
            def call(self, endpoint, data=None, method=None, form=False):
                import urllib.parse
                headers = {'Authorization': 'Bearer '+bearer}
                if data is not None:
                    headers['Content-Type'] = ('application/x-www-form-urlencoded' if form else 'application/json')
                    data = urllib.parse.urlencode(data) if form else json.dumps(data)
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                try:
                    connection.request(method or ('POST' if data is not None else 'GET'),
                                       base+'panel/api/'+endpoint, data, headers)
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    require(response.status == 200 and body.get('success') is True,
                            'Pinned panel API operation failed: '+endpoint)
                    return body.get('obj')
                finally:
                    connection.close()
        api = API()
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen([binary, 'run'], env=env, cwd=app, stdout=log,
                                       stderr=log, start_new_session=True)
            try:
                wait_for(lambda: api.call('inbounds/list') == [], 'loopback panel API')
                keys = subprocess.check_output([str(app/'bin/xray-linux-amd64'), 'x25519'], text=True)
                private = re.search(r'PrivateKey:\s*(\S+)', keys).group(1)
                public = re.search(r'(?:Password(?: \(PublicKey\))?|PublicKey):\s*(\S+)', keys).group(1)
                row = inbound('panel.test', private, public, token(8))
                row['listen'] = '127.0.0.1'  # Harness never exposes a public listener.
                api.call('inbounds/add', row)
                state = {}
                ensure_admin(state, api, lambda value: save(root/'state.json', value))
                before = snapshot_clients(api)
                ensure_admin(state, api, lambda value: save(root/'state.json', value))
                require(snapshot_clients(api) == before, 'Repeated admin operation changed clients')
                original, _ = validate_admin(state, *read_clients(api), only_admin=True)
                api.call('inbounds/update/'+str(original['id']), original)
                validate_admin(state, *read_clients(api), only_admin=True)
                view = json_obj(api.call('xray/', {}, method='POST'))
                template = view['xraySetting']
                if isinstance(template, str):
                    template = json.loads(template)
                configured = server_template(template, 'skip')
                api.call('xray/update', {'xraySetting': json.dumps(configured)}, form=True)
                after = json_obj(api.call('xray/', {}, method='POST'))['xraySetting']
                if isinstance(after, str):
                    after = json.loads(after)
                require(after['routing'] == configured['routing'] and after['geodata'] == configured['geodata'],
                        'Routing/GeoData changed after real API save')
                temporary = dict(state['admin_client'], id=str(uuid.uuid4()), subId=token(24),
                                 email='selfsni-e2e-'+token(6))
                protected = snapshot_clients(api)
                api.call('clients/add', {'client': temporary, 'inboundIds': [original['id']]})
                cleanup_owned_client(api, temporary, protected)
                validate_admin(state, *read_clients(api), only_admin=True)
                print('PASS: pinned GeoData; admin creation/idempotency; real API remark/routing save; owned cleanup preserves admin')
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
