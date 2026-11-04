<p align="center"><img src="assets/banner.svg" alt="Bob Auto Xray" width="100%"></p>

# Bob Auto Xray

**Из чистой VPS — в собственную VLESS REALITY-ноду за один запуск установщика.**

Bob настраивает Xray и 3x-ui на вашем домене. После установки можно открыть панель и сразу подключить первого клиента.

[![CI](https://github.com/DaMistic727/Bob-auto-xray/actions/workflows/ci.yml/badge.svg)](https://github.com/DaMistic727/Bob-auto-xray/actions/workflows/ci.yml)
[![Релиз](https://img.shields.io/github/v/release/DaMistic727/Bob-auto-xray?include_prereleases&color=2b9e88)](https://github.com/DaMistic727/Bob-auto-xray/releases)

## Что вы получите

- **VLESS TCP REALITY + Vision.** Self-SNI использует ваш домен и HTTPS-сайт на том же TCP/443.
- **Панель и подписки.** 3x-ui и ссылки подписок доступны по HTTPS на 443. Bob получает и настраивает продление сертификата Let's Encrypt.
- **Профили Happ и INCY.** По правилам профиля российские ресурсы идут напрямую с устройства. Сервер отдельно блокирует рекламные категории GeoSite в трафике туннеля.
- **WARP по желанию.** Его можно подключить для отдельных категорий AI-сервисов и проверок IP.
- **Защита и диагностика.** Bob настраивает UFW и Fail2ban, а через меню показывает состояние ноды и данные подключения.

## Быстрый старт

**1. Подготовьте VPS и домен.** Возьмите чистую Ubuntu 26.04 amd64 (поддерживается также 24.04 LTS; 22.04 не поддерживается) с публичным IPv4 и доступом root по SSH. Откройте TCP-порты SSH, 80 и 443. Добавьте A-запись `vpn.example.com → IPv4 вашего VPS`. Отключите CDN-проксирование. В Cloudflare выберите **DNS only**. У домена не должно быть AAAA-записи. Дождитесь завершения cloud-init.

**2. Запустите установку от root.** Мастер спросит домен и режим WARP. Bootstrap проверит SHA256 архива перед запуском.

```bash
bash <(curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' https://github.com/DaMistic727/Bob-auto-xray/releases/download/v0.4.2/install.sh)
```

**3. Подключитесь.** Откройте выданную ссылку на 3x-ui. В `vless by bob` уже есть VPN-клиент `admin`. Скопируйте его ссылку или подписку в приложение.

## После установки

```bash
bob          # данные панели, подписка admin, состояние сервера
bob doctor   # диагностика
```

Запускайте команды от root. Меню доступно, если имя `bob` было свободно при установке. Храните пароль, URL панели и ссылки подписок как секреты. Меню показывает исходный пароль установки.

> **v0.4.2 Preview.** VPS E2E v0.4.2 не проводился. Испытания на Ubuntu 26.04.1 amd64 относятся к v0.4.0. Старые ноды автоматически не мигрируют. Доступность зависит от сети и её ограничений. [Матрица проверок](docs/TESTING.md)

## Документация

[Архитектура и маршрутизация](docs/ARCHITECTURE.md) · [HTTPS-панель](docs/PANEL-HTTPS.md) · [Эксплуатация](docs/OPERATIONS.md) · [Релизы и установка](docs/RELEASE.md) · [История изменений](CHANGELOG.md)

[Безопасность](SECURITY.md) · [Участие в проекте](CONTRIBUTING.md) · [Планы](docs/ROADMAP.md)

[GPL-3.0](LICENSE) · [Атрибуция и лицензии Xray, 3x-ui, VPS-Server-Menu и других компонентов](THIRD_PARTY_NOTICES.md)
