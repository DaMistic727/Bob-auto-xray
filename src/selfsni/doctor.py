"""Read-only, on-host diagnostics for an installed Bob Auto Xray server.

Never call acceptance.verify here: acceptance intentionally writes panel settings.
All output is assembled from fixed text and small, non-secret measurements.
"""
import argparse
from datetime import datetime, timezone
import email.utils
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys

from .geodata import GEODATA_CRON, GEODATA_TIMEZONE, REQUIRED, automatic_config, categories
from .core import InstallError, fqdn
from .install_state import SCHEMA, warp_mode
from .routing import BLOCKED_DOMAINS, BLOCKED_IPS, WARP_DOMAINS
from . import ipv6, swap


STATE = '/var/lib/selfsni/state.json'
CONFIG = '/usr/local/x-ui/bin/config.json'
SOCKET = '/run/selfsni/nginx.sock'
SCHEMA_VERSION = 1
CA_BUNDLE = '/etc/ssl/certs/ca-certificates.crt'
SUGGEST = {
    'state': 'Проверьте завершение установки и права root; не запускайте resume без выяснения причины.',
    'service': 'Проверьте статус и журнал службы вручную; Doctor её не перезапускает.',
    'config': 'Сверьте конфигурацию в панели и runtime-файл; перед правками сохраните резервную копию.',
    'listener': 'Проверьте службу Xray и занятость TCP/443 вручную.',
    'socket': 'Проверьте конфигурацию и состояние nginx; не меняйте сокет во время активных подключений.',
    'cert': 'Проверьте сертификат и Certbot вручную; Doctor не запускает обновление.',
    'warp': 'Проверьте warp-svc, proxy mode и SOCKS5 вручную, сохранив регистрацию WARP.',
    'routing': 'Проверьте порядок правил и outbounds в 3x-ui без сохранения через Doctor.',
    'geodata': 'Проверьте штатные GeoData assets и расписание в 3x-ui вручную.',
    'security': 'Проверьте защиту и права файлов вручную.',
    'system': 'Проверьте параметры ядра и доступное место вручную.',
    'ipv6': 'Сверьте IPv6 sysctl, networkd drop-in и реальные интерфейсы вручную.',
    'swap': 'Сверьте state, /swapfile, /etc/fstab и активный swap вручную.',
    'deep': 'Повторите проверку позже и сопоставьте с журналами; сбой внешнего endpoint не доказывает отказ VPS.',
}


