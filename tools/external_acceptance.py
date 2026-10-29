#!/usr/bin/env python3
"""Run on a DIFFERENT host; SSH key/agent or an existing ControlMaster required.
No SSH password is accepted or stored. Needs an official compatible Xray binary.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request

from selfsni.acceptance import verify_sub_response
from selfsni.api import public_panel_url
from selfsni.core import require, save, run, free_port, wait_for, InstallError

p=argparse.ArgumentParser()
p.add_argument('--host',required=True)
p.add_argument('--control-path',required=True)
p.add_argument('--identity',help='Optional SSH private key for a direct BatchMode connection')
p.add_argument('--known-hosts',default='/tmp/selfsni-known-hosts',
               help='Known-hosts file containing the already verified VPS host key')
p.add_argument('--xray',required=True)
p.add_argument('--output',default='external-acceptance.json')
a=p.parse_args()

def remote(action):
    ssh = ['ssh','-o','BatchMode=yes','-o','ControlPath='+a.control_path,
           '-o','UserKnownHostsFile='+a.known_hosts]
    if a.identity:
        ssh += ['-o','IdentitiesOnly=yes','-i',a.identity]
    return run(*ssh,a.host,
        'PYTHONPATH=/root/selfsni/src python3 /root/selfsni/tools/remote_fixture.py '+action,timeout=60)

def http(url, agent=None):
    req=urllib.request.Request(url,headers={'User-Agent':agent} if agent else {})
    with urllib.request.urlopen(req,timeout=30) as response:
        return response.status,{k.lower():v for k,v in response.headers.items()},response.read()

created=False
report={'scope':'external Mac client → public VPS TCP/443','checks':[]}
try:
    fixture_json=remote('create')
    created=True
    s=json.loads(fixture_json)
    warp_enabled=s['warp_mode'] != 'skip'
    for kind,agent in [('happ','Happ/3.0.0 (iOS)'),('incy','INCY/1.0.0/ios')]:
        verify_sub_response(kind,*http(f'https://{s["domain"]}:{s["sub_port"]}{s["sub_path"]}{s["client"]["subId"]}',agent))
        report['checks'].append(kind+' external subscription: HTTPS + Base64/JSON/deeplink')
    require(http('https://'+s['domain']+'/')[0]==200,'Public decoy unavailable')
    require(http(public_panel_url(s))[0]==200,'Public panel unavailable')
    if s.get('panel_access') == 'https-443':
        try:
            with socket.create_connection((s['ipv4'], s['panel_port']), timeout=3):
                raise InstallError('Internal panel port is publicly reachable')
        except (ConnectionError, TimeoutError, socket.timeout):
            pass
        report['checks'].append('Internal panel port rejects external TCP connections')
    with tempfile.TemporaryDirectory(prefix='selfsni-external-') as d:
        port=free_port({40000})
        config={'log':{'loglevel':'warning'},'inbounds':[{'listen':'127.0.0.1','port':port,'protocol':'socks','settings':{'auth':'noauth'}}],
            'outbounds':[{'protocol':'vless','settings':{'vnext':[{'address':s['ipv4'],'port':443,'users':[{'id':s['client']['id'],'flow':'xtls-rprx-vision','encryption':'none'}]}]},
            'streamSettings':{'network':'tcp','security':'reality','realitySettings':{'serverName':s['domain'],'fingerprint':'firefox','publicKey':s['reality_public'],'shortId':s['short_id'],'spiderX':'/'}}}]}
        path=Path(d)/'client.json';save(path,config)
        proc=subprocess.Popen([a.xray,'run','-config',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            def fetch(url, must_succeed=True):
                return run('curl','-fsS' if must_succeed else '-sS','--socks5-hostname',f'127.0.0.1:{port}',
                    '--connect-timeout','10','--max-time','35',url)
            wait_for(lambda: bool(fetch('https://example.com')),'External REALITY Vision',attempts=6)
            report['checks'].append('VLESS REALITY Vision: real external connection')
            require('warp=off' in fetch('https://www.cloudflare.com/cdn-cgi/trace'),'DIRECT unexpectedly uses WARP')
            ip=fetch('https://api.ipify.org').strip()
            require((ip != s['ipv4']) if warp_enabled else (ip == s['ipv4']),
                    'IP-check egress does not match selected WARP mode')
            report['checks'].append('Ordinary traffic DIRECT; IP-check uses '+('WARP' if warp_enabled else 'DIRECT'))
            before=json.loads(remote('stats')) if warp_enabled else None
            # HTTP 401/403 from an AI service is still a functional TLS/HTTP exchange.
            response=fetch('https://api.openai.com/v1/models',must_succeed=False)
            require(bool(response),'AI service produced no HTTP body')
            if warp_enabled:
                after=json.loads(remote('stats'))
                def count(obj):
                    return sum(int(x.get('value',0)) for x in obj.get('stat',[]) if x.get('name','').startswith('outbound>>>warp-outbound>>>'))
                require(count(after)>count(before),'AI traffic did not increment WARP outbound counters')
                report['checks'].append('OpenAI HTTP exchange increments live WARP outbound byte counters')
            else:
                report['checks'].append('OpenAI HTTP exchange succeeds over DIRECT')
            for url in ['https://yandex.ru','https://77.88.8.8','https://77.88.8.8.nip.io']:
                try: fetch(url)
                except InstallError: pass
                else: raise InstallError('Russian destination reachable through VPN')
            report['checks'].append('RU domain, RU IP, and foreign-name-to-RU-IP blocked over real external VLESS tunnel')
            if warp_enabled:
                try:
                    with socket.create_connection((s['ipv4'],40000),timeout=3) as sock:
                        sock.settimeout(3)
                        sock.sendall(b'\x05\x01\x00')
                        reply = sock.recv(2)
                        require(not reply.startswith(b'\x05'), 'Public SOCKS5 negotiation exposed')
                except (ConnectionError,TimeoutError,socket.timeout): pass
                report['checks'].append('WARP SOCKS5 negotiation unavailable externally')
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait(timeout=5)
finally:
    if created:
        remote('cleanup')
        report['cleanup']='temporary client removed'
save(a.output,report)
print(json.dumps(report,indent=2))
