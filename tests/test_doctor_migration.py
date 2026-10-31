"""Doctor on current main installs and on legacy nodes (migration audit P1)."""
import json
import unittest

from selfsni import doctor, ipv6

from test_doctor import FakeProbe, check

GLOBAL_ADS = 'geosite:category-ads-all'
INTERNAL = ('security.internalListeners', 'security.internalFirewall', 'core.publicRoutes')
PUBLIC = ('deep.publicPanel', 'deep.publicSubscription')


def run(probe, deep=False):
    return doctor.Doctor(probe, deep=deep).run()


def statuses(report, ids):
    return {identifier: check(report, identifier)['status'] for identifier in ids}


def legacy(probe, version):
    """Reshape the v0.3.8 fake into what an older release actually left behind."""
    if version in ('v0.3.4', 'v0.3.5'):
        probe.state.pop('admin_client')
        for rule in probe.config['routing']['rules']:
            if isinstance(rule.get('domain'), list) and GLOBAL_ADS in rule['domain']:
                rule['domain'] = [d for d in rule['domain'] if d != GLOBAL_ADS]
    if version in ('v0.3.4', 'v0.3.5', 'v0.3.6'):
        probe.state.pop('subscription_access')
        # Subscriptions were served directly on their own public TLS port.
        listeners = probe.commands[('ss', '-H', '-lntp')][1].replace('127.0.0.1:22000', '0.0.0.0:22000')
        probe.commands[('ss', '-H', '-lntp')] = (0, listeners)
        probe.files['/etc/ufw/user.rules'] += '-A ufw-user-input -p tcp --dport 22000 -j ACCEPT\n'
    if version == 'v0.3.4':
        probe.state.pop('panel_access')
        listeners = probe.commands[('ss', '-H', '-lntp')][1].replace('127.0.0.1:21000', '0.0.0.0:21000')
        probe.commands[('ss', '-H', '-lntp')] = (0, listeners)
        probe.files['/etc/ufw/user.rules'] += '-A ufw-user-input -p tcp --dport 21000 -j ACCEPT\n'
        probe.files[str(probe.network_path)] = ipv6.LEGACY_NETWORK_CONTENT
        for name in ('/etc/nginx/conf.d/selfsni-tls.conf', '/etc/nginx/snippets/selfsni-panel.conf',
                     '/etc/nginx/snippets/selfsni-subscription.conf'):
            probe.files.pop(name)
    probe.sync()
    return probe


