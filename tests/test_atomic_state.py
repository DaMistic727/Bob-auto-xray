"""state.json must never be left truncated by an interrupted write."""
import errno
import json
import os
import stat
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from selfsni import core
from selfsni.core import InstallError, save
from selfsni.install_state import checkpoint, fail, initialize, load

REAL_FDOPEN = os.fdopen
REAL_FSTAT = os.fstat  # unaffected by fault injection into core.os.fstat


def state():
    return initialize({'domain': 'vpn.example.com', 'warp_mode': 'install', 'username': 'user',
                       'password': 'secret-' + 'p' * 40, 'panel_port': 21000, 'sub_port': 22000,
                       'panel_path': '/' + 'a' * 48 + '/', 'sub_path': '/' + 'b' * 48 + '/'},
                      ['/etc/nginx'])


class Torn:
    """File object that persists half of the payload, then dies mid-write."""

    def __init__(self, handle, error):
        self.handle, self.error = handle, error

    def write(self, text):
        self.handle.write(text[:len(text) // 2])
        self.handle.flush()
        raise self.error

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()
        return False


def torn(error):
    return patch.object(core.os, 'fdopen', side_effect=lambda fd, *args, **kwargs:
                        Torn(REAL_FDOPEN(fd, *args, **kwargs), error))


class AtomicStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'state.json'

    def assertOnlyState(self):
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['state.json'])

    def test_interrupted_checkpoint_or_failure_keeps_previous_state(self):
        writers = {'checkpoint': lambda s: checkpoint(self.root, s, 'WARP'),
                   'fail': lambda s: fail(self.root, s, 'Сбой: ' + 'x' * 200)}
        for name, writer in writers.items():
            for error in (OSError(28, 'No space left on device'), KeyboardInterrupt()):
                with self.subTest(writer=name, error=type(error).__name__):
                    previous = state()
                    checkpoint(self.root, previous, 'APT_INSTALL', completed=True)
                    expected = json.loads(self.path.read_text())
                    with torn(error), self.assertRaises(type(error)):
                        writer(dict(previous, resume_count=7))
                    self.assertEqual(load(self.root), expected)
                    self.assertOnlyState()

    def test_interrupted_first_write_leaves_no_partial_state(self):
        with torn(OSError(5, 'I/O error')), self.assertRaises(OSError):
            save(self.path, state())
        self.assertIsNone(load(self.root))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_state_is_replaced_durably(self):
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            kind = 'directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file'
            events.append(('fsync', kind))
            real_fsync(fd)

        def replace(source, target):
            events.append(('replace', Path(source).parent == self.root, Path(target) == self.path))
            real_replace(source, target)

        save(self.path, {'old': True})
        events.clear()
        with patch.object(core.os, 'fsync', side_effect=fsync), \
             patch.object(core.os, 'replace', side_effect=replace):
            save(self.path, state())
        self.assertEqual(events, [('fsync', 'file'), ('replace', True, True), ('fsync', 'directory')])
        self.assertOnlyState()

    def test_state_is_root_only_when_created_and_replaced(self):
        save(self.path, {'first': True})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.path.chmod(0o644)
        save(self.path, {'second': True})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(load(self.root), {'second': True})

    def test_symlinked_state_is_refused_without_touching_target(self):
        target = self.root / 'elsewhere.json'
        target.write_text('{"foreign": true}\n')
        self.path.symlink_to(target)
        with self.assertRaisesRegex(InstallError, 'Refusing symlink'):
            save(self.path, state())
        self.assertEqual(target.read_text(), '{"foreign": true}\n')
        self.assertTrue(self.path.is_symlink())

    def test_non_ascii_failure_message_round_trips(self):
        saved = state()
        fail(self.root, saved, 'Не удалось получить SSL-сертификат.\nДомен не найден в DNS.')
        self.assertEqual(load(self.root)['failure'], saved['failure'])


class AtomicWriteDescriptorTests(unittest.TestCase):
    """The mkstemp descriptor is closed exactly once on every failure path."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'state.json'
        save(self.path, {'previous': True})
        self.previous = self.path.read_bytes()
        self.descriptors = []
        real_mkstemp = tempfile.mkstemp

        def mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            info = REAL_FSTAT(fd)
            self.descriptors.append(fd)
            # A leaked temporary descriptor must not outlive a failing test.
            self.addCleanup(self.close_if_same, fd, (info.st_dev, info.st_ino))
            return fd, name
        patcher = patch.object(core.tempfile, 'mkstemp', side_effect=mkstemp)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def close_if_same(fd, identity):
        try:
            current = REAL_FSTAT(fd)
        except OSError:
            return
        if (current.st_dev, current.st_ino) == identity:
            os.close(fd)

    def assertClosed(self, fd):
        with self.assertRaises(OSError) as raised:
            REAL_FSTAT(fd)
        self.assertEqual(raised.exception.errno, errno.EBADF)

    def assertPreviousStateOnly(self):
        self.assertEqual(self.path.read_bytes(), self.previous)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['state.json'])

    def fstat_failing_after(self, calls):
        """Real fstat for the first `calls` invocations, EIO afterwards."""
        seen = []

        def fstat(fd):
            seen.append(fd)
            if len(seen) > calls:
                raise OSError(errno.EIO, 'Input/output error')
            return REAL_FSTAT(fd)
        return patch.object(core.os, 'fstat', side_effect=fstat)

    def test_fchmod_failure_closes_owned_descriptor(self):
        for error in (OSError(errno.EIO, 'Input/output error'), KeyboardInterrupt()):
            for cleanup_fstat_fails in (False, True):
                with self.subTest(error=type(error).__name__, cleanup_fstat_fails=cleanup_fstat_fails):
                    with patch.object(core.os, 'fchmod', side_effect=error), \
                         (self.fstat_failing_after(1) if cleanup_fstat_fails else nullcontext()), \
                         self.assertRaises(type(error)):
                        save(self.path, state())
                    self.assertClosed(self.descriptors[-1])
                    self.assertPreviousStateOnly()

    def test_initial_fstat_failure_closes_descriptor(self):
        with self.fstat_failing_after(0), self.assertRaisesRegex(OSError, 'Input/output error'):
            save(self.path, state())
        self.assertClosed(self.descriptors[-1])
        self.assertPreviousStateOnly()

    def test_fdopen_failure_before_ownership_closes_descriptor(self):
        for error in (OSError(errno.EMFILE, 'Too many open files'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), \
                 patch.object(core.os, 'fdopen', side_effect=error), \
                 self.assertRaises(type(error)):
                save(self.path, state())
            self.assertClosed(self.descriptors[-1])
            self.assertPreviousStateOnly()

    def test_fdopen_failure_after_ownership_never_closes_reused_descriptor(self):
        # The file object already closed the descriptor, and its number was
        # reused by an unrelated file. That file must stay open.
        foreign = self.root / 'foreign'

        def fdopen(fd, *args, **kwargs):
            REAL_FDOPEN(fd, *args, **kwargs).close()
            holder = open(foreign, 'w')
            self.addCleanup(holder.close)
            if holder.fileno() != fd:
                os.dup2(holder.fileno(), fd)
                info = REAL_FSTAT(fd)
                self.addCleanup(self.close_if_same, fd, (info.st_dev, info.st_ino))
            raise MemoryError('wrapper allocation failed')

        with patch.object(core.os, 'fdopen', side_effect=fdopen), \
             self.assertRaises(MemoryError):
            save(self.path, state())
        self.assertEqual(os.fstat(self.descriptors[-1]).st_ino, foreign.stat().st_ino)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['foreign', 'state.json'])
        self.assertEqual(self.path.read_bytes(), self.previous)

    def test_successful_write_closes_descriptor_once(self):
        closes = []
        real_close = os.close

        def close(fd):
            closes.append(fd)
            real_close(fd)
        with patch.object(core.os, 'close', side_effect=close):
            save(self.path, {'next': True})
        fd = self.descriptors[-1]
        self.assertClosed(fd)
        self.assertNotIn(fd, closes[:-1])  # only the directory fd may reuse the number
        self.assertEqual(load(self.root), {'next': True})


if __name__ == '__main__':
    unittest.main()
