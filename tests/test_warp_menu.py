import tempfile
import unittest
from unittest.mock import call, patch

from selfsni.core import InstallError
from selfsni.provision import _health_json, install_warp


HEALTHY = {'status':'HEALTHY','reason':'ok','mode':'proxy','port':40000}


class WarpMenuIntegrationTests(unittest.TestCase):
    def test_health_parser_accepts_preceding_install_output(self):
        import json
        self.assertEqual(_health_json('installing\n' + json.dumps(HEALTHY)), HEALTHY)

    def test_fresh_install_calls_official_cli_then_independent_acceptance(self):
        state = {'adopt_warp':False}
        with tempfile.TemporaryDirectory() as directory, \
             patch('selfsni.provision.install_menu') as menu, \
             patch('selfsni.provision._menu_warp', side_effect=[HEALTHY, HEALTHY]) as cli, \
             patch('selfsni.acceptance.check_warp') as trace:
            install_warp(state, {}, directory)
        menu.assert_called_once()
        self.assertEqual(cli.call_args_list, [
            call('install','--port','40000','--json'),
            call('health','--port','40000','--json')])
        trace.assert_called_once()
        self.assertEqual(state['warp_health']['status'], 'HEALTHY')

    def test_adopt_is_read_only_and_requires_healthy(self):
        state = {'adopt_warp':True}
        down = {'status':'DOWN','reason':'listener missing','mode':'proxy','port':40000}
        with tempfile.TemporaryDirectory() as directory, \
             patch('selfsni.provision.install_menu'), \
             patch('selfsni.provision._menu_warp', return_value=down) as cli:
            with self.assertRaises(InstallError):
                install_warp(state, {}, directory)
        cli.assert_called_once_with('health','--port','40000','--json')


if __name__ == '__main__':
    unittest.main()
