import unittest
from pathlib import Path
import tempfile

from selfsni.apt import AptError, AptRunner, background_package_processes, classify
from selfsni.core import CommandError


class AptTests(unittest.TestCase):
    def runner(self, outcomes, notices=None):
        calls = []
        outcomes = list(outcomes)
        def command(*args, **kwargs):
            calls.append((args, kwargs))
            value = outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        sink = notices if notices is not None else []
        return AptRunner(command, notify=sink.append, sleep=lambda _: None), calls

    def test_free_apt_runs_with_bounded_lock_wait(self):
        apt, calls = self.runner(['ok'])
        self.assertEqual(apt.update(), 'ok')
        self.assertIn('DPkg::Lock::Timeout=600', calls[0][0])

    def test_lock_timeout_is_specific_and_not_retried(self):
        notices = []
        error = CommandError('apt-get', 100, 'E: Could not get lock /var/lib/dpkg/lock-frontend')
        apt, calls = self.runner([error], notices)
        with self.assertRaises(AptError) as caught:
            apt.install(['nginx'])
        self.assertEqual(caught.exception.category, 'lock')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(notices), 1)
        self.assertIn('блокировки', notices[0])

    def test_background_process_without_lock_is_silent(self):
        notices = []
        apt, _ = self.runner(['ok'], notices)
        with unittest.mock.patch('selfsni.apt.background_package_processes',
                                 return_value={'unattended-upgr'}):
            self.assertEqual(apt.update(), 'ok')
        self.assertEqual(notices, [])

    def test_real_lock_output_notifies_once_while_waiting(self):
        notices = []
        def command(*args, **kwargs):
            self.assertIn('DPkg::Lock::Timeout=600', args)
            callback = kwargs['on_output']
            callback('Waiting for cache lock: Could not get lock /var/lib/dpkg/lock-frontend')
            callback('Waiting for cache lock: Could not get lock /var/lib/dpkg/lock-frontend')
            return 'done'
        apt = AptRunner(command, notify=notices.append)
        self.assertEqual(apt.update(), 'done')
        self.assertEqual(len(notices), 1)

    def test_temporary_update_network_failure_retries(self):
        error = CommandError('apt-get', 100, 'Temporary failure resolving archive.ubuntu.com')
        apt, calls = self.runner([error, error, 'ok'])
        self.assertEqual(apt.update(), 'ok')
        self.assertEqual(len(calls), 3)

    def test_install_partial_failure_is_never_blindly_retried(self):
        error = CommandError('apt-get', 100, 'dpkg: error processing package nginx')
        apt, calls = self.runner([error])
        with self.assertRaises(AptError) as caught:
            apt.install(['nginx'])
        self.assertEqual(caught.exception.category, 'dpkg')
        self.assertEqual(len(calls), 1)

    def test_upgrade_dependency_failure_is_specific(self):
        error = CommandError('apt-get', 100, 'E: Unmet dependencies. Try apt --fix-broken install')
        apt, _ = self.runner([error])
        with self.assertRaises(AptError) as caught:
            apt.upgrade()
        self.assertEqual(caught.exception.category, 'dependencies')

    def test_timeout_and_systemd_are_distinct(self):
        self.assertEqual(classify(CommandError('apt-get', output='', timed_out=True))[0], 'timeout')
        self.assertEqual(classify(CommandError('apt-get', 100, 'Failed to connect to bus'))[0], 'systemd')

    def test_background_provider_process_is_detected_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc/'123').mkdir()
            (proc/'123/comm').write_text('unattended-upgr\n')
            self.assertEqual(background_package_processes(proc), {'unattended-upgr'})


if __name__ == '__main__':
    unittest.main()
