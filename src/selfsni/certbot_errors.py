"""Human-readable ACME diagnostics; original command output stays in the private log."""
import datetime as dt
import re
from zoneinfo import ZoneInfo


# Let's Encrypt answers overload with urn:ietf:params:acme:error:rateLimited and
# "Service busy; retry later" (load shedding, frequent on staging). It is not a
# limit of this domain or account and passes with a later attempt.
_BUSY = re.compile(r'service busy', re.I)


def explain(output):
    lower = output.lower()
    if _BUSY.search(output):
        return ('Серверы Let’s Encrypt временно перегружены (Service busy). Это не лимит вашего '
                'домена: повторите попытку позже.')
    if 'too many' in lower or 'rate limit' in lower or 'ratelimited' in lower:
        if 'too many certificates' in lower:
            message = ('Let’s Encrypt временно ограничил выпуск сертификатов для этого домена. '
                       'Превышен лимит повторных сертификатов.')
        elif 'too many failed authorizations' in lower:
            message = ('Let’s Encrypt временно ограничил проверки домена: слишком много неудачных '
                       'попыток за последний час. Устраните причину (DNS A-запись, порт 80) и повторите позже.')
        else:
            message = 'Let’s Encrypt временно ограничил частоту запросов с этого сервера или аккаунта.'
        match = re.search(r'retry after\s+(\d{4}-\d\d-\d\d[ t]\d\d:\d\d:\d\d(?:\.\d+)?\s*(?:utc|z|[+-]\d\d:?\d\d))', output, re.I)
        if match:
            try:
                raw = match.group(1).upper().replace(' UTC', '+00:00').replace('Z', '+00:00')
                instant = dt.datetime.fromisoformat(raw)
                if instant.tzinfo:
                    local = instant.astimezone(ZoneInfo('Europe/Moscow'))
                    message += '\nПовторить попытку можно после: ' + local.strftime('%d.%m.%Y, %H:%M:%S МСК')
            except ValueError:
                pass
        return message
    if 'nxdomain' in lower:
        return 'Домен не найден в DNS. Проверьте A-запись домена.'
    if 'caa' in lower:
        return 'DNS-запись CAA запрещает выпуск сертификата Let’s Encrypt. Проверьте настройки CAA.'
    if any(x in lower for x in ('connection refused', 'connection timed out', 'timeout during connect', 'port 80')):
        return 'Порт 80 недоступен извне. Проверьте firewall и правила провайдера VPS.'
    if any(x in lower for x in ('unauthorized', 'invalid response', 'challenge failed')):
        return 'Не удалось подтвердить владение доменом через HTTP. Проверьте DNS A-запись, проксирование и доступность порта 80.'
    if any(x in lower for x in ('dns problem', 'no valid a records', 'incorrect ip address')):
        return 'Домен не указывает на этот сервер. Проверьте DNS A-запись и отключите проксирование.'
    if any(x in lower for x in ('network is unreachable', 'temporary failure', 'connection reset')):
        return 'Сетевая ошибка при обращении к Let’s Encrypt. Проверьте подключение и повторите позже.'
    return 'Let’s Encrypt не выдал сертификат. Подробная причина сохранена в журнале установки.'


# `certbot renew --dry-run` failures, most specific first. Order matters:
# an ACME validation detail ("Timeout during connect") or a deploy-hook error
# must never be mistaken for a client-side network failure worth retrying.
# certbot 2.9 (Ubuntu 24.04) and 4.0 (26.04), display.ops.report_executed_command:
# a non-zero hook exit is only logged ("Hook 'deploy-hook' reported error code 1")
# and certbot still exits 0. Hook stderr alone is not a failure: `nginx -t`
# reports success on stderr.
_HOOK = re.compile(r"Hook 'deploy-hook' reported error code", re.I)
_RATE_LIMIT = re.compile(r'too many|rate.?limit', re.I)
_VALIDATION = re.compile(
    r'urn:ietf:params:acme:error:(?:unauthorized|connection|dns|caa|incorrectresponse|'
    r'rejectedidentifier)|failed to authenticate|reported these problems|challenge failed|'
    r'some challenges have failed|invalid response from|timeout during connect|acme-challenge|'
    r'dns problem|no valid a records|nxdomain|incorrect ip address', re.I)
_TLS = re.compile(r'certificate.?verify.?failed|hostname .{0,80}doesn.t match|wrong version number', re.I)
_TRANSIENT = re.compile(
    r'connection reset|connection aborted|remotedisconnected|connection refused|'
    r'max retries exceeded|failed to establish a new connection|connecttimeout|readtimeout|'
    r'read timed out|timed out|temporary failure in name resolution|name or service not known|'
    r'network is unreachable|no route to host|eof occurred in violation of protocol|unexpected.eof|'
    r'urn:ietf:params:acme:error:(?:serverinternal|badnonce)|\b5\d\d server error|'
    r'service unavailable|bad gateway|gateway time-?out', re.I)


def deploy_hook_result(output, log, hook):
    """ran | failed | skipped | not-run for one dry run that exited 0.

    `log` is the part of letsencrypt.log written by that run: certbot logs each
    hook start ("Running deploy-hook command: <cmd>") at INFO, which reaches only
    the log file. `ran` requires exactly one start, of exactly `hook`.
    """
    text = (output or '') + '\n' + (log or '')
    if _HOOK.search(text):
        return 'failed'
    if re.search(r'skipping deploy hook', text, re.I):
        return 'skipped'
    started = [x.strip() for x in re.findall(r'Running deploy-hook command: ([^\n]*)', log or '')]
    return 'ran' if started == [hook] else 'not-run'


def classify_renewal(output, timed_out=False):
    """hook | rate-limit | validation | tls | transient | other. Only transient is retried."""
    for kind, pattern in (('hook', _HOOK), ('transient', _BUSY), ('rate-limit', _RATE_LIMIT),
                          ('validation', _VALIDATION),
                          ('tls', _TLS), ('transient', _TRANSIENT)):
        if pattern.search(output or ''):
            return kind
    return 'transient' if timed_out else 'other'


_REQUEST = re.compile(r'Sending (GET|HEAD|POST) request to https://([A-Za-z0-9.-]+)(/[^\s]*?)?\.?(?:\s|$)')


def acme_step(log_text):
    """Last ACME request as 'METHOD host step', without account/order identifiers."""
    matches = _REQUEST.findall(log_text or '')
    if not matches:
        return None
    method, host, path = matches[-1]
    parts = [x for x in (path or '/').split('/') if x]
    step = (parts[1] if parts[:1] == ['acme'] and len(parts) > 1 else parts[0] if parts else 'directory')
    return f'{method} {host} {step if re.fullmatch(r"[a-z0-9-]{1,32}", step) else "other"}'
