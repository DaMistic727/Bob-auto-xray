# Архитектура и происхождение правил

Xray-core v26.10.21 слушает `0.0.0.0:443`, VLESS TCP REALITY. Сервис 3x-ui v3.10.2 управляет Xray; установщик проверяет официальный ZIP и бинарный SHA256 перед заменой встроенного ядра панели. Собственных постоянных демонов установщика нет. Клиенты добавляются вручную с Vision. Правила назначения применяются ко всем пользовательским inbound, поэтому добавленные через панель inbound получают ту же защиту. Служебное правило API сохраняется первым. География источника соединения никогда не фильтруется.

REALITY `target=/run/selfsni/nginx.sock`, `xver=1`. Ключ `target` нужен модели веб-редактора 3x-ui v3.10.2; legacy `dest` не записывается рядом. nginx принимает исходный TLS с PROXY v1 (`ssl http2 proxy_protocol`), завершает TLS и передаёт корень сайта и случайный auth endpoint в PHP-FPM, а секретный 48-hex префикс — в HTTPS listener панели на 127.0.0.1. URI передаётся целиком без переписывания webBasePath. Публичного второго порта заглушки нет. В закреплённом Xray 26.10.21 текущая конфигурация RAW проходит `run -test` и реальное подключение REALITY/Vision. Проверка обычного HTTPS через TCP/443 одновременно проверяет TLS passthrough, Unix socket и PROXY protocol: nginx без корректного префикса отвергает соединение.

Каталог сокета создаётся tmpfiles и ExecStartPre nginx. Права каталога 0750 root:root; Xray работает в официальном root-сервисе панели. x-ui запускается после nginx и проверяет наличие сокета; зависимость от `warp-svc` добавляется только в режимах install/adopt. nginx при рестарте пересоздаёт listener; REALITY открывает новое Unix-соединение для каждого fallback.

Certbot использует только HTTP-01 webroot `/var/lib/selfsni-acme`. Панель, nginx и подписки читают одни и те же `live/<FQDN>/{fullchain,privkey}.pem`. Deploy hook проверяет и перезагружает nginx, затем перезапускает x-ui (панель, Xray, подписки). Проверяется `certbot renew --dry-run --run-deploy-hooks`.

## TCP BBR и FQ

В том же этапе установщик отключает IPv6 через отдельный модуль и файл `/etc/sysctl.d/99-zz-bob-auto-xray-ipv6.conf` с `net.ipv6.conf.all.disable_ipv6 = 1`, `net.ipv6.conf.default.disable_ipv6 = 1` и `net.ipv6.conf.*.disable_ipv6 = 1`. Последний параметр нужен для интерфейсов, которые появляются после раннего запуска `systemd-sysctl`: systemd применяет glob к каждому новому интерфейсу. Установщик также применяет параметр к уже существующим интерфейсам. На netplan-интерфейсе IPv4 SSH `systemd-networkd` может позднее повторно включить IPv6 link-local; для такого интерфейса установщик добавляет отдельный `/etc/systemd/network/<имя>.network.d/99-zz-bob-auto-xray-ipv4-only.conf` с `LinkLocalAddressing=no` и `IPv6AcceptRA=no`. Имя берётся из активного netplan-файла networkd. Исходный netplan-файл не изменяется, отдельный сервис не создаётся. Перед изменением проверяются IPv4 SSH, DNS без AAAA, отсутствие чужих IPv6 sysctl-назначений и конфликтующих networkd drop-in. Ошибка применения откатывает runtime-значения и оба проектных файла; ошибка последующей настройки BBR также откатывает IPv6. `--preflight-only` только читает состояние. Загрузчик не меняется.

Установщик проверяет возможности запущенного ядра и выбирает стандартный Linux TCP BBR как системный congestion control для новых TCP-сокетов. Он записывает только два параметра в управляемый файл `/etc/sysctl.d/99-zz-bob-auto-xray-bbr.conf`:

```ini
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
```

`fq` обеспечивает нужный BBR packet pacing. Если `tcp_bbr` является загружаемым модулем, отдельный modules-load файл включает его до применения sysctl при загрузке; встроенный в ядро BBR не обязан отображаться в `lsmod`. Запись атомарна, а ошибка применения восстанавливает прежние runtime-значения и проектные файлы. Установщик не меняет TCP buffers, ECN, Fast Open, MTU и другие сетевые параметры.

