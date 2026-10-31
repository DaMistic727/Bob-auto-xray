import copy
import unittest
from unittest.mock import patch
from selfsni import subscriptions as sub
from selfsni.api import public_subscription_url, public_subscription_port
from selfsni.core import InstallError


def state():
    return dict(domain='vpn.example.com',panel_path='/'+'a'*48+'/',sub_path='/'+'b'*48+'/',
                panel_port=21000,sub_port=22000,subscription_access='https-443',admin_client={'subId':'c'*48})


class SubscriptionTests(unittest.TestCase):
    def test_public_and_legacy_urls(self):
        s=state()
        self.assertEqual(public_subscription_url(s,'id'),'https://vpn.example.com'+s['sub_path']+'id')
        self.assertEqual(public_subscription_port(s),443)
        del s['subscription_access']
        self.assertEqual(public_subscription_port(s),22000)
        self.assertIn(':22000/',public_subscription_url(s,'id'))
        self.assertNotIn('subscription_access',s)

    def test_paths_reject_collision_weak_encoded_traversal_and_port_collision(self):
        sub.validate_paths(state())
        for path in (state()['panel_path'],'/short/','/%2e%2e/','/../', '/'+'a'*48+'/extra/'):
            s=state();s['sub_path']=path
            with self.assertRaises(InstallError):sub.validate_paths(s)
        s=state();s['sub_port']=s['panel_port']
        with self.assertRaises(InstallError):sub.validate_paths(s)

    def test_persisted_settings_fail_closed(self):
        s=state();cert='/etc/letsencrypt/live/'+s['domain']+'/'
        settings=dict(subEnable=True,subListen='127.0.0.1',subPort=22000,subDomain=s['domain'],
                      subPath=s['sub_path'],subURI=public_subscription_url(s),
                      subCertFile=cert+'fullchain.pem',subKeyFile=cert+'privkey.pem')
        sub.verify_settings(s,settings)
        for key,value in [('subListen','0.0.0.0'),('subPort',443),('subURI','http://wrong'),('subKeyFile','')]:
            bad=dict(settings);bad[key]=value
            with self.assertRaises(InstallError):sub.verify_settings(s,bad)

    def test_network_rejects_public_ipv6_or_duplicate_socket_and_ufw_rules(self):
        def exercise(addresses,rule=''):
            lines='\n'.join('LISTEN 0 128 '+a+' 0.0.0.0:* users:(("x-ui",pid=123,fd=4))' for a in addresses)
            with patch('selfsni.panel_checks.panel_network'),patch.object(sub,'run',side_effect=[lines,'123']),patch.object(sub.Path,'read_text',return_value=rule):
                sub.verify_network(state())
        exercise(['127.0.0.1:22000'])
        for addresses in (['0.0.0.0:22000'],['[::]:22000'],['127.0.0.1:22000','[::1]:22000'],[]):
            with self.assertRaises(InstallError):exercise(addresses)
        with self.assertRaises(InstallError):exercise(['127.0.0.1:22000'],'--dport 22000 -j ACCEPT')

    def test_public_negative_routes_and_foreign_host(self):
        sub.verify_public_routes(state(),lambda *a,**k:(404,{},b''))
        with self.assertRaises(InstallError):
            sub.verify_public_routes(state(),lambda *a,**k:(200,{},b''))

    def test_public_and_internal_identity_checks_are_independent(self):
        import base64
        from selfsni.clients import verify_admin_subscriptions
        s=state();s.update(reality_public='public', short_id='abcd')
        s['admin_client']['id']='bc026b34-8565-4320-93eb-efad05e288f5'
        link=('vless://'+s['admin_client']['id']+'@vpn.example.com:443?flow=xtls-rprx-vision&fp=firefox&security=reality&sni=vpn.example.com&pbk=public&sid=abcd').encode()
        for public,port in ((False,22000),(True,443)):
            with patch('selfsni.acceptance.verify_sub_response'),patch('selfsni.api.request'):
                calls=[]
                def send(*args,**kwargs):
                    calls.append(args)
                    return 200,{},base64.b64encode(link)
                verify_admin_subscriptions(s,send,public=public)
                self.assertEqual([a[1] for a in calls],[port,port])
                for bad in (link.replace(b'pbk=public',b'pbk=wrong'),link.replace(b'sni=vpn.example.com',b'sni=wrong'),link.replace(b'xtls-rprx-vision',b'none')):
                    with self.assertRaises(InstallError):
                        verify_admin_subscriptions(s,lambda *a,**k:(200,{},base64.b64encode(bad)),public=public)
