"""Install the reviewed Xray binary independently of the pinned 3x-ui archive."""
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

from .core import InstallError, digest, download, require, run


XRAY_ARCHIVES = {
    'amd64': 'Xray-linux-64.zip',
    'arm64': 'Xray-linux-arm64-v8a.zip',
}


def pinned_xray_spec(lock, arch):
    require(lock.get('xray_version') == 'v26.10.21', 'Unreviewed Xray version')
    require(arch in XRAY_ARCHIVES, 'Unsupported Xray architecture')
    spec = lock.get('xray_downloads', {}).get(arch, {})
    expected_url = ('https://github.com/XTLS/Xray-core/releases/download/'
                    + lock['xray_version'] + '/' + XRAY_ARCHIVES[arch])
    require(spec.get('url') == expected_url, 'Xray source is not the pinned official release')
    for key in ('sha256', 'binary_sha256'):
        require(re.fullmatch(r'[0-9a-f]{64}', spec.get(key, '')) is not None,
                'Missing Xray ' + key)
    return spec


def install_pinned_xray(lock, arch, work, destination=None):
    """Verify both archive and binary, then replace the stopped panel's core."""
    spec = pinned_xray_spec(lock, arch)
    destination = Path(destination or '/usr/local/x-ui/bin/xray-linux-' + arch)
    require(destination.is_file() and not destination.is_symlink(),
            'Panel Xray binary is absent or unsafe')
    # The panel archive carries the withdrawn core; never leave it running
    # while fetching or validating the separately pinned replacement.
    run('systemctl', 'stop', 'x-ui')
    archive_path = download(spec['url'], Path(work)/XRAY_ARCHIVES[arch], spec['sha256'])
    require(digest(archive_path) == spec['sha256'], 'Xray archive SHA256 mismatch')

    staged = None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = [item for item in archive.infolist() if item.filename == 'xray']
            require(len(members) == 1 and
                    stat.S_IFMT(members[0].external_attr >> 16) == stat.S_IFREG and
                    0 < members[0].file_size <= 64 * 1024 * 1024,
                    'Official Xray archive has no regular xray binary')
            fd, staged = tempfile.mkstemp(prefix='.xray-', dir=destination.parent)
            with os.fdopen(fd, 'wb') as target, archive.open(members[0]) as source:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fchmod(target.fileno(), 0o755)
                os.fsync(target.fileno())
        require(digest(staged) == spec['binary_sha256'], 'Xray binary SHA256 mismatch')
        os.replace(staged, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        version = run(str(destination), 'version').splitlines()[0]
        require(version.startswith('Xray 26.10.21 ') and '1b1647c' in version,
                'Pinned Xray version/commit mismatch')
        return spec['binary_sha256']
    except zipfile.BadZipFile as error:
        raise InstallError('Invalid pinned Xray archive') from error
    finally:
        if staged is not None and os.path.exists(staged):
            os.unlink(staged)
