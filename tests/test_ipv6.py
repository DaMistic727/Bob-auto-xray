import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selfsni import ipv6
from selfsni.core import InstallError


class Kernel:
    def __init__(self, fail=None):
        self.values = {f'net.ipv6.conf.{name}.disable_ipv6': '0'
                       for name in ('all', 'default', 'lo', 'ens3')}
        self.values['net.ipv4.ip_forward'] = '0'
        self.fail = fail

    def __call__(self, *args):
        if args[:2] == ('sysctl', '-n'):
            return self.values[args[2]] + '\n'
        if args[:2] == ('sysctl', '-w'):
            key, value = args[2].split('=')
            if key == self.fail:
                raise InstallError('sysctl failed')
            self.values[key] = value
            if key.endswith('.all.disable_ipv6'):
                for interface in ('lo', 'ens3'):
                    self.values[f'net.ipv6.conf.{interface}.disable_ipv6'] = value
            return value
        if args == ('ip', '-j', 'link', 'show'):
            return json.dumps([{'ifname': 'lo'}, {'ifname': 'ens3'}])
        if args == ('ss', '-H', '-6', '-tnp', 'state', 'established'):
            return ''
        if args == ('ip', '-j', '-6', 'address', 'show', 'scope', 'global'):
            return '[]'
        if args == ('ip', '-j', '-4', 'address', 'show', 'scope', 'global'):
            return '[{"ifname":"ens3","addr_info":[{"local":"192.0.2.1"}]}]'
        if args and args[0] == 'curl':
            return '192.0.2.1'
        raise AssertionError(args)


class IPv6Tests(unittest.TestCase):
    def paths(self, root):
        etc = root / 'etc'; run_root = root / 'run'
        (etc / 'sysctl.d').mkdir(parents=True)
        (run_root / 'sysctl.d').mkdir(parents=True)
        return etc, run_root, etc / 'sysctl.d' / '99-zz-bob-auto-xray-ipv6.conf'

    def apply(self, kernel, etc, run_root, config):
        with patch('selfsni.ipv6.socket.getaddrinfo', return_value=[
                (socket.AF_INET, None, None, None, ('192.0.2.1', 80))]):
            return ipv6.configure('test.example', kernel, config, etc, run_root,
                                  '192.0.2.10 12345 192.0.2.1 22')

    def test_apply_and_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            self.apply(kernel, etc, run_root, config)
            self.apply(kernel, etc, run_root, config)
            self.assertEqual(config.read_text(), ipv6.CONTENT)
            self.assertEqual(ipv6.verify(kernel, config, etc, run_root)['interfaces'],
                             ['all', 'default', 'lo', 'ens3'])

    def test_foreign_assignment_refused_without_write(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            (etc / 'sysctl.conf').write_text('net.ipv6.conf.ens3.disable_ipv6 = 0\n')
            with self.assertRaisesRegex(InstallError, 'Foreign persistent'):
                self.apply(kernel, etc, run_root, config)
            self.assertFalse(config.exists())

    def test_sysctl_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d))
            kernel = Kernel('net.ipv6.conf.default.disable_ipv6')
            with self.assertRaises(InstallError):
                self.apply(kernel, etc, run_root, config)
            self.assertFalse(config.exists())
            self.assertEqual(kernel.values['net.ipv6.conf.all.disable_ipv6'], '0')

    def test_ipv6_ssh_and_aaaa_refused(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            with self.assertRaisesRegex(InstallError, 'IPv4 SSH'):
                ipv6.preflight('test.example', kernel, etc, run_root, config,
                               '2001:db8::1 1 2001:db8::2 22')
            with patch('selfsni.ipv6.socket.getaddrinfo', return_value=[
                    (socket.AF_INET6, None, None, None, ('2001:db8::1', 80, 0, 0))]):
                with self.assertRaisesRegex(InstallError, 'AAAA'):
                    ipv6.preflight('test.example', kernel, etc, run_root, config,
                                   '192.0.2.10 1 192.0.2.1 22')

    def test_other_active_ipv6_ssh_refused(self):
        with tempfile.TemporaryDirectory() as d:
            etc, run_root, config = self.paths(Path(d)); kernel = Kernel()
            def command(*args):
                if args == ('ss', '-H', '-6', '-tnp', 'state', 'established'):
                    return 'ESTAB 0 0 [2001:db8::1]:22 [2001:db8::2]:1000 users:(("sshd"))'
                return kernel(*args)
            with self.assertRaisesRegex(InstallError, 'active IPv6 SSH'):
                ipv6.preflight('test.example', command, etc, run_root, config,
                               '192.0.2.10 1 192.0.2.1 22')


if __name__ == '__main__':
    unittest.main()
