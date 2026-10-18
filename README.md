<p align="center"><img src="assets/banner.svg" alt="Bob Auto Xray — Self-SNI, automated." width="100%"></p>

<p align="center">
  <a href="https://github.com/DaMistic727/Bob-auto-xray/actions/workflows/ci.yml"><img src="https://github.com/DaMistic727/Bob-auto-xray/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/DaMistic727/Bob-auto-xray/releases"><img src="https://img.shields.io/github/v/release/DaMistic727/Bob-auto-xray?include_prereleases&amp;color=2b9e88" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue" alt="GPL-3.0"></a>
  <img src="https://img.shields.io/badge/tested-Ubuntu%2026.04%20amd64-e95420" alt="Tested on Ubuntu 26.04 amd64">
</p>

# Bob Auto Xray

**Чистая VPS → готовый VLESS TCP REALITY Vision с собственным SNI.**

Одноразовый установщик разворачивает 3x-ui, HTTPS-заглушку частного облака, выборочную маршрутизацию через WARP и подписки для Happ/INCY. Конфигурации создаются автоматически, клиентов вы добавляете через панель.

> **Статус: preview.** Базовый полный clean install v0.3.1, новая регистрация WARP, native Geodata Auto-Update, Firefox/REALITY Vision, внешний клиент и реальная перезагрузка проверены на Ubuntu 26.04.1 LTS amd64. Happ и INCY проверены пользователем на физическом iPhone. BBR в v0.3.2 покрыт локальными и mock-тестами; отдельный clean install и reboot именно v0.3.2 ещё не выполнялись. [Подробный отчёт →](docs/TESTING.md)

## Что получится

