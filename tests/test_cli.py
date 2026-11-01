"""`bob` CLI: menu, permissions, errors, secret redaction, non-TTY and Doctor compatibility."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from selfsni import cli, doctor

from test_doctor import FakeProbe

ADMIN_UUID = str(uuid.uuid4())
SECRETS = ('SECRET_PASSWORD', 'usecretlogin', 'a' * 48, 'c' * 48, ADMIN_UUID, 'SECRET_TOKEN',
           'SECRET_PRIVATE', 'secret-id')


class TTY(io.StringIO):
    encoding = 'utf-8'

    def isatty(self):
        return True


class Pipe(io.StringIO):
    def isatty(self):
        return False


class NoRead(Pipe):
    def readline(self, *args):
        raise AssertionError('non-interactive bob must never wait for input')


def node_state():
    state = dict(FakeProbe().state)
    state.update(username='usecretlogin', admin_client={'id': ADMIN_UUID, 'subId': 'c' * 48})
    return state


def runtime_config(admin=True):
    clients = [{'id': ADMIN_UUID, 'email': 'admin', 'flow': 'xtls-rprx-vision'}] if admin else []
    return {'inbounds': [{'port': 443, 'protocol': 'vless', 'settings': {'clients': clients}}]}


class Node(unittest.TestCase):
    """A fake filesystem root where the current user plays root."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.state_dir = self.root / 'var/lib/selfsni'
        self.state_dir.mkdir(parents=True)
        self.state_dir.chmod(0o700)
        (self.root / 'usr/local/x-ui/bin').mkdir(parents=True)
        for name, value in (('TRUSTED_ROOT', self.root), ('ROOT_UID', os.getuid()),
                            ('STATE', self.state_dir / 'state.json'),
                            ('ACCEPTANCE', self.state_dir / 'acceptance.json'),
                            ('CONFIG', self.root / 'usr/local/x-ui/bin/config.json')):
            patcher = patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.probe = FakeProbe('install')
        patcher = patch.object(doctor, 'Probe', lambda: self.probe)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.put(cli.STATE, node_state())
        self.put(cli.CONFIG, runtime_config())

    def put(self, path, value, mode=0o600):
        path = Path(path)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        path.chmod(mode)

    def run_cli(self, argv, stdin=None, tty=False, env=None):
        stdout, stderr = (TTY() if tty else Pipe()), io.StringIO()
        stdin = stdin if stdin is not None else (TTY() if tty else NoRead())
        with patch('sys.stderr', stderr):
            code = cli.main(argv, stdin=stdin, stdout=stdout,
                            env={'TERM': 'xterm-256color'} if env is None else env)
        return code, stdout.getvalue(), stderr.getvalue()

    def assertNoSecrets(self, *texts):
        for text in texts:
            for secret in SECRETS:
                self.assertNotIn(secret, text)


