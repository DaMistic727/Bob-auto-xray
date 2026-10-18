import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]

class InstallError(RuntimeError):
    pass

def require(condition, message):
    if not condition:
        raise InstallError(message)

def run(*args, timeout=180, env=None, input=None):
    # Never print commands: CLI arguments can contain generated credentials.
    try:
        p = subprocess.run([str(a) for a in args], input=input, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise InstallError(f'{Path(str(args[0])).name}: {type(e).__name__}') from e
    if p.returncode:
        # Output can contain keys, upstream install credentials or subscription IDs.
        raise InstallError(f'{Path(str(args[0])).name} exited {p.returncode}')
    return p.stdout

def write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), f'Refusing symlink: {path}')
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    path.chmod(mode)

def atomic_write(path, content, mode=0o600):
    """Replace a regular file atomically without following a target symlink."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), f'Refusing symlink: {path}')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def save(path, obj):
    write(path, json.dumps(obj, ensure_ascii=False, indent=2) + '\n')

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def download(url, path, sha256=None):
    require(url.startswith('https://'), 'HTTPS required')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=45) as response, open(path, 'wb') as f:
                require(response.url.startswith('https://'), 'Insecure redirect')
                shutil.copyfileobj(response, f)
            break
        except (OSError, TimeoutError):
            if attempt == 2:
                raise InstallError('Download failed: ' + url.split('?')[0])
            time.sleep(2)
    if sha256:
        require(digest(path) == sha256, f'SHA256 mismatch: {Path(path).name}')
    return Path(path)

def verify_bundle():
    lock = json.loads((ROOT / 'versions.lock.json').read_text())
    for name, expected in lock['files'].items():
        require(digest(ROOT / name) == expected, f'Bundled file changed: {name}')
    return lock

def extract(archive, destination):
    # Vendor menu and project release archives must contain ordinary files only.
    destination = Path(destination).resolve()
    with tarfile.open(archive) as tf:
        for m in tf.getmembers():
            target = (destination / m.name).resolve()
            require(destination in target.parents and not (m.issym() or m.islnk() or m.isdev()),
                    'Unsafe archive member')
        tf.extractall(destination)

def fqdn(value):
    try:
        value = value.strip().rstrip('.').encode('idna').decode('ascii').lower()
    except UnicodeError as e:
        raise InstallError('Invalid domain') from e
    labels = value.split('.')
    require(len(value) <= 253 and len(labels) > 1, 'Expected a public FQDN')
    for label in labels:
        require(1 <= len(label) <= 63 and label[0].isalnum() and label[-1].isalnum()
                and all(c.isalnum() or c == '-' for c in label), 'Invalid domain')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise InstallError('Use an FQDN, not an IP address')

def token(n=24):
    return secrets.token_hex(n)

def free_port(excluded):
    for _ in range(100):
        port = 20000 + secrets.randbelow(40000)
        if port in excluded:
            continue
        try:
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(('0.0.0.0', port))
            return port
        except OSError:
            pass
    raise InstallError('Cannot select a free port')

def wait_for(check, description, attempts=30):
    for _ in range(attempts):
        try:
            if check():
                return
        except (OSError, InstallError):
            pass
        time.sleep(2)
    raise InstallError('Timed out: ' + description)


def json_obj(value):
    """3x-ui endpoints return both JSON objects and JSON-encoded strings."""
    return json.loads(value) if isinstance(value, str) else value
