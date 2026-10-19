from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from selfsni.core import CommandError, configure_activity, configure_diagnostics, download, run


class DiagnosticTests(unittest.TestCase):
    def tearDown(self):
        configure_diagnostics(None)
        configure_activity(None)

    def test_full_output_is_logged_root_only_and_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'install.log'
            secret = 'PanelPasswordValue123456789'
            configure_diagnostics(log, [secret])
            with self.assertRaises(CommandError) as caught:
                run(sys.executable, '-c',
                    f"print('E: failed password={secret} uuid=123e4567-e89b-12d3-a456-426614174000');raise SystemExit(100)")
            self.assertEqual(caught.exception.returncode, 100)
            content = log.read_text()
            self.assertIn('exit=100', content)
            self.assertNotIn(secret, content)
            self.assertNotIn('123e4567-e89b-12d3-a456-426614174000', content)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_real_subprocess_timeout_preserves_partial_stdout(self):
        with self.assertRaises(CommandError) as caught:
            run(sys.executable, '-c',
                "import sys,time;sys.stdout.buffer.write(b'ready\\xff\\n');"
                "sys.stdout.flush();time.sleep(10)", timeout=0.1)
        self.assertTrue(caught.exception.timed_out)
        self.assertIsInstance(caught.exception.output, str)
        self.assertIn('ready', caught.exception.output)

    def test_long_running_command_emits_heartbeat_and_keeps_output(self):
        pulses = []
        configure_activity(lambda: pulses.append(True))
        output = run(sys.executable, '-c', "import time;print('before',flush=True);time.sleep(1.2);print('after')", timeout=3)
        self.assertTrue(pulses)
        self.assertEqual(output.strip().splitlines(), ['before', 'after'])

    def test_interrupt_stops_command(self):
        configure_activity(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            run(sys.executable, '-c', 'import time;time.sleep(10)', timeout=15)

    def test_slow_download_emits_heartbeat(self):
        import time
        class Response:
            url = 'https://example.com/archive'
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def read(self, size):
                if not hasattr(self, 'sent'):
                    self.sent = True
                    time.sleep(1.2)
                    return b'archive'
                return b''
        pulses = []
        configure_activity(lambda: pulses.append(True))
        with tempfile.TemporaryDirectory() as directory, \
             patch('selfsni.core.urllib.request.urlopen', return_value=Response()):
            path = download('https://example.com/archive', Path(directory)/'archive')
            self.assertEqual(path.read_bytes(), b'archive')
        self.assertTrue(pulses)


if __name__ == '__main__':
    unittest.main()
