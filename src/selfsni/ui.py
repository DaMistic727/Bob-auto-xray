import os
import re
import shutil
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
        self.stage_started = self.started
        self.current = None
        self.completed = 0
        self.ticks = 0
        self.last_non_tty = self.started
        self.live = False

    def _paint(self, code, value):
        return f'\033[{code}m{value}\033[0m' if self.color else value

    def _clear(self):
        if self.live:
            print('\r\033[K' if self.color else '\r' + ' ' * min(shutil.get_terminal_size((80, 20)).columns - 1, 100) + '\r',
                  end='', file=self.stream, flush=True)
            self.live = False

    def banner(self, version):
        width = shutil.get_terminal_size((80, 20)).columns
        lines = [f'BOB AUTO XRAY · Версия {version}', 'Автоматическая настройка VPN']
        if self.unicode and width >= 48:
            lines = ['╭' + '─' * 46 + '╮', '│' + 'BOB AUTO XRAY'.center(46) + '│',
                     '│' + 'Автоматическая настройка VPN'.center(46) + '│',
                     '│' + ('Версия ' + version).center(46) + '│', '╰' + '─' * 46 + '╯']
        elif width >= 48:
            lines = ['+' + '-' * 46 + '+', '|' + 'BOB AUTO XRAY'.center(46) + '|',
                     '|' + 'Автоматическая настройка VPN'.center(46) + '|',
                     '|' + ('Версия ' + version).center(46) + '|', '+' + '-' * 46 + '+']
        print(self._paint('36;1', '\n'.join(lines)), file=self.stream, flush=True)

    def _render(self):
        if not self.current or not self.tty:
            return
        number, total, label = self.current
        width = max(20, shutil.get_terminal_size((80, 20)).columns - 1)
        elapsed = int(time.monotonic() - self.stage_started)
        bar_width = max(4, min(24, width - 47))
        filled = round(bar_width * self.completed / total)
        bar = ('█' * filled + '░' * (bar_width - filled)) if self.unicode else ('#' * filled + '.' * (bar_width - filled))
        spinner = ('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' if self.unicode else '|/-\\')[self.ticks % (10 if self.unicode else 4)]
        value = f'{spinner} [{bar}] {self.completed}/{total} · {label} · {elapsed // 60:02d}:{elapsed % 60:02d}'
        print('\r' + self._paint('36', value[:width].ljust(width)), end='', file=self.stream, flush=True)
        self.live = True

    def stage(self, number, total, label, state='running'):
        self._clear()
        self.current = (number, total, label)
        if state in ('ok', 'skipped'):
            self.completed = max(self.completed, number)
            symbol = ('—' if self.unicode else 'SKIP') if state == 'skipped' else ('✓' if self.unicode else 'OK')
            elapsed = 0 if state == 'skipped' else int(time.monotonic() - self.stage_started)
            print(self._paint('32', f'{symbol} {label} ({self.completed}/{total}, {elapsed // 60:02d}:{elapsed % 60:02d})'),
                  file=self.stream, flush=True)
        elif state == 'failed':
            print(self._paint('31', f'Ошибка: {label}'), file=self.stream, flush=True)
        elif self.tty:
            self.stage_started = time.monotonic()
            self._render()
        else:
            self.stage_started = time.monotonic()
            print(f'[{number}/{total}] {label}: выполняется', file=self.stream, flush=True)

    def pulse(self):
        self.ticks += 1
        if self.tty:
            self._render()
        elif self.current and time.monotonic() - self.last_non_tty >= 30:
            self.last_non_tty = time.monotonic()
            print(f'[{self.current[0]}/{self.current[1]}] {self.current[2]}: '
                  f'выполняется {int(self.last_non_tty - self.stage_started)} с',
                  file=self.stream, flush=True)

    def note(self, message):
        self._clear()
        print('  ' + message, file=self.stream, flush=True)
        if self.tty:
            self._render()

    def failure(self, error, log_path):
        self._clear()
        label = self.current[2] if self.current else 'Установка'
        print(self._paint('31;1', f'\n✗ {label}'), file=self.stream)
        print(f'\nПричина: {error}\nЖурнал: {log_path}\n', file=self.stream, flush=True)

    def success(self, state):
        self._clear()
        if self.unicode:
            print('\n╭' + '─' * 46 + '╮\n│' + 'УСТАНОВКА ЗАВЕРШЕНА'.center(46) +
                  '│\n╰' + '─' * 46 + '╯', file=self.stream)
        else:
            print('\nУСТАНОВКА ЗАВЕРШЕНА', file=self.stream)
        checks = ['Сервер настроен', 'SSL-сертификат получен', '3x-ui работает']
        checks.append('WARP HEALTHY' if state.get('warp_mode', 'install') != 'skip' else 'WARP не используется')
        checks.append('Локальные проверки пройдены')
        mark = '✓' if self.unicode else 'OK'
        print('\n' + '\n'.join(f'{mark} {item}' for item in checks), file=self.stream)
        print(f'\nПанель управления:\nhttps://{state["domain"]}:{state["panel_port"]}{state["panel_path"]}',
              file=self.stream)
        if self.tty:
            print(f'\nЛогин:  {state["username"]}\nПароль: {state["password"]}', file=self.stream)
        else:
            print('\nРеквизиты сохранены в /var/lib/selfsni/state.json (доступ root).',
                  file=self.stream)
        print('\nСледующий шаг:\nОткройте панель 3x-ui и создайте первого VPN-клиента '
              'с flow xtls-rprx-vision.\nПосле создания клиента используйте его ссылку '
              'или подписку для подключения.\n\nОтчёт:\n/var/lib/selfsni/acceptance.json',
              file=self.stream, flush=True)


