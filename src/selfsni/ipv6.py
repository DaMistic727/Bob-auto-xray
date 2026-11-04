"""IPv4-only host policy, best effort and independent of TCP BBR, FQ and swap.

Bob owns the policy only in mode ``managed``. A host where another tool has
already disabled IPv6 is ``external``; a host where Bob cannot apply the policy
safely is ``skipped``. In both of these modes Bob writes nothing, and the
installation continues only if public reachability of internal services over
IPv6 is excluded (``check_exposure``).
"""
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
UFW_DEFAULTS = Path('/etc/default/ufw')
# Ports Bob publishes by design (HTTP-01/redirect and REALITY); SSH ports are added per host.
PUBLIC_PORTS = frozenset({80, 443})
UFW_REQUIRED = ('IPv6 remains enabled, but UFW does not filter IPv6 (IPV6=yes in /etc/default/ufw); '
                'public reachability of internal services over IPv6 cannot be excluded')


class Unsupported(InstallError):
    """The policy cannot be applied safely on this host; nothing was written."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class ApplyFailed(InstallError):
    """Applying failed after a partial change, and every change was rolled back."""


class RollbackError(InstallError):
    """A partial change could not be undone: the host is in an unknown state."""


def _supported(condition, reason, message):
    if not condition:
        raise Unsupported(reason, message)


def readable_by_networkd(directory_mode, file_mode):
    return directory_mode & 0o005 == 0o005 and bool(file_mode & 0o004)


def _interfaces(command):
    rows = json.loads(command('ip', '-j', 'link', 'show'))
    return [row['ifname'] for row in rows]


def _value(command, key):
    return command('sysctl', '-n', key).strip()


def _network_path(command, etc, destination, local=None):
    """Locate the netplan-generated networkd file for the IPv4 route."""
    route = json.loads(command('ip', '-j', '-4', 'route', 'get', destination))
    _supported(len(route) == 1 and (local is None or route[0].get('prefsrc') == local),
               'topology', 'IPv4 route does not use the expected local address')
    interface = route[0].get('dev')
    _supported(interface in _interfaces(command), 'topology', 'IPv4 SSH interface is missing')
    status = command('networkctl', 'status', '--no-pager', '--full', interface)
    match = re.search(r'^\s*Network File:\s*(\S+)\s*$', status, re.M)
    _supported(match is not None, 'topology', 'IPv4 SSH interface is not managed by networkd')
    network_file = Path(match.group(1))
    _supported(network_file.parent == Path('/run/systemd/network') and
               re.fullmatch(r'10-netplan-[\w.-]+\.network', network_file.name),
               'topology', 'IPv4 SSH interface does not use a netplan-generated networkd file')
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
                raise Unsupported('external', f'Foreign networkd IPv6 setting: {other}')
    return path


def _assignments(etc, run_root, config):
    """Every persistent disable_ipv6 assignment outside Bob's file, as (location, value)."""
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
                found.append((f'{path}:{number}', match.group(2)))
    return found


def _foreign(etc, run_root, config):
    """Assignments that contradict the IPv4-only policy; agreeing ones (= 1) are not conflicts."""
    return [where for where, value in _assignments(etc, run_root, config) if value != '1']


def _runtime(command):
    names = ['all', 'default', *_interfaces(command)]
    values = {name: _value(command, f'net.ipv6.conf.{name}.disable_ipv6')
              for name in dict.fromkeys(names)}
    # Every scope, as verify() does: a link-local address also means IPv6 is live.
    addresses = json.loads(command('ip', '-j', '-6', 'address', 'show'))
    disabled = (all(value == '1' for value in values.values()) and
                not any(row.get('addr_info') for row in addresses))
    return values, disabled


def _ssh_guard(command, ssh_connection):
    """Disabling IPv6 must never cut an SSH session; an unproven session is treated as IPv6."""
    ssh = (os.environ.get('SSH_CONNECTION', '') if ssh_connection is None
           else ssh_connection).split()
    try:
        ipv4 = len(ssh) == 4 and all(ipaddress.ip_address(x).version == 4 for x in (ssh[0], ssh[2]))
    except ValueError:
        ipv4 = False
    _supported(ipv4, 'ssh', 'IPv4 SSH connection is required; IPv6 SSH must not be disconnected')
    _supported(not any('sshd' in line for line in command(
                   'ss', '-H', '-6', '-tnp', 'state', 'established').splitlines()),
               'ssh', 'An active IPv6 SSH connection must not be disconnected')
    return ssh


