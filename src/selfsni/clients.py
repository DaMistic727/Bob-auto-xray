"""Owned bootstrap VPN client and narrow temporary-client cleanup."""
import base64
import copy
import re
import urllib.parse
import uuid

from .api import request
from .core import InstallError, json_obj, register_secrets, require, token

ADMIN_FIELDS = {'email': 'admin', 'flow': 'xtls-rprx-vision', 'enable': True,
                'limitIp': 0, 'totalGB': 0, 'expiryTime': 0, 'reset': 0}


def main_inbound(rows):
    require(isinstance(rows, list), 'Invalid inbound list')
    matches = [row for row in rows if row.get('port') == 443 and row.get('protocol') == 'vless']
    require(len(matches) == 1 and type(matches[0].get('id')) is int,
            'Expected one primary VLESS inbound with a real API ID')
    return matches[0]


def read_clients(api):
    rows, catalog = api.call('inbounds/list'), api.call('clients/list')
    require(isinstance(rows, list) and isinstance(catalog, list), 'Invalid client API response')
    for row in rows:
        require(isinstance(json_obj(row['settings']).get('clients'), list), 'Invalid inbound clients')
    return rows, catalog


def validate_admin(state, rows, catalog, only_admin=False):
    original = main_inbound(rows)
    require(original.get('remark') == 'vless by bob', 'Primary inbound remark was not persisted')
    attached = [(row['id'], client) for row in rows
                for client in json_obj(row['settings'])['clients']]
    admins = [(inbound_id, client) for inbound_id, client in attached if client.get('email') == 'admin']
    records = [client for client in catalog if client.get('email') == 'admin']
    require(len(admins) == len(records) == 1 and admins[0][0] == original['id'],
            'Permanent admin must exist once in the primary inbound and catalog')
    client, record = admins[0][1], records[0]
    expected = state.get('admin_client')
    require(isinstance(expected, dict), 'Owned admin identity is missing from installer state')
    try:
        identity = uuid.UUID(client['id'])
        valid_uuid = identity.version == 4 and str(identity) == client['id']
    except (KeyError, ValueError, TypeError, AttributeError):
        valid_uuid = False
    require(valid_uuid and re.fullmatch(r'[0-9a-f]{48}', client.get('subId', '')) is not None,
            'Invalid permanent admin identity')
    for key, value in {**ADMIN_FIELDS, 'id': expected.get('id'), 'subId': expected.get('subId')}.items():
        require(client.get(key) == value and type(client.get(key)) is type(value),
                'Permanent admin parameters differ from the owned identity')
        # Flow is authoritative on the inbound attachment, not the denormalized
        # ClientRecord.flow (the pinned API may leave it empty).
        if key == 'flow':
            continue
        catalog_key = 'uuid' if key == 'id' else key
        require(record.get(catalog_key) == value and type(record.get(catalog_key)) is type(value),
                'Permanent admin catalog parameters differ')
    require(record.get('limitHwid') == 0 and type(record.get('limitHwid')) is int,
            'Permanent admin device limit is not unlimited')
    require(record.get('inboundIds') == [original['id']], 'Permanent admin has unexpected attachments')
    require(sum(c.get('id') == client['id'] or c.get('subId') == client['subId']
                for _, c in attached) == 1 and
            sum(c.get('uuid') == client['id'] or c.get('subId') == client['subId']
                for c in catalog) == 1, 'Permanent admin identity is not unique')
    if only_admin:
        require(len(attached) == len(catalog) == 1, 'Fresh installation must contain only admin')
    return original, client


