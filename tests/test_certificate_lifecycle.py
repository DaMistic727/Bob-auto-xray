"""Certificate lifecycle: resume after issuance, reload retry, Doctor expiry policy.

Mock/stub only: no ACME request, no systemd. The retry command itself is run by
/bin/sh against temporary files.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from selfsni import doctor, provision
from selfsni.core import InstallError
from selfsni.tls_renewal import LEGACY_HOOK_SCRIPT, RENEW_HOOK, RENEW_HOOK_SCRIPT, RETRY_DROPIN, retry_dropin

import test_resume_preflight as resume
from test_doctor import FakeProbe, check

DOMAIN = 'vpn.example.com'
RENEWAL_CONF = ('[renewalparams]\nauthenticator = webroot\nwebroot_path = /var/lib/selfsni-acme,\n'
                'server = https://acme-v02.api.letsencrypt.org/directory\nkey_type = ecdsa\n')


class CertificateResumeTests(unittest.TestCase):
    """A failure after `certbot certonly` succeeded must stay resumable."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.host = resume.Host(directory.name)
        self.host.enable('selfsni-http')
        self.host.own_nginx()
        self.le = Path(directory.name) / 'etc/letsencrypt'

    def lineage(self, name=DOMAIN, conf=RENEWAL_CONF, parts=('live', 'archive', 'renewal')):
        for part in parts:
            target = self.le / part / (name + '.conf' if part == 'renewal' else name)
            if part == 'renewal':
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(conf)
            else:
                target.mkdir(parents=True, exist_ok=True)
                (target / ('fullchain1.pem' if part == 'archive' else 'fullchain.pem')).write_text('pem')
        (self.le / 'live').mkdir(parents=True, exist_ok=True)
        (self.le / 'live/README').write_text('certbot README\n')

    def preflight(self, stage='CERTIFICATE'):
        state = self.host.saved(stage, 'skip', absent_paths=['/etc/nginx', '/etc/letsencrypt'])
        with patch.object(provision, 'SYSTEM_ROOT', self.host.root):
            return self.host.preflight(state)

    def test_bob_lineage_left_by_an_interrupted_certificate_stage_is_accepted(self):
        self.lineage()
        self.assertEqual(self.preflight()['stage'], 'CERTIFICATE')

    def test_no_lineage_is_accepted(self):
        self.assertEqual(self.preflight()['stage'], 'CERTIFICATE')

    def test_foreign_partial_or_early_lineage_is_refused(self):
        cases = {
            'foreign lineage': (dict(), dict(name='other.example.com'), 'Чужие'),
            'second lineage name': (dict(), dict(name=DOMAIN + '-0001'), 'Чужие'),
            'partial lineage': (None, dict(parts=('live', 'archive')), 'Неполный'),
            'lineage with own hooks': (None, dict(conf=RENEWAL_CONF + 'pre_hook = /opt/x\n'), 'hooks'),
            'staging lineage': (None, dict(conf=RENEWAL_CONF.replace('acme-v02', 'acme-staging-v02')),
                                'HTTP-01 webroot'),
        }
        for name, (bob, extra, pattern) in cases.items():
            with self.subTest(name):
                self.setUp()
                if bob is not None:
                    self.lineage()
                self.lineage(**extra)
                with self.assertRaisesRegex(InstallError, pattern):
                    self.preflight()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.host = resume.Host(directory.name)  # WARP: nginx still has its default site
        self.host.enable('default')
        self.host.own_nginx()
        self.le = Path(directory.name) / 'etc/letsencrypt'
        self.lineage()
        with self.assertRaisesRegex(InstallError, 'Неполный'):
            self.preflight('WARP')  # no certificate exists before CERTIFICATE

    def test_certonly_never_reissues_a_valid_certificate(self):
        commands = []
        fake_path = MagicMock(side_effect=lambda value: MagicMock(
            exists=MagicMock(return_value=False), is_symlink=MagicMock(return_value=False)))
        with patch.object(provision, 'run', side_effect=lambda *a, **k: commands.append(a) or ''), \
             patch.object(provision, 'write'), patch.object(provision, 'Path', fake_path):
            provision.certificate({'domain': DOMAIN}, '')
        certonly = next(c for c in commands if c[:2] == ('certbot', 'certonly'))
        self.assertIn('--keep-until-expiring', certonly)
        self.assertNotIn('--force-renewal', certonly)
        self.assertNotIn('--renew-by-default', certonly)


