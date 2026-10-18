"""Install and verify the standard Linux TCP BBR congestion controller."""
import json
import os
from pathlib import Path
import platform
import re
import socket

from .core import InstallError, atomic_write, require, run


SYSCTL_CONFIG = Path('/etc/sysctl.d/99-zz-bob-auto-xray-bbr.conf')
MODULE_CONFIG = Path('/etc/modules-load.d/99-bob-auto-xray-bbr.conf')
DESIRED = {
    'net.core.default_qdisc': 'fq',
    'net.ipv4.tcp_congestion_control': 'bbr',
}
RESTRICTED_VIRTUALIZATION = {
    'docker', 'lxc', 'lxc-libvirt', 'openvz', 'podman', 'systemd-nspawn', 'wsl',
}


def _safe(command, *args):
    try:
        return command(*args).strip()
    except InstallError:
        return ''


def _sysctl(command, key):
    return command('sysctl', '-n', key).strip()


def _assignments(path):
    found = []
    try:
        lines = Path(path).read_text().splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return found
    for number, line in enumerate(lines, 1):
        line = line.split('#', 1)[0].split(';', 1)[0].strip()
        match = re.fullmatch(r'(-?[A-Za-z0-9_.\/]+)\s*=\s*(\S+)\s*', line)
        if match:
            key = match.group(1).lstrip('-').replace('/', '.')
            if key in DESIRED:
                found.append({'file': str(path), 'line': number,
                              'key': key, 'value': match.group(2)})
    return found


def persistent_assignments(etc=Path('/etc'), run_root=Path('/run')):
    """Read administrator/runtime sysctl sources; vendor defaults are overridden normally."""
    paths = [etc / 'sysctl.conf']
    paths.extend(sorted((etc / 'sysctl.d').glob('*.conf')))
    paths.extend(sorted((run_root / 'sysctl.d').glob('*.conf')))
    values = []
    for path in paths:
        if path != SYSCTL_CONFIG:
            values.extend(_assignments(path))
    return values


def conflicts(etc=Path('/etc'), run_root=Path('/run')):
    return [item for item in persistent_assignments(etc, run_root)
            if item['value'] != DESIRED[item['key']]]


def duplicates(etc=Path('/etc'), run_root=Path('/run')):
    return [item for item in persistent_assignments(etc, run_root)
            if item['value'] == DESIRED[item['key']]]


def _module_kind(command):
    filename = _safe(command, 'modinfo', '-F', 'filename', 'tcp_bbr')
    if filename == '(builtin)':
        return 'built-in'
    if filename:
        return 'loadable'
    return 'unknown'


def qdisc_diagnostics(command=run):
    result = {'defaultRouteInterface': '', 'interfaceQdisc': []}
    routes = _safe(command, 'ip', '-j', 'route', 'show', 'default')
    if not routes:
        return result
    try:
        rows = json.loads(routes)
        interface = next((row.get('dev') for row in rows if row.get('dev')), '')
    except (json.JSONDecodeError, TypeError):
        interface = ''
    result['defaultRouteInterface'] = interface
    if interface:
        output = _safe(command, 'tc', '-j', 'qdisc', 'show', 'dev', interface)
        try:
            result['interfaceQdisc'] = json.loads(output) if output else []
        except json.JSONDecodeError:
            result['interfaceQdisc'] = [{'raw': output}]
    return result


def probe(command=run, etc=Path('/etc'), run_root=Path('/run')):
    """Read-only compatibility probe. It must never load modules or write sysctls."""
    available = _safe(command, 'sysctl', '-n',
                      'net.ipv4.tcp_available_congestion_control').split()
    virtualization = _safe(command, 'systemd-detect-virt') or 'none'
    report = {
        'kernel': _safe(command, 'uname', '-r') or platform.release(),
        'virtualization': virtualization,
        'restrictedContainer': virtualization in RESTRICTED_VIRTUALIZATION,
        'current': {key: _safe(command, 'sysctl', '-n', key) for key in DESIRED},
        'availableCongestionControl': available,
        'module': _module_kind(command),
        'persistentAssignments': persistent_assignments(etc, run_root),
        'conflicts': conflicts(etc, run_root),
        'duplicates': duplicates(etc, run_root),
        'qdisc': qdisc_diagnostics(command),
    }
    report['bbrAvailable'] = 'bbr' in available
    report['bbrSupported'] = report['bbrAvailable'] or report['module'] in ('built-in', 'loadable')
    return report


def preflight(command=run, etc=Path('/etc'), run_root=Path('/run'), allow_modprobe=False):
    report = probe(command, etc, run_root)
    require(not report['restrictedContainer'],
            'TCP BBR requires host sysctl control; restricted container detected: '
            + report['virtualization'])
    require(report['bbrSupported'],
            'Kernel does not expose BBR and no standard tcp_bbr module was found')
    report['moduleLoadedDuringPreflight'] = False
    if allow_modprobe and not report['bbrAvailable']:
        command('modprobe', 'tcp_bbr')
        available = _sysctl(command, 'net.ipv4.tcp_available_congestion_control').split()
        require('bbr' in available, 'tcp_bbr loaded but BBR is still unavailable')
        report['availableCongestionControl'] = available
        report['bbrAvailable'] = True
        report['moduleLoadedDuringPreflight'] = True
    require(not report['conflicts'],
            'Conflicting persistent TCP BBR sysctl: ' + json.dumps(report['conflicts']))
    require(not report['duplicates'],
            'TCP BBR/FQ is already configured in a foreign persistent sysctl file: '
            + json.dumps(report['duplicates']))
    return report


