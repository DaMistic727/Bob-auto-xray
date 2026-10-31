from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from selfsni import doctor, provision, ipv6, swap
from selfsni.geodata import REQUIRED, automatic_config
from selfsni.routing import inbound, server_template


class FakeProbe:
    def __init__(self, mode='adopt'):
        self.mode = mode
        self.calls = []
        self.get_calls = []
        self.now_value = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.expiry = self.now_value + timedelta(days=60)
        self.cert_matches = True
        self.cert_hash = b'current-cert'
        self.tls_hash = b'current-cert'
        self.tls_error = None
        self.get_error = None
        self.exe_path = Path('/usr/local/x-ui/bin/xray-linux-amd64')
        self.free = 3 * 1024**3
        self.conflicting_bbr = False
        self.conflicting_ipv6 = False
        self.ipv6_values = {'all': '1', 'default': '1', 'ens3': '1'}
        self.ipv6_addresses = []
        self.network_path = Path('/etc/systemd/network/10-netplan-ens3.network.d/' + ipv6.NETWORK_DROPIN)
        self.symlinks = set()
        state = {'schema': 2, 'installation_id': 'secret-id', 'status': 'HEALTHY_LOCAL',
                 'stage': 'HEALTHY_LOCAL', 'domain': 'vpn.example.com', 'warp_mode': mode,
                 'adopt_warp': mode == 'adopt', 'panel_port': 21000, 'sub_port': 22000,
                 'panel_path': '/' + 'a' * 48 + '/', 'sub_path': '/' + 'b' * 48 + '/',
                 'api_token': 'SECRET_TOKEN', 'password': 'SECRET_PASSWORD',
                 'reality_private': 'SECRET_PRIVATE', 'short_id': 'SECRET_SHORT',
                 'php_service': 'php8.4-fpm',
                 'xray_bin': '/usr/local/x-ui/bin/xray-linux-amd64',
                 'ipv6': {'networkDropin': str(self.network_path)},
                 'swap': {'status': 'created', 'path': '/swapfile', 'bytes': swap.SIZE},
                 # Current main (v0.3.7+): panel and subscriptions behind nginx on 443.
                 'panel_access': 'https-443', 'subscription_access': 'https-443',
                 'admin_client': {'id': 'SECRET_ADMIN_UUID', 'subId': 'c' * 48}}
        config = server_template({'outbounds': [
            {'tag': 'direct', 'protocol': 'freedom'},
            {'tag': 'blocked', 'protocol': 'blackhole'}],
            'routing': {'rules': [{'type': 'field', 'inboundTag': ['api'],
                                   'outboundTag': 'api'}]}}, mode)
        row = inbound('vpn.example.com', 'SECRET_PRIVATE', 'public', 'SECRET_SHORT')
        row['streamSettings'] = json.loads(row['streamSettings'])
        row['settings'] = json.loads(row['settings'])
        row['settings']['clients'] = [{'id': 'SECRET_CLIENT_UUID'}]
        config['inbounds'] = [row, {'tag': 'extra', 'port': 444, 'protocol': 'vless'}]
        config['geodata'] = automatic_config()
        self.state = state
        self.config = config
        self.files = {
            doctor.STATE: json.dumps(state), doctor.CONFIG: json.dumps(config),
            '/etc/letsencrypt/renewal-hooks/deploy/50-selfsni':
                '#!/bin/sh\nset -eu\nnginx -t\nsystemctl reload nginx\nsystemctl restart x-ui\n',
            '/etc/sysctl.d/99-zz-bob-auto-xray-bbr.conf':
                'net.ipv4.tcp_congestion_control = bbr\nnet.core.default_qdisc = fq\n',
            str(ipv6.CONFIG): ipv6.CONTENT,
            str(self.network_path): ipv6.NETWORK_CONTENT,
            str(swap.FSTAB): swap.LINE + '\n',
        }
        # Real templates, so a template change that breaks Doctor's parsing fails here.
        nginx_values = dict(DOMAIN='vpn.example.com', PANEL_PORT=21000, SUB_PORT=22000,
                            PANEL_PATH=state['panel_path'], PANEL_NO_SLASH=state['panel_path'][:-1],
                            SUB_PATH=state['sub_path'], AUTH='sauth', STYLE='sstyle', ICON='sicon')
        self.files.update({
            '/etc/nginx/conf.d/selfsni-tls.conf': provision.render('nginx-tls.conf', nginx_values),
            '/etc/nginx/snippets/selfsni-panel.conf': provision.render('nginx-panel.conf', nginx_values),
            '/etc/nginx/snippets/selfsni-subscription.conf': provision.render('nginx-subscription.conf', nginx_values),
            '/etc/ufw/user.rules': '-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n'
                                   '-A ufw-user-input -p tcp --dport 80 -j ACCEPT\n'
                                   '-A ufw-user-input -p tcp --dport 443 -j ACCEPT\n',
            '/etc/ufw/user6.rules': '-A ufw6-user-input -p tcp --dport 22 -j ACCEPT\n',
        })
        self.public_calls = []
        self.public_error = None
        proxied = {'cache-control': 'no-store', 'referrer-policy': 'no-referrer'}
        self.public_responses = {state['panel_path']: (200, dict(proxied)),
                                 state['sub_path'] + 'missing': (404, dict(proxied))}
        self.commands = {}
        self.stats = {}
        self.missing_stats = set()
        for name in ('x-ui', 'nginx', 'php8.4-fpm', 'fail2ban', 'certbot.timer', 'warp-svc'):
            self.commands[('systemctl', 'is-active', name)] = (0, 'active\n')
            self.commands[('systemctl', 'is-enabled', name)] = (0, 'enabled\n')
        self.commands[('systemctl', 'show', 'x-ui', '--property=Environment', '--value')] = (0, 'TZ=Etc/UTC\n')
        self.commands[('ss', '-H', '-lntp')] = (0,
            'LISTEN 0 4096 0.0.0.0:443 0.0.0.0:* users:(("xray",pid=111,fd=7))\n'
            'LISTEN 0 4096 127.0.0.1:40000 0.0.0.0:* users:(("warp-svc",pid=222,fd=8))\n'
            'LISTEN 0 4096 127.0.0.1:21000 0.0.0.0:* users:(("x-ui",pid=333,fd=9))\n'
            'LISTEN 0 4096 127.0.0.1:22000 0.0.0.0:* users:(("x-ui",pid=333,fd=10))\n')
        self.commands[('systemctl', 'show', 'x-ui', '--property=MainPID', '--value')] = (0, '333\n')
        self.commands[('ss', '-H', '-xl')] = (0,
            'u_str LISTEN 0 511 /run/selfsni/nginx.sock 12345 * 0\n')
        self.commands[('warp-cli', '--accept-tos', '--json', 'status')] = (0, '{"status":"Connected"}')
        self.commands[('warp-cli', '--accept-tos', '--json', 'settings')] = (0,
            '{"settings":{"operation_mode":"proxy","proxy_port":40000}}')
        self.commands[('systemctl', 'show', 'warp-svc', '--property=MainPID', '--value')] = (0, '222\n')
        self.commands[('ufw', 'status')] = (0, 'Status: active\n')
        for jail in ('sshd', '3x-ipl'):
            self.commands[('fail2ban-client', 'status', jail)] = (0, 'ok')
        self.commands[('sysctl', '-n', 'net.ipv4.tcp_congestion_control')] = (0, 'bbr\n')
        self.commands[('sysctl', '-n', 'net.core.default_qdisc')] = (0, 'fq\n')
        self.commands[('swapon', '--show=NAME', '--noheadings', '--raw')] = (0, '/swapfile\n')

    def read(self, path):
        path = str(path)
        self.calls.append(('read', path))
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def stat(self, path):
        path = str(path)
        self.calls.append(('stat', path))
        if path in self.missing_stats:
            raise FileNotFoundError(path)
        if path == doctor.SOCKET:
            mode = stat.S_IFSOCK | 0o750
        elif path in self.files or path in (doctor.STATE, doctor.CONFIG, str(swap.FILE),
              '/etc/x-ui/x-ui.db', '/etc/x-ui/install-result.env',
              '/etc/letsencrypt/live/vpn.example.com/privkey.pem',
              '/etc/letsencrypt/live/vpn.example.com/fullchain.pem'):
            mode = stat.S_IFREG | (0o700 if path.endswith('50-selfsni') else 0o600)
        else:
            raise FileNotFoundError(path)
        if path == str(swap.FILE):
            mode = stat.S_IFREG | 0o600
        return type('S', (), {'st_mode': self.stats.get(path, mode), 'st_uid': 0,
                              'st_size': swap.SIZE if path == str(swap.FILE) else 100})()

    def is_symlink(self, path):
        return str(path) in self.symlinks

    def command(self, *args, timeout=5):
        self.calls.append(('command', args, timeout))
        if args and args[0] == 'curl':
            return self.commands.get(args, (0, 'warp=on\n' if '--socks5-hostname' in args else 'warp=off\n'))
        return self.commands.get(args, (127, 'SECRET_TOKEN SECRET_CLIENT_UUID'))

    def certificate(self, path, domain):
        self.calls.append(('certificate', path))
        return self.expiry, self.cert_matches, self.cert_hash

    def tls(self, domain, port):
        self.calls.append(('tls', port))
        if self.tls_error:
            raise self.tls_error
        return self.tls_hash

    def get(self, domain, port, path):
        self.get_calls.append((port, path))
        if self.get_error:
            raise self.get_error
        return 404 if port == 22000 else 200

    def public_get(self, domain, path):
        self.public_calls.append(path)
        if self.public_error:
            raise self.public_error
        return self.public_responses.get(path, (404, {}))

    def sync(self):
        """Persist edits of self.state/self.config into the files Doctor reads."""
        self.files[doctor.STATE] = json.dumps(self.state)
        self.files[doctor.CONFIG] = json.dumps(self.config)

    def disk_free(self):
        return self.free

    def bbr_conflicts(self):
        return self.conflicting_bbr

    def ipv6_snapshot(self):
        return self.network_path, self.ipv6_values, self.ipv6_addresses

    def ipv6_conflicts(self):
        return self.conflicting_ipv6

    def categories(self, path):
        self.calls.append(('categories', path))
        return REQUIRED[Path(path).name]

    def xray_timezone(self, binary):
        self.calls.append(('xray_timezone', binary))
        return True

    def process_exe(self, pid):
        self.calls.append(('process_exe', pid))
        return self.exe_path

    def now(self):
        return self.now_value


