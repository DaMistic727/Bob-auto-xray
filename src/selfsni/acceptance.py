"""Real on-host acceptance. Temporary users are created ONLY with --e2e."""
import argparse
import base64
import copy
import json
import hashlib
import ssl
import os
from pathlib import Path
import re
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
import urllib.parse
import uuid

from .api import LocalTLS, Panel, request
from .core import (digest, json_obj, InstallError, require, run, save, token,
                   free_port, wait_for)
from .geodata import (GEODATA_ASSETS, GEODATA_CRON, GEODATA_TIMEZONE,
                      automatic_config, verify_server_geodata)
from .routing import (BLOCKED_DOMAINS, BLOCKED_IPS, WARP_DOMAINS, TAG, profile,
                      server_template)


def check_warp():
    listeners = run('ss','-H','-lntp')
    warp = [x for x in listeners.splitlines() if x.split()[3].endswith(':40000')]
    require(warp and all(x.split()[3] in ('127.0.0.1:40000','[::1]:40000') for x in warp),
            'WARP must listen on loopback only')
    trace = run('curl','-fsS','--socks5-hostname','127.0.0.1:40000',
                '--connect-timeout','10','--max-time','30','https://www.cloudflare.com/cdn-cgi/trace')
    require('warp=on' in trace or 'warp=plus' in trace, 'SOCKS5 trace is not WARP')
    direct = run('curl','-fsS','--max-time','30','https://www.cloudflare.com/cdn-cgi/trace')
    require('warp=off' in direct, 'System traffic is unexpectedly tunneled')
    return True


def xray_process(s):
    target = Path(s['xray_bin']).resolve()
    matches = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / 'exe').resolve() == target:
                matches.append(proc)
        except (FileNotFoundError, PermissionError):
            pass
    require(len(matches) == 1, 'Expected exactly one panel-managed Xray process')
    return matches[0]


def editor_reality_target(row):
    """Read Target where 3x-ui rawInboundToFormValues passes data to reset()."""
    reality = json_obj(row['streamSettings']).get('realitySettings', {})
    require('dest' not in reality, 'Legacy REALITY dest would render an empty Target field')
    target = reality.get('target')
    require(target == '/run/selfsni/nginx.sock', '3x-ui editor model lost REALITY Target')
    return target

