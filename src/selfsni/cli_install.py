"""Layout of the installed `bob` CLI and its explicit, CLI-only upgrade.

The library directory and the command are Bob's only when their exact content
proves it: the command must byte-match a wrapper Bob wrote, the library must
match a known release tree or its own manifest. Anything else is left alone.
`install.sh cli-upgrade` replaces both atomically and rolls back on failure;
it never provisions, restarts services or touches the VPN configuration.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

LIBRARY = Path('/usr/local/lib/bob-auto-xray')
COMMAND = Path('/usr/local/bin/bob')
PYTHON = Path('/usr/bin/python3')
INSTALL_LOCK = Path('/run/lock/bob-auto-xray-install.lock')
MANIFEST = '.bob-cli.json'
ROOT_UID = 0
# SHA256 tree digests of libraries installed by releases without a manifest:
# v0.4.0 copied src/selfsni of its archive (git tree v0.4.0) and nothing else.
LEGACY_TREES = {
    'b09fcb8ab83d25d4ee564d96dfddaf2cc992ac62365eef479c0a80b989fd6221': '0.4.0',
}
CLEAN_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/', 'LC_ALL': 'C.UTF-8'}

UP_TO_DATE, UPGRADED, FAILED, USAGE, REFUSED, NEED_ROOT, ROLLBACK_FAILED = 0, 0, 1, 2, 3, 4, 5


class NotOwned(Exception):
    """A path is not provably Bob's; the message is fixed text for the operator."""


def wrapper(library=LIBRARY, python=PYTHON):
    code = (f'import sys; sys.path.insert(0, {json.dumps(str(library))}); '
            'from selfsni.cli import main; sys.exit(main())')
    return ('#!/bin/sh\n# Bob Auto Xray CLI; replaced only by install.sh cli-upgrade.\nset -eu\n'
            f'exec {shlex.quote(str(python))} -I -B -c {shlex.quote(code)} "$@"\n')


def legacy_wrapper(library=LIBRARY, python=PYTHON):
    """Exactly what v0.4.0 install_doctor_cli() wrote."""
    code = (f'import sys; sys.path.insert(0, {json.dumps(str(library))}); '
            'from selfsni.doctor import main; sys.exit(main())')
    return ('#!/bin/sh\nset -eu\n'
            'if [ "${1:-}" = doctor ]; then shift; fi\n'
            f'exec {shlex.quote(str(python))} -I -B -c {shlex.quote(code)} "$@"\n')


def tree_digest(root, owner=None):
    """SHA256 over relative paths and file digests; refuses links and special files.

    __pycache__ directories with only .pyc files are ignored (not Bob's content,
    they disappear together with the replaced tree).
    """
    root = Path(root)
    entries = []

    def walk(directory, relative):
        for name in sorted(os.listdir(directory)):
            path, rel = directory / name, relative + (name,)
            info = os.lstat(path)
            if owner is not None and info.st_uid != owner:
                raise NotOwned('Владелец файла CLI не root.')
            if stat.S_ISLNK(info.st_mode):
                raise NotOwned('В каталоге CLI есть символическая ссылка.')
            if stat.S_ISDIR(info.st_mode):
                if info.st_mode & 0o022:
                    raise NotOwned('Каталог CLI доступен на запись группе или другим.')
                if name == '__pycache__':
                    if not all(stat.S_ISREG(os.lstat(path / x).st_mode) and x.endswith('.pyc')
                               for x in os.listdir(path)):
                        raise NotOwned('В каталоге CLI посторонние файлы.')
                    continue
                walk(path, rel)
            elif stat.S_ISREG(info.st_mode):
                if rel == (MANIFEST,):
                    continue
                if info.st_mode & 0o022 or info.st_nlink != 1:
                    raise NotOwned('Файл CLI доступен на запись группе или другим или имеет ссылки.')
                digest = hashlib.sha256()
                with open(path, 'rb') as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b''):
                        digest.update(chunk)
                entries.append('/'.join(rel) + '\0' + digest.hexdigest() + '\n')
            else:
                raise NotOwned('В каталоге CLI есть специальный файл.')
    walk(root, ())
    return hashlib.sha256(''.join(entries).encode()).hexdigest()


