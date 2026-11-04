import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from .api import PANEL_ACCESS, Panel, request, public_subscription_url
from . import bbr, ipv6, swap
from .clients import ensure_admin, verify_admin_runtime, verify_admin_subscriptions
from . import cli_install
from .core import (ROOT, CommandError, InstallError, configure_diagnostics, redact,
                   register_secrets, require, run, write, save, token, free_port,
                   fqdn, download, verify_bundle, wait_for, digest,
                   configure_activity)
from .certbot_errors import acme_step, classify_renewal, deploy_hook_result, explain as explain_certbot
from .apt import AptRunner, background_package_processes
from .menu_upstream import OFFICIAL_REPOSITORY, check_official_menu, install_menu
from . import menu_warp
from .install_state import (LATE_RESUME_STAGES, RESUMABLE_FAILURE_STAGES, SECURITY_STEPS, STAGES,
                            InstallerLock, checkpoint, fail, initialize, load,
                            upgrade_legacy_apt_failure, validate_resume, warp_mode)
from .ui import InstallerUI, interactive_wizard
from .geodata import GEODATA_TIMEZONE, verify_server_geodata
from .portal import generate
from .tls_renewal import (LEGACY_HOOK_SCRIPT, RELOAD_STAMP, RENEW_HOOK, RENEW_HOOK_SCRIPT,
                          RETRY_DROPIN, retry_dropin)
from .routing import deeplink, inbound, server_template
from .xray_pin import install_pinned_xray

STATE = Path('/var/lib/selfsni')
DIAGNOSTIC_LOG = Path('/var/log/bob-auto-xray/install.log')
INSTALL_LOCK = Path('/run/lock/bob-auto-xray-install.lock')
LOG_ENABLED = False
UI = None
# Paths read by the late-resume preflight; tests point this at a disposable tree.
SYSTEM_ROOT = Path('/')
ACME_WEBROOT = '/var/lib/selfsni-acme'
CERTBOT_LOG = Path('/var/log/letsencrypt/letsencrypt.log')
# Certbot reads defaults, hooks included, from these files.
CERTBOT_CLI_INI = ('/etc/letsencrypt/cli.ini', '/root/.config/letsencrypt/cli.ini')
ACME_DIRECTORIES = (('staging', 'https://acme-staging-v02.api.letsencrypt.org/directory'),
                    ('production', 'https://acme-v02.api.letsencrypt.org/directory'))
# Bounded retry of the renewal dry-run on transient network/ACME 5xx failures only.
RENEWAL_ATTEMPTS = 4
RENEWAL_BUDGET = 1200          # seconds for all attempts and pauses together
RENEWAL_ATTEMPT_TIMEOUT = 600  # one certbot run
RENEWAL_MIN_ATTEMPT = 120      # never start an attempt that cannot finish in time
RENEWAL_BACKOFF = 30           # 30, 60, 120 s (+0..50% jitter)


def host(path):
    return SYSTEM_ROOT / str(path).lstrip('/')

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
    # 22.04 is not supported: its certbot 1.21 lacks --run-deploy-hooks.
    require(values.get('ID') == 'ubuntu' and values.get('VERSION_ID') in ('24.04', '26.04'),
            'Supported OS: Ubuntu 24.04, 26.04 LTS')
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
    '/usr/local/lib/bob-auto-xray', RETRY_DROPIN,
]

DOCTOR_LIB = cli_install.LIBRARY
DOCTOR_COMMAND = cli_install.COMMAND
DOCTOR_PYTHON = cli_install.PYTHON


DOCTOR_NOTES = {
    'library-occupied': 'Каталог Doctor занят; Doctor не установлен, чужие файлы не изменены.',
    'command-name-taken': 'Имя bob занято другой программой; модуль Doctor сохранён без глобальной команды.',
    'command-failed': 'Модуль Doctor сохранён, но команду bob создать не удалось.',
    'failed': 'Bob Doctor не сохранён; запустите его из архива релиза: install.sh doctor.',
}


def install_doctor_cli():
    """Persist the packaged diagnostic CLI; a failure never fails the VPN installation.

    The library appears atomically (staged copy, then rename) and the command
    is published with link(), which never replaces an existing name.
    """
    if DOCTOR_LIB.exists() or DOCTOR_LIB.is_symlink():
        return {'status': 'library-occupied', 'command': None}
    staging = None
    try:
        staging = Path(tempfile.mkdtemp(prefix='.' + DOCTOR_LIB.name + '.', dir=DOCTOR_LIB.parent))
        # Package, VERSION and a manifest that later proves ownership to cli-upgrade.
        cli_install.stage_library(staging, ROOT / 'src/selfsni', (ROOT / 'VERSION').read_text().strip())
        os.rename(staging, DOCTOR_LIB)
        staging = None
    except Exception:
        return {'status': 'failed', 'command': None}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    if shutil.which('bob') is not None or DOCTOR_COMMAND.exists() or DOCTOR_COMMAND.is_symlink():
        return {'status': 'command-name-taken', 'command': None}
    script = cli_install.wrapper(DOCTOR_LIB, DOCTOR_PYTHON)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.' + DOCTOR_COMMAND.name + '.', dir=DOCTOR_COMMAND.parent)
        os.close(fd)
        write(temporary, script, 0o755)
        os.link(temporary, DOCTOR_COMMAND)
    except FileExistsError:
        return {'status': 'command-name-taken', 'command': None}
    except Exception:
        return {'status': 'command-failed', 'command': None}
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return {'status': 'installed', 'command': str(DOCTOR_COMMAND)}


