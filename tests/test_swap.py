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


if __name__ == '__main__':
    unittest.main()
