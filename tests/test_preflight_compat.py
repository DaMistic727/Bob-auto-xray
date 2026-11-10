"""Preflight on a host that is already configured: compatible, adaptable or a real conflict."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from selfsni import firewall, install_state, ipv6, provision
from selfsni.core import CommandError, InstallError, configure_diagnostics

from test_resume_preflight import Host
from ufw_model import FakeUfw, accept, limit


class HostCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.host = Host(directory.name)
        self.root = Path(directory.name)

    def ufw(self, **options):
        return FakeUfw(self.root, **options)

    def refused(self, pattern, *args, **kwargs):
        with self.assertRaisesRegex(InstallError, pattern) as refusal:
            self.host.preflight(*args, **kwargs)
        return str(refusal.exception)


class CleanHostTests(HostCase):
    def test_clean_ubuntu_passes_without_notes_or_questions(self):
        for version in ('24.04', '26.04'):
            for installed in (False, True):  # ufw absent, or present and never configured
                with self.subTest(version=version, ufw=installed):
                    self.setUp()
                    if installed:
                        self.ufw()
                    result = self.host.preflight(mode='skip', version=version)
                    self.assertEqual(result['ubuntu'], version)
                    self.assertEqual(result['ssh_ports'], [22])
                    self.assertEqual(result['firewall_preflight']['allow'], [22, 80, 443])
                    self.assertEqual((result['consent_required'], result['compatibility'],
                                      result['foreign_tcp_ports']), ([], [], []))
                    self.assertEqual(provision.compatibility_notes(result), [])

    def test_preflight_writes_nothing(self):
        ufw = self.ufw()
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with patch.object(provision, 'write') as write, patch.object(provision, 'save') as save:
            self.host.preflight(mode='skip')
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()})
        self.assertFalse(write.called or save.called)
        self.assertEqual(ufw.calls, [])


class ListenerTests(HostCase):
    def test_local_services_never_block(self):
        self.host.listen('127.0.0.1:5432', ('postgres', 50))
        self.host.listen('[::1]:6379', ('redis-server', 51))
        self.host.listen('127.0.0.53%lo:53', ('systemd-resolve', 52))
        self.host.listen('127.0.0.1:8125', ('netdata', 53))
        self.host.udp += ['UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=52,fd=14))',
                          'UNCONN 0 0 192.0.2.1%ens3:68 0.0.0.0:* users:(("systemd-network",pid=54,fd=20))',
                          'UNCONN 0 0 [fe80::1]%ens3:546 [::]:* users:(("systemd-network",pid=54,fd=21))']
        result = self.host.preflight(mode='skip')
        # What every Ubuntu cloud image runs is neither a question nor a line of output.
        self.assertEqual((result['consent_required'], result['foreign_tcp_ports'], result['compatibility']),
                         ([], [], []))

    def test_public_service_is_kept_and_default_deny_needs_consent(self):
        self.host.listen('0.0.0.0:8080', ('node', 60))
        self.host.udp.append('UNCONN 0 0 0.0.0.0:51820 0.0.0.0:*')
        result = self.host.preflight(mode='skip')
        self.assertEqual(result['foreign_tcp_ports'], [8080])
        self.assertIn('Сторонние службы не затронуты: TCP/8080 (node', result['compatibility'][0])
        self.assertEqual(len(result['consent_required']), 2)
        self.assertIn('ufw allow 8080/tcp', result['consent_required'][0])
        self.assertIn('ufw allow 51820/udp', result['consent_required'][1])

    def test_public_service_already_allowed_needs_no_consent(self):
        ufw = self.ufw(enabled=True)
        ufw.add(accept(22), accept(8080), accept(51820, 'udp'))
        self.host.listen('0.0.0.0:8080', ('node', 60))
        self.host.udp.append('UNCONN 0 0 0.0.0.0:51820 0.0.0.0:*')
        result = self.host.preflight(mode='skip')
        self.assertEqual(result['consent_required'], [])
        self.assertEqual(result['firewall_preflight']['allow'], [80, 443])

    def test_port_bob_needs_is_a_real_conflict(self):
        for address, name, mode in (('0.0.0.0:443', 'haproxy', 'skip'), ('[::]:80', 'apache2', 'skip'),
                                    ('127.0.0.1:62789', 'other', 'skip'), ('127.0.0.1:11111', 'other', 'skip'),
                                    ('127.0.0.1:40000', 'other', 'install')):
            with self.subTest(address=address):
                self.setUp()
                self.host.listen(address, (name, 70))
                port = address.rsplit(':', 1)[1]
                text = self.refused(f'TCP-порт {port} .*занят: {name} слушает', mode=mode)
                self.assertIn('Остальные службы сервера Bob не трогает', text)

    def test_warp_port_is_free_to_use_without_warp(self):
        self.host.listen('127.0.0.1:40000', ('other', 70))
        self.assertEqual(self.host.preflight(mode='skip')['warp_mode'], 'skip')

    def test_saved_internal_ports_must_still_be_free_on_resume(self):
        self.host.listen('127.0.0.1:21000', ('other', 70))
        self.refused('TCP-порт 21000 .панель 3x-ui', self.host.saved('APT_UPDATE'))

    def test_ipv6_only_service_needs_consent_before_ipv6_is_disabled(self):
        self.host.listen('[2001:db8::5]:8443', ('caddy', 80))
        ufw = self.ufw(enabled=True)
        ufw.add(accept(22), accept(8443))
        ufw.add(accept(8443, family=6), family=6)
        result = self.host.preflight(mode='skip')
        self.assertEqual(len(result['consent_required']), 1)
        self.assertIn('Bob отключит IPv6', result['consent_required'][0])


class ExistingUfwTests(HostCase):
    def test_active_ufw_with_safe_extra_rules(self):
        ufw = self.ufw(enabled=True)
        ufw.add(accept(22), accept(22, 'udp'), *limit(2222), accept(8080),
                '-A ufw-user-input -p tcp --dport 25 -j DROP', '-A ufw-user-input -s 198.51.100.7 -j DROP',
                '-A ufw-user-input -i tailscale0 -j ACCEPT')
        ufw.add(accept(22, family=6), '-A ufw6-user-input -p udp --dport 51820 -j ACCEPT', family=6)
        (self.host.proc / 'net').mkdir(parents=True)
        (self.host.proc / 'net/route').write_text(
            'Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n'
            'ens3 00000000 0102A8C0 0003 0 0 0 00000000 0 0 0\n')
        result = self.host.preflight(mode='skip')
        self.assertEqual(result['firewall_preflight']['allow'], [80, 443])
        self.assertEqual(result['consent_required'], [])
        notes = '\n'.join(provision.compatibility_notes(result))
        for fragment in ('tcp 8080', 'DENY tcp 25', 'tailscale0', 'ufw allow 443/tcp'):
            self.assertIn(fragment, notes)

    def test_leftover_rules_of_an_earlier_attempt_do_not_block(self):
        # A fresh run after `ufw allow 80,443` (or after a wiped Bob state) used to be refused.
        ufw = self.ufw(enabled=True)
        ufw.add(accept(22), accept(80), accept(443))
        self.assertEqual(self.host.preflight(mode='skip')['firewall_preflight']['allow'], [])

    def test_conflicting_rules_are_refused_with_the_rule_and_the_fix(self):
        ufw = self.ufw(enabled=True)
        ufw.add(accept(22), '-A ufw-user-input -p tcp --dport 443 -j DROP',
                '-A ufw-user-input -p tcp -m multiport --dports 30000:65000 -j ACCEPT')
        text = self.refused('Существующая конфигурация UFW несовместима', mode='skip')
        for fragment in ('--dport 443 -j DROP', 'запрещает входящие подключения к TCP/443',
                         'открывает из интернета внутренние порты Bob', '62789',
                         'ufw status numbered', 'ufw prepend deny 62789/tcp'):
            self.assertIn(fragment, text)
        self.assertNotIn('Existing non-SSH UFW rules', text)

    def test_stale_rule_files_without_the_ufw_binary_are_still_assessed(self):
        ufw = self.ufw()
        ufw.add('-A ufw-user-input -j ACCEPT')
        self.refused('открывает из интернета внутренние порты Bob', mode='skip')

    def test_non_standard_ssh_port_and_active_session(self):
        self.host.sshd = 'port 2222\n'
        self.host.listeners = ['LISTEN 0 128 0.0.0.0:2222 0.0.0.0:* users:(("sshd",pid=1,fd=3))']
        result = self.host.preflight(mode='skip', connection='203.0.113.9 50000 192.0.2.1 22022')
        self.assertEqual(result['ssh_ports'], [2222, 22022])
        self.assertEqual(result['firewall_preflight']['allow'], [2222, 22022, 80, 443])
        self.assertEqual(self.host.preflight(mode='skip', ssh_port=2200)['ssh_ports'], [2200, 2222])

    def test_session_that_enabling_ufw_would_lock_out(self):
        ufw = self.ufw()
        ufw.add('-A ufw-user-input -s 203.0.113.0/24 -j DROP')
        self.refused('текущее SSH-подключение с адреса 203.0.113.9', mode='skip',
                     connection='203.0.113.9 50000 192.0.2.1 22')

    def test_new_internal_ports_avoid_ranges_the_operator_opened(self):
        ufw = self.ufw(enabled=True)
        ufw.add(accept(22), '-A ufw-user-input -p tcp -m multiport --dports 21000:21999 -j ACCEPT')
        with patch.object(provision, 'free_port', side_effect=[21000, 21500, 30000, 21001, 30001]):
            result = self.host.preflight(mode='skip')
        self.assertEqual((result['panel_port'], result['sub_port']), (30000, 30001))

    def test_resume_refuses_a_rule_that_opens_the_saved_panel_port(self):
        ufw = self.ufw(enabled=True)
        ufw.add(accept(22), '-A ufw-user-input -p tcp -m multiport --dports 21000:21999 -j ACCEPT')
        self.refused('внутренние порты Bob.*21000', self.host.saved('APT_UPDATE'))


class AdaptableHostTests(HostCase):
    def test_operator_fail2ban_configuration_is_kept(self):
        self.host.jail_local.parent.mkdir(parents=True)
        self.host.jail_local.write_text('[DEFAULT]\nbantime = 1h\n[sshd]\nenabled = true\n')
        for state in (None, self.host.saved('APT_UPDATE'), self.host.saved('WARP')):
            result = self.host.preflight(state, mode='skip')
            self.assertIn('jail.local сохранён', '\n'.join(result['compatibility']))
        self.assertEqual(self.host.jail_local.read_text(), '[DEFAULT]\nbantime = 1h\n[sshd]\nenabled = true\n')

    def test_fail2ban_leftovers_of_another_3x_ui_are_a_conflict(self):
        self.host.jail_local.parent.mkdir(parents=True)
        self.host.jail_local.write_text('[3x-ipl]\nenabled = true\n')
        self.refused('3x-ipl от другой установки 3x-ui', mode='skip')
        self.host.jail_local.unlink()
        self.host.jail_local.symlink_to('/etc/passwd')
        self.refused('не является обычным файлом', mode='skip')

    def test_empty_state_directory_is_not_an_installation(self):
        state = self.root / 'var/lib/selfsni'
        state.mkdir(parents=True)
        with patch.object(provision, 'STATE', state):
            self.assertEqual(self.host.preflight(mode='skip')['status'], 'INSTALLING')
            (state / 'progress.log').write_text('x')
            self.refused('существующая установка или конфигурация: ' + str(state), mode='skip')

    def test_public_ipv4_survives_an_unavailable_service(self):
        first, second, third = provision.PUBLIC_IPV4_SOURCES
        self.host.public_ip = {first: CommandError('curl', 6), second: '<html>busy</html>', third: '8.8.8.8\n'}
        self.assertEqual(self.host.preflight(mode='skip')['ipv4'], '8.8.8.8')
        self.host.public_ip = {url: CommandError('curl', 28) for url in provision.PUBLIC_IPV4_SOURCES}
        self.refused('Не удалось определить публичный IPv4 сервера.*api.ipify.org', mode='skip')

    def test_php_fpm_of_the_distribution_is_chosen_among_several(self):
        sockets = [Path('/run/php/php8.1-fpm.sock'), Path('/run/php/php8.3-fpm.sock')]
        with patch.object(provision, 'run', return_value='php8.3-fpm (>= 8.3)') as run:
            self.assertEqual(provision.php_service(sockets), 'php8.3-fpm')
            self.assertEqual(provision.php_service(sockets[:1]), 'php8.1-fpm')
        self.assertEqual(run.call_count, 1)
        for depends, found in (('php8.4-fpm', sockets), ('', sockets), ('php8.3-fpm', [])):
            with patch.object(provision, 'run', return_value=depends), \
                 self.assertRaisesRegex(InstallError, 'PHP-FPM'):
                provision.php_service(found)


class RealConflictMessageTests(HostCase):
    def test_messages_name_the_cause_and_the_fix(self):
        import selfsni.provision as p
        cases = {'/usr/local/x-ui': 'x-ui uninstall', '/etc/nginx': 'apt purge nginx',
                 '/etc/letsencrypt': 'Certbot', str(p.ipv6.CONFIG): 'предыдущей установки Bob'}
        for path, fragment in cases.items():
            with self.subTest(path=path):
                text = str(p.existing_path(path))
                self.assertIn(path, text)
                self.assertIn(fragment, text)

    def test_existing_installations_are_still_refused(self):
        for path in ('/usr/local/x-ui', '/etc/x-ui', '/etc/letsencrypt', '/var/www/selfsni'):
            with self.subTest(path=path), patch.dict('test_resume_preflight.__dict__',
                                                    {'HOST_PATHS': {'/run/systemd/system', path}}):
                self.refused('существующая установка или конфигурация: ' + path, mode='skip')

    def test_dns_and_ssh_failures_explain_themselves(self):
        self.host.public_ip = '8.8.4.4'
        text = self.refused('DNS A-запись vpn.example.com', mode='skip')
        self.assertIn('8.8.4.4', text)
        self.assertIn('8.8.8.8', text)
        self.host.public_ip = '8.8.8.8'
        self.host.sshd = CommandError('sshd', 255)
        real = self.host.command

        def command(*parts, **kwargs):
            if parts[:2] == ('/usr/sbin/sshd', '-T'):
                raise CommandError('sshd', 255)
            return real(*parts, **kwargs)
        self.host.command = command
        self.refused('sshd -T', mode='skip')


class ConsentTests(unittest.TestCase):
    ITEM = 'Служба TCP/8080 (node, 0.0.0.0:8080) принимает подключения извне и не разрешена в UFW.'

    def approve(self, answer, interactive=True, approved=()):
        state = {'consent_required': [self.ITEM]}
        ask = MagicMock(side_effect=answer if isinstance(answer, Exception) else None, return_value=answer)
        with patch.object(provision, 'confirm', ask):
            provision.approve_changes(state, list(approved), interactive)
        return state, ask

    def test_explicit_yes_is_recorded(self):
        state, ask = self.approve(True)
        self.assertEqual(state['approved_changes'], [self.ITEM])
        self.assertNotIn('consent_required', state)
        self.assertIn(self.ITEM, ask.call_args.args[0])

    def test_no_is_a_refusal(self):
        with self.assertRaisesRegex(InstallError, 'изменения не подтверждены'):
            self.approve(False)

    def test_non_interactive_run_refuses_with_instructions_and_never_asks(self):
        ask = MagicMock()
        with patch.object(provision, 'confirm', ask), self.assertRaises(InstallError) as refusal:
            provision.approve_changes({'consent_required': [self.ITEM]}, [], False)
        ask.assert_not_called()
        self.assertIn(self.ITEM, str(refusal.exception))
        self.assertIn('без --domain', str(refusal.exception))

    def test_missing_terminal_is_a_refusal(self):
        with self.assertRaisesRegex(InstallError, 'Без подтверждения такие изменения не выполняются'):
            self.approve(InstallError('Нет терминала для подтверждения'))

    def test_already_approved_change_is_not_asked_again_on_resume(self):
        state, ask = self.approve(True, approved=[self.ITEM])
        ask.assert_not_called()
        self.assertEqual(state['approved_changes'], [self.ITEM])

    def test_only_yes_is_consent(self):
        from selfsni.ui import confirm
        for answer, expected in (('yes\n', True), ('YES\n', True), ('да\n', True), ('\n', False),
                                 ('y\n', False), ('no\n', False)):
            with self.subTest(answer=answer):
                self.assertIs(confirm('?', io.StringIO(answer), io.StringIO()), expected)
        with self.assertRaises(InstallError):
            confirm('?', io.StringIO(''), io.StringIO())


class MainFlowTests(unittest.TestCase):
    """main(): consent before any state, diagnostics of a refusal, read-only --preflight-only."""

    def run_main(self, checked, argv, euid=0, confirm=None):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.addCleanup(configure_diagnostics, None)  # main() registers credentials globally
        self.state, self.log = root / 'state', root / 'log/install.log'
        output = io.StringIO()
        preflight = MagicMock(side_effect=checked if isinstance(checked, Exception) else None,
                              return_value=checked)
        with patch.object(provision, 'STATE', self.state), \
             patch.object(provision, 'DIAGNOSTIC_LOG', self.log), \
             patch.object(provision, 'INSTALL_LOCK', root / 'install.lock'), \
             patch.object(provision, 'verify_bundle', return_value={}), \
             patch.object(provision, 'check_official_menu'), \
             patch.object(provision, 'preflight', preflight), \
             patch.object(provision, 'confirm', confirm or MagicMock(return_value=False)), \
             patch.object(provision.os, 'geteuid', return_value=euid), \
             patch.object(provision, 'AptRunner', side_effect=AssertionError('installation started')), \
             patch('sys.argv', ['install', *argv]), contextlib.redirect_stdout(output):
            try:
                provision.main()
                code = 0
            except SystemExit as exit_:
                code = exit_.code
            except AssertionError:
                code = 'started'
        return code, output.getvalue()

    CHECKED = {'domain': 'vpn.example.com', 'consent_required': ['Служба TCP/8080 будет отрезана.'],
               'compatibility': ['Сторонние службы не затронуты: TCP/8080 (node, 0.0.0.0:8080).'],
               'firewall_preflight': {'kept': ['ALLOW tcp 9000 — не затрагивает порты Bob'],
                                      'changes': ['включит UFW (сейчас выключен)'], 'enabled': False}}

    def test_non_interactive_refusal_leaves_no_state(self):
        code, output = self.run_main(dict(self.CHECKED), ['--domain', 'vpn.example.com', '--no-warp'])
        self.assertEqual(code, 1)
        self.assertIn('Служба TCP/8080 будет отрезана.', output)
        self.assertIn('Сторонние службы не затронуты', output)
        self.assertFalse(self.state.exists())

    def test_declined_change_leaves_no_state(self):
        ask = MagicMock(return_value=False)
        with patch.object(provision, 'interactive_wizard',
                          side_effect=lambda args, version: setattr(args, 'domain', 'vpn.example.com')):
            code, output = self.run_main(dict(self.CHECKED), ['--no-warp'], confirm=ask)
        ask.assert_called_once()
        self.assertEqual(code, 1)
        self.assertFalse(self.state.exists())

    def test_confirmed_change_starts_the_installation_and_is_saved(self):
        ask = MagicMock(return_value=True)
        checked = dict(self.CHECKED, username='u' + '1' * 16, password='c' * 48,
                       panel_path='/' + 'a' * 48 + '/', sub_path='/' + 'b' * 48 + '/')
        with patch.object(provision, 'interactive_wizard',
                          side_effect=lambda args, version: setattr(args, 'domain', 'vpn.example.com')), \
             patch.object(provision, 'CONTROLLED_PATHS', []):
            code, _ = self.run_main(checked, ['--no-warp'], confirm=ask)
        self.assertEqual(code, 'started')
        saved = json.loads((self.state / 'state.json').read_text())
        self.assertEqual(saved['approved_changes'], self.CHECKED['consent_required'])
        self.assertNotIn('consent_required', saved)

    def test_refusal_before_state_leaves_a_root_only_log_without_secrets(self):
        secret = 'd' * 48
        provision.TRACE[:] = ['listener: TCP/8080 (node, 0.0.0.0:8080)',
                              'UFW rule IPv4 [deny]: -A ufw-user-input -p tcp --dport 443 -j DROP',
                              'password=' + secret]
        error = InstallError('Существующая конфигурация UFW несовместима с установкой.')
        code, output = self.run_main(error, ['--domain', 'vpn.example.com', '--no-warp'])
        log = self.log.parent / provision.PREFLIGHT_LOG_NAME
        self.assertEqual(code, 1)
        self.assertIn(f'Журнал: {log}', output)
        text = log.read_text()
        self.assertIn('Существующая конфигурация UFW несовместима', text)
        self.assertIn('--dport 443 -j DROP', text)
        self.assertNotIn(secret, text)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.log.exists())

    def test_log_is_not_written_through_a_symlink_or_without_root(self):
        error = InstallError('refused')
        code, output = self.run_main(error, ['--domain', 'vpn.example.com', '--no-warp'], euid=1000)
        self.assertIn('Журнал: не создан (preflight)', output)
        self.assertFalse(self.log.parent.exists())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'victim'
            log = Path(directory) / 'log' / provision.PREFLIGHT_LOG_NAME
            log.parent.mkdir()
            log.symlink_to(target)
            with patch.object(provision, 'DIAGNOSTIC_LOG', log.parent / 'install.log'), \
                 patch.object(provision.os, 'geteuid', return_value=0):
                self.assertIsNone(provision.preflight_log('refused'))
            self.assertFalse(target.exists())

    def test_preflight_only_reports_and_writes_nothing(self):
        code, output = self.run_main(dict(self.CHECKED), ['--domain', 'vpn.example.com', '--preflight-only'])
        self.assertEqual(code, 0)
        for fragment in ('Сторонние службы не затронуты', 'правило сохранено: ALLOW tcp 9000',
                         'Bob включит UFW', 'Потребуется подтверждение при установке: Служба TCP/8080',
                         'Система не изменена'):
            self.assertIn(fragment, output)
        self.assertFalse(self.state.exists() or self.log.parent.exists())
        code, output = self.run_main(InstallError('refused'),
                                     ['--domain', 'vpn.example.com', '--preflight-only'])
        self.assertEqual(code, 1)
        self.assertFalse(self.log.parent.exists())
        self.assertIn('--preflight-only ничего не записывает', output)


class FirewallResumeTests(HostCase):
    def test_firewall_stage_is_resumable(self):
        self.assertIn('FIREWALL', install_state.RESUMABLE_FAILURE_STAGES)
        saved = self.host.saved('FIREWALL')
        self.host.enable('default')
        ufw = self.ufw()
        ufw.add(accept(22))  # the interrupted stage had already added one rule
        result = self.host.preflight(saved)
        self.assertEqual((result['stage'], result['last_completed_stage']), ('FIREWALL', 'BBR'))
        self.assertEqual(result['firewall_preflight']['allow'], [80, 443])
        for key in ('ipv6', 'bbr', 'swap'):
            self.assertEqual(result[key], saved[key])
        self.assertEqual(result['installation_id'], saved['installation_id'])

    def test_firewall_resume_requires_the_completed_system_stage(self):
        saved = self.host.saved('FIREWALL', last_completed_stage='APT_INSTALL')
        self.refused('resume', saved)
        saved = self.host.saved('FIREWALL')
        saved.pop('bbr')
        self.refused('resume', saved)

    def test_stages_that_cannot_be_repeated_stay_refused(self):
        state = self.host.saved('APT_UPDATE')
        for stage in ('BBR', 'NGINX_PORTAL', 'PANEL', 'ROUTING'):
            with self.subTest(stage=stage), self.assertRaisesRegex(InstallError, 'не поддерживается'):
                install_state.validate_resume(dict(state, stage=stage), 'vpn.example.com', 'install')
        resumed = install_state.validate_resume(dict(state, stage='FIREWALL', last_completed_stage='BBR'),
                                                'vpn.example.com', 'install')
        self.assertEqual(resumed['resume_count'], 1)

    def test_approved_changes_survive_a_resume(self):
        saved = self.host.saved('APT_UPDATE', approved_changes=['x'])
        self.assertEqual(self.host.preflight(saved)['approved_changes'], ['x'])


class Ipv6ExposureTests(unittest.TestCase):
    def exposure(self, ports, listeners):
        with tempfile.TemporaryDirectory() as directory:
            defaults = Path(directory) / 'ufw'
            defaults.write_text('IPV6=yes\n')
            return ipv6.check_exposure(ports, lambda *command: listeners, defaults)

    def test_service_that_predates_bob_is_tolerated_and_internal_ports_are_not(self):
        listeners = 'LISTEN 0 128 [::]:22 [::]:*\nLISTEN 0 511 *:8080 *:*\n'
        state = {'ssh_ports': [22], 'foreign_tcp_ports': [8080]}
        self.assertEqual(self.exposure(firewall.published_ports(state), listeners)['publicPorts'],
                         [22, 80, 443, 8080])
        with self.assertRaisesRegex(InstallError, '8080'):
            self.exposure(firewall.published_ports({'ssh_ports': [22]}), listeners)
        with self.assertRaisesRegex(InstallError, '62789'):
            self.exposure(firewall.published_ports(state), listeners + 'LISTEN 0 9 [::]:62789 [::]:*\n')


if __name__ == '__main__':
    unittest.main()