NGINX_ROOT = Path('/etc/nginx')
PROC_ROOT = Path('/proc')
NGINX_BINARY = '/usr/sbin/nginx'
NGINX_CGROUP = '/system.slice/nginx.service'
# APT_INSTALL installs nginx with its default site; certificate() swaps it for
# Bob's HTTP-01 site. Nothing else may exist before NGINX_PORTAL.
NGINX_RESUME_SITES = {'APT_INSTALL': ('default',), 'WARP': ('default',),
                      'CERTIFICATE': ('default', 'selfsni-http')}


def check_resume_nginx_config(stage, root=None):
    root = Path(root or NGINX_ROOT)
    enabled, conf_d = root / 'sites-enabled', root / 'conf.d'
    for entry in (enabled.iterdir() if enabled.is_dir() else ()):
        target = root / 'sites-available' / entry.name
        require(entry.name in NGINX_RESUME_SITES[stage] and entry.is_symlink() and
                target.is_file() and not target.is_symlink() and
                os.path.realpath(entry) == os.path.realpath(target),
                'Чужая nginx-конфигурация обнаружена во время resume')
    require(not (conf_d.is_dir() and any(conf_d.iterdir())),
            'Чужая nginx-конфигурация обнаружена во время resume')


def owned_nginx_http(resume_state, listeners, command=run, proc_root=None, nginx_root=None):
    """True only when every TCP/80 listener is the packaged nginx.service of this run.

    A process name or binary path alone is not ownership: a container or a
    hand-started nginx has both. The unit's MainPID must listen and every
    listening PID must live in the nginx.service cgroup.
    """
    lines = [line for line in listeners.splitlines()
             if int(line.split()[3].rsplit(':', 1)[1]) == 80]
    if not (lines and resume_state and resume_state.get('stage') in NGINX_RESUME_SITES and
            '/etc/nginx' in resume_state.get('absent_paths', ()) and
            Path(nginx_root or NGINX_ROOT).is_dir()):
        return False
    pids = set()
    for line in lines:
        users = re.findall(r'\("([^"]*)",pid=(\d+),fd=\d+\)', line)
        if line.split()[3] not in ('0.0.0.0:80', '[::]:80') or not users or \
                any(name != 'nginx' for name, _ in users):
            return False
        pids.update(pid for _, pid in users)
    main_pid = command('systemctl', 'show', 'nginx.service', '--property=MainPID', '--value').strip()
    if main_pid not in pids:
        return False
    proc_root = Path(proc_root or PROC_ROOT)
    for pid in pids:
        try:
            cgroups = [line.split(':', 2) for line in
                       (proc_root / pid / 'cgroup').read_text().splitlines()]
            executable = os.readlink(proc_root / pid / 'exe')
        except OSError:
            return False
        # cgroup v2 unified line or the v1 name=systemd hierarchy.
        units = [row[2] for row in cgroups if len(row) == 3 and row[1] in ('', 'name=systemd')]
        if executable != NGINX_BINARY or not units or any(unit != NGINX_CGROUP for unit in units):
            return False
    return True


def check_resume_letsencrypt(state):
    """/etc/letsencrypt was absent before this installation: allow only Bob's lineage.

    A failure after `certbot certonly` succeeded (certbot.timer, a timeout after
    saving, Ctrl+C) leaves exactly live/README, live/<FQDN>, archive/<FQDN> and
    renewal/<FQDN>.conf; certificate() then keeps that certificate.
    """
    le, domain = host('/etc/letsencrypt'), state['domain']
    allowed = {'live': {'README', domain}, 'archive': {domain}, 'renewal': {domain + '.conf'}}
    present = {name: {x.name for x in (le / name).glob('*')} for name in allowed} if le.exists() else {}
    require(all(names <= allowed[name] for name, names in present.items()),
            'Чужие Let’s Encrypt certificates обнаружены во время resume')
    lineage = [bool(present.get(name, set()) & {domain, domain + '.conf'}) for name in allowed]
    if any(lineage):
        require(state.get('stage') == 'CERTIFICATE' and all(lineage),
                'Неполный сертификат Let’s Encrypt обнаружен во время resume')
        check_renewal_config(domain)


def _carried(state, key, valid):
    value = state.get(key)
    try:
        ok = isinstance(value, dict) and bool(valid(value))
    except (OSError, TypeError, KeyError, ValueError):
        ok = False
    require(ok, f'Результат этапа ({key}) отсутствует или не совпадает с системой; resume запрещён')
    return value


