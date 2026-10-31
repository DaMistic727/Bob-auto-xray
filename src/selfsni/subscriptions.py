"""Subscription HTTPS isolation and persisted/public contracts."""
import re
from pathlib import Path
from .api import request, public_subscription_url
from .core import require, run


def validate_paths(state):
    paths = [state['panel_path'], state['sub_path']]
    require(all(re.fullmatch(r'/[a-f0-9]{48}/', p) for p in paths) and paths[0] != paths[1],
            'Panel and subscription prefixes must be distinct 48-hex secrets')
    require(type(state['sub_port']) is int and 1024 <= state['sub_port'] <= 65535 and
            state['sub_port'] != state['panel_port'], 'Invalid internal subscription port')


def verify_settings(state, settings):
    cert = '/etc/letsencrypt/live/'+state['domain']+'/'
    expected = dict(subEnable=True, subListen='127.0.0.1', subPort=state['sub_port'],
                    subDomain=state['domain'], subPath=state['sub_path'],
                    subURI=public_subscription_url(state), subCertFile=cert+'fullchain.pem',
                    subKeyFile=cert+'privkey.pem')
    require(all(settings.get(k) == v for k, v in expected.items()),
            'Subscription persisted TLS/binding/URL settings mismatch')


def verify_network(state):
    from .panel_checks import panel_network
    panel_network(state)
    lines = run('ss', '-H', '-lntp').splitlines()
    listeners = [line for line in lines if line.split()[3].endswith(':'+str(state['sub_port']))]
    pid = run('systemctl','show','x-ui','--property=MainPID','--value').strip()
    require(len(listeners) == 1 and listeners[0].split()[3] == '127.0.0.1:'+str(state['sub_port'])
            and ('pid='+pid+',') in listeners[0], 'Subscription is not exclusively owned loopback TLS')
    for name in ('user.rules','user6.rules'):
        require(not re.search(r'--dport\s+'+str(state['sub_port'])+r'\b', Path('/etc/ufw',name).read_text()),
                'Internal subscription port has a UFW rule')


def verify_public_routes(state, send=request):
    validate_paths(state)
    for path in ('/sub/missing', state['sub_path'], state['sub_path']+'invalid',
                 state['sub_path'][:-1]+'extra/missing'):
        require(send(state['domain'],443,path)[0] in (400,404), 'Unknown subscription route exposed')
    require(send(state['domain'],443,state['sub_path']+state['admin_client']['subId'],
                 headers={'Host':'invalid.example'})[0] == 404, 'Foreign subscription Host accepted')