class NonInteractiveTests(Node):
    def test_bare_bob_without_tty_prints_help_and_never_reads(self):
        code, out, err = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn('bob doctor [--deep] [--json]', out)
        self.assertIn('bob panel', out)
        self.assertNotIn('\033[', out)
        self.assertEqual(err, '')

    def test_help_and_version(self):
        for argv in (['--help'], ['-h'], ['help']):
            code, out, _ = self.run_cli(argv)
            self.assertEqual(code, 0)
            self.assertIn('Коды выхода', out)
        code, out, _ = self.run_cli(['--version'])
        self.assertEqual((code, out.strip()), (0, cli.version()))
        self.assertRegex(cli.version(), r'^[0-9]+\.[0-9]+\.[0-9]+$')

    def test_unknown_command_and_options_are_usage_errors(self):
        for argv in (['bogus'], ['panel', '--force'], ['status', 'x'], ['--help', 'x'], ['--deep', 'x']):
            code, out, err = self.run_cli(argv)
            self.assertEqual(code, cli.USAGE, argv)
            self.assertIn('bob --help', err)

    def test_secrets_require_flag_without_terminal(self):
        for command in ('panel', 'admin'):
            code, out, err = self.run_cli([command])
            self.assertEqual(code, cli.CANCELLED)
            self.assertIn('--show-secrets', err)
            self.assertNoSecrets(out, err)
            self.assertNotIn('https://', out + err)

    def test_explicit_flag_reveals_panel_access_with_password_caveat(self):
        code, out, err = self.run_cli(['panel', '--show-secrets'])
        self.assertEqual(code, 0, err)
        self.assertIn('https://vpn.example.com/' + 'a' * 48 + '/', out)
        self.assertIn('usecretlogin', out)
        self.assertIn('SECRET_PASSWORD', out)
        self.assertIn('исходный', out)
        self.assertIn('не сбрасывает', out)
        # Other state secrets are never part of the output.
        for secret in ('SECRET_TOKEN', 'SECRET_PRIVATE', ADMIN_UUID, 'c' * 48):
            self.assertNotIn(secret, out + err)

    def test_explicit_flag_reveals_only_admin_subscription(self):
        code, out, err = self.run_cli(['admin', '--show-secrets'])
        self.assertEqual(code, 0, err)
        self.assertIn('https://vpn.example.com/' + 'b' * 48 + '/' + 'c' * 48, out)
        self.assertIn('подтверждён в runtime', out)
        for secret in ('SECRET_PASSWORD', 'usecretlogin', ADMIN_UUID, 'a' * 48, 'SECRET_TOKEN'):
            self.assertNotIn(secret, out + err)

    def test_legacy_subscription_port_is_kept_for_old_nodes(self):
        state = node_state()
        state.pop('subscription_access')
        self.put(cli.STATE, state)
        code, out, _ = self.run_cli(['admin', '--show-secrets'])
        self.assertEqual(code, 0)
        self.assertIn('https://vpn.example.com:22000/', out)

    def test_admin_missing_from_runtime_is_reported(self):
        self.put(cli.CONFIG, runtime_config(admin=False))
        code, out, _ = self.run_cli(['admin', '--show-secrets'])
        self.assertEqual(code, 0)
        self.assertIn('не найден в runtime', out)
        cli.CONFIG.unlink()
        code, out, _ = self.run_cli(['admin', '--show-secrets'])
        self.assertEqual(code, 0)
        self.assertIn('не подтверждено', out)

    def test_invalid_or_missing_admin_identity_is_never_printed(self):
        for client, message in ((None, 'до v0.3.6'), ({'id': 'not-a-uuid', 'subId': 'c' * 48}, 'повреждены'),
                                ({'id': ADMIN_UUID, 'subId': 'short'}, 'повреждены'),
                                ('garbage', 'повреждены')):
            state = node_state()
            if client is None:
                state.pop('admin_client')
            else:
                state['admin_client'] = client
            self.put(cli.STATE, state)
            code, out, err = self.run_cli(['admin', '--show-secrets'])
            self.assertEqual(code, cli.UNAVAILABLE, client)
            self.assertIn(message, err)
            self.assertNoSecrets(out, err)
            self.assertEqual(out, '')

    def test_corrupt_credentials_are_refused(self):
        for username, password in (('', 'x'), ('user name', 'x'), ('u', 'pass\x1b[2J'), (None, 'x')):
            state = node_state()
            state.update(username=username, password=password)
            self.put(cli.STATE, state)
            code, out, err = self.run_cli(['panel', '--show-secrets'])
            self.assertEqual(code, cli.UNAVAILABLE)
            self.assertEqual(out, '')
            self.assertNotIn('\x1b', err)

    def test_status_uses_doctor_checks_read_only(self):
        code, out, err = self.run_cli(['status'])
        self.assertEqual(code, 0, err)
        for label in ('x-ui', 'nginx', 'WARP', 'Сертификат', 'Bob CLI'):
            self.assertIn(label, out)
        self.assertIn('Осталось дней: 60', out)
        self.assertNoSecrets(out, err)
        commands = {x[1] for x in self.probe.calls if x[0] == 'command'}
        allowed = {('systemctl', verb, unit) for verb in ('is-active', 'is-enabled')
                   for unit in ('x-ui', 'nginx', 'warp-svc')}
        self.assertTrue(commands <= allowed, commands)
        self.assertNotIn('read', {x[0] for x in self.probe.calls})

    def test_status_without_warp_mode_skips_warp(self):
        state = node_state()
        state.update(warp_mode='skip', adopt_warp=False)
        self.put(cli.STATE, state)
        code, out, _ = self.run_cli(['status'])
        self.assertEqual(code, 0)
        self.assertIn('WARP намеренно отключён', out)
        self.assertNotIn(('command', ('systemctl', 'is-active', 'warp-svc'), 5), self.probe.calls)

    def test_status_reports_failed_service_and_expiring_certificate(self):
        self.probe.commands[('systemctl', 'is-active', 'nginx')] = (3, 'failed\n')
        self.probe.expiry = self.probe.now_value
        code, out, _ = self.run_cli(['status'])
        self.assertEqual(code, 0)
        self.assertIn('FAIL', out)
        self.assertIn('скоро истекает', out)

    def test_report_summarises_acceptance_without_reasons(self):
        self.put(cli.ACCEPTANCE, {'checks': [
            {'name': 'nginx configuration', 'passed': True},
            {'name': 'Xray configuration', 'passed': False, 'reason': 'SECRET_TOKEN at /' + 'a' * 48},
            {'name': 'bad\x1b[31mname', 'passed': False}]})
        code, out, err = self.run_cli(['report'])
        self.assertEqual(code, 0, err)
        self.assertIn('Пройдено проверок: 1 из 3', out)
        self.assertIn('Xray configuration', out)
        self.assertNoSecrets(out, err)
        self.assertNotIn('\x1b', out)

    def test_report_rejects_malformed_or_missing_file(self):
        code, _, err = self.run_cli(['report'])
        self.assertEqual(code, cli.UNAVAILABLE)
        self.assertIn('не найден', err)
        for value in ('[]', '{"checks": []}', '{"checks": [{"passed": "yes"}]}', '{bad'):
            self.put(cli.ACCEPTANCE, value)
            code, out, err = self.run_cli(['report'])
            self.assertEqual(code, cli.UNAVAILABLE, value)
            self.assertEqual(out, '')


