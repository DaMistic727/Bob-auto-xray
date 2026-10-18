import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from .api import Panel, request
from . import bbr
from .core import (ROOT, InstallError, require, run, write, save, token, free_port,
                   fqdn, download, verify_bundle, extract, wait_for, digest)
from .geodata import GEODATA_TIMEZONE, verify_server_geodata
from .portal import generate
from .routing import deeplink, inbound, server_template

STATE = Path('/var/lib/selfsni')
LOG_ENABLED = False

def stage(message):
    print('[selfsni] ' + message, flush=True)
    if LOG_ENABLED and STATE.exists():
        with (STATE / 'progress.log').open('a') as f:
            f.write(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' ' + message + '\n')

def render(name, values):
    text = (ROOT / 'templates' / name).read_text()
    for k, v in values.items():
        text = text.replace('@@' + k + '@@', str(v))
    require('@@' not in text, 'Unexpanded template: ' + name)
    return text

def os_info(path='/etc/os-release'):
    values = {}
    for line in Path(path).read_text().splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            values[k] = v.strip('"')
    require(values.get('ID') == 'ubuntu' and values.get('VERSION_ID') in ('22.04','24.04','26.04'),
            'Supported OS: Ubuntu 22.04, 24.04, 26.04 LTS')
    require(platform.machine() in ('x86_64', 'aarch64'), 'Supported architectures: amd64, arm64')
    return values

