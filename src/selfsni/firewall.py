"""Read-only assessment of an existing UFW configuration and of host listeners.

Bob appends its own allow rules after the operator's rules, and iptables acts
on the first rule that matches. A rule is therefore judged by its effect on
the connections Bob needs and on the ports Bob keeps private, never by its
spelling:

* compatible: kept as is and reported;
* conflict: blocks a required connection, opens an internal port to the
  public, or cannot be assessed and may do either. Bob never edits, reorders
  or deletes a foreign rule; the operator gets the rule and the fix;
* consent: Bob's own change would alter how the host works today. Services
  that default deny would cut off are listed separately (cutoff): sockets come
  and go, so they are shown and confirmed once, at preflight, and are not a
  reason to stop the FIREWALL stage later.

An option the parser does not know only narrows what a rule matches, so the
known part of a rule is an upper bound of its effect. An upper bound is enough
to prove that a rule is harmless and never enough to rely on a rule.
"""
from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import re
import shlex

from .core import InstallError

UFW_ROOT = Path('/etc/ufw')
UFW_DEFAULTS = Path('/etc/default/ufw')
PROC_ROOT = Path('/proc')
# user.rules names its chains ufw-*, user6.rules ufw6-*.
CHAIN = re.compile(r'ufw6?-user-input')
PUBLIC_TCP = (80, 443)
XRAY_API, XRAY_METRICS, WARP_SOCKS = 62789, 11111, 40000
# Answered by UFW's own before.rules (DHCP client, DHCPv6 client, mDNS, UPnP).
UFW_BUILTIN_UDP = frozenset({68, 546, 5353, 1900})
ACTIONS = {'accept': 'ALLOW', 'limit': 'LIMIT', 'deny': 'DENY', 'unknown': 'неизвестное действие',
           'pass': 'без действия'}
HOW_TO_DELETE = 'найдите правило: `ufw status numbered`, удалите его: `ufw delete <номер>`'


@dataclass(frozen=True)
class Rule:
    family: int
    line: str
    action: str             # accept | limit | deny | pass | unknown
    proto: str = None       # None: every protocol
    ports: tuple = None     # ((low, high), ...); None: every port
    source: object = None   # ip_network; None: any address
    destination: object = None
    interface: str = None
    narrowed: bool = False  # has matches this parser does not model

    def covers(self, proto, port):
        return self.proto in (None, 'all', proto) and (
            self.ports is None or any(low <= port <= high for low, high in self.ports))

    @property
    def wide(self):
        """No source, destination or interface limits what the rule matches."""
        return not (self.source or self.destination or self.interface)

    @property
    def everyone(self):
        """True when the rule matches every packet to the ports it names."""
        return self.wide and not self.narrowed

    def describe(self):
        ports = 'все порты' if self.ports is None else ','.join(
            str(low) if low == high else f'{low}:{high}' for low, high in self.ports)
        text = f'{ACTIONS[self.action]} {self.proto or "любой протокол"} {ports}'
        if self.source:
            text += f' от {self.source}'
        if self.destination:
            text += f' к {self.destination}'
        if self.interface:
            text += f' на интерфейсе {self.interface}'
        return text


@dataclass(frozen=True)
class Listener:
    proto: str
    address: object         # ip_address; None for the dual-stack wildcard "*"
    port: int
    processes: tuple
    text: str

    @property
    def public(self):
        return self.address is None or not (self.address.is_loopback or self.address.is_link_local)

    @property
    def owner(self):
        return ', '.join(self.processes) or 'процесс не определён'

    def describe(self):
        return f'{self.proto.upper()}/{self.port} ({self.owner}, {self.text.split()[3]})'


@dataclass(frozen=True)
class Client:
    """The SSH connection the installer runs in (SSH_CONNECTION)."""
    address: object
    server: object
    port: int


