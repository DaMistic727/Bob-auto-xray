import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selfsni import acceptance
from selfsni.core import InstallError
from selfsni.geodata import GEODATA_ASSETS, automatic_config


class GeodataRecoveryTests(unittest.TestCase):
    def test_failed_rollback_restores_assets_and_daily_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = {}
            for asset in GEODATA_ASSETS:
                path = root / asset['file']
                before[path.name] = b'original-' + path.name.encode()
                path.write_bytes(before[path.name])
            current = {'geodata': automatic_config()}

            class FakePanel:
                def __init__(self, state):
                    pass

                def call(self, endpoint, payload, **kwargs):
                    if endpoint == 'xray/':
                        return {'xraySetting': json.dumps(current)}
                    if endpoint == 'xray/update':
                        current.clear()
                        current.update(json.loads(payload['xraySetting']))
                        return None
                    raise AssertionError(endpoint)

            def wait_for(predicate, label, **kwargs):
                if label == 'native geodata download tick':
                    (root / 'geosite.dat').write_bytes(b'corrupt 200 response')
                    raise InstallError('induced geodata failure')
                self.assertTrue(predicate())

            with patch.object(acceptance, 'Panel', FakePanel), \
                 patch.object(acceptance, 'run', return_value=''), \
                 patch.object(acceptance, 'request', return_value=(200, {}, b'')), \
                 patch.object(acceptance, 'xray_process', return_value=Path('/proc/123')), \
                 patch.object(acceptance, 'wait_for', side_effect=wait_for), \
                 patch.object(acceptance, 'verify_server_geodata'):
                with self.assertRaisesRegex(InstallError, 'induced geodata failure'):
                    acceptance.geodata_e2e({'domain': 'vpn.example.com',
                                            'panel_port': 12345, 'panel_path': '/panel/'}, root)
            self.assertEqual(current['geodata'], automatic_config())
            self.assertEqual({name: (root/name).read_bytes() for name in before}, before)
            self.assertFalse(list(root.glob('.selfsni-geodata-*')))


if __name__ == '__main__':
    unittest.main()
