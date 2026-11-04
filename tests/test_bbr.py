import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from selfsni import bbr
from selfsni.core import InstallError


class FakeKernel:
    def __init__(self, qdisc='fq_codel', congestion='cubic',
                 available=('reno', 'cubic', 'bbr'), module='loadable'):
        self.values = {
            'net.core.default_qdisc': qdisc,
            'net.ipv4.tcp_congestion_control': congestion,
            'net.ipv4.tcp_available_congestion_control': ' '.join(available),
        }
        self.module = module
        self.calls = []
        self.fail_once = None

    def __call__(self, *args):
        self.calls.append(tuple(args))
        if args == ('uname', '-r'):
            return '6.8.0-test\n'
        if args == ('systemd-detect-virt',):
            return 'kvm\n'
        if args[:3] == ('modinfo', '-F', 'filename'):
            if self.module == 'built-in':
                return '(builtin)\n'
            if self.module == 'loadable':
                return '/lib/modules/test/kernel/net/ipv4/tcp_bbr.ko.zst\n'
            raise InstallError('modinfo exited 1')
        if args == ('modprobe', 'tcp_bbr'):
            names = self.values['net.ipv4.tcp_available_congestion_control'].split()
            if 'bbr' not in names:
                names.append('bbr')
            self.values['net.ipv4.tcp_available_congestion_control'] = ' '.join(names)
            return ''
        if args[:2] == ('sysctl', '-n'):
            return self.values[args[2]] + '\n'
        if args[:2] == ('sysctl', '-w'):
            key, value = args[2].split('=', 1)
            if self.fail_once == key:
                self.fail_once = None
                raise InstallError('sysctl exited 1')
            self.values[key] = value
            return key + ' = ' + value + '\n'
        if args == ('ip', '-j', 'route', 'show', 'default'):
            return '[{"dev":"eth0"}]\n'
        if args == ('tc', '-j', 'qdisc', 'show', 'dev', 'eth0'):
            return '[{"kind":"noqueue","dev":"eth0"}]\n'
        raise AssertionError(args)


def socket_report(available=None):
    alternatives = [name for name in (available or []) if name != 'bbr']
    alternative = ({'requested': alternatives[0], 'selected': alternatives[0]}
                   if alternatives else None)
    return {'connectedSocketCongestionControl': 'bbr',
            'explicitAlternative': alternative}