class ReloadRetryTests(unittest.TestCase):
    """The certbot.service ExecStopPost command, run by /bin/sh (subclasses: dash, bash)."""

    shell = '/bin/sh'

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.archive = root / 'archive/fullchain2.pem'
        self.archive.parent.mkdir()
        self.archive.write_text('new certificate')
        (root / 'live' / DOMAIN).mkdir(parents=True)
        (root / 'live' / DOMAIN / 'fullchain.pem').symlink_to(self.archive)
        self.stamp = root / 'tls-reloaded'
        self.ran = root / 'hook-ran'
        self.hook = root / 'hook'
        self.hook.write_text(f'#!/bin/sh\necho run >> {self.ran}\nexit "${{HOOK_EXIT:-0}}"\n')
        self.hook.chmod(0o700)
        unit = retry_dropin(DOMAIN, live=root / 'live', stamp=self.stamp, hook=self.hook)
        line = next(x for x in unit.splitlines() if x.startswith('ExecStopPost='))
        prefix = "ExecStopPost=/bin/sh -c '"
        self.assertTrue(line.startswith(prefix) and line.endswith("'"))
        self.command = line[len(prefix):-1].replace('$$', '$')  # what systemd passes to sh

    def run_retry(self, **env):
        return subprocess.run([self.shell, '-c', self.command], env=dict(os.environ, **env)).returncode

    def age(self, path, seconds):
        os.utime(path, (1_700_000_000 + seconds, 1_700_000_000 + seconds))

    def runs(self):
        return self.ran.read_text().count('run') if self.ran.exists() else 0

    def test_reload_after_success_is_not_repeated(self):
        self.age(self.archive, 0)
        self.stamp.write_text('')
        self.age(self.stamp, 10)
        self.assertEqual(self.run_retry(), 0)
        self.assertEqual(self.runs(), 0)

    def test_renewed_certificate_without_successful_reload_is_retried_until_it_succeeds(self):
        self.stamp.write_text('')
        self.age(self.stamp, 0)
        self.age(self.archive, 10)  # renewed after the last successful reload
        self.assertNotEqual(self.run_retry(HOOK_EXIT='1'), 0)  # failure stays visible
        self.assertNotEqual(self.run_retry(HOOK_EXIT='1'), 0)
        self.assertEqual(self.run_retry(), 0)
        self.assertEqual(self.runs(), 3)

    def test_missing_stamp_triggers_a_reload(self):
        self.assertEqual(self.run_retry(), 0)
        self.assertEqual(self.runs(), 1)

    def test_missing_certificate_does_nothing(self):
        self.archive.unlink()
        self.assertEqual(self.run_retry(), 0)
        self.assertEqual(self.runs(), 0)

    def test_hook_writes_the_stamp_only_after_every_reload(self):
        lines = RENEW_HOOK_SCRIPT.splitlines()
        self.assertEqual(lines[:2], ['#!/bin/sh', 'set -eu'])
        self.assertEqual(lines[-1], 'touch /var/lib/selfsni/tls-reloaded')
        self.assertEqual(RENEW_HOOK_SCRIPT, LEGACY_HOOK_SCRIPT + lines[-1] + '\n')
        self.assertIn(RETRY_DROPIN, provision.CONTROLLED_PATHS)


# Ubuntu's /bin/sh is dash; its `test -nt` differs from bash for a missing file.
for _name in ('dash', 'bash'):
    if shutil.which(_name):
        globals()[f'ReloadRetry{_name.title()}Tests'] = type(
            f'ReloadRetry{_name.title()}Tests', (ReloadRetryTests,), {'shell': shutil.which(_name)})


class DoctorCertificatePolicyTests(unittest.TestCase):
    def status(self, lifetime_days, remaining_days):
        probe = FakeProbe()
        probe.expiry = probe.now_value + timedelta(days=remaining_days)
        probe.issued = probe.expiry - timedelta(days=lifetime_days)
        return check(doctor.Doctor(probe).run(), 'tls.certificate')['status']

    def test_warning_only_once_renewal_is_overdue_for_each_lifetime(self):
        # certbot 4.0 renews with 1/3 left; a warning follows 3 days of failures.
        for lifetime, ok, overdue in ((90, 28, 26), (64, 19, 18), (45, 13, 11)):
            with self.subTest(lifetime=lifetime):
                self.assertEqual(self.status(lifetime, ok), 'PASS')
                self.assertEqual(self.status(lifetime, overdue), 'WARN')
        self.assertEqual(self.status(45, -1), 'FAIL')

    def test_probe_reads_issue_and_expiry_dates(self):
        probe = doctor.Probe()
        probe.command = lambda *args, timeout=5: (
            (0, 'notBefore=Nov  4 00:00:00 2026 GMT\nnotAfter=Dec 19 00:00:00 2026 GMT\n')
            if '-enddate' in args else (0, f'Hostname {DOMAIN} does match certificate\n'))
        probe.read = lambda path: '-----BEGIN CERTIFICATE-----\nMA==\n-----END CERTIFICATE-----\n'
        expiry, matches, _, issued = probe.certificate('/x', DOMAIN)
        self.assertTrue(matches)
        self.assertEqual(expiry - issued, timedelta(days=45))

    def test_current_hook_requires_the_reload_retry(self):
        probe = FakeProbe()
        probe.files[RENEW_HOOK] = RENEW_HOOK_SCRIPT
        self.assertEqual(check(doctor.Doctor(probe).run(), 'tls.hook')['status'], 'WARN')
        probe.files[RETRY_DROPIN] = retry_dropin(DOMAIN)
        self.assertEqual(check(doctor.Doctor(probe).run(), 'tls.hook')['status'], 'PASS')
        probe.files[RETRY_DROPIN] = retry_dropin('other.example.com')
        self.assertEqual(check(doctor.Doctor(probe).run(), 'tls.hook')['status'], 'WARN')
        legacy = FakeProbe()  # v0.4.0-v0.4.2 node: legacy hook without retry
        self.assertEqual(legacy.files[RENEW_HOOK], LEGACY_HOOK_SCRIPT)
        self.assertEqual(check(doctor.Doctor(legacy).run(), 'tls.hook')['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