class CurrentInstallTests(unittest.TestCase):
    def test_current_install_is_healthy_with_internal_and_public_checks(self):
        for mode in ('install', 'adopt', 'skip'):
            with self.subTest(mode=mode):
                probe = FakeProbe(mode)
                report = run(probe)
                self.assertEqual(report['overallStatus'], 'HEALTHY')
                self.assertEqual(set(statuses(report, INTERNAL).values()), {'PASS'})
                self.assertEqual(probe.public_calls, [], 'basic mode must not send HTTP requests')
                deep = run(FakeProbe(mode), deep=True)
                self.assertEqual(deep['overallStatus'], 'HEALTHY')
                self.assertEqual(set(statuses(deep, PUBLIC).values()), {'PASS'})

    def test_internal_port_exposed_beyond_loopback_is_fail(self):
        for address in ('0.0.0.0:21000', '[::]:21000', '203.0.113.7:22000'):
            with self.subTest(address=address):
                probe = FakeProbe('install')
                port = address.rsplit(':', 1)[1]
                listeners = probe.commands[('ss', '-H', '-lntp')][1].replace('127.0.0.1:' + port, address)
                probe.commands[('ss', '-H', '-lntp')] = (0, listeners)
                report = run(probe)
                self.assertEqual(check(report, 'security.internalListeners')['status'], 'FAIL')
                self.assertEqual(report['overallStatus'], 'UNHEALTHY')

    def test_internal_listener_of_other_process_or_missing_is_fail(self):
        probe = FakeProbe('install')
        listeners = probe.commands[('ss', '-H', '-lntp')][1].replace('("x-ui",pid=333,fd=9)', '("python3",pid=444,fd=3)')
        probe.commands[('ss', '-H', '-lntp')] = (0, listeners)
        self.assertEqual(check(run(probe), 'security.internalListeners')['status'], 'FAIL')
        probe = FakeProbe('install')
        listeners = '\n'.join(line for line in probe.commands[('ss', '-H', '-lntp')][1].splitlines()
                              if ':22000 ' not in line)
        probe.commands[('ss', '-H', '-lntp')] = (0, listeners)
        self.assertEqual(check(run(probe), 'security.internalListeners')['status'], 'FAIL')

    def test_unavailable_listener_or_pid_data_is_unknown_not_pass(self):
        for key in (('ss', '-H', '-lntp'), ('systemctl', 'show', 'x-ui', '--property=MainPID', '--value')):
            with self.subTest(command=key[0]):
                probe = FakeProbe('install')
                probe.commands[key] = (1, '')
                self.assertEqual(check(run(probe), 'security.internalListeners')['status'], 'UNKNOWN')

    def test_ufw_rule_for_internal_port_is_fail(self):
        for name, port in (('/etc/ufw/user.rules', 21000), ('/etc/ufw/user6.rules', 22000)):
            with self.subTest(rules=name, port=port):
                probe = FakeProbe('install')
                probe.files[name] += f'-A ufw-user-input -p tcp --dport {port} -j ACCEPT\n'
                report = run(probe)
                self.assertEqual(check(report, 'security.internalFirewall')['status'], 'FAIL')
                self.assertEqual(report['overallStatus'], 'UNHEALTHY')
        probe = FakeProbe('install')
        probe.files.pop('/etc/ufw/user6.rules')
        self.assertEqual(check(run(probe), 'security.internalFirewall')['status'], 'UNKNOWN')

    def test_broken_nginx_public_route_config_is_fail(self):
        tls, panel, sub = ('/etc/nginx/conf.d/selfsni-tls.conf', '/etc/nginx/snippets/selfsni-panel.conf',
                           '/etc/nginx/snippets/selfsni-subscription.conf')
        cases = {
            'panel location removed': lambda p: p.files.__setitem__(tls, p.files[tls].replace(
                'location ^~ ' + p.state['panel_path'], 'location ^~ /moved/')),
            'subscription include removed': lambda p: p.files.__setitem__(tls, p.files[tls].replace(
                'include /etc/nginx/snippets/selfsni-subscription.conf;', '')),
            'panel upstream port changed': lambda p: p.files.__setitem__(panel, p.files[panel].replace(
                '127.0.0.1:21000', '127.0.0.1:21001')),
            'subscription TLS verification off': lambda p: p.files.__setitem__(sub, p.files[sub].replace(
                'proxy_ssl_verify on;', 'proxy_ssl_verify off;')),
            'snippet missing': lambda p: p.files.pop(panel),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                probe = FakeProbe('install')
                mutate(probe)
                report = run(probe)
                self.assertEqual(check(report, 'core.publicRoutes')['status'], 'FAIL')
                self.assertEqual(report['overallStatus'], 'UNHEALTHY')

    def test_deep_public_route_must_reach_the_protected_proxy(self):
        panel_path = FakeProbe().state['panel_path']
        sub_missing = FakeProbe().state['sub_path'] + 'missing'
        cases = {
            # nginx `location /` answers 404 without the proxy's no-store/no-referrer headers.
            'panel falls through to 404': (panel_path, (404, {}), 'deep.publicPanel', 'FAIL'),
            'panel upstream down': (panel_path, (502, {'cache-control': 'no-store',
                                                       'referrer-policy': 'no-referrer'}),
                                    'deep.publicPanel', 'FAIL'),
            'subscription not proxied': (sub_missing, (404, {}), 'deep.publicSubscription', 'FAIL'),
        }
        for name, (path, response, identifier, expected) in cases.items():
            with self.subTest(name):
                probe = FakeProbe('install')
                probe.public_responses[path] = response
                report = run(probe, deep=True)
                self.assertEqual(check(report, identifier)['status'], expected)
                self.assertEqual(report['overallStatus'], 'UNHEALTHY')
        probe = FakeProbe('install')
        probe.public_error = TimeoutError('timed out')
        report = run(probe, deep=True)
        self.assertEqual(statuses(report, PUBLIC), dict.fromkeys(PUBLIC, 'UNKNOWN'))

    def test_new_checks_never_print_secret_paths(self):
        probe = FakeProbe('install')
        probe.files['/etc/ufw/user.rules'] += '-A ufw-user-input -p tcp --dport 21000 -j ACCEPT\n'
        probe.public_responses[probe.state['panel_path']] = (404, {})
        text = json.dumps(run(probe, deep=True), ensure_ascii=False)
        for secret in ('a' * 48, 'b' * 48, 'c' * 48, 'SECRET_ADMIN_UUID', 'vpn.example.com'):
            self.assertNotIn(secret, text)


class LegacyNodeTests(unittest.TestCase):
    def test_legacy_nodes_are_degraded_not_unhealthy(self):
        expected = {
            'v0.3.4': {'routing.invariants': 'WARN', 'system.ipv6': 'WARN',
                       'security.internalListeners': 'WARN', 'security.internalFirewall': 'SKIP',
                       'core.publicRoutes': 'SKIP'},
            'v0.3.5': {'routing.invariants': 'WARN', 'system.ipv6': 'PASS',
                       'security.internalListeners': 'PASS', 'security.internalFirewall': 'PASS',
                       'core.publicRoutes': 'PASS'},
            'v0.3.6': {'routing.invariants': 'PASS', 'system.ipv6': 'PASS',
                       'security.internalListeners': 'PASS', 'security.internalFirewall': 'PASS',
                       'core.publicRoutes': 'PASS'},
        }
        for version, checks in expected.items():
            with self.subTest(version=version):
                report = run(legacy(FakeProbe('install'), version))
                self.assertEqual(statuses(report, checks), checks)
                self.assertEqual(report['overallStatus'],
                                 'DEGRADED' if 'WARN' in checks.values() else 'HEALTHY')

    def test_legacy_deep_public_checks_follow_the_node_schema(self):
        report = run(legacy(FakeProbe('install'), 'v0.3.4'), deep=True)
        self.assertEqual(statuses(report, PUBLIC), dict.fromkeys(PUBLIC, 'SKIP'))
        report = run(legacy(FakeProbe('install'), 'v0.3.6'), deep=True)
        self.assertEqual(statuses(report, PUBLIC), {'deep.publicPanel': 'PASS',
                                                    'deep.publicSubscription': 'SKIP'})

    def test_current_policy_is_still_enforced_on_current_nodes(self):
        # A v0.3.6+ node (admin client present) must keep the global ads block.
        probe = FakeProbe('install')
        for rule in probe.config['routing']['rules']:
            if isinstance(rule.get('domain'), list) and GLOBAL_ADS in rule['domain']:
                rule['domain'] = [d for d in rule['domain'] if d != GLOBAL_ADS]
        probe.sync()
        self.assertEqual(check(run(probe), 'routing.invariants')['status'], 'FAIL')
        # A v0.3.5+ node (panel on 443) must use the current networkd drop-in.
        probe = FakeProbe('install')
        probe.files[str(probe.network_path)] = ipv6.LEGACY_NETWORK_CONTENT
        self.assertEqual(check(run(probe), 'system.ipv6')['status'], 'FAIL')

    def test_impossible_or_unknown_markers_are_unknown(self):
        cases = {'subscription without panel': lambda s: s.pop('panel_access'),
                 'unknown panel schema': lambda s: s.update(panel_access='public'),
                 'unknown subscription schema': lambda s: s.update(subscription_access=True)}
        for name, mutate in cases.items():
            with self.subTest(name):
                probe = FakeProbe('install')
                mutate(probe.state)
                probe.sync()
                report = run(probe)
                self.assertEqual(check(report, 'core.state')['status'], 'UNKNOWN')
                self.assertEqual(report['overallStatus'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