def _snapshot(path):
    path = Path(path)
    return path.read_bytes() if path.exists() else None


def _restore(path, previous, mode):
    path = Path(path)
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, previous.decode(), mode)


def configure(command=run, sysctl_config=SYSCTL_CONFIG,
              module_config=MODULE_CONFIG, etc=Path('/etc'), run_root=Path('/run')):
    """Apply the project-owned settings, rolling files and runtime back on failure."""
    before = preflight(command, etc, run_root)
    old_runtime = {key: _sysctl(command, key) for key in DESIRED}
    old_sysctl = _snapshot(sysctl_config)
    old_module = _snapshot(module_config)
    module_kind = before['module']
    loaded = False
    try:
        if not before['bbrAvailable']:
            command('modprobe', 'tcp_bbr')
            loaded = True
            available = _sysctl(command, 'net.ipv4.tcp_available_congestion_control').split()
            require('bbr' in available, 'tcp_bbr loaded but BBR is still unavailable')
        if module_kind == 'loadable':
            atomic_write(module_config, 'tcp_bbr\n', 0o644)
        else:
            Path(module_config).unlink(missing_ok=True)
        content = ''.join(f'{key} = {value}\n' for key, value in DESIRED.items())
        atomic_write(sysctl_config, content, 0o644)
        for key, value in DESIRED.items():
            command('sysctl', '-w', f'{key}={value}')
        result = verify(command, sysctl_config, etc, run_root)
        result['moduleLoadedByInstaller'] = loaded
        return result
    except Exception:
        for key, value in reversed(list(old_runtime.items())):
            try:
                command('sysctl', '-w', f'{key}={value}')
            except Exception:
                pass
        _restore(sysctl_config, old_sysctl, 0o644)
        _restore(module_config, old_module, 0o644)
        raise


def socket_evidence(available=None):
    """Prove BBR on a connected new socket and an explicit per-socket override."""
    option = getattr(socket, 'TCP_CONGESTION', 13)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    accepted = None
    try:
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        client.connect(server.getsockname())
        accepted, _ = server.accept()
        active = client.getsockopt(socket.IPPROTO_TCP, option, 32).rstrip(b'\0').decode()
    finally:
        client.close()
        if accepted is not None:
            accepted.close()
        server.close()
    alternatives = [name for name in (available or []) if name != 'bbr']
    explicit = None
    if alternatives:
        test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            requested = alternatives[0]
            test.setsockopt(socket.IPPROTO_TCP, option, requested.encode())
            selected = test.getsockopt(socket.IPPROTO_TCP, option, 32).rstrip(b'\0').decode()
            explicit = {'requested': requested, 'selected': selected}
        finally:
            test.close()
    return {'connectedSocketCongestionControl': active,
            'explicitAlternative': explicit}


def verify(command=run, sysctl_config=SYSCTL_CONFIG,
           etc=Path('/etc'), run_root=Path('/run')):
    current = {key: _sysctl(command, key) for key in DESIRED}
    require(current == DESIRED, 'Runtime TCP BBR/FQ settings differ: ' + json.dumps(current))
    expected = ''.join(f'{key} = {value}\n' for key, value in DESIRED.items())
    require(Path(sysctl_config).read_text() == expected,
            'Managed TCP BBR sysctl file is absent or changed')
    available = _sysctl(command, 'net.ipv4.tcp_available_congestion_control').split()
    require('bbr' in available, 'BBR is absent from available congestion controls')
    active_conflicts = conflicts(etc, run_root)
    require(not active_conflicts,
            'Conflicting persistent TCP BBR sysctl: ' + json.dumps(active_conflicts))
    foreign_duplicates = duplicates(etc, run_root)
    require(not foreign_duplicates,
            'Duplicate foreign TCP BBR/FQ sysctl: ' + json.dumps(foreign_duplicates))
    sockets = socket_evidence(available)
    require(sockets['connectedSocketCongestionControl'] == 'bbr',
            'A new connected TCP socket did not inherit BBR')
    if sockets['explicitAlternative']:
        require(sockets['explicitAlternative']['requested'] ==
                sockets['explicitAlternative']['selected'],
                'Explicit per-socket congestion control was not honored')
    return {
        'runtime': current,
        'persistentConfig': str(sysctl_config),
        'availableCongestionControl': available,
        'conflicts': active_conflicts,
        'duplicates': foreign_duplicates,
        'sockets': sockets,
        'qdisc': qdisc_diagnostics(command),
    }