def stage_library(target, source, release_version):
    """Fill an empty directory with the package, VERSION and a manifest of both."""
    target = Path(target)
    shutil.copytree(Path(source), target / 'selfsni',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (target / 'VERSION').write_text(release_version + '\n')
    for directory, names, files in os.walk(target):
        os.chmod(directory, 0o755)
        for name in files:
            os.chmod(os.path.join(directory, name), 0o644)
    manifest = {'schema': 1, 'version': release_version, 'tree': tree_digest(target)}
    (target / MANIFEST).write_text(json.dumps(manifest, sort_keys=True) + '\n')
    os.chmod(target / MANIFEST, 0o644)
    return manifest


def inspect_library(path=LIBRARY, owner=ROOT_UID):
    """Return (version, tree digest) of a provably Bob-installed library."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise NotOwned('Каталог CLI Bob отсутствует.') from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise NotOwned('Каталог CLI является ссылкой или не каталогом.')
    if info.st_uid != owner or info.st_mode & 0o022:
        raise NotOwned('Каталог CLI принадлежит не root или доступен на запись другим.')
    digest = tree_digest(path, owner)
    manifest = path / MANIFEST
    try:
        fd = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if digest in LEGACY_TREES:
            return LEGACY_TREES[digest], digest
        raise NotOwned('Каталог CLI не совпадает ни с одним релизом Bob (изменён вручную?).') from None
    except OSError:
        raise NotOwned('Манифест CLI недоступен.') from None
    with os.fdopen(fd, 'rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise NotOwned('Манифест CLI не является обычным файлом.')
        raw = f.read(65536)
    try:
        value = json.loads(raw)
    except ValueError:
        raise NotOwned('Манифест CLI повреждён.') from None
    if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('tree') != digest
            or not isinstance(value.get('version'), str)
            or re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value['version']) is None):
        raise NotOwned('Каталог CLI не совпадает со своим манифестом (изменён вручную?).')
    return value['version'], digest


def inspect_command(path=COMMAND, library=LIBRARY, python=PYTHON, owner=ROOT_UID):
    """Return the exact wrapper bytes when the command is one Bob wrote."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise NotOwned('Команда bob отсутствует.') from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022
            or info.st_nlink != 1 or info.st_size > 4096):
        raise NotOwned('Команда bob — ссылка, чужой или изменяемый другими файл.')
    content = path.read_bytes()
    if content not in (legacy_wrapper(library, python).encode(), wrapper(library, python).encode()):
        raise NotOwned('Команда bob создана не Bob или изменена; она не будет заменена.')
    return content


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(path, content, mode=0o755):
    """Atomically replace path with content via a same-directory rename."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _sibling(path, tag):
    return path.parent / f'.{path.name}.{tag}.{secrets.token_hex(6)}'


def _run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False,
                            stdin=subprocess.DEVNULL, env=CLEAN_ENV, cwd='/')
    return result.returncode, result.stdout.strip()


def verify_command(command, release_version):
    """The published command must start and report the new version, and Doctor must load."""
    code, output = _run(str(command), '--version')
    if code or output != release_version:
        raise RuntimeError('version check failed')
    code, _ = _run(str(command), 'doctor', '--help')
    if code:
        raise RuntimeError('doctor check failed')


def upgrade(source, release_version, say=print, check_only=False, library=LIBRARY,
            command=COMMAND, python=PYTHON, owner=ROOT_UID, verify=verify_command):
    """Replace only a provably Bob-owned CLI; returns an exit code."""
    try:
        installed, installed_tree = inspect_library(library, owner)
        old_wrapper = inspect_command(command, library, python, owner)
    except NotOwned as error:
        say('Обновление CLI отклонено: ' + str(error))
        say('Чужие файлы не изменены. Bob CLI обновляется только поверх своей установки (v0.4.0+).')
        return REFUSED
    except OSError:
        say('Обновление CLI отклонено: файлы CLI недоступны для проверки.')
        return REFUSED
    current = wrapper(library, python).encode()
    # A real upgrade stages beside the library so that rename() stays on one filesystem.
    staging = Path(tempfile.mkdtemp(prefix=f'.{library.name}.new.',
                                    dir=None if check_only else library.parent))
    backup = None
    moved = replaced = False
    try:
        new_tree = stage_library(staging, source, release_version)['tree']
        if new_tree == installed_tree and old_wrapper == current:
            say(f'Bob CLI {installed} уже актуален; ничего не изменено.')
            return UP_TO_DATE
        say(f'Bob CLI: установлен {installed}, будет установлен {release_version}.')
        if check_only:
            say('Проверка пройдена: файлы принадлежат Bob. Ничего не изменено (--check).')
            return UP_TO_DATE
        # The staged copy must import and run before anything installed is touched.
        code, output = _run(str(python), '-I', '-B', '-c',
                            'import sys; sys.path.insert(0, sys.argv[1]); '
                            'from selfsni.cli import main; sys.exit(main(["--version"]))',
                            str(staging))
        if code or output != release_version:
            raise RuntimeError('staged CLI check failed')
        backup = _sibling(library, 'old')
        os.rename(library, backup)
        os.rename(staging, library)
        moved = True
        staging = None
        _fsync_directory(library.parent)
        if inspect_command(command, library, python, owner) != old_wrapper:
            raise RuntimeError('command changed during upgrade')
        _publish(command, current)
        replaced = True
        verify(command, release_version)
    except Exception:
        try:
            if replaced:
                _publish(command, old_wrapper)
            if backup is not None:
                failed = None
                if moved:
                    failed = _sibling(library, 'failed')
                    os.rename(library, failed)
                os.rename(backup, library)
                backup = None
                _fsync_directory(library.parent)
                if failed is not None:
                    shutil.rmtree(failed, ignore_errors=True)
        except Exception:
            say('ОШИБКА: обновление CLI прервано, откат не завершён.')
            if backup is not None:
                say(f'Прежняя библиотека сохранена в {backup}; восстановите её вручную в {library}.')
            return ROLLBACK_FAILED
        say('Обновление CLI не выполнено; прежняя версия CLI сохранена без изменений.')
        return FAILED
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    say(f'Bob CLI обновлён до {release_version}. VPN, службы и state не изменялись.')
    return UPGRADED


def main(argv=None):
    """Entry point of `install.sh cli-upgrade [--check]` from a verified release tree."""
    argv = sys.argv[1:] if argv is None else argv
    if any(x not in ('--check',) for x in argv):
        print('Использование: install.sh cli-upgrade [--check]', file=sys.stderr)
        return USAGE
    root = Path(__file__).resolve().parents[2]
    try:
        release_version = (root / 'VERSION').read_text().strip()
    except OSError:
        release_version = ''
    if (re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', release_version) is None
            or not (root / 'versions.lock.json').is_file()):
        print('Запускайте обновление CLI из проверенного архива релиза: install.sh cli-upgrade.',
              file=sys.stderr)
        return USAGE
    if os.geteuid() != ROOT_UID:
        print('Нужны права root: обновление CLI меняет /usr/local/bin/bob.', file=sys.stderr)
        return NEED_ROOT
    os.umask(0o022)
    from .core import InstallError
    from .install_state import InstallerLock
    try:
        # The installer lock also keeps two upgrades and a running install apart.
        with InstallerLock(INSTALL_LOCK):
            return upgrade(root / 'src/selfsni', release_version, check_only='--check' in argv)
    except InstallError:
        print('Выполняется установка Bob или другое обновление; повторите позже.', file=sys.stderr)
        return REFUSED


if __name__ == '__main__':
    sys.exit(main())
