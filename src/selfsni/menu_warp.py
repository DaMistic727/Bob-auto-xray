"""Strict consumer of the current official server-menu WARP CLI."""
import json
import os
import signal
import subprocess
import tempfile
import time

from .core import InstallError, _log_command, _pulse, redact, require

CLI = '/usr/local/bin/server-menu'
PORT = 40000


def _execute(action, timeout=1200):
    args = [CLI, 'warp', action]
    if action != 'xray-outbound':
        args += ['--port', str(PORT), '--json']
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = None
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stdout,
                                       stderr=stderr, start_new_session=True)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    stderr.seek(0)
                    _log_command('server-menu', 'timeout', stderr.read().decode('utf-8', 'replace'))
                    raise InstallError(f'VPS-Server-Menu warp {action}: тайм-аут')
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    _pulse()
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read().decode('utf-8', 'replace')
            errors = stderr.read().decode('utf-8', 'replace')
            _log_command('server-menu', process.returncode, errors)
            return process.returncode, output, errors
        except OSError as error:
            raise InstallError(f'Не удалось запустить VPS-Server-Menu warp {action}') from error
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def _json_object(output, action):
    try:
        result = json.loads(output)
    except (ValueError, TypeError) as error:
        raise InstallError(f'VPS-Server-Menu warp {action}: некорректный JSON') from error
    require(isinstance(result, dict), f'VPS-Server-Menu warp {action}: ожидается JSON object')
    return result


def _health_fields(data, action):
    require(data.get('status') in ('HEALTHY', 'DEGRADED', 'DOWN') and
            isinstance(data.get('reason'), str) and
            isinstance(data.get('mode'), str) and
            type(data.get('port')) is int,
            f'VPS-Server-Menu warp {action}: неполный health JSON')
    return data


def _require_healthy(data, code, action):
    expected = {'HEALTHY': 0, 'DEGRADED': 2, 'DOWN': 3}[data['status']]
    if code == 64:
        raise InstallError(f'VPS-Server-Menu warp {action}: ошибка аргументов')
    require(code == expected,
            f'VPS-Server-Menu warp {action}: exit code {code} противоречит status')
    require(data['status'] == 'HEALTHY' and data['mode'] == 'proxy' and data['port'] == PORT,
            f'VPS-Server-Menu warp {action}: {data["status"]}, {redact(data["reason"])[:200]}; '
            f'требуется HEALTHY proxy на порту {PORT}')


def health():
    code, output, _ = _execute('health')
    require(code != 64, 'VPS-Server-Menu warp health: ошибка аргументов (exit 64)')
    data = _health_fields(_json_object(output, 'health'), 'health')
    _require_healthy(data, code, 'health')
    from .acceptance import check_warp
    check_warp()
    return data


def install():
    code, output, _ = _execute('install')
    require(code != 64, 'VPS-Server-Menu warp install: ошибка аргументов (exit 64)')
    data = _health_fields(_json_object(output, 'install'), 'install')
    operation = data.get('operation')
    require(isinstance(operation, dict) and type(operation.get('success')) is bool and
            type(operation.get('exitCode')) is int,
            'VPS-Server-Menu warp install: неполный operation JSON')
    require(operation['success'] and operation['exitCode'] == 0 and code != 1,
            f'VPS-Server-Menu warp install: операция завершилась ошибкой '
            f'(exit {code}, operation.exitCode={operation["exitCode"]}, '
            f'{redact(data["reason"])[:200]})')
    _require_healthy(data, code, 'install')
    result = health()
    outbound_code, outbound_output, _ = _execute('xray-outbound')
    require(outbound_code == 0, 'VPS-Server-Menu warp xray-outbound: ошибка')
    outbound = _json_object(outbound_output, 'xray-outbound')
    require(outbound == {'tag': 'warp-outbound', 'protocol': 'socks',
                         'settings': {'servers': [{'address': '127.0.0.1',
                                                   'port': PORT, 'users': []}]}},
            'VPS-Server-Menu warp xray-outbound: несовместимый SOCKS5 outbound')
    return result
