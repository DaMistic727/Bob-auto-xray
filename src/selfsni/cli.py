"""`bob`: read-only management menu and commands for an installed server.

Nothing here changes the server. Secrets are read from the root-only installer
state, shown only after explicit confirmation, and never logged or copied.
`bob doctor` is delegated unchanged to selfsni.doctor.main (JSON and exit codes).
"""
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

from . import doctor
from .api import public_panel_url, public_subscription_url
from .clients import main_inbound, verify_admin_runtime
from .core import InstallError

STATE = Path(doctor.STATE)
ACCEPTANCE = Path(doctor.STATE_DIR) / 'acceptance.json'
CONFIG = Path(doctor.CONFIG)
# Patched by tests; production paths are checked from the filesystem root as root.
TRUSTED_ROOT = Path('/')
ROOT_UID = 0
MAX_BYTES = 1024 * 1024

OK, CANCELLED, USAGE, UNAVAILABLE, NEED_ROOT, INTERRUPTED = 0, 1, 2, 3, 4, 130
YES = ('y', 'yes', 'д', 'да')

HELP = '''Bob Auto Xray {version} — управление установленным сервером (только чтение)

Использование:
  bob                      интерактивное меню (только в терминале)
  bob panel                доступ к панели: URL, исходный логин и пароль
  bob admin                ссылка подписки постоянного клиента admin
  bob status               x-ui, nginx, WARP, сертификат и версия Bob
  bob report               сводка отчёта установки (acceptance.json)
  bob doctor [--deep] [--json]
                           диагностика Bob Doctor
  bob --help | --version

Секреты (panel, admin) показываются только после подтверждения в терминале;
без терминала нужен явный флаг --show-secrets. Команды ничего не изменяют
на сервере и требуют прав root.

Коды выхода: 0 — успех, 1 — отменено, 2 — неверные аргументы,
3 — данные недоступны или небезопасны, 4 — нужны права root.
bob doctor сохраняет свои коды: 0 HEALTHY, 1 DEGRADED, 2 UNHEALTHY, 3 UNKNOWN.
'''

PASSWORD_NOTE = ('Пароль — исходный, сохранённый при установке. Если его меняли в 3x-ui,\n'
                 'он больше не действует: Bob не хранит текущий пароль и не сбрасывает его.')


class CliError(Exception):
    """A fixed, secret-free message for the operator and an exit code."""

    def __init__(self, message, code=UNAVAILABLE):
        super().__init__(message)
        self.code = code


def version():
    here = Path(__file__).resolve().parent
    # Installed library: <lib>/VERSION beside the package; release tree: <root>/VERSION.
    for candidate in (here.parent / 'VERSION', here.parents[1] / 'VERSION'):
        try:
            value = candidate.read_text().strip()
        except OSError:
            continue
        if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value):
            return value
    return 'неизвестна'


def is_root():
    return os.geteuid() == ROOT_UID


def require_root():
    if not is_root():
        raise CliError('Нужны права root: запустите команду через sudo.', NEED_ROOT)


def read_private(path, label, root_dirs=True):
    """Read a small root-only regular file without following any symlink.

    Every directory from TRUSTED_ROOT down must be a real directory not writable
    by group/other and, with root_dirs, owned by root; the file itself must be a
    single-link regular file owned by root with no group/other permissions.
    """
    path = Path(path)
    unsafe = CliError(f'{label}: небезопасный путь или права; ожидается обычный файл root '
                      'с правами 0600 без ссылок. Файл не прочитан.')
    try:
        parts = path.relative_to(TRUSTED_ROOT).parts
    except ValueError:
        raise unsafe from None
    directory = TRUSTED_ROOT
    try:
        for part in (None,) + parts[:-1]:
            directory = directory if part is None else directory / part
            info = os.lstat(directory)
            if (not stat.S_ISDIR(info.st_mode) or (root_dirs and info.st_uid != ROOT_UID)
                    or info.st_mode & 0o022):
                raise unsafe
        # O_NONBLOCK: a FIFO planted in place of the file must not hang the menu.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise CliError(f'{label}: файл не найден.') from None
    except PermissionError:
        raise CliError(f'{label}: нет доступа; нужны права root.', NEED_ROOT) from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise unsafe from None
        raise CliError(f'{label}: файл недоступен.') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > MAX_BYTES):
            raise unsafe
        chunks, size = [], 0
        while size <= MAX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        raise CliError(f'{label}: файл недоступен.') from None
    finally:
        os.close(fd)
    if size > MAX_BYTES:
        raise unsafe
    try:
        return b''.join(chunks).decode('utf-8'), info
    except UnicodeDecodeError:
        raise CliError(f'{label}: файл повреждён.') from None