def carried_results(state, ipv6_report):
    """Validated results of completed stages that the resumed run will not repeat.

    Live checks (bbr_preflight, ipv6_preflight, WARP health) are always
    recomputed; only durable stage outputs are carried, after re-reading the
    managed files they describe.
    """
    stage = state.get('stage')
    require(stage in RESUMABLE_FAILURE_STAGES and
            state.get('last_completed_stage') in STAGES[STAGES.index(stage) - 1:STAGES.index(stage) + 1],
            f'Этап {stage} не согласован с last_completed_stage; resume запрещён')
    if stage not in ('WARP', 'CERTIFICATE'):
        return {}
    results = dict(
        ipv6=_carried(state, 'ipv6', lambda r:
            r['persistentConfig'] == str(ipv6.CONFIG) and ipv6.CONFIG.read_text() == ipv6.CONTENT and
            r['networkDropin'] == ipv6_report.get('networkDropin') and
            Path(r['networkDropin']).name == ipv6.NETWORK_DROPIN and
            Path(r['networkDropin']).read_text() == ipv6.NETWORK_CONTENT and
            isinstance(r['interfaces'], list) and all(isinstance(x, str) for x in r['interfaces'])),
        bbr=_carried(state, 'bbr', lambda r:
            r['runtime'] == bbr.DESIRED and r['persistentConfig'] == str(bbr.SYSCTL_CONFIG) and
            bbr.SYSCTL_CONFIG.read_text() == ''.join(f'{k} = {v}\n' for k, v in bbr.DESIRED.items())),
        swap=_carried(state, 'swap', lambda r:
            (r['status'] == 'created' and r['path'] == str(swap.FILE) and r['bytes'] == swap.SIZE) or
            (r['status'] == 'existing' and r['path'] == str(swap.FILE)) or
            (r['status'] == 'skipped' and isinstance(r['reason'], str))))
    swap.verify(results['swap'])
    # install re-checks health only; adopt re-resolves the menu and overwrites it.
    if stage == 'CERTIFICATE' and warp_mode(state) != 'skip':
        results['menu_revision'] = _carried(state, 'menu_revision', lambda r:
            r['origin'] == OFFICIAL_REPOSITORY and r['branch'] == 'main' and
            re.fullmatch(r'[0-9a-f]{40}', r['commit']) is not None and isinstance(r['resolved_at'], str))
    return results


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
                    check_resume_nginx_config(resume_state['stage'])
                if p == '/etc/letsencrypt':
                    check_resume_letsencrypt(resume_state)
                continue
        # Existing WARP files belong to the operator in adopt/skip mode.
        if (p == '/root/VPS-Server-Menu' and args.warp_mode != 'skip') or (
                args.warp_mode in ('adopt', 'skip') and
                (p.endswith('/cloudflare-client.list') or p == '/root/VPS-Server-Menu')):
            continue
        require(not (Path(p).exists() or Path(p).is_symlink()),
                'Existing installation/configuration: ' + p)
    warp_present = shutil.which('warp-cli') is not None if args.warp_mode != 'skip' else False
    warp_resume = bool(args.warp_mode == 'install' and resume_state and resume_state.get('stage') in ('WARP', 'CERTIFICATE'))
    require(not warp_present or args.warp_mode in ('adopt', 'skip') or warp_resume,
            'Existing WARP: needs explicit --adopt-warp')
    require(args.warp_mode != 'adopt' or warp_present, '--adopt-warp requires an existing installation')
    # On a fresh VPS cloud-init may still be upgrading openssh-server: sshd -T then exits 255.
    require('cloud-init' not in background_package_processes(PROC_ROOT),
            'cloud-init ещё настраивает систему; дождитесь `cloud-init status --wait` '
            'и запустите установку снова')
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
    nginx_http = owned_nginx_http(resume_state, listeners, run)
    if nginx_http:
        allowed.add(80)
    for line in listeners.splitlines():
        port = int(line.split()[3].rsplit(':', 1)[1])
        require(port in allowed, f'Foreign TCP listener on port {port}')
    for port in [80,443,62789,11111]:
        if nginx_http and port == 80:
            continue
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('0.0.0.0', port))
            except OSError as e:
                raise InstallError(f'Required port {port} is occupied') from e
    if shutil.which('ufw'):
        resume_ports = {80, 443} if resume_state and resume_state.get('stage') in ('WARP', 'CERTIFICATE') else set()
        check_ufw_rules(ssh_ports | resume_ports)
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
        require(1024 <= panel_port <= 65535 and 1024 <= sub_port <= 65535 and panel_port != sub_port,
                'Сохранённые порты некорректны или совпадают')
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
                panel_access=PANEL_ACCESS, subscription_access=PANEL_ACCESS,
                warp_mode=args.warp_mode, adopt_warp=args.warp_mode == 'adopt',
                bbr_preflight=bbr_report, ipv6_preflight=ipv6_report, **credentials,
                status='INSTALLING')
    if resume_state:
        preserved = ('panel_path', 'sub_path', 'username', 'password', 'panel_port', 'sub_port',
                     'installation_id', 'schema', 'absent_paths', 'resume_count', 'stage',
                     'last_completed_stage', 'legacy_resume')
        fresh.update({key: resume_state[key] for key in preserved if key in resume_state})
        fresh.update(carried_results(resume_state, ipv6_report))
    return fresh

def check_ufw_rules(allowed, root=Path('/etc/ufw')):
    for name in ['user.rules', 'user6.rules']:
        for line in (Path(root) / name).read_text().splitlines():
            if line.startswith('-A ufw-user-input '):
                match = re.fullmatch(r'-A ufw-user-input -p tcp --dport (\d+) -j ACCEPT', line)
                require(match is not None and int(match.group(1)) in allowed,
                        'Existing non-SSH UFW rules')