def check(report, identifier):
    return next(x for x in report['checks'] if x['id'] == identifier)


class DoctorTests(unittest.TestCase):
    def test_healthy_with_clients_and_extra_inbound(self):
        probe = FakeProbe()
        before = dict(probe.files)
        report = doctor.Doctor(probe).run()
        self.assertEqual(report['overallStatus'], 'HEALTHY')
        self.assertEqual(check(report, 'core.inbound')['status'], 'PASS')
        self.assertEqual(check(report, 'routing.invariants')['status'], 'PASS')
        self.assertEqual([x['id'] for x in report['checks']],
                         [x['id'] for x in doctor.Doctor(FakeProbe()).run()['checks']])
        self.assertFalse(probe.get_calls)
        self.assertEqual(probe.files, before)
        self.assertFalse(any(call[0] == 'command' and call[1][0] == 'curl'
                             for call in probe.calls))

    def test_core_failures_and_unknown_are_isolated(self):
        probe = FakeProbe()
        probe.commands[('systemctl', 'is-active', 'x-ui')] = (3, 'inactive\n')
        probe.stats[doctor.SOCKET] = stat.S_IFREG | 0o600
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'core.xui')['status'], 'FAIL')
        self.assertEqual(check(report, 'core.socket')['status'], 'FAIL')
        self.assertEqual(report['overallStatus'], 'UNHEALTHY')
        self.assertEqual(check(report, 'system.disk')['status'], 'PASS')
        probe = FakeProbe()
        probe.commands[('ss', '-H', '-lntp')] = (1, 'SECRET_TOKEN')
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'core.port443')['status'], 'UNKNOWN')
        self.assertEqual(report['overallStatus'], 'UNKNOWN')
        stale = FakeProbe()
        stale.commands[('ss', '-H', '-xl')] = (0, '')
        self.assertEqual(check(doctor.Doctor(stale).run(), 'core.socket')['status'], 'FAIL')

    def test_port443_owner_uses_executable_identity(self):
        probe = FakeProbe()
        probe.exe_path = Path('/usr/bin/foreign-xray')
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'core.port443')['status'], 'FAIL')
        self.assertEqual(report['overallStatus'], 'UNHEALTHY')
        probe = FakeProbe()
        probe.commands[('ss', '-H', '-lntp')] = (0,
            'LISTEN 0 4096 0.0.0.0:443 0.0.0.0:* users:(("xray"))\n')
        self.assertEqual(check(doctor.Doctor(probe).run(), 'core.port443')['status'], 'UNKNOWN')

    def test_certificate_expired_soon_stale_and_timer(self):
        probe = FakeProbe()
        probe.expiry = probe.now_value - timedelta(days=1)
        self.assertEqual(check(doctor.Doctor(probe).run(), 'tls.certificate')['status'], 'FAIL')
        probe.expiry = probe.now_value + timedelta(days=15)
        self.assertEqual(check(doctor.Doctor(probe).run(), 'tls.certificate')['status'], 'WARN')
        probe.expiry = probe.now_value + timedelta(days=60)
        probe.tls_hash = b'stale'
        probe.commands[('systemctl', 'is-active', 'certbot.timer')] = (3, 'inactive\n')
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'tls.listeners')['status'], 'FAIL')
        self.assertEqual(check(report, 'tls.timer')['status'], 'WARN')
        missing = FakeProbe()
        missing.missing_stats.add('/etc/letsencrypt/live/vpn.example.com/fullchain.pem')
        self.assertEqual(check(doctor.Doctor(missing).run(), 'tls.certificate')['status'], 'FAIL')

    def test_persistent_bbr_conflict_is_warning(self):
        probe = FakeProbe()
        probe.conflicting_bbr = True
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'system.bbr')['status'], 'WARN')
        self.assertEqual(report['overallStatus'], 'DEGRADED')

    def test_ipv6_current_install_and_fail_closed_states(self):
        healthy = doctor.Doctor(FakeProbe()).run()
        self.assertEqual(check(healthy, 'system.ipv6')['status'], 'PASS')
        for change in ('interface', 'address', 'sysctl', 'dropin', 'conflict', 'symlink'):
            probe = FakeProbe()
            if change == 'interface':
                probe.ipv6_values['ens3'] = '0'
            elif change == 'address':
                probe.ipv6_addresses = [{'addr_info': [{'local': '2001:db8::1'}]}]
            elif change == 'sysctl':
                probe.files[str(ipv6.CONFIG)] = 'net.ipv6.conf.all.disable_ipv6 = 0\n'
            elif change == 'dropin':
                del probe.files[str(probe.network_path)]
            elif change == 'conflict':
                probe.conflicting_ipv6 = True
            else:
                probe.symlinks.add(str(ipv6.CONFIG))
            report = doctor.Doctor(probe).run()
            expected = 'WARN' if change == 'conflict' else 'FAIL'
            self.assertEqual(check(report, 'system.ipv6')['status'], expected, change)
            self.assertNotEqual(report['overallStatus'], 'HEALTHY', change)
        probe = FakeProbe()
        probe.ipv6_snapshot = lambda: (_ for _ in ()).throw(OSError('SECRET_TOKEN'))
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'system.ipv6')['status'], 'UNKNOWN')
        self.assertNotIn('SECRET_TOKEN', json.dumps(report))

    def test_swap_created_skipped_legacy_and_broken(self):
        self.assertEqual(check(doctor.Doctor(FakeProbe()).run(), 'system.swap')['status'], 'PASS')
        for status in (None, 'skipped'):
            probe = FakeProbe()
            if status is None:
                del probe.state['swap']
            else:
                probe.state['swap'] = {'status': 'skipped', 'reason': 'insufficient free disk space'}
            del probe.state['ipv6']
            probe.files[doctor.STATE] = json.dumps(probe.state)
            probe.missing_stats.update((str(swap.FILE), str(ipv6.CONFIG)))
            del probe.files[str(ipv6.CONFIG)]
            probe.files[str(swap.FSTAB)] = ''
            probe.commands[('swapon', '--show=NAME', '--noheadings', '--raw')] = (0, '')
            report = doctor.Doctor(probe).run()
            self.assertEqual(check(report, 'system.swap')['status'], 'SKIP')
            self.assertEqual(check(report, 'system.ipv6')['status'], 'SKIP')
            self.assertEqual(report['overallStatus'], 'HEALTHY')
        for change in ('missing', 'inactive', 'fstab', 'symlink', 'unknown'):
            probe = FakeProbe()
            if change == 'missing':
                probe.missing_stats.add(str(swap.FILE))
            elif change == 'inactive':
                probe.commands[('swapon', '--show=NAME', '--noheadings', '--raw')] = (0, '')
            elif change == 'fstab':
                probe.files[str(swap.FSTAB)] = ''
            elif change == 'symlink':
                probe.symlinks.add(str(swap.FILE))
            else:
                probe.state['swap'] = {'status': 'unexpected'}
                probe.files[doctor.STATE] = json.dumps(probe.state)
            report = doctor.Doctor(probe).run()
            self.assertEqual(check(report, 'system.swap')['status'],
                             'UNKNOWN' if change == 'unknown' else 'FAIL', change)
            self.assertNotEqual(report['overallStatus'], 'HEALTHY', change)

    def test_skipped_swap_with_foreign_file_is_unknown(self):
        probe = FakeProbe()
        probe.state['swap'] = {'status': 'skipped'}
        probe.files[doctor.STATE] = json.dumps(probe.state)
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'system.swap')['status'], 'UNKNOWN')
        self.assertEqual(report['overallStatus'], 'UNKNOWN')

    def test_commented_out_deploy_hook_is_not_pass(self):
        probe = FakeProbe()
        path = '/etc/letsencrypt/renewal-hooks/deploy/50-selfsni'
        probe.files[path] = '#!/bin/sh\n# nginx -t\n# systemctl reload nginx\n# systemctl restart x-ui\n'
        self.assertEqual(check(doctor.Doctor(probe).run(), 'tls.hook')['status'], 'WARN')

    def test_warp_modes_and_failures(self):
        probe = FakeProbe('skip')
        report = doctor.Doctor(probe).run()
        self.assertEqual(report['overallStatus'], 'HEALTHY')
        self.assertTrue(all(check(report, x)['status'] == 'SKIP' for x in
                            ('warp.service', 'warp.settings', 'warp.listener')))
        self.assertFalse(any('warp-cli' in str(x) for x in probe.calls))
        for mode in ('adopt', 'install'):
            live = FakeProbe(mode)
            self.assertEqual(doctor.Doctor(live).run()['overallStatus'], 'HEALTHY')
            warp_calls = [call[1] for call in live.calls if call[0] == 'command'
                          and call[1][0] == 'warp-cli']
            self.assertEqual(warp_calls, [
                ('warp-cli', '--accept-tos', '--json', 'status'),
                ('warp-cli', '--accept-tos', '--json', 'settings')])
        probe = FakeProbe('adopt')
        probe.commands[('warp-cli', '--accept-tos', '--json', 'status')] = (0, '{"status":"Disconnected"}')
        self.assertEqual(check(doctor.Doctor(probe).run(), 'warp.settings')['status'], 'FAIL')
        legacy = FakeProbe('adopt')
        del legacy.state['warp_mode']
        legacy.files[doctor.STATE] = json.dumps(legacy.state)
        self.assertEqual(check(doctor.Doctor(legacy).run(), 'core.state')['details']['warpMode'], 'adopt')
        skipped = FakeProbe('skip')
        skipped.config['outbounds'].append({'tag': 'warp-outbound', 'protocol': 'socks'})
        skipped.files[doctor.CONFIG] = json.dumps(skipped.config)
        self.assertEqual(check(doctor.Doctor(skipped).run(), 'routing.invariants')['status'], 'FAIL')

    def test_warp_listener_requires_warp_service_pid(self):
        probe = FakeProbe()
        probe.commands[('systemctl', 'show', 'warp-svc', '--property=MainPID', '--value')] = (0, '333\n')
        self.assertEqual(check(doctor.Doctor(probe).run(), 'warp.listener')['status'], 'FAIL')
        probe.commands[('systemctl', 'show', 'warp-svc', '--property=MainPID', '--value')] = (1, '')
        self.assertEqual(check(doctor.Doctor(probe).run(), 'warp.listener')['status'], 'UNKNOWN')

    def test_deep_expected_responses_and_custom_rules(self):
        probe = FakeProbe()
        probe.config['routing']['rules'].append({'type': 'field', 'domain': ['example.com'],
                                                 'outboundTag': 'direct'})
        probe.files[doctor.CONFIG] = json.dumps(probe.config)
        report = doctor.Doctor(probe, deep=True).run()
        self.assertEqual(report['overallStatus'], 'HEALTHY')
        self.assertEqual(check(report, 'deep.warp')['status'], 'PASS')
        self.assertEqual(check(report, 'routing.invariants')['status'], 'PASS')
        probe.get = lambda domain, port, path: 404
        report = doctor.Doctor(probe, deep=True).run()
        self.assertEqual(check(report, 'deep.fallback')['status'], 'FAIL')

    def test_missing_damaged_and_incomplete_state_stop_early(self):
        for content in (None, 'broken', '{}', json.dumps({'schema': 2, 'status': 'INSTALLING'})):
            probe = FakeProbe()
            if content is None:
                del probe.files[doctor.STATE]
            else:
                probe.files[doctor.STATE] = content
            report = doctor.Doctor(probe, deep=True).run()
            self.assertEqual(report['overallStatus'], 'UNKNOWN')
            self.assertEqual(len(report['checks']), 1)
            self.assertFalse(probe.get_calls)
            self.assertFalse(any(call[0] == 'command' for call in probe.calls))

    def test_untrusted_deep_paths_and_duplicate_tls_ports_stop_before_get(self):
        for change in ({'panel_path': '/panel/api/inbounds/delete/1'},
                       {'sub_path': '/sub/?update=1'},
                       {'panel_port': 443}, {'sub_port': 21000}):
            probe = FakeProbe()
            probe.state.update(change)
            probe.files[doctor.STATE] = json.dumps(probe.state)
            report = doctor.Doctor(probe, deep=True).run()
            self.assertEqual(report['overallStatus'], 'UNKNOWN')
            self.assertEqual(len(report['checks']), 1)
            self.assertFalse(probe.get_calls)

    def test_deep_get_trace_timeout_and_no_mutations(self):
        probe = FakeProbe()
        probe.get_error = socket.timeout('SECRET_TOKEN')
        report = doctor.Doctor(probe, deep=True).run()
        self.assertEqual(check(report, 'deep.fallback')['status'], 'UNKNOWN')
        self.assertEqual(report['overallStatus'], 'UNKNOWN')
        self.assertEqual(len(probe.get_calls), 3)
        self.assertFalse(any(call[0] == 'command' and call[1][0] == 'curl'
                             for call in FakeProbe().calls))
        commands = [c[1] for c in probe.calls if c[0] == 'command']
        self.assertTrue(all(c[0] in ('systemctl', 'ss', 'warp-cli', 'ufw',
                                      'fail2ban-client', 'sysctl', 'swapon', 'curl') for c in commands))
        self.assertTrue(all(c[1] not in ('restart', 'reload', 'stop', 'update') for c in commands))
        self.assertTrue(all(c[1][1:3] == ('-q', '--noproxy') for c in probe.calls
                            if c[0] == 'command' and c[1][0] == 'curl'))

    def test_deep_timeout_cannot_touch_clients_or_panel_api(self):
        probe = FakeProbe()
        original = dict(probe.files)
        def timeout(*args, **kwargs):
            if args[0] == 'curl':
                raise subprocess.TimeoutExpired(args, 15, output='SECRET_CLIENT_UUID')
            return FakeProbe.command(probe, *args, **kwargs)
        with patch.object(probe, 'command', side_effect=timeout), \
             patch('selfsni.acceptance.verify') as acceptance_verify, \
             patch('selfsni.api.Panel.call') as panel_call, \
             patch('selfsni.install_state.InstallerLock') as lock, \
             patch('selfsni.install_state.save') as state_save, \
             patch('selfsni.core.save') as core_save:
            report = doctor.Doctor(probe, deep=True).run()
        for forbidden in (acceptance_verify, panel_call, lock, state_save, core_save):
            forbidden.assert_not_called()
        self.assertEqual(check(report, 'deep.warp')['status'], 'UNKNOWN')
        self.assertEqual(check(report, 'deep.egress')['status'], 'UNKNOWN')
        self.assertEqual(report['overallStatus'], 'UNKNOWN')
        self.assertEqual(probe.files, original)
        self.assertNotIn('SECRET_CLIENT_UUID', json.dumps(report))

    def test_secret_free_output_and_exit_codes(self):
        for status, expected in (('HEALTHY', 0), ('DEGRADED', 1),
                                 ('UNHEALTHY', 2), ('UNKNOWN', 3)):
            report = doctor.Doctor(FakeProbe()).run()
            report['overallStatus'] = status
            output = io.StringIO()
            with patch.object(doctor, 'Doctor') as cls, patch('sys.stdout', output):
                cls.return_value.run.return_value = report
                self.assertEqual(doctor.main(['--json']), expected)
            parsed = json.loads(output.getvalue())
            self.assertEqual(parsed['schemaVersion'], 1)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            for secret in ('SECRET_TOKEN', 'SECRET_PASSWORD', 'SECRET_PRIVATE',
                           'SECRET_CLIENT_UUID', 'SECRET_SHORT', 'a' * 48,
                           'b' * 48, 'vpn.example.com'):
                self.assertNotIn(secret, output.getvalue())

    def test_invalid_json_invocation_returns_one_safe_object(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            self.assertEqual(doctor.main(['--json', '--bad=SECRET_TOKEN']), 3)
        self.assertEqual(json.loads(stdout.getvalue())['overallStatus'], 'UNKNOWN')
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertNotIn('SECRET_TOKEN', stdout.getvalue() + stderr.getvalue())

    def test_internal_exception_does_not_leak_to_json_or_stderr(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(doctor.Doctor, 'run', side_effect=RuntimeError('SECRET_TOKEN')), \
             patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            self.assertEqual(doctor.main(['--deep', '--json']), 3)
        self.assertEqual(json.loads(stdout.getvalue())['overallStatus'], 'UNKNOWN')
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertNotIn('SECRET_TOKEN', stdout.getvalue() + stderr.getvalue())

    def test_cli_install_preserves_foreign_command(self):
        with tempfile.TemporaryDirectory() as temp:
            command = Path(temp) / 'bob'
            command.write_text('foreign')
            with patch.object(provision, 'DOCTOR_COMMAND', command), \
                 patch.object(provision, 'DOCTOR_LIB', Path(temp) / 'lib'):
                self.assertFalse(provision.install_doctor_cli())
            self.assertEqual(command.read_text(), 'foreign')
            self.assertTrue((Path(temp) / 'lib/selfsni/doctor.py').is_file())

    def test_cli_install_uses_packaged_module(self):
        with tempfile.TemporaryDirectory() as temp:
            command = Path(temp) / 'bob'
            library = Path(temp) / 'lib'
            with patch.object(provision, 'DOCTOR_COMMAND', command), \
                 patch.object(provision, 'DOCTOR_LIB', library), \
                 patch.object(provision, 'DOCTOR_PYTHON', Path(sys.executable)):
                self.assertTrue(provision.install_doctor_cli())
            self.assertTrue((library / 'selfsni/doctor.py').is_file())
            self.assertTrue(command.stat().st_mode & stat.S_IXUSR)
            self.assertIn(' -I -B -c ', command.read_text())
            malicious = Path(temp) / 'cwd/selfsni'
            malicious.mkdir(parents=True)
            marker = Path(temp) / 'imported-foreign-package'
            (malicious / '__init__.py').write_text(f'open({str(marker)!r}, "w").close()')
            result = subprocess.run([str(command), 'doctor', '--help'],
                                    cwd=malicious.parent, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())

    def test_subprocess_has_fixed_environment_and_timeout(self):
        completed = subprocess.CompletedProcess(['ss'], 0, 'safe output', 'SECRET_TOKEN')
        with patch('selfsni.doctor.subprocess.run', return_value=completed) as run:
            self.assertEqual(doctor.Probe().command('ss', '-H', timeout=4), (0, 'safe output'))
        options = run.call_args.kwargs
        self.assertEqual(options['timeout'], 4)
        self.assertIs(options['stdin'], subprocess.DEVNULL)
        self.assertTrue(options['start_new_session'])
        self.assertEqual(options['env']['PATH'], '/usr/sbin:/usr/bin:/sbin:/bin')
        self.assertNotIn('PYTHONPATH', options['env'])

    def test_tls_context_ignores_keylog_and_ca_environment(self):
        cafile = ssl.get_default_verify_paths().cafile
        if not cafile or not Path(cafile).is_file():
            self.skipTest('No local CA bundle for this test host')
        with tempfile.TemporaryDirectory() as temp:
            keylog = Path(temp) / 'keys.log'
            with patch.object(doctor, 'CA_BUNDLE', cafile), \
                 patch.dict(os.environ, {'SSLKEYLOGFILE': str(keylog),
                                          'SSL_CERT_FILE': str(Path(temp) / 'foreign-ca')}):
                context = doctor.verified_context()
            self.assertIsNone(context.keylog_filename)
            self.assertFalse(keylog.exists())
            self.assertGreater(context.cert_store_stats()['x509_ca'], 0)

    def test_deep_http_probe_uses_only_unauthenticated_get(self):
        with patch('selfsni.api.LocalTLS') as connection, \
             patch.object(doctor, 'verified_context', return_value=object()):
            connection.return_value.getresponse.return_value.status = 200
            self.assertEqual(doctor.Probe().get('vpn.example.com', 443, '/'), 200)
        connection.return_value.request.assert_called_once_with(
            'GET', '/', headers={'Connection': 'close'})
        connection.return_value.close.assert_called_once_with()

    def test_warp_outbound_must_be_single_unauthenticated_loopback(self):
        for servers in ([{'address': '127.0.0.1', 'port': 40000, 'users': []},
                         {'address': '198.51.100.1', 'port': 40000}],
                        [{'address': '127.0.0.1', 'port': 40000,
                          'users': [{'user': 'SECRET_TOKEN'}]}]):
            probe = FakeProbe()
            warp = next(x for x in probe.config['outbounds'] if x['tag'] == 'warp-outbound')
            warp['settings']['servers'] = servers
            probe.files[doctor.CONFIG] = json.dumps(probe.config)
            self.assertEqual(check(doctor.Doctor(probe).run(), 'routing.invariants')['status'], 'FAIL')

    def test_malformed_runtime_routing_is_fail(self):
        for change in ({'outbounds': ['invalid']},
                       {'outbounds': [{'tag': ['invalid'], 'protocol': 'freedom'}]},
                       {'routing': []},
                       {'routing': {'domainStrategy': 'IPOnDemand', 'rules': ['invalid']}}):
            probe = FakeProbe()
            probe.config.update(change)
            probe.files[doctor.CONFIG] = json.dumps(probe.config)
            report = doctor.Doctor(probe).run()
            self.assertEqual(check(report, 'routing.invariants')['status'], 'FAIL')
            self.assertEqual(report['overallStatus'], 'UNHEALTHY')


    def test_malicious_state_and_runtime_symlink_fail_closed(self):
        for change in ({'php_service': '--system'},
                       {'xray_bin': '/usr/local/x-ui/bin/../../bin/foreign'}):
            probe = FakeProbe()
            probe.state.update(change)
            probe.files[doctor.STATE] = json.dumps(probe.state)
            report = doctor.Doctor(probe, deep=True).run()
            self.assertEqual(report['overallStatus'], 'UNKNOWN')
            self.assertEqual(len(report['checks']), 1)
            self.assertFalse(probe.get_calls)
        probe = FakeProbe()
        probe.symlinks.add(doctor.STATE)
        self.assertEqual(len(doctor.Doctor(probe).run()['checks']), 1)
        probe = FakeProbe()
        probe.symlinks.add(doctor.CONFIG)
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'core.config')['status'], 'UNKNOWN')
        self.assertEqual(report['overallStatus'], 'UNKNOWN')

    def test_dns_tls_http_and_trace_failures_cannot_be_healthy(self):
        for error in (socket.gaierror('SECRET_TOKEN'), ssl.SSLCertVerificationError('bad cert'),
                      socket.timeout('timeout')):
            probe = FakeProbe()
            probe.get_error = error
            report = doctor.Doctor(probe, deep=True).run()
            self.assertEqual(check(report, 'deep.fallback')['status'], 'UNKNOWN')
            self.assertEqual(report['overallStatus'], 'UNKNOWN')
            self.assertNotIn('SECRET_TOKEN', json.dumps(report))
        probe = FakeProbe()
        probe.get = lambda domain, port, path: 500
        self.assertEqual(check(doctor.Doctor(probe, deep=True).run(),
                               'deep.fallback')['status'], 'FAIL')
        probe = FakeProbe()
        probe.commands[('curl', '-q', '--noproxy', '', '-fsS', '--socks5-hostname',
                        '127.0.0.1:40000', '--connect-timeout', '5', '--max-time', '12',
                        'https://www.cloudflare.com/cdn-cgi/trace')] = (0, 'warp=off\n')
        report = doctor.Doctor(probe, deep=True).run()
        self.assertEqual(check(report, 'deep.warp')['status'], 'WARN')
        self.assertEqual(report['overallStatus'], 'DEGRADED')

    def test_ipv6_state_path_mismatch_is_warning(self):
        probe = FakeProbe()
        probe.state['ipv6']['networkDropin'] = '/etc/systemd/network/10-netplan-other.network.d/99-zz-bob-auto-xray-ipv4-only.conf'
        probe.files[doctor.STATE] = json.dumps(probe.state)
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'system.ipv6')['status'], 'WARN')
        self.assertEqual(report['overallStatus'], 'DEGRADED')

    def test_skipped_swap_with_foreign_fstab_entry_is_unknown(self):
        probe = FakeProbe()
        probe.state['swap'] = {'status': 'skipped'}
        probe.files[doctor.STATE] = json.dumps(probe.state)
        probe.missing_stats.add(str(swap.FILE))
        probe.commands[('swapon', '--show=NAME', '--noheadings', '--raw')] = (0, '')
        probe.files[str(swap.FSTAB)] = '/swapfile none swap defaults 0 0\n'
        self.assertEqual(check(doctor.Doctor(probe).run(), 'system.swap')['status'], 'UNKNOWN')

    def test_foreign_swap_is_not_silent_healthy(self):
        probe = FakeProbe()
        probe.commands[('swapon', '--show=NAME', '--noheadings', '--raw')] = (0, '/swapfile\n/dev/zram0\n')
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'system.swap')['status'], 'WARN')
        self.assertEqual(report['overallStatus'], 'DEGRADED')
        probe.state['swap'] = {'status': 'skipped'}
        probe.files[doctor.STATE] = json.dumps(probe.state)
        probe.missing_stats.add(str(swap.FILE))
        probe.files[str(swap.FSTAB)] = ''
        report = doctor.Doctor(probe).run()
        self.assertEqual(check(report, 'system.swap')['status'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
