"""Fail closed until official VPS-Server-Menu exposes safe automation."""
import re

from .core import InstallError, require, run

OFFICIAL_REPOSITORY = 'https://github.com/Diman6267/VPS-Server-Menu.git'
OFFICIAL_INSTALLER = 'https://raw.githubusercontent.com/Diman6267/VPS-Server-Menu/main/install.sh'
UNSAFE_MAIN = '3775143b00b197a801d9872ebc0bf5407431302d'


def check_official_menu(lock, command=run):
    """Inspect main without changing the host; reject unreviewed automation."""
    require(lock.get('menu_repository') == OFFICIAL_REPOSITORY and
            lock.get('menu_branch') == 'main' and
            lock.get('menu_installer_url') == OFFICIAL_INSTALLER and
            'menu_commit' not in lock and 'menu.tar.gz' not in lock.get('downloads', {}),
            'Настройки меню должны указывать только на официальный main')
    output = command('git', 'ls-remote', OFFICIAL_REPOSITORY, 'refs/heads/main', timeout=45)
    match = re.fullmatch(r'([0-9a-f]{40})\s+refs/heads/main\s*', output)
    require(match is not None, 'Не удалось определить commit официального main VPS-Server-Menu')
    commit = match.group(1)
    if commit == UNSAFE_MAIN:
        raise InstallError(
            f'Официальный VPS-Server-Menu main {commit} пока несовместим с безопасной автоматической установкой Bob: '
            'оригинальный install.sh делает git reset --hard и меняет .bashrc без параметров отключения; '
            'install_official_warp() создаёт cron restart и может запросить интерактивное восстановление; '
            'публичного non-interactive WARP CLI нет. Система не изменена. '
            'Нужен безопасный интерфейс в официальном upstream main.')
    raise InstallError(
        f'Официальный VPS-Server-Menu main {commit} изменился и ещё не проверен на совместимость. '
        'Система не изменена; проверьте оригинальный installer и WARP API перед новой попыткой.')
