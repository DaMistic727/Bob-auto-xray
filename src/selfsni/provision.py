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
from .core import (ROOT, CommandError, InstallError, configure_diagnostics, redact,
                   register_secrets, require, run, write, save, token, free_port,
                   fqdn, download, verify_bundle, extract, wait_for, digest)
from .apt import AptRunner
from .install_state import (InstallerLock, checkpoint, fail, initialize, load,
                            upgrade_legacy_apt_failure, validate_resume)
from .ui import InstallerUI, interactive_wizard
from .geodata import GEODATA_TIMEZONE, verify_server_geodata
from .portal import generate
from .routing import deeplink, inbound, server_template

STATE = Path('/var/lib/selfsni')
DIAGNOSTIC_LOG = Path('/var/log/bob-auto-xray/install.log')
INSTALL_LOCK = Path('/run/lock/bob-auto-xray-install.lock')
LOG_ENABLED = False
UI = None

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

CONTROLLED_PATHS = [
    '/usr/local/x-ui', '/etc/x-ui', '/root/VPS-Server-Menu', '/etc/nginx',
    '/etc/letsencrypt', '/var/www/selfsni', '/var/lib/selfsni-acme', '/run/selfsni',
    '/etc/tmpfiles.d/selfsni.conf', '/etc/systemd/system/x-ui.service.d',
    '/etc/systemd/system/nginx.service.d/selfsni.conf',
    '/etc/systemd/system/x-ui.service', '/usr/bin/x-ui', '/etc/fail2ban/jail.local',
    '/etc/fail2ban/jail.d/selfsni-ssh.local',
    '/etc/apt/sources.list.d/cloudflare-client.list', str(bbr.SYSCTL_CONFIG),
    str(bbr.MODULE_CONFIG),
]


