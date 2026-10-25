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
from . import bbr, ipv6
from .core import (ROOT, CommandError, InstallError, configure_diagnostics, redact,
                   register_secrets, require, run, write, save, token, free_port,
                   fqdn, download, verify_bundle, wait_for, digest,
                   configure_activity)
from .certbot_errors import explain as explain_certbot
from .apt import AptRunner
from .menu_upstream import check_official_menu, install_menu
from . import menu_warp
from .install_state import (InstallerLock, checkpoint, fail, initialize, load,
                            upgrade_legacy_apt_failure, validate_resume, warp_mode)
from .ui import InstallerUI, interactive_wizard
from .geodata import GEODATA_TIMEZONE, verify_server_geodata
from .portal import generate
from .routing import deeplink, inbound, server_template
from .xray_pin import install_pinned_xray

STATE = Path('/var/lib/selfsni')
DIAGNOSTIC_LOG = Path('/var/log/bob-auto-xray/install.log')
INSTALL_LOCK = Path('/run/lock/bob-auto-xray-install.lock')
LOG_ENABLED = False
UI = None

def stage(message):
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
    str(bbr.MODULE_CONFIG), str(ipv6.CONFIG),
]


def preflight(args, resume_state=None):
    require(os.geteuid() == 0, 'Run as root on the test/clean VPS')
    info = os_info()
    require(Path('/run/systemd/system').exists(), 'systemd is required')
    if args.warp_mode != 'skip':
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
                    str(bbr.SYSCTL_CONFIG), str(bbr.MODULE_CONFIG), str(ipv6.CONFIG)})
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
        # Existing WARP files belong to the operator in adopt/skip mode.
        if (p == '/root/VPS-Server-Menu' and args.warp_mode != 'skip') or (
                args.warp_mode in ('adopt', 'skip') and
                (p.endswith('/cloudflare-client.list') or p == '/root/VPS-Server-Menu')):
            continue
        require(not Path(p).exists(), 'Existing installation/configuration: ' + p)
    warp_present = shutil.which('warp-cli') is not None if args.warp_mode != 'skip' else False
    warp_resume = bool(args.warp_mode == 'install' and resume_state and resume_state.get('stage') in ('WARP', 'CERTIFICATE'))
    certificate_resume = bool(resume_state and resume_state.get('stage') == 'CERTIFICATE')
    require(not warp_present or args.warp_mode in ('adopt', 'skip') or warp_resume,
            'Existing WARP: needs explicit --adopt-warp')
    require(args.warp_mode != 'adopt' or warp_present, '--adopt-warp requires an existing installation')
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
    if args.warp_mode == 'adopt' or warp_resume:
        allowed.add(40000)
    if args.warp_mode == 'skip':
        # A pre-existing WARP daemon is outside this installation's ownership.
        allowed.update(int(line.split()[3].rsplit(':', 1)[1]) for line in listeners.splitlines()
                       if 'warp-svc' in line)
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
                    resume_ports = {80, 443} if resume_state and resume_state.get('stage') in ('WARP', 'CERTIFICATE') else set()
                    require(match is not None and int(match.group(1)) in ssh_ports | resume_ports,
                            'Existing non-SSH UFW rules')
    domain = fqdn(args.domain)
    addresses = {x[4][0] for x in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
    v4 = {x for x in addresses if ipaddress.ip_address(x).version == 4}
    public = run('curl', '-4fsS', '--connect-timeout', '10', '--max-time', '20', 'https://api.ipify.org').strip()
    require(ipaddress.ip_address(public).is_global and v4 == {public},
            'DNS A must contain only this public IPv4; disable CDN proxying')
    v6 = {x for x in addresses if ipaddress.ip_address(x).version == 6}
    require(not v6, 'Installation FQDN has an AAAA record; IPv4-only requires removing it')
    ipv6_report = ipv6.preflight(domain)
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
                warp_mode=args.warp_mode, adopt_warp=args.warp_mode == 'adopt',
                bbr_preflight=bbr_report, ipv6_preflight=ipv6_report, **credentials,
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


def install_warp(s, lock, work):
    stage('WARP: официальный VPS-Server-Menu')
    install_menu(lock, work)
    s['warp_health'] = menu_warp.install()


def adopt_warp(s, lock, work):
    """Install/check the menu, then use its read-only health command."""
    install_menu(lock, work)
    s['warp_health'] = menu_warp.health()

def certificate(s, email):
    stage('nginx + Certbot HTTP-01 webroot')
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
        raise InstallError('Не удалось получить SSL-сертификат.\n' + explain_certbot(error.output) +
                           '\nНастройки предыдущих этапов сохранены. После устранения причины '
                           'запустите установщик повторно для продолжения.') from error
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
    s['xray_sha256'] = install_pinned_xray(lock, s['arch'], work)
    cert = f'/etc/letsencrypt/live/{s["domain"]}/'
    run('/usr/local/x-ui/x-ui','setting','-webCert',cert+'fullchain.pem','-webCertKey',cert+'privkey.pem',
        '-listenIP','0.0.0.0')
    write('/etc/systemd/system/x-ui.service.d/20-selfsni.conf',
          xui_unit_override(s.get('warp_mode', 'install')),
          0o644)
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


def xui_unit_override(mode):
    require(mode in ('install', 'adopt', 'skip'), 'Unknown WARP mode')
    warp_unit = ' warp-svc.service' if mode != 'skip' else ''
    return (f'[Unit]\nAfter=nginx.service{warp_unit} network-online.target\n'
            f'Wants=nginx.service{warp_unit} network-online.target\n'
            '[Service]\nEnvironment=TZ=' + GEODATA_TIMEZONE + '\n'
            'ExecStartPre=/usr/bin/test -S /run/selfsni/nginx.sock\n')


def version_packages(mode):
    require(mode in ('install', 'adopt', 'skip'), 'Unknown WARP mode')
    packages = ['nginx', 'php-fpm', 'certbot', 'fail2ban', 'ufw']
    if mode != 'skip':
        packages.append('cloudflare-warp')
    return packages

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
    api.call('xray/update', {'xraySetting': json.dumps(server_template(xray, s.get('warp_mode', 'install')))}, form=True)
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
    s['xui_version'] = lock['xui_version']

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
    parser = argparse.ArgumentParser(description='Bob Auto Xray: автоматическая настройка VPN-сервера')
    parser.add_argument('--domain')
    parser.add_argument('--email', default='')
    parser.add_argument('--ssh-port', type=int)
    warp_flags = parser.add_mutually_exclusive_group()
    warp_flags.add_argument('--adopt-warp', action='store_true')
    warp_flags.add_argument('--no-warp', action='store_true')
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    explicit_warp_mode = args.adopt_warp or args.no_warp
    args.warp_mode = 'adopt' if args.adopt_warp else 'skip' if args.no_warp else None
    UI = InstallerUI()
    configure_activity(UI.pulse)
    try:
        resume_preview = load(STATE) if not args.preflight_only else None
        if resume_preview and not args.domain:
            args.domain = resume_preview['domain']
            UI.note(f'Продолжаем сохранённую установку для {args.domain}. Параметры будут использованы повторно.')
        if resume_preview and not explicit_warp_mode:
            args.warp_mode = warp_mode(resume_preview)
        if not args.domain:
            version = (ROOT/'VERSION').read_text().strip()
            interactive_wizard(args, version)
        else:
            args.domain = fqdn(args.domain)
        if args.warp_mode is None:
            args.warp_mode = 'install'
        args.adopt_warp = args.warp_mode == 'adopt'
        lock = verify_bundle(args.warp_mode)
        if not args.preflight_only and args.warp_mode in ('install', 'adopt'):
            check_official_menu(lock)
        if args.preflight_only:
            UI.stage(1, 13, 'Проверка системы')
            preflight(args)
            UI.stage(1, 13, 'Проверка системы', 'ok')
            UI.note('Проверка пройдена. Система не изменена.')
            return
        with InstallerLock(INSTALL_LOCK):
            previous = load(STATE)
            if previous:
                previous = upgrade_legacy_apt_failure(previous, STATE, CONTROLLED_PATHS)
                previous = validate_resume(previous, args.domain, args.warp_mode)
                require(not args.email or args.email == previous.get('email', ''),
                        'Для продолжения используйте исходный Let’s Encrypt email')
                args.email = previous.get('email', '')
                UI.completed = {'APT_UPDATE': 1, 'APT_UPGRADE': 2, 'APT_INSTALL': 3,
                                'WARP': 6, 'CERTIFICATE': 7}.get(previous.get('stage'), 0)
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
            apt_steps = [('APT_UPDATE', 2, 'Обновление списка пакетов', apt.update),
                         ('APT_UPGRADE', 3, 'Обновление Ubuntu', apt.upgrade),
                         ('APT_INSTALL', 4, 'Установка зависимостей', lambda: apt.install(packages))]
            resume_index = next((i for i, item in enumerate(apt_steps)
                                 if item[0] == s.get('stage')), len(apt_steps))
            for name, number, label, action in apt_steps[resume_index:]:
                checkpoint(STATE, s, name)
                if name == 'APT_UPGRADE' and s['warp_mode'] == 'adopt':
                    # A global upgrade may replace and restart the operator's WARP package.
                    UI.stage(number, 13, 'Обновление Ubuntu пропущено для существующего WARP', 'skipped')
                else:
                    UI.stage(number, 13, label)
                    action()
                    UI.stage(number, 13, label, 'ok')
                checkpoint(STATE, s, name, completed=True)
            if Path('/var/run/reboot-required').exists():
                UI.note('Ubuntu сообщает о необходимости reboot; автоматическая перезагрузка отключена.')
            with tempfile.TemporaryDirectory(prefix='selfsni-') as wd:
                work = Path(wd)
                if resume_target not in ('WARP', 'CERTIFICATE'):
                    checkpoint(STATE, s, 'BBR'); UI.stage(5, 13, 'Настройка IPv6, TCP BBR и FQ')
                    s['ipv6'], ipv6_before = ipv6.configure(s['domain'])
                    try:
                        s['bbr'] = bbr.configure()
                    except Exception:
                        ipv6.rollback(ipv6_before)
                        raise
                    checkpoint(STATE, s, 'BBR', True)
                    UI.stage(5, 13, 'Настройка IPv6, TCP BBR и FQ', 'ok')
                    checkpoint(STATE, s, 'FIREWALL'); UI.stage(6, 13, 'Настройка сетевой защиты')
                    firewall(s); checkpoint(STATE, s, 'FIREWALL', True)
                    UI.stage(6, 13, 'Настройка сетевой защиты', 'ok')
                if resume_target != 'CERTIFICATE':
                    checkpoint(STATE, s, 'WARP')
                    if s['warp_mode'] == 'skip':
                        UI.stage(7, 13, 'WARP пропущен', 'skipped')
                    else:
                        UI.stage(7, 13, 'Проверка WARP' if s['warp_mode'] == 'adopt' else 'VPS-Server-Menu и WARP')
                        if s['warp_mode'] == 'adopt':
                            adopt_warp(s, lock, work)
                        else:
                            install_warp(s, lock, work)
                        UI.stage(7, 13, 'WARP', 'ok')
                    checkpoint(STATE, s, 'WARP', True)
                elif s['warp_mode'] == 'adopt':
                    adopt_warp(s, lock, work)
                elif s['warp_mode'] == 'install':
                    s['warp_health'] = menu_warp.health()
                checkpoint(STATE, s, 'CERTIFICATE'); UI.stage(8, 13, 'Получение SSL-сертификата')
                certificate(s,s['email']); checkpoint(STATE, s, 'CERTIFICATE', True)
                UI.stage(8, 13, 'Получение SSL-сертификата', 'ok')
                checkpoint(STATE, s, 'NGINX_PORTAL'); UI.stage(9, 13, 'Настройка nginx и страницы входа')
                decoy(s); checkpoint(STATE, s, 'NGINX_PORTAL', True)
                UI.stage(9, 13, 'Настройка nginx и страницы входа', 'ok')
                checkpoint(STATE, s, 'PANEL'); UI.stage(10, 13, 'Установка панели 3x-ui')
                panel(s,lock,work); checkpoint(STATE, s, 'PANEL', True)
                UI.stage(10, 13, 'Установка панели 3x-ui', 'ok')
                checkpoint(STATE, s, 'SECURITY'); UI.stage(11, 13, 'Защита и продление сертификата')
                security(s); renew_hook(s); firewall(s,public=True)
                checkpoint(STATE, s, 'SECURITY', True)
                UI.stage(11, 13, 'Защита и продление сертификата', 'ok')
                checkpoint(STATE, s, 'ACCEPTANCE'); UI.stage(12, 13, 'Проверка результата')
                from .acceptance import verify
                report = verify(s)
                save(STATE/'acceptance.json',report)
                require(all(x['passed'] for x in report['checks']),
                        'Acceptance failed; inspect /var/lib/selfsni/acceptance.json')
                checkpoint(STATE, s, 'ACCEPTANCE', True)
                UI.stage(12, 13, 'Проверка результата', 'ok')
                s['status'] = 'HEALTHY_LOCAL'; s['stage'] = 'HEALTHY_LOCAL'
                s['last_completed_stage'] = 'HEALTHY_LOCAL'
                s.pop('failure', None)
                s['versions'] = run('dpkg-query','-W',*version_packages(s['warp_mode']))
                save(STATE/'state.json',s)
                (STATE/'FAILED').unlink(missing_ok=True)
                UI.stage(13, 13, 'Готово', 'ok')
        configure_activity(None)
        UI.success(s)
    except (InstallError, OSError, ValueError, KeyError, KeyboardInterrupt) as e:
        configure_activity(None)
        message = 'Установка прервана пользователем (Ctrl+C)' if isinstance(e, KeyboardInterrupt) else redact(str(e))
        stage('FAILED: ' + message)
        if owns_state:
            current = locals().get('s', state)
            if current:
                fail(STATE, current, message)
            write(STATE/'FAILED', message+'\n')
        if UI:
            UI.failure(message, DIAGNOSTIC_LOG if owns_state else 'не создан (preflight)')
        raise SystemExit(130 if isinstance(e, KeyboardInterrupt) else 1)

if __name__ == '__main__':
    main()
