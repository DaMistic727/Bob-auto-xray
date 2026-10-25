"""Human-readable ACME diagnostics; original command output stays in the private log."""
import datetime as dt
import re
from zoneinfo import ZoneInfo


def explain(output):
    lower = output.lower()
    if 'too many certificates' in lower or 'rate limit' in lower or 'ratelimited' in lower:
        message = 'Let’s Encrypt временно ограничил выпуск сертификатов для этого домена. Превышен лимит повторных сертификатов.'
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
