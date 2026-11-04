import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import DEFAULT, MagicMock, patch

from selfsni import ipv6, provision
from selfsni.core import InstallError


class Kernel:
    def __init__(self, fail=None):
        self.values = {f'net.ipv6.conf.{name}.disable_ipv6': '0'
                       for name in ('all', 'default', 'lo', 'ens3')}
        self.values['net.ipv4.ip_forward'] = '0'
        self.fail = fail
        self.reloads = 0
        self.addresses = '[]'
        self.listeners6 = 'LISTEN 0 511 [::]:80 [::]:*\nLISTEN 0 4096 [::1]:62789 [::]:*\n'

    def __call__(self, *args):
        if args[:2] == ('sysctl', '-n'):
            return self.values[args[2]] + '\n'
        if args[:2] == ('sysctl', '-w'):
            key, value = args[2].split('=')
            if key == self.fail:
                raise InstallError('sysctl failed')
            self.values[key] = value
            if key.endswith('.all.disable_ipv6'):
                for interface in ('lo', 'ens3'):
                    self.values[f'net.ipv6.conf.{interface}.disable_ipv6'] = value
            return value
        if args == ('ip', '-j', 'link', 'show'):
            return json.dumps([{'ifname': 'lo'}, {'ifname': 'ens3'}])
        if args == ('ip', '-j', '-4', 'route', 'get', '192.0.2.10'):
            return '[{"dev":"ens3","prefsrc":"192.0.2.1"}]'
        if args == ('ip', '-j', '-4', 'route', 'get', '1.1.1.1'):
            return '[{"dev":"ens3","prefsrc":"192.0.2.1"}]'
        if args == ('networkctl', 'status', '--no-pager', '--full', 'ens3'):
            return 'Network File: /run/systemd/network/10-netplan-ens3.network\n'
        if args == ('networkctl', 'reload'):
            self.reloads += 1
            if self.fail == 'networkctl reload' and self.reloads == 1:
                raise InstallError('networkctl reload failed')
            return ''
        if args == ('ss', '-H', '-6', '-tnp', 'state', 'established'):
            return ''
        if args == ('ip', '-j', '-6', 'address', 'show', 'scope', 'global'):
            return '[]'
        if args == ('ip', '-j', '-6', 'address', 'show'):
            return self.addresses
        if args == ('ss', '-H', '-6', '-lnt'):
            return self.listeners6
        if args == ('ip', '-j', '-4', 'address', 'show', 'scope', 'global'):
            return '[{"ifname":"ens3","addr_info":[{"local":"192.0.2.1"}]}]'
        if args and args[0] == 'curl':
            return '192.0.2.1'
        raise AssertionError(args)