@dataclass
class Plan:
    enabled: bool = False
    incoming: str = 'DROP'
    outgoing: str = 'ACCEPT'
    allow: list = field(default_factory=list)       # TCP ports Bob adds an allow rule for
    removable: list = field(default_factory=list)   # of them, ports no existing rule names
    kept: list = field(default_factory=list)        # compatible: reported, untouched
    changes: list = field(default_factory=list)     # what Bob does
    consent: list = field(default_factory=list)     # of it, what needs an explicit yes
    cutoff: list = field(default_factory=list)      # services that default deny would cut off
    conflicts: list = field(default_factory=list)   # (what, fix)
    rules: list = field(default_factory=list)
    interfaces: object = None                       # public interfaces; None: unknown

    def refuse(self):
        if self.conflicts:
            raise InstallError(
                'Существующая конфигурация UFW несовместима с установкой. Bob не изменяет и не '
                'удаляет чужие правила.\n' + '\n'.join(
                    f'• {what}\n  Как исправить: {fix}' for what, fix in self.conflicts) +
                '\nПосле исправления запустите установщик снова.')

    def exposes(self, port):
        """True when an existing rule would leave TCP `port` open to the public."""
        return any(_exposing(self.rules, family, port, self.interfaces)[0]
                   for family in {rule.family for rule in self.rules})

    def summary(self):
        return {'enabled': self.enabled, 'incoming': self.incoming, 'outgoing': self.outgoing,
                'allow': list(self.allow), 'kept': list(self.kept), 'changes': list(self.changes),
                'consent': list(self.consent), 'cutoff': list(self.cutoff)}


def _ports(value):
    ranges = []
    for part in value.split(','):
        low, _, high = part.partition(':')
        low, high = int(low), int(high or low)
        if not 0 <= low <= high <= 65535:
            raise ValueError(value)
        ranges.append((low, high))
    return tuple(ranges)


def parse_rule(line, family):
    """One `-A ufw-user-input` line as written by ufw into user.rules/user6.rules."""
    anything = Rule(family, line, 'unknown', narrowed=True)
    try:
        tokens = shlex.split(line)
    except ValueError:
        return anything
    # A negation widens a match and -I changes the order: neither is modelled.
    if len(tokens) < 2 or tokens[0] != '-A' or not CHAIN.fullmatch(tokens[1]) or '!' in tokens:
        return anything
    values, narrowed, target, recent, index = {}, False, None, False, 2
    while index < len(tokens):
        option = tokens[index]
        argument = tokens[index + 1] if index + 1 < len(tokens) else None
        index += 2
        try:
            if option in ('--set', '--update', '--rsource'):
                index -= 1
            elif argument is None:
                narrowed = True
            elif option == '-p':
                values['proto'] = argument.lower()
            elif option in ('-s', '-d'):
                network = ipaddress.ip_network(argument, strict=False)
                if network.version != family:
                    raise ValueError(argument)
                if network.prefixlen:
                    values['source' if option == '-s' else 'destination'] = network
            elif option == '-i':
                values['interface'] = argument
            elif option in ('--dport', '--dports'):
                values['ports'] = _ports(argument)
            elif option == '-m':
                recent = recent or argument == 'recent'
                narrowed = narrowed or argument not in ('multiport', 'conntrack', 'comment', 'recent',
                                                         'tcp', 'udp')
            elif option == '--ctstate':
                narrowed = narrowed or argument != 'NEW'
            elif option in ('-j', '-g'):
                target = argument if option == '-j' else '?'
            elif option not in ('--comment', '--seconds', '--hitcount', '--name', '--reject-with',
                                '--log-prefix', '--log-level'):
                narrowed = True
                if argument.startswith('-'):
                    index -= 1
        except ValueError:
            narrowed = True
    target = re.sub(r'^ufw6-', 'ufw-', target or '')
    if target in ('', 'LOG', 'ufw-user-logging-input', 'ufw-logging-allow', 'ufw-logging-deny'):
        action = 'pass'
    elif target == 'ufw-user-limit':
        # `ufw limit`: reached only by the --update rule once the rate is exceeded.
        action = 'pass' if recent else 'deny'
    else:
        action = {'ACCEPT': 'accept', 'ufw-user-limit-accept': 'limit', 'DROP': 'deny',
                  'REJECT': 'deny'}.get(target, 'unknown')
    return Rule(family, line, action, narrowed=narrowed, **values)