def read_json(path, label, root_dirs=True):
    text, info = read_private(path, label, root_dirs)
    try:
        value = json.loads(text)
    except ValueError:
        raise CliError(f'{label}: файл повреждён.') from None
    if not isinstance(value, dict):
        raise CliError(f'{label}: неверный формат.')
    return value, info


class VerifiedState:
    """Doctor probe that serves the already safely read state and delegates the rest."""

    def __init__(self, text, base):
        self.text = text
        self.base = base

    def __getattr__(self, name):
        return getattr(self.base, name)

    def read(self, path):
        return self.text if str(path) == doctor.STATE else self.base.read(path)

    def is_symlink(self, path):
        return False if str(path) == doctor.STATE else self.base.is_symlink(path)


def load_state(probe=None):
    """Return (raw state, Doctor) after Doctor's own ownership/shape validation."""
    require_root()
    text, _ = read_private(STATE, 'State установки')
    try:
        raw = json.loads(text)
    except ValueError:
        raise CliError('State установки повреждён.') from None
    checker = doctor.Doctor(VerifiedState(text, probe or doctor.Probe()))
    status, summary, _, _ = checker.state_check()
    if status != 'PASS':
        raise CliError(summary)
    return raw, checker


def _printable(value, limit=256):
    return (isinstance(value, str) and 0 < len(value) <= limit and value.isprintable()
            and not any(ch.isspace() for ch in value))


def panel_access(probe=None):
    raw, checker = load_state(probe)
    username, password = raw.get('username'), raw.get('password')
    if not _printable(username) or not _printable(password):
        raise CliError('В state нет корректных исходных данных входа в панель.')
    url = public_panel_url(checker.state)
    return [('Панель', url), ('Логин', username), ('Пароль', password)]


def admin_subscription(probe=None):
    raw, checker = load_state(probe)
    client = raw.get('admin_client')
    if client is None:
        raise CliError('Постоянный клиент admin не создавался этой установкой (установки до v0.3.6).')
    identity = client.get('id') if isinstance(client, dict) else None
    sub_id = client.get('subId') if isinstance(client, dict) else None
    try:
        valid = uuid.UUID(identity).version == 4 and str(uuid.UUID(identity)) == identity
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid or not isinstance(sub_id, str) or re.fullmatch(r'[0-9a-f]{48}', sub_id) is None:
        raise CliError('Данные постоянного клиента admin в state повреждены.')
    return public_subscription_url(checker.state, sub_id), runtime_admin(identity)


