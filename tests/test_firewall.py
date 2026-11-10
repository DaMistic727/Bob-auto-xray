"""UFW assessment by effect, and the idempotent, reversible FIREWALL stage."""
import ipaddress
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selfsni import firewall, provision
from selfsni.core import InstallError

from ufw_model import FakeUfw, accept, limit

INTERNAL = {62789, 11111, 40000, 21000, 22000}
ROUTE = ('Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n'
         'ens3\t00000000\t0102A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n'
         'ens3\t0002A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n')


def settings(**changes):
    return dict(dict(installed=True, enabled=True, incoming='DROP', outgoing='ACCEPT', ipv6=True),
                **changes)


def rules(v4=(), v6=()):
    return (firewall.rules_from_text('\n'.join(v4), 4) + firewall.rules_from_text('\n'.join(v6), 6))


def assess(v4=(), v6=(), ssh=(22,), client=None, interfaces=frozenset({'ens3'}), listeners=(),
           internal=INTERNAL, **changes):
    return firewall.assess(rules(v4, v6), settings(**changes), ssh, internal,
                           firewall.ssh_client(client), interfaces, listeners)


def tcp(address, name='app'):
    return firewall.parse_listeners(f'LISTEN 0 128 {address} 0.0.0.0:* users:(("{name}",pid=7,fd=3))', 'tcp')


def udp(address, name=None):
    users = f' users:(("{name}",pid=7,fd=3))' if name else ''
    return firewall.parse_listeners(f'UNCONN 0 0 {address} 0.0.0.0:*{users}', 'udp')


