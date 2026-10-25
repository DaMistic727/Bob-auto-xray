"""Optional, persistent 2 GiB swap file for a fresh installation."""
import os
from pathlib import Path
import shutil

from .core import InstallError, atomic_write, require, run

FILE = Path('/swapfile')
FSTAB = Path('/etc/fstab')
LINE = '/swapfile none swap sw 0 0'
SIZE = 2 * 1024**3
RESERVE = 2 * 1024**3


def preflight(command=run, file=FILE, fstab=FSTAB):
    """Read-only ownership check before the network stage changes anything."""
    require(not file.is_symlink() and not fstab.is_symlink(), 'Swap path is a symlink')
    lines = fstab.read_text().splitlines()
    require(not any(row.split()[:1] == [str(file)] and row != LINE for row in lines),
            'Foreign /swapfile fstab entry')
    active = str(file) in command('swapon', '--noheadings', '--raw', '--output=NAME').splitlines()
    if file.exists():
        require(file.is_file() and file.stat().st_size == SIZE and
                file.stat().st_mode & 0o777 == 0o600 and active and LINE in lines,
                'Existing /swapfile is not the managed 2 GiB swap; refusing to overwrite it')
    else:
        require(LINE not in lines and not active, 'Stale /swapfile fstab or runtime entry')
    return {'existing': file.exists()}


def configure(command=run, file=FILE, fstab=FSTAB, disk_usage=shutil.disk_usage):
    """Keep existing swap untouched; skip when capacity is insufficient."""
    current = preflight(command, file, fstab)
    if current['existing']:
        return {'status': 'existing', 'path': str(file)}
    original = fstab.read_text()
    if disk_usage(file.parent).free < SIZE + RESERVE:
        return {'status': 'skipped', 'reason': 'insufficient free disk space'}
    updated = None
    try:
        command('fallocate', '-l', '2G', str(file))
        os.chmod(file, 0o600)
        command('mkswap', str(file))
        command('swapon', str(file))
        updated = original + ('' if original.endswith('\n') else '\n') + LINE + '\n'
        atomic_write(fstab, updated, fstab.stat().st_mode & 0o777)
        require(file.stat().st_size == SIZE and
                str(file) in command('swapon', '--noheadings', '--raw', '--output=NAME').splitlines(),
                'Swap activation verification failed')
        require(fstab.read_text().splitlines().count(LINE) == 1,
                'Swap persistence verification failed')
    except Exception as error:
        failures = []
        try:
            if str(file) in command('swapon', '--noheadings', '--raw', '--output=NAME').splitlines():
                command('swapoff', str(file))
        except Exception as rollback_error:
            failures.append('swapoff: ' + str(rollback_error))
        try:
            if updated is not None and fstab.read_text() == updated:
                atomic_write(fstab, original, fstab.stat().st_mode & 0o777)
            elif fstab.read_text() != original:
                failures.append('fstab changed unexpectedly')
        except Exception as rollback_error:
            failures.append('fstab restore: ' + str(rollback_error))
        if not failures:
            try:
                file.unlink(missing_ok=True)
            except Exception as rollback_error:
                failures.append('swap file removal: ' + str(rollback_error))
        if failures:
            raise InstallError('Swap rollback incomplete: ' + '; '.join(failures)) from error
        raise
    return {'status': 'created', 'path': str(file), 'bytes': SIZE}


def verify(report, command=run, file=FILE, fstab=FSTAB):
    if report.get('status') == 'skipped':
        require(not file.exists(), 'Skipped swap unexpectedly has a file')
        return report
    require(file.is_file() and not file.is_symlink() and file.stat().st_size == SIZE,
            'Managed 2 GiB swap file is missing or changed')
    require(file.stat().st_mode & 0o777 == 0o600, 'Swap file permissions changed')
    require(fstab.read_text().splitlines().count(LINE) == 1, 'Swap fstab entry missing')
    require(str(file) in command('swapon', '--noheadings', '--raw', '--output=NAME').splitlines(),
            'Swap is not active')
    return report
