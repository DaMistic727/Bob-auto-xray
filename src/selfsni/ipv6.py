"""Persistent IPv4-only host setup, independent of TCP BBR."""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket

from .core import InstallError, atomic_write, require, run

CONFIG = Path('/etc/sysctl.d/99-zz-bob-auto-xray-ipv6.conf')
DESIRED = {'net.ipv6.conf.all.disable_ipv6': '1',
           'net.ipv6.conf.default.disable_ipv6': '1'}
CONTENT = (''.join(f'{key} = {value}\n' for key, value in DESIRED.items()) +
           'net.ipv6.conf.*.disable_ipv6 = 1\n')
LEGACY_NETWORK_CONTENT = '[Network]\nLinkLocalAddressing=no\nIPv6AcceptRA=no\n'
# Keep networkd's address generation policy aligned with the sysctl policy.
NETWORK_CONTENT = LEGACY_NETWORK_CONTENT + 'IPv6LinkLocalAddressGenerationMode=none\n'
NETWORK_DROPIN = '99-zz-bob-auto-xray-ipv4-only.conf'


def _interfaces(command):
    rows = json.loads(command('ip', '-j', 'link', 'show'))
    return [row['ifname'] for row in rows]


def _value(command, key):
    return command('sysctl', '-n', key).strip()


def _network_path(command, etc, destination, local=None):
    """Locate the netplan-generated networkd file for the IPv4 route."""
    route = json.loads(command('ip', '-j', '-4', 'route', 'get', destination))
    require(len(route) == 1 and (local is None or route[0].get('prefsrc') == local),
            'IPv4 route does not use the expected local address')
    interface = route[0].get('dev')
    require(interface in _interfaces(command), 'IPv4 SSH interface is missing')
    status = command('networkctl', 'status', '--no-pager', '--full', interface)
    match = re.search(r'^\s*Network File:\s*(\S+)\s*$', status, re.M)
    require(match is not None, 'IPv4 SSH interface is not managed by networkd')
    network_file = Path(match.group(1))
    require(network_file.parent == Path('/run/systemd/network') and
            re.fullmatch(r'10-netplan-[\w.-]+\.network', network_file.name),
            'IPv4 SSH interface does not use a netplan-generated networkd file')
    path = etc / 'systemd/network' / (network_file.name + '.d') / NETWORK_DROPIN
    require(not path.parent.is_symlink() and not path.is_symlink(),
            'Managed networkd drop-in path is a symlink')
    for root in (etc / 'systemd/network', Path('/run/systemd/network'),
                 Path('/usr/lib/systemd/network')):
        directory = root / (network_file.name + '.d')
        for other in directory.glob('*.conf'):
            if other == path:
                continue
            if re.search(r'^\s*(LinkLocalAddressing|IPv6AcceptRA|IPv6LinkLocalAddressGenerationMode)\s*=',
                         other.read_text(), re.M):
                raise InstallError(f'Foreign networkd IPv6 setting: {other}')
    return path


def _foreign(etc, run_root, config):
    paths = [etc / 'sysctl.conf', *(etc / 'sysctl.d').glob('*.conf'),
             *(run_root / 'sysctl.d').glob('*.conf')]
    found = []
    for path in paths:
        if path == config:
            continue
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            continue
        for number, line in enumerate(lines, 1):
            line = line.split('#', 1)[0].split(';', 1)[0].strip()
            match = re.fullmatch(r'-?([\w./*]+)\s*=\s*(\S+)', line)
            if match and re.fullmatch(r'net\.ipv6\.conf\.[^.]+\.disable_ipv6',
                                      match.group(1).replace('/', '.')):
                found.append(f'{path}:{number}')
    return found


def preflight(domain, command=run, etc=Path('/etc'), run_root=Path('/run'),
              config=CONFIG, ssh_connection=None):
    """Only read state; never write a sysctl or a file."""
    ssh = (os.environ.get('SSH_CONNECTION', '') if ssh_connection is None
           else ssh_connection).split()
    require(len(ssh) == 4 and all(ipaddress.ip_address(x).version == 4
                                  for x in (ssh[0], ssh[2])),
            'IPv4 SSH connection is required; IPv6 SSH must not be disconnected')
    require(not any('sshd' in line for line in command(
                'ss', '-H', '-6', '-tnp', 'state', 'established').splitlines()),
            'An active IPv6 SSH connection must not be disconnected')
    require(_value(command, 'net.ipv4.ip_forward') in ('0', '1'),
            'IPv4 sysctl is unavailable')
    addresses = json.loads(command('ip', '-j', '-6', 'address', 'show', 'scope', 'global'))
    # The DNS answer is checked again by provision.preflight() against the public IPv4.
    require(not any(row[0] == socket.AF_INET6 for row in
                    socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)),
            'Installation FQDN has an AAAA record; IPv4-only requires removing it')
    foreign = _foreign(etc, run_root, config)
    require(not foreign, 'Foreign persistent IPv6 sysctl assignment: ' + ', '.join(foreign))
    require(not config.is_symlink(), 'Managed IPv6 sysctl path is a symlink')
    if config.exists():
        require(config.read_text() == CONTENT, 'Managed IPv6 sysctl file was changed')
    network_path = _network_path(command, etc, ssh[0], ssh[2])
    if network_path.exists():
        require(network_path.read_text() in (LEGACY_NETWORK_CONTENT, NETWORK_CONTENT),
                'Managed networkd drop-in was changed')
    return {'sshIPv4': True, 'globalIPv6AddressesBefore': sum(
        len(row.get('addr_info', [])) for row in addresses),
        'foreignAssignments': foreign, 'networkDropin': str(network_path)}


