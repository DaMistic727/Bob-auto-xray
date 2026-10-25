"""Pinned official VPS-Server-Menu checkout and archive verification."""
import shutil
import tempfile
from pathlib import Path

from .core import digest, download, extract, require, run

OFFICIAL_REPOSITORY = 'https://github.com/Diman6267/VPS-Server-Menu.git'
SUPPORTED_COMMIT = '3ed5166bbc5b4fb4cc3bb26616bc4db3bbe2734c'
SUPPORTED_SHA256 = '9d7a2aced39fe206a9cb4cd46481ee6876471458a63988b9fca152238fa7ec29'
MENU_DIR = Path('/root/VPS-Server-Menu')
BIN_DIR = Path('/usr/local/bin')


def pinned_menu_archive(lock):
    require(lock.get('menu_repository') == OFFICIAL_REPOSITORY and
            'menu_branch' not in lock and 'menu_installer_url' not in lock and
            'menu.tar.gz' not in lock.get('downloads', {}),
            'WARP требует только официальный VPS-Server-Menu с закреплённым commit')
    require(lock.get('menu_commit') == SUPPORTED_COMMIT and
            lock.get('menu_archive_sha256') == SUPPORTED_SHA256,
            'Неподдерживаемый commit или SHA256 официального VPS-Server-Menu')
    return (f'https://codeload.github.com/Diman6267/VPS-Server-Menu/tar.gz/{SUPPORTED_COMMIT}',
            SUPPORTED_SHA256)


def _manifest(root):
    result = {}
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if '.git' in relative.parts:
            continue
        require(not path.is_symlink(), f'Symlink в VPS-Server-Menu: {relative}')
        if path.is_file():
            result[str(relative)] = digest(path)
        else:
            require(path.is_dir(), f'Необычный файл в VPS-Server-Menu: {relative}')
    return result


def inspect_existing_menu(lock, target=MENU_DIR, command=run):
    """Read-only check before Bob touches state, APT, or system services."""
    pinned_menu_archive(lock)
    target = Path(target)
    require(not target.is_symlink(), 'VPS-Server-Menu: путь checkout является symlink')
    if not target.exists():
        return False
    require(target.is_dir() and (target / '.git').is_dir() and
            not (target / '.git').is_symlink(),
            'VPS-Server-Menu: существующий путь не является обычным Git checkout; перенесите его вручную')
    require(command('git', '-C', target, 'remote', 'get-url', 'origin').strip() == OFFICIAL_REPOSITORY,
            'VPS-Server-Menu: origin не официальный; миграция существующего checkout только вручную')
    require(command('git', '-C', target, 'rev-parse', 'HEAD').strip() == SUPPORTED_COMMIT,
            'VPS-Server-Menu: HEAD не совпадает с закреплённым commit; миграция только вручную')
    require(not command('git', '-c', 'core.filemode=false', '-C', target,
                        'status', '--porcelain', '--untracked-files=all').strip(),
            'VPS-Server-Menu: рабочее дерево изменено; устраните изменения вручную')
    _manifest(target)
    return True


def _archive_source(lock, work, fetch=download):
    url, checksum = pinned_menu_archive(lock)
    archive = Path(work) / 'menu.tar.gz'
    fetch(url, archive, checksum)
    require(digest(archive) == checksum, 'SHA256 архива VPS-Server-Menu не совпадает')
    unpacked = Path(work) / 'menu-archive'
    unpacked.mkdir()
    extract(archive, unpacked)
    children = list(unpacked.iterdir())
    require(len(children) == 1 and children[0].is_dir(), 'Некорректная структура архива VPS-Server-Menu')
    source = children[0]
    require((source / 'install.sh').is_file() and (source / 'server-menu').is_file() and
            (source / 'menu_warp.sh').is_file(), 'В архиве нет официального WARP API')
    return source


def check_official_menu(lock, command=run, fetch=download, target=MENU_DIR):
    """Verify pin, archive bytes and existing checkout before system changes."""
    existing = inspect_existing_menu(lock, target, command)
    with tempfile.TemporaryDirectory(prefix='bob-menu-check-') as directory:
        source = _archive_source(lock, directory, fetch)
        if existing:
            require(_manifest(source) == _manifest(Path(target)),
                    'VPS-Server-Menu checkout отличается от проверенного архива')
        _check_links(source, Path(target), BIN_DIR)
    return SUPPORTED_COMMIT


def _check_links(source, target, bin_dir):
    require(not bin_dir.is_symlink(), f'Каталог команд меню является symlink: {bin_dir}')
    needs_install = False
    for path in source.iterdir():
        if not (path.is_file() and (path.suffix == '.sh' or path.name in ('server-menu', 'ipv6-menu'))):
            continue
        link = bin_dir / path.name
        if link.exists() or link.is_symlink():
            require(link.is_symlink() and link.readlink() == target / path.name,
                    f'Посторонняя команда меню: {link}; устраните конфликт вручную')
        else:
            needs_install = True
    return needs_install


def install_menu(lock, work, target=MENU_DIR, bin_dir=BIN_DIR, command=run, fetch=download):
    """Install from a verified Git checkout without replacing existing data."""
    target, bin_dir = Path(target), Path(bin_dir)
    existing = inspect_existing_menu(lock, target, command)
    source = _archive_source(lock, work, fetch)
    if existing:
        require(_manifest(source) == _manifest(target),
                'VPS-Server-Menu checkout отличается от проверенного архива')
    needs_install = _check_links(source, target, bin_dir)
    if not existing:
        require(not target.exists() and not target.is_symlink(),
                'VPS-Server-Menu: путь уже занят; миграция только вручную')
        require(target.parent.is_dir() and not target.parent.is_symlink(),
                'Некорректный родительский каталог VPS-Server-Menu')
        staging = Path(tempfile.mkdtemp(prefix='.bob-menu-', dir=target.parent))
        try:
            command('git', '-C', staging, 'init')
            command('git', '-C', staging, 'remote', 'add', 'origin', OFFICIAL_REPOSITORY)
            command('git', '-C', staging, 'fetch', '--depth=1', 'origin', SUPPORTED_COMMIT, timeout=300)
            command('git', '-C', staging, 'checkout', '--detach', 'FETCH_HEAD')
            inspect_existing_menu(lock, staging, command)
            require(_manifest(source) == _manifest(staging),
                    'Git checkout меню отличается от SHA256-проверенного архива')
            require(not target.exists() and not target.is_symlink(),
                    'VPS-Server-Menu: путь появился во время подготовки; установка остановлена')
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        needs_install = True
    if needs_install:
        command('bash', target / 'install.sh', '--non-interactive', '--no-autostart',
                '--source-dir', target, '--pinned-commit', SUPPORTED_COMMIT, timeout=900)
        inspect_existing_menu(lock, target, command)
        require(not _check_links(source, target, bin_dir), 'Установщик меню не создал нужные ссылки')
    return SUPPORTED_COMMIT