class BbrTests(unittest.TestCase):
    def paths(self, root):
        etc = root / 'etc'
        run_root = root / 'run'
        (etc / 'sysctl.d').mkdir(parents=True)
        (run_root / 'sysctl.d').mkdir(parents=True)
        return (etc, run_root, root / 'managed.conf', root / 'module.conf')

    def configure(self, kernel, paths):
        etc, run_root, managed, module = paths
        with patch('selfsni.bbr.socket_evidence', side_effect=socket_report):
            return bbr.configure(kernel, managed, module, etc, run_root)

    def test_fresh_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); paths = self.paths(root); kernel = FakeKernel()
            report = self.configure(kernel, paths)
            self.assertEqual(report['runtime'], bbr.DESIRED)
            self.assertEqual(paths[2].read_text(),
                             'net.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr\n')
            self.assertEqual(paths[3].read_text(), 'tcp_bbr\n')

    def test_already_enabled_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory)); kernel = FakeKernel('fq', 'bbr')
            first = self.configure(kernel, paths)
            first_bytes = paths[2].read_bytes()
            second = self.configure(kernel, paths)
            self.assertEqual(first['runtime'], second['runtime'])
            self.assertEqual(first_bytes, paths[2].read_bytes())

    def test_loadable_module_is_loaded_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            kernel = FakeKernel(available=('reno', 'cubic'), module='loadable')
            report = self.configure(kernel, paths)
            self.assertTrue(report['moduleLoadedByInstaller'])
            self.assertIn(('modprobe', 'tcp_bbr'), kernel.calls)
            self.assertEqual(paths[3].read_text(), 'tcp_bbr\n')

    def test_built_in_bbr_needs_no_module_file(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            kernel = FakeKernel(available=('reno', 'cubic', 'bbr'), module='built-in')
            self.configure(kernel, paths)
            self.assertFalse(paths[3].exists())
            self.assertNotIn(('modprobe', 'tcp_bbr'), kernel.calls)

    def test_unavailable_bbr_fails_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); etc, run_root, _, _ = self.paths(root)
            kernel = FakeKernel(available=('reno', 'cubic'), module='unknown')
            with self.assertRaisesRegex(InstallError, 'no standard tcp_bbr module'):
                bbr.preflight(kernel, etc, run_root)

    def test_sysctl_file_write_denied_leaves_no_files(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory)); kernel = FakeKernel()
            with patch('selfsni.bbr.atomic_write', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    self.configure(kernel, paths)
            self.assertFalse(paths[2].exists())
            self.assertFalse(paths[3].exists())
            self.assertEqual(kernel.values['net.core.default_qdisc'], 'fq_codel')

    def test_foreign_persistent_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); etc, run_root, _, _ = self.paths(root)
            foreign = etc / 'sysctl.d/80-foreign.conf'
            foreign.write_text('net.ipv4.tcp_congestion_control = cubic\n')
            with self.assertRaisesRegex(InstallError, 'Conflicting persistent'):
                bbr.preflight(FakeKernel(), etc, run_root)

    def test_foreign_duplicate_is_rejected_instead_of_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); etc, run_root, _, _ = self.paths(root)
            foreign = etc / 'sysctl.conf'
            foreign.write_text('net.core.default_qdisc=fq\n')
            with self.assertRaisesRegex(InstallError, 'already configured in a foreign'):
                bbr.preflight(FakeKernel(), etc, run_root)

    def test_apply_failure_rolls_back_runtime_and_files(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory)); kernel = FakeKernel()
            paths[2].write_text('previous managed content\n')
            paths[3].write_text('previous_module\n')
            kernel.fail_once = 'net.ipv4.tcp_congestion_control'
            with self.assertRaises(InstallError):
                self.configure(kernel, paths)
            self.assertEqual(kernel.values['net.core.default_qdisc'], 'fq_codel')
            self.assertEqual(kernel.values['net.ipv4.tcp_congestion_control'], 'cubic')
            self.assertEqual(paths[2].read_text(), 'previous managed content\n')
            self.assertEqual(paths[3].read_text(), 'previous_module\n')

    def test_persistent_config_survives_repeated_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory)); kernel = FakeKernel()
            self.configure(kernel, paths)
            with patch('selfsni.bbr.socket_evidence', side_effect=socket_report):
                first = bbr.verify(kernel, paths[2], paths[0], paths[1])
                second = bbr.verify(kernel, paths[2], paths[0], paths[1])
            self.assertEqual(first, second)

    def test_preflight_probe_is_strictly_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); etc, run_root, _, _ = self.paths(root)
            kernel = FakeKernel(available=('reno', 'cubic'), module='loadable')
            report = bbr.preflight(kernel, etc, run_root)
            self.assertFalse(report['bbrAvailable'])
            mutations = [call for call in kernel.calls
                         if call[:2] == ('sysctl', '-w') or call[:1] == ('modprobe',)]
            self.assertEqual(mutations, [])

    def test_install_preflight_can_load_standard_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); etc, run_root, _, _ = self.paths(root)
            kernel = FakeKernel(available=('reno', 'cubic'), module='loadable')
            report = bbr.preflight(kernel, etc, run_root, allow_modprobe=True)
            self.assertTrue(report['bbrAvailable'])
            self.assertTrue(report['moduleLoadedDuringPreflight'])
            self.assertIn(('modprobe', 'tcp_bbr'), kernel.calls)

    def test_missing_modinfo_uses_module_files_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); etc, run_root, _, _ = self.paths(root)
            module = root / 'modules/6.8.0-test/kernel/net/ipv4/tcp_bbr.ko.zst'
            module.parent.mkdir(parents=True)
            module.write_bytes(b'')
            kernel = FakeKernel(available=('reno', 'cubic'), module='unknown')
            report = bbr.preflight(kernel, etc, run_root, module_root=root / 'modules')
            self.assertEqual(report['module'], 'loadable')
            self.assertFalse(report['bbrAvailable'])
            self.assertNotIn(('modprobe', 'tcp_bbr'), kernel.calls)

    def test_qdisc_is_diagnostic_even_when_interface_is_noqueue(self):
        kernel = FakeKernel()
        report = bbr.qdisc_diagnostics(kernel)
        self.assertEqual(report['defaultRouteInterface'], 'eth0')
        self.assertEqual(report['interfaceQdisc'][0]['kind'], 'noqueue')

    def test_restricted_container_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); etc, run_root, _, _ = self.paths(root)
            kernel = FakeKernel()
            original = kernel.__call__
            def docker(*args):
                return 'docker\n' if args == ('systemd-detect-virt',) else original(*args)
            with self.assertRaisesRegex(InstallError, 'restricted container'):
                bbr.preflight(docker, etc, run_root)


# Exactly what the official VPS-Server-Menu ("enable BBR") appends to /etc/sysctl.conf.
MENU_SYSCTL = 'net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n'