def _assignments(path):
    path = Path(path)
    if not path.is_file():
        return None
    return {key: value.strip('"\'') for key, value in
            re.findall(r'^\s*([A-Z0-9_]+)\s*=\s*(\S*)', path.read_text(), re.M)}


def read_settings(root=UFW_ROOT, defaults=UFW_DEFAULTS):
    """Enabled state, default policies and IPv6 filtering, from the files ufw itself reads."""
    conf = default = None
    if root is not None:
        conf, default = _assignments(Path(root) / 'ufw.conf'), _assignments(defaults)
    return {'installed': conf is not None and default is not None,
            'enabled': (conf or {}).get('ENABLED', 'no').lower() == 'yes',
            'incoming': (default or {}).get('DEFAULT_INPUT_POLICY', 'DROP').upper(),
            'outgoing': (default or {}).get('DEFAULT_OUTPUT_POLICY', 'ACCEPT').upper(),
            # ufw loads user6.rules only with IPV6=yes; a host without ufw gets the default yes.
            'ipv6': (default or {}).get('IPV6', 'yes').lower() == 'yes'}


def rules_from_text(text, family):
    """Rules of the user input chain, in file order."""
    lines = (line.strip() for line in text.splitlines())
    return [parse_rule(line, family) for line in lines
            if re.match(r'-[A-Z] ' + CHAIN.pattern + r'(\s|$)', line)]


def read_rules(root=UFW_ROOT, ipv6=True):
    """Both rule files; a missing file means no rules. root=None: ufw was never installed."""
    rules = []
    for family, name in ((4, 'user.rules'), (6, 'user6.rules')):
        path = Path(root or '', name)
        if root is None or (family == 6 and not ipv6) or not (path.exists() or path.is_symlink()):
            continue
        rules += rules_from_text(path.read_text(), family)
    return rules


def parse_listeners(output, proto):
    """`ss -H -lntp` / `ss -H -lnup`: State Recv-Q Send-Q Local Peer [users]."""
    listeners = []
    for line in output.splitlines():
        fields = line.split()
        try:
            host, _, port = fields[3].rpartition(':')
            host = host.split('%', 1)[0].strip('[]')
            address = None if host == '*' else ipaddress.ip_address(host)
            # [::ffff:127.0.0.1] is the IPv4 address it wraps.
            address = getattr(address, 'ipv4_mapped', None) or address
            listeners.append(Listener(proto, address, int(port),
                                      tuple(dict.fromkeys(re.findall(r'\("([^"]*)"', line))), line))
        except (IndexError, ValueError) as error:
            raise InstallError('Не удалось разобрать список сетевых служб (ss): ' + line[:120]) from error
    return listeners


def ssh_client(value):
    """SSH_CONNECTION: client address, client port, server address, server port."""
    parts = (value or '').split()
    try:
        return Client(ipaddress.ip_address(parts[0]), ipaddress.ip_address(parts[2]), int(parts[3]))
    except (IndexError, ValueError):
        return None


def default_route_interfaces(route, route6=''):
    """Interfaces with a default route, from /proc/net/route and /proc/net/ipv6_route."""
    names = {fields[0] for fields in map(str.split, route.splitlines()[1:])
             if len(fields) > 7 and fields[1] == fields[7] == '00000000'}
    names |= {fields[9] for fields in map(str.split, route6.splitlines())
              if len(fields) > 9 and fields[0] == '0' * 32 and fields[1] == '00' and fields[9] != 'lo'}
    return names or None


def public_interfaces(proc=PROC_ROOT):
    """Interfaces that face the Internet; None (assume any) when the kernel tables are unreadable."""
    try:
        route = (Path(proc) / 'net/route').read_text()
    except OSError:
        return None
    try:
        route6 = (Path(proc) / 'net/ipv6_route').read_text()
    except OSError:
        route6 = ''
    return default_route_interfaces(route, route6)


