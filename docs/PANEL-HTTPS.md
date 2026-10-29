# Панель через Self-SNI HTTPS/443

Для новых установок публичный URL — `https://<domain>/<48-hex-secret>/`.
`secrets.token_hex(24)` по-прежнему генерирует 192 бита энтропии. `panel_port`
в state — случайный **внутренний** порт HTTPS 3x-ui, а не 443. Маркер
`panel_access: "https-443"` отличает новый контракт от прежнего state schema 2.

```
Internet TCP/443 → Xray REALITY
  ├─ аутентифицированный VLESS Vision → существующая маршрутизация
  └─ обычный TLS + PROXY v1 → /run/selfsni/nginx.sock → Nginx TLS
       ├─ /, decoy auth, CSS/SVG → существующий PHP-FPM portal
       ├─ /<48-hex>/… → HTTPS 127.0.0.1:<panel_port> → 3x-ui
       └─ неизвестные пути → 404
```

Nginx не получает дополнительного TCP listener. REALITY `target` и `xver=1`
не меняются. Подписки Happ/INCY продолжают использовать прежние sub_port,
sub_path и сертификат, независимо от панели. Версии 3x-ui/Xray и зависимости
не изменены.

## Bootstrap и перезапуск

1. Preflight проверяет чистую VPS, ownership, DNS и свободные порты.
2. UFW default deny включается до bootstrap; разрешены SSH/80/443.
3. Certbot получает сертификат; Nginx создаёт существующий Unix socket и proxy
   location. До готовности upstream секретный маршрут может вернуть 502.
4. До запуска официального install.sh записывается systemd drop-in с
   `ConditionPathExists=/var/lib/selfsni/panel-ready`. Маркер отсутствует;
   попытки stock installer запустить службу пропускаются. Plaintext/pubic
   bootstrap listener не появляется.
5. Устанавливается проверенное ядро Xray, штатный CLI 3x-ui сохраняет HTTPS и
   `listenIP=127.0.0.1`. CLI v3.10.2 может вернуть exit 0 при ошибке записи,
   поэтому Bob отдельно читает `-getListen -getCert`, проверяет точные значения
   и загружает certificate/key в SSLContext до создания root-only marker.
6. `ExecStartPre` перед каждым стартом/рестартом повторяет loopback binding
   и проверяет точную строку `listenIP: 127.0.0.1`; silent CLI failure
   не разрешает запуск службы. Команды не вызывают restart и не образуют цикл;
   сохраняется штатный ограниченный Restart=on-failure/StartLimit из vendor unit.
   внутренний LocalTLS подключается к 127.0.0.1, проверяя сертификат по домену.
7. Сохраняются настройки панели, subscriptions и REALITY inbound. Финальный UFW
   разрешает дополнительно **только** подписки. Acceptance проверяет внутренние
   API и публичные запросы через Xray TCP/443.

При ошибке до marker панель не стартует. После marker она остаётся на loopback
и её порт не разрешён firewall. Перезапуск Nginx восстанавливает Unix socket;
перезапуск x-ui ждёт его наличия. Deploy hook Certbot проверяет и reload'ит
Nginx, затем restart'ит x-ui; оба читают актуальные live certificate paths.

## Совместимость 3x-ui v3.10.2

