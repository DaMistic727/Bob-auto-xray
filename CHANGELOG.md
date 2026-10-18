# Changelog

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
