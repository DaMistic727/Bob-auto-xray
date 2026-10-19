import io
import sys
import unittest
from unittest.mock import patch

from selfsni.ui import InstallerUI


class UiTests(unittest.TestCase):
    def test_non_tty_uses_ascii_without_color(self):
        stream = io.StringIO()
        ui = InstallerUI(stream, tty=False, env={})
        ui.banner('0.3.4')
        ui.stage(1, 13, 'Проверка')
        text = stream.getvalue()
        self.assertIn('+--------------------------------------------+', text)
        self.assertNotIn('\033[', text)
        self.assertNotIn('⟳', text)

    def test_no_color_preserves_unicode(self):
        class Utf8Stream(io.StringIO):
            encoding = 'utf-8'
        stream = Utf8Stream()
        ui = InstallerUI(stream, tty=True, env={'NO_COLOR':'1','TERM':'xterm'})
        ui.banner('0.3.4')
        self.assertIn('╭', stream.getvalue())
        self.assertNotIn('\033[', stream.getvalue())

    def test_interactive_input_errors_exit_cleanly(self):
        import selfsni.provision as provision
        real_wizard = provision.interactive_wizard

        cases = (
            ('not-a-fqdn\n', 'Expected a public FQDN'),
            ('vpn.example.com\n\nn\n', 'Установка отменена пользователем'),
        )
        for answers, expected in cases:
            with self.subTest(expected=expected):
                stream = io.StringIO()
                def wizard(args, version):
                    return real_wizard(
                        args, version, input_stream=io.StringIO(answers), output=sys.stdout)
                with patch.object(provision, 'interactive_wizard', side_effect=wizard), \
                     patch.object(provision, 'LOG_ENABLED', False), \
                     patch('sys.argv', ['install']), \
                     patch('sys.stdout', stream):
                    with self.assertRaises(SystemExit) as caught:
                        provision.main()
                self.assertEqual(caught.exception.code, 1)
                self.assertIn(expected, stream.getvalue())
                self.assertNotIn('Traceback', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