| Компонент | Результат |
|---|---|
| **3x-ui** | HTTPS, случайный порт и путь, уникальные credentials |
| **REALITY** | TCP/443, Self-SNI, Vision, Unix socket nginx, PROXY protocol v1 |
| **Заглушка** | Уникальный облачный login portal, PHP-FPM, CSRF; данные формы не сохраняются |
| **TLS** | Certbot HTTP-01 webroot, автоматическое продление для всех TLS-сервисов |
| **WARP** | SOCKS5 только на loopback; компактные GeoSite AI/IP-check через WARP |
| **Маршрутизация** | Basic UI `blocked`, RU/UA/private/BitTorrent блокируются; `IPOnDemand`, остальное DIRECT |
| **Geodata** | Штатный Xray hot reload четырёх файлов ежедневно в 03:00 МСК через `direct`, с rollback |
| **TCP BBR** | Автоматически настраивается на поддерживаемом ядре Linux, с FQ pacing и сохранением после reboot |
| **Happ / INCY** | Отдельные профили и способы доставки; правила из [DigneZzZ/routing](https://github.com/DigneZzZ/routing) |
| **Защита** | UFW с сохранением SSH, штатная интеграция Fail2ban 3x-ui |

## Быстрый старт

1. **Создайте DNS A-запись** своего FQDN на IPv4 VPS, без CDN proxy. Если IPv6 не настроен, удалите AAAA.
2. **Подготовьте чистую VPS** с root-доступом и открытыми у провайдера TCP/80 и TCP/443. Проверенная ОС — Ubuntu 26.04 amd64; установщик использует официальный WARP-пакет именно для codename VPS.
3. **Запустите установщик одной командой** от root:

   ```bash
   bash <(curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' https://github.com/DaMistic727/Bob-auto-xray/releases/download/v0.3.2/install.sh)
   ```

   Он запросит FQDN и необязательный email Let's Encrypt. Bootstrap загружает **конкретный релиз** и проверяет закреплённый SHA256 архива до выполнения модулей.

4. **Сохраните HTTPS-ссылку, логин и пароль**, которые появятся после успешных проверок. На VPS результат хранится в root-only `/var/lib/selfsni/state.json`.
5. **Добавляйте клиентов через 3x-ui**, привязывая их к созданному inbound и выбирая flow `xtls-rprx-vision`. Подписки и клиентская маршрутизация уже настроены.

<details>
<summary>Уже установлен WARP / нужен non-interactive запуск?</summary>

Скопируйте и распакуйте проверенный release archive, затем:

```bash
bash /root/selfsni/install.sh --domain vpn.example.com --adopt-warp
```

`--adopt-warp` явно разрешает использовать совместимый существующий WARP. Регистрация и режим не меняются; обязательны proxy mode, loopback listener и рабочий trace. Существующий VPS-Server-Menu принимается только на закреплённом commit без изменений содержимого файлов. Без этого флага обнаруженная установка WARP вызывает отказ.

Параметры: `--domain`, `--email`, `--ssh-port`, `--adopt-warp`, `--preflight-only`. На частично установленной или чужой конфигурации повторная установка по умолчанию запрещена.

</details>

## Как устроен трафик

```mermaid
flowchart LR
    Internet -->|TCP 80| HTTP[nginx · ACME / redirect]
    Internet -->|TCP 443| REALITY[Xray · REALITY]
    REALITY -->|обычный TLS + PROXY v1| Socket[Unix socket]
    Socket --> Portal[nginx TLS + PHP portal]
    REALITY -->|VLESS Vision| Rules{Серверные правила}
    Rules -->|RU / UA / private / BitTorrent| Block[blocked · Basic UI]
    Rules -->|GeoSite AI / IP-check| WARP[WARP SOCKS5 · loopback]
    Rules -->|остальное| Direct[DIRECT]
```

Клиентские профили Happ/INCY направляют российские и локальные ресурсы напрямую с устройства. Серверная RU-блокировка служит дополнительной защитой, если такой трафик всё-таки попадёт в туннель. Полный состав и приоритеты — в [описании архитектуры](docs/ARCHITECTURE.md).

## Поддержка и проверки

| Платформа / сценарий | Статус |
|---|---|
| v0.2.0 · Ubuntu 24.04 amd64 + существующий WARP | Проверены установка через `--adopt-warp`, внешний клиент и reboot |
| v0.3.1 · Ubuntu 26.04.1 amd64, чистая ОС | Проверены установка всех VPN-компонентов, Target UI, новая регистрация WARP, внешний клиент и reboot |
| v0.3.2 · TCP BBR/FQ | 14 mock-сценариев и общая регрессия; clean install и реальный reboot v0.3.2 пока не выполнены |
| Ubuntu 22.04; arm64 | Предусмотрены в коде; отдельное развёртывание не проверено |
| Native Geodata Auto-Update | Проверены 4 assets, UTC→03:00 МСК, hot reload, ошибка, rollback и reboot |
| Happ / INCY contracts | Проверены реальные HTTP-ответы, Base64/JSON и deeplink |
| iOS Happ/INCY | **User-verified E2E: VPN connection, applied routing and WARP confirmed.** Физический iPhone, 18.10.2026 |
| Android | Пока не проверен |

CI выполняет статические проверки, unit/bootstrap-тесты и воспроизводимую сборку. Он **не заменяет** развёртывание на VPS. Подробные подтверждения: [тестирование](docs/TESTING.md), [REALITY Target API/DB/editor/runtime](docs/reference-target-acceptance.json), [обезличенный API/runtime snapshot](docs/reference-config-redacted.json), [native geodata E2E](docs/reference-geodata-acceptance.json), [on-host E2E](docs/reference-onhost-acceptance.json), [внешний клиент](docs/reference-external-acceptance.json). Ранние результаты Ubuntu 24.04 сохранены отдельно в [истории](docs/history/README.md).

## Документация и участие

- [Архитектура, маршрутизация и безопасность](docs/ARCHITECTURE.md)
- [Релизы, SHA256 и установка из исходников](docs/RELEASE.md)
- [Версии и контрольные суммы upstream](versions.lock.json)
- [Изменения](CHANGELOG.md) · [Планы](docs/ROADMAP.md)
- [Как предложить изменение](CONTRIBUTING.md) · [Сообщить об уязвимости](SECURITY.md)

Проект использует официальные [3x-ui](https://github.com/MHSanaei/3x-ui), [VPS-Server-Menu](https://github.com/Diman6267/VPS-Server-Menu) и [DigneZzZ/routing](https://github.com/DigneZzZ/routing). [Атрибуция и лицензии зависимостей →](THIRD_PARTY_NOTICES.md)

Код проекта распространяется под [GPL-3.0](LICENSE). Данные и материалы третьих сторон сохраняют условия своих правообладателей.
