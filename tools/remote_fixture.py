#!/usr/bin/env python3
"""Disposable host only. Owned temporary client; secret-free error diagnostics."""
import fcntl
import hashlib
import json
from pathlib import Path
import re
import ssl
import sys
import uuid

from selfsni.api import Panel
from selfsni.clients import (read_clients, validate_admin, snapshot_clients, cleanup_owned_client)
from selfsni.core import json_obj, require, save, token, run, wait_for


def cleanup_fixture(api, fixture, owner):
    if not fixture.exists():
        return
    saved = json.loads(fixture.read_text())
    require(saved.get('owner') == owner, 'Fixture belongs to another run; not removed')
    client = saved['client']

    cleanup_owned_client(api, client, saved.get('protected'))
    fixture.unlink()


def panel_certificate_fingerprint(state):
    pem = Path(f'/etc/letsencrypt/live/{state["domain"]}/fullchain.pem').read_text()
    leaf = pem.split('-----END CERTIFICATE-----')[0]+'-----END CERTIFICATE-----\n'
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(leaf)).hexdigest()


def create_fixture(state, api, fixture, owner):
    require(not fixture.exists(), 'Temporary fixture exists; not replaced')
    original, _ = validate_admin(state, *read_clients(api))
    protected = snapshot_clients(api)
    fingerprint = panel_certificate_fingerprint(state)
    client = {'id': str(uuid.uuid4()), 'flow': 'xtls-rprx-vision', 'email': 'external-e2e-'+token(6),
              'enable': True, 'subId': token(16), 'limitIp': 0, 'totalGB': 0, 'expiryTime': 0, 'reset': 0}
    save(fixture, {'owner': owner, 'client': client, 'inbound_id': original['id'], 'protected': protected})
    try:
        api.call('clients/add', {'client': client, 'inboundIds': [original['id']]})
        run('systemctl', 'restart', 'x-ui')
        wait_for(lambda: bool(api.call('inbounds/list')), 'client activation')
    except BaseException:
        cleanup_fixture(api, fixture, owner)
        raise
    return {k: state[k] for k in ('domain', 'ipv4', 'reality_public', 'short_id',
                                  'sub_port', 'sub_path', 'panel_port', 'panel_path')} | {
        'client': client, 'panel_access': state.get('panel_access'),
        'panel_certificate_sha256': fingerprint,
        'warp_mode': state.get('warp_mode', 'adopt' if state.get('adopt_warp') else 'install')}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    require(len(args) == 2 and args[0] in ('create', 'cleanup', 'stats') and
            re.fullmatch(r'[a-f0-9]{32}', args[1]) is not None, 'Use create/cleanup/stats <run-id>')
    action, owner = args
    state = json.loads(Path('/var/lib/selfsni/state.json').read_text())
    if action == 'stats':
        print(run(state['xray_bin'], 'api', 'statsquery', '--server=127.0.0.1:62789', '-pattern', 'outbound>>>'))
        return
    api = Panel(state)
    fixture = Path('/var/lib/selfsni/external-fixture.json')
    # Serialize a still-running create after SSH loss with the client's cleanup.
    lock_path = fixture.with_suffix('.lock')
    with lock_path.open('a') as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == 'create':
            print(json.dumps(create_fixture(state, api, fixture, owner)))
        else:
            cleanup_fixture(api, fixture, owner)
            print('Owned temporary client cleanup confirmed')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        print('Temporary fixture operation failed; inspect owned marker on VPS', file=sys.stderr)
        raise SystemExit(1)