def runtime_admin(identity):
    """('PASS'|'WARN'|'UNKNOWN', secret-free reason) for the owned admin in the Xray runtime."""
    # The official 3x-ui installer extracts its release archive with `tar` as root,
    # which keeps the archive owner (uid 1001) on /usr/local/x-ui and bin/. The
    # directories still may not be symlinks or writable by others, and the file
    # itself must be root-only; only a yes/no answer is derived from it.
    try:
        config, _ = read_json(CONFIG, 'Runtime Xray', root_dirs=False)
    except CliError as error:
        return 'UNKNOWN', str(error)
    inbounds = config.get('inbounds')
    try:
        if not isinstance(inbounds, list):
            raise InstallError('inbounds')
        settings = main_inbound([dict(row, id=index) for index, row in enumerate(inbounds)
                                 if isinstance(row, dict)]).get('settings')
        if not isinstance(settings, dict) or not isinstance(settings.get('clients'), list):
            raise InstallError('clients')
    except InstallError:
        return 'UNKNOWN', 'Runtime Xray: основной inbound VLESS на TCP/443 не распознан.'
    try:
        # Same check as the installer: exactly one client with the owned UUID, admin, Vision.
        verify_admin_runtime({'admin_client': {'id': identity}}, config)
    except InstallError:
        return 'WARN', 'отсутствует'
    return 'PASS', 'подтверждён'


def server_status(probe=None):
    """Rows (label, status, summary, details) from Doctor's own read-only checks."""
    _, checker = load_state(probe)
    for identifier, component, method in (
            ('core.xui', 'service', lambda: checker.service('x-ui')),
            ('core.nginx', 'service', lambda: checker.service('nginx')),
            ('warp.service', 'warp', checker.warp_service),
            ('tls.certificate', 'cert', checker.certificate)):
        checker.add(identifier, component, method)
    labels = {'core.xui': 'x-ui', 'core.nginx': 'nginx', 'warp.service': 'WARP',
              'tls.certificate': 'Сертификат'}
    return [(labels[c['id']], c['status'], c['summary'], c['details']) for c in checker.checks]