def late_resume_preflight(args, state):
    """SECURITY/ACCEPTANCE resume: prove that finished stages still match the system.

    The saved state is reused as is: nothing is regenerated, reinstalled or
    reissued. Every artifact the remaining steps rely on is re-read and
    compared with what the saved state would render; any divergence refuses
    the resume. Full acceptance runs afterwards.
    """
    require(os.geteuid() == 0, 'Run as root on the test/clean VPS')
    os_info()
    require(host('/run/systemd/system').exists(), 'systemd is required')
    s = dict(state)
    domain = s['domain']

    def same(path, content, mode=None):
        target = host(path)
        try:
            ok = (not target.is_symlink() and target.is_file() and target.read_text() == content and
                  (mode is None or target.stat().st_mode & 0o777 == mode))
        except OSError:
            ok = False
        require(ok, f'{path} не совпадает с сохранённым состоянием; resume запрещён')

    require(host(STATE / 'panel-ready').is_file(), 'Маркер готовности панели отсутствует; resume запрещён')
    check_renewal_config(domain)
    fullchain = f'/etc/letsencrypt/live/{domain}/fullchain.pem'
    try:
        run('openssl', 'x509', '-checkend', '0', '-noout', '-in', host(fullchain))
        hostname = run('openssl', 'x509', '-checkhost', domain, '-noout', '-in', host(fullchain))
    except CommandError as error:
        raise InstallError('Сертификат Let’s Encrypt отсутствует или истёк; resume запрещён') from error
    require('does match certificate' in hostname and 'does NOT match' not in hostname,
            'Сертификат Let’s Encrypt не соответствует домену; resume запрещён')
    same('/etc/nginx/sites-available/selfsni-http', http_site(domain))
    enabled = host('/etc/nginx/sites-enabled/selfsni-http')
    require(enabled.is_symlink() and os.readlink(enabled) == '/etc/nginx/sites-available/selfsni-http',
            'HTTP-01 сайт nginx отключён или изменён; resume запрещён')
    for path, content in nginx_files(s).items():
        same(path, content)
    same('/etc/systemd/system/x-ui.service.d/20-selfsni.conf', xui_unit_override(s['warp_mode']), 0o644)
    require(digest(host(s['xray_bin'])) == s['xray_sha256'], 'Бинарный файл Xray изменён; resume запрещён')
    # An unfinished step may have written its file; a different file is not Bob's.
    # v0.4.2 wrote the legacy hook before its renewal dry run.
    hook = host(RENEW_HOOK)
    done = 'renewal' in s['security_steps']
    legacy = hook.is_file() and not hook.is_symlink() and hook.read_text() == LEGACY_HOOK_SCRIPT
    if (hook.exists() or hook.is_symlink() or done) and not legacy:
        same(RENEW_HOOK, RENEW_HOOK_SCRIPT, 0o700 if done else None)
    for path, content in ((RETRY_DROPIN, retry_dropin(domain)), (SSH_JAIL, ssh_jail(s))):
        if host(path).exists() or host(path).is_symlink():
            same(path, content)
    if done and not legacy:
        same(RETRY_DROPIN, retry_dropin(domain), 0o644)
    if shutil.which('ufw'):
        check_ufw_rules(set(s['ssh_ports']) | {80, 443}, host('/etc/ufw'))
    # BBR already proved the IPv4-only policy persistent across reboot; a late
    # resume only reads it (sysctl, networkd drop-in, runtime) and never rewrites it.
    try:
        report = ipv6.verify()
    except InstallError as error:
        raise InstallError(f'IPv6-политика не соответствует установке ({error}); resume запрещён') from error
    require(isinstance(s['ipv6'], dict) and
            all(report[key] == s['ipv6'].get(key) for key in ('persistentConfig', 'networkDropin')),
            'Файлы IPv6-политики отличаются от сохранённых; resume запрещён')
    run('nginx', '-t')
    verify_panel_bootstrap(s)
    s['status'] = 'INSTALLING'
    return s


def check_renewal_config(domain):
    """Bob's lineage renews over HTTP-01 webroot from production with no hooks of its own.

    The dry run disables directory hooks and names Bob's deploy hook, but
    certbot would still run pre/post hooks of the lineage or of cli.ini, even
    in a dry run, and a lineage renew_hook would run on a real renewal.
    """
    try:
        text = host(f'/etc/letsencrypt/renewal/{domain}.conf').read_text()
    except OSError:
        text = ''
    require(re.search(r'^authenticator = webroot$', text, re.M) and
            re.search(r'^webroot_path = ' + re.escape(ACME_WEBROOT) + r',?$', text, re.M) and
            re.search(r'^server = https://acme-v02\.api\.letsencrypt\.org/directory$', text, re.M),
            'Renewal-конфигурация сертификата не соответствует HTTP-01 webroot Bob')
    require(not re.search(r'^\s*(?:pre|post|renew)_hook\s*=', text, re.M),
            'В renewal-конфигурации сертификата Bob заданы посторонние hooks')
    for path in CERTBOT_CLI_INI:
        try:
            ini = host(path).read_text()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise InstallError(f'{path} недоступен для проверки hooks Certbot') from error
        require(not re.search(r'^\s*(?:pre|post|deploy|renew)[-_]hook\s*=', ini, re.M | re.I),
                f'{path} задаёт hooks Certbot; изолированная проверка продления невозможна')