XUI_ARCHIVE_UID = 1001  # owner of x-ui/ and x-ui/bin/ in the pinned v3.11.0 release archive


def official_runtime(clients):
    """Real Xray runtime written by 3x-ui (repository snapshot) with the given clients."""
    snapshot = json.loads((Path(__file__).resolve().parents[1] /
                           'docs/reference-config-redacted.json').read_text())['runtime']
    for row in snapshot['inbounds']:
        if row.get('port') == 443:
            row['settings']['clients'] = clients
    return snapshot


ADMIN_RUNTIME = {'email': 'admin', 'flow': 'xtls-rprx-vision', 'id': ADMIN_UUID}


class RuntimeAdminTests(Node):
    """`bob admin` runtime confirmation on a node laid out by the official 3x-ui installer."""

    def setUp(self):
        super().setUp()
        self.xui = {self.root / 'usr/local/x-ui', self.root / 'usr/local/x-ui/bin'}
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            # `tar zxvf` as root keeps the archive owner, so these are uid 1001 on a real node.
            info = real_lstat(path, *args, **kwargs)
            if Path(path) not in self.xui:
                return info
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                                   XUI_ARCHIVE_UID, XUI_ARCHIVE_UID, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        patcher = patch.object(cli.os, 'lstat', side_effect=lstat)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.put(cli.CONFIG, official_runtime([dict(ADMIN_RUNTIME)]))

    def admin(self):
        code, out, err = self.run_cli(['admin', '--show-secrets'])
        self.assertEqual(code, 0, err)
        # Only the subscription URL is a secret here; diagnostics never echo identities.
        self.assertNotIn(ADMIN_UUID, out + err)
        self.assertNotIn('a' * 48, out + err)
        return out

    def test_admin_is_confirmed_on_archive_owned_install_tree(self):
        out = self.admin()
        self.assertIn('Клиент admin подтверждён в runtime Xray', out)
        self.assertNotIn('не подтверждено', out)

    def test_state_still_requires_root_owned_directories(self):
        self.xui = {self.state_dir}
        code, out, err = self.run_cli(['admin', '--show-secrets'])
        self.assertEqual(code, cli.UNAVAILABLE)
        self.assertEqual(out, '')
        self.assertIn('небезопасный', err)

    def test_missing_or_changed_admin_is_a_warning(self):
        for clients in ([], [{'email': 'other', 'flow': 'xtls-rprx-vision', 'id': str(uuid.uuid4())}],
                        [dict(ADMIN_RUNTIME, flow='')], [dict(ADMIN_RUNTIME, email='renamed')],
                        [dict(ADMIN_RUNTIME, id=str(uuid.uuid4()))]):
            with self.subTest(clients=clients):
                self.put(cli.CONFIG, official_runtime(clients))
                out = self.admin()
                self.assertIn('! Клиент admin не найден в runtime Xray или отличается от state', out)

    def assertUnknown(self, reason):
        out = self.admin()
        self.assertIn('? Наличие клиента admin в runtime Xray не подтверждено', out)
        self.assertIn(reason, out)
        self.assertNotIn('подтверждён в runtime', out)

    def test_unavailable_runtime_is_unknown_with_reason(self):
        cli.CONFIG.unlink()
        self.assertUnknown('файл не найден')

    def test_corrupt_runtime_is_unknown(self):
        self.put(cli.CONFIG, '{"inbounds": [')
        self.assertUnknown('повреждён')

    def test_unrecognised_runtime_structure_is_unknown(self):
        for config in ({'inbounds': []}, {'inbounds': 'x'}, {'inbounds': ['x']},
                       {'inbounds': [{'port': 443, 'protocol': 'vless', 'settings': 'x'}]},
                       {'inbounds': [{'port': 443, 'protocol': 'vless', 'settings': {}}]}):
            with self.subTest(config=config):
                self.put(cli.CONFIG, config)
                self.assertUnknown('основной inbound VLESS на TCP/443 не распознан')

    def test_insecure_runtime_file_is_unknown(self):
        cli.CONFIG.chmod(0o644)
        self.assertUnknown('небезопасный')
        cli.CONFIG.chmod(0o600)
        os.link(cli.CONFIG, self.root / 'second-link')
        self.assertUnknown('небезопасный')

    def test_runtime_read_permission_error_is_unknown(self):
        real_open = os.open

        def guarded(path, *args, **kwargs):
            if Path(path) == cli.CONFIG:
                raise PermissionError(13, 'denied')
            return real_open(path, *args, **kwargs)
        with patch.object(cli.os, 'open', side_effect=guarded):
            self.assertUnknown('нет доступа')

    def test_symlinks_and_writable_directories_are_unknown(self):
        real = self.root / 'elsewhere.json'
        self.put(real, official_runtime([dict(ADMIN_RUNTIME)]))
        cli.CONFIG.unlink()
        cli.CONFIG.symlink_to(real)
        self.assertUnknown('небезопасный')
        cli.CONFIG.unlink()
        bin_dir = self.root / 'usr/local/x-ui/bin'
        moved = self.root / 'usr/local/x-ui/real-bin'
        bin_dir.rename(moved)
        bin_dir.symlink_to(moved)
        self.assertUnknown('небезопасный')
        bin_dir.unlink()
        moved.rename(bin_dir)
        self.put(cli.CONFIG, official_runtime([dict(ADMIN_RUNTIME)]))
        bin_dir.chmod(0o777)
        self.addCleanup(bin_dir.chmod, 0o755)
        self.assertUnknown('небезопасный')


