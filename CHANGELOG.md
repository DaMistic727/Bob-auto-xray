# Changelog

## v0.3.7 — 2026-10-31 · Preview

- Новые установки отдают Happ/INCY подписки на общем HTTPS/443: Xray REALITY → PROXY v1 → Nginx Unix socket → HTTPS 127.0.0.1:<sub_port>. Внутренний случайный порт и TLS сохранены; subURI больше не содержит нестандартного порта.
- UFW новых установок открывает только обнаруженные SSH TCP, 80 и 443; subscription и panel ports остаются loopback. Acceptance проверяет сохранённые настройки, фактические sockets, TLS backend и публичную подписку независимо.
- Отдельный subscription snippet сохраняет URI/query, User-Agent и upstream metadata; TLS/SNI/chain verification обязательны, секретные responses no-store, URI logs выключены. Assets HTML-страницы доступны только под subscription secret; неизвестные глобальные routes закрыты.
- State marker subscription_access=https-443 управляет публичными URL. Старый state без маркера сохраняет URL с отдельным портом; автоматической миграции v0.3.6 и других завершённых установок нет.
- Сохранены vless by bob, постоянный admin, GeoSite ads block, routing/WARP, pinned components, panel/decoy, IPv6/BBR/swap и модель installer resume. VPS E2E/reboot этой версии не выполнялись; исторические результаты не приписываются новой сборке.

## v0.3.6 — 2026-10-31 · Preview

- Основной inbound новых установок называется точно `vless by bob`; изменён только remark, параметры REALITY/Self-SNI, TCP/443, Vision и Firefox сохранены.
- В существующее серверное правило `blocked` добавлена глобальная `geosite:category-ads-all` из основного Loyalsoldier GeoSite рядом с прежней RU-категорией. Блокировки по-прежнему предшествуют WARP; IPOnDemand, четыре источника GeoData, расписание, DIRECT/WARP и мобильные профили не изменены. Наличие категории проверено в закреплённом архиве и обязательно проверяется перед настройкой. Покрытие ограничено используемым GeoSite, полной блокировки всей рекламы не обещается.
- Новая установка создаёт через `clients/add` одного постоянного VPN-клиента `admin`: случайные UUID v4 и subId, Vision, enable=true, нулевые IP/HWID/traffic/expiry/reset лимиты. Это не учётная запись панели; её генерация не меняется. Идентичность сохраняется root-only до записи, неоднозначный ответ API проверяется чтением inbound и каталога; существующие записи не заменяются. До успешного завершения проверяются Xray runtime и подписки.
- Acceptance и E2E учитывают постоянного `admin`. Удаляется только временный клиент с совпавшими owner/UUID/subId; после очистки проверяются неизменность постоянных параметров и attachments. Автоматической миграции завершённых старых установок нет.
- Проверки выполняются локально и в GitHub Actions, включая настоящий pinned 3x-ui API в отдельной временной БД на loopback. Новые VPS/SSH/install/reboot не выполнялись. Сообщённая владельцем работоспособность v0.3.5 не приписывается v0.3.6. IPv6 persistence остаётся отдельным непроверенным на новой VPS ограничением Preview.

## v0.3.5 — 2026-10-31 · Preview

- Панель новых установок доступна на `https://<domain>/<48-hex-secret>/` через существующий REALITY TCP/443 → PROXY v1 → Nginx Unix socket. HTTPS upstream 3x-ui привязан только к `127.0.0.1`; отдельный публичный admin port отсутствует. Заглушка, API, WebSocket, подписки и точный REALITY Target сохранены; старые установки автоматически не мигрируют.
- Перед первым стартом панели проверяются сохранённые TLS/listen IP, internal port, webBasePath и отсутствие стандартной пары admin/admin, затем загружается сертификат/ключ. Несовпадение после exit-zero ошибки CLI оставляет service gate закрытым. Настоящий вход с сгенерированными credentials проверяется acceptance.
- IPv6: в управляемый networkd drop-in добавлен `IPv6LinkLocalAddressGenerationMode=none`, конфликтующий чужой параметр отклоняется. Проверка адресов теперь включает link-local; регрессии моделируют повторную инициализацию интерфейса, отказ проверки, повторное применение и rollback. Это обоснованное усиление политики, **не подтверждение устранения исторического P1 после reboot**: точная причина того сбоя остаётся не установлена без наблюдения на VPS.
- Для воспроизводимости между версиями Python явно закреплён gzip compresslevel=9 (Python 3.15 изменил default).
- Генератор notes использует раздел текущей версии CHANGELOG вместо истории v0.3.4. Release workflow выполняет Nginx harness, compileall и проверку воспроизводимости, как CI.
- Закреплённые версии Xray, 3x-ui, WARP menu и routing не изменены. WARP install/adopt/skip, routing presets, GeoData, BBR/FQ, resume и root-only данные сохранены. Bob Doctor из PR #11 исключён.
- Чистую установку и reboot финального v0.3.5 проверит владелец вручную после публикации; новые VPS E2E не проводились.

