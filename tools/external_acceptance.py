#!/usr/bin/env python3
"""Disposable VPS + a DIFFERENT client host; verified SSH ControlMaster required.
Exit 0=PASS, 1=FAIL, 2=INCONCLUSIVE. Diagnostics never copy exception messages.
"""
import argparse
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
import urllib.request

from selfsni.acceptance import verify_sub_response
from selfsni.api import public_panel_url
from selfsni.core import require, save, run, free_port, wait_for, InstallError
from selfsni.exposure import probe_panel_port


def remote(args, action, owner):
    require(action in ('create', 'cleanup', 'stats') and
            re.fullmatch(r'[a-f0-9]{32}', owner) is not None, 'Invalid fixture command')
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ControlPath='+args.control_path,
           '-o', 'UserKnownHostsFile='+args.known_hosts]
    if args.identity:
        ssh += ['-o', 'IdentitiesOnly=yes', '-i', args.identity]
    return run(*ssh, args.host,
               'PYTHONPATH=/root/selfsni/src python3 /root/selfsni/tools/remote_fixture.py '
               + action + ' ' + owner, timeout=60)


def http(url, agent=None):
    req = urllib.request.Request(url, headers={'User-Agent': agent} if agent else {})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()


def check(report, name, operation):
    try:
        operation()
        report['checks'].append({'name': name, 'status': 'PASS'})
        return True
    except Exception:
        # HTTP/SSH errors can contain secret URLs or tokens. Use a fixed code.
        report['checks'].append({'name': name, 'status': 'FAIL', 'reason': 'CHECK_FAILED'})
        return False