def preflight(args, resume_state=None):
    require(os.geteuid() == 0, 'Run as root on the test/clean VPS')
    info = os_info()
    require(Path('/run/systemd/system').exists(), 'systemd is required')
    require(Path('/dev/net/tun').exists(), '/dev/net/tun is required for WARP')
    require(shutil.disk_usage('/').free >= 2 * 1024**3, 'At least 2 GiB free disk required')
    for p in [str(STATE), *CONTROLLED_PATHS]:
        if resume_state and p == str(STATE):
            continue
        if resume_state and resume_state.get('stage') in ('APT_INSTALL', 'WARP', 'CERTIFICATE') and \
                p in resume_state.get('absent_paths', ()):
            allowed_resume_paths = {'/etc/nginx', '/etc/letsencrypt'}
            if resume_state.get('stage') in ('WARP', 'CERTIFICATE'):
                allowed_resume_paths.update({'/root/VPS-Server-Menu',
                    '/etc/apt/sources.list.d/cloudflare-client.list',
                    str(bbr.SYSCTL_CONFIG), str(bbr.MODULE_CONFIG)})
            if resume_state.get('stage') == 'CERTIFICATE':
                allowed_resume_paths.add('/var/lib/selfsni-acme')
            if p in allowed_resume_paths:
                if p == '/etc/nginx':
                    enabled = Path('/etc/nginx/sites-enabled')
                    entries = list(enabled.iterdir()) if enabled.exists() else []
                    expected = {'selfsni-http'} if resume_state.get('stage') == 'CERTIFICATE' else {'default'}
                    require(all(x.name in expected for x in entries),
                            'Чужая nginx-конфигурация обнаружена во время resume')
                if p == '/etc/letsencrypt':
                    le = Path('/etc/letsencrypt')
                    foreign = [x for name in ('live', 'archive', 'renewal')
                               for x in (le/name).glob('*')] if le.exists() else []
                    require(not foreign,
                            'Чужие Let’s Encrypt certificates обнаружены во время resume')
                continue
        # The existing official WARP repository is explicitly adopted with WARP.
        if args.adopt_warp and (p.endswith('/cloudflare-client.list') or p == '/root/VPS-Server-Menu'):
            continue
        require(not Path(p).exists(), 'Existing installation/configuration: ' + p)
    warp_present = shutil.which('warp-cli') is not None
    warp_resume = bool(resume_state and resume_state.get('stage') in ('WARP', 'CERTIFICATE'))
    certificate_resume = bool(resume_state and resume_state.get('stage') == 'CERTIFICATE')
    require(not warp_present or args.adopt_warp or warp_resume,
            'Existing WARP: needs explicit --adopt-warp')
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
    if args.adopt_warp or warp_resume:
        allowed.add(40000)
    if certificate_resume:
        allowed.add(80)
    for line in listeners.splitlines():
        port = int(line.split()[3].rsplit(':', 1)[1])
        require(port in allowed, f'Foreign TCP listener on port {port}')
    for port in [80,443,62789,11111]:
        if certificate_resume and port == 80:
            continue
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
                    resume_ports = {80, 443} if warp_resume else set()
                    require(match is not None and int(match.group(1)) in ssh_ports | resume_ports,
                            'Existing non-SSH UFW rules')
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
    if resume_state:
        panel_port = int(resume_state['panel_port'])
        sub_port = int(resume_state['sub_port'])
        require(panel_port not in excluded and sub_port not in excluded,
                'Сохранённые порты конфликтуют с системными')
    else:
        panel_port = free_port(excluded)
        sub_port = free_port(excluded | {panel_port})
    # Keep all preflight modes read-only. kmod is installed below before
    # configure() may need modprobe on a minimal Ubuntu image.
    bbr_report = bbr.preflight()
    credentials = (dict(panel_path=resume_state['panel_path'], sub_path=resume_state['sub_path'],
                        username=resume_state['username'], password=resume_state['password'])
                   if resume_state else
                   dict(panel_path='/' + token() + '/', sub_path='/' + token() + '/',
                        username='u' + token(8), password=token(24)))
    fresh = dict(domain=domain, ipv4=public, ubuntu=info['VERSION_ID'], codename=info['VERSION_CODENAME'],
                arch='amd64' if platform.machine() == 'x86_64' else 'arm64',
                ssh_ports=sorted(ssh_ports), panel_port=panel_port, sub_port=sub_port,
                adopt_warp=args.adopt_warp, bbr_preflight=bbr_report, **credentials,
                status='INSTALLING')
    if resume_state:
        preserved = ('panel_path', 'sub_path', 'username', 'password', 'panel_port', 'sub_port',
                     'installation_id', 'schema', 'absent_paths', 'resume_count', 'stage',
                     'last_completed_stage', 'legacy_resume')
        fresh.update({key: resume_state[key] for key in preserved if key in resume_state})
    return fresh

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


def _tree_manifest(root):
    root = Path(root)
    result = {}
    for path in sorted(root.rglob('*')):
        if '.git' in path.parts or not path.is_file():
            continue
        result[str(path.relative_to(root))] = digest(path)
    return result


def _health_json(output):
    starts = [0] + [match.end() for match in re.finditer(r'\n(?=\{)', output)]
    for start in reversed(starts):
        try:
            value = json.loads(output[start:])
            if isinstance(value, dict) and 'status' in value:
                return value
        except json.JSONDecodeError:
            pass
    raise InstallError('VPS-Server-Menu вернул некорректный health JSON')


def _menu_warp(*arguments):
    try:
        output = run('/usr/local/bin/server-menu', 'warp', *arguments, timeout=1200)
    except CommandError as error:
        health = _health_json(error.output) if '{' in error.output else None
        if health:
            raise InstallError(f'WARP {health["status"]}: {health.get("reason", "без причины")}') from error
        raise
    return _health_json(output)