- На этапе IPv6 и BBR/FQ добавлена необязательная установка swap-файла `/swapfile` на 2 ГиБ. Он создаётся только при запасе не менее 2 ГиБ свободного места после выделения файла, включается сразу и закрепляется в `/etc/fstab`. Уже существующий swap-файл не удаляется и не перезаписывается; при нехватке места этап пропускается. Доступны read-only preflight, проверка результата и откат при ошибке.

## v0.3.4 — 2026-10-25 · Preview

- Smoke E2E #1 на чистой одноразовой Ubuntu 26.04.1 amd64 подтвердил установку официального VPS-Server-Menu, новую WARP-регистрацию, SOCKS5 `127.0.0.1:40000` с `warp=on`, Certbot, REALITY Vision, DIRECT/WARP/BLOCK и on-host acceptance 27/27. Установщик завершился `HEALTHY_LOCAL`; владелец отдельно проверил VPN, WARP, скорость и маршрутизацию вручную. Независимый автоматизированный внешний E2E и reboot после этой чистой установки не выполнялись.
- После smoke-теста добавлено постоянное отключение IPv6 через проектный sysctl-файл и drop-in systemd-networkd для netplan-интерфейса. На уже установленной одноразовой VPS проверены preflight, применение, настоящий reboot, IPv4 SSH, BBR/FQ, WARP и внешнее REALITY/Vision-соединение с существующим клиентом. Чистая установка с новым IPv6-этапом не выполнялась.

