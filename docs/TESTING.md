# Отчёт о тестировании · 18 октября 2026

Проверенная среда: чистое повторное развёртывание на Ubuntu 24.04.5 LTS amd64. Адрес и домен удалены из публикуемых материалов. Существующий Cloudflare WARP был принят через `--adopt-warp`: регистрация не удалялась и не создавалась заново, listener остался на `127.0.0.1:40000`.

Перед тестом прежняя установка была сохранена в закрытом root-каталоге VPS. После очистки компонентов Self-SNI preflight подтвердил единственные TCP listeners: SSH, локальный DNS и WARP. Полный запуск выполнен одной командой:

```sh
bash /root/selfsni/install.sh --domain vpn.example.com --adopt-warp
```

## Проверенная конфигурация

- Один VLESS TCP REALITY inbound на 443, постоянных клиентов нет.
- REALITY: Self-SNI через Unix socket, `show: true`, Firefox fingerprint.
- Sniffing: `http`, `tls`, `quic`, `fakedns`, итоговый `routeOnly: true`.
- User outbounds: `direct`, штатный `blocked`, `warp-outbound`; отдельного `block` нет.
- Сначала служебное правило API, затем blocked IP, BitTorrent, blocked domains и WARP GeoSite; terminal DIRECT rule отсутствует.
- `domainStrategy: IPOnDemand`.
- Happ: `Tun-Type=xray`, SOCKS/HTTP auth `auto`; поддерживаемый TUN mode не перезаписывается.
- INCY использует собственный body/deeplink без Happ-заголовков.

Полный обезличенный снимок сохранён в [reference-config-redacted.json](reference-config-redacted.json). Он содержит Xray setting из API панели, inbound, Hosts, релевантные subscription settings и фактический runtime `/usr/local/x-ui/bin/config.json`. REALITY keys, short IDs, credentials, адрес, домен и случайные пути заменены маркерами.

## Автоматические проверки

Локально прошли 19 unittest/bootstrap tests. Они покрывают checksums, безопасную распаковку, offline adapter официального 3x-ui installer, отказ на неверных входных данных, profiles/subscription contracts, точный порядок и идемпотентность routing, Firefox/REALITY/sniffing и разбор обязательных protobuf-категорий GeoSite/GeoIP.

Статические проверки:

```sh
git diff --check
PYTHONPYCACHEPREFIX=/tmp/selfsni-pycache python3 -m compileall -q src tools
bash -n install.sh vendor/3x-ui/x-ui.sh vendor/3x-ui/install.sh
shellcheck install.sh
```

После полного install и после reboot прошли все 23 базовые on-host проверки:

- канонический Basic UI/API shape и сохранение того же Xray JSON через `xray/update`;
- наличие категорий во всех четырёх установленных `.dat`;
- `nginx -t`, `xray run -test`, runtime outbounds и API service;
- отсутствие private REALITY key в журнале x-ui;
- HTTPS panel, реальный login/CSRF/logout, единый сертификат на трёх TLS listeners;
- fallback REALITY → PROXY v1 → Unix socket → nginx/PHP portal;
- отсутствие тестовых form values в sessions/logs;
- WARP `warp=on` через loopback и системный `warp=off`;
- сохранённые Happ/INCY profiles и settings;
- live route tests для `.ru`, `.su`, `.рф`, RU IPv4/IPv6, BitTorrent, AI, IP-check и DIRECT;
- иностранный домен `77.88.8.8.nip.io`, разрешающийся в RU IP, блокируется;
- systemd, UFW, Fail2ban и Certbot timer.

[Обезличенный on-host JSON →](reference-onhost-acceptance.json)

## Реальные соединения

On-host E2E создаёт одного временного клиента через актуальный clients API и всегда удаляет его в `finally`. Проверены реальные подписки Happ/INCY, Firefox VLESS REALITY Vision, DIRECT, WARP IP-check и отказ RU destinations.

Поведение `routeOnly` проверено без подмены конфигурацией: SOCKS5 получает конкретный IP, а TLS передаёт отдельный SNI. При `routeOnly: false` Xray переписал dial target на sniffed домен; при `true` сохранил исходный IP, из-за чего ожидаемо не прошла проверка сертификата. WARP GeoSite работал в обоих режимах. Финальное значение восстановлено в `true`.

Внешний Mac/arm64 Xray 26.10.10 подключался к публичному TCP/443. Прошли:

- Happ и INCY subscription contracts;
- VLESS REALITY Vision с Firefox fingerprint;
- DIRECT trace и отдельный WARP egress для IP-check;
- настоящий OpenAI HTTP exchange с ростом byte counters `warp-outbound`;
- блокировка RU domain, RU IP и иностранного имени с RU IP;
- отсутствие внешнего SOCKS5 handshake на порту WARP.

[Обезличенный внешний JSON →](reference-external-acceptance.json)

## Перезагрузка

Выполнена реальная перезагрузка. Boot ID изменился, SSH восстановился, `x-ui`, nginx, WARP, Fail2ban и PHP-FPM запустились автоматически. После загрузки повторно прошли 23/23 on-host checks, временный REALITY E2E и полный внешний Mac test. Временные клиенты удалены. [Краткое reboot evidence →](reference-reboot-acceptance.json)

## Границы подтверждения

- Ubuntu 22.04, 26.04 и arm64 VPS не проверялись отдельным развёртыванием.
- Реальные мобильные приложения Happ/INCY на устройствах не запускались; проверены их HTTP/Base64/JSON/deeplink contracts и Xray-клиент.
- Новая регистрация WARP реализована, но в этом прогоне использовалась существующая разрешённая регистрация.
- GeoIP/GeoSite — закреплённый снимок из официального релиза 3x-ui; SHA256 и наличие используемых категорий проверяются, качество классификации зависит от upstream.
- Проверен TCP/HTTPS через WARP; UDP через WARP SOCKS отдельно не проверялся.