def install_menu(lock, work):
    target = Path('/root/VPS-Server-Menu')
    commit = lock['menu_commit']
    if target.exists():
        require((target/'.git').is_dir(), 'Existing VPS-Server-Menu is not a Git checkout')
        require(run('git','-C',target,'rev-parse','HEAD').strip() == commit,
                'Existing menu has a different revision')
        require(not run('git','-c','core.filemode=false','-C',target,'diff','HEAD','--').strip(),
                'Existing menu has local changes')
    else:
        spec = lock['downloads']['menu.tar.gz']
        archive = download(spec['url'], work / 'menu.tgz', spec['sha256'])
        unpacked = work / 'menu-archive'
        unpacked.mkdir()
        extract(archive, unpacked)
        source = next(unpacked.iterdir())
        for path in source.iterdir():
            if path.is_file() and (path.suffix == '.sh' or path.name in ('server-menu','ipv6-menu')):
                destination = Path('/usr/local/bin') / path.name
                require(not destination.exists() and not destination.is_symlink(),
                        'Foreign menu command: ' + str(destination))
        target.mkdir(mode=0o700)
        repository = lock.get('menu_repository', 'https://github.com/Diman6267/VPS-Server-Menu.git')
        run('git', '-C', target, 'init')
        run('git', '-C', target, 'remote', 'add', 'origin', repository)
        run('git', '-C', target, 'fetch', '--depth=1', 'origin', commit, timeout=300)
        run('git', '-C', target, 'checkout', '--detach', 'FETCH_HEAD')
        require(run('git','-C',target,'rev-parse','HEAD').strip() == commit,
                'Fetched menu revision mismatch')
        require(_tree_manifest(source) == _tree_manifest(target),
                'Menu Git checkout differs from the SHA256-pinned archive')
    run('bash', target/'install.sh', '--non-interactive', '--no-autostart',
        '--source-dir', target, '--pinned-commit', commit, timeout=900)
    require(Path('/usr/local/bin/server-menu').is_symlink(), 'server-menu link was not installed')


def install_warp(s, lock, work):
    stage('WARP: VPS-Server-Menu non-interactive API')
    install_menu(lock, work)
    if s['adopt_warp']:
        health = _menu_warp('health', '--port', '40000', '--json')
    else:
        health = _menu_warp('install', '--port', '40000', '--json')
        health = _menu_warp('health', '--port', '40000', '--json')
    require(health.get('status') == 'HEALTHY',
            f'WARP {health.get("status", "UNKNOWN")}: {health.get("reason", "без причины")}')
    require(health.get('mode') == 'proxy' and health.get('port') == 40000,
            'VPS-Server-Menu подтвердил неожиданный WARP mode/port')
    # Independent functional acceptance confirms the actual routed SOCKS5 path.
    from .acceptance import check_warp
    check_warp()
    s['warp_health'] = health

