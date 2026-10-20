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
        self.assertIn('BOB AUTO XRAY', text)
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
            ('not-a-fqdn\n', 'Некорректный домен'),
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

    def test_wizard_skips_supplied_email_and_confirms_changes(self):
        from argparse import Namespace
        from selfsni.ui import interactive_wizard
        args = Namespace(domain=None, email='admin@example.com', adopt_warp=False)
        output = io.StringIO()
        interactive_wizard(args, '0.3.4', io.StringIO('vpn.example.com\n\n'), output)
        self.assertEqual(args.domain, 'vpn.example.com')
        self.assertNotIn('Email (необязательно) ›', output.getvalue())
        self.assertIn('Будут обновлены системные пакеты', output.getvalue())

    def test_progress_and_no_color_have_no_ansi(self):
        class Utf8Stream(io.StringIO):
            encoding = 'utf-8'
        stream = Utf8Stream()
        ui = InstallerUI(stream, tty=True, env={'NO_COLOR': '1', 'TERM': 'xterm'})
        ui.stage(1, 13, 'Проверка')
        ui.pulse()
        ui.stage(1, 13, 'Проверка', 'ok')
        ui.stage(2, 13, 'Загрузка')
        self.assertIn('1/13', stream.getvalue())
        self.assertIn('█', stream.getvalue())
        self.assertNotIn('\033[', stream.getvalue())

    def test_saved_domain_skips_wizard_on_resume(self):
        import selfsni.provision as provision
        stream = io.StringIO()
        saved = {'domain': 'vpn.example.com', 'adopt_warp': False}
        with patch.object(provision, 'load', return_value=saved), \
             patch.object(provision, 'interactive_wizard') as wizard, \
             patch.object(provision, 'verify_bundle', side_effect=provision.InstallError('test stop')), \
             patch('sys.argv', ['install']), patch('sys.stdout', stream):
            with self.assertRaises(SystemExit):
                provision.main()
        wizard.assert_not_called()
        self.assertIn('Продолжаем сохранённую установку', stream.getvalue())

    def test_success_hides_subscription_and_non_tty_credentials(self):
        state = dict(domain='vpn.example.com', panel_port=12345, panel_path='/panel/',
                     username='private-user', password='private-password')
        output = io.StringIO()
        InstallerUI(output, tty=False).success(state)
        text = output.getvalue()
        self.assertIn('Панель управления:', text)
        self.assertIn('создайте первого VPN-клиента', text)
        self.assertNotIn('Подписки:', text)
        self.assertNotIn('private-password', text)
        class Utf8Stream(io.StringIO):
            encoding = 'utf-8'
        terminal = Utf8Stream()
        InstallerUI(terminal, tty=True, env={'NO_COLOR': '1', 'TERM': 'xterm'}).success(state)
        self.assertIn('private-password', terminal.getvalue())


if __name__ == '__main__':
    unittest.main()
