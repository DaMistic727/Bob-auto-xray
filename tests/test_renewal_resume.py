"""Certbot renewal dry-run retry and resume after FAILED/SECURITY (mocked network only).

These tests prove the control flow and classification. They do not prove that a
real ACME staging renewal works on a VPS.
"""
import hashlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from selfsni import provision
from selfsni.certbot_errors import acme_step, classify_renewal, deploy_hook_result
from selfsni.core import CommandError, InstallError
from selfsni.install_state import SECURITY_STEPS, initialize, validate_resume
from selfsni.tls_renewal import LEGACY_HOOK_SCRIPT, retry_dropin

DOMAIN = 'vpn.example.com'
RESET = ("Failed to renew certificate vpn.example.com with error: ('Connection aborted.', "
         "ConnectionResetError(104, 'Connection reset by peer'))\nAll simulated renewals failed.")
HTTP01 = ("Certbot failed to authenticate some domains (authenticator: webroot). The Certificate "
          "Authority reported these problems:\n  Domain: vpn.example.com\n  Type:   unauthorized\n"
          "  Detail: 192.0.2.1: Invalid response from http://vpn.example.com/.well-known/"
          "acme-challenge/abc: 404\nAll simulated renewals failed.")
HOOK = ("Hook 'deploy-hook' ran with error output:\n nginx: the configuration file "
        "/etc/nginx/nginx.conf syntax is ok\n Job for x-ui.service failed: connection reset\n"
        "Hook 'deploy-hook' reported error code 1\n")
HOOK_OK = ("Simulating renewal of an existing certificate for vpn.example.com\n"
           "Hook 'deploy-hook' ran with error output:\n nginx: the configuration file "
           "/etc/nginx/nginx.conf syntax is ok\n nginx: configuration file /etc/nginx/nginx.conf "
           "test is successful\nCongratulations, all simulated renewals succeeded:\n")
LOG = ("2026-11-04 10:00:00,000:DEBUG:acme.client:Sending GET request to "
       "https://acme-staging-v02.api.letsencrypt.org/directory.\n"
       "2026-11-04 10:00:01,000:DEBUG:acme.client:Sending POST request to "
       "https://acme-staging-v02.api.letsencrypt.org/acme/new-order. args: (), kwargs: {}\n")
HOOK_RUN = ('2026-11-04 10:00:09,000:INFO:certbot.compat.misc:Running deploy-hook command: '
            '/etc/letsencrypt/renewal-hooks/deploy/50-selfsni\n')
RENEWAL_CONF = ('[renewalparams]\naccount = 0123\nauthenticator = webroot\n'
                'webroot_path = /var/lib/selfsni-acme,\n'
                'server = https://acme-v02.api.letsencrypt.org/directory\nkey_type = ecdsa\n'
                '[[webroot_map]]\nvpn.example.com = /var/lib/selfsni-acme\n')


def certbot_failure(output, timed_out=False):
    return CommandError('certbot', None if timed_out else 1, output, timed_out=timed_out)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ClassificationTests(unittest.TestCase):
    def test_client_network_failures_and_acme_5xx_are_transient(self):
        for output in (RESET, 'HTTPSConnectionPool(host=...): Max retries exceeded (Caused by '
                       'NewConnectionError: [Errno -3] Temporary failure in name resolution)',
                       'ReadTimeoutError: Read timed out. (read timeout=45)',
                       'urn:ietf:params:acme:error:serverInternal :: The server experienced an internal error',
                       '503 Server Error: Service Unavailable for url: https://acme-staging-v02',
                       "SSLEOFError(8, 'EOF occurred in violation of protocol')"):
            with self.subTest(output=output[:40]):
                self.assertEqual(classify_renewal(output), 'transient')
        self.assertEqual(classify_renewal('', timed_out=True), 'transient')

    def test_configuration_hook_tls_and_rate_limit_are_never_transient(self):
        self.assertEqual(classify_renewal(HTTP01), 'validation')
        self.assertEqual(classify_renewal('Type: connection\nDetail: 192.0.2.1: Fetching http://x/'
                                          '.well-known/acme-challenge/a: Timeout during connect '
                                          '(likely firewall problem)'), 'validation')
        self.assertEqual(classify_renewal(HOOK), 'hook')
        self.assertEqual(classify_renewal('SSLError(SSLCertVerificationError(1, "[SSL: '
                                          'CERTIFICATE_VERIFY_FAILED] certificate verify failed"))'), 'tls')
        self.assertEqual(classify_renewal('urn:ietf:params:acme:error:rateLimited :: too many '
                                          'failed authorizations recently'), 'rate-limit')
        self.assertEqual(classify_renewal('certbot: error: unrecognized arguments: --run-deploy-hooks'),
                         'other')

    def test_acme_step_omits_account_and_order_identifiers(self):
        self.assertEqual(acme_step(LOG), 'POST acme-staging-v02.api.letsencrypt.org new-order')
        self.assertEqual(acme_step('Sending POST request to https://acme-staging-v02.api.letsencrypt.org'
                                   '/acme/finalize/123456/789. args'),
                         'POST acme-staging-v02.api.letsencrypt.org finalize')
        self.assertIsNone(acme_step('no requests'))

    def test_hook_result_needs_exactly_one_start_of_bob_hook(self):
        hook = provision.RENEW_HOOK
        self.assertEqual(deploy_hook_result(HOOK_OK, HOOK_RUN, hook), 'ran')
        self.assertEqual(deploy_hook_result(HOOK, HOOK_RUN, hook), 'failed')
        self.assertEqual(deploy_hook_result('', 'Dry run: skipping deploy hook command: ' + hook, hook),
                         'skipped')
        self.assertEqual(deploy_hook_result(HOOK_OK, '', hook), 'not-run')
        foreign = HOOK_RUN.replace('50-selfsni', '10-foreign')
        self.assertEqual(deploy_hook_result(HOOK_OK, foreign + HOOK_RUN, hook), 'not-run')
        self.assertEqual(deploy_hook_result(HOOK_OK, HOOK_RUN + HOOK_RUN, hook), 'not-run')


