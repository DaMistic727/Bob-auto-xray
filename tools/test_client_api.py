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
import ssl
import socket
import subprocess
import tarfile
import tempfile
import uuid
import zipfile

from selfsni.clients import (ensure_admin, read_clients, validate_admin,
                             snapshot_clients, cleanup_owned_client,
                             verify_admin_runtime, verify_admin_subscriptions)
from selfsni.core import ROOT, download, free_port, json_obj, require, save, token, wait_for
from selfsni.geodata import verify_server_geodata
from selfsni.provision import render
from selfsni.routing import inbound, server_template, deeplink


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
        core_spec = lock['xray_downloads']['amd64']
        core_archive = download(core_spec['url'], root/'xray.zip', core_spec['sha256'])
        with zipfile.ZipFile(core_archive) as zf:
            core = zf.read('xray')
        require(hashlib.sha256(core).hexdigest() == core_spec['binary_sha256'], 'Pinned Xray mismatch')
        (app/'bin/xray-linux-amd64').write_bytes(core)
        (app/'bin/xray-linux-amd64').chmod(0o755)
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
                sub_port, sub_path = free_port({port, 443, 62789}), '/'+token(24)+'/'
                settings = api.call('setting/all', {}, method='POST')
                subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1','-keyout',str(root/'sub.key'),'-out',str(root/'sub.crt')],check=True,stdout=log,stderr=log)
                settings.update(subHappAutoDetect=True, subIncyAppAutoDetect=True, subEnableRouting=True, subRoutingRules=deeplink('happ'),
                                subIncyEnableRouting=True, subIncyRoutingRules=deeplink('incy'),
                                subHappTunType='xray', subHappLocalProxyAuth='auto',
                                subEnable=True, subListen='127.0.0.1', subPort=sub_port,
                                subPath=sub_path, subDomain='127.0.0.1', subURI='https://127.0.0.1'+sub_path, subEncrypt=True,
                                subCertFile=str(root/'sub.crt'), subKeyFile=str(root/'sub.key'))
                api.call('setting/update', settings)
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
                process = subprocess.Popen([binary, 'run'], env=env, cwd=app, stdout=log,
                                           stderr=log, start_new_session=True)
                wait_for(lambda: bool(api.call('inbounds/list')), 'persisted panel after restart')
                runtime = json.loads((app/'bin/config.json').read_text())
                verify_admin_runtime(state, runtime)
                subprocess.run([str(app/'bin/xray-linux-amd64'), 'run', '-test', '-config',
                                str(app/'bin/config.json')], check=True, stdout=log, stderr=log)
                state.update(domain='127.0.0.1', sub_port=sub_port, sub_path=sub_path)
                def subscription(domain, port, path, headers):
                    connection = http.client.HTTPSConnection(domain, port, timeout=10, context=ssl.create_default_context(cafile=str(root/'sub.crt')))
                    try:
                        connection.request('GET', path, headers=headers)
                        response = connection.getresponse()
                        return response.status, dict((k.lower(),v) for k,v in response.getheaders()), response.read()
                    finally:
                        connection.close()
                try:
                    wait_for(lambda: (verify_admin_subscriptions(state, subscription), True)[1],
                             'permanent admin Happ/INCY subscriptions')
                except Exception:
                    verify_admin_subscriptions(state, subscription)
                    raise
                # The pinned Go TLS guard uses an HTTP/0.0 error status for
                # plaintext. Inspect the wire status instead of requiring a
                # valid HTTP-client response from a deliberately wrong protocol.
                try:
                    with socket.create_connection(('127.0.0.1',sub_port),timeout=5) as plain:
                        plain.sendall(('GET '+sub_path+state['admin_client']['subId']+' HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n').encode())
                        reply=plain.recv(512)
                        status = re.search(rb'HTTP/[0-9.]+ ([0-9]{3})',reply)
                        tls_alert = reply.startswith(b'\x15\x03')
                        require(not reply or tls_alert or (status and status.group(1) in (b'400',b'403',b'404',b'426')),
                                'Subscription plaintext rejection unrecognized; prefix='+reply[:8].hex()+
                                '; status='+(status.group(1).decode() if status else 'none'))
                except OSError:
                    pass
                stored = api.call('setting/all', {}, method='POST')
                require(stored['subListen']=='127.0.0.1' and stored['subPort']==sub_port and stored['subCertFile']==str(root/'sub.crt'), 'Subscription binding/TLS readback failed')
                listeners = subprocess.check_output(['ss','-H','-lntp'],text=True).splitlines()
                listeners = [line.split()[3] for line in listeners if line.split()[3].endswith(':'+str(sub_port))]
                require(listeners==['127.0.0.1:'+str(sub_port)],'Real subscription socket is public or duplicated')
                status, _, html = subscription('127.0.0.1',sub_port,sub_path+state['admin_client']['subId']+'?html=1',{'Accept':'text/html'})
                require(status == 200, 'Subscription browser page unavailable')
                assets = re.findall(rb'(?:src|href)="([^"]+assets/[^"]+)"',html)
                require(assets and all(a.decode().startswith(sub_path) for a in assets), 'Subscription assets escaped secret prefix')
                for asset in assets:
                    require(subscription('127.0.0.1',sub_port,asset.decode(),{})[0] == 200, 'Subscription asset unavailable')
                # Real pinned HTTPS subscriptions through the production Nginx
                # Unix-socket/PROXY configuration, with no public TCP listener.
                proxy_values = dict(DOMAIN='127.0.0.1', SUB_PATH=sub_path, SUB_PORT=sub_port,
                                    PANEL_PATH=base, PANEL_NO_SLASH=base[:-1], AUTH='auth', STYLE='style', ICON='icon')
                conf = render('nginx-tls.conf', proxy_values)
                conf = conf.replace('/run/selfsni/nginx.sock',str(root/'proxy.sock'))
                conf = conf.replace('/etc/letsencrypt/live/127.0.0.1/fullchain.pem',str(root/'sub.crt'))
                conf = conf.replace('/etc/letsencrypt/live/127.0.0.1/privkey.pem',str(root/'sub.key'))
                for snippet in ('panel','subscription','fastcgi'):
                    conf = conf.replace('/etc/nginx/snippets/selfsni-'+snippet+'.conf',str(root/(snippet+'.conf')))
                (root/'panel.conf').write_text('return 503;')
                (root/'fastcgi.conf').write_text('return 200 "decoy";')
                proxy = render('nginx-subscription.conf',proxy_values).replace('/etc/ssl/certs/ca-certificates.crt',str(root/'sub.crt'))
                (root/'subscription.conf').write_text(proxy)
                (root/'nginx.conf').write_text('pid '+str(root/'nginx.pid')+'; error_log /dev/null crit; events {} http { access_log off; '+conf+' }')
                nginx = subprocess.Popen(['nginx','-p',str(root),'-c',str(root/'nginx.conf'),'-g','daemon off;'],stdout=log,stderr=log)
                try:
                    wait_for(lambda: (root/'proxy.sock').exists(), 'Nginx subscription socket')
                    def through_proxy(domain, port, path, headers):
                        raw = socket.socket(socket.AF_UNIX);raw.settimeout(10);raw.connect(str(root/'proxy.sock'))
                        raw.sendall(b'PROXY TCP4 198.51.100.1 203.0.113.1 12345 443\r\n')
                        tls = ssl.create_default_context(cafile=str(root/'sub.crt')).wrap_socket(raw,server_hostname='127.0.0.1')
                        connection = http.client.HTTPConnection('127.0.0.1');connection.sock=tls
                        try:
                            connection.request('GET',path,headers=headers)
                            response=connection.getresponse()
                            return response.status,dict((k.lower(),v) for k,v in response.getheaders()),response.read()
                        finally:connection.close()
                    verify_admin_subscriptions(state,through_proxy,public=True)
                    for missing in (sub_path,sub_path+'invalid','/assets/missing.js','/sub/invalid'):
                        require(through_proxy('127.0.0.1',443,missing,{})[0] in (400,404),'Unknown subscription route exposed')
                    status,_,html=through_proxy('127.0.0.1',443,sub_path+state['admin_client']['subId']+'?html=1',{'Accept':'text/html'})
                    require(status==200,'Proxied HTML subscription failed')
                    assets=re.findall(rb'(?:src|href)="([^"]+assets/[^"]+)"',html)
                    require(assets and all(a.decode().startswith(sub_path) for a in assets),'Unsafe proxied assets')
                    for asset in assets:
                        require(through_proxy('127.0.0.1',443,asset.decode(),{})[0]==200,'Proxied asset failed')
                finally:
                    nginx.terminate();nginx.wait(timeout=10)
                temporary = dict(state['admin_client'], id=str(uuid.uuid4()), subId=token(24),
                                 email='selfsni-e2e-'+token(6))
                protected = snapshot_clients(api)
                api.call('clients/add', {'client': temporary, 'inboundIds': [original['id']]})
                cleanup_owned_client(api, temporary, protected)
                validate_admin(state, *read_clients(api), only_admin=True)
                print('PASS: pinned GeoData; admin creation/idempotency; real API remark/routing save; pinned Xray runtime; admin subscriptions; owned cleanup preserves admin')
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