def preflight(args):
    require(os.geteuid() == 0, 'Run as root on the test/clean VPS')
    info = os_info()
    require(Path('/run/systemd/system').exists(), 'systemd is required')
    require(Path('/dev/net/tun').exists(), '/dev/net/tun is required for WARP')
    require(shutil.disk_usage('/').free >= 2 * 1024**3, 'At least 2 GiB free disk required')
    for p in [str(STATE), '/usr/local/x-ui', '/etc/x-ui', '/root/VPS-Server-Menu',
              '/etc/nginx', '/etc/letsencrypt', '/var/www/selfsni', '/var/lib/selfsni-acme',
              '/run/selfsni', '/etc/tmpfiles.d/selfsni.conf',
              '/etc/systemd/system/x-ui.service.d',
              '/etc/systemd/system/nginx.service.d/selfsni.conf',
              '/etc/systemd/system/x-ui.service', '/usr/bin/x-ui',
              '/etc/fail2ban/jail.local', '/etc/fail2ban/jail.d/selfsni-ssh.local',
              '/etc/apt/sources.list.d/cloudflare-client.list', str(bbr.SYSCTL_CONFIG),
              str(bbr.MODULE_CONFIG)]:
        # The existing official WARP repository is explicitly adopted with WARP.
        if args.adopt_warp and (p.endswith('/cloudflare-client.list') or p == '/root/VPS-Server-Menu'):
            continue
        require(not Path(p).exists(), 'Existing installation/configuration: ' + p)
    warp_present = shutil.which('warp-cli') is not None
    require(not warp_present or args.adopt_warp, 'Existing WARP: needs explicit --adopt-warp')
    require(not args.adopt_warp or warp_present, '--adopt-warp requires an existing installation')
    # Socket activation may expose ports absent from sshd_config; preserve both.
    ssh_ports = set()
    ssh_config = run('/usr/sbin/sshd', '-T')
    for line in ssh_config.splitlines():
        if line.startswith('port '):
            ssh_ports.add(int(line.split()[1]))
    connection = os.environ.get('SSH_CONNECTION', '').split()
    if len(connection) == 4:
        ssh_ports.add(int(connection[3]))
    listeners = run('ss', '-H', '-lntp')
    for line in listeners.splitlines():
        if 'sshd' in line:
            ssh_ports.add(int(line.split()[3].rsplit(':', 1)[1]))
    if args.ssh_port:
        ssh_ports.add(args.ssh_port)
    require(bool(ssh_ports) and all(1 <= x <= 65535 for x in ssh_ports), 'Cannot detect SSH port')
    allowed = ssh_ports | {53}
    if args.adopt_warp:
        allowed.add(40000)
    for line in listeners.splitlines():
        port = int(line.split()[3].rsplit(':', 1)[1])
        require(port in allowed, f'Foreign TCP listener on port {port}')
    for port in [80,443,62789,11111]:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('0.0.0.0', port))
            except OSError as e:
                raise InstallError(f'Required port {port} is occupied') from e
    if shutil.which('ufw'):
        for name in ['user.rules', 'user6.rules']:
            for line in Path('/etc/ufw', name).read_text().splitlines():
                if line.startswith('-A ufw-user-input '):
                    match = re.fullmatch(r'-A ufw-user-input -p tcp --dport (\d+) -j ACCEPT', line)
                    require(match is not None and int(match.group(1)) in ssh_ports, 'Existing non-SSH UFW rules')
    domain = fqdn(args.domain)
    addresses = {x[4][0] for x in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
    v4 = {x for x in addresses if ipaddress.ip_address(x).version == 4}
    public = run('curl', '-4fsS', '--connect-timeout', '10', '--max-time', '20', 'https://api.ipify.org').strip()
    require(ipaddress.ip_address(public).is_global and v4 == {public},
            'DNS A must contain only this public IPv4; disable CDN proxying')
    v6 = {x for x in addresses if ipaddress.ip_address(x).version == 6}
    if v6:
        local = json.loads(run('ip', '-j', '-6', 'address', 'show', 'scope', 'global'))
        own = {a['local'] for dev in local for a in dev.get('addr_info', [])}
        require(v6 <= own, 'AAAA points elsewhere: remove or correct AAAA')
        public6 = run('curl', '-6fsS', '--connect-timeout', '10', '--max-time', '20', 'https://api6.ipify.org').strip()
        require(public6 in v6, 'AAAA IPv6 connectivity is not verified')
    excluded = set(ssh_ports) | {80,443,40000,62789,11111}
    panel_port = free_port(excluded)
    sub_port = free_port(excluded | {panel_port})
    # Keep all preflight modes read-only. kmod is installed below before
    # configure() may need modprobe on a minimal Ubuntu image.
    bbr_report = bbr.preflight()
    return dict(domain=domain, ipv4=public, ubuntu=info['VERSION_ID'], codename=info['VERSION_CODENAME'],
                arch='amd64' if platform.machine() == 'x86_64' else 'arm64',
                ssh_ports=sorted(ssh_ports), panel_port=panel_port, sub_port=sub_port,
                panel_path='/' + token() + '/', sub_path='/' + token() + '/',
                username='u' + token(8), password=token(24), adopt_warp=args.adopt_warp,
                bbr_preflight=bbr_report,
                status='INSTALLING')

def firewall(s, public=False):
    for port in s['ssh_ports']:
        run('ufw', 'allow', str(port) + '/tcp', 'comment', 'Self-SNI SSH')
    run('ufw', 'allow', '80/tcp')
    run('ufw', 'allow', '443/tcp')
    if public:
        for port in [s['panel_port'], s['sub_port']]:
            run('ufw', 'allow', str(port) + '/tcp')
    run('ufw', 'default', 'deny', 'incoming')
    run('ufw', 'default', 'allow', 'outgoing')
    run('ufw', '--force', 'enable')

def install_apt_key(source, target):
    run('gpg', '--batch', '--yes', '--dearmor', '--output', target, source)
    # The installer runs under umask 077, while APT verifies repositories as
    # the unprivileged _apt user. Signing keys are public and must be readable.
    Path(target).chmod(0o644)


def install_warp(s, lock, work):
    stage('WARP: validate/adapt VPS-Server-Menu proxy configuration')
    if not s['adopt_warp']:
        key = '/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg'
        install_apt_key(ROOT / 'vendor/cloudflare-pubkey.gpg', key)
        write('/etc/apt/sources.list.d/cloudflare-client.list',
              f'deb [arch={s["arch"]} signed-by={key}] https://pkg.cloudflareclient.com/ {s["codename"]} main\n', 0o644)
        run('apt-get', 'update', timeout=300)
        require('Candidate: (none)' not in run('apt-cache', 'policy', 'cloudflare-warp'),
                'No official WARP package for this Ubuntu/architecture')
        run('apt-get', 'install', '-y', 'cloudflare-warp', timeout=600)
        run('systemctl', 'enable', '--now', 'warp-svc')
        wait_for(lambda: run('warp-cli', '--accept-tos', 'registration', 'new') is not None, 'WARP registration')
        for command in [('mode','proxy'), ('proxy','port','40000'), ('connect',)]:
            run('warp-cli', '--accept-tos', *command)
    else:
        settings = run('warp-cli', '--accept-tos', 'settings')
        require('proxy' in settings.lower(), 'Existing WARP is not in proxy mode')
        run('systemctl', 'enable', 'warp-svc')
    from .acceptance import check_warp
    wait_for(check_warp, 'WARP SOCKS5 functional trace')
    if Path('/root/VPS-Server-Menu').exists():
        require(run('git','-C','/root/VPS-Server-Menu','rev-parse','HEAD').strip() == lock['menu_commit'], 'Existing menu has a different revision')
        require(not run('git','-c','core.filemode=false','-C','/root/VPS-Server-Menu','diff','HEAD','--').strip(), 'Existing menu has local changes')
        return
    spec = lock['downloads']['menu.tar.gz']
    archive = download(spec['url'], work / 'menu.tgz', spec['sha256'])
    menu_dir = work / 'menu'
    menu_dir.mkdir()
    extract(archive, menu_dir)
    source = next(menu_dir.iterdir())
    shutil.copytree(source, '/root/VPS-Server-Menu')
    for p in Path('/root/VPS-Server-Menu').iterdir():
        if p.is_file() and (p.suffix == '.sh' or p.name in ('server-menu','ipv6-menu')):
            p.chmod(0o755)
            target = Path('/usr/local/bin') / p.name
            require(not target.exists() and not target.is_symlink(), 'Foreign menu command: ' + str(target))
            target.symlink_to(p)

def certificate(s, email):
    stage('nginx + Certbot HTTP-01 webroot')
    Path('/var/lib/selfsni/acme/.well-known/acme-challenge').mkdir(parents=True)
    # STATE contains no traversable secrets; give nginx access only via a dedicated webroot.
    run('install', '-d', '-m', '755', '/var/lib/selfsni-acme')
    # Keep private installer state 0700; use the public webroot in rendered config.
    cfg = render('nginx-http.conf', {'DOMAIN': s['domain']}).replace('/var/lib/selfsni/acme','/var/lib/selfsni-acme')
    write('/etc/nginx/sites-available/selfsni-http', cfg, 0o644)
    default = Path('/etc/nginx/sites-enabled/default')
    # This symlink was installed by apt in this run, not an existing configuration.
    require(default.is_symlink(), 'Unexpected nginx default site')
    default.unlink()
    Path('/etc/nginx/sites-enabled/selfsni-http').symlink_to('/etc/nginx/sites-available/selfsni-http')
    run('nginx', '-t')
    run('systemctl', 'enable', '--now', 'nginx')
    run('systemctl', 'reload', 'nginx')
    cmd = ['certbot','certonly','--non-interactive','--agree-tos','--webroot','-w',
           '/var/lib/selfsni-acme','-d',s['domain'],'--key-type','ecdsa']
    if email:
        cmd += ['--email', email]
    else:
        cmd += ['--register-unsafely-without-email']
    run(*cmd, timeout=300)
    run('systemctl', 'enable', '--now', 'certbot.timer')

def decoy(s):
    stage('Unique cloud portal + PHP-FPM + REALITY TLS socket')
    values = generate('/var/www/selfsni')
    sockets = list(Path('/run/php').glob('php[0-9]*-fpm.sock'))
    require(len(sockets) == 1, 'Expected one PHP-FPM version')
    s['php_service'] = sockets[0].name.replace('.sock', '')
    s['portal'] = values
    write('/etc/nginx/snippets/selfsni-fastcgi.conf',
          f'include fastcgi_params;\nfastcgi_param SCRIPT_FILENAME /var/www/selfsni/index.php;\n'
          f'fastcgi_param HTTPS on;\nfastcgi_pass unix:{sockets[0]};\n', 0o644)
    write('/etc/tmpfiles.d/selfsni.conf', 'd /run/selfsni 0750 root root -\n', 0o644)
    run('systemd-tmpfiles', '--create', '/etc/tmpfiles.d/selfsni.conf')
    write('/etc/nginx/conf.d/selfsni-tls.conf', render('nginx-tls.conf', dict(values, DOMAIN=s['domain'])), 0o644)
    write('/etc/systemd/system/nginx.service.d/selfsni.conf',
          '[Service]\nUMask=0007\nExecStartPre=/usr/bin/install -d -m 0750 -o root -g root /run/selfsni\n', 0o644)
    run('systemctl','daemon-reload')
    run('nginx','-t')
    run('systemctl','restart','nginx')
    run('systemctl','enable', s['php_service'])

def panel(s, lock, work):
    stage('Official non-interactive 3x-ui installer with verified offline downloads')
    cache = {}
    name = f'x-ui-linux-{s["arch"]}.tar.gz'
    spec = lock['downloads'][name]
    archive = download(spec['url'], work / name, spec['sha256'])
    cache[spec['url']] = str(archive)
    checksum = work / (name + '.sha256')
    write(checksum, spec['sha256'] + '  ' + name + '\n')
    cache[spec['url'] + '.sha256'] = str(checksum)
    for n in ['x-ui.sh','x-ui.service.debian']:
        cache[f'https://raw.githubusercontent.com/MHSanaei/3x-ui/{lock["xui_version"]}/{n}'] = str(ROOT / 'vendor/3x-ui' / n)
    mapping = work / 'curl-map.json'
    save(mapping, cache)
    shim = work / 'bin'
    shim.mkdir()
    shutil.copyfile(ROOT / 'tools/offline-curl.py', shim / 'curl')
    (shim / 'curl').chmod(0o755)
    env = dict(os.environ, PATH=str(shim) + ':' + os.environ['PATH'], SELFSNI_CURL_MAP=str(mapping),
               DEBIAN_FRONTEND='noninteractive', XUI_NONINTERACTIVE='1', XUI_SSL_MODE='none',
               XUI_ENABLE_FAIL2BAN='false', XUI_DB_TYPE='sqlite', XUI_SERVER_IP=s['ipv4'],
               XUI_USERNAME=s['username'], XUI_PASSWORD=s['password'],
               XUI_PANEL_PORT=str(s['panel_port']), XUI_WEB_BASE_PATH=s['panel_path'].strip('/'))
    # UFW already blocks the temporary plaintext panel during official bootstrap.
    run('bash', ROOT / 'vendor/3x-ui/install.sh', lock['xui_version'], env=env, timeout=900)
    cert = f'/etc/letsencrypt/live/{s["domain"]}/'
    run('/usr/local/x-ui/x-ui','setting','-webCert',cert+'fullchain.pem','-webCertKey',cert+'privkey.pem',
        '-listenIP','0.0.0.0')
    write('/etc/systemd/system/x-ui.service.d/20-selfsni.conf',
          '[Unit]\nAfter=nginx.service warp-svc.service network-online.target\nWants=nginx.service warp-svc.service network-online.target\n'
          '[Service]\nEnvironment=TZ=' + GEODATA_TIMEZONE + '\n'
          'ExecStartPre=/usr/bin/test -S /run/selfsni/nginx.sock\n', 0o644)
    run('systemctl','daemon-reload')
    run('systemctl','restart','x-ui')
    wait_for(lambda: request(s['domain'], s['panel_port'], s['panel_path'])[0] == 200, 'HTTPS panel')
    output = run('/usr/local/x-ui/x-ui','setting','-getApiToken')
    match = re.search(r'^apiToken:\s*(\S+)', output, re.M)
    require(match is not None, 'Official CLI API-token response changed')
    s['api_token'] = match.group(1)
    save(STATE/'state.json',s)
    configure_panel(s, lock)

def configure_panel(s, lock):
    cert = f'/etc/letsencrypt/live/{s["domain"]}/'
    api = Panel(s)
    settings = api.call('setting/all', {}, method='POST')
    settings.update(subEnable=True, subListen='0.0.0.0', subPort=s['sub_port'],
        subDomain=s['domain'], subPath=s['sub_path'], subURI=f'https://{s["domain"]}:{s["sub_port"]}{s["sub_path"]}',
        subCertFile=cert+'fullchain.pem', subKeyFile=cert+'privkey.pem',
        subEncrypt=True, subEnableRouting=True, subRoutingRules=deeplink('happ'),
        subIncyEnableRouting=True, subIncyRoutingRules=deeplink('incy'),
        subHappAutoDetect=True, subHappTunType='xray', subHappLocalProxyAuth='auto',
        subIncyAppAutoDetect=True,
        subJsonEnable=False, subJsonAutoDetect=False, subClashEnable=False,
        webListen='0.0.0.0')
    api.call('setting/update', settings)
    # Keep the stock databases from the SHA256-pinned 3x-ui archive. Happ/INCY
    # profiles use their separately pinned DigneZzZ mobile datasets.
    geodata_root = Path('/usr/local/x-ui/bin')
    for name, expected in lock['server_geodata']['files'].items():
        require(digest(geodata_root/name) == expected, 'Unexpected server geodata: '+name)
    verify_server_geodata(geodata_root)
    view = api.call('xray/', {}, method='POST')
    if isinstance(view, str):
        view = json.loads(view)
    xray = view['xraySetting']
    if isinstance(xray, str):
        xray = json.loads(xray)
    api.call('xray/update', {'xraySetting': json.dumps(server_template(xray))}, form=True)
    require(not api.call('inbounds/list'), 'Fresh panel contains pre-existing inbounds')
    s['xray_bin'] = '/usr/local/x-ui/bin/xray-linux-' + s['arch']
    keys = run(s['xray_bin'], 'x25519')
    private = re.search(r'PrivateKey:\s*(\S+)', keys)
    public = re.search(r'(?:Password(?: \(PublicKey\))?|PublicKey):\s*(\S+)', keys)
    require(private is not None and public is not None, 'Unknown Xray x25519 output')
    s['reality_private'] = private.group(1)
    s['reality_public'] = public.group(1)
    s['short_id'] = token(8)
    save(STATE/'state.json',s)
    api.call('inbounds/add', inbound(s['domain'], s['reality_private'], s['reality_public'], s['short_id']))
    run('systemctl','restart','x-ui')
    wait_for(lambda: request(s['domain'],s['sub_port'],s['sub_path']+'missing')[0] in (400,404), 'TLS subscriptions')
    s['xray_version'] = run(s['xray_bin'], 'version').splitlines()[0]
    s['xui_version'] = verify_bundle()['xui_version']

def security(s):
    stage('Official 3x-ui Fail2ban integration + SSH jail')
    run('/usr/bin/x-ui','setup-fail2ban',timeout=300)
    # The official IP-limit jail is retained; this is an additional sshd override.
    write('/etc/fail2ban/jail.d/selfsni-ssh.local',
          '[sshd]\nenabled = true\nbackend = systemd\nport = ' + ','.join(map(str,s['ssh_ports'])) + '\n', 0o644)
    run('systemctl','enable','--now','fail2ban')
    run('systemctl','restart','fail2ban')
    wait_for(lambda: 'sshd' in run('fail2ban-client','status'), 'Fail2ban sshd jail')

def renew_hook(s):
    write('/etc/letsencrypt/renewal-hooks/deploy/50-selfsni',
          '#!/bin/sh\nset -eu\nnginx -t\nsystemctl reload nginx\nsystemctl restart x-ui\n', 0o700)
    # All services use the live certificate path directly; no stale copies.
    run('certbot','renew','--dry-run','--run-deploy-hooks','--no-random-sleep-on-renew',timeout=600)

def main():
    global LOG_ENABLED
    owns_state = False
    parser = argparse.ArgumentParser(description='One-shot VLESS REALITY Self-SNI installer')
    parser.add_argument('--domain')
    parser.add_argument('--email', default='')
    parser.add_argument('--ssh-port', type=int)
    parser.add_argument('--adopt-warp', action='store_true')
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    if not args.domain:
        with open('/dev/tty') as tty:
            print('Server FQDN: ', end='', flush=True)
            args.domain = tty.readline().strip()
            print('Let’s Encrypt email (optional): ', end='', flush=True)
            args.email = tty.readline().strip()
    try:
        lock = verify_bundle()
        stage('Preflight: OS, clean host, SSH, listeners, DNS, network')
        s = preflight(args)
        if args.preflight_only:
            stage('PREFLIGHT PASSED; no configuration changed')
            return
        STATE.mkdir(mode=0o700)
        owns_state = True
        LOG_ENABLED = True
        os.umask(0o077)
        save(STATE/'state.json',s)
        with tempfile.TemporaryDirectory(prefix='selfsni-') as wd:
            work = Path(wd)
            stage('Install distribution packages')
            os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
            run('apt-get','update',timeout=300)
            run('apt-get','install','-y','nginx','certbot','php-fpm','php-cli','curl','openssl',
                'ufw','fail2ban','nftables','python3','python3-systemd','gnupg','ca-certificates',
                'dnsutils','bc','jq','qrencode','git','iproute2','kmod',timeout=900)
            stage('TCP BBR congestion control + FQ pacing')
            s['bbr'] = bbr.configure()
            save(STATE/'state.json',s)
            firewall(s)
            install_warp(s,lock,work)
            certificate(s,args.email)
            decoy(s)
            panel(s,lock,work)
            save(STATE/'state.json',s)
            security(s)
            renew_hook(s)
            firewall(s,public=True)
            from .acceptance import verify
            report = verify(s)
            save(STATE/'acceptance.json',report)
            require(all(x['passed'] for x in report['checks']), 'Acceptance failed; inspect /var/lib/selfsni/acceptance.json')
            s['status'] = 'HEALTHY_LOCAL'
            s['versions'] = run('dpkg-query','-W','nginx','php-fpm','certbot','cloudflare-warp','fail2ban','ufw')
            save(STATE/'state.json',s)
            (STATE/'FAILED').unlink(missing_ok=True)
        print(f'\nINSTALLATION COMPLETED · local acceptance passed\nPanel: https://{s["domain"]}:{s["panel_port"]}{s["panel_path"]}\nUsername: {s["username"]}\nPassword: {s["password"]}\nSubscription base: https://{s["domain"]}:{s["sub_port"]}{s["sub_path"]}\nClients: 0 · add clients with flow xtls-rprx-vision\nReport: /var/lib/selfsni/acceptance.json\nExternal client and reboot acceptance are separate required tests.')
    except (InstallError, OSError, ValueError, KeyError) as e:
        stage('FAILED: ' + str(e))
        if owns_state:
            write(STATE/'FAILED', str(e)+'\n')
        raise SystemExit(1)

if __name__ == '__main__':
    main()
