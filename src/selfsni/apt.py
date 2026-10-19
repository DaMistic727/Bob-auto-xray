import os
import re
import time
from pathlib import Path

from .core import CommandError, InstallError, redact

LOCK_TIMEOUT = 600
NETWORK_RETRIES = 3

LOCK_PATTERNS = (
    'could not get lock', 'unable to acquire the dpkg frontend lock',
    'is another process using it', 'waiting for cache lock',
)
NETWORK_PATTERNS = (
    'temporary failure resolving', 'could not resolve', 'connection timed out',
    'connection failed', 'failed to fetch', 'network is unreachable',
    'tls connection was non-properly terminated',
)
DEPENDENCY_PATTERNS = (
    'unmet dependencies', 'held broken packages', 'pkgproblemresolver',
)
DPKG_PATTERNS = (
    'dpkg was interrupted', 'sub-process /usr/bin/dpkg returned an error',
    'error processing package', 'dpkg: error', 'dependency problems prevent',
)
SYSTEMD_PATTERNS = (
    'failed to connect to bus', 'system has not been booted with systemd',
)


def background_package_processes(proc_root=Path('/proc')):
    names = {'apt', 'apt-get', 'dpkg', 'unattended-upgrade', 'cloud-init'}
    found = set()
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            name = (entry/'comm').read_text().strip()
        except OSError:
            continue
        if name in names or name.startswith('unattended-upgr'):
            found.add(name)
    return found


class AptError(InstallError):
    def __init__(self, operation, category, command_error, detail):
        self.operation = operation
        self.category = category
        self.returncode = command_error.returncode
        self.output = command_error.output
        self.detail = detail
        code = 'timeout' if command_error.timed_out else command_error.returncode
        super().__init__(f'Ошибка обновления пакетов ({operation}, {category}, код {code}): {detail}')


def classify(error):
    text = error.output.lower()
    if error.timed_out:
        return 'timeout', 'истекло ограниченное время выполнения команды'
    if any(x in text for x in LOCK_PATTERNS):
        return 'lock', 'пакетный менеджер занят другим процессом'
    if any(x in text for x in SYSTEMD_PATTERNS):
        return 'systemd', 'пакетный скрипт не смог обратиться к systemd'
    if any(x in text for x in DEPENDENCY_PATTERNS):
        return 'dependencies', 'APT обнаружил несовместимые или удерживаемые зависимости'
    if any(x in text for x in DPKG_PATTERNS):
        return 'dpkg', 'dpkg не смог распаковать или настроить пакет'
    if any(x in text for x in NETWORK_PATTERNS):
        return 'network', 'временная ошибка загрузки из репозитория'
    return 'unknown', 'APT завершился с ошибкой без однозначной классификации'


def relevant_lines(output, limit=8):
    selected = []
    marker = re.compile(r'(?i)(^E:|^W:|error|failed|failure|lock|dpkg|depend|resolve|timed? out|systemd|bus)')
    for line in redact(output).splitlines():
        if marker.search(line.strip()):
            selected.append(line.strip())
    return selected[-limit:]


class AptRunner:
    def __init__(self, command, notify=lambda message: None, sleep=time.sleep,
                 lock_timeout=LOCK_TIMEOUT, network_retries=NETWORK_RETRIES):
        self.command = command
        self.notify = notify
        self.sleep = sleep
        self.lock_timeout = lock_timeout
        self.network_retries = network_retries

    def _execute(self, operation, args, timeout=900):
        base = ['apt-get', '-o', f'DPkg::Lock::Timeout={self.lock_timeout}']
        attempts = self.network_retries if operation == 'update' else 1
        busy = background_package_processes()
        if busy:
            self.notify('Пакетный менеджер занят, ожидаем завершения фонового обновления: ' +
                        ', '.join(sorted(busy)))
        for attempt in range(1, attempts + 1):
            try:
                return self.command(*base, *args, timeout=timeout,
                                    env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'))
            except CommandError as error:
                category, detail = classify(error)
                if category == 'lock':
                    self.notify('Пакетный менеджер занят, ожидаем завершения фонового обновления')
                if category == 'network' and attempt < attempts:
                    self.notify(f'Временная ошибка сети; повтор {attempt + 1}/{attempts}')
                    self.sleep(min(2 ** attempt, 8))
                    continue
                lines = relevant_lines(error.output)
                if lines:
                    detail += ': ' + ' | '.join(lines)
                raise AptError(operation, category, error, detail) from error

    def update(self):
        return self._execute('update', ['update'], timeout=self.lock_timeout + 120)

    def upgrade(self):
        return self._execute('upgrade', [
            '-o', 'Dpkg::Options::=--force-confold', '--no-remove',
            'upgrade', '-y',
        ], timeout=1800)

    def install(self, packages):
        return self._execute('install', [
            '-o', 'Dpkg::Options::=--force-confold', 'install', '-y',
            '--no-install-recommends', *packages,
        ], timeout=1800)