class PermissionTests(Node):
    def refused(self, argv=('panel', '--show-secrets')):
        code, out, err = self.run_cli(list(argv))
        self.assertEqual(out, '')
        self.assertNoSecrets(out, err)
        return code, err

    def test_requires_root(self):
        with patch.object(cli, 'ROOT_UID', os.getuid() + 1):
            for argv in (['panel', '--show-secrets'], ['admin', '--show-secrets'], ['status'],
                         ['report']):
                code, err = self.refused(argv)
                self.assertEqual(code, cli.NEED_ROOT)
                self.assertIn('sudo', err)
            code, out, err = self.run_cli([], stdin=TTY(), tty=True)
            self.assertEqual(code, cli.NEED_ROOT)

    def test_state_symlink_is_refused(self):
        real = self.root / 'elsewhere.json'
        self.put(real, node_state())
        cli.STATE.unlink()
        cli.STATE.symlink_to(real)
        code, err = self.refused()
        self.assertEqual(code, cli.UNAVAILABLE)
        self.assertIn('небезопасный', err)

    def test_symlinked_parent_directory_is_refused(self):
        moved = self.root / 'var/lib/real-selfsni'
        self.state_dir.rename(moved)
        self.state_dir.symlink_to(moved)
        code, err = self.refused()
        self.assertEqual(code, cli.UNAVAILABLE)
        self.assertIn('небезопасный', err)

    def test_world_readable_or_group_writable_paths_are_refused(self):
        cli.STATE.chmod(0o644)
        self.assertEqual(self.refused()[0], cli.UNAVAILABLE)
        cli.STATE.chmod(0o600)
        (self.root / 'var/lib').chmod(0o777)
        self.addCleanup((self.root / 'var/lib').chmod, 0o755)
        self.assertEqual(self.refused()[0], cli.UNAVAILABLE)

    def test_hard_link_fifo_and_directory_are_refused(self):
        os.link(cli.STATE, self.root / 'second-link')
        self.assertEqual(self.refused()[0], cli.UNAVAILABLE)
        (self.root / 'second-link').unlink()
        cli.STATE.unlink()
        os.mkfifo(cli.STATE, 0o600)
        self.assertEqual(self.refused()[0], cli.UNAVAILABLE)  # must not block on open()
        cli.STATE.unlink()
        cli.STATE.mkdir(mode=0o700)
        self.assertEqual(self.refused()[0], cli.UNAVAILABLE)

    def test_oversized_corrupt_or_unfinished_state_is_refused(self):
        for value, message in (('x' * (cli.MAX_BYTES + 1), 'небезопасный'), ('{bad', 'повреждён'),
                               ('\xff', 'повреждён')):
            if value == '\xff':
                cli.STATE.write_bytes(b'\xff')
            else:
                self.put(cli.STATE, value)
            code, err = self.refused()
            self.assertEqual(code, cli.UNAVAILABLE)
            self.assertIn(message, err)
        state = node_state()
        state.update(status='INSTALLING', stage='PANEL')
        self.put(cli.STATE, state)
        code, err = self.refused()
        self.assertEqual(code, cli.UNAVAILABLE)
        self.assertIn('не завершена', err)

    def test_unowned_state_schema_is_refused(self):
        state = node_state()
        state['schema'] = 1
        self.put(cli.STATE, state)
        code, err = self.refused()
        self.assertEqual(code, cli.UNAVAILABLE)
        self.assertIn('принадлежность', err)

    def test_missing_state(self):
        cli.STATE.unlink()
        code, err = self.refused()
        self.assertEqual(code, cli.UNAVAILABLE)
        self.assertIn('не найден', err)