BBR управляет перегрузкой TCP-соединений, созданных после смены default. Приложение может явно выбрать другой доступный алгоритм для своего сокета. BBR не относится напрямую к UDP, не заменяет WARP и не гарантирует рост скорости каждого VPN-соединения: результат зависит от канала, RTT, потерь, провайдера и виртуальной сети VPS.

`net.core.default_qdisc=fq` задаёт default для новых подходящих сетевых очередей, но уже существующий виртуальный интерфейс может законно показывать `noqueue`, `mq` или qdisc своего virtio/гипервизора. Acceptance сохраняет этот qdisc как диагностику и не выполняет опасный `tc qdisc replace` на удалённом интерфейсе. Базовая ручная проверка:

```bash
sysctl net.ipv4.tcp_congestion_control
sysctl net.core.default_qdisc
```

## Проверяемые зависимости

`versions.lock.json` фиксирует 3x-ui, upstream scripts и commit routing. Для нового WARP зарезервированы `menu_commit` и `menu_archive_sha256`: пока PR #6 не объединён и не проверен, оба поля `null`, а режим install прекращается до изменений системы. После заполнения Bob требует точного совпадения pinned commit с официальным `main` и проверяет SHA256 commit-addressed архива до APT. Плавающие URL `main` и непроверенный форк отвергаются. В adopt/skip данные меню не проверяются и не скачиваются. Release assets 3x-ui сверяются с SHA256 GitHub release metadata, записанным в lock при разработке. Оригинальный non-interactive `install.sh` 3x-ui запускается без изменений; временный offline curl adapter обслуживает исключительно заранее скачанные и проверенные файлы. Доступ к неизвестным URL прекращается. До применения loopback/TLS и проверенного ядра старт службы заблокирован systemd ConditionPathExists. Порт панели никогда не открывается в UFW; публичный доступ идёт через существующий Self-SNI listener 443. Root-only marker создаётся после успешной настройки; ExecStartPre повторно устанавливает loopback перед каждым стартом службы.

Новый WARP остаётся заблокированным до объединения и интеграции проверенного non-interactive контракта PR #6 в официальном VPS-Server-Menu. На 23 октября 2026 его `main` всё ещё содержит несовместимый для Bob commit `3775143…`. Bob не вызывает интерактивное меню и не выполняет разрушительное обновление checkout. Режим `adopt` не вызывает меню: он читает active `warp-svc`, `warp-cli status/settings`, loopback listener и Cloudflare trace через SOCKS5. Установка существующего аккаунта, смена proxy mode/порта и перезапуск сервиса запрещены. Режим `skip` не обращается к WARP, не требует `/dev/net/tun` и допускает уже установленный сторонний WARP без изменений.

Пакеты Ubuntu и WARP обновляются из подписанных APT-репозиториев; фактически установленные версии сохраняются в state. Это не побитово воспроизводимый snapshot APT. Поддержка 26.04/arm64 условна: требуются официальные пакеты для этой пары ОС/архитектуры; обход через пакеты другой Ubuntu отсутствует.

## DigneZzZ/routing

Единый источник: https://github.com/DigneZzZ/routing/tree/a2241b8d6793ee3e5fb2d0eeb8fe9eac7cd79876 . Автоматического исполнения кода или обновления плавающей ветки нет.

Клиентские `profiles/happ.json` и `profiles/incy.json` взяты из `v2ray/{happ,incy}/default.json`; единственное изменение — фиксация commit в URLs geodata. Сохранены upstream `RouteOrder=block-proxy-direct`, GlobalProxy, DIRECT для private/RU, а также дополнительные DIRECT-категории Apple/Microsoft/игр из upstream. Это lite-профили с proxy по умолчанию и DoH upstream. Их semantically identical JSON передаётся разными deeplink `happ://routing/onadd/<Base64(JSON)>` и `incy://routing/onadd/<Base64(JSON)>`.