- Исправлено продолжение после Certbot: удалено создание неиспользуемого приватного ACME-каталога, из-за которого повтор на этапе `CERTIFICATE` останавливался с `File exists`. Реальная VPS проверка с контролируемым отказом Certbot подтвердила сохранение installation ID и credentials до успешного resume.
- На отдельной чистой Ubuntu 26.04.1 VPS проверен `--adopt-warp` с заранее работающим официальным Cloudflare WARP: полная установка, фактический `xray run -test`, on-host 27/27 и внешний REALITY/WARP 7/7 до и после reboot. При установке регистрация, настройки, PID и счётчик рестартов WARP не изменились. Первую попытку остановил внешний лимит повторного сертификата Let’s Encrypt; успешный E2E выполнен после сброса VPS с другим доменом.
- GeoData E2E теперь проверяет rollback на `geosite_RU.dat`, который действительно используется правилами режима без WARP. Ожидание журналов привязано к cursor, а восстановление дожидается готовности панели. Штатный updater Xray прошёл успешную загрузку, HTTP 404 и невалидный HTTP 200 с rollback без собственного сервиса обновления.
- Xray-core закреплён отдельно от 3x-ui: официальный v26.10.21 для amd64/arm64, проверка SHA256 архива и извлечённого бинарного файла перед заменой ядра панели. Панель остаётся v3.10.2; её встроенный Xray 26.10.10 заменяется до настройки и итогового запуска. Acceptance проверяет digest, версию и реальный `xray run -test`.
- Добавлены три режима WARP: install (по умолчанию), read-only adopt и skip (`--no-warp`). В skip нет WARP outbound/rules, зависимости x-ui от warp-svc, проверки `/dev/net/tun` и обязательного пакета cloudflare-warp; уже установленный сторонний WARP не изменяется.
- Проверка bundle в adopt/skip не требует WARP keyring; повторная общая проверка в середине установки удалена. Несовместимый upstream WARP API не блокирует режим skip.
- Режим WARP сохраняется в state и защищён от незаметной смены при resume. Старые state с `adopt_warp` читаются без смены installation ID и credentials; пропущенный этап WARP осознанно отмечается завершённым.
- Режимы server routing, on-host acceptance и внешнего E2E разделены; блокировки RU/UA/private/BitTorrent и клиентские Happ/INCY presets сохранены. Новый WARP использует закреплённый официальный API.
- APT разделён на `update`, обычный `upgrade` и `install`: штатное ожидание dpkg lock до 10 минут, ограниченные повторы только временных ошибок репозитория и точная классификация lock/network/dependencies/dpkg/systemd/timeout.
- Добавлены redacted diagnostic log 0600, installation lock и state machine с сохранением credentials. Подтверждённые сбои APT, частичная установка WARP и retry Certbot безопасно продолжаются той же командой; поздние неоднозначные стадии автоматически не возобновляются.
- Добавлен русскоязычный TTY-мастер, ASCII/non-TTY fallback и `NO_COLOR`; release bootstrap больше не показывает progress bar curl.
- Вопросы установки переписаны понятным языком, с примерами, предупреждением о системных изменениях и подтверждением. Указанные CLI-значения не спрашиваются повторно; сохранённая установка продолжает работу без повторного мастера.
- Живой прогресс считает только завершённые этапы; длительные команды показывают время и индикатор активности, non-TTY вывод остаётся обычным текстом. Вывод команд сохраняется в root-only журнале, тайм-аут и Ctrl+C останавливают процесс.
- Ошибки Certbot классифицируются по ответу ACME; подтверждённое время снятия лимита переводится из UTC в МСК без догадок. Состояние после сбоя сертификата сохраняется для resume.
- Убрано предупреждение APT по одному лишь процессу `unattended-upgr`; реальное ожидание lock отображается по выводу APT однократно. Сохранены `DPkg::Lock::Timeout=600`, индикатор активности и полный защищённый журнал.
- Финальный экран больше не показывает ссылку на подписки при нуле клиентов, объясняет создание первого клиента и выводит реквизиты только в интерактивном терминале.
- Официальный VPS-Server-Menu закреплён на `3ed5166bbc5b4fb4cc3bb26616bc4db3bbe2734c` и SHA256 `9d7a2aced39fe206a9cb4cd46481ee6876471458a63988b9fca152238fa7ec29`. Bob проверяет codeload-архив, origin/HEAD/чистоту checkout и symlink-конфликты до APT; сравнивает Git checkout с архивом, запускает официальный установщик без автозапуска и принимает только согласованные JSON/exit codes install и health. Режим adopt использует только health, skip не загружает меню.
- Добавлены регрессии APT, UI и отказа до системных изменений при несовместимом upstream.

## v0.3.3 — 2026-10-18

- Minimal-image BBR preflight распознаёт `tcp_bbr.ko*` в дереве модулей запущенного ядра, даже если команда `modinfo` ещё отсутствует; probe остаётся read-only, а `kmod` устанавливается до возможного `modprobe`.
- Добавлен regression для preflight без `modinfo`; локально проходят 37/37 тестов, включая 15 BBR-сценариев.
- На одноразовой Ubuntu 26.04.1 amd64 проверен кандидат из PR #6: clean install с новой WARP-регистрацией, BBR/FQ, Target UI, geodata, 26/26 on-host и 7/7 external checks до и после реального reboot. Опубликованный архив v0.3.3 отдельно не разворачивался.

## v0.3.2 — 2026-10-18

- Добавлена автоматическая настройка стандартного Linux TCP BBR и `fq` через отдельный атомарно записываемый sysctl-файл.
- Read-only preflight проверяет ядро, доступные congestion controls, тип виртуализации и чужие persistent-конфликты; установка при необходимости загружает и сохраняет штатный `tcp_bbr` module.
- Ошибка применения восстанавливает исходные runtime-значения и проектные файлы; повторное применение идемпотентно.
- Acceptance проверяет runtime, persistence, доступность BBR, новый подключённый TCP-сокет, явный per-socket override и диагностирует qdisc внешнего интерфейса.
- Добавлены 14 mock-регрессий BBR. Полный clean install и reboot v0.3.2 на одноразовой VPS пока не выполнялись.
- Зафиксирован пользовательский E2E Happ/INCY на физическом iPhone: VPN connection, applied routing and WARP confirmed. Android, DNS leaks, UDP/QUIC и каждое отдельное клиентское правило остаются непроверенными.
- Старые v0.1.0 evidence Ubuntu 24.04 перенесены в `docs/history/` без изменения содержимого.

