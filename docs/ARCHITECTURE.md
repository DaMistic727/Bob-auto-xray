# Архитектура и происхождение правил

Xray слушает `0.0.0.0:443`, VLESS TCP REALITY. Сервис 3x-ui v3.10.2 управляет Xray; собственных постоянных демонов установщика нет. Клиенты добавляются вручную с Vision. Правила назначения применяются ко всем пользовательским inbound, поэтому добавленные через панель inbound получают ту же защиту. Служебное правило API сохраняется первым. География источника соединения никогда не фильтруется.

REALITY `target=/run/selfsni/nginx.sock`, `xver=1`. Ключ `target` нужен модели веб-редактора 3x-ui v3.10.2; legacy `dest` не записывается рядом. nginx принимает исходный TLS с PROXY v1 (`ssl http2 proxy_protocol`), завершает TLS и передаёт только корень сайта и случайный auth endpoint в PHP-FPM. Публичного второго порта заглушки нет. В Xray 26.10.10 RAW и TCP — совместимые имена транспорта. Проверка обычного HTTPS через TCP/443 одновременно проверяет TLS passthrough, Unix socket и PROXY protocol: nginx без корректного префикса отвергает соединение.

Каталог сокета создаётся tmpfiles и ExecStartPre nginx. Права каталога 0750 root:root; Xray работает в официальном root-сервисе панели. x-ui запускается после nginx и WARP и проверяет наличие сокета. nginx при рестарте пересоздаёт listener; REALITY открывает новое Unix-соединение для каждого fallback.

Certbot использует только HTTP-01 webroot `/var/lib/selfsni-acme`. Панель, nginx и подписки читают одни и те же `live/<FQDN>/{fullchain,privkey}.pem`. Deploy hook проверяет и перезагружает nginx, затем перезапускает x-ui (панель, Xray, подписки). Проверяется `certbot renew --dry-run --run-deploy-hooks`.

## Проверяемые зависимости

`versions.lock.json` фиксирует 3x-ui, upstream scripts, commit VPS-Server-Menu и commit routing. Release assets 3x-ui сверяются с SHA256 GitHub release metadata, записанным в lock при разработке. Оригинальный non-interactive `install.sh` 3x-ui запускается без изменений; временный offline curl adapter обслуживает исключительно заранее скачанные и проверенные файлы. Доступ к неизвестным URL прекращается. Временный HTTP порт панели закрыт UFW; наружу он открывается после настройки HTTPS и сертификата.

WARP adapter повторяет официальный APT-репозиторий, keyring, `registration new`, `mode proxy`, `proxy port 40000`, `connect` из закреплённого `menu_warp.sh`. Это та же установка WARP, которой затем управляет VPS-Server-Menu. Upstream интерактивное меню не автоматизируется через stdin. Новый экземпляр меню копируется из закреплённого архива; автозапуск в shell не добавляется. При `--adopt-warp` регистрация и режим не изменяются: обязательны proxy mode, loopback listener и функциональный trace. Существующий menu допускается только с закреплённым commit и без изменений содержимого tracked files (upstream installer сам меняет execute-биты).

Пакеты Ubuntu и WARP обновляются из подписанных APT-репозиториев; фактически установленные версии сохраняются в state. Это не побитово воспроизводимый snapshot APT. Поддержка 26.04/arm64 условна: требуются официальные пакеты для этой пары ОС/архитектуры; обход через пакеты другой Ubuntu отсутствует.

## DigneZzZ/routing

Единый источник: https://github.com/DigneZzZ/routing/tree/a2241b8d6793ee3e5fb2d0eeb8fe9eac7cd79876 . Автоматического исполнения кода или обновления плавающей ветки нет.

Клиентские `profiles/happ.json` и `profiles/incy.json` взяты из `v2ray/{happ,incy}/default.json`; единственное изменение — фиксация commit в URLs geodata. Сохранены upstream `RouteOrder=block-proxy-direct`, GlobalProxy, DIRECT для private/RU, а также дополнительные DIRECT-категории Apple/Microsoft/игр из upstream. Это lite-профили с proxy по умолчанию и DoH upstream. Их semantically identical JSON передаётся разными deeplink `happ://routing/onadd/<Base64(JSON)>` и `incy://routing/onadd/<Base64(JSON)>`.

В v3.10.2 upstream уже исправил конфликт #6273: для INCY собственный профиль находится в теле raw/Base64 subscription, а Happ Routing header удаляется. Happ получает свой Routing header. Никаких patches upstream и лишнего subscription proxy нет. Тесты проверяют именно эти разные контракты; для INCY отсутствие Routing является требуемым результатом, а не ошибкой. Реальные мобильные приложения проверяются отдельно от HTTP-контракта.

Начальные серверные GeoSite/GeoIP берутся из закреплённого официального архива 3x-ui. Архив содержит основной набор Loyalsoldier и RU-набор runetfreedom; SHA256 каждого `.dat` записан в `versions.lock.json`, а установщик разбирает protobuf и проверяет наличие всех реально используемых категорий до настройки панели. Малые файлы DigneZzZ остаются только в клиентских профилях Happ/INCY.

## Штатное обновление geodata

После установки Xray сам обновляет четыре server assets через native `geodata.assets`: основные `geosite.dat`/`geoip.dat` из Loyalsoldier и RU-файлы из runetfreedom. `geodata.outbound` равен `direct`; отдельные cron jobs, download scripts и systemd timers отсутствуют. Конфигурация хранится в штатном Xray JSON, отображается в Geodata UI 3x-ui и сохраняется обычным `xray/update`.

