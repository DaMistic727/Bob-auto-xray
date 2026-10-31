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
import threading
import time
import urllib.request
import re
import signal

ROOT = Path(__file__).resolve().parents[2]

class InstallError(RuntimeError):
    pass

class CommandError(InstallError):
    def __init__(self, command, returncode=None, output='', timed_out=False):
        self.command = Path(str(command)).name
        self.returncode = returncode
        self.output = output or ''
        self.timed_out = timed_out
        detail = 'timed out' if timed_out else f'exited {returncode}'
        super().__init__(f'{self.command} {detail}')

_DIAGNOSTIC_LOG = None
_SECRETS = set()
_ACTIVITY = None

def configure_activity(callback=None):
    global _ACTIVITY
    _ACTIVITY = callback

def _pulse():
    if _ACTIVITY:
        _ACTIVITY()

def configure_diagnostics(path=None, secrets=()):
    """Enable a root-only command transcript without recording command arguments."""
    global _DIAGNOSTIC_LOG, _SECRETS
    _DIAGNOSTIC_LOG = Path(path) if path else None
    _SECRETS = {str(x) for x in secrets if x}
    if _DIAGNOSTIC_LOG:
        _DIAGNOSTIC_LOG.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(_DIAGNOSTIC_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        _DIAGNOSTIC_LOG.chmod(0o600)

def register_secrets(*values):
    _SECRETS.update(str(x) for x in values if x)

def redact(value):
    text = str(value)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(password|token|private.?key|uuid)(\s*[:=]\s*)\S+',
                  r'\1\2[REDACTED]', text)
    text = re.sub(r'\b[0-9a-f]{8}-[0-9a-f-]{27,}\b', '[REDACTED-UUID]', text,
                  flags=re.I)
    text = re.sub(r'\b[A-Za-z0-9_-]{32,}\b', '[REDACTED-VALUE]', text)
    return text

def _log_command(command, returncode, output):
    if not _DIAGNOSTIC_LOG:
        return
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    body = redact(output).rstrip()
    with _DIAGNOSTIC_LOG.open('a') as log:
        log.write(f'[{stamp}] {Path(str(command)).name} exit={returncode}\n')
        if body:
            log.write(body + '\n')
    _DIAGNOSTIC_LOG.chmod(0o600)

def require(condition, message):
    if not condition:
        raise InstallError(message)

def _output_text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value or ''

def run(*args, timeout=180, env=None, input=None, on_output=None):
    # Never print commands: CLI arguments can contain generated credentials.
    command = [str(a) for a in args]
    try:
        with tempfile.TemporaryFile() as transcript:
            p = subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                 stdout=transcript, stderr=subprocess.STDOUT, env=env,
                                 start_new_session=True)
            try:
                read_offset = 0
                if input is not None:
                    p.stdin.write(input.encode() if isinstance(input, str) else input)
                    p.stdin.close()
                deadline = time.monotonic() + timeout
                while p.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        try:
                            os.killpg(p.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        p.wait()
                        transcript.seek(0)
                        output = transcript.read().decode('utf-8', errors='replace')
                        _log_command(args[0], 'timeout', output)
                        raise CommandError(args[0], output=output, timed_out=True)
                    try:
                        p.wait(timeout=min(1, remaining))
                    except subprocess.TimeoutExpired:
                        if on_output:
                            transcript.seek(read_offset)
                            chunk = transcript.read()
                            read_offset = transcript.tell()
                            if chunk:
                                on_output(chunk.decode('utf-8', errors='replace'))
                        _pulse()
            except BaseException:
                if p.poll() is None:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    p.wait()
                raise
            transcript.seek(0)
            output = transcript.read().decode('utf-8', errors='replace')
    except OSError as e:
        _log_command(args[0], 'os-error', str(e))
        raise CommandError(args[0], output=str(e)) from e
    _log_command(args[0], p.returncode, output)
    if p.returncode:
        raise CommandError(args[0], p.returncode, output)
    return output

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
    # Installer state is rewritten at every checkpoint; a crash must leave the
    # previous complete JSON, never a truncated file that blocks resume.
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + '\n')

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def download(url, path, sha256=None):
    require(url.startswith('https://'), 'HTTPS required')
    done = threading.Event()
    cancelled = threading.Event()
    result = []

    def fetch():
        try:
            for attempt in range(3):
                if cancelled.is_set():
                    return
                try:
                    with urllib.request.urlopen(url, timeout=45) as response, open(path, 'wb') as f:
                        require(response.url.startswith('https://'), 'Insecure redirect')
                        while not cancelled.is_set():
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            if cancelled.is_set():
                                return
                            f.write(chunk)
                    if cancelled.is_set():
                        return
                    if sha256:
                        require(digest(path) == sha256, f'SHA256 mismatch: {Path(path).name}')
                    return
                except (OSError, TimeoutError) as error:
                    if attempt == 2:
                        raise InstallError('Не удалось загрузить: ' + url.split('?')[0]) from error
                    cancelled.wait(2)
        except Exception as error:
            result.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=fetch, daemon=True)
    worker.start()
    try:
        while not done.wait(1):
            _pulse()
    except BaseException:
        cancelled.set()
        Path(path).unlink(missing_ok=True)
        raise
    if result:
        raise result[0]
    return Path(path)

def verify_bundle(warp_mode='install'):
    require(warp_mode in ('install', 'adopt', 'skip'), 'Unknown WARP mode')
    lock = json.loads((ROOT / 'versions.lock.json').read_text())
    for name, expected in lock['files'].items():
        if warp_mode != 'install' and name == 'vendor/cloudflare-pubkey.gpg':
            continue
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
        raise InstallError('Некорректный домен') from e
    labels = value.split('.')
    require(len(value) <= 253 and len(labels) > 1, 'Укажите полный домен, например vpn.example.com')
    for label in labels:
        require(1 <= len(label) <= 63 and label[0].isalnum() and label[-1].isalnum()
                and all(c.isalnum() or c == '-' for c in label), 'Некорректный домен')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise InstallError('Укажите домен, а не IP-адрес')

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
