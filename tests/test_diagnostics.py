from pathlib import Path
import sys
import tempfile
import unittest

from selfsni.core import CommandError, configure_diagnostics, run


class DiagnosticTests(unittest.TestCase):
    def tearDown(self):
        configure_diagnostics(None)

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


if __name__ == '__main__':
    unittest.main()