class BestEffortTests(unittest.TestCase):
    """bbr.apply(): optional for the installation; raises only after a failed rollback."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = BbrTests.paths(None, Path(directory.name))

    def apply(self, kernel):
        etc, run_root, managed, module = self.paths
        with patch('selfsni.bbr.socket_evidence', side_effect=socket_report):
            return bbr.apply(kernel, managed, module, etc, run_root)

    def assertUntouched(self, kernel, runtime):
        self.assertFalse(self.paths[2].exists())
        self.assertFalse(self.paths[3].exists())
        self.assertEqual({k: kernel.values[k] for k in bbr.DESIRED}, runtime)
        self.assertEqual([c for c in kernel.calls if c[:2] == ('sysctl', '-w') or c[:1] == ('modprobe',)], [])

    def test_menu_sysctl_already_effective_is_external_without_writes(self):
        sysctl = self.paths[0] / 'sysctl.conf'
        sysctl.write_text(MENU_SYSCTL)
        kernel = FakeKernel('fq', 'bbr')
        report = self.apply(kernel)
        self.assertEqual(report['mode'], 'external')
        self.assertIn('sysctl.conf:1', report['detail'])
        self.assertEqual(sysctl.read_text(), MENU_SYSCTL)
        self.assertUntouched(kernel, bbr.DESIRED)

    def test_menu_sysctl_not_effective_is_skipped_not_layered(self):
        (self.paths[0] / 'sysctl.conf').write_text(MENU_SYSCTL)
        kernel = FakeKernel()
        report = self.apply(kernel)
        self.assertEqual((report['mode'], report['reason']), ('skipped', 'external'))
        self.assertUntouched(kernel, {'net.core.default_qdisc': 'fq_codel',
                                      'net.ipv4.tcp_congestion_control': 'cubic'})

    def test_conflict_container_and_unsupported_kernel_are_skipped(self):
        def docker(kernel):
            def command(*args):
                return 'docker\n' if args == ('systemd-detect-virt',) else kernel(*args)
            return command
        cases = {'conflict': (FakeKernel(), 'net.ipv4.tcp_congestion_control = cubic\n'),
                 'container': (FakeKernel(), None),
                 'unsupported': (FakeKernel(available=('reno', 'cubic'), module='unknown'), None)}
        for reason, (kernel, foreign) in cases.items():
            with self.subTest(reason=reason):
                self.setUp()
                if foreign:
                    (self.paths[0] / 'sysctl.d/80-foreign.conf').write_text(foreign)
                report = self.apply(docker(kernel) if reason == 'container' else kernel)
                self.assertEqual((report['mode'], report['reason']), ('skipped', reason))
                self.assertFalse(self.paths[2].exists())

    def test_partial_failure_rolls_back_and_skips(self):
        kernel = FakeKernel()
        kernel.fail_once = 'net.ipv4.tcp_congestion_control'
        report = self.apply(kernel)
        self.assertEqual((report['mode'], report['reason']), ('skipped', 'apply-failed'))
        self.assertIn('rolled back', report['detail'])
        self.assertFalse(self.paths[2].exists())
        self.assertEqual(kernel.values['net.core.default_qdisc'], 'fq_codel')

    def test_failed_rollback_is_fatal(self):
        kernel = FakeKernel()
        original = kernel.__call__

        def command(*args):
            if args == ('sysctl', '-w', 'net.ipv4.tcp_congestion_control=bbr'):
                raise InstallError('sysctl exited 1')
            if args == ('sysctl', '-w', 'net.core.default_qdisc=fq_codel'):
                raise InstallError('sysctl exited 255')
            return original(*args)
        with self.assertRaisesRegex(bbr.RollbackError, 'TCP BBR rollback incomplete'):
            self.apply(command)

    def test_interrupt_rolls_back_and_propagates(self):
        kernel = FakeKernel()
        original = kernel.__call__

        def command(*args):
            if args == ('sysctl', '-w', 'net.ipv4.tcp_congestion_control=bbr'):
                raise KeyboardInterrupt
            return original(*args)
        with self.assertRaises(KeyboardInterrupt):
            self.apply(command)
        self.assertFalse(self.paths[2].exists())
        self.assertEqual(kernel.values['net.core.default_qdisc'], 'fq_codel')

    def test_fresh_host_is_managed(self):
        report = self.apply(FakeKernel())
        self.assertEqual((report['mode'], report['runtime']), ('managed', bbr.DESIRED))

    def test_decision_is_read_only(self):
        (self.paths[0] / 'sysctl.conf').write_text(MENU_SYSCTL)
        kernel = FakeKernel(available=('reno', 'cubic'))
        bbr.decision(kernel, self.paths[0], self.paths[1])
        self.assertUntouched(kernel, {'net.core.default_qdisc': 'fq_codel',
                                      'net.ipv4.tcp_congestion_control': 'cubic'})


if __name__ == '__main__':
    unittest.main()
