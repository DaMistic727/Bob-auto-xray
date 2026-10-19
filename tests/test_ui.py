import io
import unittest

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


if __name__ == '__main__':
    unittest.main()
