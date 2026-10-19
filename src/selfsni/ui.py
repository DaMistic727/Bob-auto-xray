import os
import sys
import time

from .core import InstallError, fqdn


class InstallerUI:
    def __init__(self, stream=None, tty=None, env=None):
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty() if tty is None else tty
        env = os.environ if env is None else env
        encoding = (getattr(self.stream, 'encoding', '') or '').lower()
        self.unicode = self.tty and 'utf' in encoding and env.get('TERM') != 'dumb'
        self.color = self.tty and 'NO_COLOR' not in env
        self.started = time.monotonic()
        self.current = None

    def _paint(self, code, text):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def banner(self, version):
        if self.unicode:
            lines = ['╭────────────────────────────────────────────╮',
                     '│              BOB AUTO XRAY                 │',
                     '│       VLESS REALITY • Self-SNI • WARP      │',
                     f'│              Setup v{version:<22}│',
                     '╰────────────────────────────────────────────╯']
        else:
            lines = ['+--------------------------------------------+',
                     '|              BOB AUTO XRAY                 |',
                     '|       VLESS REALITY / Self-SNI / WARP      |',
                     f'|              Setup v{version:<22}|',
                     '+--------------------------------------------+']
        print(self._paint('36;1', '\n'.join(lines)), file=self.stream)

    def stage(self, number, total, label, state='running'):
        elapsed = int(time.monotonic() - self.started)
        symbols = {'running': '⟳' if self.unicode else '...',
                   'ok': '✓' if self.unicode else 'OK',
                   'failed': '✗' if self.unicode else 'FAILED'}
        colors = {'running': '36', 'ok': '32', 'failed': '31'}
        self.current = (number, total, label)
        message = f'[{number:02d}/{total:02d}] {label:<32} {symbols[state]}  {elapsed}s'
        print(self._paint(colors[state], message), file=self.stream, flush=True)

    def note(self, message):
        print('  ' + message, file=self.stream, flush=True)

    def failure(self, error, log_path):
        label = self.current[2] if self.current else 'Установка'
        print(self._paint('31;1', f'\n✗ {label}'), file=self.stream)
        print(f'\n  Причина: {error}\n\n  Журнал:\n  {log_path}\n', file=self.stream)


def interactive_wizard(args, version, input_stream=None, output=None):
    output = output or sys.stdout
    source = input_stream
    close = False
    if source is None:
        try:
            source = open('/dev/tty')
            close = True
        except OSError as error:
            raise InstallError('Non-interactive запуск требует --domain') from error
    try:
        ui = InstallerUI(output, tty=True)
        ui.banner(version)
        print('\n  ШАГ 1/3  Настройка домена\n  Укажи FQDN, направленный A-записью на эту VPS.\n', file=output)
        print('  › ', end='', file=output, flush=True)
        args.domain = fqdn(source.readline().strip())
        print('  ✓ Формат домена корректен\n', file=output)
        print("  ШАГ 2/3  Let's Encrypt\n  Email необязателен; Enter — пропустить.\n", file=output)
        print('  › ', end='', file=output, flush=True)
        args.email = source.readline().strip()
        print(f'\n  ШАГ 3/3  Подтверждение\n  Домен: {args.domain}\n'
              f'  WARP: {"использовать существующий" if args.adopt_warp else "установить через VPS-Server-Menu"}\n',
              file=output)
        print('  Начать установку? [y/N] › ', end='', file=output, flush=True)
        if source.readline().strip().lower() not in ('y', 'yes', 'д', 'да'):
            raise InstallError('Установка отменена пользователем')
        return args
    finally:
        if close:
            source.close()