def _read(source, output, prompt):
    print(prompt, end='', file=output, flush=True)
    value = source.readline()
    if not value:
        raise InstallError('Ввод прерван. Установка не начата.')
    return value.strip()


def interactive_wizard(args, version, input_stream=None, output=None):
    output = output or sys.stdout
    source = input_stream
    close = False
    if source is None:
        try:
            source = open('/dev/tty')
            close = True
        except OSError as error:
            raise InstallError('Для запуска без вопросов укажите --domain vpn.example.com') from error
    try:
        ui = InstallerUI(output, tty=output.isatty())
        ui.banner(version)
        if not args.domain:
            print('\nШАГ 1/4 — Домен сервера\n' + '─' * 44 + '\n'
                  'Введите домен для подключения к VPN.\nПример: vpn.example.com\n\n'
                  'Перед продолжением проверьте:\n'
                  '• DNS-запись A указывает на IP этого сервера.\n'
                  '• Проксирование Cloudflare отключено.\n'
                  '• Домен доступен из интернета.\n', file=output)
            try:
                args.domain = fqdn(_read(source, output, 'Домен сервера › '))
            except InstallError as error:
                raise InstallError('Некорректный домен. Введите адрес вида vpn.example.com, без https:// и пути.') from error
        if not args.email:
            print('\nШАГ 2/4 — SSL-сертификат\n' + '─' * 44 + '\n'
                  'Для защищённого соединения будет получен бесплатный\n'
                  'сертификат Let’s Encrypt. Email для уведомлений\n'
                  'необязателен: нажмите Enter, чтобы пропустить.\n'
                  'Пример: admin@example.com\n', file=output)
            args.email = _read(source, output, 'Email (необязательно) › ')
        if args.email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', args.email):
            raise InstallError('Некорректный email. Пример: admin@example.com')
        if not getattr(args, 'warp_mode', None):
            print('\nШАГ 3/4 — Cloudflare WARP\n' + '─' * 44 + '\n'
                  '1. Установить Cloudflare WARP (по умолчанию)\n'
                  '2. Использовать существующий WARP\n'
                  '3. Не использовать WARP\n', file=output)
            choice = _read(source, output, 'Режим WARP [1/2/3] › ')
            if choice not in ('', '1', '2', '3'):
                raise InstallError('Выберите режим WARP: 1, 2 или 3')
            args.warp_mode = {'': 'install', '1': 'install', '2': 'adopt', '3': 'skip'}[choice]
        args.adopt_warp = args.warp_mode == 'adopt'
        print('\nШАГ 4/4 — Подтверждение\n' + '─' * 44 + '\n'
              f'Домен:        {args.domain}\n'
              'Сертификат:   Let’s Encrypt\n'
              f'WARP:         {dict(install="Автоматическая установка", adopt="Проверка существующей установки", skip="Не используется")[args.warp_mode]}\n'
              'Панель:       3x-ui\n\n'
              'Будут обновлены системные пакеты и настроены\nкомпоненты VPN-сервера.\n', file=output)
        if _read(source, output, 'Начать установку? [Y/n] › ').lower() not in ('', 'y', 'yes', 'д', 'да'):
            raise InstallError('Установка отменена пользователем')
        return args
    finally:
        if close:
            source.close()
