import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from selfsni import swap
from selfsni.core import InstallError


class Commands:
    def __init__(self, file, fail=None):
        self.file = file
        self.fail = fail
        self.active = False

    def __call__(self, *args):
        if args == ('swapon', '--show=NAME', '--noheadings', '--raw'):
            return str(self.file) + '\n' if self.active else ''
        if args[0] == self.fail:
            raise InstallError('injected failure')
        if args[0] == 'fallocate':
            with self.file.open('wb') as stream:
                stream.truncate(swap.SIZE)
        elif args[0] == 'swapon':
            self.active = True
        elif args[0] == 'swapoff':
            self.active = False
        return ''


class SwapTests(unittest.TestCase):
    def test_create_repeat_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'swapfile'
            fstab = Path(directory) / 'fstab'
            fstab.write_text('# existing\n')
            command = Commands(file)
            space = lambda _: SimpleNamespace(free=swap.SIZE + swap.RESERVE)
            self.assertEqual(swap.configure(command, file, fstab, space)['status'], 'created')
            self.assertTrue(command.active)
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(fstab.read_text().count(swap.LINE), 1)
            self.assertEqual(swap.configure(command, file, fstab, space)['status'], 'existing')
            self.assertEqual(fstab.read_text().count(swap.LINE), 1)

    def test_insufficient_space_skips_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'swapfile'
            fstab = Path(directory) / 'fstab'
            fstab.write_text('# existing\n')
            result = swap.configure(Commands(file), file, fstab,
                                    lambda _: SimpleNamespace(free=swap.SIZE))
            self.assertEqual(result['status'], 'skipped')
            self.assertFalse(file.exists())
            self.assertEqual(fstab.read_text(), '# existing\n')

    def test_mkswap_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'swapfile'
            fstab = Path(directory) / 'fstab'
            fstab.write_text('# existing\n')
            with self.assertRaises(InstallError):
                swap.configure(Commands(file, 'mkswap'), file, fstab,
                               lambda _: SimpleNamespace(free=swap.SIZE + swap.RESERVE))
            self.assertFalse(file.exists())
            self.assertEqual(fstab.read_text(), '# existing\n')

    def test_swapon_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'swapfile'
            fstab = Path(directory) / 'fstab'
            fstab.write_text('# existing\n')
            with self.assertRaises(InstallError):
                swap.configure(Commands(file, 'swapon'), file, fstab,
                               lambda _: SimpleNamespace(free=swap.SIZE + swap.RESERVE))
            self.assertFalse(file.exists())
            self.assertEqual(fstab.read_text(), '# existing\n')

    def test_preflight_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'swapfile'
            fstab = Path(directory) / 'fstab'
            fstab.write_text('# existing\n')
            self.assertEqual(swap.preflight(Commands(file), file, fstab), {'existing': False})
            self.assertFalse(file.exists())
            self.assertEqual(fstab.read_text(), '# existing\n')

    def test_foreign_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'swapfile'
            fstab = Path(directory) / 'fstab'
            file.write_text('foreign')
            fstab.write_text('# existing\n')
            with self.assertRaisesRegex(InstallError, 'refusing to overwrite'):
                swap.configure(Commands(file), file, fstab)
            self.assertEqual(file.read_text(), 'foreign')


class BestEffortTests(unittest.TestCase):
    """swap.apply(): another tool's swap is left alone; only a failed rollback is fatal."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file = Path(directory.name) / 'swapfile'
        self.fstab = Path(directory.name) / 'fstab'
        self.fstab.write_text('# existing\n')
        self.space = lambda _: SimpleNamespace(free=swap.SIZE + swap.RESERVE)

    def test_menu_swap_of_another_size_is_external_and_untouched(self):
        # VPS-Server-Menu: 1/2/4 GiB or custom /swapfile with the same fstab line.
        with self.file.open('wb') as stream:
            stream.truncate(1024**3)
        self.file.chmod(0o600)
        self.fstab.write_text('# existing\n' + swap.LINE + '\n')
        command = Commands(self.file)
        command.active = True
        report = swap.apply(command, self.file, self.fstab, self.space)
        self.assertEqual((report['status'], report['path']), ('external', str(self.file)))
        self.assertEqual(self.file.stat().st_size, 1024**3)
        self.assertEqual(self.fstab.read_text(), '# existing\n' + swap.LINE + '\n')
        self.assertEqual(swap.verify(report, command, self.file, self.fstab), report)

    def test_inactive_foreign_file_or_entry_is_skipped(self):
        for case in ('file', 'entry', 'symlink'):
            with self.subTest(case=case):
                self.setUp()
                if case == 'file':
                    self.file.write_text('foreign')
                elif case == 'entry':
                    self.fstab.write_text(f'{self.file} none swap defaults,pri=5 0 0\n')
                else:
                    target = self.fstab.with_name('real')
                    target.write_text(self.fstab.read_text())
                    self.fstab.unlink()
                    self.fstab.symlink_to(target)
                before = self.fstab.read_text()
                report = swap.apply(Commands(self.file), self.file, self.fstab, self.space)
                self.assertEqual((report['status'], report['foreign']), ('skipped', True))
                self.assertEqual(self.fstab.read_text(), before)
                self.assertEqual(swap.verify(report, Commands(self.file), self.file, self.fstab), report)

    def test_creation_failure_rolls_back_and_skips(self):
        report = swap.apply(Commands(self.file, 'mkswap'), self.file, self.fstab, self.space)
        self.assertEqual(report['status'], 'skipped')
        self.assertIn('rolled back', report['reason'])
        self.assertFalse(self.file.exists())
        self.assertEqual(self.fstab.read_text(), '# existing\n')

    def test_failed_rollback_is_fatal(self):
        command = Commands(self.file, 'mkswap')
        original = command.__call__
        attempted = []

        def failing(*args):
            if args[0] == 'mkswap':
                attempted.append(args)
            elif attempted and args == ('swapon', '--show=NAME', '--noheadings', '--raw'):
                raise InstallError('swapon exited 1')  # the rollback cannot prove swapoff
            return original(*args)
        with self.assertRaisesRegex(swap.RollbackError, 'Swap rollback incomplete'):
            swap.apply(failing, self.file, self.fstab, self.space)

    def test_interrupt_rolls_back_and_propagates(self):
        command = Commands(self.file)
        original = command.__call__

        def interrupted(*args):
            if args[0] == 'mkswap':
                raise KeyboardInterrupt
            return original(*args)
        with self.assertRaises(KeyboardInterrupt):
            swap.apply(interrupted, self.file, self.fstab, self.space)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.fstab.read_text(), '# existing\n')

    def test_clean_host_creates_managed_swap(self):
        report = swap.apply(Commands(self.file), self.file, self.fstab, self.space)
        self.assertEqual(report['status'], 'created')


if __name__ == '__main__':
    unittest.main()