def ensure_admin(state, api, persist):
    rows, catalog = read_clients(api)
    original = main_inbound(rows)
    if 'admin_client' not in state:
        require(not any(c.get('email') == 'admin' for c in catalog) and
                not any(c.get('email') == 'admin' for row in rows
                        for c in json_obj(row['settings'])['clients']),
                'Unowned admin exists; refusing to replace or adopt it')
        state['admin_client'] = {**ADMIN_FIELDS, 'id': str(uuid.uuid4()), 'subId': token(24), 'limitHwid': 0}
        register_secrets(state['admin_client']['id'], state['admin_client']['subId'])
        persist(state)  # Root-only checkpoint before the first possibly ambiguous write.
    client = state['admin_client']
    register_secrets(client['id'], client['subId'])
    if any(c.get('email') == 'admin' for c in catalog) or any(
            c.get('email') == 'admin' for row in rows for c in json_obj(row['settings'])['clients']):
        return validate_admin(state, rows, catalog)[1]
    require(not any(c.get('uuid') == client['id'] or c.get('subId') == client['subId'] for c in catalog),
            'Bootstrap client identity collision')
    try:
        api.call('clients/add', {'client': client, 'inboundIds': [original['id']]})
    except Exception:
        # Even success:false/timeout can follow a committed write. Never mint a
        # replacement identity or blindly retry; both read models must agree.
        pass
    try:
        return validate_admin(state, *read_clients(api))[1]
    except Exception:
        raise InstallError('Permanent admin creation was not confirmed; installer remains failed') from None


def verify_admin_runtime(state, config):
    inbound = main_inbound([dict(row, id=index) for index, row in enumerate(config.get('inbounds', []))])
    clients = inbound.get('settings', {}).get('clients', [])
    expected = state['admin_client']
    matches = [c for c in clients if c.get('id') == expected['id']]
    require(len(matches) == 1 and matches[0].get('email') == 'admin' and
            matches[0].get('flow') == 'xtls-rprx-vision', 'Permanent admin is missing from Xray runtime')


def verify_admin_subscriptions(state, send=request):
    expected = state['admin_client']
    for agent in ('Happ/3.0.0 (iOS)', 'INCY/1.0.0/ios'):
        status, _, body = send(state['domain'], state['sub_port'], state['sub_path']+expected['subId'],
                               headers={'User-Agent': agent})
        require(status == 200, 'Permanent admin subscription HTTP error')
        try:
            text = base64.b64decode(body.strip(), validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            text = body.decode()
        links = [urllib.parse.urlsplit(line) for line in text.splitlines() if line.startswith('vless://')]
        require(len(links) == 1 and links[0].username == expected['id'] and
                urllib.parse.parse_qs(links[0].query).get('flow') == ['xtls-rprx-vision'],
                'Permanent admin subscription identity/flow mismatch')


def snapshot_clients(api, exclude=None):
    rows, catalog = read_clients(api)
    # Traffic and timestamps may advance during a real test; identity, limits,
    # enabled flags, attachments and all other client parameters must not change.
    def stable(client):
        return {k: copy.deepcopy(v) for k, v in client.items()
                if k not in ('traffic', 'created_at', 'updated_at', 'createdAt', 'updatedAt')}
    return {'inbounds': {str(row['id']): [stable(c) for c in json_obj(row['settings'])['clients']
                                         if c.get('email') != exclude] for row in rows},
            'catalog': [stable(c) for c in catalog if c.get('email') != exclude]}


def cleanup_owned_client(api, client, protected=None):
    require(client.get('email', '').startswith(('selfsni-e2e-', 'external-e2e-')),
            'Refusing to delete a non-test client')
    def present():
        rows, catalog = read_clients(api)
        attached = [c for row in rows for c in json_obj(row['settings'])['clients']
                    if c.get('email') == client['email']]
        records = [c for c in catalog if c.get('email') == client['email']]
        require(all(c.get('id') == client['id'] and c.get('subId') == client['subId'] for c in attached) and
                all(c.get('uuid') == client['id'] and c.get('subId') == client['subId'] for c in records),
                'Temporary client identity changed; not removed')
        return bool(attached or records)
    if present():
        try:
            api.call('clients/del/'+client['email'], {})
        except Exception:
            if present():
                raise InstallError('Temporary client cleanup not confirmed') from None
    require(not present(), 'Temporary client cleanup not confirmed')
    if protected is not None:
        require(snapshot_clients(api, client['email']) == protected,
                'Permanent clients changed during E2E')
