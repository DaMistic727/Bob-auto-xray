"""Bob Doctor migration audit P2: CLI installation, delivery, IPv6 scope, Xray pin."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import DEFAULT, MagicMock, patch

from selfsni import doctor, provision
from selfsni.core import InstallError

from test_doctor import FakeProbe, check


class CliInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'lib').mkdir()
        (self.root / 'bin').mkdir()
        self.library = self.root / 'lib/bob-auto-xray'
        self.command = self.root / 'bin/bob'
        for name, value in (('DOCTOR_LIB', self.library), ('DOCTOR_COMMAND', self.command),
                            ('DOCTOR_PYTHON', Path(sys.executable))):
            patcher = patch.object(provision, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(provision.shutil, 'which', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def leftovers(self):
        return sorted([p.name for p in (self.root / 'lib').iterdir()] +
                      [p.name for p in (self.root / 'bin').iterdir()])

    def test_success_publishes_library_and_command(self):
        result = provision.install_doctor_cli()
        self.assertEqual(result, {'status': 'installed', 'command': str(self.command)})
        self.assertTrue((self.library / 'selfsni/doctor.py').is_file())
        self.assertEqual(self.leftovers(), ['bob', 'bob-auto-xray'])
        run = subprocess.run([str(self.command), 'doctor', '--help'], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_copy_failure_is_not_fatal_and_leaves_nothing_partial(self):
        real_copytree = shutil.copytree
        copied = []

        def copy(source, target, *args, **kwargs):
            if len(copied) == 3:
                raise OSError(28, 'No space left on device')
            copied.append(source)
            return shutil.copy2(source, target, *args, **kwargs)

        # copytree binds copy_function at definition time; fail inside a real partial copy.
        def copytree(source, target, **kwargs):
            return real_copytree(source, target, copy_function=copy, **kwargs)
        with patch.object(provision.shutil, 'copytree', side_effect=copytree):
            result = provision.install_doctor_cli()
        self.assertEqual(len(copied), 3)
        self.assertEqual(result, {'status': 'failed', 'command': None})
        self.assertEqual(self.leftovers(), [])

    def test_occupied_library_is_reported_and_left_untouched(self):
        self.library.mkdir()
        (self.library / 'foreign').write_text('keep')
        result = provision.install_doctor_cli()
        self.assertEqual(result, {'status': 'library-occupied', 'command': None})
        self.assertEqual((self.library / 'foreign').read_text(), 'keep')
        self.assertFalse(self.command.exists())

    def test_foreign_command_is_never_replaced_even_in_a_race(self):
        self.command.write_text('foreign')
        result = provision.install_doctor_cli()
        self.assertEqual(result, {'status': 'command-name-taken', 'command': None})
        self.assertEqual(self.command.read_text(), 'foreign')
        self.assertTrue((self.library / 'selfsni/doctor.py').is_file())
        self.command.unlink()
        shutil.rmtree(self.library)
        # Another program creates `bob` after the existence check: link() must not clobber it.
        real_link = os.link

        def link(source, target, *args, **kwargs):
            Path(target).write_text('raced')
            return real_link(source, target, *args, **kwargs)
        with patch.object(provision.os, 'link', side_effect=link):
            result = provision.install_doctor_cli()
        self.assertEqual(result, {'status': 'command-name-taken', 'command': None})
        self.assertEqual(self.command.read_text(), 'raced')
        self.assertEqual(self.leftovers(), ['bob', 'bob-auto-xray'])

    def test_installer_records_result_and_completes_when_cli_fails(self):
        import selfsni.acceptance as acceptance
        from test_warp_modes import state
        for result in ({'status': 'failed', 'command': None},
                       {'status': 'installed', 'command': '/usr/local/bin/bob'}):
            with self.subTest(status=result['status']), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.object(provision, 'STATE', root / 'state'), \
                     patch.object(provision, 'DIAGNOSTIC_LOG', root / 'install.log'), \
                     patch.object(provision, 'INSTALL_LOCK', root / 'install.lock'), \
                     patch.object(provision, 'CONTROLLED_PATHS', []), \
                     patch.object(provision, 'preflight', return_value=state('skip')), \
                     patch.object(provision, 'AptRunner'), \
                     patch.object(provision, 'run', return_value='versions'), \
                     patch.object(provision, 'firewall'), \
                     patch.multiple(provision,
                                    ipv6=MagicMock(configure=MagicMock(return_value=({}, {}))),
                                    bbr=MagicMock(configure=MagicMock(return_value={})),
                                    swap=MagicMock(configure=MagicMock(return_value={'status': 'skipped'}))), \
                     patch.multiple(provision, certificate=DEFAULT, decoy=DEFAULT, panel=DEFAULT,
                                    security=DEFAULT, renew_hook=DEFAULT), \
                     patch.object(provision, 'install_doctor_cli', return_value=result), \
                     patch.object(acceptance, 'verify', return_value={'checks': [{'passed': True}]}), \
                     patch('sys.argv', ['install', '--domain', 'vpn.example.com', '--no-warp']), \
                     patch('sys.stdout', io.StringIO()):
                    provision.main()
                saved = json.loads((root / 'state/state.json').read_text())
                self.assertEqual(saved['status'], 'HEALTHY_LOCAL')
                self.assertEqual(saved['doctor_cli'], result)


class DeliveryTests(unittest.TestCase):
    def test_verified_release_runs_doctor_instead_of_installer(self):
        run = subprocess.run(['bash', 'install.sh', 'doctor', '--help'], capture_output=True,
                             text=True, timeout=60, env=dict(os.environ, PYTHONPATH=''))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Безопасная диагностика', run.stdout)
        self.assertNotIn('автоматическая настройка VPN-сервера', run.stdout)

    def test_release_notes_describe_doctor_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tools').mkdir()
            for name in ('release.py', 'package.py'):
                shutil.copyfile('tools/' + name, root / 'tools' / name)
            shutil.copyfile('install.sh', root / 'install.sh')
            (root / 'versions.lock.json').write_text('{}\n')
            (root / 'VERSION').write_text('1.2.3\n')
            (root / 'CHANGELOG.md').write_text('# Changes\n\n## v1.2.3\n\nCurrent.\n')
            subprocess.run([sys.executable, str(root / 'tools/release.py'), '--tag', 'v1.2.3'],
                           check=True, capture_output=True)
            notes = (root / 'dist/release-notes.md').read_text()
        self.assertNotIn('Bob Doctor (PR #11) не включён', notes)
        self.assertIn('install.sh) doctor', notes)


class Ipv6ScopeTests(unittest.TestCase):
    def test_probe_reads_ipv6_addresses_of_every_scope(self):
        commands = []

        def command(*args, timeout=5):
            commands.append(args)
            if args[:2] == ('ip', '-j'):
                return 0, '[{"ifname":"ens3"}]' if 'link' in args else '[]'
            return 0, '1\n'
        probe = doctor.Probe()
        with patch.object(probe, 'command', side_effect=command), \
             patch.object(doctor.ipv6, '_network_path', return_value=Path('/etc/x.conf')):
            probe.ipv6_snapshot()
        queries = [args for args in commands if args[:3] == ('ip', '-j', '-6')]
        self.assertEqual(queries, [('ip', '-j', '-6', 'address', 'show')])

    def test_link_local_address_is_fail(self):
        probe = FakeProbe('install')
        probe.ipv6_addresses = [{'ifname': 'ens3', 'addr_info': [
            {'family': 'inet6', 'local': 'fe80::1', 'scope': 'link'}]}]
        self.assertEqual(check(doctor.Doctor(probe).run(), 'system.ipv6')['status'], 'FAIL')


class XrayPinTests(unittest.TestCase):
    def run_pin(self, mutate=lambda probe: None):
        probe = FakeProbe('install')
        mutate(probe)
        probe.sync()
        return check(doctor.Doctor(probe).run(), 'core.xrayPin')['status']

    def test_pinned_core_is_verified_against_the_installer_digest(self):
        self.assertEqual(self.run_pin(), 'PASS')
        self.assertEqual(self.run_pin(lambda p: setattr(p, 'xray_digest', '0' * 64)), 'WARN')
        self.assertEqual(self.run_pin(lambda p: setattr(p, 'xray_digest', FileNotFoundError())), 'FAIL')
        self.assertEqual(self.run_pin(lambda p: setattr(p, 'xray_digest', PermissionError())), 'UNKNOWN')
        self.assertEqual(self.run_pin(lambda p: p.state.pop('xray_sha256')), 'SKIP')
        self.assertEqual(self.run_pin(lambda p: p.state.update(xray_sha256='not-a-digest')), 'UNKNOWN')

    def test_digest_is_never_printed(self):
        probe = FakeProbe('install')
        probe.xray_digest = '0' * 64
        text = json.dumps(doctor.Doctor(probe).run())
        self.assertNotIn('f' * 64, text)
        self.assertNotIn('0' * 64, text)


if __name__ == '__main__':
    unittest.main()
