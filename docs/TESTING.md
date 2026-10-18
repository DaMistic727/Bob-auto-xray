# Отчёт о тестировании · 18 октября 2026

Проверенная VPS: Ubuntu 24.04.5 LTS, amd64, адрес и домен скрыты перед публикацией. SSH-порт 22. Адрес в примере команды заменён на `vpn.example.com`; результаты проверок сохранены. WARP и VPS-Server-Menu существовали до работы; пользователь разрешил их использовать. Регистрация WARP не удалялась, не пересоздавалась и режим не менялся. Полный путь новой установки WARP реализован, но на этой VPS не выполнялся.

После исправления обнаруженных при интеграции отличий API созданные установщиком компоненты были архивированы в закрытый root-каталог. Затем состоялся полный успешный запуск единственной точки входа:

```sh
bash /root/selfsni/install.sh --domain vpn.example.com --adopt-warp
```

Проверки не подменяются значениями конфигурации: где использовалась маршрутизационная API-проба, это отмечено отдельно от реального соединения.

## Версии на VPS

| Компонент | Версия |
|---|---|
| 3x-ui | v3.10.2 |
| Xray | 26.10.10, commit 701af60 |
| nginx | 1.24.0-2ubuntu7.18 |
| PHP-FPM | 8.3.6-0ubuntu0.24.04.11 |
| Certbot | 2.9.0-1 |
| WARP | 2026.8.2110.0, существующая регистрация |
| Fail2ban | 1.0.2-3ubuntu0.1 |
| UFW | 0.36.2-6 |
| VPS-Server-Menu | 6ffecb5ba628b5f43523b3cc4fa360f032524603 |
| DigneZzZ/routing | a2241b8d6793ee3e5fb2d0eeb8fe9eac7cd79876 |

## Выполненные проверки

- 14 локальных unittest: domain validation, неподдерживаемая ОС, checksums, archive traversal, запрет неизвестных загрузок official installer, JSON object/string API compatibility, профили/deeplink/Base64/HTTP-контракт, routing scope/приоритеты, portal uniqueness/privacy, отказ Certbot, ложный WARP trace, внешний WARP listener, повторная установка без записи в существующий state.
- Ещё 3 bootstrap integration tests: настоящий SHA256 и archive validation, успешное исполнение проверенного fixture release, отказ при неверном digest и symlink. Транспорт curl заменён локальным fixture; тест не объявляется проверкой публичного hosting.
- ShellCheck `install.sh`, `bash -n` собственного bootstrap и закреплённых upstream scripts, Python compilation.
- Полный запуск через install.sh, без ручной правки серверных конфигураций.
- 17 on-host acceptance checks при полном install.sh, расширенные до 20 после reboot: один REALITY inbound и ноль клиентов, nginx -t, Xray -test, HTTPS-панель, одинаковый действующий сертификат на 443/панели/подписках, реальный fallback TLS → PROXY v1 → Unix socket → PHP, GET/POST/303/error/CSRF, WARP trace `warp=on`, системный trace `warp=off`, профили панели, live Xray routing API для RU/AI/IP-check/DIRECT, службы, UFW, Fail2ban jails, Certbot timer, настоящий вход с credentials/CSRF, права секретных файлов и отсутствие тестовых данных формы в sessions/logs. [JSON до reboot](onhost-before-reboot.json), [20 проверок после reboot](onhost-after-reboot.json).
- Certbot HTTP-01 production issuance и `renew --dry-run --run-deploy-hooks` прошли. Deploy hook реально перезапустил сервисы; TLS-проверки после этого прошли.
- Временный Vision client через актуальный clients API: реальные VLESS-соединения, DIRECT international HTTP, WARP IP-check (IP совпал с прямым запросом через WARP SOCKS), RU domain/IP connection failure. Клиент удалён; число клиентов восстановлено до нуля.
- Реальные внешние соединения с Mac/arm64 Xray 26.10.10: HTTPS-панель/заглушка/подписки; Happ header и INCY body; VLESS REALITY Vision; DIRECT trace; отдельный WARP egress для IP-check; реальный HTTPS-ответ OpenAI и увеличение счётчиков `warp-outbound`; невозможность соединения с RU domain/IP через туннель; отсутствие внешнего SOCKS5 handshake. [Внешний отчёт](external-acceptance.json).
- nginx restart пересоздал сокет; HTTPS на 443 остался работоспособен.
- Повторный запуск отказался на существующей установке; hashes всех файлов state до/после совпали.

Реальная перезагрузка выполнена. Boot ID изменился, SSH восстановился; nginx, PHP-FPM, 3x-ui/Xray, WARP и Fail2ban поднялись без ручного старта. Все 20 серверных проверок, on-host Vision/subscription e2e и полный внешний Mac/Xray тест повторно прошли; временные клиенты удалены. [Reboot evidence](reboot-acceptance.json).

## Границы подтверждения

- Ubuntu 22.04, 26.04 и arm64 VPS не проверялись установкой. На них требуется наличие официального WARP package для соответствующего codename/architecture; подмена пакетом от другой Ubuntu не выполняется.
- Проверены реальные HTTP-ответы с User-Agent Happ/INCY, JSON/Base64/deeplink и реальный Xray client. Мобильные приложения Happ/INCY на устройствах не запускались; их UI, импорт geodata и платформенные DNS/TUN особенности не подтверждены.
- География IP внешнего тестового клиента не фиксировалась. Отсутствие блокировки входящих российских клиентов подтверждается областью правил и отсутствием source/geo firewall; отдельного подключения с подтверждённого российского IP не было.
- TCP CONNECT на Mac давал положительный ответ и для закрытых контрольных портов. Поэтому внешняя проверка SOCKS проверяет negotiation, а закрытие порта подтверждается также loopback bind и UFW; SYN-only scan не объявляется доказательством.
- Проверен TCP/HTTPS трафик через WARP; UDP через WARP SOCKS отдельно не проверялся.
- GeoIP/geosite — снимок закреплённого commit: качество классификации зависит от upstream, автоматического обновления снимка нет. План обновления установщика по ТЗ не реализован.
- Публикуемый релиз содержит bootstrap с закреплёнными URL и SHA256 payload. Проверки транспорта bootstrap используют fixture; сетевой download опубликованного release asset проверяется отдельно от provisioning VPS.
- Результат подтверждён для указанной среды; универсальная production certification для всех заявленных ОС и мобильных приложений не заявляется.