Исследован pinned commit `608ef4e4010bf83e2e78328e08f77efc051ad4d7`:
[router](https://github.com/MHSanaei/3x-ui/blob/608ef4e4010bf83e2e78328e08f77efc051ad4d7/internal/web/web.go),
[login/CSRF](https://github.com/MHSanaei/3x-ui/blob/608ef4e4010bf83e2e78328e08f77efc051ad4d7/internal/web/controller/index.go),
[IP resolution](https://github.com/MHSanaei/3x-ui/blob/608ef4e4010bf83e2e78328e08f77efc051ad4d7/internal/web/controller/util.go),
[WebSocket](https://github.com/MHSanaei/3x-ui/blob/608ef4e4010bf83e2e78328e08f77efc051ad4d7/internal/web/controller/websocket.go).

Assets, API, login, logout, CSRF и `/ws` зарегистрированы под webBasePath.
SPA формирует URL с этим же префиксом. Session cookie имеет Path=webBasePath,
HttpOnly, SameSite=Lax и Secure при внутреннем TLS. CSRF проверяется штатным
middleware; Bearer API Bob сохраняет свой штатный контракт.

`proxy_pass https://127.0.0.1:<port>` задан **без URI suffix**: префикс
не удаляется и не дублируется. Host фиксируется на домене; foreign Host получает
404. SNI — домен, verify включён, доверие — системный CA bundle Ubuntu.
Проверка имени и цепочки обязательна; отключения TLS verification нет.
[Семантика proxy_pass и TLS](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).
Upgrade/Connection передаются по HTTP/1.1, read/send timeout 3600 секунд,
buffering выключен. Нормальные мутации upstream ограничены 10 MiB; Nginx
допускает 64 MiB, включая импорт DB. Большие backups требуют отдельного решения.
Глобальный лимит 8k заглушки сохраняется.

## Границы защиты

PROXY v1 принимается только на Unix socket в root-only каталоге. Nginx
восстанавливает `$remote_addr`, полностью заменяет X-Real-IP/X-Forwarded-For,
фиксирует HTTPS/Host/443 и удаляет Forwarded. 3x-ui доверяет только
`127.0.0.1/32`. Клиентские IP-заголовки не используются как доверенная цепочка.
Компрометация локального root или самого Nginx выходит за эту модель.

POST на точный секретный login endpoint: 6 запросов/мин на реальный IP,
burst 5, HTTP 429. Лимит дополняет stock login limiter и сильные credentials;
общий NAT разделяет бюджет. Публичных исключений `/assets`, `/api`, `/login`
нет. Точный путь без завершающего `/` получает относительный 308; остальные
похожие префиксы возвращают 404. Относительные stock redirects сохраняются,
upstream authority с внутренним портом переписывается на публичный домен.

Access logs HTTP/TLS выключены; error logs этих server направлены в /dev/null,
поскольку штатный Nginx error format содержит URI/Referer. Это осознанно
ограничивает диагностику: используйте `nginx -t`, systemd и обезличенный
acceptance. Панель получает no-store/no-referrer, включая ошибки; её штатные
CSP и security headers сохраняются. Конфигурации с секретным путём — 0600,
state/DB/private keys — root-only, redaction установщика сохранён. Сам URL
будет виден авторизованному браузеру и его истории: не публикуйте его и не
записывайте в стороннюю аналитику. Знание URL не заменяет аутентификацию.

Jail `3x-ipl` читает `[LIMIT_IP]` и отключения старого IP VPN-клиента; это не
login jail. SSH jail и штатная IP-limit интеграция сохранены. При размещении
панели и VPN на 443 бан IP-limit может затронуть доступ к 443: не считать этот
jail отдельной защитой или whitelist административного HTTPS.

Stock 3x-ui использует подписанную cookie без серверной ревокации на logout.
Logout очищает браузерную cookie; ранее скопированная cookie может жить до
истечения срока. Новая схема не меняет этот upstream контракт. Не заявляйте
серверную ревокацию сессии по результату успешного logout.

## Существующие установки

Завершённые и поздние failed установки автоматически не мигрируют. State без
panel_access сохраняет прежний формат публичного URL; внутренних данных и
credentials helper не меняет. Разрешённый ранний APT/WARP/CERTIFICATE resume
идёт до установки панели, сохраняет credentials и получает новую схему.

Безопасный поддерживаемый путь — новая чистая VPS с проверкой DNS/сертификата,
временным тестовым клиентом, подписками и reboot до переключения пользователей.
Старую ноду сохраняйте до проверки. На месте нельзя просто перезапустить новый
installer: preflight его отклонит. Ручная миграция требует отдельного проверенного
механизма: root-only backup state/DB/config/UFW, закрытия старого порта до
изменения binding, proxy с проверкой upstream TLS, проверки всех клиентов и
плана возврата. Готового in-place migration скрипта в этом изменении нет.

## Проверка

На разрешённой одноразовой VPS: `PYTHONPATH=src python3 -m selfsni.acceptance`.
Она проверяет сеть, сохранённый binding, публичный prefix, assets, авторизацию,
неверный пароль, CSRF, cookie, read-only API >8 KiB и настоящий WS upgrade.
Повторяйте после restart x-ui, restart Nginx и reboot. Частые повторы могут
попасть под login rate limit; дождитесь освобождения бюджета.

Для внешнего клиента используйте `tools/external_acceptance.py`, SSH
ControlMaster и совместимый Xray: он проверяет реальный Vision, подписки и
наблюдаемую внешнюю доступность внутреннего порта. HTTP/WS acceptance не заменяет браузерный
E2E. Результаты конкретного кандидата приведены в PANEL-HTTPS-REPORT.md.

## Внешний network probe: доказательства и неопределённость

`probe_panel_port` использует IPv4/внутренний порт для TCP, а домен — для SNI
и обязательной проверки CA/hostname. До любых проб HTTP-путь и credentials
не передаются этому потенциально чужому listener. Публичный leaf certificate
считывается read-only на VPS, его SHA256 передаётся через проверенный SSH.
Успешный TLS сравнивается также с этим fingerprint: локально доверенный
TLS interceptor с другим сертификатом не даёт ложный FAIL.

| Наблюдение из одной внешней точки | Статус |
|---|---|
| Явный TCP connection refused | PASS: наблюдался отказ именно на этом сетевом пути |
| TCP timeout или иная сетевая ошибка | INCONCLUSIVE |
| Connect успешен, TLS failed/timeout | INCONCLUSIVE, включая вероятный TCP interception |
| Сертификат не прошёл CA/hostname verification | INCONCLUSIVE: идентичность listener не установлена |
| TLS прошёл, но fingerprint VPS отсутствует или отличается | INCONCLUSIVE: возможна TLS interception/смена сертификата |
| TLS прошёл с правильным SNI, CA/hostname и fingerprint VPS | FAIL: ожидаемый TLS identity доступен на предназначенном для loopback порту |

PASS ограничен наблюдаемым путём, временем и семейством IPv4. Отказ может
исходить от промежуточного оборудования; он не доказывает глобальную закрытость
или правильный binding на VPS. Проверяйте отдельно read-only `ss -lntp`, PID
3x-ui, UFW active/default deny и отсутствие permissive rules для IPv4/IPv6.
Даже правильные host-side настройки не превращают timeout/failed TLS в PASS.
Смена сертификата между чтением SSH и TLS probe даёт INCONCLUSIVE, а не обход
верификации. Проверенная TLS identity на внутреннем порту — достаточная причина
FAIL; HTTP-аутентификация при этом не пробуется. Компрометация ключа/SSH вне модели.

JSON содержит отдельный `status` каждого check и общий status. Любой FAIL
даёт exit 1, INCONCLUSIVE/NOT TESTED без FAIL — exit 2, только все PASS — exit 0.
Результат порта не прерывает независимые Vision/subscription/routing checks.
Если сам Vision не поднялся, зависимые route tests отмечаются NOT TESTED.
Exception text, URLs, certificate bodies и credential payload в отчёт не копируются.

Перед любым `create` harness генерирует run-id. Root-only fixture хранит owner;
create/cleanup сериализуются file lock, поэтому потерянный SSH response не
позволяет cleanup обогнать ещё работающий create. Cleanup вызывается из finally
при PASS/FAIL/INCONCLUSIVE, ошибке JSON, частичном create и исключениях. На VPS
проверяются owner и id клиента; чужие fixtures/клиенты не удаляются. Cleanup
идемпотентен и не требует нулевого числа посторонних клиентов после теста.
Старый marker без owner автоматически не удаляется.

Гарантируется попытка cleanup и проверка удаления; физически гарантировать
удаление при потере SSH/API невозможно. Тогда общий результат FAIL,
`cleanup=NOT VERIFIED`, root-only marker остаётся для восстановления. На VPS
проверьте owner/client id и повторите `remote_fixture.py cleanup <run-id>`
из packaged src окружения. Не удаляйте marker до подтверждённого удаления клиента.
Не запускайте harness на production или без отдельного разрешения создавать
временного VPN-клиента. В этой ревизии новый VPS/внешний Vision E2E не выполнялся.

## Решение по Nginx diagnostic logging после code review

`log_format` настраивает request access logs; он не редактирует сообщения
`error_log`. У error log совместимых Ubuntu Nginx нет используемого Bob
allowlist-формата полей, который гарантированно убирает URI/Referer из сообщения.
Syslog или изменение severity не удаляет секрет из самого сообщения.
Numeric-only access log технически возможен, но не заменяет причины upstream
ошибок; новая система очистки логов и фоновые обработчики здесь не вводятся.
Сохранены access_log off и error_log /dev/null crit. Следствие — отсутствие
request-level причин 4xx/5xx; доступны nginx -t, состояние systemd, readiness,
безопасные фиксированные коды network probe и отдельная acceptance. Нельзя
считать systemd journal способом получить обычные request errors без риска URI.
Основание: [error_log](https://nginx.org/en/docs/ngx_core_module.html#error_log),
[access log_format](https://nginx.org/en/docs/http/ngx_http_log_module.html#log_format).