def internal_ports(state):
    """TCP ports that must never be reachable from outside the host."""
    ports = {XRAY_API, XRAY_METRICS}
    if state.get('warp_mode', 'install') != 'skip':
        ports.add(WARP_SOCKS)
    ports.update(int(state[key]) for key in ('panel_port', 'sub_port') if state.get(key))
    return ports


def _within(network, address):
    return network is None or (address.version == network.version and address in network)


def _flow(rules, family, port, proto='tcp'):
    """First rule that decides `proto/port` for every source: (state, rule, restricted, blocked).

    state is accept, limit, deny, unknown or absent; restricted and blocked
    are the earlier allow and deny rules that apply to some sources only.
    """
    restricted, blocked = [], []
    for rule in rules:
        if rule.family != family or rule.action == 'pass' or not rule.covers(proto, port):
            continue
        allows = rule.action in ('accept', 'limit')
        if rule.everyone and rule.action != 'unknown':
            return rule.action, rule, restricted, blocked
        if rule.wide:
            if allows:
                continue  # an unmodelled match: the allow is not guaranteed
            return 'unknown', rule, restricted, blocked  # may deny everyone
        (restricted if allows else blocked).append(rule)
    return 'absent', None, restricted, blocked


def _client_flow(rules, port, client):
    """What the first matching rule does to the active SSH client: accept, deny or None."""
    for rule in rules:
        if rule.family != client.address.version or rule.action == 'pass' or \
                not rule.covers('tcp', port) or \
                not (_within(rule.source, client.address) and _within(rule.destination, client.server)):
            continue
        if rule.action in ('accept', 'limit'):
            if not rule.narrowed and rule.interface is None:
                return 'accept', rule
            continue  # may match: never a guarantee
        return 'deny', rule  # a deny or an unknown rule that may match is treated as a deny
    return None, None


def _public(rule, interfaces):
    """An allow rule that the public Internet can use (upper bound)."""
    if rule.source is not None and rule.source.prefixlen >= (8 if rule.family == 4 else 16):
        return False
    if rule.interface is None:
        return True
    return rule.interface != 'lo' and (interfaces is None or rule.interface in interfaces)


def _exposing(rules, family, port, interfaces):
    """(rule that opens TCP `port` to the public or None, allow rules limited to trusted sources)."""
    trusted = []
    for rule in rules:
        if rule.family != family or rule.action == 'pass' or not rule.covers('tcp', port):
            continue
        if rule.action == 'deny':
            if rule.everyone:
                break
            continue
        if _public(rule, interfaces):
            return rule, trusted
        trusted.append(rule)
    return None, trusted


def _quote(rule):
    return f'{rule.describe()} (IPv{rule.family}: `{rule.line}`)'