class MenuTests(Node):
    def menu(self, keys, env=None):
        stdin = TTY(''.join(key + '\n' for key in keys))
        return self.run_cli([], stdin=stdin, tty=True, env=env)

    def test_exit_and_eof(self):
        code, out, _ = self.menu(['0'])
        self.assertEqual(code, 0)
        for item in ('Доступ к панели', 'Подписка admin', 'Bob Doctor', 'Глубокая диагностика',
                     'Состояние сервера', 'Отчёт установки', 'Выход'):
            self.assertIn(item, out)
        code, out, _ = self.menu([])
        self.assertEqual(code, 0)

    def test_invalid_choice_redraws_menu(self):
        code, out, _ = self.menu(['9', 'abc', '0'])
        self.assertEqual(code, 0)
        self.assertEqual(out.count('Нет такого пункта'), 2)

    def test_panel_requires_confirmation(self):
        for answer in ('', 'n', 'нет', 'yes please'):
            code, out, _ = self.menu(['1', answer, '', '0'])
            self.assertEqual(code, 0)
            self.assertIn('данные не показаны', out)
            self.assertNoSecrets(out)
        code, out, _ = self.menu(['1', 'да', '', '0'])
        self.assertIn('SECRET_PASSWORD', out)
        self.assertIn('usecretlogin', out)
        self.assertIn('скрыть и вернуться', out)
        # Revealed secrets are wiped from the screen and scrollback on return.
        self.assertIn('\033[3J', out.split('SECRET_PASSWORD', 1)[1])

    def test_admin_requires_confirmation(self):
        code, out, _ = self.menu(['2', 'n', '', '0'])
        self.assertNoSecrets(out)
        code, out, _ = self.menu(['2', 'y', '', '0'])
        self.assertIn('c' * 48, out)

    def test_menu_runs_existing_doctor(self):
        with patch.object(cli.doctor, 'main', return_value=1) as main:
            code, out, _ = self.menu(['3', '', '4', '', '0'])
        self.assertEqual(code, 0)
        self.assertEqual([call.args[0] for call in main.call_args_list], [[], ['--deep']])

    def test_status_and_report_items(self):
        self.put(cli.ACCEPTANCE, {'checks': [{'name': 'nginx configuration', 'passed': True}]})
        code, out, _ = self.menu(['5', '', '6', '', '0'])
        self.assertEqual(code, 0)
        self.assertIn('Состояние сервера', out)
        self.assertIn('Пройдено проверок: 1 из 1', out)
        self.assertNoSecrets(out)

    def test_errors_return_to_menu(self):
        cli.STATE.unlink()
        code, out, _ = self.menu(['1', '', '0'])
        self.assertEqual(code, 0)
        self.assertIn('State установки: файл не найден', out)
        self.assertGreaterEqual(out.count('Выход'), 2)

    def test_ctrl_c_exits_with_130(self):
        class Interrupt(TTY):
            def readline(self, *args):
                raise KeyboardInterrupt
        code, _, _ = self.run_cli([], stdin=Interrupt(), tty=True)
        self.assertEqual(code, cli.INTERRUPTED)

    def test_ctrl_c_after_reveal_still_wipes_secrets(self):
        class InterruptAtReturn(TTY):
            answers = iter(['1\n', 'y\n'])

            def readline(self, *args):
                try:
                    return next(self.answers)
                except StopIteration:
                    raise KeyboardInterrupt from None
        code, out, _ = self.run_cli([], stdin=InterruptAtReturn(), tty=True)
        self.assertEqual(code, cli.INTERRUPTED)
        self.assertIn('\033[3J', out.split('SECRET_PASSWORD', 1)[1])

    def test_plain_terminal_has_no_ansi(self):
        for env in ({'TERM': 'dumb'}, {}):
            code, out, _ = self.menu(['1', 'y', '', '5', '', '0'], env=env)
            self.assertEqual(code, 0)
            self.assertNotIn('\033', out)
            self.assertNotIn('╭', out)
            self.assertIn('BOB AUTO XRAY', out)

    def test_no_color_keeps_layout_without_colors(self):
        code, out, _ = self.menu(['0'], env={'TERM': 'xterm', 'NO_COLOR': '1'})
        self.assertEqual(code, 0)
        self.assertIn('╭', out)
        self.assertNotIn('\033[3', out)

    def test_menu_needs_both_ends_on_a_terminal(self):
        stdout = Pipe()
        with patch('sys.stderr', io.StringIO()):
            code = cli.main([], stdin=TTY('1\n'), stdout=stdout, env={'TERM': 'xterm'})
        self.assertEqual(code, 0)
        self.assertIn('Использование', stdout.getvalue())


