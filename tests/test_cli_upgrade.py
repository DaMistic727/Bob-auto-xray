"""CLI layout on fresh installs and the explicit, ownership-checked CLI-only upgrade."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from selfsni import cli_install, provision

SOURCE = Path(__file__).resolve().parents[1] / 'src/selfsni'
V040_WRAPPER = ('#!/bin/sh\nset -eu\nif [ "${1:-}" = doctor ]; then shift; fi\n'
                'exec /usr/bin/python3 -I -B -c \'import sys; sys.path.insert(0, '
                '"/usr/local/lib/bob-auto-xray"); from selfsni.doctor import main; '
                'sys.exit(main())\' "$@"\n')


def git_tag_tree(tag, target):
    """Extract src/selfsni of a git tag, or return False when the tag is unavailable."""
    root = SOURCE.parents[1]
    try:
        archive = subprocess.run(['git', '-C', str(root), 'archive', tag, 'src/selfsni'],
                                 capture_output=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    subprocess.run(['tar', '-x', '-C', str(target)], input=archive, check=True, timeout=30)
    return True


class Layout(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / 'lib').mkdir()
        (self.root / 'bin').mkdir()
        self.library = self.root / 'lib/bob-auto-xray'
        self.command = self.root / 'bin/bob'
        self.python = Path(sys.executable)
        self.messages = []

    def legacy_node(self):
        """A v0.4.0-shaped node: package without cli.py, legacy wrapper, no manifest."""
        shutil.copytree(SOURCE, self.library / 'selfsni',
                        ignore=shutil.ignore_patterns('__pycache__', 'cli.py', 'cli_install.py'))
        self.library.chmod(0o700)  # v0.4.0 published its mkdtemp() directory as is.
        digest = cli_install.tree_digest(self.library)
        patcher = patch.dict(cli_install.LEGACY_TREES, {digest: '0.4.0'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.command.write_text(cli_install.legacy_wrapper(self.library, self.python))
        self.command.chmod(0o755)
        return digest

    def upgrade(self, version='9.9.9', **kwargs):
        kwargs.setdefault('source', SOURCE)
        return cli_install.upgrade(release_version=version, say=self.messages.append,
                                   library=self.library, command=self.command, python=self.python,
                                   owner=os.getuid(), **kwargs)

    def leftovers(self):
        return sorted(p.name for d in ('lib', 'bin') for p in (self.root / d).iterdir())

    def bob(self, *args):
        return subprocess.run([str(self.command), *args], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL, cwd='/')

    def snapshot(self):
        return (self.command.read_bytes(), cli_install.tree_digest(self.library),
                (self.library / cli_install.MANIFEST).exists())


class WrapperTests(Layout):
    def test_legacy_wrapper_is_byte_exact_v040(self):
        self.assertEqual(cli_install.legacy_wrapper(), V040_WRAPPER)

    def test_real_v040_library_tree_is_recognised(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            if not git_tag_tree('v0.4.0', target):
                self.skipTest('git tag v0.4.0 is not available in this checkout')
            self.assertEqual(cli_install.inspect_library(target / 'src', os.getuid())[0], '0.4.0')

    def test_fresh_install_records_manifest_and_new_wrapper(self):
        with patch.multiple(provision, DOCTOR_LIB=self.library, DOCTOR_COMMAND=self.command,
                            DOCTOR_PYTHON=self.python), \
             patch.object(provision.shutil, 'which', return_value=None):
            self.assertEqual(provision.install_doctor_cli(),
                             {'status': 'installed', 'command': str(self.command)})
        version = (SOURCE.parents[1] / 'VERSION').read_text().strip()
        self.assertEqual(cli_install.inspect_library(self.library, os.getuid())[0], version)
        self.assertEqual(self.command.read_text(), cli_install.wrapper(self.library, self.python))
        self.assertEqual(self.leftovers(), ['bob', 'bob-auto-xray'])
        self.assertEqual(oct(self.library.stat().st_mode & 0o777), '0o755')
        run = self.bob('--version')
        self.assertEqual((run.returncode, run.stdout.strip()), (0, version), run.stderr)
        run = self.bob()
        self.assertEqual(run.returncode, 0)
        self.assertIn('Использование', run.stdout)

    def test_pycache_is_ignored_but_foreign_files_are_not(self):
        self.legacy_node()
        cache = self.library / 'selfsni/__pycache__'
        cache.mkdir()
        (cache / 'doctor.cpython-312.pyc').write_bytes(b'\0')
        self.assertEqual(cli_install.inspect_library(self.library, os.getuid())[0], '0.4.0')
        (cache / 'payload.sh').write_text('x')
        with self.assertRaises(cli_install.NotOwned):
            cli_install.inspect_library(self.library, os.getuid())


class UpgradeTests(Layout):
    def test_v040_node_is_upgraded_and_doctor_contract_kept(self):
        self.legacy_node()
        before = self.bob('doctor', '--json')
        self.assertEqual(self.upgrade(), 0, self.messages)
        self.assertEqual(self.command.read_text(), cli_install.wrapper(self.library, self.python))
        self.assertEqual(cli_install.inspect_library(self.library, os.getuid())[0], '9.9.9')
        self.assertEqual(self.leftovers(), ['bob', 'bob-auto-xray'])
        self.assertEqual(oct(self.command.stat().st_mode & 0o777), '0o755')
        self.assertEqual(self.bob('--version').stdout.strip(), '9.9.9')
        after = self.bob('doctor', '--json')
        self.assertEqual(after.returncode, before.returncode)
        drop = lambda text: {k: v for k, v in json.loads(text).items() if k != 'timestamp'}
        self.assertEqual(drop(after.stdout), drop(before.stdout))
        self.assertIn('VPN, службы и state не изменялись', self.messages[-1])

    def test_second_run_is_a_no_op(self):
        self.legacy_node()
        self.assertEqual(self.upgrade(), 0)
        inode = self.library.stat().st_ino, self.command.stat().st_ino
        self.assertEqual(self.upgrade(), 0)
        self.assertIn('уже актуален', self.messages[-1])
        self.assertEqual((self.library.stat().st_ino, self.command.stat().st_ino), inode)
        self.assertEqual(self.leftovers(), ['bob', 'bob-auto-xray'])

    def test_manifest_install_upgrades_to_next_version(self):
        with patch.multiple(provision, DOCTOR_LIB=self.library, DOCTOR_COMMAND=self.command,
                            DOCTOR_PYTHON=self.python), \
             patch.object(provision.shutil, 'which', return_value=None):
            provision.install_doctor_cli()
        self.assertEqual(self.upgrade('9.9.10'), 0, self.messages)
        self.assertEqual(self.bob('--version').stdout.strip(), '9.9.10')

    def test_check_only_changes_nothing(self):
        self.legacy_node()
        before = self.snapshot()
        self.assertEqual(self.upgrade(check_only=True), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.leftovers(), ['bob', 'bob-auto-xray'])
        self.assertIn('Ничего не изменено', self.messages[-1])

    def assertRefused(self, before=None):
        self.assertEqual(self.upgrade(), cli_install.REFUSED, self.messages)
        self.assertIn('отклонено', self.messages[0])
        if before is not None:
            self.assertEqual((self.command.read_bytes(), sorted(os.listdir(self.library))), before)

    def test_foreign_or_modified_command_is_never_replaced(self):
        self.legacy_node()
        for content in ('#!/bin/sh\necho foreign\n',
                        cli_install.legacy_wrapper(self.library, self.python) + '# edited\n'):
            self.messages.clear()
            self.command.write_text(content)
            self.assertRefused()
            self.assertEqual(self.command.read_text(), content)
        self.command.unlink()
        self.messages.clear()
        self.assertRefused()
        self.assertFalse(self.command.exists())

    def test_symlinked_writable_or_hard_linked_command_is_refused(self):
        self.legacy_node()
        real = self.root / 'real-bob'
        shutil.copy(self.command, real)
        good = self.command.read_bytes()
        self.command.unlink()
        self.command.symlink_to(real)
        self.assertRefused()
        self.assertTrue(self.command.is_symlink())
        self.command.unlink()
        self.command.write_bytes(good)
        self.command.chmod(0o777)
        self.messages.clear()
        self.assertRefused()
        self.command.chmod(0o755)
        os.link(self.command, self.root / 'second')
        self.messages.clear()
        self.assertRefused()

    def test_modified_or_foreign_library_is_refused(self):
        self.legacy_node()
        before = (self.command.read_bytes(), sorted(os.listdir(self.library)))
        doctor_file = self.library / 'selfsni/doctor.py'
        original = doctor_file.read_text()
        doctor_file.write_text(original + '# local edit\n')
        self.assertRefused(before)
        self.assertTrue(doctor_file.read_text().endswith('# local edit\n'))
        doctor_file.write_text(original)
        (self.library / 'foreign.txt').write_text('keep')
        self.messages.clear()
        self.assertRefused()
        self.assertEqual((self.library / 'foreign.txt').read_text(), 'keep')

    def test_symlink_inside_or_instead_of_library_is_refused(self):
        self.legacy_node()
        (self.library / 'selfsni/link.py').symlink_to(self.library / 'selfsni/doctor.py')
        self.assertRefused()
        (self.library / 'selfsni/link.py').unlink()
        moved = self.root / 'lib/real'
        self.library.rename(moved)
        self.library.symlink_to(moved)
        self.messages.clear()
        self.assertRefused()
        self.assertTrue(self.library.is_symlink())

    def test_tampered_manifest_is_refused(self):
        with patch.multiple(provision, DOCTOR_LIB=self.library, DOCTOR_COMMAND=self.command,
                            DOCTOR_PYTHON=self.python), \
             patch.object(provision.shutil, 'which', return_value=None):
            provision.install_doctor_cli()
        (self.library / 'selfsni/cli.py').write_text('raise SystemExit(0)\n')
        self.assertRefused()
        self.assertIn('манифестом', self.messages[0])

    def test_absent_library_is_refused_without_installing(self):
        self.command.write_text(cli_install.legacy_wrapper(self.library, self.python))
        self.command.chmod(0o755)
        self.assertRefused()
        self.assertFalse(self.library.exists())

    def assertRolledBack(self, code, before):
        self.assertEqual(code, cli_install.FAILED, self.messages)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.leftovers(), ['bob', 'bob-auto-xray'])
        self.assertIn('прежняя версия CLI сохранена', self.messages[-1])
        run = self.bob('doctor', '--json')
        self.assertEqual(json.loads(run.stdout)['schemaVersion'], 1)

    def test_failed_verification_rolls_back_library_and_command(self):
        self.legacy_node()
        before = self.snapshot()

        def broken(command, version):
            raise RuntimeError('new CLI does not start')
        self.assertRolledBack(self.upgrade(verify=broken), before)

    def test_broken_staged_package_touches_nothing(self):
        self.legacy_node()
        before = self.snapshot()
        source = self.root / 'broken-source'
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns('__pycache__'))
        (source / 'cli.py').write_text('raise ImportError\n')
        self.assertRolledBack(self.upgrade(source=source), before)

    def test_failed_rename_into_place_restores_library(self):
        self.legacy_node()
        before = self.snapshot()
        real_rename = os.rename
        calls = []

        def rename(source, target):
            calls.append(source)
            if len(calls) == 2:
                raise OSError(28, 'No space left on device')
            return real_rename(source, target)
        with patch.object(cli_install.os, 'rename', side_effect=rename):
            code = self.upgrade()
        self.assertRolledBack(code, before)

    def test_failed_rollback_reports_where_the_old_library_is(self):
        self.legacy_node()
        real_rename = os.rename
        calls = []

        def rename(source, target):
            calls.append(source)
            if len(calls) == 3:  # library -> failed, during rollback
                raise OSError(5, 'I/O error')
            return real_rename(source, target)

        def broken(command, version):
            raise RuntimeError
        with patch.object(cli_install.os, 'rename', side_effect=rename):
            code = self.upgrade(verify=broken)
        self.assertEqual(code, cli_install.ROLLBACK_FAILED)
        backup = next(p for p in (self.root / 'lib').iterdir() if '.old.' in p.name)
        self.assertIn(str(backup), self.messages[-1])
        self.assertTrue((backup / 'selfsni/doctor.py').is_file())
        # The command was restored before the library step failed.
        self.assertEqual(self.command.read_text(), cli_install.legacy_wrapper(self.library, self.python))


class EntryPointTests(unittest.TestCase):
    def run_main(self, argv, **patches):
        stderr = []
        patches.setdefault('ROOT_UID', cli_install.ROOT_UID)
        with patch.multiple(cli_install, **patches), \
             patch('builtins.print', lambda *a, **k: stderr.append(' '.join(map(str, a)))):
            return cli_install.main(argv), stderr

    def test_usage_and_root_checks_come_first(self):
        code, out = self.run_main(['--force'])
        self.assertEqual(code, cli_install.USAGE)
        code, out = self.run_main([], ROOT_UID=os.getuid() + 1)
        self.assertEqual(code, cli_install.NEED_ROOT)
        self.assertIn('root', out[0])

    def test_running_installer_blocks_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'install.lock'
            with lock.open('a+') as held, patch.object(cli_install, 'upgrade') as upgrade:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                code, out = self.run_main([], ROOT_UID=os.getuid(), INSTALL_LOCK=lock)
                upgrade.assert_not_called()
            self.assertEqual(code, cli_install.REFUSED)
            self.assertIn('повторите позже', out[0])
            with patch.object(cli_install, 'upgrade', return_value=0) as upgrade:
                with patch.object(cli_install.os, 'umask'):
                    code, _ = self.run_main(['--check'], ROOT_UID=os.getuid(), INSTALL_LOCK=lock)
            self.assertEqual(code, 0)
            self.assertTrue(upgrade.call_args.kwargs['check_only'])
            self.assertEqual(upgrade.call_args.args[0], SOURCE)

    def test_installed_library_cannot_upgrade_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / 'bob-auto-xray'
            shutil.copytree(SOURCE, library / 'selfsni', ignore=shutil.ignore_patterns('__pycache__'))
            (library / 'VERSION').write_text('9.9.9\n')
            run = subprocess.run([sys.executable, '-I', '-B', '-c',
                                  'import sys; sys.path.insert(0, sys.argv.pop(1)); '
                                  'from selfsni.cli_install import main; sys.exit(main())',
                                  str(library)], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, cli_install.USAGE)
        self.assertIn('архива релиза', run.stderr)

    def test_install_sh_dispatches_cli_upgrade_without_side_effects(self):
        env = dict(os.environ, PYTHONPATH='')
        run = subprocess.run(['bash', 'install.sh', 'cli-upgrade', '--bogus'], capture_output=True,
                             text=True, timeout=60, env=env, cwd=SOURCE.parents[1])
        self.assertEqual(run.returncode, cli_install.USAGE, run.stderr)
        self.assertIn('cli-upgrade [--check]', run.stderr)
        if os.geteuid() != 0:
            run = subprocess.run(['bash', 'install.sh', 'cli-upgrade', '--check'],
                                 capture_output=True, text=True, timeout=60, env=env,
                                 cwd=SOURCE.parents[1])
            self.assertEqual(run.returncode, cli_install.NEED_ROOT, run.stderr)


if __name__ == '__main__':
    unittest.main()