def ufw_filters_ipv6(path=UFW_DEFAULTS):
    """UFW manages ip6tables only with IPV6=yes (the Ubuntu default) in /etc/default/ufw."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    values = re.findall(r'^\s*IPV6\s*=\s*["\']?(\w+)["\']?\s*(?:#.*)?$', path.read_text(), re.M)
    return bool(values) and values[-1].lower() == 'yes'


def _ipv6_listeners(command):
    for line in command('ss', '-H', '-6', '-lnt').splitlines():
        fields = line.split()
        require(len(fields) >= 4 and ':' in fields[3], 'Unexpected IPv6 listener list')
        host, _, port = fields[3].rpartition(':')
        host = host.strip('[]').split('%', 1)[0]
        yield ipaddress.ip_address('::' if host == '*' else host), int(port)


def check_exposure(ssh_ports, command=run, ufw_defaults=UFW_DEFAULTS):
    """IPv6 is not Bob's: prove that only published services are reachable over it.

    Read-only. UFW (default deny incoming, enabled by the FIREWALL stage) must
    filter IPv6, and no TCP listener on a routable IPv6 address may serve a
    port other than SSH, 80 and 443. Anything else is a fatal refusal.
    """
    require(ufw_filters_ipv6(ufw_defaults), UFW_REQUIRED)
    public = {int(port) for port in ssh_ports} | PUBLIC_PORTS
    exposed = sorted({port for address, port in _ipv6_listeners(command)
                      if not (address.is_loopback or address.is_link_local) and port not in public})
    require(not exposed, 'Internal TCP port listens on a public IPv6 address: ' +
            ', '.join(map(str, exposed)))
    return {'ufwIPv6': True, 'publicPorts': sorted(public)}


def _unmanaged(report, reason, detail, command, ufw_defaults=None):
    """Result for a host whose IPv6 Bob does not touch; nothing is written."""
    values, disabled = _runtime(command)
    # Only another tool's policy that is already effective counts as external.
    mode = 'external' if reason == 'external' and disabled else 'skipped'
    if ufw_defaults is not None and (ufw_defaults.exists() or ufw_defaults.is_symlink()):
        # Fail before any change when UFW is already known not to cover IPv6.
        require(ufw_filters_ipv6(ufw_defaults), UFW_REQUIRED)
    return dict(report, mode=mode, reason=reason, detail=detail,
                runtimeDisabled=disabled, interfaces=list(values))


def preflight(domain, command=run, etc=Path('/etc'), run_root=Path('/run'),
              config=CONFIG, ssh_connection=None, ufw_defaults=None):
    """Only read state; never write a sysctl or a file.

    Returns the decision: mode ``managed`` (Bob may apply its policy) or a
    non-managed mode with the reason. DNS, IPv4 and Bob's own files stay fatal.
    """
    require(_value(command, 'net.ipv4.ip_forward') in ('0', '1'),
            'IPv4 sysctl is unavailable')
    addresses = json.loads(command('ip', '-j', '-6', 'address', 'show', 'scope', 'global'))
    # The DNS answer is checked again by provision.preflight() against the public IPv4.
    require(not any(row[0] == socket.AF_INET6 for row in
                    socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)),
            'Installation FQDN has an AAAA record; IPv4-only requires removing it')
    require(not config.is_symlink(), 'Managed IPv6 sysctl path is a symlink')
    if config.exists():
        require(config.read_text() == CONTENT, 'Managed IPv6 sysctl file was changed')
    assignments = _assignments(etc, run_root, config)
    report = {'globalIPv6AddressesBefore': sum(len(row.get('addr_info', [])) for row in addresses),
              'foreignAssignments': [where for where, _ in assignments]}
    try:
        conflicts = [where for where, value in assignments if value != '1']
        _supported(not conflicts, 'conflict',
                   'Foreign persistent IPv6 sysctl assignment: ' + ', '.join(conflicts))
        # Bob never layers its policy over another tool's; once Bob's own file
        # exists (a resumed managed install) agreeing values are harmless.
        _supported(not assignments or config.exists(), 'external',
                   'Foreign persistent IPv6 sysctl assignment: ' + ', '.join(report['foreignAssignments']))
        ssh = _ssh_guard(command, ssh_connection)
        network_path = _network_path(command, etc, ssh[0], ssh[2])
    except Unsupported as error:
        return _unmanaged(report, error.reason, str(error), command, ufw_defaults)
    if network_path.exists():
        require(network_path.read_text() in (LEGACY_NETWORK_CONTENT, NETWORK_CONTENT),
                'Managed networkd drop-in was changed')
    return dict(report, mode='managed', sshIPv4=True, networkDropin=str(network_path))


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
    """Restore both files and every runtime value, then prove it by re-reading."""
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
    if not errors:
        try:
            changed = [key for key, value in before['runtime'].items() if _value(command, key) != value]
            if changed:
                errors.append('runtime differs: ' + ', '.join(changed))
            path = before['network_path']
            for name, target, content in (('sysctl file', config, before['file']),
                                          ('networkd drop-in', path, before['network_file'])):
                if (target.read_bytes() if target.exists() else None) != content:
                    errors.append(name + ' differs')
        except (InstallError, OSError) as error:
            errors.append(str(error))
    if errors:
        raise RollbackError('IPv6 rollback incomplete: ' + '; '.join(errors))


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
    require(readable_by_networkd(network_path.parent.stat().st_mode, network_path.stat().st_mode),
            'Managed networkd drop-in is not readable by systemd-networkd')
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
    return {'mode': 'managed', 'interfaces': names, 'persistentConfig': str(config),
            'networkDropin': str(network_path)}


def _enforce(report, command, config, etc, run_root, ssh_connection):
    # A new IPv6 SSH session may have appeared since preflight; nothing is written yet.
    _ssh_guard(command, ssh_connection)
    network_path = Path(report['networkDropin'])
    try:
        before = snapshot(command, config, network_path)
    except (InstallError, OSError) as error:
        raise Unsupported('snapshot', f'IPv6 state cannot be saved for rollback: {error}') from error
    try:
        atomic_write(config, CONTENT, 0o644)
        # systemd-networkd runs as systemd-network: under the installer's umask 077
        # the new directory was 0700 and networkd silently ignored the drop-in.
        network_path.parent.mkdir(parents=True, exist_ok=True)
        network_path.parent.chmod(0o755)
        atomic_write(network_path, NETWORK_CONTENT, 0o644)
        command('networkctl', 'reload')
        for key, value in DESIRED.items():
            command('sysctl', '-w', f'{key}={value}')
        for name in _interfaces(command):
            command('sysctl', '-w', f'net.ipv6.conf.{name}.disable_ipv6=1')
        return verify(command, config, etc, run_root, ssh_connection), before
    except BaseException as error:
        # Also on Ctrl+C: a partial policy is never left behind. A failed
        # rollback raises RollbackError, which is always fatal.
        rollback(before, command, config)
        if isinstance(error, (InstallError, OSError, ValueError)):
            raise ApplyFailed(f'{error} (IPv6 changes rolled back)') from error
        raise


def configure(domain, command=run, config=CONFIG, etc=Path('/etc'),
              run_root=Path('/run'), ssh_connection=None):
    """Apply Bob's managed policy or raise; used where Bob already owns IPv6."""
    report = preflight(domain, command, etc, run_root, config, ssh_connection)
    if report['mode'] != 'managed':
        raise Unsupported(report['reason'], report['detail'])
    return _enforce(report, command, config, etc, run_root, ssh_connection)


def apply(domain, ssh_ports, command=run, config=CONFIG, etc=Path('/etc'),
          run_root=Path('/run'), ssh_connection=None, ufw_defaults=UFW_DEFAULTS):
    """Best-effort policy for a fresh installation; returns the result kept in state['ipv6'].

    Raises only when a partial change could not be rolled back (RollbackError)
    or when IPv6 stays Bob-unmanaged and its exposure cannot be excluded.
    """
    report = preflight(domain, command, etc, run_root, config, ssh_connection, ufw_defaults)
    if report['mode'] == 'managed':
        try:
            return _enforce(report, command, config, etc, run_root, ssh_connection)[0]
        except Unsupported as error:
            report = _unmanaged(report, error.reason, str(error), command)
        except ApplyFailed as error:
            report = _unmanaged(report, 'apply-failed', str(error), command)
    return dict(report, exposure=check_exposure(ssh_ports, command, ufw_defaults))