class IPv6Tests(unittest.TestCase):
    def paths(self, root):
        etc = root / 'etc'; run_root = root / 'run'
        (etc / 'sysctl.d').mkdir(parents=True)
        (run_root / 'sysctl.d').mkdir(parents=True)
        return etc, run_root, etc / 'sysctl.d' / '99-zz-bob-auto-xray-ipv6.conf'

    def apply(self, kernel, etc, run_root, config):
        with patch('selfsni.ipv6.socket.getaddrinfo', return_value=[
                (socket.AF_INET, None, None, None, ('192.0.2.1', 80))]):
            return ipv6.configure('test.example', kernel, config, etc, run_root,
                                  '192.0.2.10 12345 192.0.2.1 22')

    def test_apply_and_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            self.apply(kernel, etc, run_root, config)
            self.apply(kernel, etc, run_root, config)
            self.assertEqual(config.read_text(), ipv6.CONTENT)
            dropin = etc / 'systemd/network/10-netplan-ens3.network.d' / ipv6.NETWORK_DROPIN
            self.assertEqual(dropin.read_text(), ipv6.NETWORK_CONTENT)
            self.assertEqual(ipv6.verify(kernel, config, etc, run_root,
                                         '192.0.2.10 12345 192.0.2.1 22')['interfaces'],
                             ['all', 'default', 'lo', 'ens3'])
            self.assertEqual(ipv6.verify(kernel, config, etc, run_root)['interfaces'],
                             ['all', 'default', 'lo', 'ens3'])

    def test_dropin_directory_is_readable_by_networkd_under_installer_umask(self):
        # VPS E2E 2026-11-01: a 0700 directory made networkd skip the drop-in at boot
        # ("Failed to chase and open directory ... Permission denied") and write
        # disable_ipv6=0 on ens3.
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as d:
                etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
                directory = etc / 'systemd/network/10-netplan-ens3.network.d'
                old = os.umask(0o077)
                try:
                    if existing:
                        directory.mkdir(parents=True)
                        (directory / ipv6.NETWORK_DROPIN).write_text(ipv6.NETWORK_CONTENT)
                    self.apply(kernel, etc, run_root, config)
                finally:
                    os.umask(old)
                self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
                self.assertEqual((directory / ipv6.NETWORK_DROPIN).stat().st_mode & 0o777, 0o644)

    def test_verify_refuses_dropin_hidden_from_networkd(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            self.apply(kernel, etc, run_root, config)
            directory = etc / 'systemd/network/10-netplan-ens3.network.d'
            directory.chmod(0o700)
            with self.assertRaisesRegex(InstallError, 'not readable by systemd-networkd'):
                ipv6.verify(kernel, config, etc, run_root, '192.0.2.10 12345 192.0.2.1 22')

    def test_foreign_assignment_refused_without_write(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            (etc / 'sysctl.conf').write_text('net.ipv6.conf.ens3.disable_ipv6 = 0\n')
            with self.assertRaisesRegex(InstallError, 'Foreign persistent'):
                self.apply(kernel, etc, run_root, config)
            self.assertFalse(config.exists())

    def test_sysctl_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d))
            kernel = Kernel('net.ipv6.conf.default.disable_ipv6')
            with self.assertRaises(InstallError):
                self.apply(kernel, etc, run_root, config)
            self.assertFalse(config.exists())
            self.assertFalse((etc / 'systemd/network/10-netplan-ens3.network.d' /
                              ipv6.NETWORK_DROPIN).exists())
            self.assertEqual(kernel.values['net.ipv6.conf.all.disable_ipv6'], '0')

    def test_foreign_networkd_dropin_refused(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            directory = etc / 'systemd/network/10-netplan-ens3.network.d'
            directory.mkdir(parents=True)
            (directory / '80-foreign.conf').write_text('[Network]\nLinkLocalAddressing=ipv6\n')
            with self.assertRaisesRegex(InstallError, 'Foreign networkd'):
                self.apply(kernel, etc, run_root, config)
            self.assertFalse(config.exists())

    def test_rollback_restores_both_files(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            _, before = self.apply(kernel, etc, run_root, config)
            ipv6.rollback(before, kernel, config)
            self.assertFalse(config.exists())
            self.assertFalse((etc / 'systemd/network/10-netplan-ens3.network.d' /
                              ipv6.NETWORK_DROPIN).exists())
            self.assertEqual(kernel.values['net.ipv6.conf.ens3.disable_ipv6'], '0')

    def test_networkd_reload_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d))
            kernel = Kernel('networkctl reload')
            with self.assertRaisesRegex(InstallError, 'networkctl reload failed'):
                self.apply(kernel, etc, run_root, config)
            self.assertFalse(config.exists())
            self.assertFalse((etc / 'systemd/network/10-netplan-ens3.network.d' /
                              ipv6.NETWORK_DROPIN).exists())
            self.assertEqual(kernel.reloads, 2)

    def test_reinitialized_interface_cannot_pass_verification_if_reenabled(self):
        # Model the observed lifecycle: sysctl succeeds, then a network manager
        # reinitializes ens3 and overwrites its per-interface value.
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            self.apply(kernel, etc, run_root, config)
            kernel.values['net.ipv6.conf.ens3.disable_ipv6'] = '0'
            with self.assertRaisesRegex(InstallError, 'IPv6 remains enabled'):
                ipv6.verify(kernel, config, etc, run_root)
            # A permitted repeat reapplies the policy without cycling the link.
            self.apply(kernel, etc, run_root, config)
            self.assertEqual(kernel.values['net.ipv6.conf.ens3.disable_ipv6'], '1')

    def test_link_local_is_not_hidden_by_global_address_filter(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            self.apply(kernel, etc, run_root, config)
            def command(*args):
                if args == ('ip', '-j', '-6', 'address', 'show'):
                    return '[{"ifname":"ens3","addr_info":[{"local":"fe80::1","scope":"link"}]}]'
                return kernel(*args)
            with self.assertRaisesRegex(InstallError, 'including link-local'):
                ipv6.verify(command, config, etc, run_root)

    def test_legacy_owned_dropin_can_be_extended_and_rolled_back(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            dropin = etc / 'systemd/network/10-netplan-ens3.network.d' / ipv6.NETWORK_DROPIN
            dropin.parent.mkdir(parents=True)
            dropin.write_text(ipv6.LEGACY_NETWORK_CONTENT)
            _, before = self.apply(kernel, etc, run_root, config)
            self.assertIn('IPv6LinkLocalAddressGenerationMode=none', dropin.read_text())
            ipv6.rollback(before, kernel, config)
            self.assertEqual(dropin.read_text(), ipv6.LEGACY_NETWORK_CONTENT)

    def test_foreign_address_generation_policy_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            directory = etc / 'systemd/network/10-netplan-ens3.network.d'
            directory.mkdir(parents=True)
            (directory / '80-foreign.conf').write_text(
                '[Network]\nIPv6LinkLocalAddressGenerationMode=stable-privacy\n')
            with self.assertRaisesRegex(InstallError, 'Foreign networkd'):
                self.apply(kernel, etc, run_root, config)
            self.assertFalse(config.exists())

    def test_ipv6_ssh_and_aaaa_refused(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            with patch('selfsni.ipv6.socket.getaddrinfo', return_value=[
                    (socket.AF_INET, None, None, None, ('192.0.2.1', 80))]):
                with self.assertRaisesRegex(InstallError, 'IPv4 SSH'):
                    ipv6.configure('test.example', kernel, config, etc, run_root,
                                   '2001:db8::1 1 2001:db8::2 22')
                report = ipv6.preflight('test.example', kernel, etc, run_root, config,
                                        '2001:db8::1 1 2001:db8::2 22')
            self.assertEqual((report['mode'], report['reason']), ('skipped', 'ssh'))
            self.assertFalse(config.exists())
            with patch('selfsni.ipv6.socket.getaddrinfo', return_value=[
                    (socket.AF_INET6, None, None, None, ('2001:db8::1', 80, 0, 0))]):
                with self.assertRaisesRegex(InstallError, 'AAAA'):
                    ipv6.preflight('test.example', kernel, etc, run_root, config,
                                   '192.0.2.10 1 192.0.2.1 22')

    def test_other_active_ipv6_ssh_refused(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            def command(*args):
                if args == ('ss', '-H', '-6', '-tnp', 'state', 'established'):
                    return 'ESTAB 0 0 [2001:db8::1]:22 [2001:db8::2]:1000 users:(("sshd"))'
                return kernel(*args)
            with patch('selfsni.ipv6.socket.getaddrinfo', return_value=[
                    (socket.AF_INET, None, None, None, ('192.0.2.1', 80))]):
                with self.assertRaisesRegex(InstallError, 'active IPv6 SSH'):
                    ipv6.configure('test.example', command, config, etc, run_root,
                                   '192.0.2.10 1 192.0.2.1 22')
            self.assertFalse(config.exists())
            self.assertEqual(kernel.values['net.ipv6.conf.all.disable_ipv6'], '0')


# Exactly what the official VPS-Server-Menu ipv6-menu appends to /etc/sysctl.conf.
MENU_SYSCTL = ('# Kernel sysctl configuration\n\n'
               'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n')
SSH = '192.0.2.10 12345 192.0.2.1 22'


class BestEffortTests(unittest.TestCase):
    """ipv6.apply(): managed, external or skipped; fatal only for unknown state or exposure."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.etc, self.run_root, self.config = IPv6Tests.paths(None, root)
        self.ufw = root / 'default-ufw'
        self.ufw.write_text('# /etc/default/ufw\nIPV6=yes\nDEFAULT_INPUT_POLICY="DROP"\n')
        self.dropin = self.etc / 'systemd/network/10-netplan-ens3.network.d' / ipv6.NETWORK_DROPIN
        self.kernel = Kernel()

    def apply(self, command=None, ssh=SSH):
        with patch('selfsni.ipv6.socket.getaddrinfo', return_value=[
                (socket.AF_INET, None, None, None, ('192.0.2.1', 80))]):
            return ipv6.apply('test.example', [22], command or self.kernel, self.config, self.etc,
                              self.run_root, ssh, self.ufw)

    def disable_runtime(self):
        for key in list(self.kernel.values):
            if key.endswith('.disable_ipv6'):
                self.kernel.values[key] = '1'

    def assertUntouched(self, runtime=None):
        self.assertFalse(self.config.exists())
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.dropin.parent.exists())
        if runtime is not None:
            self.assertEqual({k: v for k, v in self.kernel.values.items() if 'ipv6' in k}, runtime)

    def runtime(self):
        return {k: v for k, v in self.kernel.values.items() if 'ipv6' in k}

    def test_matching_menu_sysctl_with_disabled_ipv6_is_external_without_writes(self):
        (self.etc / 'sysctl.conf').write_text(MENU_SYSCTL)
        self.disable_runtime()
        before = self.runtime()
        report = self.apply()
        self.assertEqual((report['mode'], report['reason']), ('external', 'external'))
        self.assertTrue(report['runtimeDisabled'])
        self.assertEqual(report['foreignAssignments'],
                         [f'{self.etc}/sysctl.conf:3', f'{self.etc}/sysctl.conf:4'])
        self.assertEqual(report['exposure']['ufwIPv6'], True)
        self.assertEqual((self.etc / 'sysctl.conf').read_text(), MENU_SYSCTL)
        self.assertUntouched(before)
        self.assertEqual(self.kernel.reloads, 0)

    def test_matching_values_are_not_conflicts(self):
        (self.etc / 'sysctl.conf').write_text(MENU_SYSCTL)
        self.assertEqual(ipv6._foreign(self.etc, self.run_root, self.config), [])

    def test_matching_menu_sysctl_with_ipv6_back_after_reboot_is_skipped(self):
        # sysctl.conf alone does not hold: networkd re-enables link-local at boot.
        (self.etc / 'sysctl.conf').write_text(MENU_SYSCTL)
        self.disable_runtime()
        self.kernel.values['net.ipv6.conf.ens3.disable_ipv6'] = '0'
        self.kernel.addresses = '[{"ifname":"ens3","addr_info":[{"local":"fe80::1","scope":"link"}]}]'
        before = self.runtime()
        report = self.apply()
        self.assertEqual((report['mode'], report['reason']), ('skipped', 'external'))
        self.assertFalse(report['runtimeDisabled'])
        self.assertUntouched(before)

    def test_contradicting_sysctl_is_skipped_and_left_untouched(self):
        for location in ('sysctl.conf', 'sysctl.d/60-operator.conf'):
            with self.subTest(location=location):
                self.setUp()
                text = 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.ens3.disable_ipv6=0\n'
                (self.etc / location).write_text(text)
                before = self.runtime()
                report = self.apply()
                self.assertEqual((report['mode'], report['reason']), ('skipped', 'conflict'))
                self.assertIn(f'{self.etc}/{location}:2', report['detail'])
                self.assertEqual((self.etc / location).read_text(), text)
                self.assertUntouched(before)

    def test_skipped_without_ipv6_filtering_firewall_is_fatal_and_writes_nothing(self):
        (self.etc / 'sysctl.conf').write_text('net.ipv6.conf.all.disable_ipv6 = 0\n')
        for content in ('IPV6=no\n', 'IPV6=yes\nIPV6=no\n', None):
            with self.subTest(content=content):
                if content is None:
                    self.ufw.unlink()
                else:
                    self.ufw.write_text(content)
                with self.assertRaisesRegex(InstallError, 'UFW does not filter IPv6'):
                    self.apply()
                self.assertUntouched()
        # preflight already refuses when the firewall is known not to cover IPv6.
        self.ufw.write_text('IPV6=no\n')
        with patch('selfsni.ipv6.socket.getaddrinfo', return_value=[
                (socket.AF_INET, None, None, None, ('192.0.2.1', 80))]), \
                self.assertRaisesRegex(InstallError, 'UFW does not filter IPv6'):
            ipv6.preflight('test.example', self.kernel, self.etc, self.run_root, self.config,
                           SSH, self.ufw)

    def test_internal_port_on_public_ipv6_is_fatal(self):
        (self.etc / 'sysctl.conf').write_text('net.ipv6.conf.all.disable_ipv6 = 0\n')
        self.kernel.listeners6 = 'LISTEN 0 4096 [::]:62789 [::]:*\n'
        with self.assertRaisesRegex(InstallError, 'public IPv6 address: 62789'):
            self.apply()
        self.kernel.listeners6 = 'LISTEN 0 4096 *:2053 *:*\n'
        with self.assertRaisesRegex(InstallError, '2053'):
            self.apply()
        # Loopback, link-local and the published SSH/HTTP/REALITY ports are not exposure.
        self.kernel.listeners6 = ('LISTEN 0 4096 [::1]:62789 [::]:*\n'
                                  'LISTEN 0 4096 [fe80::1%ens3]:546 [::]:*\n'
                                  'LISTEN 0 128 [::]:22 [::]:*\nLISTEN 0 511 [::]:80 [::]:*\n'
                                  'LISTEN 0 511 *:443 *:*\n')
        self.assertEqual(self.apply()['mode'], 'skipped')

    def test_partial_failure_rolls_back_completely_and_skips(self):
        for fail in ('net.ipv6.conf.ens3.disable_ipv6', 'networkctl reload'):
            with self.subTest(fail=fail):
                self.setUp()
                calls = []

                def command(*args, kernel=self.kernel):
                    calls.append(args)
                    if fail == 'net.ipv6.conf.ens3.disable_ipv6' and args == (
                            'sysctl', '-w', 'net.ipv6.conf.ens3.disable_ipv6=1'):
                        raise InstallError('sysctl failed')
                    return kernel(*args)
                if fail == 'networkctl reload':
                    self.kernel.fail = fail
                before = self.runtime()
                report = self.apply(command)
                self.assertEqual((report['mode'], report['reason']), ('skipped', 'apply-failed'))
                self.assertIn('rolled back', report['detail'])
                self.assertUntouched(before)
                # The policy had reached the kernel before the failure in the first case.
                if fail != 'networkctl reload':
                    self.assertIn(('sysctl', '-w', 'net.ipv6.conf.all.disable_ipv6=1'), calls)

    def test_failed_rollback_is_fatal(self):
        kernel = Kernel('net.ipv6.conf.default.disable_ipv6')  # fails on apply and on rollback
        with self.assertRaisesRegex(ipv6.RollbackError, 'IPv6 rollback incomplete: sysctl failed'):
            self.apply(kernel)

    def test_rollback_that_does_not_restore_runtime_is_fatal(self):
        # e.g. networkd rewrites a value behind the rollback: the state is unknown.
        def command(*args):
            if args == ('sysctl', '-w', 'net.ipv6.conf.all.disable_ipv6=0'):
                return '0'  # accepted, but the value stays 1
            if args == ('curl', '-4fsS', '--connect-timeout', '10', '--max-time', '20',
                        'https://api.ipify.org'):
                raise InstallError('curl exited 7')
            return self.kernel(*args)
        with self.assertRaisesRegex(ipv6.RollbackError, 'runtime differs: net.ipv6.conf.all'):
            self.apply(command)

    def test_interrupt_mid_apply_rolls_back_and_propagates(self):
        def command(*args):
            if args == ('sysctl', '-w', 'net.ipv6.conf.lo.disable_ipv6=1'):
                raise KeyboardInterrupt
            return self.kernel(*args)
        before = self.runtime()
        with self.assertRaises(KeyboardInterrupt):
            self.apply(command)
        self.assertUntouched(before)

    def test_active_ipv6_ssh_is_never_cut(self):
        established = 'ESTAB 0 0 [2001:db8::1]:22 [2001:db8::2]:1000 users:(("sshd",pid=7,fd=4))'
        for case in ('ipv6 session', 'ssh over ipv6', 'no SSH_CONNECTION', 'appears during apply'):
            with self.subTest(case=case):
                self.setUp()
                seen = []

                def command(*args):
                    if args == ('ss', '-H', '-6', '-tnp', 'state', 'established'):
                        seen.append(args)
                        if case == 'ipv6 session' or (case == 'appears during apply' and len(seen) > 1):
                            return established
                        return ''
                    return self.kernel(*args)
                ssh = {'ssh over ipv6': '2001:db8::2 1000 2001:db8::1 22',
                       'no SSH_CONNECTION': ''}.get(case, SSH)
                before = self.runtime()
                report = self.apply(command, ssh)
                self.assertEqual((report['mode'], report['reason']), ('skipped', 'ssh'))
                self.assertUntouched(before)

    def test_non_netplan_interface_is_skipped(self):
        def command(*args):
            if args[:2] == ('networkctl', 'status'):
                return 'Network File: n/a\n'
            return self.kernel(*args)
        report = self.apply(command)
        self.assertEqual((report['mode'], report['reason']), ('skipped', 'topology'))
        self.assertUntouched()

    def test_symlinked_managed_paths_stay_fatal(self):
        target = self.etc / 'elsewhere'
        target.write_text(ipv6.CONTENT)
        self.config.symlink_to(target)
        with self.assertRaisesRegex(InstallError, 'symlink'):
            self.apply()
        self.config.unlink()
        self.dropin.parent.mkdir(parents=True)
        self.dropin.symlink_to(target)
        with self.assertRaisesRegex(InstallError, 'symlink'):
            self.apply()
        self.assertEqual(target.read_text(), ipv6.CONTENT)

    def test_managed_install_and_repeat(self):
        first = self.apply()
        self.assertEqual(first['mode'], 'managed')
        self.assertEqual(first['persistentConfig'], str(self.config))
        self.assertEqual(self.config.read_text(), ipv6.CONTENT)
        # Repeat (resume or re-run): Bob owns the file, agreeing foreign values are harmless.
        (self.etc / 'sysctl.conf').write_text(MENU_SYSCTL)
        self.assertEqual(self.apply()['mode'], 'managed')
        self.assertEqual(ipv6.verify(self.kernel, self.config, self.etc, self.run_root, SSH)['mode'],
                         'managed')

    def test_ufw_defaults_parsing(self):
        for text, expected in (('IPV6=yes\n', True), ('IPV6="yes" # default\n', True),
                               ('IPV6=no\n', False), ('#IPV6=yes\n', False), ('', False),
                               ('IPV6=yes\nIPV6=no\n', False)):
            with self.subTest(text=text):
                self.ufw.write_text(text)
                self.assertEqual(ipv6.ufw_filters_ipv6(self.ufw), expected)
        link = self.ufw.with_name('link')
        link.symlink_to(self.ufw)
        self.ufw.write_text('IPV6=yes\n')
        self.assertFalse(ipv6.ufw_filters_ipv6(link))


EXTERNAL = {'mode': 'external', 'reason': 'external', 'runtimeDisabled': True,
            'detail': 'Foreign persistent IPv6 sysctl assignment: /etc/sysctl.conf:3, /etc/sysctl.conf:4',
            'foreignAssignments': ['/etc/sysctl.conf:3', '/etc/sysctl.conf:4'],
            'interfaces': ['all', 'default', 'lo', 'ens3'],
            'exposure': {'ufwIPv6': True, 'publicPorts': [22, 80, 443]}}


class InstallerStageTests(unittest.TestCase):
    """main() stage 5: BBR/FQ and swap never depend on the IPv6 result."""

    def run_installer(self, apply=None, bbr=None, bbr_result=None, swap_result=None):
        import selfsni.acceptance as acceptance
        from test_warp_modes import state
        self.ipv6 = MagicMock(apply=MagicMock(side_effect=apply or (lambda *a: dict(EXTERNAL))))
        self.bbr = MagicMock(apply=MagicMock(side_effect=bbr, return_value=bbr_result or {'runtime': 'bbr'}))
        self.swap = MagicMock(apply=MagicMock(return_value=swap_result or {
            'status': 'created', 'path': '/swapfile', 'bytes': 2 * 1024**3}))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(provision, 'STATE', root / 'state'), \
                 patch.object(provision, 'DIAGNOSTIC_LOG', root / 'install.log'), \
                 patch.object(provision, 'INSTALL_LOCK', root / 'install.lock'), \
                 patch.object(provision, 'CONTROLLED_PATHS', []), \
                 patch.object(provision, 'preflight', return_value=state('skip')), \
                 patch.object(provision, 'AptRunner'), \
                 patch.object(provision, 'run', return_value='versions'), \
                 patch.object(provision, 'firewall'), \
                 patch.multiple(provision, ipv6=self.ipv6, bbr=self.bbr, swap=self.swap), \
                 patch.multiple(provision, certificate=DEFAULT, decoy=DEFAULT, panel=DEFAULT,
                                security=DEFAULT, renew_hook=DEFAULT), \
                 patch.object(provision, 'install_doctor_cli', return_value={'status': 'installed'}), \
                 patch.object(acceptance, 'verify', return_value={'checks': [{'passed': True}]}), \
                 patch('sys.argv', ['install', '--domain', 'vpn.example.com', '--no-warp']), \
                 patch('sys.stdout', out):
                try:
                    provision.main()
                    code = 0
                except SystemExit as error:
                    code = error.code
            saved = json.loads((root / 'state/state.json').read_text())
        return code, saved, out.getvalue()

    def test_external_ipv6_keeps_bbr_and_continues(self):
        code, saved, output = self.run_installer()
        self.assertEqual(code, 0)
        self.assertEqual(saved['status'], 'HEALTHY_LOCAL')
        self.assertEqual(saved['ipv6']['mode'], 'external')
        self.assertEqual(saved['bbr'], {'runtime': 'bbr'})
        self.ipv6.apply.assert_called_once_with('vpn.example.com', [22])
        lines = [line.strip() for line in output.splitlines()]
        start = lines.index('⚠ IPv6: настройка пропущена — обнаружена внешняя конфигурация.')
        self.assertLess(start, lines.index('✓ TCP BBR/FQ: настроены.'))
        self.assertLess(lines.index('✓ TCP BBR/FQ: настроены.'), lines.index('✓ Установка VPN продолжается.'))
        self.assertNotIn('IPv4-only', output)
        self.assertNotIn('Настройка IPv6, TCP BBR', output.split('⚠ IPv6')[1])

    def test_managed_ipv6_is_reported_as_ipv4_only(self):
        code, saved, output = self.run_installer(apply=lambda *a: {'mode': 'managed'})
        self.assertEqual(code, 0)
        self.assertIn('✓ IPv6: отключён и закреплён (IPv4-only).', output)
        self.assertNotIn('Установка VPN продолжается', output)

    def test_failed_ipv6_rollback_stops_install_but_keeps_bbr_and_swap(self):
        def fail(*args):
            raise ipv6.RollbackError('IPv6 rollback incomplete: sysctl failed')
        code, saved, output = self.run_installer(apply=fail)
        self.assertEqual(code, 1)
        self.assertEqual((saved['status'], saved['stage']), ('FAILED', 'BBR'))
        self.assertIn('IPv6 rollback incomplete', saved['failure'])
        self.assertEqual(saved['bbr'], {'runtime': 'bbr'})
        self.assertEqual(saved['swap']['status'], 'created')
        self.assertNotIn('ipv6', saved)
        self.bbr.rollback.assert_not_called()
        self.swap.rollback.assert_not_called()
        self.assertNotIn('Установка VPN продолжается', output)

    def test_vps_server_menu_host_installs_with_every_setting_left_to_the_menu(self):
        # The reported VPS: IPv6, BBR/FQ and swap were all set up by VPS-Server-Menu.
        code, saved, output = self.run_installer(
            bbr_result={'mode': 'external', 'reason': 'external',
                        'detail': 'configured in /etc/sysctl.conf:1, /etc/sysctl.conf:2'},
            swap_result={'status': 'external', 'path': '/swapfile', 'reason': 'foreign'})
        self.assertEqual(code, 0)
        self.assertEqual(saved['status'], 'HEALTHY_LOCAL')
        self.assertEqual((saved['bbr']['mode'], saved['swap']['status']), ('external', 'external'))
        for line in ('⚠ IPv6: настройка пропущена — обнаружена внешняя конфигурация.',
                     '✓ TCP BBR/FQ: уже включены внешней конфигурацией; Bob её не менял.',
                     '✓ Swap: уже есть (/swapfile настроен не Bob), не изменён.',
                     '✓ Установка VPN продолжается.'):
            self.assertIn(line, [x.strip() for x in output.splitlines()])
        self.assertNotIn('TCP BBR/FQ: настроены', output)

    def test_skipped_bbr_is_a_warning_not_a_failure(self):
        code, saved, output = self.run_installer(
            apply=lambda *a: {'mode': 'managed'},
            bbr_result={'mode': 'skipped', 'reason': 'container', 'detail': 'restricted container: lxc'})
        self.assertEqual(code, 0)
        self.assertIn('⚠ TCP BBR/FQ: настройка пропущена — контейнер без управления sysctl ядра.', output)
        self.assertIn('✓ Установка VPN продолжается.', output)

    def test_bbr_failure_never_touches_ipv6(self):
        code, saved, _ = self.run_installer(bbr=InstallError('modprobe tcp_bbr failed'))
        self.assertEqual(code, 1)
        self.ipv6.apply.assert_not_called()
        self.ipv6.rollback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