def saved_secrets(s):
    admin = s.get('admin_client') or {}
    return (s.get('api_token'), s.get('reality_private'), s.get('reality_public'),
            s.get('short_id'), admin.get('id'), admin.get('subId'))


def firewall(s, public=False):
    for port in s['ssh_ports']:
        run('ufw', 'allow', str(port) + '/tcp', 'comment', 'Self-SNI SSH')
    run('ufw', 'allow', '80/tcp')
    run('ufw', 'allow', '443/tcp')
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
    s['menu_revision'] = install_menu(lock, work)
    s['warp_health'] = menu_warp.install()


def adopt_warp(s, lock, work):
    """Install/check the menu, then use its read-only health command."""
    s['menu_revision'] = install_menu(lock, work)
    s['warp_health'] = menu_warp.health()

def http_site(domain):
    # Keep private installer state 0700; use the public webroot in rendered config.
    return render('nginx-http.conf', {'DOMAIN': domain}).replace('/var/lib/selfsni/acme', ACME_WEBROOT)


def certificate(s, email):
    stage('nginx + Certbot HTTP-01 webroot')
    # STATE contains no traversable secrets; give nginx access only via a dedicated webroot.
    run('install', '-d', '-m', '755', ACME_WEBROOT)
    write('/etc/nginx/sites-available/selfsni-http', http_site(s['domain']), 0o644)
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
    # --keep-until-expiring: a resumed run never reissues a valid certificate
    # (Let's Encrypt allows 5 identical certificates per week).
    cmd = ['certbot','certonly','--non-interactive','--agree-tos','--keep-until-expiring','--webroot','-w',
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

def nginx_files(s):
    """Every nginx/systemd file NGINX_PORTAL renders, as a function of the saved state."""
    panel_values = dict(SUB_PATH=s['sub_path'], SUB_PORT=s['sub_port'], DOMAIN=s['domain'], PANEL_PORT=s['panel_port'],
                        PANEL_PATH=s['panel_path'], PANEL_NO_SLASH=s['panel_path'][:-1])
    return {
        '/etc/nginx/snippets/selfsni-fastcgi.conf':
            f'include fastcgi_params;\nfastcgi_param SCRIPT_FILENAME /var/www/selfsni/index.php;\n'
            f'fastcgi_param HTTPS on;\nfastcgi_pass unix:/run/php/{s["php_service"]}.sock;\n',
        '/etc/nginx/snippets/selfsni-subscription.conf': render('nginx-subscription.conf', panel_values),
        '/etc/nginx/snippets/selfsni-panel.conf': render('nginx-panel.conf', panel_values),
        '/etc/nginx/conf.d/selfsni-tls.conf': render('nginx-tls.conf', dict(s['portal'], **panel_values)),
        '/etc/systemd/system/nginx.service.d/selfsni.conf':
            '[Service]\nUMask=0007\nExecStartPre=/usr/bin/install -d -m 0750 -o root -g root /run/selfsni\n',
    }


def decoy(s):
    stage('Unique cloud portal + PHP-FPM + REALITY TLS socket')
    values = generate('/var/www/selfsni')
    sockets = list(Path('/run/php').glob('php[0-9]*-fpm.sock'))
    require(len(sockets) == 1, 'Expected one PHP-FPM version')
    s['php_service'] = sockets[0].name.replace('.sock', '')
    s['portal'] = values
    files = nginx_files(s)
    write('/etc/nginx/snippets/selfsni-fastcgi.conf', files['/etc/nginx/snippets/selfsni-fastcgi.conf'], 0o644)
    write('/etc/tmpfiles.d/selfsni.conf', 'd /run/selfsni 0750 root root -\n', 0o644)
    run('systemd-tmpfiles', '--create', '/etc/tmpfiles.d/selfsni.conf')
    require(re.fullmatch(r'/[0-9a-f]{48}/', s['panel_path']) is not None,
            'Panel path must contain exactly 48 lowercase hex characters')
    require(isinstance(s['panel_port'], int) and 1024 <= s['panel_port'] <= 65535,
            'Invalid internal panel port')
    from .subscriptions import validate_paths
    validate_paths(s)
    for path in ('/etc/nginx/snippets/selfsni-subscription.conf', '/etc/nginx/snippets/selfsni-panel.conf',
                 '/etc/nginx/conf.d/selfsni-tls.conf'):
        write(path, files[path])
    write('/etc/systemd/system/nginx.service.d/selfsni.conf',
          files['/etc/systemd/system/nginx.service.d/selfsni.conf'], 0o644)
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
    # The stock installer starts x-ui before Bob can bind it or enable TLS.
    # An absent root-only marker blocks EVERY service start throughout bootstrap.
    require(not (STATE/'panel-ready').exists(), 'Unexpected panel bootstrap marker')
    write('/etc/systemd/system/x-ui.service.d/20-selfsni.conf',
          xui_unit_override(s.get('warp_mode', 'install')), 0o644)
    run('systemctl', 'daemon-reload')
    run('bash', ROOT / 'vendor/3x-ui/install.sh', lock['xui_version'], env=env, timeout=900)
    s['xray_sha256'] = install_pinned_xray(lock, s['arch'], work)
    cert = f'/etc/letsencrypt/live/{s["domain"]}/'
    run('/usr/local/x-ui/x-ui','setting','-webCert',cert+'fullchain.pem','-webCertKey',cert+'privkey.pem',
        '-listenIP','127.0.0.1')
    verify_panel_bootstrap(s)
    write(STATE/'panel-ready', 'loopback TLS configured\n')
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


def check_panel_settings(output, cert, key, port, path):
    """`x-ui setting -getListen -getCert -show`; tools/test_client_api.py checks the
    contract with the pinned binary after a full setting/update, as on a late resume."""
    for name, value in (('listenIP', '127.0.0.1'), ('cert', cert), ('key', key), ('port', str(port)),
                        ('webBasePath', path), ('hasDefaultCredential', 'false')):
        require(re.findall(r'^' + name + r':[ \t]*([^\r\n]*)$', output, re.M) == [value],
                'Panel bootstrap settings verification failed')


def verify_panel_bootstrap(s):
    # The pinned CLI can exit 0 after failed settings writes. Read them back
    # while the service is still gated, and validate the actual certificate/key.
    cert = f'/etc/letsencrypt/live/{s["domain"]}/'
    check_panel_settings(run('/usr/local/x-ui/x-ui', 'setting', '-getListen', '-getCert', '-show'),
                         cert + 'fullchain.pem', cert + 'privkey.pem', s['panel_port'], s['panel_path'])
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert+'fullchain.pem', cert+'privkey.pem')
    except (OSError, ssl.SSLError) as error:
        raise InstallError('Panel bootstrap TLS certificate/key validation failed') from error


