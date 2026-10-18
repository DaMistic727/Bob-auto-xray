# Upstream и материалы третьих сторон

| Проект | Использование | Источник / условия |
|---|---|---|
| 3x-ui | Оригинальные installer/CLI scripts и service unit; release binary скачивается при установке | [MHSanaei/3x-ui](https://github.com/MHSanaei/3x-ui), GPL-3.0; копия лицензии в `vendor/3x-ui/LICENSE` |
| Xray-core | Поставляется внутри закреплённого релиза 3x-ui | [XTLS/Xray-core](https://github.com/XTLS/Xray-core); условия upstream сохраняются |
| VPS-Server-Menu | Закреплённый архив скачивается при установке; WARP compatibility adapter использует тот же способ настройки | [Diman6267/VPS-Server-Menu](https://github.com/Diman6267/VPS-Server-Menu); условия upstream сохраняются |
| DigneZzZ/routing | Клиентские rules, geodata URLs и профили Happ/INCY | [DigneZzZ/routing](https://github.com/DigneZzZ/routing); отдельный LICENSE в использованном снимке не найден, новая лицензия на эти данные не назначается |
| Loyalsoldier/v2ray-rules-dat | Начальные и автоматически обновляемые `geosite.dat`/`geoip.dat` | [Loyalsoldier/v2ray-rules-dat](https://github.com/Loyalsoldier/v2ray-rules-dat); GPL-3.0 |
| runetfreedom/russia-v2ray-rules-dat | Начальные и автоматически обновляемые `geosite_RU.dat`/`geoip_RU.dat` | [runetfreedom/russia-v2ray-rules-dat](https://github.com/runetfreedom/russia-v2ray-rules-dat); условия upstream сохраняются |
| Cloudflare WARP | Официальный подписанный APT-репозиторий и public package signing key | [Cloudflare](https://pkg.cloudflareclient.com/); условия поставщика |

Commit IDs, версии и SHA256 находятся в [versions.lock.json](versions.lock.json). Лицензия корня репозитория относится к коду проекта и не заменяет права и условия на материалы третьих сторон. Сторонние сервисы, бренды и авторы не заявляются участниками или спонсорами этого проекта.
