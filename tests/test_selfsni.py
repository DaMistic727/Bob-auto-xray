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
from selfsni.geodata import (GEODATA_ASSETS, GEODATA_CRON, GEODATA_TIMEZONE,
                             REQUIRED, automatic_config, categories,
                             verify_server_geodata)
from selfsni.routing import (BLOCKED_DOMAINS, BLOCKED_IPS, WARP_DOMAINS, deeplink,
                             profile, server_template, inbound, TAG)
from selfsni.acceptance import editor_reality_target, verify_sub_response
from selfsni.provision import install_apt_key, os_info

class InstallerTests(unittest.TestCase):
    def test_bundle_checksums(self):
        lock=verify_bundle()
        self.assertEqual(lock['xui_version'],'v3.10.2')
        self.assertEqual(lock['menu_repository'],
                         'https://github.com/Diman6267/VPS-Server-Menu.git')
        self.assertEqual(lock['menu_commit'], '3ed5166bbc5b4fb4cc3bb26616bc4db3bbe2734c')
        self.assertEqual(lock['menu_archive_sha256'],
                         '9d7a2aced39fe206a9cb4cd46481ee6876471458a63988b9fca152238fa7ec29')
        self.assertNotIn('menu_branch', lock)
        self.assertNotIn('menu.tar.gz', lock['downloads'])
        self.assertEqual(set(lock['server_geodata']['files']), set(REQUIRED))

    def test_profiles_and_http_contract(self):
        for kind in ['happ','incy']:
            link=deeplink(kind)
            self.assertEqual(json.loads(base64.b64decode(link.split('/onadd/')[1])),profile(kind))
            self.assertIn('geoip:ru',profile(kind)['DirectIp'])
            self.assertIn('@a2241b8',profile(kind)['Geoipurl'])
            text='vless://id@example.com:443?security=reality&fp=firefox\n'+(link+'\n' if kind=='incy' else '')
            headers=({'routing':link,'routing-enable':'true','tun-type':'xray',
                      'socks-auth-mode':'auto','http-auth-mode':'auto'}
                     if kind=='happ' else {'routing-enable':'true'})
            self.assertTrue(verify_sub_response(kind,200,headers,base64.b64encode(text.encode())))
        with self.assertRaises(InstallError):
            verify_sub_response('incy',200,{'routing':deeplink('happ')},b'vless://example')

    def test_route_precedence_and_scope(self):
        original={'inbounds':[{'tag':'api'}],'routing':{'rules':[
                      {'inboundTag':['api'],'outboundTag':'api'},
                      {'type':'field','ip':['geoip:private'],'outboundTag':'blocked'}]},
                  'outbounds':[{'tag':'direct','protocol':'freedom'},{'tag':'blocked','protocol':'blackhole'}]}
        before=copy.deepcopy(original)
        result=server_template(original)
        self.assertEqual(original,before)
        self.assertEqual(result['inbounds'],before['inbounds'])
        self.assertEqual(result['routing']['rules'][0],before['routing']['rules'][0])
        rules=result['routing']['rules'][1:]
        self.assertEqual([x['outboundTag'] for x in rules],
                         ['blocked','blocked','blocked','warp-outbound'])
        self.assertEqual(rules[0]['ip'], BLOCKED_IPS)
        self.assertEqual(rules[1]['protocol'], ['bittorrent'])
        self.assertEqual(rules[2]['domain'], BLOCKED_DOMAINS)
        self.assertEqual(rules[3]['domain'], WARP_DOMAINS)
        self.assertTrue(all('source' not in x and 'inboundTag' not in x for x in rules))
        self.assertEqual(result['routing']['domainStrategy'],'IPOnDemand')
        self.assertEqual([x['tag'] for x in result['outbounds']],
                         ['direct','blocked','warp-outbound'])
        self.assertNotIn('block', [x['tag'] for x in result['outbounds']])
        self.assertFalse(any(x.get('outboundTag') == 'direct' for x in rules))
        self.assertEqual(result['geodata'], automatic_config())
        self.assertEqual(result['geodata']['cron'], '0 0 * * *')
        self.assertEqual(result['geodata']['outbound'], 'direct')
        self.assertEqual({x['file'] for x in result['geodata']['assets']}, set(REQUIRED))
        self.assertEqual(server_template(result), result)

    def test_native_geodata_schedule_and_sources(self):
        self.assertEqual(GEODATA_TIMEZONE, 'Etc/UTC')
        self.assertEqual(GEODATA_CRON, '0 0 * * *')
        self.assertEqual(len(GEODATA_ASSETS), 4)
        self.assertEqual({x['file'] for x in GEODATA_ASSETS}, set(REQUIRED))
        self.assertTrue(all(x['url'].startswith('https://') for x in GEODATA_ASSETS))
        self.assertTrue(any('Loyalsoldier/v2ray-rules-dat' in x['url'] for x in GEODATA_ASSETS))
        self.assertTrue(any('runetfreedom/russia-v2ray-rules-dat' in x['url'] for x in GEODATA_ASSETS))

    def test_server_geodata_categories(self):
        def varint(value):
            out=bytearray()
            while value > 0x7f:
                out.append((value & 0x7f) | 0x80)
                value >>= 7
            out.append(value)
            return bytes(out)
        def field(number, value):
            return varint((number << 3) | 2) + varint(len(value)) + value
        def database(names):
            return b''.join(field(1, field(1, name.upper().encode())) for name in names)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name, expected in REQUIRED.items():
                (root/name).write_bytes(database(expected))
            found=verify_server_geodata(root)
            self.assertIn('category-ai-!cn',found['geosite.dat'])
            self.assertIn('category-ip-geo-detect',found['geosite.dat'])
            self.assertIn('category-ads-all',found['geosite_RU.dat'])
            self.assertIn('ru-available-only-inside',found['geosite_RU.dat'])
            self.assertIn('ru',categories(root/'geoip_RU.dat'))

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

    def test_apt_key_is_readable_under_private_installer_umask(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'archive-keyring.gpg'
            def fake_run(*args, **kwargs):
                target.write_bytes(b'public key')
            with patch('selfsni.provision.run', side_effect=fake_run):
                install_apt_key('source.asc', target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_reality_zero_clients(self):
        i=inbound('test.example','priv','pub','aabb')
        self.assertEqual(json.loads(i['settings'])['clients'],[])
        stream=json.loads(i['streamSettings'])
        reality=stream['realitySettings']
        self.assertEqual(reality['target'],'/run/selfsni/nginx.sock')
        self.assertNotIn('dest',reality)
        self.assertEqual(editor_reality_target(i),'/run/selfsni/nginx.sock')
        self.assertEqual(reality['xver'],1)
        self.assertTrue(reality['show'])
        self.assertEqual(reality['settings']['fingerprint'],'firefox')
        sniffing=json.loads(i['sniffing'])
        self.assertEqual(sniffing['destOverride'],['http','tls','quic','fakedns'])
        self.assertTrue(sniffing['routeOnly'])
        self.assertFalse(json.loads(inbound('test.example','priv','pub','aabb',False)['sniffing'])['routeOnly'])
        self.assertEqual(i['tag'],TAG)

    def test_reality_target_is_present_in_web_editor_model(self):
        generated = inbound('test.example','priv','pub','aabb')
        reality = json.loads(generated['streamSettings'])['realitySettings']
        self.assertEqual(reality['target'], '/run/selfsni/nginx.sock')
        self.assertNotIn('dest', reality)
        # 3x-ui v3.10.2 rawInboundToFormValues passes raw streamSettings to
        # methods.reset(), so this is the value rendered by the Target field.
        self.assertEqual(editor_reality_target(generated), '/run/selfsni/nginx.sock')

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
        for flag in ('off', 'plus'):
            output=['LISTEN 0 1024 127.0.0.1:40000 0.0.0.0:* users:(("warp-svc"))',
                    f'warp={flag}\n']
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
            apt=MagicMock()
            fixture={'domain':'test.example','ipv4':'192.0.2.1','ubuntu':'26.04','codename':'resolute',
                     'arch':'amd64','ssh_ports':[22],'panel_port':21000,'sub_port':22000,
                     'panel_path':'/panel/','sub_path':'/sub/','username':'user',
                     'password':'password','adopt_warp':False,'warp_mode':'install',
                     'bbr_preflight':{},'status':'INSTALLING'}
            with patch.object(provision,'STATE',state), patch.object(provision,'verify_bundle',return_value={}), \
                 patch.object(provision,'check_official_menu'), \
                 patch.object(provision,'DIAGNOSTIC_LOG',Path(d)/'install.log'), \
                 patch.object(provision,'INSTALL_LOCK',Path(d)/'install.lock'), \
                 patch.object(provision,'CONTROLLED_PATHS',[]), \
                 patch.object(provision,'preflight',return_value=fixture), \
                 patch.object(provision,'AptRunner',return_value=apt), \
                 patch.object(provision,'run'), patch.object(provision,'firewall'), \
                 patch.object(provision.bbr,'configure',return_value={}), \
                 patch.object(provision,'install_warp'), \
                 patch.object(provision,'certificate',side_effect=InstallError('Certbot validation failed')), \
                 patch.object(provision,'decoy') as next_stage, \
                 patch('sys.argv',['install','--domain','test.example']), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as result: provision.main()
                self.assertEqual(result.exception.code,1)
                self.assertFalse(next_stage.called)
                self.assertIn('Причина: Certbot validation failed',out.getvalue())
                self.assertNotIn('INSTALLATION COMPLETED',out.getvalue())
                state_file = state/'state.json'
                self.assertTrue(state_file.exists(), out.getvalue())
                saved = json.loads(state_file.read_text())
                self.assertEqual(saved['status'], 'FAILED')
                self.assertEqual(saved['stage'], 'CERTIFICATE')
                self.assertEqual(saved['failure'], 'Certbot validation failed')

    def test_archive_traversal_refused(self):
        import io,tarfile
        with tempfile.TemporaryDirectory() as d:
            archive=Path(d)/'bad.tgz'
            with tarfile.open(archive,'w:gz') as tf:
                info=tarfile.TarInfo('../escaped');info.size=1
                tf.addfile(info,io.BytesIO(b'x'))
            with self.assertRaises(InstallError):extract(archive,Path(d)/'out')


class PreflightOwnershipTests(unittest.TestCase):
    def test_preflight_only_creates_no_state_log_or_lock(self):
        import selfsni.provision as provision
        import contextlib,io
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'state'; log=Path(d)/'install.log'; lock=Path(d)/'install.lock'
            with patch.object(provision,'STATE',state), \
                 patch.object(provision,'DIAGNOSTIC_LOG',log), \
                 patch.object(provision,'INSTALL_LOCK',lock), \
                 patch.object(provision,'verify_bundle',return_value={}), \
                 patch.object(provision,'preflight',return_value={}) as check, \
                 patch.object(provision,'InstallerLock') as installer_lock, \
                 patch('sys.argv',['install','--domain','test.example','--preflight-only']), \
                 contextlib.redirect_stdout(io.StringIO()):
                provision.main()
            check.assert_called_once()
            installer_lock.assert_not_called()
            self.assertFalse(state.exists())
            self.assertFalse(log.exists())
            self.assertFalse(lock.exists())

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
