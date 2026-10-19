import fcntl
import json
import os
from pathlib import Path
import uuid

from .core import InstallError, save

SCHEMA = 2
RESUMABLE_FAILURE_STAGES = {'APT_UPDATE', 'APT_UPGRADE', 'APT_INSTALL', 'WARP'}
STAGES = (
    'PREFLIGHT', 'APT_UPDATE', 'APT_UPGRADE', 'APT_INSTALL', 'BBR', 'FIREWALL',
    'WARP', 'CERTIFICATE', 'NGINX_PORTAL', 'PANEL', 'ROUTING', 'SECURITY',
    'ACCEPTANCE', 'HEALTHY_LOCAL',
)


class InstallerLock:
    def __init__(self, path=Path('/run/lock/bob-auto-xray-install.lock')):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+')
        self.path.chmod(0o600)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.file.close()
            raise InstallError('Другой экземпляр установщика уже выполняется') from error
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()) + '\n')
        self.file.flush()
        return self

    def __exit__(self, *_):
        if self.file:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()


def load(state_root):
    path = Path(state_root) / 'state.json'
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError('Состояние предыдущей установки повреждено; автоматическое продолжение запрещено') from error
    if not isinstance(value, dict):
        raise InstallError('Неизвестный формат состояния предыдущей установки')
    return value


def initialize(values, absent_paths):
    state = dict(values)
    state.update(schema=SCHEMA, installation_id=str(uuid.uuid4()), status='INSTALLING',
                 stage='APT_UPDATE', last_completed_stage='PREFLIGHT',
                 absent_paths=sorted(map(str, absent_paths)), resume_count=0)
    return state


def validate_resume(state, domain, adopt_warp):
    if state.get('status') == 'HEALTHY_LOCAL':
        raise InstallError('Установка уже завершена (HEALTHY_LOCAL); повторная установка запрещена')
    if state.get('schema') != SCHEMA or not state.get('installation_id'):
        raise InstallError('Состояние не доказывает принадлежность Bob Auto Xray; продолжение запрещено')
    stage = state.get('stage')
    if state.get('status') not in ('FAILED', 'INSTALLING') or stage not in RESUMABLE_FAILURE_STAGES:
        raise InstallError(f'Безопасное продолжение после этапа {stage or "UNKNOWN"} не поддерживается')
    if state.get('domain') != domain or bool(state.get('adopt_warp')) != bool(adopt_warp):
        raise InstallError('Для продолжения повторите исходные --domain и --adopt-warp')
    required = ('username', 'password', 'panel_port', 'sub_port', 'panel_path', 'sub_path')
    if any(not state.get(key) for key in required):
        raise InstallError('В состоянии отсутствуют исходные credentials; продолжение запрещено')
    state['resume_count'] = int(state.get('resume_count', 0)) + 1
    state['status'] = 'INSTALLING'
    return state


def upgrade_legacy_apt_failure(state, state_root, controlled_paths):
    """Recognize the narrow v0.3.3 failure produced before any managed path existed.

    The migration is returned in memory and is persisted only after the new
    read-only preflight succeeds.
    """
    root = Path(state_root)
    required = ('domain', 'username', 'password', 'panel_port', 'sub_port',
                'panel_path', 'sub_path', 'adopt_warp')
    if state.get('schema') is not None:
        return state
    if state.get('status') != 'INSTALLING' or any(key not in state for key in required) or \
            any(not state.get(key) for key in required if key != 'adopt_warp'):
        raise InstallError('Legacy state не содержит достаточных доказательств для resume')
    try:
        failure = (root/'FAILED').read_text().strip()
        progress = [line.split('Z ', 1)[-1] for line in
                    (root/'progress.log').read_text().splitlines() if line.strip()]
    except OSError as error:
        raise InstallError('Legacy state не содержит журнал исходного APT-сбоя') from error
    if failure != 'apt-get exited 100' or progress != [
            'Install distribution packages', 'FAILED: apt-get exited 100']:
        raise InstallError('Legacy state не соответствует известному раннему APT-сбою v0.3.3')
    present = [str(path) for path in map(Path, controlled_paths) if path.exists()]
    if present:
        raise InstallError('Legacy APT resume запрещён: появились управляемые пути: ' + ', '.join(present))
    upgraded = initialize(state, controlled_paths)
    upgraded['status'] = 'FAILED'
    upgraded['failure'] = 'legacy v0.3.3 APT exit 100; re-run from APT_UPDATE'
    upgraded['legacy_resume'] = 'v0.3.3-apt-exit-100'
    return upgraded


def checkpoint(state_root, state, stage, completed=False):
    state['stage'] = stage
    state['status'] = 'INSTALLING'
    if completed:
        state['last_completed_stage'] = stage
    save(Path(state_root) / 'state.json', state)


def fail(state_root, state, message):
    state['status'] = 'FAILED'
    state['failure'] = str(message)
    save(Path(state_root) / 'state.json', state)