def xui_unit_override(mode):
    require(mode in ('install', 'adopt', 'skip'), 'Unknown WARP mode')
    warp_unit = ' warp-svc.service' if mode != 'skip' else ''
    return (f'[Unit]\nAfter=nginx.service{warp_unit} network-online.target\n'
            f'Wants=nginx.service{warp_unit} network-online.target\n'
            'ConditionPathExists=/var/lib/selfsni/panel-ready\n'
            '[Service]\nEnvironment=TZ=' + GEODATA_TIMEZONE + '\n'
            'ExecStartPre=/usr/bin/test -S /run/selfsni/nginx.sock\n'
            'ExecStartPre=/usr/local/x-ui/x-ui setting -listenIP 127.0.0.1\n'
            'ExecStartPre=/bin/sh -ec \'/usr/local/x-ui/x-ui setting -getListen | '
            '/usr/bin/grep -Fx "listenIP: 127.0.0.1"\'\n')


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
    settings.update(subEnable=True, subListen='127.0.0.1', subPort=s['sub_port'],
        subDomain=s['domain'], subPath=s['sub_path'], subURI=public_subscription_url(s),
        subCertFile=cert+'fullchain.pem', subKeyFile=cert+'privkey.pem',
        subEncrypt=True, subEnableRouting=True, subRoutingRules=deeplink('happ'),
        subIncyEnableRouting=True, subIncyRoutingRules=deeplink('incy'),
        subHappAutoDetect=True, subHappTunType='xray', subHappLocalProxyAuth='auto',
        subIncyAppAutoDetect=True,
        subJsonEnable=False, subJsonAutoDetect=False, subClashEnable=False,
        webListen='127.0.0.1', trustedProxyCIDRs='127.0.0.1/32')
    api.call('setting/update', settings)
    from .subscriptions import verify_settings
    verify_settings(s, api.call('setting/all', {}, method='POST'))
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
    ensure_admin(s, api, lambda state: save(STATE/'state.json', state))
    run('systemctl','restart','x-ui')
    wait_for(lambda: request(s['domain'],s['sub_port'],s['sub_path']+'missing')[0] in (400,404), 'TLS subscriptions')
    verify_admin_runtime(s, json.loads(Path('/usr/local/x-ui/bin/config.json').read_text()))
    verify_admin_subscriptions(s)
    s['xray_version'] = run(s['xray_bin'], 'version').splitlines()[0]
    s['xui_version'] = lock['xui_version']

SSH_JAIL = '/etc/fail2ban/jail.d/selfsni-ssh.local'


def ssh_jail(s):
    return '[sshd]\nenabled = true\nbackend = systemd\nport = ' + ','.join(map(str, s['ssh_ports'])) + '\n'


def security(s):
    stage('Official 3x-ui Fail2ban integration + SSH jail')
    run('/usr/bin/x-ui','setup-fail2ban',timeout=300)
    # The official IP-limit jail is retained; this is an additional sshd override.
    write(SSH_JAIL, ssh_jail(s), 0o644)
    run('systemctl','enable','--now','fail2ban')
    run('systemctl','restart','fail2ban')
    wait_for(lambda: 'sshd' in run('fail2ban-client','status'), 'Fail2ban sshd jail')