def certificate(s, email):
    stage('nginx + Certbot HTTP-01 webroot')
    Path('/var/lib/selfsni/acme/.well-known/acme-challenge').mkdir(parents=True)
    # STATE contains no traversable secrets; give nginx access only via a dedicated webroot.
    run('install', '-d', '-m', '755', '/var/lib/selfsni-acme')
    # Keep private installer state 0700; use the public webroot in rendered config.
    cfg = render('nginx-http.conf', {'DOMAIN': s['domain']}).replace('/var/lib/selfsni/acme','/var/lib/selfsni-acme')
    write('/etc/nginx/sites-available/selfsni-http', cfg, 0o644)
    default = Path('/etc/nginx/sites-enabled/default')
    enabled = Path('/etc/nginx/sites-enabled/selfsni-http')
    if default.exists() or default.is_symlink():
        # This symlink was installed by apt in this run, not an existing configuration.
        require(default.is_symlink(), 'Unexpected nginx default site')
        default.unlink()
    if enabled.exists() or enabled.is_symlink():
        require(enabled.is_symlink() and enabled.resolve() == Path('/etc/nginx/sites-available/selfsni-http'),
                'Unexpected Self-SNI HTTP site')
    else:
        enabled.symlink_to('/etc/nginx/sites-available/selfsni-http')
    run('nginx', '-t')
    run('systemctl', 'enable', '--now', 'nginx')
    run('systemctl', 'reload', 'nginx')
    cmd = ['certbot','certonly','--non-interactive','--agree-tos','--webroot','-w',
           '/var/lib/selfsni-acme','-d',s['domain'],'--key-type','ecdsa']
    if email:
        cmd += ['--email', email]
    else:
        cmd += ['--register-unsafely-without-email']
    try:
        run(*cmd, timeout=300)
    except CommandError as error:
        details = [line.strip() for line in redact(error.output).splitlines()
                   if re.search(r'(?i)(error|too many certificates|retry after|unauthorized|challenge)', line)]
        suffix = ': ' + ' | '.join(details[-4:]) if details else ''
        raise InstallError(f'Certbot exited {error.returncode}{suffix}') from error
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
    register_secrets(s['api_token'])
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
    register_secrets(s['reality_private'], s['reality_public'], s['short_id'])
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
    global LOG_ENABLED, UI
    owns_state = False
    state = None
    parser = argparse.ArgumentParser(description='Bob Auto Xray VLESS REALITY Self-SNI installer')
    parser.add_argument('--domain')
    parser.add_argument('--email', default='')
    parser.add_argument('--ssh-port', type=int)
    parser.add_argument('--adopt-warp', action='store_true')
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    if not args.domain:
        version = (ROOT/'VERSION').read_text().strip()
        interactive_wizard(args, version)
    else:
        args.domain = fqdn(args.domain)
    UI = InstallerUI()
    try:
        lock = verify_bundle()
        if args.preflight_only:
            UI.stage(1, 13, 'Проверка системы')
            preflight(args)
            UI.stage(1, 13, 'Проверка системы', 'ok')
            stage('PREFLIGHT PASSED; no configuration changed')
            return
        with InstallerLock(INSTALL_LOCK):
            previous = load(STATE)
            if previous:
                previous = upgrade_legacy_apt_failure(previous, STATE, CONTROLLED_PATHS)
                previous = validate_resume(previous, args.domain, args.adopt_warp)
                require(not args.email or args.email == previous.get('email', ''),
                        'Для продолжения используйте исходный Let’s Encrypt email')
                args.email = previous.get('email', '')
            UI.stage(1, 13, 'Проверка системы')
            stage('Preflight: OS, ownership, SSH, listeners, DNS, network')
            s = preflight(args, previous)
            s['email'] = args.email
            UI.stage(1, 13, 'Проверка системы', 'ok')
            os.umask(0o077)
            if previous:
                s['status'] = 'INSTALLING'
            else:
                absent = [path for path in CONTROLLED_PATHS if not Path(path).exists()]
                s = initialize(s, absent)
                STATE.mkdir(mode=0o700)
            owns_state = True
            LOG_ENABLED = True
            configure_diagnostics(DIAGNOSTIC_LOG, (s['username'], s['password'],
                                                   s['panel_path'], s['sub_path']))
            save(STATE/'state.json',s)
            packages = ['nginx','certbot','php-fpm','php-cli','curl','openssl','ufw',
                        'fail2ban','nftables','python3','python3-systemd','gnupg',
                        'ca-certificates','dnsutils','bc','jq','qrencode','git','iproute2','kmod']
            apt = AptRunner(run, notify=UI.note)
            resume_target = s.get('stage')
            apt_steps = [('APT_UPDATE', 2, 'Обновление индексов APT', apt.update),
                         ('APT_UPGRADE', 3, 'Обновление Ubuntu', apt.upgrade),
                         ('APT_INSTALL', 4, 'Установка зависимостей', lambda: apt.install(packages))]
            resume_index = next((i for i, item in enumerate(apt_steps)
                                 if item[0] == s.get('stage')), len(apt_steps))
            for name, number, label, action in apt_steps[resume_index:]:
                checkpoint(STATE, s, name)
                UI.stage(number, 13, label)
                action()
                checkpoint(STATE, s, name, completed=True)
                UI.stage(number, 13, label, 'ok')
            if Path('/var/run/reboot-required').exists():
                UI.note('Ubuntu сообщает о необходимости reboot; автоматическая перезагрузка отключена.')
            with tempfile.TemporaryDirectory(prefix='selfsni-') as wd:
                work = Path(wd)
                if resume_target not in ('WARP', 'CERTIFICATE'):
                    checkpoint(STATE, s, 'BBR'); UI.stage(5, 13, 'TCP BBR и FQ')
                    s['bbr'] = bbr.configure(); checkpoint(STATE, s, 'BBR', True)
                    checkpoint(STATE, s, 'FIREWALL'); UI.stage(6, 13, 'Firewall')
                    firewall(s); checkpoint(STATE, s, 'FIREWALL', True)
                if resume_target != 'CERTIFICATE':
                    checkpoint(STATE, s, 'WARP'); UI.stage(7, 13, 'VPS-Server-Menu и WARP')
                    install_warp(s,lock,work); checkpoint(STATE, s, 'WARP', True)
                else:
                    health = _menu_warp('health', '--port', '40000', '--json')
                    require(health.get('status') == 'HEALTHY',
                            f'WARP {health.get("status", "UNKNOWN")}: {health.get("reason", "без причины")}')
                    from .acceptance import check_warp
                    check_warp()
                checkpoint(STATE, s, 'CERTIFICATE'); UI.stage(8, 13, 'Сертификат')
                certificate(s,s['email']); checkpoint(STATE, s, 'CERTIFICATE', True)
                checkpoint(STATE, s, 'NGINX_PORTAL'); UI.stage(9, 13, 'Self-SNI portal')
                decoy(s); checkpoint(STATE, s, 'NGINX_PORTAL', True)
                checkpoint(STATE, s, 'PANEL'); UI.stage(10, 13, '3x-ui')
                panel(s,lock,work); checkpoint(STATE, s, 'PANEL', True)
                checkpoint(STATE, s, 'SECURITY'); UI.stage(11, 13, 'Fail2ban и renew hook')
                security(s); renew_hook(s); firewall(s,public=True)
                checkpoint(STATE, s, 'SECURITY', True)
                checkpoint(STATE, s, 'ACCEPTANCE'); UI.stage(12, 13, 'Acceptance checks')
                from .acceptance import verify
                report = verify(s)
                save(STATE/'acceptance.json',report)
                require(all(x['passed'] for x in report['checks']),
                        'Acceptance failed; inspect /var/lib/selfsni/acceptance.json')
                checkpoint(STATE, s, 'ACCEPTANCE', True)
                s['status'] = 'HEALTHY_LOCAL'; s['stage'] = 'HEALTHY_LOCAL'
                s['last_completed_stage'] = 'HEALTHY_LOCAL'
                s.pop('failure', None)
                s['versions'] = run('dpkg-query','-W','nginx','php-fpm','certbot',
                                    'cloudflare-warp','fail2ban','ufw')
                save(STATE/'state.json',s)
                (STATE/'FAILED').unlink(missing_ok=True)
                UI.stage(13, 13, 'Готово', 'ok')
        print(f'\nINSTALLATION COMPLETED · local acceptance passed\nPanel: https://{s["domain"]}:{s["panel_port"]}{s["panel_path"]}\nUsername: {s["username"]}\nPassword: {s["password"]}\nSubscription base: https://{s["domain"]}:{s["sub_port"]}{s["sub_path"]}\nClients: 0 · add clients with flow xtls-rprx-vision\nReport: /var/lib/selfsni/acceptance.json\nExternal client and reboot acceptance are separate required tests.')
    except (InstallError, OSError, ValueError, KeyError) as e:
        message = redact(str(e))
        stage('FAILED: ' + message)
        if owns_state:
            current = locals().get('s', state)
            if current:
                fail(STATE, current, message)
            write(STATE/'FAILED', message+'\n')
        if UI:
            UI.failure(message, DIAGNOSTIC_LOG if owns_state else 'не создан (preflight)')
        raise SystemExit(1)

if __name__ == '__main__':
    main()