В v3.10.2 upstream уже исправил конфликт #6273: для INCY собственный профиль находится в теле raw/Base64 subscription, а Happ Routing header удаляется. Happ получает свой Routing header. Никаких patches upstream и лишнего subscription proxy нет. Тесты проверяют именно эти разные контракты; для INCY отсутствие Routing является требуемым результатом, а не ошибкой. Реальные мобильные приложения проверяются отдельно от HTTP-контракта.

Начальные серверные GeoSite/GeoIP берутся из закреплённого официального архива 3x-ui. Архив содержит основной набор Loyalsoldier и RU-набор runetfreedom; SHA256 каждого `.dat` записан в `versions.lock.json`, а установщик разбирает protobuf и проверяет наличие всех реально используемых категорий до настройки панели. Малые файлы DigneZzZ остаются только в клиентских профилях Happ/INCY.

## Штатное обновление geodata

После установки Xray сам обновляет четыре server assets через native `geodata.assets`: основные `geosite.dat`/`geoip.dat` из Loyalsoldier и RU-файлы из runetfreedom. `geodata.outbound` равен `direct`; отдельные cron jobs, download scripts и systemd timers отсутствуют. Конфигурация хранится в штатном Xray JSON, отображается в Geodata UI 3x-ui и сохраняется обычным `xray/update`.

Systemd drop-in задаёт `TZ=Etc/UTC` для `x-ui` и дочернего Xray, а cron `0 0 * * *` означает 00:00 UTC = 03:00 Europe/Moscow. На Xray 26.10.21 конфигурация native GeoData прошла acceptance после замены бинарного файла; фактический очередной ежедневный запуск cron для этой версии отдельно не проверен. Системный timezone сервера не меняется.

Native updater сначала загружает все assets во временные файлы, затем атомарно заменяет цели и вызывает reload registries. Ошибка скачивания не меняет установленные файлы; ошибка reload возвращает прежние файлы. Reload перечитывает только GeoData, на которые ссылаются активные matchers: в режиме skip `geosite.dat` не задействован правилами, а `geosite_RU.dat` задействован. Поэтому проверка rollback подменяет последний. Acceptance проверяет оба пути, hot reload без смены PID и повторно валидирует используемые категории.

Штатные теги Basic UI — `direct` и `blocked`; отдельный дублирующий `block` не создаётся. В install/adopt порядок пользовательских outbounds — `direct`, `blocked`, `warp-outbound`; в skip — только `direct`, `blocked`. Правила:

1. Штатное служебное правило `api`.
2. `blocked`: `geoip:private`, `ext:geoip_RU.dat:ru`, `geoip:ua`.
3. `blocked`: протокол `bittorrent`.
4. `blocked`: `ext:geosite_RU.dat:category-ads-all`, `ext:geosite_RU.dat:ru-available-only-inside`, суффиксы `.ru`, `.su`, `.xn--p1ai`.
5. Только install/adopt: `warp-outbound` для `geosite:category-ai-!cn`, `geosite:category-ip-geo-detect`.

Терминальное правило DIRECT отсутствует: Xray использует первый outbound (`direct`) для трафика, который не совпал с правилами. Такой вид совпадает с Basic UI, переживает сохранение через API без появления дублей и оставляет служебный inbound панели работоспособным.

`IPOnDemand` принципиален: Xray разрешает домен тогда, когда раннему IP-правилу нужен адрес. Поэтому иностранное имя, которое разрешается в российский IP, блокируется до более позднего GeoSite WARP. `IPIfNonMatch` может пропустить раннюю IP-проверку, если позднее совпадёт доменное правило. DNS Xray использует DoH через системный путь; серверные блокировки не касаются Certbot, SSH, системного DNS, WARP daemon или панели. В режиме skip AI/IP-check попадает в обычный DIRECT после блокирующих правил. В install/adopt недоступный WARP не переключается молча на DIRECT.

REALITY хранится с `show: true` и Firefox uTLS fingerprint. Sniffing включает `http`, `tls`, `quic`, `fakedns`; итоговый `routeOnly: true` использует sniffed имя для выбора маршрута и сохраняет запрошенный адрес назначения. Контрольный SOCKS5+TLS тест с раздельными IP и SNI подтвердил различие режимов: при `false` Xray переписывает dial target, при `true` сохраняет исходный IP. Firefox также фиксируется в выдаваемых VLESS URI и во временных E2E-клиентах.

