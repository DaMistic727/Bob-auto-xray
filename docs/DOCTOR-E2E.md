# План безопасного E2E Bob Doctor на одноразовой Ubuntu VPS

Выполняется только на явно выделенной одноразовой VPS с разрешённым reboot и тестовыми данными; production и чужой сервер не подходят. Doctor не выполняет эти шаги сам. Снимки и временные скрипты создаются вне `/var/lib/selfsni` с правами `0700/0600`; в отчёт попадают только хэши, счётчики и обезличенные результаты. Обезличенный snapshot и подход к сравнению «до/после» можно переиспользовать из E2E PR #17 ([E2E-PR17-RESUME.md](E2E-PR17-RESUME.md)).

**Статус перенесённого Doctor (main v0.3.8 + P1–P3): NOT TESTED на VPS.**

## Подготовка и матрица

1. Зафиксировать commit кандидата и SHA256 детерминированно собранного архива; на VPS сверить SHA256 и файлы с коммитом. VPS-Server-Menu не закреплён: фактическая ревизия записывается в `state['menu_revision']`.
2. Отдельные чистые одноразовые Ubuntu 26.04 amd64:
   - **A** — новый WARP (`install`);
   - **B** — `--no-warp`;
   - **C** — `--adopt-warp` с заранее работающим WARP;
   - **D** — нода, установленная опубликованным v0.3.4, без обновления; Doctor кандидата запускается из проверенного архива в локальном режиме и через удалённый bootstrap (см. «Команды запуска Doctor»);
   - **E** (желательно) — нода v0.3.6 для проверки схемы без подписок на 443.
3. На A–C добавить через панель постоянного тестового клиента и дополнительный inbound; проверить внешним клиентом VLESS/Vision до и после Doctor. Тестовые credentials хранить вне отчёта.

## Команды запуска Doctor

Кандидат ещё не опубликован, поэтому его архив собирается локально из коммита, копируется на VPS и сверяется по SHA256. Ни одна команда ниже не устанавливает VPN и не меняет установленную ноду.

- **`bob`** (A–C, установлены кандидатом): `bob doctor --json`, `bob doctor --deep --json`.
- **Локальный режим из проверенного архива** (A и D): архив распакован в каталог, доступный только root, например `/root/bob-doctor-candidate/selfsni`. Загрузки нет: `install.sh` видит рядом `versions.lock.json` и `src/` и запускает Doctor.
  ```bash
  bash /root/bob-doctor-candidate/selfsni/install.sh doctor --json
  ```
- **Удалённый bootstrap с проверкой SHA256** (D): `install.sh` кандидата из коммита (без вшитых значений) получает URL архива и SHA256, вычисленный локально при сборке. Без этих переменных bootstrap отказывает до загрузки. URL должен быть HTTPS и указывать на тот же архив, например временный asset или приватное хранилище владельца.
  ```bash
  SELFSNI_RELEASE_URL='https://<хост>/selfsni-<версия>.tar.gz' SELFSNI_RELEASE_SHA256='<64 hex из manifest>' bash /root/bob-doctor-bootstrap/install.sh doctor --json
  ```
  После публикации релиза тот же сценарий выполняется опубликованным bootstrap: `bash <(curl … /releases/download/<tag>/install.sh) doctor --json`. Он уже содержит URL и SHA256. Bootstrap релизов до Doctor (v0.3.8 и старше) `doctor` не поддерживает.

## Снимок до Doctor

SHA256, размер и mtime `state.json`; `installation_id`, `status`, `stage`, `resume_count`; хэши runtime Xray JSON, Certbot renewal/deploy hook, TLS key/cert, GeoData, `/etc/fstab`, IPv6/BBR sysctl, networkd и x-ui drop-in, `panel-ready`, nginx `selfsni-tls.conf` и snippets, UFW `user*.rules`; логический хэш inbound/client/routing без вывода UUID и ключей; `systemctl` состояние, PID и счётчики рестартов служб; хэши регистрации и настроек WARP; listeners, интерфейсы и IPv6-адреса; SWAP. Для SQLite сравнивать логические строки, а не байтовый хэш.

## Прогоны и ожидания