class InstallerHintTests(unittest.TestCase):
    def test_success_mentions_bob_only_when_installed(self):
        from selfsni.ui import InstallerUI
        state = dict(node_state(), doctor_cli={'status': 'installed', 'command': '/usr/local/bin/bob'})
        for cli_state, expected in ((state['doctor_cli'], True),
                                    ({'status': 'command-name-taken', 'command': None}, False)):
            out = Pipe()
            InstallerUI(out, tty=False).success(dict(state, doctor_cli=cli_state))
            self.assertEqual('Меню управления сервером (только чтение): bob' in out.getvalue(), expected)
            self.assertNotIn('SECRET_PASSWORD', out.getvalue())


class DoctorCompatibilityTests(unittest.TestCase):
    def report(self, overall):
        return {'schemaVersion': 1, 'scope': 'on-host; external ISP reachability not measured',
                'overallStatus': overall, 'summary': {'PASS': 1, 'WARN': 0, 'FAIL': 0, 'SKIP': 0,
                                                      'UNKNOWN': 0},
                'checks': [{'id': 'core.state', 'component': 'state', 'status': 'PASS',
                            'summary': 'ok', 'details': {}, 'suggestion': None}],
                'timestamp': '2026-11-01T00:00:00Z'}

    def capture(self, function, *args):
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            code = function(*args)
        return code, stdout.getvalue()

    def test_doctor_output_and_exit_codes_are_unchanged(self):
        for overall, expected in (('HEALTHY', 0), ('DEGRADED', 1), ('UNHEALTHY', 2), ('UNKNOWN', 3)):
            for args in ([], ['--json'], ['--deep'], ['--deep', '--json']):
                with self.subTest(overall=overall, args=args), \
                     patch.object(doctor.Doctor, 'run', return_value=self.report(overall)):
                    direct = self.capture(doctor.main, list(args))
                    via_bob = self.capture(cli.main, ['doctor'] + list(args))
                    self.assertEqual(via_bob, direct)
                    self.assertEqual(via_bob[0], expected)
                    if '--json' in args:
                        self.assertEqual(json.loads(via_bob[1]), self.report(overall))
                        self.assertEqual(len(via_bob[1].splitlines()), 1)

    def test_deep_flag_reaches_doctor(self):
        with patch.object(doctor, 'Doctor') as checker:
            checker.return_value.run.return_value = self.report('HEALTHY')
            self.capture(cli.main, ['doctor', '--deep', '--json'])
        checker.assert_called_once_with(deep=True)

    def test_v040_style_arguments_still_run_doctor(self):
        with patch.object(doctor.Doctor, 'run', return_value=self.report('DEGRADED')):
            for args in (['--json'], ['--deep'], ['--json', '--deep']):
                self.assertEqual(self.capture(cli.main, args), self.capture(doctor.main, args))

    def test_doctor_argument_errors_keep_unknown_json(self):
        code, out = self.capture(cli.main, ['doctor', '--json', '--bogus'])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)['overallStatus'], 'UNKNOWN')

    def test_doctor_help_exits_zero(self):
        with self.assertRaises(SystemExit) as raised, patch('sys.stdout', io.StringIO()):
            cli.main(['doctor', '--help'])
        self.assertEqual(raised.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
