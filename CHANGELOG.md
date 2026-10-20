# Changelog

## v0.3.4 — Unreleased

- APT разделён на `update`, обычный `upgrade` и `install`: штатное ожидание dpkg lock до 10 минут, ограниченные повторы только временных ошибок репозитория и точная классификация lock/network/dependencies/dpkg/systemd/timeout.
- Добавлены redacted diagnostic log 0600, installation lock и state machine с сохранением credentials. Подтверждённые сбои APT, частичная установка WARP и retry Certbot безопасно продолжаются той же командой; поздние неоднозначные стадии автоматически не возобновляются.
- Добавлен русскоязычный TTY-мастер, ASCII/non-TTY fallback и `NO_COLOR`; release bootstrap больше не показывает progress bar curl.
- Вопросы установки переписаны понятным языком, с примерами, предупреждением о системных изменениях и подтверждением. Указанные CLI-значения не спрашиваются повторно; сохранённая установка продолжает работу без повторного мастера.
- Живой прогресс считает только завершённые этапы; длительные команды показывают время и индикатор активности, non-TTY вывод остаётся обычным текстом. Вывод команд сохраняется в root-only журнале, тайм-аут и Ctrl+C останавливают процесс.
- Ошибки Certbot классифицируются по ответу ACME; подтверждённое время снятия лимита переводится из UTC в МСК без догадок. Состояние после сбоя сертификата сохраняется для resume.
- Убрано предупреждение APT по одному лишь процессу `unattended-upgr`; реальное ожидание lock отображается по выводу APT однократно. Сохранены `DPkg::Lock::Timeout=600`, индикатор активности и полный защищённый журнал.
- Финальный экран больше не показывает ссылку на подписки при нуле клиентов, объясняет создание первого клиента и выводит реквизиты только в интерактивном терминале.
- Зависимость от форка VPS-Server-Menu удалена из lock. Проверяется актуальный commit официального `Diman6267/VPS-Server-Menu` `main`. Текущий upstream не поддерживает безопасную автоматическую установку: `install.sh` может выполнить `reset --hard` и изменить `.bashrc`; `install_official_warp()` добавляет cron и может ожидать ввод. Bob останавливается до системных изменений. После появления безопасного интерфейса потребуется интеграция и полный VPS E2E.
- Добавлены регрессии APT, UI и отказа до системных изменений при несовместимом upstream. Релиз v0.3.4 не опубликован.

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
