"""Server routing and client presets."""
import base64
import copy
import json

from .core import ROOT, require


TAG = 'inbound-443'
BLOCKED_IPS = ['geoip:private', 'ext:geoip_RU.dat:ru', 'geoip:ua']
BLOCKED_DOMAINS = [
    'ext:geosite_RU.dat:category-ads-all',
    'ext:geosite_RU.dat:ru-available-only-inside',
    r'regexp:.*\.ru$',
    r'regexp:.*\.su$',
    r'regexp:.*\.xn--p1ai$',
]
WARP_DOMAINS = ['geosite:category-ai-!cn', 'geosite:category-ip-geo-detect']


def profile(kind):
    require(kind in ('happ', 'incy'), 'Unknown profile type')
    p = json.loads((ROOT / f'profiles/{kind}.json').read_text())
    require(p['GlobalProxy'] == 'true' and 'geoip:ru' in p['DirectIp'], 'Broken split routing')
    return p


def deeplink(kind):
    raw = json.dumps(profile(kind), ensure_ascii=False, separators=(',', ':')).encode()
    return f'{kind}://routing/onadd/' + base64.b64encode(raw).decode()


def _is_api_rule(rule):
    inbound = rule.get('inboundTag')
    return rule.get('outboundTag') == 'api' and (inbound == 'api' or inbound == ['api'])


def server_template(original):
    """Build the fresh-panel template in the same shape as RoutingBasic.tsx.

    The stock `blocked` rules intentionally have no inboundTag. Their `ip` fields
    match destinations, never client source addresses. Keeping them in the exact
    form used by the Basic UI makes a load/save round-trip idempotent.
    """
    result = copy.deepcopy(original)
    outbounds = result.get('outbounds', [])
    direct = [x for x in outbounds if x.get('tag') == 'direct']
    blocked = [x for x in outbounds if x.get('tag') == 'blocked']
    require(len(direct) == 1 and direct[0].get('protocol', '').lower() == 'freedom',
            'Expected one stock direct outbound')
    require(len(blocked) == 1 and blocked[0].get('protocol', '').lower() == 'blackhole',
            'Expected one stock blocked outbound')
    warp = [x for x in outbounds if x.get('tag') == 'warp-outbound']
    require(len(warp) <= 1, 'Duplicate WARP outbounds')

    extras = [x for x in outbounds if x.get('tag') not in ('direct', 'blocked', 'block', 'warp-outbound')]
    result['outbounds'] = [direct[0], blocked[0], {
        'tag': 'warp-outbound',
        'protocol': 'socks',
        'settings': {'servers': [{'address': '127.0.0.1', 'port': 40000, 'users': []}]},
    }, *extras]

    routing = result.setdefault('routing', {})
    # IPOnDemand is deliberate: AsIs cannot apply the RU IP rule when a client
    # sends a foreign domain that resolves to a Russian address. Because the IP
    # rule precedes the compact GeoSite rule, AI/IP-check names still route to
    # WARP after the destination-IP safety check.
    routing['domainStrategy'] = 'IPOnDemand'
    old_rules = routing.get('rules', [])
    service_rules = [x for x in old_rules if _is_api_rule(x)]
    require(service_rules, 'Panel API routing rule is missing')
    unknown = [x for x in old_rules if not _is_api_rule(x) and
               x.get('outboundTag') not in ('direct', 'blocked', 'block', 'warp-outbound')]
    require(not unknown, 'Unexpected pre-existing routing rules on fresh panel')
    routing['rules'] = service_rules + [
        {'type': 'field', 'ip': BLOCKED_IPS, 'outboundTag': 'blocked'},
        {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'blocked'},
        {'type': 'field', 'domain': BLOCKED_DOMAINS, 'outboundTag': 'blocked'},
        {'type': 'field', 'domain': WARP_DOMAINS, 'outboundTag': 'warp-outbound'},
    ]
    result.setdefault('policy', {}).setdefault('system', {}).update(
        statsOutboundUplink=True, statsOutboundDownlink=True)
    result['dns'] = {'servers': ['https+local://1.1.1.1/dns-query'], 'queryStrategy': 'UseIP'}
    return result


def inbound(domain, private, public, short_id, route_only=True):
    return {
        'tag': TAG,
        'enable': True,
        'remark': 'Self-SNI REALITY · add Vision clients manually',
        'listen': '0.0.0.0',
        'port': 443,
        'protocol': 'vless',
        'settings': json.dumps({'clients': [], 'decryption': 'none', 'fallbacks': []}),
        'streamSettings': json.dumps({
            'network': 'tcp',
            'security': 'reality',
            'tcpSettings': {'acceptProxyProtocol': False, 'header': {'type': 'none'}},
            'realitySettings': {
                'show': True,
                'dest': '/run/selfsni/nginx.sock',
                'xver': 1,
                'serverNames': [domain],
                'privateKey': private,
                'shortIds': [short_id],
                'settings': {
                    'publicKey': public,
                    'fingerprint': 'firefox',
                    'serverName': '',
                    'spiderX': '/',
                },
            },
        }),
        # routeOnly keeps sniffed names available for routing without replacing
        # an IP destination with an unrelated SNI. fakedns is harmless without a
        # FakeDNS pool and is accepted by the pinned Xray core.
        'sniffing': json.dumps({
            'enabled': True,
            'destOverride': ['http', 'tls', 'quic', 'fakedns'],
            'routeOnly': route_only,
        }),
        'allocate': json.dumps({'strategy': 'always'}),
    }
