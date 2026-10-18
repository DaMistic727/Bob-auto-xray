import base64
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from selfsni.core import InstallError, fqdn, verify_bundle, extract
from selfsni.portal import generate
from selfsni.routing import deeplink, profile, server_template, warp_domains, inbound, TAG
from selfsni.acceptance import verify_sub_response
from selfsni.provision import os_info

class InstallerTests(unittest.TestCase):
    def test_bundle_checksums(self):
        self.assertEqual(verify_bundle()['xui_version'],'v3.10.2')

    def test_profiles_and_http_contract(self):
        for kind in ['happ','incy']:
            link=deeplink(kind)
            self.assertEqual(json.loads(base64.b64decode(link.split('/onadd/')[1])),profile(kind))
            self.assertIn('geoip:ru',profile(kind)['DirectIp'])
            self.assertIn('@a2241b8',profile(kind)['Geoipurl'])
            text='vless://example\n'+(link+'\n' if kind=='incy' else '')
            headers={'routing':link,'routing-enable':'true'} if kind=='happ' else {'routing-enable':'true'}
            self.assertTrue(verify_sub_response(kind,200,headers,base64.b64encode(text.encode())))
        with self.assertRaises(InstallError):
            verify_sub_response('incy',200,{'routing':deeplink('happ')},b'vless://example')

    def test_route_precedence_and_scope(self):
        original={'inbounds':[{'tag':'api'}],'routing':{'rules':[{'inboundTag':['api'],'outboundTag':'api'}]},
                  'outbounds':[{'tag':'direct','protocol':'freedom'},{'tag':'blocked','protocol':'blackhole'}]}
        before=copy.deepcopy(original)
        result=server_template(original)
        self.assertEqual(original,before)
        self.assertEqual(result['inbounds'],before['inbounds'])
        self.assertEqual(result['routing']['rules'][0],before['routing']['rules'][0])
        rules=result['routing']['rules'][1:]
        self.assertEqual([x['outboundTag'] for x in rules],['block','block','warp-outbound','direct'])
        self.assertTrue(all(x['inboundTag']==[TAG] and 'source' not in x for x in rules))
        self.assertEqual(result['routing']['domainStrategy'],'IPOnDemand')
        self.assertNotIn('domain:gstatic.com',warp_domains())
        self.assertIn('domain:chatgpt.com',warp_domains())
        self.assertIn('domain:ipinfo.io',warp_domains())

    def test_portal_uniqueness_and_credential_privacy(self):
        with tempfile.TemporaryDirectory() as d:
            a=generate(Path(d)/'a'); b=generate(Path(d)/'b')
            self.assertNotEqual(a['AUTH'],b['AUTH'])
            self.assertNotEqual(a['STYLE'],b['STYLE'])
            php=(Path(d)/'a/index.php').read_text()
            self.assertNotIn('@@',php)
            self.assertIn('hash_equals',php)
            self.assertNotIn('file_put_contents',php)
            self.assertNotIn('error_log(',php)
            self.assertIn(a['USERFIELD'],php)
            self.assertIn(a['PASSFIELD'],php)
            self.assertIn(a['LAYOUT'],(Path(d)/('a/'+a['STYLE']+'.css')).read_text())

    def test_domain_validation(self):
        self.assertEqual(fqdn('ПРИМЕР.РФ'),'xn--e1afmkfd.xn--p1ai')
        for value in ['localhost','127.0.0.1','x.com;id','-foo.com','foo..com','a.com\nserver{}']:
            with self.assertRaises(InstallError): fqdn(value)

    def test_unsupported_os_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'os-release'; p.write_text('ID=debian\nVERSION_ID="12"\n')
            with self.assertRaises(InstallError): os_info(p)

    def test_reality_zero_clients(self):
        i=inbound('test.example','priv','pub','aabb')
        self.assertEqual(json.loads(i['settings'])['clients'],[])
        self.assertEqual(json.loads(i['streamSettings'])['realitySettings']['xver'],1)
        self.assertEqual(i['tag'],TAG)

    def test_offline_installer_denies_unknown_download(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'map';p.write_text('{}')
            import os,sys
            result=subprocess.run([sys.executable,'tools/offline-curl.py','https://example.com/evil'],env=dict(os.environ,SELFSNI_CURL_MAP=str(p)))
            self.assertEqual(result.returncode,22)

if __name__=='__main__': unittest.main()

class FailureTests(unittest.TestCase):
    def test_warp_requires_functional_trace(self):
        from selfsni.acceptance import check_warp
        # Listener + active service alone must never produce HEALTHY.
        output=['LISTEN 0 1024 127.0.0.1:40000 0.0.0.0:* users:(("warp-svc"))', 'warp=off\n']
        with patch('selfsni.acceptance.run',side_effect=output):
            with self.assertRaises(InstallError): check_warp()

    def test_warp_rejects_public_listener(self):
        from selfsni.acceptance import check_warp
        with patch('selfsni.acceptance.run',return_value='LISTEN 0 1024 0.0.0.0:40000 0.0.0.0:*'):
            with self.assertRaises(InstallError):check_warp()

    def test_json_object_and_string(self):
        from selfsni.core import json_obj
        self.assertEqual(json_obj({'clients':[]}),{'clients':[]})
        self.assertEqual(json_obj('{"clients":[]}'),{'clients':[]})

    def test_certbot_failure_stops_provisioning(self):
        import selfsni.provision as provision
        import contextlib,io
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'state'
            with patch.object(provision,'STATE',state), patch.object(provision,'verify_bundle',return_value={}), \
                 patch.object(provision,'preflight',return_value={'status':'INSTALLING'}), \
                 patch.object(provision,'run'), patch.object(provision,'firewall'), \
                 patch.object(provision,'install_warp'), \
                 patch.object(provision,'certificate',side_effect=InstallError('Certbot validation failed')), \
                 patch.object(provision,'decoy') as next_stage, \
                 patch('sys.argv',['install','--domain','test.example']), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as result: provision.main()
                self.assertEqual(result.exception.code,1)
                self.assertFalse(next_stage.called)
                self.assertIn('FAILED',out.getvalue())
                self.assertNotIn('INSTALLATION COMPLETED',out.getvalue())
                self.assertTrue((state/'FAILED').exists())

    def test_archive_traversal_refused(self):
        import io,tarfile
        with tempfile.TemporaryDirectory() as d:
            archive=Path(d)/'bad.tgz'
            with tarfile.open(archive,'w:gz') as tf:
                info=tarfile.TarInfo('../escaped');info.size=1
                tf.addfile(info,io.BytesIO(b'x'))
            with self.assertRaises(InstallError):extract(archive,Path(d)/'out')


class PreflightOwnershipTests(unittest.TestCase):
    def test_existing_state_is_not_modified_on_refusal(self):
        import selfsni.provision as provision
        import contextlib,io
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)
            (state/'state.json').write_text('{"status":"HEALTHY_LOCAL"}')
            before={p.name:p.read_bytes() for p in state.iterdir()}
            with patch.object(provision,'STATE',state), patch.object(provision,'LOG_ENABLED',False), \
                 patch.object(provision,'verify_bundle',return_value={}), \
                 patch.object(provision,'preflight',side_effect=InstallError('Existing installation')), \
                 patch('sys.argv',['install','--domain','test.example']), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):provision.main()
            self.assertEqual(before,{p.name:p.read_bytes() for p in state.iterdir()})