class RenewalDryRunTests(unittest.TestCase):
    """renew_hook() with a fake certbot that answers and appends to letsencrypt.log."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.log = self.root / 'var/log/letsencrypt/letsencrypt.log'
        self.log.parent.mkdir(parents=True)
        self.log.write_text('2026-11-03 earlier run: Running deploy-hook command: '
                            '/etc/letsencrypt/renewal-hooks/deploy/50-selfsni\n')
        self.put(f'/etc/letsencrypt/renewal/{DOMAIN}.conf', RENEWAL_CONF)
        self.writes = []
        self.probes = []
        self.stamp = self.root / 'var/lib/selfsni/tls-reloaded'
        self.stamp.parent.mkdir(parents=True)

    def put(self, path, content):
        target = self.root / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def system(self, *parts, **kwargs):
        # systemctl after a proven dry run: daemon-reload and the drop-in readback.
        self.systemd.append(parts)
        if parts[:2] == ('systemctl', 'show'):
            return provision.RETRY_DROPIN + '\n'
        return ''

    def patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        for target, value in (('write', MagicMock(side_effect=lambda *a: self.writes.append(a))),
                              ('SYSTEM_ROOT', self.root), ('CERTBOT_LOG', self.log),
                              ('RELOAD_STAMP', str(self.stamp)), ('UI', None)):
            stack.enter_context(patch.object(provision, target, value))
        return stack

    def renew(self, outcomes, clock=None, jitter=lambda a, b: 0):
        """outcomes: (console output or CommandError, text certbot logs during that run)."""
        clock = clock or Clock()
        calls = []
        self.systemd = []

        def command(*parts, timeout=None, **kwargs):
            if parts[0] == 'systemctl':
                return self.system(*parts)
            calls.append((parts, timeout))
            outcome, logged = outcomes[min(len(calls), len(outcomes)) - 1]
            with self.log.open('a') as log:
                log.write(logged)
            if HOOK_RUN in logged and outcome == HOOK_OK:
                self.stamp.write_text('')  # what the real hook does last
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        state = {'domain': DOMAIN}
        with patch.object(provision, 'run', side_effect=command), self.patches(), \
             patch.object(provision, 'acme_reachability',
                          side_effect=lambda url: self.probes.append(url) or 'HTTP 200'):
            try:
                provision.renew_hook(state, clock=clock, sleep=clock.sleep, jitter=jitter)
                error = None
            except InstallError as caught:
                error = caught
        return state, calls, clock, error

    def test_only_bob_lineage_and_bob_hook_run_and_nothing_is_forced_or_redirected(self):
        state, calls, _, error = self.renew([(HOOK_OK, LOG + HOOK_RUN)])
        self.assertIsNone(error)
        command = calls[0][0]
        self.assertEqual(command, ('certbot', 'renew', '--dry-run', '--cert-name', DOMAIN,
                                   '--no-directory-hooks', '--deploy-hook', provision.RENEW_HOOK,
                                   '--run-deploy-hooks', '--no-random-sleep-on-renew'))
        for forbidden in ('--force-renewal', '--server', '--staging', '--no-verify-ssl',
                          '--disable-hook-validation', '--pre-hook', '--post-hook'):
            self.assertNotIn(forbidden, command)
        self.assertEqual(self.writes, [(provision.RENEW_HOOK, provision.RENEW_HOOK_SCRIPT, 0o700),
                                       (provision.RETRY_DROPIN, retry_dropin(DOMAIN), 0o644)])
        self.assertEqual(self.systemd, [('systemctl', 'daemon-reload'),
                                        ('systemctl', 'show', 'certbot.service',
                                         '--property=DropInPaths', '--value')])
        self.assertEqual(state['renewal_check']['status'], 'PASS')
        self.assertEqual(state['renewal_check']['attempts'], 1)

    def test_retry_dropin_is_installed_only_after_a_proven_hook(self):
        for outcomes in ([(HOOK, HOOK_RUN)], [(certbot_failure(RESET), LOG)], [(certbot_failure(HTTP01), LOG)]):
            with self.subTest(outcomes[0][0].__class__.__name__):
                self.setUp()
                self.renew(outcomes)
                self.assertNotIn(provision.RETRY_DROPIN, [w[0] for w in self.writes])
                self.assertEqual(self.systemd, [])

    def test_unapplied_retry_dropin_is_not_a_pass(self):
        self.system = lambda *parts, **kw: ''  # systemd does not list the drop-in
        state, _, _, error = self.renew([(HOOK_OK, HOOK_RUN)])
        self.assertIn('не применил', str(error))
        self.assertNotIn('renewal_check', state)

    def test_foreign_hooks_cannot_join_the_dry_run(self):
        # renewal-hooks/deploy/10-foreign is excluded by --no-directory-hooks; hooks
        # certbot would still run from the lineage or cli.ini refuse the check.
        foreign = HOOK_RUN.replace('50-selfsni', '10-foreign')
        state, _, _, error = self.renew([(HOOK_OK, foreign + HOOK_RUN)])
        self.assertIn('не подтверждён', str(error))
        self.assertNotIn('renewal_check', state)
        cases = {
            'lineage pre_hook': (f'/etc/letsencrypt/renewal/{DOMAIN}.conf',
                                 RENEWAL_CONF.replace('key_type', 'pre_hook = systemctl stop nginx\nkey_type')),
            'lineage renew_hook': (f'/etc/letsencrypt/renewal/{DOMAIN}.conf',
                                   RENEWAL_CONF.replace('key_type', 'renew_hook = /opt/x\nkey_type')),
            'cli.ini post-hook': ('/etc/letsencrypt/cli.ini', 'max-log-backups = 0\npost-hook = /opt/x\n'),
            'root cli.ini deploy-hook': ('/root/.config/letsencrypt/cli.ini', 'deploy-hook = /opt/x\n'),
            'staging lineage': (f'/etc/letsencrypt/renewal/{DOMAIN}.conf',
                                RENEWAL_CONF.replace('acme-v02', 'acme-staging-v02')),
        }
        for name, (path, content) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.put(path, content)
                state, calls, _, error = self.renew([(HOOK_OK, HOOK_RUN)])
                self.assertIsInstance(error, InstallError)
                self.assertEqual(calls, [])
                self.assertNotIn('renewal_check', state)
        self.setUp()
        self.put('/etc/letsencrypt/cli.ini', '# Debian default\nmax-log-backups = 0\n')
        self.assertIsNone(self.renew([(HOOK_OK, HOOK_RUN)])[3])

    def test_own_hook_failure_with_certbot_exit_zero_is_not_a_pass(self):
        state, calls, _, error = self.renew([(HOOK, HOOK_RUN)])  # certbot only logs the failure
        self.assertEqual(len(calls), 1)
        self.assertIn('deploy hook', str(error))
        self.assertIn('не сбой Let’s Encrypt', str(error))
        self.assertNotIn('renewal_check', state)

    def test_skipped_or_unlogged_hook_is_not_a_pass(self):
        for name, logged in (('skipped', 'Dry run: skipping deploy hook command: x\n'),
                             ('only an older run logged it', '')):
            with self.subTest(name):
                self.setUp()
                state, _, _, error = self.renew([(HOOK_OK, logged)])
                self.assertIn('не подтверждён', str(error))
                self.assertNotIn('renewal_check', state)

    def test_rotated_log_is_read_from_the_start(self):
        self.systemd = []

        def rotate(*parts, **kwargs):
            if parts[0] == 'systemctl':
                return self.system(*parts)
            self.log.rename(self.log.with_suffix('.log.1'))
            self.log.write_text(HOOK_RUN)
            self.stamp.write_text('')
            return HOOK_OK

        clock = Clock()
        with patch.object(provision, 'run', side_effect=rotate), self.patches():
            state = {'domain': DOMAIN}
            provision.renew_hook(state, clock=clock, sleep=clock.sleep, jitter=lambda a, b: 0)
        self.assertEqual(state['renewal_check']['status'], 'PASS')

    def test_staging_connection_reset_with_production_certificate_fails_without_pass(self):
        state, calls, clock, error = self.renew([(certbot_failure(RESET), LOG)])
        self.assertIsInstance(error, InstallError)
        self.assertNotIn('renewal_check', state)
        self.assertEqual(len(calls), provision.RENEWAL_ATTEMPTS)
        message = str(error)
        self.assertIn('НЕ выполнена', message)
        self.assertIn('staging', message)
        self.assertIn('продление не подтверждено', message)
        self.assertIn('POST acme-staging-v02.api.letsencrypt.org new-order', message)
        self.assertIn('без перевыпуска сертификата', message)
        self.assertEqual(self.probes, [url for _, url in provision.ACME_DIRECTORIES])

    def test_transient_failure_then_success_passes_after_one_backoff(self):
        state, calls, clock, error = self.renew([(certbot_failure(RESET), LOG), (HOOK_OK, LOG + HOOK_RUN)])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)
        self.assertEqual(clock.sleeps, [provision.RENEWAL_BACKOFF])
        self.assertEqual(state['renewal_check']['attempts'], 2)

    def test_all_attempts_transient_use_exponential_backoff_and_bounded_count(self):
        _, calls, clock, error = self.renew([(certbot_failure(RESET), LOG)])
        self.assertIsNotNone(error)
        self.assertEqual(clock.sleeps, [30, 60, 120])
        self.assertTrue(all(timeout <= provision.RENEWAL_ATTEMPT_TIMEOUT for _, timeout in calls))

    def test_jitter_is_bounded_and_total_time_never_exceeds_budget(self):
        clock = Clock()

        class Hang(CommandError):
            def __init__(self):
                super().__init__('certbot', output='', timed_out=True)

        def slow_failure(*parts, timeout=None, **kwargs):
            clock.now += timeout  # every attempt hangs until its own timeout
            raise Hang()

        with patch.object(provision, 'run', side_effect=slow_failure) as command, \
             patch.object(provision, 'write'), patch.object(provision, 'UI', None), \
             patch.object(provision, 'SYSTEM_ROOT', self.root), \
             patch.object(provision, 'CERTBOT_LOG', self.log), \
             patch.object(provision, 'acme_reachability', return_value='нет ответа'):
            with self.assertRaises(InstallError):
                provision.renew_hook({'domain': DOMAIN}, clock=clock, sleep=clock.sleep,
                                     jitter=lambda a, b: b)
        self.assertLessEqual(clock.now, provision.RENEWAL_BUDGET)
        # 600 s hang, 45 s pause (30 + max jitter), 555 s hang; a third would overrun 1200 s.
        self.assertEqual([call.kwargs['timeout'] for call in command.call_args_list], [600, 555])
        self.assertEqual(clock.sleeps, [45])

    def test_invalid_http01_configuration_is_reported_once_without_retry(self):
        state, calls, clock, error = self.renew([(certbot_failure(HTTP01), LOG)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn('HTTP-01', str(error))
        self.assertNotIn('сетевая ошибка', str(error))
        self.assertEqual(self.probes, [])
        self.assertNotIn('renewal_check', state)

    def test_hook_failure_with_non_zero_exit_is_not_retried(self):
        state, calls, clock, error = self.renew([(certbot_failure(HOOK), HOOK_RUN)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn('deploy hook', str(error))
        self.assertNotIn('renewal_check', state)

    def test_interrupt_during_backoff_propagates(self):
        clock = Clock()

        def interrupted(seconds):
            raise KeyboardInterrupt

        with patch.object(provision, 'run', side_effect=certbot_failure(RESET)), \
             patch.object(provision, 'write'), patch.object(provision, 'UI', None), \
             patch.object(provision, 'SYSTEM_ROOT', self.root), \
             patch.object(provision, 'CERTBOT_LOG', self.log):
            with self.assertRaises(KeyboardInterrupt):
                provision.renew_hook({'domain': DOMAIN}, clock=clock, sleep=interrupted,
                                     jitter=lambda a, b: 0)


IPV6 = {'interfaces': ['all', 'default', 'lo', 'ens3'],
        'persistentConfig': '/etc/sysctl.d/99-zz-bob-auto-xray-ipv6.conf',
        'networkDropin': '/etc/systemd/network/10-netplan-ens3.network.d/99-zz-bob-auto-xray-ipv4-only.conf'}


def panel_state(mode='skip', **extra):
    """State exactly as PANEL leaves it, then a renewal dry-run failure in SECURITY."""
    state = initialize(dict(
        domain=DOMAIN, ipv4='192.0.2.1', ubuntu='24.04', codename='noble', arch='amd64',
        ssh_ports=[22], panel_port=21000, sub_port=22000,
        panel_path='/' + 'a' * 48 + '/', sub_path='/' + 'b' * 48 + '/',
        username='u' + '1' * 16, password='p' * 48, warp_mode=mode, adopt_warp=mode == 'adopt',
        bbr_preflight={}, ipv6_preflight={}, email='',
        ipv6=dict(IPV6), bbr={'runtime': {}}, swap={'status': 'skipped', 'reason': 'test'},
        portal={'AUTH': 's' + 'c' * 16, 'ICON': 's' + 'd' * 16, 'STYLE': 's' + 'e' * 16,
                'NAME': 'Alderspace', 'CSRF': 's' + 'f' * 16},
        php_service='php8.3-fpm', api_token='T' * 40, xray_sha256='0' * 64,
        xray_bin='/usr/local/x-ui/bin/xray-linux-amd64', reality_private='R' * 43,
        reality_public='P' * 43, short_id='0123456789abcdef',
        admin_client={'id': '11111111-2222-4333-8444-555555555555', 'subId': 'S' * 48},
        xray_version='Xray 26.10.21', xui_version='v3.11.0'), ['/etc/nginx'])
    if mode != 'skip':
        state.update(menu_revision={'origin': 'x', 'branch': 'main', 'commit': 'c' * 40,
                                    'resolved_at': '2026-11-04T00:00:00+00:00'},
                     warp_health={'status': 'HEALTHY'})
    state.update(status='FAILED', stage='SECURITY', last_completed_stage='PANEL',
                 failure='certbot exited 1')
    state.update(extra)
    return state


IDENTITY = ('ipv6', 'bbr', 'swap', 'xray_sha256', 'xray_bin', 'xray_version', 'xui_version',
            'installation_id', 'username', 'password', 'panel_path', 'sub_path', 'panel_port',
            'sub_port', 'api_token', 'reality_private', 'reality_public', 'short_id',
            'admin_client', 'portal')


class LateStateValidationTests(unittest.TestCase):
    def test_security_failure_after_panel_is_resumable(self):
        resumed = validate_resume(panel_state(), DOMAIN, 'skip')
        self.assertEqual(resumed['stage'], 'SECURITY')
        self.assertEqual(resumed['security_steps'], [])
        self.assertEqual(resumed['resume_count'], 1)

    def test_missing_panel_results_block_resume(self):
        for key in ('reality_private', 'api_token', 'admin_client', 'portal', 'xray_sha256'):
            with self.subTest(key=key):
                state = panel_state()
                del state[key]
                with self.assertRaisesRegex(InstallError, 'resume запрещён'):
                    validate_resume(state, DOMAIN, 'skip')
        with self.assertRaisesRegex(InstallError, 'menu_revision'):
            validate_resume(panel_state('install', menu_revision=None), DOMAIN, 'install')

    def test_inconsistent_or_corrupted_progress_blocks_resume(self):
        cases = [dict(last_completed_stage='NGINX_PORTAL'),
                 dict(security_steps=['renewal']),
                 dict(security_steps='fail2ban'),
                 dict(security_steps=['fail2ban', 'renewal', 'firewall', 'doctor'],
                      last_completed_stage='PANEL'),
                 dict(stage='ACCEPTANCE', last_completed_stage='SECURITY', security_steps=['fail2ban']),
                 dict(stage='ACCEPTANCE', last_completed_stage='PANEL')]
        for extra in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(InstallError):
                    validate_resume(panel_state(**extra), DOMAIN, 'skip')

    def test_acceptance_resume_from_pre_checkpoint_state_requires_completed_security(self):
        state = panel_state(stage='ACCEPTANCE', last_completed_stage='SECURITY',
                            doctor_cli={'status': 'installed'})
        self.assertEqual(validate_resume(state, DOMAIN, 'skip')['security_steps'], list(SECURITY_STEPS))
        state = panel_state(stage='ACCEPTANCE', last_completed_stage='SECURITY')
        with self.assertRaisesRegex(InstallError, 'Bob Doctor'):
            validate_resume(state, DOMAIN, 'skip')

    def test_healthy_and_panel_failures_stay_non_resumable(self):
        with self.assertRaises(InstallError):
            validate_resume(panel_state(status='HEALTHY_LOCAL'), DOMAIN, 'skip')
        with self.assertRaises(InstallError):
            validate_resume(panel_state(stage='PANEL', last_completed_stage='NGINX_PORTAL'), DOMAIN, 'skip')


class LateResumeInstallerTests(unittest.TestCase):
    """Run main() against a saved FAILED/SECURITY state with every system action mocked."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'state').mkdir(mode=0o700)

    def save(self, state):
        (self.root / 'state/state.json').write_text(json.dumps(state))

    def saved(self):
        return json.loads((self.root / 'state/state.json').read_text())

    def run_installer(self, mode='skip', security=None, renew=None, verify=None, preflight=None):
        import selfsni.acceptance as acceptance
        self.mocks = mocks = dict(
            security=MagicMock(side_effect=security), renew_hook=MagicMock(side_effect=renew),
            certificate=MagicMock(), decoy=MagicMock(), panel=MagicMock(), firewall=MagicMock(),
            install_warp=MagicMock(), adopt_warp=MagicMock(), AptRunner=MagicMock(),
            install_doctor_cli=MagicMock(return_value={'status': 'installed', 'command': '/usr/local/bin/bob'}),
            check_official_menu=MagicMock(), install_menu=MagicMock(),
            late_resume_preflight=MagicMock(side_effect=preflight or (lambda args, state: dict(state))),
            preflight=MagicMock(side_effect=AssertionError('early preflight on late resume')),
            ipv6=MagicMock(configure=MagicMock(return_value=({'interfaces': ['all'], 'fresh': True}, {}))),
            bbr=MagicMock(), swap=MagicMock(), menu_warp=MagicMock(health=MagicMock(return_value={'status': 'HEALTHY'})))
        verify_mock = MagicMock(side_effect=verify, return_value={'checks': [{'passed': True}]})
        argv = ['install'] + (['--no-warp'] if mode == 'skip' else [])
        with patch.object(provision, 'STATE', self.root / 'state'), \
             patch.object(provision, 'DIAGNOSTIC_LOG', self.root / 'install.log'), \
             patch.object(provision, 'INSTALL_LOCK', self.root / 'install.lock'), \
             patch.object(provision, 'run', return_value='versions'), \
             patch.multiple(provision, **mocks), \
             patch.object(acceptance, 'verify', verify_mock), \
             patch('sys.argv', argv), patch('sys.stdout', io.StringIO()):
            try:
                provision.main()
                code = 0
            except SystemExit as exit_:
                code = exit_.code
        mocks['verify'] = verify_mock
        return code

    def assertNothingRedone(self):
        for name in ('certificate', 'decoy', 'panel', 'install_warp', 'adopt_warp', 'install_menu',
                     'check_official_menu', 'preflight'):
            self.mocks[name].assert_not_called()
        self.mocks['AptRunner'].return_value.update.assert_not_called()
        self.mocks['AptRunner'].return_value.install.assert_not_called()
        self.mocks['bbr'].configure.assert_not_called()
        self.mocks['swap'].configure.assert_not_called()
        # IPv6 was proven read-only by late_resume_preflight(); nothing rewrites it.
        self.mocks['ipv6'].configure.assert_not_called()
        self.mocks['ipv6'].rollback.assert_not_called()

    def test_resume_after_failed_security_reaches_healthy_without_redoing_earlier_stages(self):
        original = panel_state()
        self.save(original)
        self.assertEqual(self.run_installer(), 0)
        self.assertNothingRedone()
        self.mocks['security'].assert_called_once()
        self.mocks['renew_hook'].assert_called_once()
        self.mocks['firewall'].assert_called_once()
        self.assertEqual(self.mocks['firewall'].call_args.kwargs, {'public': True})
        self.mocks['install_doctor_cli'].assert_called_once()
        self.mocks['verify'].assert_called_once()
        final = self.saved()
        self.assertEqual(final['status'], 'HEALTHY_LOCAL')
        self.assertEqual(final['last_completed_stage'], 'HEALTHY_LOCAL')
        self.assertEqual(final['security_steps'], list(SECURITY_STEPS))
        self.assertNotIn('failure', final)
        self.assertEqual(final['resume_count'], 1)
        for key in IDENTITY:
            self.assertEqual(final[key], original[key], key)
        self.assertTrue((self.root / 'state/acceptance.json').exists())

    def test_install_mode_only_rechecks_warp_health(self):
        self.save(panel_state('install'))
        self.assertEqual(self.run_installer('install'), 0)
        self.assertNothingRedone()
        self.mocks['menu_warp'].health.assert_called_once()
        self.mocks['menu_warp'].install.assert_not_called()
        self.assertEqual(self.saved()['menu_revision']['commit'], 'c' * 40)

    def test_repeated_interruption_resumes_from_the_unfinished_step(self):
        original = panel_state()
        self.save(original)
        self.assertEqual(self.run_installer(renew=KeyboardInterrupt), 130)
        interrupted = self.saved()
        self.assertEqual((interrupted['status'], interrupted['stage']), ('FAILED', 'SECURITY'))
        self.assertEqual(interrupted['security_steps'], ['fail2ban'])

        self.assertEqual(self.run_installer(renew=InstallError('staging недоступен')), 1)
        self.mocks['security'].assert_not_called()
        self.assertEqual(self.saved()['security_steps'], ['fail2ban'])

        self.assertEqual(self.run_installer(verify=KeyboardInterrupt), 130)
        self.mocks['security'].assert_not_called()
        self.mocks['renew_hook'].assert_called_once()
        self.assertEqual((self.saved()['stage'], self.saved()['last_completed_stage']),
                         ('ACCEPTANCE', 'SECURITY'))

        self.assertEqual(self.run_installer(), 0)
        for name in ('security', 'renew_hook', 'firewall', 'install_doctor_cli'):
            self.mocks[name].assert_not_called()
        self.assertNothingRedone()
        final = self.saved()
        self.assertEqual(final['status'], 'HEALTHY_LOCAL')
        self.assertEqual(final['resume_count'], 4)
        for key in IDENTITY:
            self.assertEqual(final[key], original[key], key)

    def test_invalid_state_blocks_resume_before_any_change(self):
        cases = {
            'corrupted json': None,
            'missing reality key': panel_state(reality_private=''),
            'other schema': panel_state(schema=1),
        }
        for name, state in cases.items():
            with self.subTest(name):
                path = self.root / 'state/state.json'
                path.write_text('{"stage": "SECURITY", ' if state is None else json.dumps(state))
                before = path.read_text()
                self.assertEqual(self.run_installer(), 1)
                self.assertEqual(path.read_text(), before)
                for action in ('security', 'renew_hook', 'firewall', 'late_resume_preflight'):
                    self.mocks[action].assert_not_called()

    def test_divergent_system_blocks_resume_and_keeps_state(self):
        self.save(panel_state())
        refused = InstallError('/etc/nginx/snippets/selfsni-panel.conf не совпадает с сохранённым состоянием')
        self.assertEqual(self.run_installer(preflight=refused), 1)
        for action in ('security', 'renew_hook', 'firewall'):
            self.mocks[action].assert_not_called()
        self.assertEqual(self.saved()['stage'], 'SECURITY')