def assess(rules, settings, ssh_ports, internal, client=None, interfaces=None, listeners=()):
    """Classify existing rules and listeners against what Bob needs; nothing is changed."""
    plan = Plan(settings['enabled'], settings['incoming'], settings['outgoing'], rules=list(rules),
                interfaces=interfaces)
    families = [4] + ([6] if settings['ipv6'] else [])
    ssh_ports = sorted({int(port) for port in ssh_ports})
    kept, conflicts, consent, cutoff, judged, refused = {}, {}, {}, {}, set(), set()

    def conflict(rule, why, fix=HOW_TO_DELETE):
        judged.add(rule)
        refused.add(rule)
        conflicts.setdefault((_quote(rule) + ' — ' + why, fix))

    def keep(rule, why):
        judged.add(rule)
        kept.setdefault(f'{rule.describe()} — {why}')

    for port in ssh_ports + [p for p in PUBLIC_TCP if p not in ssh_ports]:
        ssh = port in ssh_ports
        name = f'SSH-порт {port}' if ssh else f'TCP/{port}'
        states, limited, narrowed_allow = {}, [], []
        for family in families:
            state, rule, restricted, blocked = _flow(rules, family, port)
            states[family] = state
            if state == 'unknown':
                conflict(rule, f'правило не поддаётся надёжной оценке и может блокировать {name}')
            elif state == 'deny':
                conflict(rule, f'запрещает входящие подключения к {name} для всех адресов; '
                               'разрешение Bob было бы добавлено после него и не сработало бы')
            elif state == 'limit' and not ssh:
                if family == 4:
                    conflict(rule, f'ограничивает частоту подключений к {name} (6 за 30 секунд с адреса): '
                                   'VPN-клиенты и проверка Let’s Encrypt открывают больше соединений')
                else:
                    limited.append(rule)
            elif state in ('accept', 'limit'):
                keep(rule, f'{name} уже открыт' + (' с ограничением частоты' if state == 'limit' else ''))
                if state == 'limit':
                    limited.append(rule)
            narrowed_allow += restricted
            for rule in blocked:
                keep(rule, f'закрывает {name} для части адресов; правило сохранено')
        covered = None
        if ssh and client is not None and client.port == port:
            covered, rule = _client_flow(rules, port, client)
            if covered == 'deny' and rule not in refused:
                text = f'может блокировать текущее SSH-подключение с адреса {client.address}'
                if settings['enabled']:
                    keep(rule, text + '; UFW уже включён, Bob это правило не меняет')
                else:
                    conflict(rule, text + ': UFW сейчас выключен, после его включения новые '
                                          'SSH-подключения с этого адреса будут отклонены')
        # IPv4 carries every published service; IPv6 matters only to an IPv6 SSH session.
        needed = {4} | ({6} if ssh and client is not None and client.address.version == 6 and
                        6 in families else set())
        if all(states[family] in ('accept', 'limit') for family in needed):
            continue
        if any(states[family] in ('unknown', 'deny') for family in families) or \
                (not ssh and states[4] == 'limit'):
            continue
        if ssh and narrowed_allow and covered != 'deny':
            sources = ', '.join(dict.fromkeys(str(rule.source or rule.interface or 'условие')
                                              for rule in narrowed_allow))
            if client is None or client.port != port or covered == 'accept':
                for rule in narrowed_allow:
                    keep(rule, f'{name} разрешён только для: {sources}; Bob не расширяет доступ')
                continue
            consent.setdefault(
                f'{name} разрешён только для: {sources}, а текущее подключение идёт с адреса '
                f'{client.address}, который под эти правила не подпадает. Bob добавит '
                f'`ufw allow {port}/tcp`: SSH станет доступен с любого адреса.')
        plan.allow.append(port)
        if all(states[family] == 'absent' for family in families) and not narrowed_allow:
            plan.removable.append(port)
        for rule in limited:
            consent.setdefault(
                f'Правило {_quote(rule)} будет заменено на allow: ufw объединяет правила с '
                f'одинаковым портом, а Bob добавляет `ufw allow {port}/tcp`.')
        plan.changes.append(f'добавит правило `ufw allow {port}/tcp` ({name})')

    exposed = {}
    for family in families:
        for port in sorted(internal):
            rule, trusted = _exposing(rules, family, port, interfaces)
            if rule is not None:
                exposed.setdefault(rule, []).append(port)
            for rule in trusted:
                keep(rule, 'доверенный источник или интерфейс; внутренние службы Bob слушают только '
                           '127.0.0.1')
    for rule, ports in exposed.items():
        ports = ', '.join(map(str, sorted(set(ports))))
        why = ('не поддаётся надёжной оценке и может открывать' if rule.action == 'unknown' else
               'открывает из интернета')
        conflict(rule, f'{why} внутренние порты Bob (панель, подписки, Xray API/метрики, SOCKS5 WARP): '
                       f'{ports}',
                 f'сузьте правило до нужных портов или адресов ({HOW_TO_DELETE}) либо закройте '
                 'внутренние порты раньше него: ' +
                 '; '.join(f'`ufw prepend deny {port}/tcp`' for port in ports.split(', ')))

    for rule in rules:
        if rule in judged or rule.action == 'pass':
            continue
        if rule.action == 'unknown' or rule.narrowed:
            keep(rule, 'оценено по известной части: порты Bob не затрагивает')
        else:
            keep(rule, 'не затрагивает порты Bob; правило сохранено')

    if not settings['enabled']:
        plan.changes.append('включит UFW (сейчас выключен' +
                            ('' if settings['installed'] else ' или не установлен') + ')')
    if settings['incoming'] == 'ACCEPT':
        plan.changes.append('переключит политику входящих соединений UFW с allow на deny')
    if settings['outgoing'] != 'ACCEPT':
        consent.setdefault(
            f'Политика исходящих соединений UFW сейчас {settings["outgoing"]}. Bob переключит её на '
            'allow: VPN-серверу нужны исходящие соединения к любым адресам.')
    if not settings['enabled'] or settings['incoming'] == 'ACCEPT':
        # Today nothing filters these services; default deny will.
        mine = set(ssh_ports) | set(PUBLIC_TCP)
        for listener in sorted(listeners, key=lambda item: (item.proto, item.port)):
            if not listener.public or (listener.proto == 'tcp' and listener.port in mine) or \
                    (listener.proto == 'udp' and listener.port in UFW_BUILTIN_UDP):
                continue
            family = 6 if listener.address is not None and listener.address.version == 6 else 4
            if any(rule.family == family and rule.action in ('accept', 'limit') and
                   rule.covers(listener.proto, listener.port) for rule in rules):
                continue
            if 'docker-proxy' in listener.processes:
                kept.setdefault(f'{listener.describe()} — порт опубликован Docker; UFW его не фильтрует')
                continue
            cutoff.setdefault(
                f'Служба {listener.describe()} принимает подключения извне и не разрешена в UFW. '
                'После включения UFW с политикой deny входящие подключения к ней будут отклонены. '
                f'Чтобы сохранить доступ, заранее выполните `ufw allow {listener.port}/{listener.proto}`.')
    plan.kept, plan.conflicts, plan.consent = list(kept), list(conflicts), list(consent)
    plan.cutoff = list(cutoff)
    return plan