def acceptance_summary():
    require_root()
    report, info = read_json(ACCEPTANCE, 'Отчёт установки')
    checks = report.get('checks')
    if not isinstance(checks, list) or not checks or not all(
            isinstance(x, dict) and isinstance(x.get('passed'), bool) for x in checks):
        raise CliError('Отчёт установки имеет неверный формат.')
    # Only fixed check names are shown; `reason` may quote exception text and is never printed.
    failed = [_clean(x.get('name')) for x in checks if not x['passed']]
    created = datetime.fromtimestamp(info.st_mtime, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return {'total': len(checks), 'passed': len(checks) - len(failed), 'failed': failed,
            'created': created}


def _clean(value):
    if not isinstance(value, str):
        return 'без названия'
    value = ''.join(ch if ch.isprintable() else ' ' for ch in value).strip()
    return value[:120] or 'без названия'


class Terminal:
    def __init__(self, stdin, stdout, env):
        self.stdin, self.out = stdin, stdout
        self.interactive = _isatty(stdin) and _isatty(stdout)
        term = env.get('TERM', '')
        self.ansi = _isatty(stdout) and term not in ('', 'dumb')
        self.color = self.ansi and 'NO_COLOR' not in env
        encoding = (getattr(stdout, 'encoding', '') or '').lower()
        self.unicode = self.ansi and 'utf' in encoding

    def paint(self, code, text):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def say(self, text=''):
        print(text, file=self.out, flush=True)

    def ask(self, prompt):
        print(prompt, end='', file=self.out, flush=True)
        line = self.stdin.readline()
        if not line:
            raise EOFError
        return line.strip()

    def confirm(self, prompt):
        return self.ask(prompt + ' [y/N] › ').lower() in YES

    def clear(self, scrollback=False):
        # Only for terminals with ANSI support; 3J also drops revealed secrets from scrollback.
        if self.ansi:
            print('\033[H\033[2J' + ('\033[3J' if scrollback else ''), end='', file=self.out, flush=True)

    def mark(self, status):
        marks = {'PASS': ('✓', 'OK', '32'), 'WARN': ('!', '!', '33'), 'FAIL': ('✗', 'FAIL', '31'),
                 'SKIP': ('—', '-', '2'), 'UNKNOWN': ('?', '?', '33')}
        symbol, plain, code = marks.get(status, ('?', '?', '33'))
        return self.paint(code, symbol if self.unicode else plain)


def _isatty(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


SECRET_WARNING = ('Будут показаны секретные данные. Убедитесь, что экран никто не видит\n'
                  'и сеанс терминала не записывается.')


def show_panel(term, reveal):
    rows = panel_access()
    if not reveal(term, 'Показать URL панели, логин и пароль?'):
        return CANCELLED
    width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        term.say(f'{label + ":":{width}} {value}')
    term.say()
    term.say(PASSWORD_NOTE)
    return OK


def show_admin(term, reveal):
    url, (status, reason) = admin_subscription()
    if not reveal(term, 'Показать ссылку подписки admin?'):
        return CANCELLED
    # On its own line, so a terminal selection copies exactly the URL.
    term.say('Ссылка подписки:')
    term.say(url)
    term.say()
    if status == 'PASS':
        term.say(term.mark('PASS') + ' Клиент admin подтверждён в runtime Xray (UUID и Vision как в state).')
    elif status == 'WARN':
        term.say(term.mark('WARN') + ' Клиент admin не найден в runtime Xray или отличается от state: '
                 'ссылка может не работать.')
    else:
        term.say(term.mark('UNKNOWN') + ' Наличие клиента admin в runtime Xray не подтверждено.')
        term.say('  Причина: ' + reason)
    term.say('Ссылка взята из state установки. Если клиента admin изменили или удалили\n'
             'в 3x-ui, она может не работать; проверка: bob doctor.')
    return OK


def show_status(term, heading=True):
    rows = server_status()
    if heading:
        term.say('Состояние сервера')
    for label, status, summary, details in rows:
        days = details.get('daysRemaining')
        suffix = f' Осталось дней: {days}.' if isinstance(days, int) else ''
        term.say(f'  {label:11} {term.mark(status)} {summary}{suffix}')
    term.say(f'  {"Bob CLI":11} {version()}')
    return OK


def show_report(term):
    summary = acceptance_summary()
    term.say(f'Отчёт установки от {summary["created"]}')
    mark = term.mark('PASS' if not summary['failed'] else 'FAIL')
    term.say(f'  {mark} Пройдено проверок: {summary["passed"]} из {summary["total"]}.')
    for name in summary['failed'][:20]:
        term.say(f'  {term.mark("FAIL")} {name}')
    if len(summary['failed']) > 20:
        term.say(f'  … и ещё {len(summary["failed"]) - 20}.')
    term.say('Отчёт фиксирует результат на момент установки; текущее состояние — bob doctor.')
    return OK


def _flag_reveal(allowed):
    def reveal(term, question):
        if allowed:
            return True
        if not term.interactive:
            raise CliError('Секреты не выводятся без подтверждения. Без терминала укажите '
                           '--show-secrets.', CANCELLED)
        term.say(SECRET_WARNING)
        return term.confirm(question)
    return reveal


def _menu_reveal(term, question):
    term.say(SECRET_WARNING)
    return term.confirm(question)


MENU = (
    ('1', 'Доступ к панели', 'URL, исходный логин и пароль'),
    ('2', 'Подписка admin', 'ссылка постоянного клиента'),
    ('3', 'Bob Doctor', 'быстрая диагностика'),
    ('4', 'Глубокая диагностика', 'doctor --deep, до минуты'),
    ('5', 'Состояние сервера', 'службы, сертификат, версия'),
    ('6', 'Отчёт установки', 'сводка acceptance.json'),
    ('0', 'Выход', ''),
)


def draw_menu(term):
    term.clear()
    width = max(20, min(48, shutil.get_terminal_size((80, 24)).columns - 1))
    title = f'BOB AUTO XRAY · {version()}'
    subtitle = 'Управление сервером · только чтение'
    if term.unicode and width >= 40:
        inner = width - 2
        term.say(term.paint('36;1', '╭' + '─' * inner + '╮'))
        term.say(term.paint('36;1', '│' + title.center(inner) + '│'))
        term.say(term.paint('36', '│' + subtitle.center(inner) + '│'))
        term.say(term.paint('36;1', '╰' + '─' * inner + '╯'))
    else:
        term.say(title)
        term.say(subtitle)
        term.say('-' * min(width, len(subtitle)))
    for key, label, hint in MENU:
        line = f'  {term.paint("1", key)}  {label}'
        if hint and width >= 46:
            line += ' ' * max(1, 24 - len(label)) + term.paint('2', hint)
        term.say(line)
    term.say()


def run_menu(term):
    require_root()
    actions = {
        '1': lambda: show_panel(term, _menu_reveal),
        '2': lambda: show_admin(term, _menu_reveal),
        '3': lambda: doctor.main([]),
        '4': lambda: _deep(term),
        '5': lambda: show_status(term, heading=False),
        '6': lambda: show_report(term),
    }
    notice = ''
    while True:
        draw_menu(term)
        if notice:
            term.say(notice)
            notice = ''
        try:
            choice = term.ask('Выберите пункт [0-6] › ')
        except EOFError:
            term.say()
            return OK
        if choice == '0':
            return OK
        if choice not in actions:
            notice = term.paint('33', 'Нет такого пункта. Введите цифру от 0 до 6.')
            continue
        term.clear()
        term.say(term.paint('1', dict((k, label) for k, label, _ in MENU)[choice]))
        term.say()
        secret = choice in ('1', '2')
        try:
            result = actions[choice]()
            if secret and result == CANCELLED:
                term.say('Отменено: данные не показаны.')
        except CliError as error:
            term.say(term.paint('31', str(error)))
            secret = False
        try:
            term.ask('\nEnter — ' + ('скрыть и вернуться в меню' if secret else 'вернуться в меню') + ' › ')
        except EOFError:
            term.clear(scrollback=secret)
            return OK
        except KeyboardInterrupt:
            term.clear(scrollback=secret)
            raise
        term.clear(scrollback=secret)


def _deep(term):
    term.say('Выполняется глубокая диагностика: локальные HTTPS-запросы и внешние trace,\n'
             'обычно до минуты.')
    term.say()
    return doctor.main(['--deep'])


def _options(rest, allowed):
    if any(option not in allowed for option in rest):
        raise CliError('Неизвестный параметр. Справка: bob --help', USAGE)
    return set(rest)


def main(argv=None, stdin=None, stdout=None, env=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    term = Terminal(stdin or sys.stdin, stdout or sys.stdout, os.environ if env is None else env)
    try:
        if not argv:
            if term.interactive:
                return run_menu(term)
            term.say(HELP.format(version=version()).rstrip())
            return OK
        command, rest = argv[0], argv[1:]
        if command == 'doctor':
            return doctor.main(rest)
        if command.startswith('--') and set(argv) <= {'--deep', '--json'}:
            # v0.4.0 `bob` ran Doctor for any arguments; keep `bob --json` working.
            return doctor.main(argv)
        if command in ('-h', '--help', 'help') and not rest:
            term.say(HELP.format(version=version()).rstrip())
            return OK
        if command == '--version' and not rest:
            term.say(version())
            return OK
        if command in ('panel', 'admin'):
            options = _options(rest, {'--show-secrets'})
            show = show_panel if command == 'panel' else show_admin
            result = show(term, _flag_reveal('--show-secrets' in options))
            if result == CANCELLED:
                print('Отменено: данные не показаны.', file=sys.stderr)
            return result
        if command in ('status', 'report'):
            _options(rest, set())
            return show_status(term) if command == 'status' else show_report(term)
        raise CliError('Неизвестная команда. Справка: bob --help', USAGE)
    except CliError as error:
        print(str(error), file=sys.stderr)
        return error.code
    except KeyboardInterrupt:
        term.say()
        return INTERRUPTED


if __name__ == '__main__':
    sys.exit(main())
