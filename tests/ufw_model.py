"""Disposable model of the files and commands of ufw that the installer uses.

Rule lines have the format ufw 0.36 writes into user.rules/user6.rules,
including the ufw6- chain prefix of the IPv6 file and the in-place replacement
of a rule with the same port and a different action ("Rule updated").
"""
from pathlib import Path
import re

from selfsni.core import CommandError


def accept(port, proto='tcp', family=4):
    chain = 'ufw-user-input' if family == 4 else 'ufw6-user-input'
    return f'-A {chain} -p {proto} --dport {port} -j ACCEPT'


def limit(port, family=4):
    prefix = 'ufw' if family == 4 else 'ufw6'
    head = f'-A {prefix}-user-input -p tcp --dport {port}'
    return [f'{head} -m conntrack --ctstate NEW -m recent --set',
            f'{head} -m conntrack --ctstate NEW -m recent --update --seconds 30 --hitcount 6 '
            f'-j {prefix}-user-limit',
            f'{head} -j {prefix}-user-limit-accept']


class FakeUfw:
    def __init__(self, root, installed=True, enabled=False, incoming='DROP', outgoing='ACCEPT',
                 ipv6='yes'):
        self.root = Path(root)
        self.directory = self.root / 'etc/ufw'
        self.defaults = self.root / 'etc/default/ufw'
        self.tcp, self.udp, self.calls, self.fail = [], [], [], set()
        self.honest_enable = True
        if installed:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.defaults.parent.mkdir(parents=True, exist_ok=True)
            self.write_settings(enabled, incoming, outgoing, ipv6)
            self.rules(4, [])
            self.rules(6, [])

    def write_settings(self, enabled=None, incoming=None, outgoing=None, ipv6=None):
        current = self.settings() if self.defaults.exists() else {}
        enabled = current.get('enabled', False) if enabled is None else enabled
        incoming = incoming or current.get('incoming', 'DROP')
        outgoing = outgoing or current.get('outgoing', 'ACCEPT')
        ipv6 = ipv6 or current.get('ipv6', 'yes')
        (self.directory / 'ufw.conf').write_text(
            '# /etc/ufw/ufw.conf\nENABLED=' + ('yes' if enabled else 'no') + '\nLOGLEVEL=low\n')
        self.defaults.write_text(f'IPV6={ipv6}\nDEFAULT_INPUT_POLICY="{incoming}"\n'
                                 f'DEFAULT_OUTPUT_POLICY="{outgoing}"\nDEFAULT_FORWARD_POLICY="DROP"\n')

    def settings(self):
        conf = (self.directory / 'ufw.conf').read_text()
        default = self.defaults.read_text()
        return {'enabled': 'ENABLED=yes' in conf,
                'incoming': re.search(r'INPUT_POLICY="(\w+)"', default).group(1),
                'outgoing': re.search(r'OUTPUT_POLICY="(\w+)"', default).group(1),
                'ipv6': re.search(r'IPV6=(\w+)', default).group(1)}

    def path(self, family):
        return self.directory / ('user.rules' if family == 4 else 'user6.rules')

    def rules(self, family, lines=None):
        if lines is not None:
            self.path(family).write_text('*filter\n### RULES ###\n' + ''.join(x + '\n' for x in lines) +
                                         '### END RULES ###\nCOMMIT\n')
        return [line for line in self.path(family).read_text().splitlines() if line.startswith('-A ')]

    def add(self, *lines, family=4):
        self.rules(family, self.rules(family) + list(lines))

    def command(self, *parts, **_):
        """Stand-in for core.run: ss and ufw only."""
        if parts[:3] == ('ss', '-H', '-lntp'):
            return '\n'.join(self.tcp)
        if parts[:3] == ('ss', '-H', '-lnup'):
            return '\n'.join(self.udp)
        if parts[0] != 'ufw':
            raise AssertionError(parts)
        self.calls.append(parts)
        if parts[1:] in self.fail or parts[1] in self.fail:
            raise CommandError('ufw', 1, 'simulated failure')
        action = parts[1]
        if action in ('allow', 'delete'):
            port, proto = parts[3 if action == 'delete' else 2].split('/')
            for family in (4, 6) if self.settings()['ipv6'] == 'yes' else (4,):
                rule = accept(port, proto, family)
                head = rule.rsplit(' -j ', 1)[0]
                lines = self.rules(family)
                if action == 'delete':
                    lines = [line for line in lines if line != rule]
                elif rule not in lines:
                    # Same tuple only: a rule with a source or an interface is another rule.
                    same = [i for i, line in enumerate(lines)
                            if line.startswith((head + ' -j ', head + ' -m conntrack '))]
                    if same:  # "Rule updated": same port, another action
                        lines[same[0]:same[-1] + 1] = [rule]
                    else:
                        lines.append(rule)
                self.rules(family, lines)
        elif action == 'default':
            policy = {'deny': 'DROP', 'allow': 'ACCEPT', 'reject': 'REJECT'}[parts[2]]
            self.write_settings(**{parts[3]: policy})
        elif parts[1:] == ('--force', 'enable'):
            self.write_settings(enabled=self.honest_enable)
        elif action == 'disable':
            self.write_settings(enabled=False)
        else:
            raise AssertionError(parts)
        return ''

    def ufw_calls(self, verb=None):
        return [call[1:] for call in self.calls if verb is None or call[1] == verb]
