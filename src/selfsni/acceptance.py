"""Real on-host acceptance. Temporary users are created ONLY with --e2e."""
import argparse
import base64
import json
import hashlib
import ssl
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.parse
import uuid

from .api import LocalTLS, Panel, request
from .core import json_obj, InstallError, require, run, save, token, free_port, wait_for
from .routing import TAG, profile


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
    def basic_inbound():
        ins = api.call('inbounds/list')
        require(len(ins) == 1 and ins[0]['port'] == 443 and ins[0]['enable'], 'Expected one REALITY inbound')
        i = ins[0]
        require(json_obj(i['settings'])['clients'] == [], 'Permanent clients must not be created')
        stream = json_obj(i['streamSettings'])
        r = stream['realitySettings']
        require(r['dest'] == '/run/selfsni/nginx.sock' and r['xver'] == 1 and r['serverNames'] == [s['domain']], 'Incorrect Self-SNI settings')
        require(r['privateKey'] == s['reality_private'], 'REALITY key mismatch')
        require(i['tag'] == TAG, 'Routing scope mismatch')
    check('one inbound, zero clients, Self-SNI parameters', basic_inbound)
    check('nginx configuration',lambda: run('nginx','-t'))
    check('Xray configuration',lambda: run(s['xray_bin'],'run','-test','-config','/usr/local/x-ui/bin/config.json'))
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
        for kind, key in [('happ','subRoutingRules'),('incy','subIncyRoutingRules')]:
            link = settings[key]
            require(link.startswith(kind+'://routing/onadd/'), 'Wrong subscription deeplink')
            p = json.loads(base64.b64decode(link.split('/onadd/')[1],validate=True))
            require(p == profile(kind), 'Stored routing profile differs')
    check('Happ/INCY stored profile roundtrip',presets)
    def routes():
        cases = [('yandex.ru','', 'block'),('test.su','', 'block'),('пример.рф'.encode('idna').decode(),'','block'),
                 ('','77.88.8.8','block'),('','2a02:6b8::1','block'),('chatgpt.com','','warp-outbound'),
                 ('api.ipify.org','','warp-outbound'),('example.com','','direct')]
        for domain, ip, expected in cases:
            result = api.call('xray/routeTest',dict(domain=domain,ip=ip,port=443,network='tcp',inboundTag=TAG),form=True)
            require(result.get('outboundTag') == expected, 'Unexpected destination route: ' + (domain or ip))
    check('Live Xray routing API: RU, AI, IP-check, DIRECT',routes)
    for service in ['x-ui','nginx','warp-svc','fail2ban',s['php_service']]:
        check('systemd '+service,lambda service=service: (run('systemctl','is-active',service),run('systemctl','is-enabled',service)))
    check('UFW active',lambda:expect('Status: active' in run('ufw','status'),'UFW inactive'))
    check('Fail2ban jails',lambda:(run('fail2ban-client','status','sshd'),run('fail2ban-client','status','3x-ipl')))
    check('Certbot timer',lambda:run('systemctl','is-enabled','certbot.timer'))
    return {'scope':'on-host; no external client/reboot claims', 'checks':checks}

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
        links = [x for x in text.splitlines() if x.startswith('incy://routing/onadd/')]
        require(len(links) == 1, 'INCY profile missing or duplicated')
        link = links[0]
        require('happ://' not in text,'Happ profile leaked to INCY')
    require(link.startswith(kind+'://routing/onadd/'),'Wrong deeplink scheme')
    p = json.loads(base64.b64decode(link.split('/onadd/')[1],validate=True))
    require(p == profile(kind),'Subscription profile mismatch')
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
                        'serverName':s['domain'],'fingerprint':'chrome','publicKey':s['reality_public'],'shortId':s['short_id'],'spiderX':'/'}}}]}
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
            finally:
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
            'reality':'real Vision connection from second Xray process on same VPS',
            'routing':'DIRECT example/trace + WARP IP-check + RU blocked',
            'cleanup':'zero clients restored', 'external_client':'not covered by on-host test'}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--e2e',action='store_true',help='Disposable VPS ONLY: temporarily create/remove one test client')
    args=parser.parse_args()
    s=json.loads(Path('/var/lib/selfsni/state.json').read_text())
    report=verify(s)
    if args.e2e:
        report['e2e']=e2e(s)
    save('/var/lib/selfsni/acceptance.json',report)
    print(json.dumps(report,indent=2))
    require(all(x['passed'] for x in report['checks']),'Acceptance checks failed')

if __name__ == '__main__':
    main()
