"""Resume preflight: TCP/80 ownership by Bob's nginx and carried stage results."""
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from selfsni import provision
from selfsni.core import InstallError
from selfsni.install_state import initialize
from selfsni.menu_upstream import OFFICIAL_REPOSITORY

REAL_EXISTS = Path.exists
HOST_PATHS = {'/run/systemd/system', '/dev/net/tun', '/etc/nginx'}
PREVIOUS = {'APT_UPDATE': 'PREFLIGHT', 'APT_UPGRADE': 'APT_UPDATE',
            'APT_INSTALL': 'APT_UPGRADE', 'WARP': 'FIREWALL', 'CERTIFICATE': 'WARP'}
NGINX_CGROUP = '0::/system.slice/nginx.service'


class Host:
    """Disposable model of exactly the files, processes and sockets preflight reads."""

    def __init__(self, root):
        self.root = Path(root)
        self.nginx = self.root / 'etc/nginx'
        self.proc = self.root / 'proc'
        for name in ('sites-available', 'sites-enabled', 'conf.d'):
            (self.nginx / name).mkdir(parents=True)
        (self.nginx / 'sites-available/default').write_text('server { listen 80 default_server; }\n')
        self.ipv6_config = self.root / 'ipv6.conf'
        self.ipv6_config.write_text(provision.ipv6.CONTENT)
        self.dropin = self.root / 'network/10-netplan-ens3.network.d' / provision.ipv6.NETWORK_DROPIN
        self.dropin.parent.mkdir(parents=True)
        self.dropin.write_text(provision.ipv6.NETWORK_CONTENT)
        self.bbr_config = self.root / 'bbr.conf'
        self.bbr_config.write_text(''.join(f'{k} = {v}\n' for k, v in provision.bbr.DESIRED.items()))
        self.listeners = ['LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))']
        self.main_pid = '0'
        self.swap_verify = MagicMock(side_effect=lambda report: report)

    def enable(self, name):
        target = self.nginx / 'sites-available' / name
        if not target.exists():
            target.write_text('server { listen 80; }\n')
        (self.nginx / 'sites-enabled' / name).symlink_to(target)

    def process(self, pid, cgroup=NGINX_CGROUP, exe='/usr/sbin/nginx'):
        directory = self.proc / str(pid)
        directory.mkdir(parents=True)
        (directory / 'cgroup').write_text(cgroup + '\n')
        os.symlink(exe, directory / 'exe')

    def listen(self, address, *users):
        owners = ','.join(f'("{name}",pid={pid},fd=6)' for name, pid in users)
        self.listeners.append(f'LISTEN 0 511 {address} 0.0.0.0:* users:({owners})')

    def own_nginx(self):
        """Package nginx.service: master is MainPID, worker shares the unit cgroup."""
        self.main_pid = '100'
        self.process(100)
        self.process(101)
        for address in ('0.0.0.0:80', '[::]:80'):
            self.listen(address, ('nginx', 101), ('nginx', 100))

    def saved(self, stage, mode='install', **extra):
        state = initialize(dict(
            domain='vpn.example.com', ipv4='8.8.8.8', ubuntu='26.04', codename='resolute',
            arch='amd64', ssh_ports=[22], panel_port=21000, sub_port=22000,
            panel_path='/' + 'a' * 48 + '/', sub_path='/' + 'b' * 48 + '/',
            username='u' + '1' * 16, password='p' * 48, warp_mode=mode,
            adopt_warp=mode == 'adopt', bbr_preflight={'saved': True},
            ipv6_preflight={'saved': True}, email=''), ['/etc/nginx'])
        state.update(status='FAILED', stage=stage, last_completed_stage=PREVIOUS[stage],
                     failure='simulated')
        if stage in ('WARP', 'CERTIFICATE'):
            state.update(
                ipv6={'interfaces': ['all', 'default', 'lo', 'ens3'],
                      'persistentConfig': str(self.ipv6_config), 'networkDropin': str(self.dropin)},
                bbr={'runtime': dict(provision.bbr.DESIRED), 'persistentConfig': str(self.bbr_config)},
                swap={'status': 'created', 'path': '/swapfile', 'bytes': 2 * 1024**3})
        if stage == 'CERTIFICATE' and mode != 'skip':
            state.update(menu_revision={'origin': OFFICIAL_REPOSITORY, 'branch': 'main',
                                        'commit': 'c' * 40, 'resolved_at': '2026-10-31T00:00:00+00:00'},
                         warp_health={'status': 'HEALTHY', 'saved': True})
        state.update(extra)
        return state

    def command(self, *parts, **kwargs):
        if parts[:2] == ('/usr/sbin/sshd', '-T'):
            return 'port 22\n'
        if parts[:3] == ('ss', '-H', '-lntp'):
            return '\n'.join(self.listeners)
        if parts[0] == 'curl':
            return '8.8.8.8'
        if parts[:2] == ('systemctl', 'show'):
            return self.main_pid + '\n'
        raise AssertionError(parts)

    def bound(self):
        return {int(line.split()[3].rsplit(':', 1)[1]) for line in self.listeners}

    def socket(self, *args, **kwargs):
        host = self

        class Socket:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def setsockopt(self, *_):
                pass

            def bind(self, address):
                if address[1] in host.bound():
                    raise OSError(98, 'Address already in use')
        return Socket()

    def preflight(self, state):
        mode = state['warp_mode']
        args = Namespace(domain='vpn.example.com', ssh_port=None, warp_mode=mode,
                         adopt_warp=mode == 'adopt')

        # The nginx package is installed only by APT_INSTALL.
        host_paths = (HOST_PATHS - {'/etc/nginx'} if state['stage'] in ('APT_UPDATE', 'APT_UPGRADE')
                      else HOST_PATHS)

        def exists(path):
            text = str(path)
            return REAL_EXISTS(path) if text.startswith(str(self.root)) else text in host_paths

        with patch.object(provision.os, 'geteuid', return_value=0), \
             patch.object(provision, 'os_info', return_value={'VERSION_ID': '26.04', 'VERSION_CODENAME': 'resolute'}), \
             patch.object(provision.platform, 'machine', return_value='x86_64'), \
             patch.object(provision.shutil, 'disk_usage', return_value=type('D', (), {'free': 3 * 1024**3})()), \
             patch.object(provision.shutil, 'which',
                          side_effect=lambda name: '/usr/bin/warp-cli' if name == 'warp-cli' and mode == 'adopt' else None), \
             patch.object(provision.Path, 'exists', exists), \
             patch.object(provision, 'NGINX_ROOT', self.nginx, create=True), \
             patch.object(provision, 'PROC_ROOT', self.proc, create=True), \
             patch.object(provision.ipv6, 'CONFIG', self.ipv6_config), \
             patch.object(provision.bbr, 'SYSCTL_CONFIG', self.bbr_config), \
             patch.object(provision.swap, 'verify', self.swap_verify), \
             patch.object(provision, 'run', side_effect=self.command), \
             patch.object(provision.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('8.8.8.8', 80))]), \
             patch.object(provision.socket, 'socket', side_effect=self.socket), \
             patch.object(provision.bbr, 'preflight', return_value={'live': 'bbr'}), \
             patch.object(provision.ipv6, 'preflight', return_value={'live': 'ipv6', 'networkDropin': str(self.dropin)}):
            return provision.preflight(args, state)


class ResumeNginxOwnershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.host = Host(directory.name)

    def assertRefused(self, state, pattern):
        with self.assertRaisesRegex(InstallError, pattern):
            self.host.preflight(state)

    def test_own_package_nginx_on_port_80_is_accepted_after_apt_install(self):
        self.host.enable('default')
        self.host.own_nginx()
        for stage, mode in (('APT_INSTALL', 'install'), ('APT_INSTALL', 'skip'),
                            ('WARP', 'install'), ('WARP', 'skip')):
            with self.subTest(stage=stage, mode=mode):
                self.assertEqual(self.host.preflight(self.host.saved(stage, mode))['stage'], stage)

    def test_own_nginx_is_accepted_during_certificate_resume(self):
        self.host.enable('selfsni-http')
        self.host.own_nginx()
        for mode in ('install', 'adopt', 'skip'):
            with self.subTest(mode=mode):
                self.assertEqual(self.host.preflight(self.host.saved('CERTIFICATE', mode))['stage'],
                                 'CERTIFICATE')

    def test_certificate_resume_accepts_default_site_left_by_interrupted_certbot_stage(self):
        self.host.enable('default')
        self.host.own_nginx()
        self.assertEqual(self.host.preflight(self.host.saved('CERTIFICATE'))['stage'], 'CERTIFICATE')

    def test_certificate_resume_rejects_arbitrary_process_on_port_80(self):
        self.host.enable('selfsni-http')
        self.host.process(300, cgroup='0::/system.slice/foreign.service', exe='/usr/bin/python3')
        self.host.listen('0.0.0.0:80', ('python3', 300))
        self.assertRefused(self.host.saved('CERTIFICATE'), 'Foreign TCP listener on port 80')

    def test_process_named_nginx_outside_nginx_service_is_foreign(self):
        # A container or hand-started nginx can have the same name and binary path.
        for cgroup in ('0::/system.slice/docker-0123.scope', '0::/user.slice/session-4.scope'):
            for stage in ('APT_INSTALL', 'WARP', 'CERTIFICATE'):
                with self.subTest(cgroup=cgroup, stage=stage), tempfile.TemporaryDirectory() as directory:
                    host = Host(directory)
                    host.enable('selfsni-http' if stage == 'CERTIFICATE' else 'default')
                    host.main_pid = '100'
                    host.process(100)
                    host.process(400, cgroup=cgroup)
                    host.listen('0.0.0.0:80', ('nginx', 400))
                    with self.assertRaisesRegex(InstallError, 'Foreign TCP listener on port 80'):
                        host.preflight(host.saved(stage))

    def test_listener_must_include_nginx_service_main_pid(self):
        self.host.enable('default')
        self.host.main_pid = '100'
        self.host.process(100)
        self.host.process(101)
        self.host.listen('0.0.0.0:80', ('nginx', 101))
        self.assertRefused(self.host.saved('WARP'), 'Foreign TCP listener on port 80')

    def test_one_foreign_worker_or_unexpected_binary_breaks_ownership(self):
        cases = {
            'foreign cgroup worker': dict(cgroup='0::/system.slice/foreign.service'),
            'unexpected binary': dict(exe='/tmp/nginx'),
        }
        for name, process in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                host = Host(directory)
                host.enable('default')
                host.main_pid = '100'
                host.process(100)
                host.process(101, **process)
                host.listen('0.0.0.0:80', ('nginx', 100), ('nginx', 101))
                with self.assertRaisesRegex(InstallError, 'Foreign TCP listener on port 80'):
                    host.preflight(host.saved('WARP'))

    def test_extra_foreign_listener_next_to_own_nginx_is_refused(self):
        self.host.enable('default')
        self.host.own_nginx()
        self.host.process(300, cgroup='0::/system.slice/foreign.service', exe='/usr/bin/python3')
        self.host.listen('127.0.0.1:80', ('python3', 300))
        self.assertRefused(self.host.saved('WARP'), 'Foreign TCP listener on port 80')

    def test_own_nginx_never_unlocks_port_443(self):
        self.host.enable('default')
        self.host.own_nginx()
        self.host.listen('0.0.0.0:443', ('nginx', 100))
        self.assertRefused(self.host.saved('WARP'), 'Foreign TCP listener on port 443')

    def test_nginx_listener_is_foreign_before_apt_install(self):
        self.host.enable('default')
        self.host.own_nginx()
        for stage in ('APT_UPDATE', 'APT_UPGRADE'):
            with self.subTest(stage=stage):
                self.assertRefused(self.host.saved(stage), 'Foreign TCP listener on port 80')

    def test_nginx_present_before_installation_is_never_owned(self):
        self.host.enable('default')
        self.host.own_nginx()
        state = self.host.saved('WARP', absent_paths=[])
        self.assertRefused(state, 'Existing installation/configuration: /etc/nginx')

    def test_foreign_nginx_configuration_is_refused(self):
        cases = {
            'conf.d server': lambda host: (host.nginx / 'conf.d/foreign.conf').write_text('server {}\n'),
            'foreign site': lambda host: host.enable('foreign'),
            'default as regular file': lambda host: (host.nginx / 'sites-enabled/default').write_text('server {}\n'),
            'default retargeted': lambda host: (host.nginx / 'sites-enabled/default').symlink_to(
                host.nginx / 'sites-available/selfsni-http'),
        }
        for stage in ('APT_INSTALL', 'WARP', 'CERTIFICATE'):
            for name, configure in cases.items():
                with self.subTest(stage=stage, case=name), tempfile.TemporaryDirectory() as directory:
                    host = Host(directory)
                    (host.nginx / 'sites-available/selfsni-http').write_text('server { listen 80; }\n')
                    configure(host)
                    with self.assertRaisesRegex(InstallError, 'nginx'):
                        host.preflight(host.saved(stage))

    def test_each_ownership_proof_is_required_independently(self):
        """Defence in depth: break exactly one proof while all others hold."""
        def owned(state, lines):
            return provision.owned_nginx_http(state, '\n'.join(lines), self.host.command,
                                              self.host.proc, self.host.nginx)
        self.host.own_nginx()
        state = self.host.saved('WARP')
        self.assertTrue(owned(state, self.host.listeners))
        line = 'LISTEN 0 511 {} 0.0.0.0:* users:(("{}",pid=100,fd=6))'
        self.assertFalse(owned(state, [line.format('0.0.0.0:80', 'python3')]))
        for address in ('127.0.0.1:80', '192.0.2.1:80'):
            self.assertFalse(owned(state, [line.format(address, 'nginx')]))
        self.assertFalse(owned(dict(state, absent_paths=[]), self.host.listeners))
        self.assertFalse(owned(None, self.host.listeners))

    def test_selfsni_site_before_certificate_stage_is_foreign(self):
        self.host.enable('selfsni-http')
        self.assertRefused(self.host.saved('WARP'), 'nginx')


class ResumeCarriedStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.host = Host(directory.name)
        self.host.enable('default')

    def test_skipped_network_stage_results_survive_warp_and_certificate_resume(self):
        for stage in ('WARP', 'CERTIFICATE'):
            for mode in ('install', 'adopt', 'skip'):
                with self.subTest(stage=stage, mode=mode):
                    saved = self.host.saved(stage, mode)
                    result = self.host.preflight(saved)
                    for key in ('ipv6', 'bbr', 'swap'):
                        self.assertEqual(result[key], saved[key])
                    self.host.swap_verify.assert_called_with(saved['swap'])

    def test_menu_revision_survives_only_after_completed_warp_stage(self):
        for mode in ('install', 'adopt'):
            with self.subTest(mode=mode):
                saved = self.host.saved('CERTIFICATE', mode)
                self.assertEqual(self.host.preflight(saved)['menu_revision'], saved['menu_revision'])
        # The WARP stage runs again and resolves the current menu revision itself.
        stale = self.host.saved('WARP', menu_revision={'origin': OFFICIAL_REPOSITORY, 'branch': 'main',
                                                      'commit': 'd' * 40, 'resolved_at': 'x'})
        self.assertNotIn('menu_revision', self.host.preflight(stale))

    def test_live_checks_are_recomputed_not_carried(self):
        saved = self.host.saved('CERTIFICATE')
        result = self.host.preflight(saved)
        self.assertEqual(result['bbr_preflight'], {'live': 'bbr'})
        self.assertEqual(result['ipv6_preflight']['live'], 'ipv6')
        self.assertNotIn('warp_health', result)
        self.assertNotIn('failure', result)

    def test_identity_credentials_and_counters_are_preserved(self):
        for stage in PREVIOUS:
            with self.subTest(stage=stage):
                saved = self.host.saved(stage, resume_count=4)
                result = self.host.preflight(saved)
                for key in ('schema', 'installation_id', 'resume_count', 'absent_paths', 'stage',
                            'last_completed_stage', 'username', 'password', 'panel_path',
                            'sub_path', 'panel_port', 'sub_port'):
                    self.assertEqual(result[key], saved[key])

    def test_stage_results_are_not_carried_into_stages_that_recompute_them(self):
        results = dict(ipv6={'interfaces': []}, bbr={}, swap={'status': 'created'},
                       menu_revision={})
        for stage in ('APT_UPDATE', 'APT_UPGRADE', 'APT_INSTALL'):
            with self.subTest(stage=stage):
                result = self.host.preflight(self.host.saved(stage, **results))
                for key in results:
                    self.assertNotIn(key, result)

    def test_missing_or_invalid_skipped_stage_results_refuse_resume(self):
        # A valid-looking drop-in that the live networkd lookup does not select.
        other = self.host.root / 'other.network.d' / provision.ipv6.NETWORK_DROPIN
        other.parent.mkdir()
        other.write_text(provision.ipv6.NETWORK_CONTENT)
        cases = {
            'swap missing': dict(swap=None),
            'swap unknown status': dict(swap={'status': 'resized'}),
            'swap wrong path': dict(swap={'status': 'created', 'path': '/data/swap', 'bytes': 2 * 1024**3}),
            'bbr missing': dict(bbr=None),
            'bbr wrong runtime': dict(bbr={'runtime': {'net.ipv4.tcp_congestion_control': 'cubic'},
                                           'persistentConfig': None}),
            'ipv6 missing': dict(ipv6=None),
            'ipv6 foreign drop-in': dict(ipv6={'interfaces': ['all'], 'persistentConfig': None,
                                               'networkDropin': str(other)}),
        }
        for stage in ('WARP', 'CERTIFICATE'):
            for name, change in cases.items():
                with self.subTest(stage=stage, case=name):
                    saved = self.host.saved(stage)
                    for key, value in change.items():
                        if value is None:
                            saved.pop(key)
                        else:
                            if 'persistentConfig' in value and value['persistentConfig'] is None:
                                value = dict(value, persistentConfig=saved[key]['persistentConfig'])
                            saved[key] = value
                    with self.assertRaisesRegex(InstallError, 'resume'):
                        self.host.preflight(saved)

    def test_managed_files_must_still_match_carried_results(self):
        for name, path in (('ipv6 sysctl', self.host.ipv6_config), ('networkd', self.host.dropin),
                           ('bbr sysctl', self.host.bbr_config)):
            with self.subTest(name):
                original = path.read_text()
                path.write_text(original + '# changed\n')
                try:
                    with self.assertRaisesRegex(InstallError, 'resume'):
                        self.host.preflight(self.host.saved('WARP'))
                finally:
                    path.write_text(original)

    def test_live_swap_mismatch_refuses_resume(self):
        self.host.swap_verify.side_effect = InstallError('Swap is not active')
        with self.assertRaisesRegex(InstallError, 'Swap is not active'):
            self.host.preflight(self.host.saved('CERTIFICATE'))

    def test_invalid_menu_revision_refuses_certificate_resume(self):
        for revision in (None, {'origin': 'https://example.invalid/menu.git', 'branch': 'main',
                                'commit': 'c' * 40, 'resolved_at': 'x'},
                         {'origin': OFFICIAL_REPOSITORY, 'branch': 'main', 'commit': 'HEAD',
                          'resolved_at': 'x'}):
            with self.subTest(revision=revision):
                saved = self.host.saved('CERTIFICATE')
                if revision is None:
                    saved.pop('menu_revision')
                else:
                    saved['menu_revision'] = revision
                with self.assertRaisesRegex(InstallError, 'resume'):
                    self.host.preflight(saved)

    def test_inconsistent_stage_progress_refuses_resume(self):
        # CERTIFICATE resume skips the network stage; it must be proven completed.
        for stage, last in (('CERTIFICATE', 'APT_INSTALL'), ('WARP', 'APT_UPGRADE'),
                            ('APT_INSTALL', 'PREFLIGHT')):
            with self.subTest(stage=stage, last=last):
                saved = self.host.saved(stage, last_completed_stage=last)
                with self.assertRaisesRegex(InstallError, 'resume'):
                    self.host.preflight(saved)

    def test_interrupted_checkpoint_between_completed_and_next_stage_is_accepted(self):
        saved = self.host.saved('WARP', last_completed_stage='WARP')
        self.assertEqual(self.host.preflight(saved)['swap'], saved['swap'])


if __name__ == '__main__':
    unittest.main()
