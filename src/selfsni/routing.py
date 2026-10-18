"""DigneZzZ rules, isolated to proxy destination traffic."""
import base64
import copy
import json
import re
from .core import ROOT, require

# Upstream ip-check also contains push services / RU portals. Only these entries
# are IP-check services. Explicit additions restore the destinations in the spec.
CHECK_ALLOW = {'api.ipify.org', 'checkip.amazonaws.com', 'ifconfig.me'}
CHECK_EXTRA = ['domain:ipinfo.io', 'domain:ip-api.com', 'domain:ipify.org']
TAG = 'inbound-443'

def domains(path):
    result = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        suffix = line.startswith('+.')
        value = line[2:] if suffix else line
        require(re.fullmatch(r'[a-z0-9_.-]+', value) is not None, 'Unsupported domain list syntax')
        result.append(('domain:' if suffix else 'full:') + value)
    require(bool(result), 'Empty domain list')
    return result

def warp_domains():
    checks = domains(ROOT / 'vendor/routing/ip-check.list')
    checks = [x for x in checks if x.split(':', 1)[1] in CHECK_ALLOW]
    return sorted(set(domains(ROOT / 'vendor/routing/ai.list') + checks + CHECK_EXTRA))

def profile(kind):
    require(kind in ('happ', 'incy'), 'Unknown profile type')
    p = json.loads((ROOT / f'profiles/{kind}.json').read_text())
    require(p['GlobalProxy'] == 'true' and 'geoip:ru' in p['DirectIp'], 'Broken split routing')
    return p

def deeplink(kind):
    raw = json.dumps(profile(kind), ensure_ascii=False, separators=(',', ':')).encode()
    return f'{kind}://routing/onadd/' + base64.b64encode(raw).decode()

def server_template(original):
    result = copy.deepcopy(original)
    tags = {x['tag'] for x in result['outbounds']}
    require('direct' in tags and 'warp-outbound' not in tags and 'block' not in tags,
            'Unexpected existing outbound configuration')
    result['outbounds'] += [
        {'tag': 'block', 'protocol': 'blackhole', 'settings': {}},
        {'tag': 'warp-outbound', 'protocol': 'socks', 'settings': {'servers': [
            {'address': '127.0.0.1', 'port': 40000, 'users': []}]}}]
    def rule(**kwargs):
        return dict(type='field', inboundTag=[TAG], **kwargs)
    ru = ['geosite:category-ru', 'domain:ru', 'domain:su', 'domain:xn--p1ai']
    # IPOnDemand resolves a target BEFORE matching domain WARP rules. This is
    # essential: IPIfNonMatch skips RU IP rules when a later domain rule matches.
    result['routing']['domainStrategy'] = 'IPOnDemand'
    result['routing']['rules'] += [
        rule(domain=ru, outboundTag='block'),
        rule(ip=['geoip:ru'], outboundTag='block'),
        rule(domain=warp_domains(), outboundTag='warp-outbound'),
        rule(network='tcp,udp', outboundTag='direct')]
    result.setdefault('policy', {}).setdefault('system', {}).update(statsOutboundUplink=True, statsOutboundDownlink=True)
    result['dns'] = {'servers': ['https+local://1.1.1.1/dns-query'], 'queryStrategy': 'UseIP'}
    return result

def inbound(domain, private, public, short_id):
    return {'tag': TAG, 'enable': True, 'remark': 'Self-SNI REALITY · add Vision clients manually',
            'listen': '0.0.0.0', 'port': 443, 'protocol': 'vless',
            'settings': json.dumps({'clients': [], 'decryption': 'none', 'fallbacks': []}),
            'streamSettings': json.dumps({'network': 'tcp', 'security': 'reality',
                'tcpSettings': {'acceptProxyProtocol': False, 'header': {'type': 'none'}},
                'realitySettings': {'show': False, 'dest': '/run/selfsni/nginx.sock',
                    'xver': 1, 'serverNames': [domain], 'privateKey': private,
                    'shortIds': [short_id], 'settings': {'publicKey': public,
                        'fingerprint': 'chrome', 'serverName': '', 'spiderX': '/'}}}),
            'sniffing': json.dumps({'enabled': True, 'destOverride': ['http', 'tls', 'quic'],
                                   'routeOnly': True}),
            'allocate': json.dumps({'strategy': 'always'})}