Xray-core 26.10.10 создаёт `robfig/cron` без `WithLocation`, поэтому cron использует Go `time.Local` процесса. Systemd drop-in устойчиво задаёт `TZ=Etc/UTC` для `x-ui` и дочернего Xray, а cron `0 0 * * *` означает 00:00 UTC = 03:00 Europe/Moscow. Системный timezone сервера не меняется. Drop-in переживает restart и reboot.

Native updater сначала загружает все assets во временные файлы, затем атомарно заменяет цели и вызывает reload registries. Ошибка скачивания не меняет установленные файлы; ошибка reload возвращает прежние файлы. Acceptance проверяет оба пути, hot reload без смены PID и повторно валидирует используемые категории.

Штатные теги Basic UI — `direct` и `blocked`; отдельный дублирующий `block` не создаётся. Пользовательские outbounds имеют ровно такой порядок: `direct`, `blocked`, `warp-outbound`. Правила:

1. Штатное служебное правило `api`.
2. `blocked`: `geoip:private`, `ext:geoip_RU.dat:ru`, `geoip:ua`.
3. `blocked`: протокол `bittorrent`.
4. `blocked`: `ext:geosite_RU.dat:category-ads-all`, `ext:geosite_RU.dat:ru-available-only-inside`, суффиксы `.ru`, `.su`, `.xn--p1ai`.
5. `warp-outbound`: `geosite:category-ai-!cn`, `geosite:category-ip-geo-detect`.

Терминальное правило DIRECT отсутствует: Xray использует первый outbound (`direct`) для трафика, который не совпал с правилами. Такой вид совпадает с Basic UI, переживает сохранение через API без появления дублей и оставляет служебный inbound панели работоспособным.

`IPOnDemand` принципиален: Xray разрешает домен тогда, когда раннему IP-правилу нужен адрес. Поэтому иностранное имя, которое разрешается в российский IP, блокируется до более позднего GeoSite WARP. `IPIfNonMatch` может пропустить раннюю IP-проверку, если позднее совпадёт доменное правило. DNS Xray использует DoH через системный путь; серверные блокировки не касаются Certbot, SSH, системного DNS, WARP daemon или панели. При недоступности WARP соответствующие назначения не переключаются молча на DIRECT.

REALITY хранится с `show: true` и Firefox uTLS fingerprint. Sniffing включает `http`, `tls`, `quic`, `fakedns`; итоговый `routeOnly: true` использует sniffed имя для выбора маршрута и сохраняет запрошенный адрес назначения. Контрольный SOCKS5+TLS тест с раздельными IP и SNI подтвердил различие режимов: при `false` Xray переписывает dial target, при `true` сохраняет исходный IP. Firefox также фиксируется в выдаваемых VLESS URI и во временных E2E-клиентах.

## Защита и данные портала

CSPRNG меняет реально используемые названия, цвета, SVG-геометрию, CSS classes, IDs, пути, cookie и поля формы. На GET создаётся CSRF token в PHP session; cookie Secure/HttpOnly/SameSite=Strict. POST проверяет token, удаляет массив POST, меняет session ID и после ограниченной случайной задержки отвечает 303. В session сохраняются только csrf/error. Логины и пароли не сохраняются и не пересылаются; access log портала выключен. Nginx ограничивает размер тела и скорость auth-запросов. Ответы чувствительных страниц: no-store, CSP, no-referrer.

UFW сохраняет все обнаруженные SSH-порты и открывает только SSH/80/443/панель/подписки. Существующие правила разрешены только для обнаруженных SSH-портов; неизвестные правила вызывают отказ. Fail2ban настраивается через официальный `x-ui setup-fail2ban` (3x-ipl) и дополнительный override штатного sshd jail. Панель также имеет штатное ограничение попыток входа upstream, сильный пароль, TLS и CSRF. Credentials и private keys хранятся в файлах 0600 внутри root-only state directory; вывод команд с секретами не логируется.

При сбое установка сообщает FAILED и не выводит credentials как успешный результат. Чужие конфигурации не удаляются. Автоматического отката пакетов/сертификата нет: он мог бы удалить полезную диагностику или разорвать SSH. Повторная установка на частично настроенный хост по умолчанию запрещена.

## Сопоставление с пользовательским 3xui_guide.txt

Предоставленный позднее гайд подтверждает выбранную основу: nginx `ssl http2 proxy_protocol` на Unix socket, REALITY `xVer=1`, `set_real_ip_from unix:`, `real_ip_header proxy_protocol`, HTTP redirect и SOCKS outbound `127.0.0.1:40000` с тегом `warp-outbound`. Все эти механизмы сохранены и проверены функционально.

Путь сокета изменён с примера `/dev/shm/nginx.sock` на `/run/selfsni/nginx.sock`, чтобы закрыть каталог правами и явно восстановить его через systemd. Это по-прежнему Unix-socket Self-SNI, а не TCP reverse proxy. Certbot standalone из старого гайда заменён webroot в соответствии с основным ТЗ; остановка nginx для продления не требуется. Тестовый домен берётся из текущего ввода пользователя, домен из примера гайда не используется. Удаление чужого `/var/www/html`, ручные nano-правки и запуск установщика с плавающей ветки из гайда не переносятся в provisioning.