1. `bob doctor --json` и `--deep --json` (A–C), локальный режим `install.sh doctor --json` и `--deep --json` (A и D), удалённый bootstrap (D); отдельно — bootstrap без `SELFSNI_RELEASE_*` и с неверным SHA256 должен отказать до запуска Doctor. Один валидный JSON, стабильные ID, коды выхода, отсутствие FQDN, URL панели/подписки, токенов, паролей, ключей, UUID и Short ID в stdout/stderr.
2. A–C: все проверки HTTPS/443 (`core.publicRoutes`, `security.internalListeners`, `security.internalFirewall`, `deep.publicPanel`, `deep.publicSubscription`), `core.panelGate`, `core.adminClient`, `core.xrayPin`, `security.stateTemp` — `PASS`; итог `HEALTHY`, кроме документированного блокера IPv6 после reboot.
3. B: WARP-проверки `SKIP`, вызовов `warp-cli` и WARP trace нет. A/C: ровно один `warp-outbound` на loopback SOCKS5; после Doctor хэши регистрации и настроек WARP не меняются (контроль `warp-cli --accept-tos`).
4. D: `routing.invariants`, `system.ipv6`, `security.internalListeners` — `WARN`; проверки схемы HTTPS/443, gate и клиента `admin` — `SKIP`; итог `DEGRADED`, не `UNHEALTHY`. `bob` не появляется, установка не меняется.
5. Повторить снимок: state, конфигурации, логические строки клиентов/inbound/routing, сертификаты, GeoData, UFW, sysctl, networkd, `/etc/fstab`, WARP и SWAP неизменны; installer lock не создан; службы не перезапускались. Допустимые побочные эффекты `--deep`: обычные журналы, счётчики соединений, PHP-сессия.
6. Reboot одноразовой VPS, повтор default/deep и снимка. PID и время запуска закономерно меняются; проверяется логическое состояние и отсутствие лишних рестартов.

## Безопасная имитация отказов

Ничего не останавливать и не редактировать в рабочей конфигурации. В отдельном Python-процессе на одноразовой VPS запускать `Doctor` с узким `Probe`-адаптером, который подменяет **только ответ одной проверки**, оставляя остальные реальные read-only probes: inactive Xray, отсутствующий Unix socket, истёкший/близкий к истечению сертификат, inactive certbot.timer, WARP disconnected, чужой SOCKS5 PID, потерянный ответ команды, timeout HTTPS GET и Cloudflare trace, отключённый IPv6-интерфейс, повреждённый SWAP и persistent-файл. Для неполного state направить только Doctor на отдельный временный JSON через локальный override константы `STATE`. Проверить PASS/WARN/FAIL/SKIP/UNKNOWN и коды 0/1/2/3. Удалить временные тестовые файлы после сравнения; не использовать `systemctl stop`, Certbot renew, изменение клиентов, UFW, WARP, sysctl или SWAP для инъекции отказа.

## Критерий завершения

Default и deep не меняют конфигурацию и установочное состояние, клиент остаётся подключаемым, отчёты не раскрывают секреты, неподтверждённые проверки дают `UNKNOWN` вместо ложного `HEALTHY`, старая нода D даёт `DEGRADED`, а не `UNHEALTHY`. Любое расхождение сначала объяснить по журналам, затем повторить на свежей одноразовой VPS. Результаты A–E, активного клиента и reboot отмечать раздельно.

## История: прогон кандидата `badbe6f` (25.10.2026, база v0.3.4)

Результаты ниже не переносятся на перенесённый Doctor.

На одной подтверждённой одноразовой Ubuntu 26.04.1 amd64 выполнена чистая установка с WARP `install` без email. Установщик завершился `HEALTHY_LOCAL`; WARP, сертификат, BBR/FQ и управляемый SWAP присутствуют. После установки добавлены один постоянный VLESS/Vision-клиент и второй допустимый loopback REALITY inbound. Независимый Xray 26.10.21 на macOS подтвердил HTTPS через VPN, WARP для IP-check и DIRECT для обычного трафика до и после reboot.

До reboot basic дал 27/27 `PASS`, deep — 32/32 `PASS`, оба `HEALTHY`/exit 0. Единственная обнаруженная в ходе первого прогона ошибка Doctor: установленный `warp-cli` требует `--accept-tos` и для read-only JSON status/settings; без флага проверка ошибочно давала `UNKNOWN`. После минимальной правки повторный прогон прошёл. Хэши файлов WARP до и после этих CLI-запросов совпали. В ходе мониторинга внешнего клиента 79 из 80 повторных HTTPS-запросов прошли; причина одного отказа не установлена. Во втором post-reboot мониторинге прошли 50 из 50 запросов.

На post-reboot VPS `ens3` получил `net.ipv6.conf.ens3.disable_ipv6=0` и link-local IPv6-адрес при `all/default=1`. Управляемые sysctl-файл и networkd drop-in остались на месте. Doctor верно вернул `system.ipv6=FAIL`, итог `UNHEALTHY`/exit 2 в basic и deep; этот результат блокирует production readiness. Снимки до и после каждого запуска Doctor совпали по 42/43 контролируемым файлам, state, SWAP и состоянию служб; логические конфигурационные строки 3x-ui также совпали. Сырой хэш SQLite менялся из-за счётчиков активного клиента. В отчётах не обнаружены credentials, FQDN или UUID, stderr был пуст.

На этой VPS выполнены 11 безопасных негативных сценариев через подмену ответов только в `Probe`; проверены PASS, WARN, FAIL, UNKNOWN и exit 0/1/2/3. После них конфигурационные снимки совпали. Режимы `--no-warp` и `--adopt-warp` для **этого кандидата**, старая установка v0.3.4, Ubuntu 22.04/24.04 и строго непрерывная клиентская сессия остаются **NOT TESTED**. Для них требуются отдельные чистые среды или снимки; этот прогон не приписывает им результат.