def verify(s):
    checks = []
    def check(name, fn):
        try:
            fn()
            checks.append(dict(name=name, passed=True))
        except Exception as e:
            checks.append(dict(name=name, passed=False, reason=str(e)))
    def expect(value, message):
        require(value, message)
    api = Panel(s)
    def xray_setting():
        view = api.call('xray/', {}, method='POST')
        view = json_obj(view)
        return json_obj(view['xraySetting'])

    def basic_inbound():
        ins = api.call('inbounds/list')
        require(len(ins) == 1 and ins[0]['port'] == 443 and ins[0]['enable'], 'Expected one REALITY inbound')
        i = ins[0]
        require(json_obj(i['settings'])['clients'] == [], 'Permanent clients must not be created')
        stream = json_obj(i['streamSettings'])
        r = stream['realitySettings']
        require(editor_reality_target(i) == '/run/selfsni/nginx.sock',
                'REALITY Target is absent from the 3x-ui editor model')
        require(r['target'] == '/run/selfsni/nginx.sock' and 'dest' not in r
                and r['xver'] == 1 and r['serverNames'] == [s['domain']],
                'Incorrect Self-SNI settings')
        require(r['privateKey'] == s['reality_private'], 'REALITY key mismatch')
        require(r['show'] is True and r['settings']['fingerprint'] == 'firefox',
                'REALITY show/fingerprint mismatch')
        sniffing = json_obj(i['sniffing'])
        require(sniffing == {'enabled': True, 'destOverride': ['http', 'tls', 'quic', 'fakedns'],
                             'routeOnly': True}, 'Sniffing settings mismatch')
        require(i['tag'] in (TAG, 'in-443-tcp'), 'Unexpected REALITY inbound tag')
        require(not api.call('hosts/list'), 'Fresh install must not contain Host fingerprint overrides')
    check('one inbound, zero clients, Self-SNI + Firefox parameters', basic_inbound)

    def inbound_editor_roundtrip():
        before = api.call('inbounds/list')[0]
        require(editor_reality_target(before) == '/run/selfsni/nginx.sock',
                'API model does not expose REALITY Target')
        with sqlite3.connect('/etc/x-ui/x-ui.db') as db:
            stored = db.execute('SELECT stream_settings FROM inbounds WHERE id = ?',
                                (before['id'],)).fetchone()
        require(stored is not None, 'Inbound is absent from the 3x-ui database')
        db_reality = json_obj(stored[0])['realitySettings']
        require(db_reality.get('target') == '/run/selfsni/nginx.sock' and 'dest' not in db_reality,
                '3x-ui database does not persist REALITY Target')
        api.call('inbounds/update/'+str(before['id']), before)
        wait_for(lambda: editor_reality_target(api.call('inbounds/list')[0]) ==
                 '/run/selfsni/nginx.sock', 'REALITY Target after editor-style save')
        after = api.call('inbounds/list')[0]
        require(editor_reality_target(after) == '/run/selfsni/nginx.sock',
                'REALITY Target was lost after API save')
    check('REALITY Target in API/DB/editor model survives inbound save', inbound_editor_roundtrip)

    def routing_roundtrip():
        before = xray_setting()
        require(server_template(before) == before, 'Stored Xray template is not canonical')
        tags = [x.get('tag') for x in before['outbounds']]
        require(tags[:3] == ['direct', 'blocked', 'warp-outbound'], 'Unexpected outbound order')
        require(len(tags) == len(set(tags)) and 'block' not in tags, 'Duplicate/legacy outbound present')
        rules = before['routing']['rules']
        require(rules[0].get('outboundTag') == 'api', 'API service rule lost precedence')
        require(rules[1:] == [
            {'type': 'field', 'ip': BLOCKED_IPS, 'outboundTag': 'blocked'},
            {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'blocked'},
            {'type': 'field', 'domain': BLOCKED_DOMAINS, 'outboundTag': 'blocked'},
            {'type': 'field', 'domain': WARP_DOMAINS, 'outboundTag': 'warp-outbound'},
        ], 'Basic/WARP rules differ or are duplicated')
        require(before['routing']['domainStrategy'] == 'IPOnDemand', 'Unsafe routing domain strategy')
        require(before.get('geodata') == automatic_config(), 'Native geodata configuration mismatch')
        view = json_obj(api.call('xray/', {}, method='POST'))
        standard = view.get('geodataSources', [])
        require(all(asset in standard for asset in GEODATA_ASSETS),
                'Configured geodata assets are not visible as standard 3x-ui sources')
        api.call('xray/update', {'xraySetting': json.dumps(before)}, form=True)
        wait_for(lambda: request(s['domain'], s['panel_port'], s['panel_path'])[0] == 200,
                 'panel after Xray settings round-trip')
        after = xray_setting()
        require(after['routing'] == before['routing'] and after['outbounds'] == before['outbounds']
                and after.get('geodata') == before.get('geodata'),
                'Xray settings changed after panel API round-trip')
    check('Basic UI + native geodata panel API save round-trip', routing_roundtrip)

    check('Installed GeoSite/GeoIP categories',
          lambda: verify_server_geodata('/usr/local/x-ui/bin'))
    check('nginx configuration',lambda: run('nginx','-t'))
    check('Xray configuration',lambda: run(s['xray_bin'],'run','-test','-config','/usr/local/x-ui/bin/config.json'))
    def runtime_shape():
        config = json.loads(Path('/usr/local/x-ui/bin/config.json').read_text())
        tags = [x.get('tag') for x in config['outbounds']]
        require(tags == ['direct', 'blocked', 'warp-outbound'],
                'Runtime user outbounds differ from direct/blocked/WARP')
        inbound = next((item for item in config.get('inbounds', [])
                        if item.get('port') == 443
                        and item.get('streamSettings', {}).get('security') == 'reality'), None)
        require(inbound is not None, 'Runtime REALITY inbound is missing')
        reality = inbound['streamSettings']['realitySettings']
        require(reality.get('target') == '/run/selfsni/nginx.sock' and 'dest' not in reality,
                'Runtime Xray JSON lost REALITY Target')
        require(config.get('api', {}).get('tag') == 'api', 'Runtime API service outbound is missing')
        require(config.get('geodata') == automatic_config(), 'Runtime native geodata settings mismatch')
        require(s['reality_private'] not in run('journalctl','-u','x-ui','--no-pager','-n','500'),
                'REALITY private key leaked into x-ui journal')
    check('Runtime outbound set + safe REALITY diagnostics', runtime_shape)
    def geodata_timezone():
        unit_env = run('systemctl','show','x-ui','--property=Environment','--value').split()
        require('TZ='+GEODATA_TIMEZONE in unit_env, 'x-ui service does not pin the geodata timezone')
        proc = xray_process(s)
        process_env = (proc/'environ').read_bytes().split(b'\0')
        require(('TZ='+GEODATA_TIMEZONE).encode() in process_env,
                'Xray did not inherit the pinned UTC timezone')
        require(GEODATA_CRON == '0 0 * * *', '03:00 Moscow cron mapping changed')
    check('Native geodata process timezone and 03:00 Europe/Moscow schedule', geodata_timezone)
    check('HTTPS panel certificate and response', lambda: expect(request(s['domain'],s['panel_port'],s['panel_path'])[0] == 200,'Panel unavailable'))
    def credential_login():
        require(s['username'] != 'admin' and len(s['password']) >= 32 and len(s['panel_path']) >= 48, 'Weak bootstrap credentials')
        status, headers, body = request(s['domain'], s['panel_port'], s['panel_path']+'csrf-token')
        require(status == 200, 'Panel CSRF endpoint unavailable')
        csrf = json.loads(body)['obj']
        cookie = headers['set-cookie'].split(';')[0]
        status, headers, body = request(s['domain'], s['panel_port'], s['panel_path']+'login', 'POST',
            json.dumps({'username':s['username'],'password':s['password']}).encode(),
            {'Content-Type':'application/json','Cookie':cookie,'X-CSRF-Token':csrf})
        require(status == 200 and json.loads(body).get('success') is True, 'Generated credentials cannot log in')
        cookie = headers.get('set-cookie', cookie).split(';')[0]
        status, _, body = request(s['domain'], s['panel_port'], s['panel_path']+'panel/api/inbounds/list', headers={'Cookie':cookie})
        require(status == 200 and json.loads(body).get('success') is True, 'Authenticated panel session unavailable')
        status, h, body = request(s['domain'], s['panel_port'], s['panel_path']+'csrf-token', headers={'Cookie':cookie})
        cookie = h.get('set-cookie', cookie).split(';')[0]
        request(s['domain'], s['panel_port'], s['panel_path']+'logout','POST',b'',
                {'Cookie':cookie,'X-CSRF-Token':json.loads(body)['obj']})
    check('Generated credentials authenticate with HTTPS session and CSRF', credential_login)
    def secret_permissions():
        for name in ['/var/lib/selfsni/state.json','/etc/x-ui/x-ui.db',
                     '/usr/local/x-ui/bin/config.json','/etc/x-ui/install-result.env']:
            st = Path(name).stat()
            require(st.st_uid == 0 and st.st_mode & 0o077 == 0, 'Insecure secret file permissions: '+name)
    check('Sensitive state, DB and runtime configuration are root-only', secret_permissions)
    def certificate_identity():
        pem = Path(f'/etc/letsencrypt/live/{s["domain"]}/fullchain.pem').read_text()
        pem = pem.split('-----END CERTIFICATE-----')[0] + '-----END CERTIFICATE-----\n'
        expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()
        for port in [443, s['panel_port'], s['sub_port']]:
            connection = LocalTLS(s['domain'], port, timeout=15, context=ssl.create_default_context())
            try:
                connection.connect()
                require(hashlib.sha256(connection.sock.getpeercert(binary_form=True)).digest() == expected, 'Stale TLS certificate')
            finally:
                connection.close()
    check('All three TLS listeners use the current live certificate', certificate_identity)
    def decoy():
        status, headers, body = request(s['domain'],443,'/')
        require(status == 200 and s['portal']['NAME'].encode() in body, 'REALITY TLS fallback failed')
        require('set-cookie' in headers and 'secure' in headers['set-cookie'].lower() and 'httponly' in headers['set-cookie'].lower(), 'Unsafe session cookie')
        match = re.search(rb'name="' + s['portal']['CSRF'].encode() + rb'" value="([a-f0-9]+)"',body)
        require(match is not None,'CSRF token missing')
        data = urllib.parse.urlencode({s['portal']['CSRF']:match.group(1).decode(),
             s['portal']['USERFIELD']:'acceptance.invalid',s['portal']['PASSFIELD']:token()}).encode()
        status, h, _ = request(s['domain'],443,'/'+s['portal']['AUTH'],'POST',data,
            {'Content-Type':'application/x-www-form-urlencoded','Cookie':headers['set-cookie'].split(';')[0]})
        require(status == 303 and h.get('location') == '/', 'POST handler failed')
        cookie = h.get('set-cookie',headers['set-cookie']).split(';')[0]
        status, _, body = request(s['domain'],443,'/',headers={'Cookie':cookie})
        require(status == 200 and b'Incorrect username or password.' in body, 'Missing login error')
        require(request(s['domain'],443,'/'+s['portal']['AUTH'],'POST',b'',{'Content-Type':'application/x-www-form-urlencoded'})[0] == 403,'CSRF not enforced')
    check('REALITY fallback, PROXY v1, portal GET/POST/CSRF',decoy)
    def portal_privacy():
        for directory in ['/var/lib/php/sessions','/var/log/nginx']:
            for path in Path(directory).glob('*'):
                if path.is_file() and path.stat().st_size < 5 * 1024 * 1024:
                    require(b'acceptance.invalid' not in path.read_bytes(), 'Portal submitted identity was persisted')
    check('Portal submitted identity absent from sessions and nginx logs', portal_privacy)
    check('WARP SOCKS trace + direct system trace', check_warp)
    def presets():
        settings = api.call('setting/all',{},method='POST')
        require(settings.get('subHappTunType') == 'xray', 'Happ TUN type was not persisted')
        require(settings.get('subHappLocalProxyAuth') == 'auto', 'Happ local proxy auth was not persisted')
        require(settings.get('subHappTunMode', '') in ('', 'system', 'gvisor'), 'Unsupported Happ TUN mode')
        for kind, key in [('happ','subRoutingRules'),('incy','subIncyRoutingRules')]:
            link = settings[key]
            require(link.startswith(kind+'://routing/onadd/'), 'Wrong subscription deeplink')
            p = json.loads(base64.b64decode(link.split('/onadd/')[1],validate=True))
            require(p == profile(kind), 'Stored routing profile differs')
    check('Happ/INCY settings and stored profile round-trip',presets)
    def routes():
        cases = [
            ({'domain':'yandex.ru'}, 'blocked'),
            ({'domain':'test.su'}, 'blocked'),
            ({'domain':'пример.рф'.encode('idna').decode()}, 'blocked'),
            ({'ip':'77.88.8.8'}, 'blocked'),
            ({'ip':'2a02:6b8::1'}, 'blocked'),
            ({'domain':'77.88.8.8.nip.io'}, 'blocked'),
            ({'domain':'chatgpt.com'}, 'warp-outbound'),
            ({'domain':'api.ipify.org'}, 'warp-outbound'),
            ({'domain':'example.com'}, ''),
            ({'domain':'example.com', 'protocol':'bittorrent'}, 'blocked'),
        ]
        for target, expected in cases:
            data = dict(domain='', ip='', protocol='', port=443, network='tcp', inboundTag=TAG)
            data.update(target)
            result = api.call('xray/routeTest', data, form=True)
            actual = result.get('outboundTag', '')
            require(actual == expected, 'Unexpected destination route: ' + str(target))
    check('Live routing: RU/IP/DNS, BitTorrent, GeoSite WARP, default DIRECT',routes)
    for service in ['x-ui','nginx','warp-svc','fail2ban',s['php_service']]:
        check('systemd '+service,lambda service=service: (run('systemctl','is-active',service),run('systemctl','is-enabled',service)))
    check('UFW active',lambda:expect('Status: active' in run('ufw','status'),'UFW inactive'))
    check('Fail2ban jails',lambda:(run('fail2ban-client','status','sshd'),run('fail2ban-client','status','3x-ipl')))
    check('Certbot timer',lambda:run('systemctl','is-enabled','certbot.timer'))
    return {'scope':'on-host; no external client/reboot claims', 'checks':checks}