def vision_checks(args, state, report, remote_call):
    warp_enabled = state['warp_mode'] != 'skip'
    dependent = ['DIRECT trace', 'IP-check egress', 'AI HTTP exchange', 'RU routing block']
    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix='selfsni-external-') as directory:
            port = free_port({40000})
            config = {'log': {'loglevel': 'warning'}, 'inbounds': [
                {'listen': '127.0.0.1', 'port': port, 'protocol': 'socks', 'settings': {'auth': 'noauth'}}],
                'outbounds': [{'protocol': 'vless', 'settings': {'vnext': [
                    {'address': state['ipv4'], 'port': 443, 'users': [
                        {'id': state['client']['id'], 'flow': 'xtls-rprx-vision', 'encryption': 'none'}]}]},
                    'streamSettings': {'network': 'tcp', 'security': 'reality', 'realitySettings': {
                        'serverName': state['domain'], 'fingerprint': 'firefox',
                        'publicKey': state['reality_public'], 'shortId': state['short_id'], 'spiderX': '/'}}}]}
            path = Path(directory)/'client.json'
            save(path, config)
            proc = subprocess.Popen([args.xray, 'run', '-config', str(path)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def fetch(url, must_succeed=True):
                return run('curl', '-fsS' if must_succeed else '-sS', '--socks5-hostname',
                           f'127.0.0.1:{port}', '--connect-timeout', '10', '--max-time', '35', url)

            def connect():
                wait_for(lambda: bool(fetch('https://example.com')), 'External REALITY Vision', attempts=6)

            if not check(report, 'VLESS REALITY Vision', connect):
                report['checks'].extend({'name': name, 'status': 'NOT TESTED'} for name in dependent)
                return
            check(report, dependent[0], lambda: require(
                'warp=off' in fetch('https://www.cloudflare.com/cdn-cgi/trace'), 'Unexpected WARP'))

            def ip_check():
                ip = fetch('https://api.ipify.org').strip()
                require((ip != state['ipv4']) if warp_enabled else (ip == state['ipv4']),
                        'Unexpected egress')
            check(report, dependent[1], ip_check)

            def ai_check():
                before = json.loads(remote_call('stats')) if warp_enabled else None
                require(bool(fetch('https://api.openai.com/v1/models', must_succeed=False)), 'Empty response')
                if warp_enabled:
                    after = json.loads(remote_call('stats'))
                    def count(obj):
                        return sum(int(x.get('value', 0)) for x in obj.get('stat', [])
                                   if x.get('name', '').startswith('outbound>>>warp-outbound>>>'))
                    require(count(after) > count(before), 'No WARP traffic increment')
            check(report, dependent[2], ai_check)

            def ru_check():
                for url in ('https://yandex.ru', 'https://77.88.8.8', 'https://77.88.8.8.nip.io'):
                    try:
                        fetch(url)
                    except InstallError:
                        continue
                    raise InstallError('RU destination reachable')
            check(report, dependent[3], ru_check)
            if warp_enabled:
                def socks_check():
                    try:
                        with socket.create_connection((state['ipv4'], 40000), timeout=3) as sock:
                            sock.settimeout(3)
                            sock.sendall(b'\x05\x01\x00')
                            require(not sock.recv(2).startswith(b'\x05'), 'Public SOCKS5 negotiation')
                    except (ConnectionError, TimeoutError):
                        pass
                check(report, 'External WARP SOCKS5 negotiation unavailable', socks_check)
    finally:
        if proc is not None:
            def stop():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            check(report, 'Local Xray process cleanup', stop)


def run_acceptance(args):
    report = {'scope': 'external client; one observation point', 'checks': []}
    owner = secrets.token_hex(16)
    remote_call = lambda action: remote(args, action, owner)
    attempted = False
    try:
        # Cover partial create, lost SSH responses and bad fixture JSON as well.
        attempted = True
        state = json.loads(remote_call('create'))
        for kind, agent in [('happ', 'Happ/3.0.0 (iOS)'), ('incy', 'INCY/1.0.0/ios')]:
            def subscription(kind=kind, agent=agent):
                from selfsni.api import public_subscription_url
                url = public_subscription_url(state, state["client"]["subId"])
                verify_sub_response(kind, *http(url, agent))
            check(report, kind+' external subscription', subscription)
        check(report, 'Public decoy', lambda: require(
            http('https://'+state['domain']+'/')[0] == 200, 'Decoy unavailable'))
        check(report, 'Public panel', lambda: require(
            http(public_panel_url(state))[0] == 200, 'Panel unavailable'))
        if state.get('panel_access') == 'https-443':
            try:
                evidence = probe_panel_port(state['ipv4'], state['panel_port'], state['domain'],
                                            expected_certificate_sha256=state.get('panel_certificate_sha256'))
            except Exception:
                evidence = {'status': 'INCONCLUSIVE', 'reason': 'PROBE_DID_NOT_COMPLETE'}
            report['checks'].append(dict(evidence, name='Internal panel port external isolation'))
        # A FAIL/INCONCLUSIVE port observation does not skip independent checks.
        vision_checks(args, state, report, remote_call)
    except (Exception, KeyboardInterrupt):
        report['checks'].append({'name': 'External E2E execution', 'status': 'FAIL',
                                 'reason': 'RUN_DID_NOT_COMPLETE'})
    finally:
        if attempted:
            cleaned = check(report, 'Temporary VPN client cleanup', lambda: remote_call('cleanup'))
            report['cleanup'] = 'CONFIRMED' if cleaned else 'NOT VERIFIED'
    statuses = [item['status'] for item in report['checks']]
    report['status'] = ('FAIL' if 'FAIL' in statuses else
                        'INCONCLUSIVE' if any(x != 'PASS' for x in statuses) else 'PASS')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--control-path', required=True)
    parser.add_argument('--identity', help='Optional SSH private key')
    parser.add_argument('--known-hosts', default='/tmp/selfsni-known-hosts')
    parser.add_argument('--xray', required=True)
    parser.add_argument('--output', default='external-acceptance.json')
    args = parser.parse_args(argv)
    report = run_acceptance(args)
    save(args.output, report)
    print(json.dumps(report, indent=2))
    return {'PASS': 0, 'FAIL': 1, 'INCONCLUSIVE': 2}[report['status']]


if __name__ == '__main__':
    raise SystemExit(main())