## v0.3.1 — 2026-10-18

- REALITY Unix socket теперь сохраняется как `realitySettings.target`, поэтому поле Target заполнено в редакторе 3x-ui v3.10.2.
- Acceptance отдельно проверяет Target в API, SQLite DB, модели `rawInboundToFormValues`, после повторного сохранения формы и в Xray runtime.
- Учтена штатная нормализация inbound tag веб-редактором с `inbound-443` на `in-443-tcp` без изменения глобальной маршрутизации.
- Повторно проверены fallback, Firefox REALITY Vision, geodata timezone/cron/assets, restart и reboot на Ubuntu 26.04.1.

## v0.3.0 — 2026-10-18

- Добавлен штатный Xray Geodata Auto-Update для четырёх Loyalsoldier/runetfreedom assets через `direct`.
- Xray process timezone закреплён на UTC; cron `0 0 * * *` гарантирует ежедневное обновление ровно в 03:00 Europe/Moscow без изменения timezone VPS.
- Acceptance проверяет UI/API persistence, hot reload без смены PID, отказ скачивания, атомарный rollback и обязательные GeoSite/GeoIP-категории.
- Исправлены права Cloudflare APT keyring при приватном installer umask, обнаруженные на чистой Ubuntu 26.
- Полный clean install с новой регистрацией WARP, on-host и внешний Firefox REALITY Vision E2E проверены на Ubuntu 26.04.1 amd64 до и после reboot.

## v0.2.0 — 2026-10-18

- Серверная конфигурация приведена к штатной модели Basic UI: `direct`, `blocked`, `warp-outbound`, без дублирующего `block` и без терминального catch-all.
- RU/UA/private/BitTorrent блокируются через проверенные категории GeoIP/GeoSite; WARP использует компактные `category-ai-!cn` и `category-ip-geo-detect`.
- Firefox uTLS fingerprint применяется во inbound, подписках и E2E-клиентах; REALITY включает `show`.
- Sniffing дополнен `fakedns`; выбран и функционально проверен `routeOnly: true`.
- Добавлены настройки Happ `Tun-Type: xray` и автоматическая локальная proxy-аутентификация без изменения поддерживаемого TUN mode; контракт INCY сохранён отдельно.
- Установщик проверяет SHA256 и требуемые protobuf-категории четырёх серверных geodata-файлов из релиза 3x-ui.
- Добавлены API roundtrip, runtime, routing, external и reboot acceptance-доказательства без секретов.

## v0.1.1 — 2026-10-18

- Нормализованы права файлов в release archive: одинаковые Git-исходники дают одинаковый SHA256 на локальной машине и CI.
- Добавлен regression test для файлов с правами 0600 и 0644.
- Команда быстрого старта закреплена на v0.1.1; серверный provisioning не изменён.

## v0.1.0 — 2026-10-18

Первый preview-релиз.

- Одноразовый provisioning 3x-ui + VLESS TCP REALITY Vision с Self-SNI.
- nginx Unix socket с PROXY v1, уникальный PHP-FPM login portal и Certbot webroot.
- Выборочная маршрутизация AI/IP-check через WARP и блокировка RU-назначений.
- Отдельные подписки Happ/INCY с профилями DigneZzZ/routing.
- Закреплённые upstream версии, SHA256-проверки и versioned bootstrap.
- UFW, штатный Fail2ban 3x-ui, сертификатные hooks и функциональные acceptance checks.
- Проверены Ubuntu 24.04 amd64 с существующим WARP, внешний Vision-клиент и reboot.
- Добавлены CI, воспроизводимая сборка и документация. Другие платформы и реальные мобильные приложения остаются в плане проверки.