def survey(ssh_ports, internal, client=None, listeners=(), root=UFW_ROOT, defaults=UFW_DEFAULTS,
           proc=PROC_ROOT):
    """Read the UFW files and assess them. Read-only."""
    settings = read_settings(root, defaults)
    try:
        rules = read_rules(root, settings['ipv6'])
    except OSError as error:
        raise InstallError(f'Правила UFW не читаются ({error}); проверьте /etc/ufw/user.rules и '
                           '/etc/ufw/user6.rules') from error
    return assess(rules, settings, ssh_ports, internal, client, public_interfaces(proc), listeners)


def exposing(rules, port, interfaces=None):
    """Rule that leaves TCP `port` open to the public, or None."""
    for family in (4, 6):
        rule = _exposing(rules, family, int(port), interfaces)[0]
        if rule is not None:
            return rule
    return None


def names_port(text, port):
    """True when a rule that is not a deny names `port` as a destination port."""
    return any(re.search(r'--dports?\s+(?:\S*[,:])?' + str(int(port)) + r'\b', line) and
               not re.search(r'-j\s+(?:DROP|REJECT)\b', line) for line in text.splitlines())


def require_private(port, message, root=UFW_ROOT, proc=PROC_ROOT):
    """Acceptance: no UFW rule names an internal port or leaves it open to the public."""
    rules = []
    for family, name in ((4, 'user.rules'), (6, 'user6.rules')):
        text = Path(root, name).read_text()
        if names_port(text, port):
            raise InstallError(message)
        rules += rules_from_text(text, family)
    if exposing(rules, port, public_interfaces(proc)) is not None:
        raise InstallError(message)


def published_ports(state):
    """TCP ports that may listen on a routable address: SSH and services that predate Bob."""
    return sorted({*map(int, state['ssh_ports']), *map(int, state.get('foreign_tcp_ports', ()))})