def geodata_e2e(s):
    """Exercise Xray's native scheduled download, hot reload, failure, and rollback."""
    api = Panel(s)
    root = Path('/usr/local/x-ui/bin')
    paths = [root/asset['file'] for asset in GEODATA_ASSETS]

    def template():
        view = json_obj(api.call('xray/', {}, method='POST'))
        return json_obj(view['xraySetting'])

    def apply(config):
        api.call('xray/update', {'xraySetting': json.dumps(config)}, form=True)
        run('systemctl','restart','x-ui')
        wait_for(lambda: request(s['domain'],s['panel_port'],s['panel_path'])[0] == 200,
                 'panel after geodata configuration restart')

    def state():
        return {path.name: (digest(path), path.stat().st_mtime_ns) for path in paths}

    def journal_since(start):
        return run('journalctl','-u','x-ui','--no-pager','--since','@'+str(int(start)))

    original = template()
    require(original.get('geodata') == automatic_config(), 'Geodata E2E requires production settings')
    report = {}
    try:
        live = copy.deepcopy(original)
        live['geodata'] = automatic_config('* * * * *')
        apply(live)
        before = state()
        pid = xray_process(s).name
        wait_for(lambda: all(state()[name][1] > value[1] for name, value in before.items()),
                 'native geodata download tick', attempts=50)
        require(xray_process(s).name == pid, 'Native geodata reload restarted Xray')
        verify_server_geodata(root)
        report['hotReload'] = 'four upstream assets downloaded and reloaded without PID change'

        failed = copy.deepcopy(original)
        failed['geodata'] = automatic_config('* * * * *', [{
            'url': ('https://github.com/Loyalsoldier/v2ray-rules-dat/'
                    'releases/latest/download/selfsni-not-found.dat'),
            'file': 'geosite.dat',
        }])
        apply(failed)
        before = state()
        pid = xray_process(s).name
        started = time.time()
        wait_for(lambda: 'scheduled geodata reload failed' in journal_since(started),
                 'native geodata download failure', attempts=50)
        require(state() == before, 'Failed geodata download changed installed assets')
        require(xray_process(s).name == pid, 'Failed geodata download restarted Xray')
        report['downloadFailure'] = 'HTTP error left every installed asset unchanged'

        rollback = copy.deepcopy(original)
        rollback['geodata'] = automatic_config('* * * * *', [{
            'url': 'https://example.com/',
            'file': 'geosite.dat',
        }])
        apply(rollback)
        before = state()
        pid = xray_process(s).name
        started = time.time()
        wait_for(lambda: 'rolling back' in journal_since(started),
                 'native geodata rollback', attempts=50)
        require(state() == before, 'Invalid geodata was not rolled back atomically')
        require(xray_process(s).name == pid, 'Geodata rollback restarted Xray')
        verify_server_geodata(root)
        report['rollback'] = 'invalid 200 response failed reload and restored the original file'
    finally:
        apply(original)
        require(template().get('geodata') == automatic_config(),
                'Production geodata schedule was not restored')
        verify_server_geodata(root)
    report['restored'] = 'daily 03:00 Europe/Moscow settings and required categories'
    return report

