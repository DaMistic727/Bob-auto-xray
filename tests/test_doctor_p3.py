"""Bob Doctor migration audit P3 and the read-only contract."""
import builtins
import json
import os
import re
import unittest
from unittest.mock import patch

from selfsni import doctor

from test_doctor import FakeProbe, check
from test_doctor_migration import legacy

GATE_MARKER = '/var/lib/selfsni/panel-ready'
GATE_DROPIN = '/etc/systemd/system/x-ui.service.d/20-selfsni.conf'

# Every subprocess a full --deep run may start. Anything else (restart, reload,
# enable, warp-cli connect/mode/registration, ufw allow, certbot, x-ui setting)
# would break the read-only contract.
READ_ONLY = [
    r"systemctl is-(active|enabled) [\w.@-]+",
    r"systemctl show (x-ui|warp-svc|nginx\.service) --property=(MainPID|Environment) --value",
    r"systemctl show nginx\.service -p ActiveState -p MainPID -p ControlGroup",
    r"ss -H -(lntp|xl)",
    r"warp-cli --accept-tos --json (status|settings)",
    r"ufw status",
    r"fail2ban-client status [\w-]+",
    r"sysctl -n [\w.]+",
    r"swapon --show=NAME --noheadings --raw",
    r"curl -q --noproxy (\*)? -fsS( --socks5-hostname 127\.0\.0\.1:40000)? --connect-timeout 5 "
    r"--max-time 12 https://www\.cloudflare\.com/cdn-cgi/trace",
]


def run(probe, deep=False):
    probe.sync()
    return doctor.Doctor(probe, deep=deep).run()


class PanelGateTests(unittest.TestCase):
    def status(self, mutate=lambda probe: None, version=None):
        probe = FakeProbe('install')
        if version:
            legacy(probe, version)
        mutate(probe)
        return check(run(probe), 'core.panelGate')['status']

    def test_x_ui_start_gate_is_checked_on_v035_and_later(self):
        self.assertEqual(self.status(), 'PASS')
        for mode in ('adopt', 'skip'):
            probe = FakeProbe(mode)
            self.assertEqual(check(run(probe), 'core.panelGate')['status'], 'PASS')
        # Without the marker ConditionPathExists keeps x-ui down after any restart.
        self.assertEqual(self.status(lambda p: p.files.pop(GATE_MARKER)), 'FAIL')
        self.assertEqual(self.status(lambda p: p.files.pop(GATE_DROPIN)), 'FAIL')
        self.assertEqual(self.status(lambda p: p.files.__setitem__(
            GATE_DROPIN, p.files[GATE_DROPIN].replace('ConditionPathExists', '#ConditionPathExists'))), 'WARN')
        self.assertEqual(self.status(lambda p: p.symlinks.add(GATE_MARKER)), 'FAIL')

    def test_nodes_before_the_gate_skip_it(self):
        self.assertEqual(self.status(version='v0.3.4'), 'SKIP')
        self.assertEqual(self.status(version='v0.3.5'), 'PASS')


class AdminClientTests(unittest.TestCase):
    def status(self, mutate=lambda probe: None, version=None):
        probe = FakeProbe('install')
        if version:
            legacy(probe, version)
        mutate(probe)
        return check(run(probe), 'core.adminClient')['status']

    def clients(self, probe):
        return probe.config['inbounds'][0]['settings']['clients']

    def test_permanent_admin_client_is_reported_without_identity(self):
        self.assertEqual(self.status(), 'PASS')
        self.assertEqual(self.status(lambda p: self.clients(p).pop()), 'WARN')
        self.assertEqual(self.status(lambda p: self.clients(p)[-1].update(flow='')), 'WARN')
        self.assertEqual(self.status(lambda p: self.clients(p).append(
            {'id': 'other', 'email': 'admin', 'flow': 'xtls-rprx-vision'})), 'WARN')
        report = run(FakeProbe('install'))
        self.assertNotIn('SECRET_ADMIN_UUID', json.dumps(report))

    def test_nodes_before_the_admin_client_skip_it(self):
        self.assertEqual(self.status(version='v0.3.5'), 'SKIP')
        self.assertEqual(self.status(version='v0.3.6'), 'PASS')


class StateTemporaryTests(unittest.TestCase):
    def status(self, state_dir):
        probe = FakeProbe('install')
        probe.state_dir = state_dir
        return check(run(probe), 'security.stateTemp')['status']

    def test_leftover_state_temporary_is_reported(self):
        self.assertEqual(self.status(['state.json', 'panel-ready']), 'PASS')
        # atomic_write() leaves .state.json.<random> only after kill -9 or power loss.
        self.assertEqual(self.status(['state.json', '.state.json.k2j9x1ab']), 'WARN')
        self.assertEqual(self.status(['state.json', '.acceptance.json.q8w7e6r5']), 'WARN')
        self.assertEqual(self.status(PermissionError()), 'UNKNOWN')

    def test_temporary_name_is_not_printed(self):
        probe = FakeProbe('install')
        probe.state_dir = ['state.json', '.state.json.k2j9x1ab']
        self.assertNotIn('k2j9x1ab', json.dumps(run(probe)))


class ReadOnlyContractTests(unittest.TestCase):
    def test_full_deep_run_starts_only_read_only_commands(self):
        for mode in ('install', 'adopt', 'skip'):
            with self.subTest(mode=mode):
                probe = FakeProbe(mode)
                run(probe, deep=True)
                commands = [' '.join(call[1]) for call in probe.calls if call[0] == 'command']
                self.assertTrue(commands)
                for command in commands:
                    self.assertTrue(any(re.fullmatch(pattern, command) for pattern in READ_ONLY), command)

    def test_doctor_never_writes_the_filesystem(self):
        real_open = builtins.open

        def guarded_open(file, mode='r', *args, **kwargs):
            if any(flag in mode for flag in 'wax+'):
                raise AssertionError('Doctor opened a file for writing: ' + str(file))
            return real_open(file, mode, *args, **kwargs)

        def forbidden(*args, **kwargs):
            raise AssertionError('Doctor changed the filesystem')
        with patch.object(builtins, 'open', side_effect=guarded_open), \
             patch.multiple(os, replace=forbidden, rename=forbidden, unlink=forbidden,
                            remove=forbidden, chmod=forbidden, mkdir=forbidden, rmdir=forbidden):
            for mode in ('install', 'skip'):
                report = doctor.Doctor(FakeProbe(mode), deep=True).run()
                self.assertEqual(report['overallStatus'], 'HEALTHY')


if __name__ == '__main__':
    unittest.main()
