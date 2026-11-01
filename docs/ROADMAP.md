# Следующие проверки и улучшения

План отражает непроверенные сценарии и задачи, а не обещание уже реализованной совместимости.

- [ ] Ubuntu 22.04 amd64: установка, Certbot, внешний клиент, reboot.
- [x] Ubuntu 26.04 amd64: чистая установка, новый WARP, native geodata, внешний клиент и reboot.
- [ ] arm64 VPS: проверка release binary и WARP package.
- [x] iOS Happ/INCY на физическом iPhone: user-verified VPN connection, applied routing and WARP.
- [ ] Android Happ/INCY: импорт и реальная работа на устройстве.
- [ ] Отдельные измерения DNS leaks, UDP, QUIC и точного соответствия каждого клиентского правила.
- [x] v0.3.2-derived кандидат: полный clean install и BBR/FQ persistence после реального reboot одноразовой VPS.
- [ ] Подключение с подтверждённого российского IP и проверка отсутствия source-блокировки.
- [ ] Отдельная проверка UDP-сценариев через WARP proxy.
- [x] Bob Doctor: локальные read-only проверки, ограниченный deep-режим и отчёт без секретов.
- [ ] Испытать Bob Doctor на одноразовой VPS с реальными клиентами и после reboot перед релизом.

Готовые проверки и их границы: [TESTING.md](TESTING.md).