class Probe:
    """Narrow I/O boundary. Commands have fixed arguments and bounded runtime."""

    def read(self, path):
        return Path(path).read_text()

    def stat(self, path):
        return Path(path).stat()

    def is_symlink(self, path):
        return Path(path).is_symlink()

    def command(self, *args, timeout=5):
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                check=False, stdin=subprocess.DEVNULL, start_new_session=True,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                                     'HOME': '/', 'LC_ALL': 'C'})
        return result.returncode, result.stdout

    def certificate(self, path, domain):
        code, value = self.command('openssl', 'x509', '-enddate', '-noout', '-in', path)
        if code or not value.startswith('notAfter='):
            raise ValueError('certificate date unavailable')
        expiry = email.utils.parsedate_to_datetime(value.split('=', 1)[1].strip())
        code, value = self.command('openssl', 'x509', '-checkhost', domain, '-noout', '-in', path)
        if code and 'does NOT match certificate' not in value:
            raise ValueError('certificate hostname unavailable')
        matches = 'does match certificate' in value and 'does NOT match' not in value
        pem = self.read(path).split('-----END CERTIFICATE-----', 1)[0] + '-----END CERTIFICATE-----\n'
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()
        return expiry, matches, fingerprint

    def tls(self, domain, port):
        context = verified_context()
        with socket.create_connection(('127.0.0.1', port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=domain) as conn:
                conn.settimeout(5)
                return hashlib.sha256(conn.getpeercert(binary_form=True)).digest()

    def get(self, domain, port, path):
        # HTTP(S) GET uses loopback and verified SNI; response bodies are discarded.
        from .api import LocalTLS
        conn = LocalTLS(domain, port, timeout=7, context=verified_context())
        try:
            conn.request('GET', path, headers={'Connection': 'close'})
            return conn.getresponse().status
        finally:
            conn.close()

    def disk_free(self):
        return shutil.disk_usage('/').free

    def bbr_conflicts(self):
        from .bbr import conflicts
        return bool(conflicts())

    def ipv6_snapshot(self):
        # Reuse only the read-only topology parser, never ipv6.verify/configure.
        def output(*args):
            code, value = self.command(*args)
            if code:
                raise OSError('IPv6 probe unavailable')
            return value
        path = ipv6._network_path(output, Path('/etc'), '1.1.1.1')
        names = ['all', 'default', *ipv6._interfaces(output)]
        values = {name: output('sysctl', '-n',
                              f'net.ipv6.conf.{name}.disable_ipv6').strip()
                  for name in dict.fromkeys(names)}
        addresses = json.loads(output('ip', '-j', '-6', 'address', 'show', 'scope', 'global'))
        return path, values, addresses

    def ipv6_conflicts(self):
        return ipv6._foreign(Path('/etc'), Path('/run'), ipv6.CONFIG)

    def categories(self, path):
        return categories(path)

    def xray_timezone(self, binary):
        target = Path(binary).resolve()
        found = False
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if (proc / 'exe').resolve() == target:
                    found = True
                    if b'TZ=Etc/UTC' not in (proc / 'environ').read_bytes().split(b'\0'):
                        return False
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
        return True if found else None

    def process_exe(self, pid):
        return (Path('/proc') / str(pid) / 'exe').resolve(strict=True)

    def now(self):
        return datetime.now(timezone.utc)


def _result(identifier, component, status, summary, details=None, suggestion=None):
    return dict(id=identifier, component=component, status=status, summary=summary,
                details=details or {}, suggestion=suggestion)


def verified_context():
    """Use Ubuntu's CA bundle without SSLKEYLOGFILE or CA env overrides."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=CA_BUNDLE)
    return context


class Doctor:
    def __init__(self, probe=None, deep=False):
        self.p = probe or Probe()
        self.deep = deep
        self.checks = []
        self.state = None
        self.config = None
        self.mode = None
        self.cert_fingerprint = None

    def add(self, identifier, component, method):
        try:
            status, summary, details, suggestion = method()
            assert status in ('PASS', 'WARN', 'FAIL', 'SKIP', 'UNKNOWN')
            self.checks.append(_result(identifier, component, status, summary,
                                       details, suggestion))
        except Exception:
            # Exceptions can contain paths, credentials, command output or URLs.
            self.checks.append(_result(identifier, component, 'UNKNOWN',
                'Проверка не дала достоверного результата.', suggestion=SUGGEST.get(component)))

    @staticmethod
    def answer(status, summary, details=None, suggestion=None):
        return status, summary, details or {}, suggestion

    def state_check(self):
        try:
            if self.p.is_symlink(STATE):
                return self.answer('UNKNOWN', 'State является ссылкой; принадлежность Bob не подтверждена.',
                                   suggestion=SUGGEST['state'])
            value = json.loads(self.p.read(STATE))
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            return self.answer('UNKNOWN', 'Состояние Bob недоступно или повреждено.',
                               suggestion=SUGGEST['state'])
        if not isinstance(value, dict) or value.get('schema') != SCHEMA or not value.get('installation_id'):
            return self.answer('UNKNOWN', 'Состояние установки не подтверждает принадлежность Bob.',
                               suggestion=SUGGEST['state'])
        if value.get('status') != 'HEALTHY_LOCAL' or value.get('stage') != 'HEALTHY_LOCAL':
            return self.answer('UNKNOWN', 'Установка ещё не завершена; функциональные пробы пропущены.',
                               suggestion=SUGGEST['state'])
        try:
            mode = warp_mode(value)
            if not isinstance(value.get('domain'), str) or not value['domain']:
                raise ValueError('domain')
            if fqdn(value['domain']) != value['domain']:
                raise ValueError('domain')
            for key in ('panel_port', 'sub_port'):
                if not 1 <= int(value[key]) <= 65535:
                    raise ValueError(key)
            if (int(value['panel_port']) == int(value['sub_port']) or
                    443 in (int(value['panel_port']), int(value['sub_port']))):
                raise ValueError('ports')
            for key in ('panel_path', 'sub_path'):
                if (not isinstance(value.get(key), str) or
                        re.fullmatch(r'/[0-9a-f]{48}/', value[key]) is None):
                    raise ValueError(key)
            if not isinstance(value.get('php_service'), str) or re.fullmatch(
                    r'php[0-9]+\.[0-9]+-fpm', value['php_service']) is None:
                raise ValueError('php_service')
            if value.get('xray_bin') not in ('/usr/local/x-ui/bin/xray-linux-amd64',
                                              '/usr/local/x-ui/bin/xray-linux-arm64'):
                raise ValueError('xray_bin')
        except (ValueError, KeyError, TypeError, InstallError):
            return self.answer('UNKNOWN', 'В состоянии установки отсутствуют необходимые параметры.',
                               suggestion=SUGGEST['state'])
        self.state = {key: value.get(key) for key in (
            'domain', 'panel_port', 'sub_port', 'panel_path', 'sub_path',
            'php_service', 'xray_bin', 'ipv6', 'swap')}
        self.mode = mode
        return self.answer('PASS', 'Установка Bob завершена.', {'warpMode': mode})

    def service(self, name, timer=False):
        active_code, active = self.p.command('systemctl', 'is-active', name)
        enabled_code, enabled = self.p.command('systemctl', 'is-enabled', name)
        active, enabled = active.strip(), enabled.strip()
        if active == 'active' and enabled in ('enabled', 'static'):
            return self.answer('PASS', 'Служба работает и включена.')
        if active in ('inactive', 'failed') or enabled in ('disabled', 'masked'):
            return self.answer('FAIL' if not timer else 'WARN',
                               'Служба не работает или не включена.',
                               suggestion=SUGGEST['service'])
        if active_code or enabled_code:
            return self.answer('UNKNOWN', 'Статус службы не удалось определить.',
                               suggestion=SUGGEST['service'])
        return self.answer('WARN', 'Состояние службы требует проверки.',
                           suggestion=SUGGEST['service'])

    def runtime_config(self):
        try:
            if self.p.is_symlink(CONFIG):
                return self.answer('UNKNOWN', 'Runtime-конфигурация Xray является ссылкой.',
                                   suggestion=SUGGEST['config'])
            value = json.loads(self.p.read(CONFIG))
        except FileNotFoundError:
            return self.answer('FAIL', 'Runtime-конфигурация Xray отсутствует.',
                               suggestion=SUGGEST['config'])
        except ValueError:
            return self.answer('FAIL', 'Runtime-конфигурация Xray повреждена.',
                               suggestion=SUGGEST['config'])
        if not isinstance(value, dict):
            return self.answer('FAIL', 'Runtime-конфигурация Xray имеет неверный формат.',
                               suggestion=SUGGEST['config'])
        self.config = value
        return self.answer('PASS', 'Runtime-конфигурация Xray читается.')

    def inbound(self):
        matches = [x for x in self.config.get('inbounds', []) if isinstance(x, dict)
                   and x.get('port') == 443]
        if len(matches) != 1:
            return self.answer('FAIL', 'Ожидается один основной inbound на TCP/443.',
                               suggestion=SUGGEST['config'])
        row = matches[0]
        stream = row.get('streamSettings', {})
        reality = stream.get('realitySettings', {}) if isinstance(stream, dict) else {}
        if (row.get('protocol') != 'vless' or stream.get('network') != 'tcp'
                or stream.get('security') != 'reality'):
            return self.answer('FAIL', 'Основной inbound не соответствует VLESS TCP REALITY.',
                               suggestion=SUGGEST['config'])
        if (reality.get('target') != SOCKET or 'dest' in reality or reality.get('xver') != 1
                or self.state['domain'] not in reality.get('serverNames', [])):
            return self.answer('FAIL', 'Self-SNI target, xver или имя сервера неверны.',
                               suggestion=SUGGEST['config'])
        return self.answer('PASS', 'Основной REALITY inbound и Self-SNI настроены.')

    def listener(self):
        code, output = self.p.command('ss', '-H', '-lntp')
        if code:
            return self.answer('UNKNOWN', 'Список TCP listeners недоступен.', suggestion=SUGGEST['listener'])
        rows = [line for line in output.splitlines() if len(line.split()) >= 5
                and line.split()[3].rsplit(':', 1)[-1] == '443']
        if not rows:
            return self.answer('FAIL', 'TCP/443 не слушается.', suggestion=SUGGEST['listener'])
        expected = self.state.get('xray_bin')
        if not isinstance(expected, str) or not expected.startswith('/usr/local/x-ui/bin/'):
            return self.answer('UNKNOWN', 'Путь Xray для TCP/443 не подтверждён.',
                               suggestion=SUGGEST['listener'])
        for line in rows:
            match = re.search(r'\bpid=(\d+)\b', line)
            if not match:
                return self.answer('UNKNOWN', 'PID владельца TCP/443 недоступен.',
                                   suggestion=SUGGEST['listener'])
            try:
                if self.p.process_exe(int(match.group(1))) != Path(expected).resolve():
                    return self.answer('FAIL', 'TCP/443 слушает не установленный Xray.',
                                       suggestion=SUGGEST['listener'])
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                return self.answer('UNKNOWN', 'Владелец TCP/443 сменился во время проверки.',
                                   suggestion=SUGGEST['listener'])
        return self.answer('PASS', 'TCP/443 слушается Xray.')

    def unix_socket(self):
        try:
            mode = self.p.stat(SOCKET).st_mode
        except FileNotFoundError:
            return self.answer('FAIL', 'Unix socket nginx отсутствует.', suggestion=SUGGEST['socket'])
        if not stat.S_ISSOCK(mode):
            return self.answer('FAIL', 'Путь fallback не является Unix socket.',
                               suggestion=SUGGEST['socket'])
        code, output = self.p.command('ss', '-H', '-xl')
        if code:
            return self.answer('UNKNOWN', 'Unix listeners не удалось проверить.',
                               suggestion=SUGGEST['socket'])
        if not any(SOCKET in line and 'LISTEN' in line for line in output.splitlines()):
            return self.answer('FAIL', 'Unix socket существует, но не слушается.',
                               suggestion=SUGGEST['socket'])
        return self.answer('PASS', 'Unix socket fallback слушается.')

    def certificate(self):
        path = '/etc/letsencrypt/live/' + self.state['domain'] + '/fullchain.pem'
        try:
            self.p.stat(path)
        except FileNotFoundError:
            return self.answer('FAIL', 'Сертификат Let’s Encrypt отсутствует.',
                               suggestion=SUGGEST['cert'])
        except PermissionError:
            return self.answer('UNKNOWN', 'Сертификат недоступен с текущими правами.',
                               suggestion=SUGGEST['cert'])
        try:
            expiry, matches, fingerprint = self.p.certificate(path, self.state['domain'])
        except ValueError:
            return self.answer('FAIL', 'Сертификат Let’s Encrypt повреждён.',
                               suggestion=SUGGEST['cert'])
        self.cert_fingerprint = fingerprint
        days = int((expiry - self.p.now()).total_seconds() // 86400)
        if not matches:
            return self.answer('FAIL', 'Сертификат не соответствует FQDN.',
                               suggestion=SUGGEST['cert'])
        if days < 0:
            return self.answer('FAIL', 'Сертификат истёк.', {'daysRemaining': days}, SUGGEST['cert'])
        if days < 30:
            return self.answer('WARN', 'Срок сертификата скоро истекает.',
                               {'daysRemaining': days}, SUGGEST['cert'])
        return self.answer('PASS', 'Сертификат действителен.', {'daysRemaining': days})

    def tls_listeners(self):
        ports = (443, int(self.state['panel_port']), int(self.state['sub_port']))
        fingerprints = []
        try:
            for port in ports:
                fingerprints.append(self.p.tls(self.state['domain'], port))
        except (ssl.SSLCertVerificationError, ssl.CertificateError):
            return self.answer('FAIL', 'TLS listener не прошёл проверку сертификата/FQDN.',
                               suggestion=SUGGEST['cert'])
        except (socket.timeout, TimeoutError, ConnectionError, OSError):
            return self.answer('UNKNOWN', 'Не удалось проверить один из локальных TLS listeners.',
                               suggestion=SUGGEST['cert'])
        if self.cert_fingerprint is None:
            return self.answer('UNKNOWN', 'Эталонный сертификат недоступен.', suggestion=SUGGEST['cert'])
        if any(x != self.cert_fingerprint for x in fingerprints):
            return self.answer('FAIL', 'TLS listener использует устаревший или другой сертификат.',
                               suggestion=SUGGEST['cert'])
        return self.answer('PASS', 'Все локальные TLS listeners используют текущий сертификат.')

    def renew_hook(self):
        path = '/etc/letsencrypt/renewal-hooks/deploy/50-selfsni'
        try:
            script = self.p.read(path)
            info = self.p.stat(path)
        except FileNotFoundError:
            return self.answer('WARN', 'Deploy hook Certbot отсутствует.', suggestion=SUGGEST['cert'])
        expected = ('#!/bin/sh\nset -eu\nnginx -t\nsystemctl reload nginx\n'
                    'systemctl restart x-ui\n')
        if (not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_IXUSR
                or info.st_uid != 0 or script != expected):
            return self.answer('WARN', 'Deploy hook Certbot не соответствует ожидаемому.',
                               suggestion=SUGGEST['cert'])
        return self.answer('PASS', 'Deploy hook Certbot присутствует.')

    def routing(self):
        outbounds = self.config.get('outbounds', [])
        if not isinstance(outbounds, list) or not outbounds or any(
                not isinstance(item, dict) for item in outbounds):
            return self.answer('FAIL', 'Runtime outbounds имеют неверный формат.',
                               suggestion=SUGGEST['routing'])
        tags = [x.get('tag') for x in outbounds]
        if any(not isinstance(tag, str) for tag in tags):
            return self.answer('FAIL', 'Runtime outbound tags имеют неверный формат.',
                               suggestion=SUGGEST['routing'])
        if (not outbounds or outbounds[0].get('tag') != 'direct'
                or outbounds[0].get('protocol') != 'freedom'
                or len(tags) != len(set(tags)) or tags.count('blocked') != 1):
            return self.answer('FAIL', 'Критический порядок или состав outbounds нарушен.',
                               suggestion=SUGGEST['routing'])
        blocked = next(x for x in outbounds if x.get('tag') == 'blocked')
        if blocked.get('protocol') != 'blackhole':
            return self.answer('FAIL', 'Outbound blocked не блокирует трафик.',
                               suggestion=SUGGEST['routing'])
        warp = next((x for x in outbounds if x.get('tag') == 'warp-outbound'), None)
        if self.mode == 'skip' and warp:
            return self.answer('FAIL', 'WARP outbound присутствует в режиме skip.',
                               suggestion=SUGGEST['routing'])
        warp_settings = warp.get('settings') if isinstance(warp, dict) else None
        servers = warp_settings.get('servers') if isinstance(warp_settings, dict) else None
        if self.mode != 'skip' and (not warp or warp.get('protocol') != 'socks' or
                not isinstance(servers, list) or len(servers) != 1 or
                not isinstance(servers[0], dict) or
                servers[0].get('address') != '127.0.0.1' or
                servers[0].get('port') != 40000 or servers[0].get('users', []) != []):
            return self.answer('FAIL', 'WARP outbound не указывает на loopback SOCKS5.',
                               suggestion=SUGGEST['routing'])
        route = self.config.get('routing', {})
        if not isinstance(route, dict):
            return self.answer('FAIL', 'Runtime routing имеет неверный формат.',
                               suggestion=SUGGEST['routing'])
        rules = route.get('rules', [])
        if not isinstance(rules, list) or any(not isinstance(item, dict) for item in rules):
            return self.answer('FAIL', 'Runtime routing rules имеют неверный формат.',
                               suggestion=SUGGEST['routing'])
        if route.get('domainStrategy') != 'IPOnDemand':
            return self.answer('FAIL', 'Routing domainStrategy не соответствует защитному правилу.',
                               suggestion=SUGGEST['routing'])
        expected = [('blocked', 'ip', BLOCKED_IPS),
                    ('blocked', 'protocol', ['bittorrent']),
                    ('blocked', 'domain', BLOCKED_DOMAINS)]
        if self.mode != 'skip':
            expected.append(('warp-outbound', 'domain', WARP_DOMAINS))
        if not rules or rules[0].get('outboundTag') != 'api' or rules[0].get('inboundTag') not in ('api', ['api']):
            return self.answer('FAIL', 'Служебное правило API отсутствует в начале маршрутизации.',
                               suggestion=SUGGEST['routing'])
        positions = []
        for tag, field, values in expected:
            positions.append(next((i for i, rule in enumerate(rules)
                if rule.get('outboundTag') == tag and
                isinstance(rule.get(field), list) and
                all(value in rule[field] for value in values)), -1))
        if positions != list(range(1, len(expected) + 1)):
            return self.answer('FAIL', 'Критические правила маршрутизации отсутствуют или переставлены.',
                               suggestion=SUGGEST['routing'])
        if self.mode == 'skip' and any(x.get('outboundTag') == 'warp-outbound' for x in rules):
            return self.answer('FAIL', 'Правило WARP осталось в режиме skip.',
                               suggestion=SUGGEST['routing'])
        return self.answer('PASS', 'Критические routing invariants соблюдены.')

    def geodata(self):
        missing = []
        for name, required in REQUIRED.items():
            path = '/usr/local/x-ui/bin/' + name
            try:
                if not required.issubset(self.p.categories(path)):
                    missing.append(name)
            except PermissionError:
                return self.answer('UNKNOWN', 'GeoData нельзя прочитать с текущими правами.',
                                   suggestion=SUGGEST['geodata'])
            except (FileNotFoundError, ValueError, InstallError):
                missing.append(name)
        if missing:
            return self.answer('FAIL', 'GeoData assets или необходимые категории отсутствуют.',
                               {'affectedFiles': len(missing)}, SUGGEST['geodata'])
        return self.answer('PASS', 'Необходимые категории GeoData доступны.')

    def geodata_schedule(self):
        value = self.config.get('geodata')
        if value != automatic_config():
            return self.answer('WARN', 'Штатное расписание GeoData отличается от ожидаемого.',
                               suggestion=SUGGEST['geodata'])
        code, env = self.p.command('systemctl', 'show', 'x-ui', '--property=Environment', '--value')
        if code:
            return self.answer('UNKNOWN', 'Timezone процесса x-ui не удалось определить.',
                               suggestion=SUGGEST['geodata'])
        if 'TZ=' + GEODATA_TIMEZONE not in env.split() or GEODATA_CRON != '0 0 * * *':
            return self.answer('WARN', 'Timezone процесса или расписание GeoData неверно.',
                               suggestion=SUGGEST['geodata'])
        return self.answer('PASS', 'Штатное обновление GeoData настроено на 03:00 МСК.')

    def geodata_timezone(self):
        binary = self.state.get('xray_bin')
        if not isinstance(binary, str) or not binary.startswith('/usr/local/x-ui/bin/'):
            return self.answer('UNKNOWN', 'Путь процесса Xray не подтверждён.',
                               suggestion=SUGGEST['geodata'])
        value = self.p.xray_timezone(binary)
        if value is None:
            return self.answer('UNKNOWN', 'Процесс Xray не найден для проверки timezone.',
                               suggestion=SUGGEST['geodata'])
        if not value:
            return self.answer('WARN', 'Процесс Xray не унаследовал UTC timezone.',
                               suggestion=SUGGEST['geodata'])
        return self.answer('PASS', 'Процесс Xray использует UTC timezone.')

    def warp_service(self):
        if self.mode == 'skip':
            return self.answer('SKIP', 'WARP намеренно отключён в Bob.')
        return self.service('warp-svc')

    def warp_settings(self):
        if self.mode == 'skip':
            return self.answer('SKIP', 'WARP намеренно отключён в Bob.')
        code, output = self.p.command('warp-cli', '--json', 'status')
        if code:
            return self.answer('UNKNOWN', 'Статус WARP недоступен.', suggestion=SUGGEST['warp'])
        status = json.loads(output)
        code, output = self.p.command('warp-cli', '--json', 'settings')
        if code:
            return self.answer('UNKNOWN', 'Настройки WARP недоступны.', suggestion=SUGGEST['warp'])
        settings = json.loads(output)
        config = settings.get('settings') if isinstance(settings, dict) else None
        if (not isinstance(status, dict) or status.get('status') != 'Connected'
                or not isinstance(config, dict) or config.get('operation_mode') != 'proxy'
                or type(config.get('proxy_port')) is not int or config['proxy_port'] != 40000):
            return self.answer('FAIL', 'WARP не подключён в ожидаемом proxy mode.',
                               suggestion=SUGGEST['warp'])
        return self.answer('PASS', 'WARP подключён в proxy mode.')

    def warp_listener(self):
        if self.mode == 'skip':
            return self.answer('SKIP', 'WARP намеренно отключён в Bob.')
        code, output = self.p.command('ss', '-H', '-lntp')
        if code:
            return self.answer('UNKNOWN', 'SOCKS5 listener WARP недоступен.', suggestion=SUGGEST['warp'])
        rows = [line for line in output.splitlines()
                if len(line.split()) >= 5 and line.split()[3].rsplit(':', 1)[-1] == '40000']
        if not rows or not all(line.split()[3] in ('127.0.0.1:40000', '[::1]:40000') for line in rows):
            return self.answer('FAIL', 'WARP SOCKS5 отсутствует или слушает не только loopback.',
                               suggestion=SUGGEST['warp'])
        code, output = self.p.command('systemctl', 'show', 'warp-svc',
                                      '--property=MainPID', '--value')
        if code or not output.strip().isdigit() or int(output.strip()) == 0:
            return self.answer('UNKNOWN', 'PID службы WARP не удалось определить.',
                               suggestion=SUGGEST['warp'])
        expected_pid = int(output.strip())
        for line in rows:
            match = re.search(r'\bpid=(\d+)\b', line)
            if not match:
                return self.answer('UNKNOWN', 'PID владельца SOCKS5 listener недоступен.',
                                   suggestion=SUGGEST['warp'])
            if int(match.group(1)) != expected_pid:
                return self.answer('FAIL', 'SOCKS5 listener принадлежит другой службе.',
                                   suggestion=SUGGEST['warp'])
        return self.answer('PASS', 'WARP SOCKS5 слушает только loopback.')

    def ufw(self):
        code, output = self.p.command('ufw', 'status')
        if code:
            return self.answer('UNKNOWN', 'Статус UFW недоступен.', suggestion=SUGGEST['security'])
        if not output.startswith('Status: active'):
            return self.answer('WARN', 'UFW не активен.', suggestion=SUGGEST['security'])
        return self.answer('PASS', 'UFW активен.')

    def fail2ban_jails(self):
        for jail in ('sshd', '3x-ipl'):
            code, _ = self.p.command('fail2ban-client', 'status', jail)
            if code:
                return self.answer('WARN', 'Необходимый Fail2ban jail недоступен.',
                                   suggestion=SUGGEST['security'])
        return self.answer('PASS', 'Необходимые Fail2ban jails доступны.')

    def secret_permissions(self):
        paths = (STATE, '/etc/x-ui/x-ui.db', CONFIG, '/etc/x-ui/install-result.env',
                 '/etc/letsencrypt/live/' + self.state['domain'] + '/privkey.pem')
        for path in paths:
            try:
                info = self.p.stat(path)
            except FileNotFoundError:
                return self.answer('WARN', 'Один из root-only файлов отсутствует.',
                                   suggestion=SUGGEST['security'])
            if info.st_uid != 0 or info.st_mode & 0o077:
                return self.answer('FAIL', 'Права root-only файла небезопасны.',
                                   suggestion=SUGGEST['security'])
        return self.answer('PASS', 'Секретные файлы доступны только root.')

    def bbr(self):
        expected = {'net.ipv4.tcp_congestion_control': 'bbr', 'net.core.default_qdisc': 'fq'}
        for key, value in expected.items():
            code, actual = self.p.command('sysctl', '-n', key)
            if code:
                return self.answer('UNKNOWN', 'Runtime sysctl недоступен.', suggestion=SUGGEST['system'])
            if actual.strip() != value:
                return self.answer('WARN', 'BBR/FQ runtime параметры отличаются.',
                                   suggestion=SUGGEST['system'])
        try:
            text = self.p.read('/etc/sysctl.d/99-zz-bob-auto-xray-bbr.conf')
        except FileNotFoundError:
            return self.answer('WARN', 'Persist-конфигурация BBR/FQ отсутствует.',
                               suggestion=SUGGEST['system'])
        if not all(re.search(r'(?m)^\s*' + re.escape(k) + r'\s*=\s*' + v + r'\s*$', text)
                   for k, v in expected.items()):
            return self.answer('WARN', 'Persist-конфигурация BBR/FQ отличается.',
                               suggestion=SUGGEST['system'])
        if self.p.bbr_conflicts():
            return self.answer('WARN', 'Обнаружен конфликтующий persistent sysctl для BBR/FQ.',
                               suggestion=SUGGEST['system'])
        return self.answer('PASS', 'BBR/FQ работают и сохранены в sysctl.')

    def ipv6_check(self):
        report = self.state.get('ipv6')
        if report is None:
            try:
                self.p.stat(ipv6.CONFIG)
            except FileNotFoundError:
                return self.answer('SKIP', 'Эта установка Bob не управляла IPv6.')
            return self.answer('UNKNOWN', 'IPv6-файл существует без подтверждения в state.',
                               suggestion=SUGGEST['ipv6'])
        if not isinstance(report, dict) or not isinstance(report.get('networkDropin'), str):
            return self.answer('UNKNOWN', 'Состояние IPv6 неполно.', suggestion=SUGGEST['ipv6'])
        try:
            if (self.p.is_symlink(ipv6.CONFIG) or self.p.is_symlink(ipv6.CONFIG.parent)
                    or self.p.read(ipv6.CONFIG) != ipv6.CONTENT):
                return self.answer('FAIL', 'Управляемый IPv6 sysctl изменён.',
                                   suggestion=SUGGEST['ipv6'])
        except FileNotFoundError:
            return self.answer('FAIL', 'Управляемый IPv6 sysctl отсутствует.',
                               suggestion=SUGGEST['ipv6'])
        path, values, addresses = self.p.ipv6_snapshot()
        if str(path) != report['networkDropin']:
            return self.answer('WARN', 'Активный IPv4-интерфейс не совпал с IPv6 state.',
                               suggestion=SUGGEST['ipv6'])
        if (self.p.is_symlink(path) or self.p.is_symlink(path.parent)
                or self.p.is_symlink(path.parent.parent)):
            return self.answer('FAIL', 'Networkd drop-in заменён ссылкой.',
                               suggestion=SUGGEST['ipv6'])
        try:
            if self.p.read(path) != ipv6.NETWORK_CONTENT:
                return self.answer('FAIL', 'Управляемый networkd drop-in изменён.',
                                   suggestion=SUGGEST['ipv6'])
        except FileNotFoundError:
            return self.answer('FAIL', 'Управляемый networkd drop-in отсутствует.',
                               suggestion=SUGGEST['ipv6'])
        if self.p.ipv6_conflicts():
            return self.answer('WARN', 'Найден конфликтующий persistent IPv6 sysctl.',
                               suggestion=SUGGEST['ipv6'])
        if (len(values) < 3 or not isinstance(addresses, list) or
                any(not isinstance(row, dict) for row in addresses)):
            return self.answer('UNKNOWN', 'Список IPv6-интерфейсов неполон.',
                               suggestion=SUGGEST['ipv6'])
        if any(value != '1' for value in values.values()) or any(
                row.get('addr_info') for row in addresses):
            return self.answer('FAIL', 'IPv6 включён на интерфейсе или имеет global address.',
                               suggestion=SUGGEST['ipv6'])
        return self.answer('PASS', 'IPv6 отключён на текущих интерфейсах и закреплён.')

    def swap_check(self):
        report = self.state.get('swap')
        if self.p.is_symlink(swap.FSTAB):
            return self.answer('UNKNOWN', '/etc/fstab является ссылкой.',
                               suggestion=SUGGEST['swap'])
        fstab = self.p.read(swap.FSTAB).splitlines()
        owned_entries = [line for line in fstab if line.split()[:1] == [str(swap.FILE)]]
        foreign_entries = [line for line in fstab if not line.lstrip().startswith('#')
                           and len(line.split()) >= 3 and line.split()[2] == 'swap'
                           and line.split()[0] != str(swap.FILE)]
        code, output = self.p.command('swapon', '--show=NAME', '--noheadings', '--raw')
        if code:
            return self.answer('UNKNOWN', 'Активный swap нельзя определить.',
                               suggestion=SUGGEST['swap'])
        active_paths = set(output.splitlines())
        active = str(swap.FILE) in active_paths
        foreign = bool(foreign_entries or active_paths - {str(swap.FILE)})
        entries = fstab.count(swap.LINE)
        try:
            info = self.p.stat(swap.FILE)
        except FileNotFoundError:
            info = None
        if report is None:
            if info is None and not active and not owned_entries and not foreign:
                return self.answer('SKIP', 'Старая установка не управляла SWAP.')
            return self.answer('UNKNOWN', 'SWAP присутствует без подтверждения в state.',
                               suggestion=SUGGEST['swap'])
        if not isinstance(report, dict) or report.get('status') not in ('created', 'existing', 'skipped'):
            return self.answer('UNKNOWN', 'Состояние SWAP неполно.', suggestion=SUGGEST['swap'])
        if report['status'] == 'skipped':
            if info is None and not active and not owned_entries and not foreign:
                return self.answer('SKIP', 'Создание SWAP штатно пропущено.')
            return self.answer('UNKNOWN', 'После пропуска SWAP обнаружен /swapfile.',
                               suggestion=SUGGEST['swap'])
        if report.get('path') != str(swap.FILE):
            return self.answer('UNKNOWN', 'Путь управляемого SWAP не подтверждён.',
                               suggestion=SUGGEST['swap'])
        if (info is None or self.p.is_symlink(swap.FILE) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0 or info.st_size != swap.SIZE or info.st_mode & 0o777 != 0o600
                or entries != 1 or len(owned_entries) != 1 or not active):
            return self.answer('FAIL', 'Управляемый SWAP отсутствует или повреждён.',
                               suggestion=SUGGEST['swap'])
        if foreign:
            return self.answer('WARN', 'Помимо управляемого SWAP обнаружен сторонний.',
                               suggestion=SUGGEST['swap'])
        return self.answer('PASS', 'Управляемый SWAP активен и закреплён.')

    def disk(self):
        free = self.p.disk_free()
        gib = round(free / 1024**3, 1)
        if free < 1024**3:
            return self.answer('WARN', 'Свободного места меньше 1 GiB.',
                               {'freeGiB': gib}, SUGGEST['system'])
        return self.answer('PASS', 'Свободного места достаточно.', {'freeGiB': gib})

    def deep_get(self, port, path, label, expected):
        try:
            status = self.p.get(self.state['domain'], int(port), path)
        except (socket.timeout, TimeoutError, ConnectionError, OSError):
            return self.answer('UNKNOWN', label + ': ответ не получен.', suggestion=SUGGEST['deep'])
        if status in expected:
            return self.answer('PASS', label + ': endpoint ответил.', {'httpStatus': status})
        return self.answer('FAIL', label + ': неожиданный HTTP-статус.',
                           {'httpStatus': status}, SUGGEST['deep'])

    def trace(self, via_warp):
        args = ['curl', '-q', '--noproxy', '' if via_warp else '*', '-fsS']
        if via_warp:
            args += ['--socks5-hostname', '127.0.0.1:40000']
        args += ['--connect-timeout', '5', '--max-time', '12',
                 'https://www.cloudflare.com/cdn-cgi/trace']
        code, output = self.p.command(*args, timeout=15)
        if code:
            return self.answer('UNKNOWN', 'Внешний trace недоступен.', suggestion=SUGGEST['deep'])
        match = re.search(r'(?m)^warp=(on|plus|off)$', output)
        if not match:
            return self.answer('UNKNOWN', 'Внешний trace вернул неопределённый результат.',
                               suggestion=SUGGEST['deep'])
        if via_warp and match.group(1) == 'off':
            return self.answer('WARN', 'SOCKS5 trace не подтвердил WARP.',
                               suggestion=SUGGEST['deep'])
        if not via_warp and match.group(1) != 'off':
            return self.answer('WARN', 'Прямой egress выглядит туннелированным.',
                               suggestion=SUGGEST['deep'])
        return self.answer('PASS', 'Cloudflare trace сообщил ожидаемый WARP-статус.')

    def run(self):
        self.add('core.state', 'state', self.state_check)
        if self.state is None:
            overall = 'UNKNOWN'
        else:
            self.add('core.xui', 'service', lambda: self.service('x-ui'))
            self.add('core.nginx', 'service', lambda: self.service('nginx'))
            self.add('core.php', 'service', lambda: self.service(self.state.get('php_service', 'php-fpm')))
            self.add('core.config', 'config', self.runtime_config)
            if self.config is not None:
                self.add('core.inbound', 'config', self.inbound)
                self.add('routing.invariants', 'routing', self.routing)
                self.add('geodata.schedule', 'geodata', self.geodata_schedule)
            self.add('core.port443', 'listener', self.listener)
            self.add('core.socket', 'socket', self.unix_socket)
            self.add('tls.certificate', 'cert', self.certificate)
            self.add('tls.listeners', 'cert', self.tls_listeners)
            self.add('tls.timer', 'service', lambda: self.service('certbot.timer', timer=True))
            self.add('tls.hook', 'cert', self.renew_hook)
            self.add('warp.service', 'warp', self.warp_service)
            self.add('warp.settings', 'warp', self.warp_settings)
            self.add('warp.listener', 'warp', self.warp_listener)
            self.add('geodata.categories', 'geodata', self.geodata)
            self.add('geodata.timezone', 'geodata', self.geodata_timezone)
            self.add('security.ufw', 'security', self.ufw)
            self.add('security.fail2ban', 'service', lambda: self.service('fail2ban'))
            self.add('security.jails', 'security', self.fail2ban_jails)
            self.add('security.permissions', 'security', self.secret_permissions)
            self.add('system.bbr', 'system', self.bbr)
            self.add('system.ipv6', 'ipv6', self.ipv6_check)
            self.add('system.swap', 'swap', self.swap_check)
            self.add('system.disk', 'system', self.disk)
            if self.deep:
                self.add('deep.fallback', 'deep', lambda: self.deep_get(443, '/', 'HTTPS fallback', (200,)))
                self.add('deep.panel', 'deep', lambda: self.deep_get(
                    self.state['panel_port'], self.state['panel_path'], 'Панель', (200,)))
                self.add('deep.subscription', 'deep', lambda: self.deep_get(
                    self.state['sub_port'], self.state['sub_path'] + 'missing',
                    'Подписки', (400, 404)))
                if self.mode == 'skip':
                    self.checks.append(_result('deep.warp', 'warp', 'SKIP', 'WARP намеренно отключён в Bob.'))
                else:
                    self.add('deep.warp', 'deep', lambda: self.trace(True))
                self.add('deep.egress', 'deep', lambda: self.trace(False))
            statuses = {x['status'] for x in self.checks}
            overall = ('UNHEALTHY' if 'FAIL' in statuses else
                       'UNKNOWN' if 'UNKNOWN' in statuses else
                       'DEGRADED' if 'WARN' in statuses else 'HEALTHY')
        counts = {status: sum(x['status'] == status for x in self.checks)
                  for status in ('PASS', 'WARN', 'FAIL', 'SKIP', 'UNKNOWN')}
        return {'schemaVersion': SCHEMA_VERSION,
                'scope': 'on-host; external ISP reachability not measured',
                'overallStatus': overall, 'summary': counts,
                'checks': self.checks,
                'timestamp': self.p.now().isoformat().replace('+00:00', 'Z')}


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('invalid arguments')


def _failure_report():
    return {'schemaVersion': SCHEMA_VERSION,
            'scope': 'on-host; external ISP reachability not measured',
            'overallStatus': 'UNKNOWN', 'summary': {'PASS': 0, 'WARN': 0,
                'FAIL': 0, 'SKIP': 0, 'UNKNOWN': 1},
            'checks': [_result('doctor.internal', 'doctor', 'UNKNOWN',
                               'Диагностика не смогла завершиться.')],
            'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


def main(argv=None):
    parser = SafeArgumentParser(prog='bob doctor',
        description='Безопасная диагностика установленного Bob Auto Xray без автоматического исправления.')
    parser.add_argument('--deep', action='store_true',
                        help='добавить HTTP GET и внешние trace с ограниченным временем')
    parser.add_argument('--json', action='store_true', help='вывести один JSON-объект')
    argv = sys.argv[1:] if argv is None else argv
    json_mode = '--json' in argv
    try:
        args = parser.parse_args(argv)
        report = Doctor(deep=args.deep).run()
    except Exception:
        report = _failure_report()
    if json_mode:
        print(json.dumps(report, ensure_ascii=False, separators=(',', ':')))
    else:
        print('Bob Doctor: ' + report['overallStatus'])
        for check in report['checks']:
            print(f"{check['status']:7} {check['id']}: {check['summary']}")
            if check['suggestion'] and check['status'] not in ('PASS', 'SKIP'):
                print('        ' + check['suggestion'])
        print('Локальная проверка не подтверждает доступность VPN из сети конкретного провайдера.')
    return {'HEALTHY': 0, 'DEGRADED': 1, 'UNHEALTHY': 2, 'UNKNOWN': 3}[report['overallStatus']]


if __name__ == '__main__':
    sys.exit(main())
