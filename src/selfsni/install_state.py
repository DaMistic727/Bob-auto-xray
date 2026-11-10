import fcntl
import json
import os
from pathlib import Path
import uuid

from .core import InstallError, save

SCHEMA = 2
WARP_MODES = ('install', 'adopt', 'skip')


def warp_mode(state):
    """Read old state files without changing their credentials or identity."""
    mode = state.get('warp_mode')
    if mode is None:
        mode = 'adopt' if state.get('adopt_warp', False) else 'install'
    if mode not in WARP_MODES or ('adopt_warp' in state and
                                  bool(state['adopt_warp']) != (mode == 'adopt')):
        raise InstallError('Некорректный режим WARP в сохранённом состоянии')
    return mode
# Who owns an optional host setting (state['ipv6'|'bbr']['mode']); see ipv6.py, bbr.py.
POLICY_MODES = IPV6_MODES = ('managed', 'external', 'skipped')


def policy_mode(report):
    """Mode of an IPv6 or BBR stage result. Results written before modes existed were managed."""
    if not isinstance(report, dict):
        raise ValueError('policy')
    mode = report.get('mode', 'managed')
    if mode not in POLICY_MODES:
        raise ValueError('policy mode')
    return mode


ipv6_mode = bbr_mode = policy_mode


# FIREWALL only adds missing allow rules and undoes its own commands on failure.
RESUMABLE_FAILURE_STAGES = {'APT_UPDATE', 'APT_UPGRADE', 'APT_INSTALL', 'FIREWALL', 'WARP', 'CERTIFICATE'}
# After PANEL every credential, key and identity exists; a late resume reuses
# them as saved and repeats only unfinished SECURITY steps and acceptance.
LATE_RESUME_STAGES = ('SECURITY', 'ACCEPTANCE')
SECURITY_STEPS = ('fail2ban', 'renewal', 'firewall', 'doctor')
PANEL_RESULTS = ('ipv4', 'arch', 'ssh_ports', 'ipv6', 'bbr', 'swap', 'portal', 'php_service',
                 'api_token', 'xray_sha256', 'xray_bin', 'reality_private', 'reality_public',
                 'short_id', 'admin_client', 'xray_version', 'xui_version')
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


def validate_resume(state, domain, mode):
    if isinstance(mode, bool):
        mode = 'adopt' if mode else 'install'
    if state.get('status') == 'HEALTHY_LOCAL':
        raise InstallError('Установка уже завершена (HEALTHY_LOCAL); повторная установка запрещена')
    if state.get('schema') != SCHEMA or not state.get('installation_id'):
        raise InstallError('Состояние не доказывает принадлежность Bob Auto Xray; продолжение запрещено')
    stage = state.get('stage')
    if state.get('status') not in ('FAILED', 'INSTALLING') or \
            stage not in RESUMABLE_FAILURE_STAGES | set(LATE_RESUME_STAGES):
        raise InstallError(f'Безопасное продолжение после этапа {stage or "UNKNOWN"} не поддерживается')
    if state.get('domain') != domain or warp_mode(state) != mode:
        raise InstallError('Для продолжения повторите исходные --domain и режим WARP (--adopt-warp/--no-warp)')
    required = ('username', 'password', 'panel_port', 'sub_port', 'panel_path', 'sub_path')
    if any(not state.get(key) for key in required):
        raise InstallError('В состоянии отсутствуют исходные credentials; продолжение запрещено')
    if stage in LATE_RESUME_STAGES:
        validate_late_stage(state, mode)
    state['resume_count'] = int(state.get('resume_count', 0)) + 1
    state['warp_mode'] = mode
    state['status'] = 'INSTALLING'
    return state


def validate_late_stage(state, mode):
    """Fail closed unless the saved state is a complete record of PANEL and earlier."""
    stage, last = state['stage'], state.get('last_completed_stage')
    expected = {'SECURITY': ('PANEL', 'SECURITY'), 'ACCEPTANCE': ('SECURITY', 'ACCEPTANCE')}[stage]
    if last not in expected:
        raise InstallError(f'Этап {stage} не согласован с last_completed_stage; resume запрещён')
    required = PANEL_RESULTS + (('menu_revision',) if mode != 'skip' else ())
    missing = [key for key in required if not state.get(key)]
    admin = state.get('admin_client')
    if missing or not isinstance(admin, dict) or not admin.get('id') or not admin.get('subId'):
        raise InstallError('Состояние не содержит результатов установки панели '
                           f'({", ".join(missing) or "admin_client"}); resume запрещён')
    for key, label in (('ipv6', 'IPv6'), ('bbr', 'TCP BBR/FQ')):
        try:
            policy_mode(state[key])
        except ValueError as error:
            raise InstallError(f'Результат этапа {label} в состоянии повреждён; resume запрещён') from error
    steps = state.get('security_steps')
    if last != 'PANEL' and steps is None:
        # Before per-step checkpoints SECURITY was recorded complete only after all its steps.
        steps = list(SECURITY_STEPS)
    steps = [] if steps is None else steps
    if not isinstance(steps, list) or steps != list(SECURITY_STEPS[:len(steps)]) or \
            (last != 'PANEL' and len(steps) != len(SECURITY_STEPS)):
        raise InstallError('Шаги этапа SECURITY в состоянии повреждены; resume запрещён')
    if 'doctor' in steps and not isinstance(state.get('doctor_cli'), dict):
        raise InstallError('Результат установки Bob Doctor отсутствует; resume запрещён')
    state['security_steps'] = steps


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
    upgraded['warp_mode'] = warp_mode(upgraded)
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