class LateResumePreflightTests(unittest.TestCase):
    """late_resume_preflight against a disposable copy of the files a finished PANEL leaves."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = validate_resume(panel_state(), DOMAIN, 'skip')
        xray = b'pinned xray'
        self.state['xray_sha256'] = hashlib.sha256(xray).hexdigest()
        self.put(self.state['xray_bin'], xray)
        self.put('/var/lib/selfsni/panel-ready', 'loopback TLS configured\n')
        self.put('/run/systemd/system/.keep', '')
        self.put(f'/etc/letsencrypt/renewal/{DOMAIN}.conf',
                 '[renewalparams]\nauthenticator = webroot\nwebroot_path = /var/lib/selfsni-acme,\n'
                 'server = https://acme-v02.api.letsencrypt.org/directory\nkey_type = ecdsa\n')
        self.put(f'/etc/letsencrypt/live/{DOMAIN}/fullchain.pem', 'cert')
        self.put('/etc/nginx/sites-available/selfsni-http', provision.http_site(DOMAIN))
        enabled = self.root / 'etc/nginx/sites-enabled/selfsni-http'
        enabled.parent.mkdir(parents=True)
        enabled.symlink_to('/etc/nginx/sites-available/selfsni-http')
        for path, content in provision.nginx_files(self.state).items():
            self.put(path, content)
        self.put('/etc/systemd/system/x-ui.service.d/20-selfsni.conf', provision.xui_unit_override('skip'))
        rules = '*filter\n-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n' \
                '-A ufw-user-input -p tcp --dport 80 -j ACCEPT\n-A ufw-user-input -p tcp --dport 443 -j ACCEPT\n'
        self.put('/etc/ufw/user.rules', rules)
        self.put('/etc/ufw/user6.rules', rules)
        self.commands = []

    def put(self, path, content):
        target = self.root / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        (target.write_bytes if isinstance(content, bytes) else target.write_text)(content)
        target.chmod(0o644)  # main() in other tests leaves the process umask at 077

    def command(self, *parts, **kwargs):
        self.commands.append(parts[:2])
        if parts[:2] == ('openssl', 'x509') and '-checkhost' in parts:
            return f'Hostname {DOMAIN} does match certificate\n'
        return ''

    def preflight(self, state=None, ipv6_report=None):
        args = Namespace(domain=DOMAIN, warp_mode='skip')
        verify = MagicMock(return_value=dict(IPV6) if ipv6_report is None else ipv6_report)
        if isinstance(ipv6_report, Exception):
            verify = MagicMock(side_effect=ipv6_report)
        self.ipv6 = MagicMock(verify=verify)
        with patch.object(provision, 'SYSTEM_ROOT', self.root), \
             patch.object(provision, 'ipv6', self.ipv6), \
             patch.object(provision.os, 'geteuid', return_value=0), \
             patch.object(provision, 'os_info', return_value={}), \
             patch.object(provision.shutil, 'which', return_value='/usr/sbin/ufw'), \
             patch.object(provision, 'run', side_effect=self.command), \
             patch.object(provision, 'verify_panel_bootstrap') as bootstrap:
            result = provision.late_resume_preflight(args, state or self.state)
        bootstrap.assert_called_once()
        return result

    def test_matching_system_is_accepted_and_state_is_reused_verbatim(self):
        result = self.preflight()
        for key in IDENTITY:
            self.assertEqual(result[key], self.state[key])
        self.assertIn(('nginx', '-t'), self.commands)

    def assertRefused(self, pattern):
        with self.assertRaisesRegex(InstallError, pattern):
            self.preflight()

    def test_nginx_rendered_from_other_secrets_is_refused(self):
        other = dict(self.state, panel_port=23000)
        self.put('/etc/nginx/snippets/selfsni-panel.conf',
                 provision.nginx_files(other)['/etc/nginx/snippets/selfsni-panel.conf'])
        self.assertRefused('selfsni-panel.conf не совпадает')

    def test_staging_or_non_webroot_lineage_is_refused(self):
        self.put(f'/etc/letsencrypt/renewal/{DOMAIN}.conf',
                 'authenticator = standalone\nserver = https://acme-staging-v02.api.letsencrypt.org/directory\n')
        self.assertRefused('HTTP-01 webroot')

    def test_expired_certificate_is_refused(self):
        original = self.command

        def expired(*parts, **kwargs):
            if '-checkend' in parts:
                raise CommandError('openssl', 1, 'Certificate will expire')
            return original(*parts)
        self.command = expired
        self.assertRefused('истёк')

    def test_foreign_hook_jail_xray_or_ufw_rule_is_refused(self):
        cases = {
            'hook': lambda: self.put(provision.RENEW_HOOK, '#!/bin/sh\nrm -rf /\n'),
            'jail': lambda: self.put(provision.SSH_JAIL, '[sshd]\nenabled = false\n'),
            'xray': lambda: self.put(self.state['xray_bin'], b'replaced'),
            'ufw': lambda: self.put('/etc/ufw/user.rules', '-A ufw-user-input -p tcp --dport 8080 -j ACCEPT\n'),
            'panel marker': lambda: (self.root / 'var/lib/selfsni/panel-ready').unlink(),
        }
        for name, damage in cases.items():
            with self.subTest(name):
                self.setUp()
                damage()
                with self.assertRaises(InstallError):
                    self.preflight()

    def test_ipv6_is_only_read_and_any_drift_is_refused(self):
        self.preflight()
        self.ipv6.verify.assert_called_once_with()
        self.ipv6.configure.assert_not_called()
        self.ipv6.rollback.assert_not_called()
        with self.assertRaisesRegex(InstallError, 'IPv6 remains enabled.*resume запрещён'):
            self.preflight(ipv6_report=InstallError('IPv6 remains enabled on an interface'))
        moved = dict(IPV6, networkDropin='/etc/systemd/network/10-other.network.d/x.conf')
        with self.assertRaisesRegex(InstallError, 'IPv6'):
            self.preflight(ipv6_report=moved)
        with self.assertRaisesRegex(InstallError, 'IPv6'):
            self.preflight(dict(self.state, ipv6=['not', 'a', 'report']))

    def test_foreign_certbot_hooks_are_refused(self):
        self.put(f'/etc/letsencrypt/renewal/{DOMAIN}.conf',
                 self.root.joinpath(f'etc/letsencrypt/renewal/{DOMAIN}.conf').read_text() +
                 'post_hook = systemctl restart foreign\n')
        self.assertRefused('посторонние hooks')

    def test_completed_renewal_step_requires_bob_hook(self):
        self.state['security_steps'] = ['fail2ban', 'renewal']
        self.assertRefused('50-selfsni')
        self.put(provision.RENEW_HOOK, provision.RENEW_HOOK_SCRIPT)
        os.chmod(self.root / provision.RENEW_HOOK.lstrip('/'), 0o700)
        self.assertRefused('certbot.service.d/50-selfsni.conf')  # the reload retry is part of it
        self.put(provision.RETRY_DROPIN, retry_dropin(DOMAIN))
        self.preflight()

    def test_v042_hook_left_by_the_failed_dry_run_is_accepted(self):
        # What the reported VPS has: v0.4.2 wrote its hook, then the dry run failed.
        self.put(provision.RENEW_HOOK, LEGACY_HOOK_SCRIPT)
        self.assertEqual(self.preflight()['security_steps'], [])
        self.put(provision.RETRY_DROPIN, retry_dropin('other.example.com'))
        self.assertRefused('certbot.service.d/50-selfsni.conf')


if __name__ == '__main__':
    unittest.main()
