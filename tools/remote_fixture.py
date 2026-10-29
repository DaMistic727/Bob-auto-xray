#!/usr/bin/env python3
"""Disposable host only. Export a temporary client's PUBLIC connection details.
The API token and REALITY private key never leave the VPS.
"""
import json
from pathlib import Path
import sys
import uuid
from selfsni.api import Panel
from selfsni.core import json_obj, require, save, token, run, wait_for

s=json.loads(Path('/var/lib/selfsni/state.json').read_text())
api=Panel(s)
fixture=Path('/var/lib/selfsni/external-fixture.json')
action=sys.argv[1]
if action=='create':
    require(not fixture.exists(),'Temporary fixture already exists; clean it first')
    original=api.call('inbounds/list')[0]
    require(json_obj(original['settings'])['clients']==[],'Zero-client disposable VPS required')
    client={'id':str(uuid.uuid4()),'flow':'xtls-rprx-vision','email':'external-e2e-'+token(6),
            'enable':True,'subId':token(16),'limitIp':0,'totalGB':0,'expiryTime':0,'reset':0}
    save(fixture, {'client': client, 'inbound_id': original['id']})
    api.call('clients/add', {'client': client, 'inboundIds': [original['id']]})
    run('systemctl','restart','x-ui')
    wait_for(lambda: bool(api.call('inbounds/list')), 'client activation')
    print(json.dumps({k:s[k] for k in ['domain','ipv4','reality_public','short_id','sub_port','sub_path','panel_port','panel_path']}
                     | {'client':client, 'panel_access':s.get('panel_access'),
                        'warp_mode':s.get('warp_mode', 'adopt' if s.get('adopt_warp') else 'install')}))
elif action=='cleanup':
    saved=json.loads(fixture.read_text())
    api.call('clients/del/'+saved['client']['email'], {})
    require(json_obj(api.call('inbounds/list')[0]['settings'])['clients']==[],'Cleanup failed')
    fixture.unlink()
    print('Temporary client removed; clients=0')
elif action=='stats':
    print(run(s['xray_bin'],'api','statsquery','--server=127.0.0.1:62789','-pattern','outbound>>>'))
else:
    raise SystemExit('Use create, cleanup, stats')