## Защита и данные портала

CSPRNG меняет реально используемые названия, цвета, SVG-геометрию, CSS classes, IDs, пути, cookie и поля формы. На GET создаётся CSRF token в PHP session; cookie Secure/HttpOnly/SameSite=Strict. POST проверяет token, удаляет массив POST, меняет session ID и после ограниченной случайной задержки отвечает 303. В session сохраняются только csrf/error. Логины и пароли не сохраняются и не пересылаются; access log портала выключен. Nginx ограничивает размер тела и скорость auth-запросов. Ответы чувствительных страниц: no-store, CSP, no-referrer.

UFW сохраняет все обнаруженные SSH-порты и открывает только SSH/80/443/подписки. Существующие правила разрешены только для обнаруженных SSH-портов; неизвестные правила вызывают отказ. Fail2ban настраивается через официальный `x-ui setup-fail2ban` (3x-ipl) и дополнительный override штатного sshd jail. Jail 3x-ipl реагирует на LIMIT_IP VPN-клиентов и не защищает вход в панель. Nginx ограничивает POST на секретный login endpoint, а панель также имеет штатное ограничение попыток входа upstream, сильный пароль, TLS и CSRF. Credentials и private keys хранятся в файлах 0600 внутри root-only state directory; вывод команд с секретами не логируется.

Перед provisioning APT выполняет отдельные `update`, обычный `upgrade -y` и `install -y` с `DPkg::Lock::Timeout=600`, noninteractive/`--force-confold` и запретом удаления пакетов при upgrade. Сетевые повторы ограничены и применяются только к `update`; неизвестный частичный install автоматически не повторяется внутри того же запуска. Полный вывод команд хранится в redacted журнале 0600, а терминал показывает классификацию lock/network/dependencies/dpkg/systemd/timeout.

State machine сохраняет installation ID, режим WARP, текущий и последний завершённый этап, исходные credentials и доказательства отсутствовавших управляемых путей. Resume после APT/WARP/Certbot проверяет домен, неизменность режима, ownership и nginx default site. Старые state с `adopt_warp` читаются как install/adopt. В skip этап WARP отмечается завершённым как осознанно пропущенный. При adopt перед Certbot повторно проверяется работающий туннель без его изменения. После сертификата, панели и других неоднозначных стадий автоматическое продолжение прекращается с инструкцией. `HEALTHY_LOCAL` повторно не устанавливается. Процесс защищён `flock`; `--preflight-only` не создаёт state, lock или лог.

При сбое установка сообщает FAILED и не выводит credentials как успешный результат. Чужие конфигурации не удаляются. Автоматического отката пакетов/сертификата нет: он мог бы удалить полезную диагностику или разорвать SSH.

## Сопоставление с пользовательским 3xui_guide.txt

Предоставленный позднее гайд подтверждает выбранную основу: nginx `ssl http2 proxy_protocol` на Unix socket, REALITY `xVer=1`, `set_real_ip_from unix:`, `real_ip_header proxy_protocol`, HTTP redirect и SOCKS outbound `127.0.0.1:40000` с тегом `warp-outbound`. Все эти механизмы сохранены и проверены функционально.

Путь сокета изменён с примера `/dev/shm/nginx.sock` на `/run/selfsni/nginx.sock`, чтобы закрыть каталог правами и явно восстановить его через systemd. Это по-прежнему Unix-socket Self-SNI, а не TCP reverse proxy. Certbot standalone из старого гайда заменён webroot в соответствии с основным ТЗ; остановка nginx для продления не требуется. Тестовый домен берётся из текущего ввода пользователя, домен из примера гайда не используется. Удаление чужого `/var/www/html`, ручные nano-правки и запуск установщика с плавающей ветки из гайда не переносятся в provisioning.

Новая схема панели и модель угроз описаны в [PANEL-HTTPS.md](PANEL-HTTPS.md). Ранее сохранённый state без `panel_access=https-443` читается как прежний публичный порт; завершённые установки автоматически не мигрируют. Только разрешённый ранний resume до установки панели получает новую схему.