def decode_subscription(body):
    try:
        return base64.b64decode(body.strip(),validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return body.decode()

def verify_sub_response(kind, status, headers, body):
    require(status == 200,'Subscription HTTP error')
    text = decode_subscription(body)
    require('vless://' in text, 'Subscription lacks VLESS client')
    if kind == 'happ':
        link = headers.get('routing','')
        require(headers.get('routing-enable') in ('true','1'), 'Happ routing disabled')
        require('incy://' not in text, 'INCY profile leaked to Happ')
    else:
        require('routing' not in headers, 'Happ header overrides INCY')
        require('tun-type' not in headers and 'socks-auth-mode' not in headers,
                'Happ application settings leaked to INCY')
        links = [x for x in text.splitlines() if x.startswith('incy://routing/onadd/')]
        require(len(links) == 1, 'INCY profile missing or duplicated')
        link = links[0]
        require('happ://' not in text,'Happ profile leaked to INCY')
    require(link.startswith(kind+'://routing/onadd/'),'Wrong deeplink scheme')
    p = json.loads(base64.b64decode(link.split('/onadd/')[1],validate=True))
    require(p == profile(kind),'Subscription profile mismatch')
    vless = next((x for x in text.splitlines() if x.startswith('vless://')), '')
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(vless).query)
    require(params.get('fp') == ['firefox'], 'Subscription VLESS fingerprint is not Firefox')
    if kind == 'happ':
        require(headers.get('tun-type') == 'xray', 'Happ TUN type header mismatch')
        require(headers.get('socks-auth-mode') == 'auto' and headers.get('http-auth-mode') == 'auto',
                'Happ local proxy auth headers mismatch')
    return True