def acme_reachability(url):
    """Diagnostic only: an answer from the ACME directory, with normal TLS verification."""
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return f'HTTP {response.status}'
    except urllib.error.HTTPError as error:
        return f'HTTP {error.code}'
    except (OSError, ValueError) as error:
        reason = getattr(error, 'reason', error)
        return 'нет ответа (' + type(reason).__name__ + ')'


def renewal_failure(kind, output, attempts, step):
    where = f' Последний запрос ACME: {step}.' if step else ''
    if kind == 'transient':
        reach = ', '.join(f'{name} — {acme_reachability(url)}' for name, url in ACME_DIRECTORIES)
        text = ('Проверка автоматического продления сертификата НЕ выполнена: сетевая ошибка при '
                f'обращении к тестовому серверу Let’s Encrypt (staging), попыток: {attempts}.{where} '
                f'Доступность каталогов ACME сейчас: {reach}. Боевой сертификат выпущен и не изменён, '
                'но продление не подтверждено. Запустите установщик повторно позже: он продолжит этап '
                'защиты без перевыпуска сертификата и без переустановки панели.')
    elif kind == 'hook':
        text = ('Тестовое продление получено, но deploy hook Bob (nginx -t, reload nginx, restart x-ui) '
                'завершился ошибкой. Это не сбой Let’s Encrypt: проверьте `nginx -t` и '
                '`systemctl status x-ui`, затем запустите установщик повторно.')
    elif kind == 'validation':
        text = ('Тестовое продление отклонено: Let’s Encrypt не подтвердил домен через HTTP-01 '
                f'({ACME_WEBROOT}). {explain_certbot(output)}{where}')
    elif kind == 'tls':
        text = ('TLS-сертификат сервера Let’s Encrypt не прошёл проверку (возможен перехват трафика). '
                'Повтор не выполнялся.' + where)
    else:
        text = ('Проверка продления сертификата (certbot renew --dry-run) завершилась ошибкой. '
                + explain_certbot(output) + where)
    return InstallError(text + ' Подробности: /var/log/letsencrypt/letsencrypt.log и ' + str(DIAGNOSTIC_LOG))


def pause(seconds):
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(1, left))
        if UI:
            UI.pulse()


def certbot_log_mark():
    try:
        info = CERTBOT_LOG.stat()
    except OSError:
        return None
    return info.st_ino, info.st_size


def certbot_log_since(mark):
    """What one certbot run wrote, whether or not certbot rotated the log."""
    try:
        with CERTBOT_LOG.open('rb') as log:
            info = os.fstat(log.fileno())
            # A new inode (rotated) or a shorter file (truncated in place) is read whole.
            if mark and info.st_ino == mark[0] and info.st_size >= mark[1]:
                log.seek(mark[1])
            return log.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def renewal_command(domain):
    # tools/check_certbot_cli.py runs this exact line against packaged certbot in CI.
    return ('certbot', 'renew', '--dry-run', '--cert-name', domain, '--no-directory-hooks',
            '--deploy-hook', RENEW_HOOK, '--run-deploy-hooks', '--no-random-sleep-on-renew')