class RuleFormatTests(unittest.TestCase):
    """Every spelling ufw itself produces for an ordinary rule is understood."""

    def test_formats_written_by_ufw(self):
        cases = {
            accept(22): ('accept', 'tcp', ((22, 22),)),
            accept(22, 'udp'): ('accept', 'udp', ((22, 22),)),
            "-A ufw-user-input -p tcp --dport 22 -j ACCEPT -m comment --comment 'dapp_OpenSSH'":
                ('accept', 'tcp', ((22, 22),)),
            "-A ufw-user-input -p tcp -m multiport --dports 80,443 -j ACCEPT -m comment --comment "
            "'dapp_Nginx%20Full'": ('accept', 'tcp', ((80, 80), (443, 443))),
            '-A ufw-user-input -p udp -m multiport --dports 6000:6007 -j ACCEPT':
                ('accept', 'udp', ((6000, 6007),)),
            '-A ufw-user-input -p tcp --dport 25 -j DROP': ('deny', 'tcp', ((25, 25),)),
            '-A ufw-user-input -p tcp --dport 25 -j REJECT --reject-with tcp-reset':
                ('deny', 'tcp', ((25, 25),)),
            '-A ufw-user-input -p tcp --dport 22 -j ufw-user-logging-input': ('pass', 'tcp', ((22, 22),)),
            '-A ufw-user-input -j ACCEPT': ('accept', None, None),
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                rule = firewall.parse_rule(line, 4)
                self.assertEqual((rule.action, rule.proto, rule.ports), expected)
                self.assertFalse(rule.narrowed)

    def test_limit_is_a_rate_limited_allow(self):
        parsed = rules(limit(22))
        self.assertEqual([rule.action for rule in parsed], ['pass', 'pass', 'limit'])
        self.assertFalse(any(rule.narrowed for rule in parsed))

    def test_source_destination_and_interface(self):
        rule = firewall.parse_rule('-A ufw-user-input -i eth0 -p tcp -d 192.0.2.5 --dport 22 '
                                   '-s 203.0.113.0/24 -j ACCEPT', 4)
        self.assertEqual((rule.interface, str(rule.source), str(rule.destination)),
                         ('eth0', '203.0.113.0/24', '192.0.2.5/32'))
        self.assertFalse(rule.everyone)
        # "from any" is not a restriction.
        self.assertTrue(firewall.parse_rule('-A ufw-user-input -p tcp --dport 22 -s 0.0.0.0/0 -j ACCEPT',
                                            4).everyone)

    def test_ipv6_file_uses_ufw6_chains(self):
        parsed = rules(v6=[accept(22, family=6), *limit(2222, family=6),
                           '-A ufw6-user-input -p tcp --dport 443 -s 2001:db8::/32 -j ACCEPT'])
        self.assertEqual([(rule.family, rule.action) for rule in parsed],
                         [(6, 'accept'), (6, 'pass'), (6, 'pass'), (6, 'limit'), (6, 'accept')])
        self.assertEqual(str(parsed[-1].source), '2001:db8::/32')

    def test_other_chains_are_not_input_rules(self):
        text = ('*filter\n:ufw-user-input - [0:0]\n-A ufw-user-output -p tcp --dport 25 -j DROP\n'
                '-A ufw-user-forward -j ACCEPT\n-A ufw-user-logging-input -p tcp --dport 22 -j RETURN\n'
                '-A ufw-user-limit -j REJECT\n-A ufw-user-limit-accept -j ACCEPT\nCOMMIT\n')
        self.assertEqual(firewall.rules_from_text(text, 4), [])

    def test_unmodelled_rule_is_an_upper_bound_never_a_guarantee(self):
        for line in ('-A ufw-user-input -p tcp --dport 22 -m state --state NEW -j ACCEPT',
                     '-A ufw-user-input -p tcp --dport 22 --sport 1024:65535 -j ACCEPT',
                     '-A ufw-user-input -p tcp --dport 22 -m conntrack --ctstate ESTABLISHED -j ACCEPT',
                     '-A ufw-user-input -p tcp ! --dport 22 -j ACCEPT',
                     '-I ufw-user-input -p tcp --dport 22 -j ACCEPT',
                     "-A ufw-user-input -p tcp --dport 22 -j ACCEPT -m comment --comment 'broken"):
            with self.subTest(line=line):
                self.assertTrue(firewall.parse_rule(line, 4).narrowed)
        self.assertEqual(firewall.parse_rule('-A ufw-user-input -p tcp --dport 22 -j MYCHAIN', 4).action,
                         'unknown')

    def test_listener_formats(self):
        sample = ('LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
                  'LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=2,fd=15))\n'
                  'LISTEN 0 511 [::]:80 [::]:* users:(("nginx",pid=4,fd=6),("nginx",pid=3,fd=6))\n'
                  'LISTEN 0 128 [::1]:6010 [::]:*\nLISTEN 0 4096 *:8080 *:*\n'
                  'LISTEN 0 128 [fe80::1]%ens3:546 [::]:*\n')
        parsed = firewall.parse_listeners(sample, 'tcp')
        self.assertEqual([(x.port, x.public) for x in parsed],
                         [(22, True), (53, False), (80, True), (6010, False), (8080, True), (546, False)])
        self.assertEqual(parsed[2].processes, ('nginx',))
        self.assertIsNone(parsed[4].address)
        mapped = firewall.parse_listeners('LISTEN 0 50 [::ffff:127.0.0.1]:9200 *:*', 'tcp')[0]
        self.assertFalse(mapped.public)
        with self.assertRaisesRegex(InstallError, 'ss'):
            firewall.parse_listeners('LISTEN 0 128\n', 'tcp')


class CompatibleRulesTests(unittest.TestCase):
    def test_clean_host_gets_ssh_http_and_reality(self):
        for ufw_ipv6 in (True, False):
            with self.subTest(ipv6=ufw_ipv6):
                plan = assess(enabled=False, installed=False, ipv6=ufw_ipv6)
                self.assertEqual((plan.allow, plan.removable), ([22, 80, 443], [22, 80, 443]))
                self.assertEqual((plan.conflicts, plan.consent, plan.kept), ([], [], []))

    def test_what_bob_itself_wrote_needs_nothing_more(self):
        v4 = [accept(22), accept(80), accept(443)]
        plan = assess(v4, [accept(port, family=6) for port in (22, 80, 443)])
        self.assertEqual((plan.allow, plan.conflicts, plan.consent, plan.changes), ([], [], [], []))

    def test_ordinary_operator_rules_are_kept(self):
        v4 = [accept(22), accept(22, 'udp'),                      # ufw allow 22
              "-A ufw-user-input -p tcp --dport 22 -j ACCEPT -m comment --comment 'dapp_OpenSSH'",
              '-A ufw-user-input -p tcp -m multiport --dports 80,443 -j ACCEPT',
              accept(8080), accept(51820, 'udp'),                 # other services of the operator
              '-A ufw-user-input -p tcp --dport 25 -j DROP',      # an extra deny
              '-A ufw-user-input -s 198.51.100.7 -j DROP',        # a banned address
              '-A ufw-user-input -s 203.0.113.9 -j ACCEPT',       # a trusted address
              '-A ufw-user-input -i tailscale0 -j ACCEPT',        # a private interface
              '-A ufw-user-input -p udp -m multiport --dports 10000:20000 -j ACCEPT']
        plan = assess(v4)
        self.assertEqual((plan.conflicts, plan.consent, plan.allow), ([], [], []))
        kept = '\n'.join(plan.kept)
        for fragment in ('tcp 8080', 'udp 51820', 'DENY tcp 25', 'от 198.51.100.7/32',
                         'от 203.0.113.9/32', 'tailscale0', 'udp 10000:20000'):
            self.assertIn(fragment, kept)

    def test_limit_on_ssh_is_kept_and_not_replaced(self):
        plan = assess(limit(22) + [accept(80), accept(443)], limit(22, 6))
        self.assertEqual((plan.allow, plan.conflicts, plan.consent), ([], [], []))
        self.assertIn('с ограничением частоты', '\n'.join(plan.kept))

    def test_only_missing_ports_are_added(self):
        plan = assess([accept(2222), accept(443)], [accept(2222, family=6)], ssh=(2222,))
        self.assertEqual(plan.allow, [80])

    def test_ipv6_rules_are_ignored_when_ufw_does_not_load_them(self):
        blocking = ['-A ufw6-user-input -p tcp --dport 443 -j DROP']
        self.assertTrue(assess([accept(22), accept(80), accept(443)], blocking).conflicts)
        text_v6 = '\n'.join(blocking)
        with tempfile.TemporaryDirectory() as directory:
            ufw = FakeUfw(directory, enabled=True, ipv6='no')
            ufw.add(accept(22), accept(80), accept(443))
            ufw.path(6).write_text(text_v6 + '\n')
            plan = firewall.survey([22], INTERNAL, root=ufw.directory, defaults=ufw.defaults,
                                   proc=Path(directory) / 'proc')
        self.assertEqual((plan.conflicts, plan.allow), ([], []))

    def test_earlier_rule_wins(self):
        # Allow before deny: the port is open. Deny before a wide allow: the port stays closed.
        self.assertEqual(assess([accept(22), accept(80), accept(443),
                                 '-A ufw-user-input -p tcp --dport 443 -j DROP']).conflicts, [])
        closed = ['-A ufw-user-input -p tcp --dport 40000 -j DROP',
                  '-A ufw-user-input -p tcp --dport 62789 -j REJECT',
                  '-A ufw-user-input -p tcp -m multiport --dports 11111,21000,22000 -j DROP',
                  '-A ufw-user-input -p tcp -m multiport --dports 10000:65000 -j ACCEPT']
        self.assertEqual(assess(closed).conflicts, [])


class ConflictingRulesTests(unittest.TestCase):
    def conflicts(self, *lines, **options):
        plan = assess([*lines], **options)
        with self.assertRaises(InstallError) as refusal:
            plan.refuse()
        return str(refusal.exception)

    def test_deny_of_a_required_port(self):
        for port, name in ((443, 'TCP/443'), (80, 'TCP/80'), (22, 'SSH-порт 22')):
            with self.subTest(port=port):
                text = self.conflicts(f'-A ufw-user-input -p tcp --dport {port} -j DROP')
                self.assertIn(f'запрещает входящие подключения к {name}', text)
                # The operator sees the rule itself and how to remove it.
                self.assertIn(f'--dport {port} -j DROP', text)
                self.assertIn('ufw delete <номер>', text)
                self.assertIn('Bob не изменяет и не удаляет чужие правила', text)
        self.assertIn('TCP/443', self.conflicts('-A ufw-user-input -j DROP'))
        self.assertIn('TCP/80', self.conflicts('-A ufw-user-input -p tcp -m multiport --dports 1:1024 '
                                               '-j REJECT'))

    def test_rate_limit_on_a_vpn_port(self):
        self.assertIn('ограничивает частоту подключений к TCP/443', self.conflicts(*limit(443)))

    def test_public_allow_that_covers_internal_ports(self):
        for line, ports in (('-A ufw-user-input -j ACCEPT', '11111, 21000, 22000, 40000, 62789'),
                            ('-A ufw-user-input -p tcp -m multiport --dports 20000:60000 -j ACCEPT',
                             '21000, 22000, 40000'),
                            (accept(21000), '21000'),
                            ('-A ufw-user-input -i ens3 -j ACCEPT', '11111'),
                            ('-A ufw-user-input -s 0.0.0.0/1 -j ACCEPT', '62789')):
            with self.subTest(line=line):
                text = self.conflicts(line)
                self.assertIn('открывает из интернета внутренние порты Bob', text)
                self.assertIn(ports, text)
                self.assertIn('ufw prepend deny', text)

    def test_interface_rule_is_public_when_the_public_interface_is_unknown(self):
        line = '-A ufw-user-input -i tailscale0 -j ACCEPT'
        self.assertEqual(assess([line]).conflicts, [])
        self.assertTrue(assess([line], interfaces=None).conflicts)
        self.assertEqual(assess(['-A ufw-user-input -i lo -j ACCEPT'], interfaces=None).conflicts, [])

    def test_rule_that_cannot_be_assessed(self):
        for line in ('-A ufw-user-input -p tcp --dport 443 -m state --state NEW -j DROP',
                     '-I ufw-user-input -p tcp --dport 9 -j DROP',
                     '-A ufw-user-input -p tcp ! --dport 9 -j DROP',
                     '-A ufw-user-input -p tcp --dport 22 -j MYCHAIN'):
            with self.subTest(line=line):
                self.assertIn('не поддаётся надёжной оценке', self.conflicts(line))
        # The same unknown match on a port Bob does not use is harmless.
        plan = assess(['-A ufw-user-input -p tcp --dport 25 -m state --state NEW -j DROP',
                       '-A ufw-user-input -p udp --dport 53 -j MYCHAIN'])
        self.assertEqual(plan.conflicts, [])
        self.assertIn('оценено по известной части', '\n'.join(plan.kept))

    def test_internal_port_in_ipv6_rules_only(self):
        plan = assess(v6=['-A ufw6-user-input -p tcp --dport 22000 -j ACCEPT'])
        self.assertIn('22000', plan.conflicts[0][0])
        self.assertIn('IPv6', plan.conflicts[0][0])

    def test_warp_port_is_not_internal_without_warp(self):
        self.assertNotIn(40000, firewall.internal_ports({'warp_mode': 'skip'}))
        self.assertEqual(firewall.internal_ports({'warp_mode': 'install', 'panel_port': 21000,
                                                  'sub_port': '22000'}), INTERNAL)


class SshAccessTests(unittest.TestCase):
    CLIENT = '203.0.113.9 50000 192.0.2.1 2222'

    def test_non_standard_port_is_published(self):
        plan = assess(ssh=(2222,), enabled=False)
        self.assertEqual(plan.allow, [2222, 80, 443])
        plan = assess([accept(22)], ssh=(22, 2222))
        self.assertEqual(plan.allow, [2222, 80, 443])

    def test_source_restricted_ssh_is_not_widened(self):
        restricted = ['-A ufw-user-input -p tcp --dport 2222 -s 203.0.113.0/24 -j ACCEPT']
        for client in (self.CLIENT, None):  # None: installation from the provider's console
            with self.subTest(client=client):
                plan = assess(restricted, ssh=(2222,), client=client)
                self.assertEqual((plan.allow, plan.consent, plan.conflicts), ([80, 443], [], []))
                self.assertIn('Bob не расширяет доступ', '\n'.join(plan.kept))

    def test_session_outside_the_restriction_needs_consent(self):
        restricted = ['-A ufw-user-input -p tcp --dport 2222 -s 198.51.100.0/24 -j ACCEPT']
        plan = assess(restricted, ssh=(2222,), client=self.CLIENT, enabled=False)
        self.assertEqual(plan.conflicts, [])
        self.assertIn(2222, plan.allow)
        self.assertNotIn(2222, plan.removable)
        self.assertIn('203.0.113.9', plan.consent[0])
        self.assertIn('ufw allow 2222/tcp', plan.consent[0])

    def test_deny_that_hits_the_active_session(self):
        lines = ['-A ufw-user-input -s 203.0.113.0/24 -j DROP']
        inactive = assess(lines, ssh=(2222,), client=self.CLIENT, enabled=False)
        self.assertIn('текущее SSH-подключение с адреса 203.0.113.9', inactive.conflicts[0][0])
        # With UFW already enabled the rule is the operator's working policy; nothing changes.
        active = assess(lines, ssh=(2222,), client=self.CLIENT)
        self.assertEqual(active.conflicts, [])
        # Another address is not the session.
        other = assess(['-A ufw-user-input -s 198.51.100.7 -j DROP'], ssh=(2222,), client=self.CLIENT,
                       enabled=False)
        self.assertEqual(other.conflicts, [])

    def test_ipv6_session_needs_the_ipv6_rule(self):
        client = '2001:db8::9 50000 2001:db8::1 22'
        plan = assess([accept(22), accept(80), accept(443)], client=client)
        self.assertEqual(plan.allow, [22])
        self.assertEqual(assess([accept(22), accept(80), accept(443)], [accept(22, family=6)],
                                client=client).allow, [])

    def test_replacing_a_limit_needs_consent(self):
        # IPv4 has no SSH rule, IPv6 has a limit: `ufw allow 22/tcp` would replace the limit.
        plan = assess(v6=limit(22, 6), client='2001:db8::9 50000 2001:db8::1 22')
        self.assertIn(22, plan.allow)
        self.assertTrue(any('будет заменено на allow' in item for item in plan.consent))
        self.assertNotIn(22, plan.removable)

    def test_malformed_ssh_connection_is_no_client(self):
        for value in ('', 'x 1 y 22', '203.0.113.9 1 192.0.2.1', None):
            self.assertIsNone(firewall.ssh_client(value))
        self.assertEqual(firewall.ssh_client(self.CLIENT).address, ipaddress.ip_address('203.0.113.9'))


class PolicyChangeTests(unittest.TestCase):
    def test_services_that_default_deny_would_cut_off(self):
        listeners = (tcp('0.0.0.0:8080', 'node') + tcp('127.0.0.1:5432', 'postgres') +
                     tcp('[::1]:6379', 'redis') + tcp('0.0.0.0:22', 'sshd') + tcp('*:443', 'old') +
                     udp('0.0.0.0:51820') + udp('192.0.2.1%ens3:68', 'systemd-network') +
                     udp('127.0.0.53%lo:53', 'systemd-resolve') + tcp('0.0.0.0:9000', 'docker-proxy'))
        for state in (dict(enabled=False), dict(enabled=True, incoming='ACCEPT')):
            with self.subTest(**state):
                plan = assess(listeners=listeners, **state)
                self.assertEqual((len(plan.cutoff), plan.consent), (2, []))
                self.assertIn('TCP/8080 (node', plan.cutoff[0])
                self.assertIn('ufw allow 8080/tcp', plan.cutoff[0])
                self.assertIn('ufw allow 51820/udp', plan.cutoff[1])
                self.assertIn('Docker', '\n'.join(plan.kept))
                self.assertEqual(plan.conflicts, [])

    def test_no_consent_when_nothing_changes_for_the_service(self):
        listeners = tcp('0.0.0.0:8080', 'node') + udp('0.0.0.0:51820')
        # Already allowed by the operator.
        self.assertEqual(assess([accept(8080), accept(51820, 'udp')], listeners=listeners,
                                enabled=False).cutoff, [])
        # Already filtered by an enabled default-deny UFW.
        self.assertEqual(assess(listeners=listeners).cutoff, [])

    def test_outgoing_policy_change_needs_consent(self):
        for policy in ('DROP', 'REJECT'):
            plan = assess(outgoing=policy)
            self.assertIn(f'исходящих соединений UFW сейчас {policy}', plan.consent[0])
        self.assertEqual(assess().consent, [])

    def test_changes_are_announced(self):
        plan = assess(enabled=False, incoming='ACCEPT')
        text = '\n'.join(plan.changes)
        for fragment in ('ufw allow 22/tcp', 'ufw allow 443/tcp', 'включит UFW', 'с allow на deny'):
            self.assertIn(fragment, text)


class InternalPortSecrecyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ufw = FakeUfw(self.root, enabled=True)
        (self.root / 'proc/net').mkdir(parents=True)
        (self.root / 'proc/net/route').write_text(ROUTE)

    def private(self, port=21000):
        firewall.require_private(port, 'opened', self.ufw.directory, self.root / 'proc')

    def test_bob_rules_keep_internal_ports_private(self):
        self.ufw.add(accept(22), accept(80), accept(443), accept(8080))
        self.ufw.add(accept(22, family=6), family=6)
        for port in INTERNAL:
            self.private(port)

    def test_any_rule_that_names_or_covers_the_port_fails(self):
        for family, line in ((4, accept(21000)), (6, accept(21000, family=6)),
                             (4, '-A ufw-user-input -p tcp -m multiport --dports 80,21000 -j ACCEPT'),
                             (4, '-A ufw-user-input -p tcp -m multiport --dports 20000:22000 -j ACCEPT'),
                             (4, '-A ufw-user-input -j ACCEPT'),
                             (6, '-A ufw6-user-input -i ens3 -j ACCEPT'),
                             (4, '-A ufw-user-input -p tcp --dport 21000 -s 203.0.113.9 -j ACCEPT')):
            with self.subTest(line=line):
                self.ufw.rules(4, [])
                self.ufw.rules(6, [])
                self.ufw.add(line, family=family)
                with self.assertRaisesRegex(InstallError, 'opened'):
                    self.private()

    def test_deny_and_trusted_rules_do_not_fail(self):
        self.ufw.add('-A ufw-user-input -p tcp --dport 21000 -j DROP',
                     '-A ufw-user-input -p tcp -m multiport --dports 20000:22000 -j ACCEPT',
                     '-A ufw-user-input -i tailscale0 -j ACCEPT', '-A ufw-user-input -s 10.8.0.0/24 -j ACCEPT')
        self.private()

    def test_missing_rule_file_fails_closed(self):
        self.ufw.path(6).unlink()
        with self.assertRaises(OSError):
            self.private()

    def test_default_route_interfaces(self):
        self.assertEqual(firewall.public_interfaces(self.root / 'proc'), {'ens3'})
        self.assertIsNone(firewall.public_interfaces(self.root / 'missing'))
        route6 = ('00000000000000000000000000000000 00 00000000000000000000000000000000 00 '
                  'fe800000000000000000000000000001 00000400 00000001 00000000 00000003 eth1\n'
                  '00000000000000000000000000000000 00 00000000000000000000000000000000 00 '
                  '00000000000000000000000000000000 ffffffff 00000001 00000000 00200200 lo\n')
        self.assertEqual(firewall.default_route_interfaces(ROUTE, route6), {'ens3', 'eth1'})

    def test_published_ports_include_services_that_predate_bob(self):
        self.assertEqual(firewall.published_ports({'ssh_ports': [22]}), [22])
        self.assertEqual(firewall.published_ports({'ssh_ports': [2222], 'foreign_tcp_ports': [8080, 2222]}),
                         [2222, 8080])


class FirewallStageTests(unittest.TestCase):
    """provision.firewall(): adds only what is missing, verifies, and undoes itself on failure."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = dict(ssh_ports=[22], panel_port=21000, sub_port=22000, warp_mode='install',
                          approved_changes=[])

    def host(self, **options):
        self.ufw = FakeUfw(self.root, **options)
        return self.ufw

    def firewall(self, connection=''):
        with patch.object(provision, 'UFW_ROOT', self.ufw.directory), \
             patch.object(provision.ipv6, 'UFW_DEFAULTS', self.ufw.defaults), \
             patch.object(provision, 'PROC_ROOT', self.root / 'proc'), \
             patch.object(provision.shutil, 'which', return_value='/usr/sbin/ufw'), \
             patch.dict(os.environ, {'SSH_CONNECTION': connection}), \
             patch.object(provision, 'run', side_effect=self.ufw.command):
            provision.firewall(self.state, public=True)

    def snapshot(self):
        return self.ufw.rules(4), self.ufw.rules(6), self.ufw.settings()

    def test_clean_host(self):
        ufw = self.host()
        self.firewall()
        self.assertEqual(ufw.ufw_calls(), [
            ('allow', '22/tcp', 'comment', 'Self-SNI SSH'), ('allow', '80/tcp'), ('allow', '443/tcp'),
            ('default', 'deny', 'incoming'), ('default', 'allow', 'outgoing'), ('--force', 'enable')])
        self.assertEqual(ufw.rules(4), [accept(22), accept(80), accept(443)])
        self.assertEqual(ufw.rules(6), [accept(port, family=6) for port in (22, 80, 443)])
        self.assertTrue(ufw.settings()['enabled'])

    def test_repeat_and_reboot_change_nothing(self):
        ufw = self.host()
        self.firewall()
        before = self.snapshot()
        del ufw.calls[:]
        self.firewall()  # SECURITY step, a resumed run, or a run after reboot
        self.assertEqual(ufw.ufw_calls('allow'), [])
        self.assertEqual(self.snapshot(), before)

    def test_internal_ports_are_never_allowed(self):
        ufw = self.host()
        self.firewall()
        allowed = {call[1] for call in ufw.ufw_calls('allow')}
        self.assertEqual(allowed, {'22/tcp', '80/tcp', '443/tcp'})
        for port in firewall.internal_ports(self.state):
            self.assertNotIn(f'{port}/tcp', allowed)
            firewall.require_private(port, 'opened', ufw.directory, self.root / 'proc')
        self.assertIn(('default', 'deny', 'incoming'), ufw.ufw_calls())

    def test_operator_rules_survive_untouched(self):
        ufw = self.host(enabled=True)
        foreign = [*limit(22), accept(8080), '-A ufw-user-input -p tcp --dport 25 -j DROP',
                   '-A ufw-user-input -s 203.0.113.9 -j ACCEPT']
        ufw.add(*foreign)
        ufw.add(*limit(22, 6), family=6)
        self.firewall()
        self.assertEqual(ufw.ufw_calls('allow'), [('allow', '80/tcp'), ('allow', '443/tcp')])
        self.assertEqual(ufw.rules(4), foreign + [accept(80), accept(443)])
        self.assertNotIn(('ufw', 'reset'), [call[:2] for call in ufw.calls])
        self.assertEqual(ufw.ufw_calls('delete'), [])

    def test_non_standard_ssh_port(self):
        ufw = self.host()
        self.state['ssh_ports'] = [2222, 22022]
        self.firewall(connection='203.0.113.9 50000 192.0.2.1 22022')
        self.assertEqual(ufw.rules(4), [accept(2222), accept(22022), accept(80), accept(443)])

    def test_reject_policy_of_the_operator_is_kept(self):
        ufw = self.host(enabled=True, incoming='REJECT')
        self.firewall()
        self.assertEqual(ufw.settings()['incoming'], 'REJECT')

    def test_conflict_found_at_apply_time_changes_nothing(self):
        ufw = self.host()
        ufw.add('-A ufw-user-input -p tcp --dport 443 -j DROP')
        before = self.snapshot()
        with self.assertRaisesRegex(InstallError, 'запрещает входящие подключения к TCP/443'):
            self.firewall()
        self.assertEqual((ufw.calls, self.snapshot()), ([], before))

    def test_unapproved_change_found_at_apply_time_changes_nothing(self):
        # The operator switched egress to deny after preflight: loosening it was never confirmed.
        ufw = self.host(enabled=True, outgoing='DROP')
        before = self.snapshot()
        with self.assertRaisesRegex(InstallError, 'изменилась после проверки системы'):
            self.firewall()
        self.assertEqual((ufw.calls, self.snapshot()), ([], before))
        # Once exactly this change has been confirmed, the stage proceeds.
        self.state['approved_changes'] = firewall.assess(
            [], firewall.read_settings(ufw.directory, ufw.defaults), [22], set()).consent
        self.assertEqual(len(self.state['approved_changes']), 1)
        self.firewall()
        self.assertEqual(ufw.settings()['outgoing'], 'ACCEPT')

    def test_socket_that_appears_after_preflight_does_not_stop_the_stage(self):
        # Sockets come and go (a daemon restarted by apt, an ephemeral UDP port): the cut-off
        # warning belongs to preflight, where it was shown and confirmed.
        ufw = self.host()
        ufw.tcp = ['LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("node",pid=9,fd=3))']
        ufw.udp = ['UNCONN 0 0 0.0.0.0:47123 0.0.0.0:* users:(("daemon",pid=10,fd=5))']
        self.firewall()
        self.assertTrue(ufw.settings()['enabled'])
        self.assertFalse(any(call[0] == 'ss' for call in ufw.calls))

    def test_failure_rolls_back_to_the_previous_firewall(self):
        for failing in (('allow', '443/tcp'), ('default', 'deny', 'incoming'),
                        ('default', 'allow', 'outgoing'), ('--force', 'enable')):
            for options in (dict(), dict(enabled=True, incoming='ACCEPT')):
                with self.subTest(failing=failing, **options):
                    ufw = self.host(**options)
                    ufw.add(accept(8080))
                    before = self.snapshot()
                    ufw.fail = {failing}
                    with self.assertRaisesRegex(InstallError, 'отменены; этап можно повторить'):
                        self.firewall()
                    self.assertEqual(self.snapshot(), before)
                    # The stage is resumable: the next run completes it.
                    ufw.fail = set()
                    self.firewall()
                    self.assertEqual(ufw.rules(4), [accept(8080), accept(22), accept(80), accept(443)])
                    self.assertEqual(ufw.settings(), dict(before[2], enabled=True, incoming='DROP'))

    def test_unverified_result_is_rolled_back(self):
        ufw = self.host()
        ufw.honest_enable = False  # ufw exits 0 but stays disabled
        before = self.snapshot()
        with self.assertRaisesRegex(InstallError, 'UFW не включён'):
            self.firewall()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(ufw.calls[-1], ('ufw', 'delete', 'allow', '22/tcp'))

    def test_failed_rollback_is_reported_as_unknown_state(self):
        ufw = self.host()
        ufw.fail = {('--force', 'enable'), 'delete'}
        with self.assertRaisesRegex(InstallError, 'откат выполнен не полностью.*ufw delete allow'):
            self.firewall()

    def test_interrupt_rolls_back_too(self):
        ufw = self.host()
        before = self.snapshot()
        real = ufw.command

        def command(*parts, **kwargs):
            if parts[1:] == ('--force', 'enable'):
                raise KeyboardInterrupt
            return real(*parts, **kwargs)
        ufw.command = command
        with self.assertRaises(KeyboardInterrupt):
            self.firewall()
        self.assertEqual(self.snapshot(), before)

    def test_rule_of_the_operator_is_never_deleted_by_rollback(self):
        # SSH is open for one network only and the session comes from elsewhere: Bob's allow
        # is added with consent, but it is not provably Bob's alone, so rollback leaves it.
        ufw = self.host()
        restricted = '-A ufw-user-input -p tcp --dport 22 -s 198.51.100.0/24 -j ACCEPT'
        ufw.add(restricted)
        connection = '203.0.113.9 50000 192.0.2.1 22'
        self.state['approved_changes'] = firewall.assess(
            firewall.rules_from_text(restricted, 4), firewall.read_settings(ufw.directory, ufw.defaults),
            [22], firewall.internal_ports(self.state), firewall.ssh_client(connection)).consent
        ufw.fail = {('--force', 'enable')}
        with self.assertRaises(InstallError):
            self.firewall(connection)
        self.assertIn(restricted, ufw.rules(4))
        self.assertNotIn(('delete', 'allow', '22/tcp'), ufw.ufw_calls())


if __name__ == '__main__':
    unittest.main()