def e2e(s):
    api = Panel(s)
    original = api.call('inbounds/list')[0]
    require(json_obj(original['settings'])['clients'] == [], 'E2E requires a disposable zero-client server')
    client = {'id':str(uuid.uuid4()),'flow':'xtls-rprx-vision','email':'selfsni-e2e-'+token(6),
              'enable':True,'subId':token(16),'limitIp':0,'totalGB':0,'expiryTime':0,'reset':0}
    try:
        api.call('clients/add', {'client': client, 'inboundIds': [original['id']]})
        run('systemctl','restart','x-ui')
        wait_for(lambda: bool(api.call('inbounds/list')), 'client activation')
        for kind, agent in [('happ','Happ/3.0.0 (iOS)'),('incy','INCY/1.0.0/ios')]:
            status,headers,body = request(s['domain'],s['sub_port'],s['sub_path']+client['subId'],headers={'User-Agent':agent})
            verify_sub_response(kind,status,headers,body)
        with tempfile.TemporaryDirectory(prefix='selfsni-e2e-') as wd:
            port = free_port({40000})
            config = {'log':{'loglevel':'warning'},'inbounds':[{'listen':'127.0.0.1','port':port,'protocol':'socks','settings':{'auth':'noauth','udp':False}}],
                'outbounds':[{'protocol':'vless','settings':{'vnext':[{'address':s['ipv4'],'port':443,
                    'users':[{'id':client['id'],'flow':'xtls-rprx-vision','encryption':'none'}]}]},
                    'streamSettings':{'network':'tcp','security':'reality','realitySettings':{
                        'serverName':s['domain'],'fingerprint':'firefox','publicKey':s['reality_public'],'shortId':s['short_id'],'spiderX':'/'}}}]}
            path = Path(wd)/'client.json'
            save(path,config)
            process = subprocess.Popen([s['xray_bin'],'run','-config',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                def fetch(url):
                    return run('curl','-fsS','--socks5-hostname',f'127.0.0.1:{port}','--connect-timeout','10','--max-time','30',url)
                wait_for(lambda: bool(fetch('https://example.com')), 'REALITY Vision test client')
                direct = fetch('https://www.cloudflare.com/cdn-cgi/trace')
                require('warp=off' in direct,'Ordinary traffic went through WARP')
                expected_ip = run('curl','-fsS','--socks5-hostname','127.0.0.1:40000','--max-time','30','https://api.ipify.org').strip()
                require(fetch('https://api.ipify.org').strip() == expected_ip,'IP-check did not egress WARP')
                for url in ['https://yandex.ru','https://77.88.8.8']:
                    try:
                        fetch(url)
                    except InstallError:
                        pass
                    else:
                        raise InstallError('RU destination was reachable through VLESS')

                def set_route_only(value):
                    current = copy.deepcopy(api.call('inbounds/list')[0])
                    sniffing = json_obj(current['sniffing'])
                    sniffing['routeOnly'] = value
                    current['sniffing'] = json.dumps(sniffing)
                    api.call('inbounds/update/'+str(current['id']), current)
                    wait_for(lambda: bool(api.call('inbounds/list')), 'sniffing mode update')

                def tls_to_ip_with_sni(ip, hostname):
                    """Give SOCKS an IP while TLS exposes a different sniffable SNI."""
                    def receive(sock, length):
                        result = b''
                        while len(result) < length:
                            chunk = sock.recv(length - len(result))
                            require(chunk, 'SOCKS5 closed during handshake')
                            result += chunk
                        return result
                    with socket.create_connection(('127.0.0.1', port), timeout=10) as sock:
                        sock.sendall(b'\x05\x01\x00')
                        require(receive(sock, 2) == b'\x05\x00', 'SOCKS5 method rejected')
                        sock.sendall(b'\x05\x01\x00\x01' + socket.inet_aton(ip) + struct.pack('!H', 443))
                        reply = receive(sock, 4)
                        require(reply[:2] == b'\x05\x00', 'SOCKS5 connect rejected')
                        sizes = {1: 4, 4: 16}
                        address_size = sizes.get(reply[3])
                        if address_size is None:
                            require(reply[3] == 3, 'Unknown SOCKS5 address type')
                            address_size = receive(sock, 1)[0]
                        receive(sock, address_size + 2)
                        context = ssl.create_default_context()
                        with context.wrap_socket(sock, server_hostname=hostname) as tls:
                            tls.sendall(('HEAD / HTTP/1.1\r\nHost: '+hostname+'\r\nConnection: close\r\n\r\n').encode())
                            return bool(tls.recv(64))

                # Both modes must retain GeoSite routing. A controlled IP+SNI
                # mismatch then proves the behavioral difference: false rewrites
                # the dial target to the sniffed SNI, while true preserves the IP.
                set_route_only(False)
                require(bool(fetch('https://api.ipify.org')), 'WARP failed with routeOnly=false')
                require(tls_to_ip_with_sni('8.8.8.8', 'example.com'),
                        'routeOnly=false did not rewrite sniffed SNI')
                set_route_only(True)
                require(bool(fetch('https://api.ipify.org')), 'WARP failed with routeOnly=true')
                try:
                    tls_to_ip_with_sni('8.8.8.8', 'example.com')
                except (ssl.SSLError, OSError):
                    pass
                else:
                    raise InstallError('routeOnly=true unexpectedly rewrote destination')
            finally:
                current = copy.deepcopy(api.call('inbounds/list')[0])
                sniffing = json_obj(current['sniffing'])
                sniffing['routeOnly'] = True
                current['sniffing'] = json.dumps(sniffing)
                api.call('inbounds/update/'+str(current['id']), current)
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        api.call('clients/del/'+client['email'], {})
        require(json_obj(api.call('inbounds/list')[0]['settings'])['clients'] == [], 'Temporary-client cleanup failed')
    return {'subscriptions':'real HTTP for Happ + INCY; Base64/JSON/deeplinks verified',
            'reality':'real Vision connection with Firefox fingerprint from second Xray process on same VPS',
            'sniffing':'GeoSite routing passed with routeOnly false/true; final true preserves the requested IP against SNI rewriting',
            'routing':'DIRECT example/trace + WARP IP-check + RU blocked',
            'cleanup':'zero clients restored', 'external_client':'not covered by on-host test'}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--e2e',action='store_true',help='Disposable VPS ONLY: temporarily create/remove one test client')
    parser.add_argument('--geodata-e2e',action='store_true',
                        help='Disposable VPS ONLY: exercise native scheduled geodata updates and rollback')
    args=parser.parse_args()
    s=json.loads(Path('/var/lib/selfsni/state.json').read_text())
    report=verify(s)
    if args.e2e:
        report['e2e']=e2e(s)
    if args.geodata_e2e:
        report['geodataE2E']=geodata_e2e(s)
    save('/var/lib/selfsni/acceptance.json',report)
    print(json.dumps(report,indent=2))
    require(all(x['passed'] for x in report['checks']),'Acceptance checks failed')

if __name__ == '__main__':
    main()
