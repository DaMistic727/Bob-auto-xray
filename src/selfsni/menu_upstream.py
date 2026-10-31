"""Official rolling main checkout; no Bob revision pin or parallel updater."""
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from .core import digest, require, run

OFFICIAL_REPOSITORY = 'https://github.com/Diman6267/VPS-Server-Menu.git'
MENU_DIR = Path('/root/VPS-Server-Menu')
BIN_DIR = Path('/usr/local/bin')


def _official(lock):
    require(lock.get('menu_repository') == OFFICIAL_REPOSITORY, 'VPS-Server-Menu: origin не официальный')


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
    _official(lock)
    target=Path(target)
    require(not target.is_symlink(), 'VPS-Server-Menu: checkout symlink')
    if not target.exists(): return False
    require(target.is_dir() and (target/'.git').is_dir() and not (target/'.git').is_symlink(),
            'VPS-Server-Menu: повреждённый или неизвестный checkout')
    require(command('git','-C',target,'config','--local','--get-all','remote.origin.url').splitlines()==[OFFICIAL_REPOSITORY],
            'VPS-Server-Menu: origin не официальный')
    require(command('git','-C',target,'branch','--show-current').strip()=='main' and
            command('git','-C',target,'rev-parse','--abbrev-ref','@{upstream}').strip()=='origin/main',
            'VPS-Server-Menu: требуется main tracking origin/main; миграция только вручную')
    require(not command('git','--no-optional-locks','-c','core.fsmonitor=false','-c','core.filemode=false','-C',target,
                        'status','--porcelain','--untracked-files=all').strip(), 'VPS-Server-Menu: рабочее дерево изменено')
    require(not command('git','-C',target,'ls-files','--others','--ignored','--exclude-standard').strip(),
            'VPS-Server-Menu: неизвестные ignored файлы; очистите вручную')
    _manifest(target)
    return True


def _compatible(source):
    for name in ('install.sh','server-menu','menu_warp.sh'):
        require((source/name).is_file(), 'VPS-Server-Menu: отсутствует '+name)
    installer=(source/'install.sh').read_text()
    require(all(flag in installer for flag in ('--non-interactive','--no-autostart','--source-dir')),
            'VPS-Server-Menu: несовместимый unattended installer')
    require('xray-outbound' in (source/'menu_warp.sh').read_text() and '--json' in (source/'menu_warp.sh').read_text(),
            'VPS-Server-Menu: несовместимый WARP CLI')


def check_official_menu(lock, command=run, target=MENU_DIR):
    inspect_existing_menu(lock,target,command)
    with tempfile.TemporaryDirectory(prefix='bob-menu-check-') as directory:
        source=Path(directory)/'source'
        command('git','clone','--branch','main','--single-branch',OFFICIAL_REPOSITORY,source,timeout=300)
        inspect_existing_menu(lock,source,command)
        _compatible(source)
        _check_links(source,Path(target),BIN_DIR)
        return command('git','-C',source,'rev-parse','HEAD').strip()


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


def install_menu(lock, work, target=MENU_DIR, bin_dir=BIN_DIR, command=run):
    target,bin_dir=Path(target),Path(bin_dir)
    existing=inspect_existing_menu(lock,target,command)
    require(target.parent.is_dir() and not target.parent.is_symlink(), 'VPS-Server-Menu: неверный parent')
    with tempfile.TemporaryDirectory(prefix='.bob-menu-',dir=target.parent) as directory:
        source=Path(directory)/'source'
        command('git','clone','--branch','main','--single-branch',OFFICIAL_REPOSITORY,source,timeout=300)
        inspect_existing_menu(lock,source,command)
        _compatible(source)
        _check_links(source,target,bin_dir)
        if existing:
            command('git','-C',target,'fetch','origin','main',timeout=300)
            # Reject ahead/diverged histories; never reset/clean foreign work.
            command('git','-C',target,'merge-base','--is-ancestor','HEAD','origin/main')
            command('git','-c','core.filemode=false','-C',target,'merge','--ff-only','origin/main')
            inspect_existing_menu(lock,target,command)
            _compatible(target)
            _check_links(target,target,bin_dir)
        else:
            require(not target.exists() and not target.is_symlink(), 'VPS-Server-Menu: путь уже занят')
            source.rename(target)
    command('bash',target/'install.sh','--non-interactive','--no-autostart','--source-dir',target,timeout=900)
    inspect_existing_menu(lock,target,command)
    require(command('git','-C',target,'rev-parse','HEAD').strip()==command('git','-C',target,'rev-parse','origin/main').strip(),
            'VPS-Server-Menu: HEAD отличается от разрешённого origin/main')
    require(not _check_links(target,target,bin_dir), 'Установщик меню не создал нужные ссылки')
    return dict(origin=OFFICIAL_REPOSITORY,branch='main',commit=command('git','-C',target,'rev-parse','HEAD').strip(),
                resolved_at=datetime.now(timezone.utc).isoformat())
