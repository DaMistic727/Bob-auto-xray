# v0.3.6 Preview · три изменения и проверка

База: v0.3.5 / `e6d623a8cc4cacae6c08965544143cdb13bbeab5`. PR #11 не включён. Новые установки получают remark `vless by bob`, глобальную GeoSite-блокировку рекламы и одного постоянного VPN-клиента admin. Остальная архитектура сохранена.

## Категория рекламы и API-контракт

Скачан официальный закреплённый amd64 архив 3x-ui v3.10.2, проверен archive SHA256 и SHA256 всех четырёх GeoData из versions.lock.json. Основной geosite.dat содержит category-ads-all: 193644 записи, включая doubleclick.net. [Обезличенная запись проверки](pinned-geodata-v036.json). Пины и GeoData sources/schedule не изменялись. Покрытие рекламы ограничено GeoSite.

В pinned 3x-ui commit `608ef4e4010bf83e2e78328e08f77efc051ad4d7` model.ClientRecord поддерживает limitHwid, а ClientCreatePayload.UnmarshalJSON извлекает его из client payload. Проверка производится по clients/list; flow проверяется на attachment/runtime (поле canonical record может быть пустым). [Upstream service](https://github.com/MHSanaei/3x-ui/blob/608ef4e4010bf83e2e78328e08f77efc051ad4d7/internal/web/service/client.go), [model](https://github.com/MHSanaei/3x-ui/blob/608ef4e4010bf83e2e78328e08f77efc051ad4d7/internal/database/model/model.go).

## Проверки

- Локально Python 3.12/3.15: полный unittest suite. Единственный skip относится к фильтруемому в macOS настоящему connection-refused probe; его классификация покрыта детерминированными mocks.
- Новые регрессии: создание/persist-before-write/idempotency admin, timeout после успешной DB-записи, отказ/нет записи, частичный или неверный catalog record, unowned identity, некорректные UUID/subId/limits, дубли, runtime и обе подписки, успешный/ошибочный on-host E2E cleanup без изменения admin. Ошибки не приводят к массовому удалению или замене записей.
- Remark сравнивался с v0.3.5: весь inbound совпадает, кроме remark. Проверены API save round-trip remark/admin, глобальная и RU advertising categories, порядок BLOCK перед WARP, IPOnDemand и сериализация Xray setting для install/adopt/skip.
- Compileall, Bash syntax, ShellCheck и diff check; целевой secret audit; две независимые release-сборки на Python 3.12/3.15 и checksum/archive/bootstrap verification.
- Ubuntu CI Python 3.10/3.12 запускает весь unittest suite, real Nginx harness (7 групп) и tools/test_client_api.py. Последний скачивает только SHA256-pinned 3x-ui/Xray assets, использует временные DB/config/log и loopback listeners, проверяет настоящее создание/сохранение admin, remark/routing через API, runtime config pinned Xray, выдачу admin в Happ/INCY subscriptions и сохранность постоянных клиентов после owned cleanup. Installer/systemd/network/firewall не вызываются.
- Первый интеграционный commit `a5867b6`: [CI 41187749033](https://github.com/DaMistic727/Bob-auto-xray/actions/runs/41187749033), Python 3.10/3.12 — 186/186, Nginx 7/7 и real pinned API (create/idempotency/remark/routing/cleanup) PASS. Он предшествует дополнительному regression и runtime/subscription harness-проверкам; для финального тега обязательны новые зелёные PR/main CI и Release workflow.

Окончательные CI результаты проверяются по SHA тега [в Actions](https://github.com/DaMistic727/Bob-auto-xray/actions). Release assets сравниваются с независимыми локальными сборками после публикации. Подробный финальный статус указывается в PR/отчёте о выпуске.

## Сохранённые границы

versions.lock.json, profiles Happ/INCY, IPv6, BBR/FQ, swap, Certbot, UFW/Fail2ban, HTTPS/443 templates и installer checkpoint/resume не изменяются. Новый admin — VPN-клиент; credentials панели не меняются. Завершённые старые установки не мигрируют автоматически.

Владелец сообщил о работоспособности v0.3.5. Это не результаты v0.3.6. Для финальной v0.3.6 **NOT TESTED**: чистая установка на VPS, внешний REALITY/Vision, реальные мобильные клиенты/WARP, reboot/IPv6 persistence, обновление/renewal и восстановление сервисов на VPS. Исторический риск IPv6 остаётся. VPS/SSH/production installation/reboot в ходе этой задачи не использовались.
