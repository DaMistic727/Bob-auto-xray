# Changelog

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