def renew_hook(s, clock=time.monotonic, sleep=pause, jitter=random.uniform):
    write(RENEW_HOOK, RENEW_HOOK_SCRIPT, 0o700)
    check_renewal_config(s['domain'])
    # All services use the live certificate path directly; no stale copies.
    # Only Bob's lineage (--cert-name) and only Bob's hook run: directory hooks
    # are disabled and the same file is named explicitly. Real renewals by
    # certbot.timer run it from renewal-hooks/deploy. A dry run uses ACME staging
    # and writes neither live/ nor renewal/ (no --deploy-hook is saved), so a
    # repeat is safe; only failures classified as transient are repeated.
    command = renewal_command(s['domain'])
    deadline = clock() + RENEWAL_BUDGET
    for attempt in range(1, RENEWAL_ATTEMPTS + 1):
        mark = certbot_log_mark()
        try:
            output = run(*command, timeout=min(RENEWAL_ATTEMPT_TIMEOUT, deadline - clock()))
        except CommandError as error:
            kind = classify_renewal(error.output, error.timed_out)
            step = acme_step(certbot_log_since(mark))
            delay = RENEWAL_BACKOFF * 2 ** (attempt - 1)
            delay += jitter(0, delay / 2)
            if kind != 'transient' or attempt == RENEWAL_ATTEMPTS or \
                    clock() + delay + RENEWAL_MIN_ATTEMPT > deadline:
                raise renewal_failure(kind, error.output, attempt, step) from error
            message = (f'Проверка продления: временная сетевая ошибка ACME staging'
                       f'{" (" + step + ")" if step else ""}; попытка {attempt + 1}/{RENEWAL_ATTEMPTS} '
                       f'через {round(delay)} с')
            stage(message)
            if UI:
                UI.note(message)
            sleep(delay)
            continue
        hook = deploy_hook_result(output, certbot_log_since(mark), RENEW_HOOK)
        if hook == 'failed':
            raise renewal_failure('hook', output, attempt, None)
        require(hook == 'ran', 'Запуск deploy hook Bob при тестовом продлении не подтверждён '
                               f'({hook}); продление не подтверждено')
        # The proven hook has just written RELOAD_STAMP; from now on certbot.service
        # repeats it after a renewal until it succeeds.
        require(Path(RELOAD_STAMP).is_file(), 'Deploy hook Bob не записал отметку перезагрузки TLS')
        write(RETRY_DROPIN, retry_dropin(s['domain']), 0o644)
        run('systemctl', 'daemon-reload')
        require(RETRY_DROPIN in run('systemctl', 'show', 'certbot.service', '--property=DropInPaths',
                                    '--value'), 'systemd не применил повтор deploy hook для certbot.service')
        s['renewal_check'] = {'status': 'PASS', 'attempts': attempt,
                              'checked_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        return


def install_doctor(s):
    s['doctor_cli'] = install_doctor_cli()
    if UI and s['doctor_cli']['status'] in DOCTOR_NOTES:
        UI.note(DOCTOR_NOTES[s['doctor_cli']['status']])


def secure(s):
    """SECURITY with a checkpoint after every step; a resume repeats only unfinished steps."""
    actions = {'fail2ban': security, 'renewal': renew_hook,
               'firewall': lambda state: firewall(state, public=True), 'doctor': install_doctor}
    done = s.setdefault('security_steps', [])
    for name in SECURITY_STEPS:
        if name not in done:
            actions[name](s)
            done.append(name)
            save(STATE/'state.json', s)

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
        late_preview = bool(resume_preview) and resume_preview.get('stage') in LATE_RESUME_STAGES
        # A late resume never touches the menu; WARP is only health-checked.
        if not args.preflight_only and args.warp_mode in ('install', 'adopt') and not late_preview:
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
                                'WARP': 6, 'CERTIFICATE': 7, 'SECURITY': 10,
                                'ACCEPTANCE': 11}.get(previous.get('stage'), 0)
            late = bool(previous) and previous.get('stage') in LATE_RESUME_STAGES
            UI.stage(1, 13, 'Проверка системы')
            if late:
                stage('Preflight: saved state against installed certificate, nginx, panel, Xray')
                s = late_resume_preflight(args, previous)
            else:
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
            if late:
                register_secrets(*saved_secrets(s))
            save(STATE/'state.json',s)
            packages = ['nginx','certbot','php-fpm','php-cli','curl','openssl','ufw',
                        'fail2ban','nftables','python3','python3-systemd','gnupg',
                        'ca-certificates','dnsutils','bc','jq','qrencode','git','iproute2','kmod']
            apt = AptRunner(run, notify=UI.note)
            resume_target = s.get('stage')

            def pending(name):
                # Stages before the resume target are complete and are never repeated.
                return STAGES.index(name) >= STAGES.index(resume_target)
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
                if pending('BBR'):
                    checkpoint(STATE, s, 'BBR'); UI.stage(5, 13, 'Настройка IPv6, TCP BBR, FQ и swap')
                    swap.preflight()
                    s['ipv6'], ipv6_before = ipv6.configure(s['domain'])
                    try:
                        s['bbr'] = bbr.configure()
                        s['swap'] = swap.configure()
                    except Exception:
                        ipv6.rollback(ipv6_before)
                        raise
                    checkpoint(STATE, s, 'BBR', True)
                    UI.stage(5, 13, 'Настройка IPv6, TCP BBR, FQ и swap', 'ok')
                    checkpoint(STATE, s, 'FIREWALL'); UI.stage(6, 13, 'Настройка сетевой защиты')
                    firewall(s); checkpoint(STATE, s, 'FIREWALL', True)
                    UI.stage(6, 13, 'Настройка сетевой защиты', 'ok')
                elif not late:
                    # A reboot between attempts can re-enable IPv6 on the interface while the
                    # persisted files stay intact; restore the runtime policy before acceptance.
                    # A late resume has verified it read-only in late_resume_preflight().
                    s['ipv6'], _ = ipv6.configure(s['domain'])
                if pending('WARP'):
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
                elif s['warp_mode'] == 'adopt' and resume_target == 'CERTIFICATE':
                    adopt_warp(s, lock, work)
                elif s['warp_mode'] != 'skip':
                    s['warp_health'] = menu_warp.health()
                if pending('CERTIFICATE'):
                    checkpoint(STATE, s, 'CERTIFICATE'); UI.stage(8, 13, 'Получение SSL-сертификата')
                    certificate(s,s['email']); checkpoint(STATE, s, 'CERTIFICATE', True)
                    UI.stage(8, 13, 'Получение SSL-сертификата', 'ok')
                if pending('NGINX_PORTAL'):
                    checkpoint(STATE, s, 'NGINX_PORTAL'); UI.stage(9, 13, 'Настройка nginx и страницы входа')
                    decoy(s); checkpoint(STATE, s, 'NGINX_PORTAL', True)
                    UI.stage(9, 13, 'Настройка nginx и страницы входа', 'ok')
                if pending('PANEL'):
                    checkpoint(STATE, s, 'PANEL'); UI.stage(10, 13, 'Установка панели 3x-ui')
                    panel(s,lock,work); checkpoint(STATE, s, 'PANEL', True)
                    UI.stage(10, 13, 'Установка панели 3x-ui', 'ok')
                if pending('SECURITY'):
                    checkpoint(STATE, s, 'SECURITY'); UI.stage(11, 13, 'Защита и продление сертификата')
                    secure(s)
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
