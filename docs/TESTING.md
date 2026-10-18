# Отчёт о тестировании · 18 октября 2026

## v0.3.0: Ubuntu 26, чистая установка

Проверенная среда: новая Ubuntu 26.04.1 LTS amd64, kernel `7.0.0-38-generic`. До запуска отсутствовали Bob Auto Xray, 3x-ui, nginx, Certbot и регистрация Cloudflare WARP; TCP слушали только SSH и локальный DNS. Адрес и домен удалены из публикуемых материалов.

Полный запуск выполнен без `--adopt-warp`:

```sh
bash /root/selfsni/install.sh --domain vpn.example.com
```

Установщик сам подключил официальный Cloudflare repository для Ubuntu `resolute`, установил WARP `2026.8.2110.0`, создал регистрацию, включил proxy mode на `127.0.0.1:40000` и подтвердил `Connected / healthy`. В ходе первого кандидата обнаружена ошибка прав нового APT keyring при строгом `umask 077`; установщик исправлен и чистый preflight повторён. Provisioning исправленного кандидата дошёл до всех компонентов. Новая проверка планировщика сначала ожидала необязательную INFO-строку при runtime loglevel `warning`; проверка заменена фактическим E2E планировщика и повторно прошла.

Фактические версии:

| Компонент | Версия |
|---|---|
| Ubuntu | 26.04.1 LTS amd64 |
| Kernel | 7.0.0-38-generic |
| 3x-ui | 3.10.2 |
| Xray-core | 26.10.10, commit `701af60`, Go 1.27.2 |
| Cloudflare WARP | 2026.8.2110.0 |
| nginx | 1.28.3-2ubuntu1.11 |
| PHP-FPM | 8.5.4-0ubuntu1.3 |
| Certbot | 4.0.0-4 |
| Fail2ban | 1.1.0-9 |
| UFW | 0.36.2-9build1 |

## Native Geodata Auto-Update

Используется только встроенный Xray `geodata`; cron jobs и дополнительные systemd timers не создаются. Исходный код Xray 26.10.10 использует локальный timezone процесса. Drop-in `x-ui.service` задаёт `TZ=Etc/UTC`, Xray наследует эту переменную, а `geodata.cron` равен `0 0 * * *`. Это соответствует ровно 03:00 Europe/Moscow (UTC+3 без сезонного перевода) и не меняет системный timezone VPS.

Через `geodata.outbound=direct` настроены четыре стандартных источника 3x-ui:

- Loyalsoldier: `geosite.dat`, `geoip.dat`;
- runetfreedom: `geosite_RU.dat`, `geoip_RU.dat`.

Активный E2E временно переключал cron на каждую минуту. Все четыре файла скачались, их mtime изменился, категории повторно прочитались, а PID Xray остался прежним. Затем 404 оставил все digest/mtime неизменными. HTTP 200 с невалидным protobuf вызвал reload failure и атомарный rollback; digest/mtime и категории сохранились. В `finally` восстановлены production assets, `direct` и `0 0 * * *`. Настройки видны в `geodataSources` 3x-ui, проходят API save round-trip и сохранились после reboot. [Обезличенный geodata E2E →](reference-geodata-acceptance.json)

## Acceptance и реальные соединения

Локально прошёл 21 unittest/bootstrap test, включая regression для прав APT keyring, точную native geodata-конфигурацию, источники и обязательные protobuf-категории.

До и после reboot прошли 24/24 базовые on-host проверки:

- Basic UI/API save round-trip, runtime Xray и geodata;
- HTTPS-панель, login/CSRF/logout и общий сертификат трёх TLS listeners;
- REALITY fallback → PROXY v1 → Unix socket → nginx/PHP;
- WARP через loopback при системном `warp=off`;
- RU/UA/private/BitTorrent block, AI/IP-check через WARP и обычный DIRECT;
- Happ/INCY contracts, UFW, Fail2ban, Certbot timer и все systemd services.

On-host E2E создал одного временного клиента, проверил Firefox VLESS REALITY Vision, route-only sniffing, реальные подписки и маршрутизацию, затем удалил клиента. [Обезличенный on-host JSON →](reference-onhost-acceptance.json)

Внешний Mac/arm64 Xray 26.10.10 до и после reboot подключался к публичному TCP/443. Проверены REALITY Vision, DIRECT trace, отдельный WARP egress, OpenAI HTTP exchange с ростом `warp-outbound` counters, блокировка RU domain/IP/foreign-name-to-RU-IP и отсутствие публичного SOCKS5. [Обезличенный внешний JSON →](reference-external-acceptance.json)

Выполнена реальная перезагрузка: boot ID изменился; `x-ui`, nginx, WARP, Fail2ban, PHP 8.5 FPM и Certbot timer запустились автоматически. Production geodata JSON и timezone сохранились. После повторных on-host и внешних E2E постоянных клиентов осталось ноль. [Краткое reboot evidence →](reference-reboot-acceptance.json)

Полный обезличенный снимок API и runtime находится в [reference-config-redacted.json](reference-config-redacted.json).

## v0.2.0: отдельная прежняя среда

v0.2.0 проверялся на Ubuntu 24.04.5 LTS amd64 с уже существующим WARP через `--adopt-warp`. В том прогоне регистрация WARP не создавалась заново. Эти результаты не использовались как доказательство совместимости v0.3.0 с Ubuntu 26.

## Границы подтверждения

- Ubuntu 22.04 и arm64 VPS не проверялись отдельным clean install.
- Реальные Happ/INCY на мобильных устройствах не запускались; проверены HTTP/Base64/JSON/deeplink contracts и совместимый Xray-клиент.
- Подключение с подтверждённого российского source IP и UDP через WARP SOCKS отдельно не проверялись.
- Классификация GeoIP/GeoSite зависит от upstream; установщик и E2E проверяют digest/структуру и все используемые категории.