def snapshot(command=run, config=CONFIG, network_path=None):
    names = ['all', 'default', *_interfaces(command)]
    keys = dict.fromkeys(f'net.ipv6.conf.{name}.disable_ipv6' for name in names)
    return {'runtime': {key: _value(command, key) for key in keys},
            'file': config.read_bytes() if config.exists() else None,
            'mode': config.stat().st_mode & 0o777 if config.exists() else None,
            'network_path': network_path,
            'network_file': network_path.read_bytes() if network_path.exists() else None,
            'network_mode': network_path.stat().st_mode & 0o777 if network_path.exists() else None}


def rollback(before, command=run, config=CONFIG):
    errors = []
    try:
        if before['file'] is None:
            config.unlink(missing_ok=True)
        else:
            atomic_write(config, before['file'].decode(), before['mode'])
    except Exception as error:
        errors.append(str(error))
    try:
        path = before['network_path']
        if before['network_file'] is None:
            path.unlink(missing_ok=True)
            if path.parent.is_dir() and not any(path.parent.iterdir()):
                path.parent.rmdir()
        else:
            atomic_write(path, before['network_file'].decode(), before['network_mode'])
    except Exception as error:
        errors.append(str(error))
    try:
        command('networkctl', 'reload')
    except Exception as error:
        errors.append(str(error))
    for key, value in before['runtime'].items():
        try:
            command('sysctl', '-w', f'{key}={value}')
        except Exception as error:
            errors.append(str(error))
    if errors:
        raise InstallError('IPv6 rollback incomplete: ' + '; '.join(errors))


def verify(command=run, config=CONFIG, etc=Path('/etc'), run_root=Path('/run'),
           ssh_connection=None):
    require(config.is_file() and not config.is_symlink() and config.read_text() == CONTENT,
            'Managed IPv6 sysctl file is absent or changed')
    require(not _foreign(etc, run_root, config), 'Foreign persistent IPv6 sysctl assignment')
    ssh = (os.environ.get('SSH_CONNECTION', '') if ssh_connection is None
           else ssh_connection).split()
    destination = ssh[0] if len(ssh) == 4 else '1.1.1.1'
    local = ssh[2] if len(ssh) == 4 else None
    network_path = _network_path(command, etc, destination, local)
    require(network_path.is_file() and network_path.read_text() == NETWORK_CONTENT,
            'Managed networkd drop-in is absent or changed')
    names = ['all', 'default', *_interfaces(command)]
    require(all(_value(command, f'net.ipv6.conf.{name}.disable_ipv6') == '1'
                for name in names), 'IPv6 remains enabled on an interface')
    require(not any(row.get('addr_info') for row in json.loads(
                command('ip', '-j', '-6', 'address', 'show'))),
            'IPv6 address remains (including link-local)')
    require(json.loads(command('ip', '-j', '-4', 'address', 'show', 'scope', 'global')),
            'No global IPv4 address')
    public = command('curl', '-4fsS', '--connect-timeout', '10', '--max-time', '20',
                     'https://api.ipify.org').strip()
    require(ipaddress.ip_address(public).version == 4,
            'System IPv4 HTTP connectivity failed')
    return {'interfaces': names, 'persistentConfig': str(config),
            'networkDropin': str(network_path)}


def configure(domain, command=run, config=CONFIG, etc=Path('/etc'),
              run_root=Path('/run'), ssh_connection=None):
    report = preflight(domain, command, etc, run_root, config, ssh_connection)
    network_path = Path(report['networkDropin'])
    before = snapshot(command, config, network_path)
    try:
        atomic_write(config, CONTENT, 0o644)
        atomic_write(network_path, NETWORK_CONTENT, 0o644)
        command('networkctl', 'reload')
        for key, value in DESIRED.items():
            command('sysctl', '-w', f'{key}={value}')
        for name in _interfaces(command):
            command('sysctl', '-w', f'net.ipv6.conf.{name}.disable_ipv6=1')
        return verify(command, config, etc, run_root, ssh_connection), before
    except Exception:
        rollback(before, command, config)
        raise
