"""Fail closed until the official menu has a reviewed, pinned WARP API."""
import re
import tempfile
from pathlib import Path

from .core import InstallError, download, require, run

OFFICIAL_REPOSITORY = 'https://github.com/Diman6267/VPS-Server-Menu.git'
UNSAFE_MAIN = '3775143b00b197a801d9872ebc0bf5407431302d'


def pinned_menu_archive(lock):
    """Return only a commit-addressed official archive and its expected digest."""
    require(lock.get('menu_repository') == OFFICIAL_REPOSITORY and
            'menu_branch' not in lock and 'menu_installer_url' not in lock and
            'menu.tar.gz' not in lock.get('downloads', {}),
            'WARP требует только официальный VPS-Server-Menu с закреплённым commit')
    commit = lock.get('menu_commit')
    checksum = lock.get('menu_archive_sha256')
    if not (isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit) and
            isinstance(checksum, str) and re.fullmatch(r'[0-9a-f]{64}', checksum)):
        raise InstallError('Для установки WARP нужен проверенный commit и SHA256 '
                           'официального VPS-Server-Menu после объединения PR #6')
    require(commit != UNSAFE_MAIN, 'Закреплённый commit VPS-Server-Menu не содержит безопасный WARP API')
    return (f'https://github.com/Diman6267/VPS-Server-Menu/archive/{commit}.tar.gz', checksum)


def check_official_menu(lock, command=run, fetch=download):
    """Require official main and the downloaded archive to match the reviewed pin."""
    url, checksum = pinned_menu_archive(lock)
    output = command('git', 'ls-remote', OFFICIAL_REPOSITORY, 'refs/heads/main', timeout=45)
    match = re.fullmatch(r'([0-9a-f]{40})\s+refs/heads/main\s*', output)
    require(match is not None, 'Не удалось определить commit официального main VPS-Server-Menu')
    require(match.group(1) == lock['menu_commit'],
            'Официальный main VPS-Server-Menu не совпадает с проверенным pinned commit')
    with tempfile.TemporaryDirectory(prefix='bob-menu-check-') as directory:
        fetch(url, Path(directory) / 'menu.tar.gz', checksum)
    return lock['menu_commit']
