#!/usr/bin/env python3
"""Export a public, redacted snapshot of the installed 3x-ui/Xray state."""
import argparse
import json
from pathlib import Path

from selfsni.api import Panel
from selfsni.core import json_obj, save


SENSITIVE_KEYS = {
    'email', 'keyfile', 'password', 'privatekey', 'shortids', 'username',
}


def redact(value, replacements):
    if isinstance(value, dict):
        return {
            key: ('<redacted>' if key.lower() in SENSITIVE_KEYS else redact(item, replacements))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, replacements) for item in value]
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    state = json.loads(Path('/var/lib/selfsni/state.json').read_text())
    api = Panel(state)
    xray_view = json_obj(api.call('xray/', {}, method='POST'))
    xray = json_obj(xray_view['xraySetting'])
    inbound = api.call('inbounds/list')[0]
    settings = api.call('setting/all', {}, method='POST')
    snapshot = {
        'scope': 'redacted 3x-ui API and Xray runtime snapshot',
        'panel': {
            'xraySetting': xray,
            'inbound': inbound,
            'hosts': api.call('hosts/list'),
            'subscriptionSettings': {
                key: settings.get(key) for key in (
                    'subHappTunType', 'subHappLocalProxyAuth', 'subHappTunMode',
                    'subRoutingRules', 'subIncyRoutingRules',
                )
            },
        },
        'runtime': json.loads(Path('/usr/local/x-ui/bin/config.json').read_text()),
    }
    replacements = {
        state['domain']: 'vpn.example.com',
        state['ipv4']: '203.0.113.10',
        state['panel_path']: '/<redacted-panel-path>/',
        state['sub_path']: '/<redacted-subscription-path>/',
        state['reality_private']: '<redacted-private-key>',
        state['reality_public']: '<redacted-public-key>',
        state['short_id']: '<redacted-short-id>',
        state['username']: '<redacted-username>',
        state['password']: '<redacted-password>',
    }
    save(args.output, redact(snapshot, replacements))
    Path(args.output).chmod(0o600)


if __name__ == '__main__':
    main()
