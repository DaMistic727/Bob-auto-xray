# Отчёт о тестировании · 23 октября 2026

## Обновление закреплённого Xray-core до v26.10.21

Официальный XTLS/Xray-core [отозвал v26.10.10 и v26.10.17 из-за проблемы безопасности certificate roots и выпустил v26.10.21](https://github.com/XTLS/Xray-core/releases/tag/v26.10.21). 3x-ui остаётся v3.10.2; его официальный архив содержит старый Xray, который установщик заменяет отдельно. В `versions.lock.json` закреплены точные URL и SHA256 официальных Linux ZIP и извлечённых `xray` для amd64/arm64. Оба ZIP загружены из официального релиза, их SHA256 совпали с GitHub asset digest, бинарные SHA256 совпали с lock. Runtime arm64 на Linux не проверялся.

| Проверка нового pin | Результат | Доказательство |
| --- | --- | --- |
| Локальные регрессии и статические проверки | **PASSED** | 92/92 unittest; ShellCheck, `bash -n`, `compileall`, JSON и `git diff --check` прошли. Отдельные тесты проверяют официальный URL, сохранение 3x-ui v3.10.2, порядок stop/replace, архивный и бинарный digest, отказ при подмене binary/fork. Два запуска `tools/package.py` дали одинаковый SHA256. |
| Установленный Xray на одноразовой Ubuntu 26.04.1 amd64 | **PASSED** | Официальный бинарный SHA256 `a27e3f95ab6ce270956d0cecd32eda4686a811afc71c64a6a734a3dede14883e`; `Xray 26.10.21`, commit `1b1647c`, Go 1.27.3. Панель осталась `v3.10.2`. |
| Конфигурация, созданная 3x-ui, и on-host acceptance | **PASSED** | Фактический `/usr/local/x-ui/bin/config.json`: `xray run -test` exit 0, on-host 26/26 и временный клиент E2E; [обезличенный отчёт](e2e-xray-261021-onhost.json). Запуск отдельного бинарного файла без `XRAY_LOCATION_ASSET` сначала обнаружил отсутствие GeoData рядом с ним; с каталожными assets панели тот же тест прошёл. |
| Внешний независимый REALITY/Vision-клиент | **PASSED** | 6/6: HTTPS-подписки, реальное подключение к TCP/443, DIRECT/IP-check, OpenAI HTTP и RU block; [обезличенный отчёт](e2e-xray-261021-external.json). Временный клиент удалён. |
| Чистая установка с новым pin, reboot после обновления, Linux arm64 runtime | **NOT TESTED** | Предыдущее clean-install/reboot доказательство ниже получено со встроенным Xray 26.10.10 до этого изменения. Требуется отдельный прогон с новым архивом проекта. |

## Текущий аудит `3ced196` и локальные исправления

**PASSED (локально):** рабочее дерево перед аудитом было чистым, ветка `feat/v0.3.4-installer-recovery-warp-menu` содержала локальный `3ced196`. Проверены ветви `skip` в preflight, bundle lock, установке, resume, Xray template, acceptance и внешнем сценарии; тесты подтверждают отсутствие обращения к upstream WARP API, TUN, WARP-пакету и зависимости `warp-svc` в этих ветвях. Мастер теперь спрашивает WARP сразу после домена, по умолчанию выбирает установку, а adopt предлагает только при обнаруженных `warp-cli` и активном `warp-svc`. Для adopt пропущен общий `apt-get upgrade`, способный косвенно обновить или перезапустить существующий WARP. `apt-get update` и установка необходимых пакетов остаются. Локальный прогон: **89/89 unittest**, `compileall`, `bash -n`, ShellCheck, `git diff --check` — успешно.

Первое SSH-подключение было остановлено изменившимся host key. Пользователь подтвердил новый ED25519 fingerprint через доверенный канал и разрешил полный E2E на одноразовой VPS; полученный ключ совпал. Домен имел единственный DNS A, указывающий на VPS. Архив Bob передан с совпавшим локальным и серверным SHA256 `25f503bdb17f5001b2cc2c9ccfa7dbb6a1015f56451bf552df2f40ce65f99c68`.

| Проверка на Ubuntu 26.04.1 amd64 | Результат | Доказательство |
| --- | --- | --- |
| `--no-warp --preflight-only`, чистая установка | **PASSED** | Preflight exit 0 без изменения системы; installer exit 0, `HEALTHY_LOCAL/skip`, 3x-ui `v3.10.2`, Xray `26.10.10`. |
| On-host acceptance и временный REALITY-клиент | **PASSED** | 25/25 до и после reboot; post-reboot обезличенный [JSON](e2e-no-warp-onhost-after.json), клиент удалён. |
| Фактический `xray run -test`, nginx, TCP/443, DNS, HTTPS, DIRECT | **PASSED** | Xray и nginx exit 0; Xray слушал публичный TCP/443; DNS A совпал с IPv4 сервера. Независимый macOS Xray `26.10.21` прошёл шесть [проверок до](e2e-no-warp-external-before.json) и [после](e2e-no-warp-external-after.json) reboot: REALITY/Vision, HTTPS-подписки, DIRECT, IP-check DIRECT, OpenAI HTTP, RU block. ZIP Xray сверяли с официальным SHA256 `926cc55fda2a8757dabe4f22dc98772f218ace025f101bd0b68aee232e59d5dc`. |
| Native GeoData, BBR/FQ, UFW, Fail2ban, Certbot | **PASSED** для штатной конфигурации | Встроенные проверки 25/25; BBR=`bbr`, qdisc=`fq`, UFW active, jails `3x-ipl` и `sshd`, Certbot timer enabled, nginx/Xray active. Certbot production certificate и renewal dry-run завершились в установщике. |
| Отсутствие WARP-зависимостей | **PASSED** | Пакет `cloudflare-warp` отсутствует, `warp-svc` inactive, `systemctl cat x-ui` не содержит `warp-svc`, runtime config не содержит WARP outbound/rules. |
| Reboot и повторная установка | **PASSED** | Boot ID изменился с `f39bf7d3-7a2c-4551-b825-dde50ccc50cf` на `ca4a5a95-11dc-4a76-b0b3-c3ad0987d275`; повторные on-host/external проверки прошли. Повторный installer exit 1, state остался `HEALTHY_LOCAL`, `resume_count=0`. |
| Инъекция невалидного GeoData HTTP 200 и rollback | **FAILED** | Встроенный Xray не откатил файл `geosite.dat`; строгий E2E exit 1. Исправленный тест теперь сохраняет исходные файлы и в `finally` восстанавливает их атомарно. Повторный запуск подтвердил четыре исходных SHA256 и 25/25 штатной acceptance; это не отменяет FAILED для rollback. |
| Отдельный VPS E2E `--adopt-warp`; реальные APT/DNS/Certbot/несовместимый WARP fault injection и resume | **NOT TESTED** | Нужен новый чистый снимок или вторая одноразовая VPS. Локальные regression tests есть, они не заменяют реальную проверку. |

Сценарий для оставшихся проверок: [E2E-NO-WARP.md](E2E-NO-WARP.md). **Production-ready не подтверждён:** GeoData rollback провалился, adopt и реальные failure/resume сценарии ещё не проверены.

## Базовый аудит опционального WARP до VPS E2E · 23 октября

В текущем кандидате добавлены режимы install/adopt/skip, сохранение режима в state, защита от смены при resume, маршрутизация и acceptance по режиму, а также проверки внешнего клиента без WARP. Локальный Python unittest: 85/85; `compileall`, ShellCheck и `bash -n` прошли. Двукратная локальная сборка дала одинаковый SHA256 payload; `SHA256SUMS` проверен. Режим skip не вызывает меню, не обращается к WARP, не требует `/dev/net/tun`, не включает `warp-svc` в unit x-ui и не проверяет пакет `cloudflare-warp`. Проверка bundle пропускает WARP keyring только в adopt/skip, сохраняя проверки остальных файлов. Regression подтверждает, что несовместимый upstream guard не блокирует полный mock-проход установки skip. Режим adopt выполняет только read-only проверки существующего клиента. Исторические E2E ниже относятся к более ранним кандидатам и не подтверждают эту доработку.

Официальный `Diman6267/VPS-Server-Menu` `main` на дату той проверки был `3775143b00b197a801d9872ebc0bf5407431302d`; API реализован в открытом PR #6, но ещё не объединён. Поэтому проверенный `menu_commit`/`menu_archive_sha256` не заполнены, а install останавливается до системных изменений. На момент коммита `3ced196` E2E новых режимов ещё не выполнялся. Результаты последующего E2E `--no-warp` приведены выше.

## v0.3.4: исправления после реального E2E

Локально прошли 75/75 Python-тестов под Python 3.12, ShellCheck, `bash -n`, `compileall` и двукратная сборка с одинаковыми SHA256. [CI PR #9](https://github.com/DaMistic727/Bob-auto-xray/actions/runs/39426489924) прошёл на Python 3.10 и 3.12. Тесты охватывают отсутствие ложного APT-предупреждения при активном `unattended-upgr`, однократное сообщение при реальном lock, TTY/non-TTY экран без ссылки на подписки и отказ при несовместимом официальном `main` до системных изменений.

Кандидат PR #9 меняет диагностику APT и финальный экран. Динамическая зависимость теперь указывает только на официальный `Diman6267/VPS-Server-Menu` `main`; при текущем SHA `3775143b00b197a801d9872ebc0bf5407431302d` установка останавливается до создания state, lock и запуска APT. Оригинальный `install.sh` при обновлении выполняет `git reset --hard` и меняет `.bashrc` без опций отключения. `install_official_warp()` создаёт cron restart и при сбое может ожидать интерактивный ответ. В официальном `main` нет публичного non-interactive install/health API. Без исправления upstream безопасная интеграция и E2E нового кандидата невозможны; PR #6 пока открыт.

Read-only проверка существующей VPS подтвердила работающие `warp-svc`, `x-ui`, nginx и Bob `HEALTHY_LOCAL` с 26/26 on-host checks. SOCKS5 слушает только `127.0.0.1:40000`; health установленного меню вернул `HEALTHY`, proxy mode и `warp=on`, cron restart отсутствует. Источник **установленного ранее** меню — форк `DaMistic727/VPS-Server-Menu`, commit `a84dfe03191e72c8853959ad47e5f816b0118b4b`. Сервер не менялся. Эти результаты не подтверждают поведение новой интеграции с официальным `main`.

Для возобновления разработки upstream должен предоставить штатные параметры безопасной установки без изменения `.bashrc` и разрушительного обновления существующего checkout, а также неинтерактивные WARP install/health с отключёнными cron и интерактивным восстановлением. После принятия интерфейса Bob должен сохранить фактический commit и origin установленного меню и выполнить полный новый E2E. Архив Bob проверяет SHA256 закреплённых компонентов, но динамический `main` меню не гарантирует побитовую воспроизводимость развёртывания.

## Предыдущий v0.3.4 UX release candidate · 19 октября

На предыдущем кандидате локально прошли 70 Python regression tests под Python 3.12 и системным Python 3.9, `compileall`, `bash -n` и ShellCheck для bootstrap. В число новых регрессий входят мастер с уже заданным email, понятные ошибки и отмена, TTY/NO_COLOR progress, heartbeat длительной команды и загрузки с сохранением полного вывода, Ctrl+C, отсутствие curl progress table, классификация ACME rate limit и перевод подтверждённого UTC-времени в МСК. Python 3.10 в локальной среде отсутствует; [GitHub Actions run 39293995469](https://github.com/DaMistic727/Bob-auto-xray/actions/runs/39293995469) успешно проверил Python 3.10 и 3.12, ShellCheck, compileall, bootstrap tests и воспроизводимую сборку с SHA256. Локальная двукратная сборка также дала одинаковые digest и прошла `SHA256SUMS`.

PR #8 был объединён до завершения обязательного E2E, вопреки собственному описанию PR. Этот факт не означает готовность v0.3.4 к выпуску. Новый кандидат требует отдельного полного прогона с настоящим сертификатом Let’s Encrypt, внешним REALITY/Vision клиентом, WARP, подписками и проверкой после reboot. Известный exact-set rate limit препятствовал выдаче сертификата; повторную выдачу до подтверждённого ACME времени не запускали. До такого прогона тег и release assets не публикуются.

В предыдущем кандидате lock закреплял commit `a84dfe03191e72c8853959ad47e5f816b0118b4b` собственного форка и SHA256 архива `d0497cccd2ff93feb5dba18d453286de027cd09b05ca5febc57818ad27ad2aeb`. Проверка функционального `HEALTHY` на VPS относится к этому коду; после изменений UX новый полный E2E ещё нужен.

## v0.3.4 candidate: installer recovery и VPS-Server-Menu

Локально проходят 61 Python regression tests. Они проверяют APT lock timeout, сохранение уже выведенного stdout при реальном subprocess timeout, ожидание фоновых apt/unattended процессов, ограниченный network retry, отдельные dependency/dpkg/systemd/timeout причины, запрет слепого повтора частичного install, root-only redacted log, сохранение credentials, installer flock, разрешённый retry Certbot, отказ resume после других поздних стадий, корректный выход interactive wizard без traceback, TTY/non-TTY/NO_COLOR и non-interactive WARP contract.

Read-only диагностика текущей Ubuntu 26.04.1 подтвердила точное пересечение: cloud-init запустил provider `apt-get dist-upgrade` в 05:01:19 и держал dpkg-транзакцию до 05:05:51; v0.3.3 начал свой пакетный этап в 05:01:31 и завершился `apt-get exited 100` в 05:01:36. Старый установщик не сохранил stderr, поэтому конкретная строка lock отсутствует, но одновременная активная dpkg-транзакция подтверждена APT/dpkg/journal. Это делает lock contention наиболее обоснованной причиной, без заявления о прямом stderr-доказательстве.

До запуска на VPS отсутствовали nginx, Certbot, 3x-ui, VPS-Server-Menu, WARP и Fail2ban; UFW был неактивен, `dpkg --audit` чист, TCP слушали только SSH и local resolver. Legacy state содержал только сгенерированные v0.3.3 параметры, `FAILED` и progress log. После реального reboot resume кандидата успешно завершил APT, BBR/FQ, firewall и установку VPS-Server-Menu/WARP. WARP подтвердил `HEALTHY`, proxy mode, loopback-порт 40000 и Cloudflare trace `warp=on`; diagnostic log имеет режим 0600.

Дальнейшая установка остановлена внешним exact-set rate limit Let’s Encrypt с указанным CA временем повтора после `2026-10-19 21:39:10 UTC`. Кандидат сохранил `FAILED/CERTIFICATE`, credentials и завершённый этап `WARP`; для безопасного повтора добавлена проверка ownership nginx/ACME webroot и повторная функциональная проверка WARP. Из-за CA-ограничения полный clean-install, REALITY E2E и post-install reboot v0.3.4 пока не подтверждены.

VPS-Server-Menu получил отдельный upstream PR с командами `server-menu warp install|health|xray-outbound`, JSON health и exit codes 0/2/3. Его offline mocks проверяют сохранение существующей регистрации, создание отсутствующей регистрации, proxy/port до connect, отсутствие автоматического WARPSCOUT, отсутствие cron, loopback-only listener и pinned install с сохранённым `.git` без `.bashrc`.

На том этапе lock Bob временно указывал на точный commit ветки upstream PR из форка автора. Теперь этот pin удалён; предыдущие доказательства v0.3.3 не приписываются новому архиву.

## v0.3.2: TCP BBR/FQ

Выполнен полный clean-install E2E на новой Ubuntu 26.04.1 LTS amd64 в KVM. Исходной точкой был тег `v0.3.2` (`b0894a3`), однако до provisioning аудит выявил edge case для минимальных образов: без `modinfo` preflight мог не распознать существующий `tcp_bbr.ko*`. На этой VPS проблема не воспроизвелась, потому что `kmod` уже присутствовал. Кандидат исправлен до установки: read-only probe проверяет дерево модулей запущенного ядра, а provisioning устанавливает `kmod` до возможного `modprobe`. Добавлен regression без мутаций системы.

Локально прошли 37/37 Python tests, из них 15 проверяют BBR. `--preflight-only` сохранил исходные `cubic`, `fq_codel`, незагруженный `tcp_bbr` и не создал управляемых файлов. Полная установка запускалась без `--adopt-warp`, самостоятельно создала WARP-регистрацию и завершилась `HEALTHY_LOCAL` после 26/26 проверок.

BBR проверен независимо от общего acceptance. До установки были доступны `reno cubic`, runtime использовал `cubic`, а default qdisc был `fq_codel`. После установки и после реального reboot доступны `reno cubic bbr`, runtime использует `bbr`, `net.core.default_qdisc=fq`, модуль `tcp_bbr` загружен и закреплён. Новый подключённый TCP-сокет использует BBR, явный override выбирает `reno`. Управляемые файлы — `/etc/sysctl.d/99-zz-bob-auto-xray-bbr.conf` и `/etc/modules-load.d/99-bob-auto-xray-bbr.conf`; чужих конфликтов и duplicate-настроек нет. Повторное применение сохранило содержимое файлов и runtime без изменений.

Во время reboot Ubuntu перешла с kernel `7.0.0-30-generic` на `7.0.0-38-generic`; BBR/FQ и модульная конфигурация сохранились. Фактический root qdisc интерфейса `ens3` — `pfifo_fast`; это отдельно зафиксированная диагностика устройства. `tc qdisc replace` не применялся, и измерений ускорения VPN не проводилось.

| Проверка | Результат |
|---|---|
| Clean Ubuntu 26 preflight | PASS, read-only |
| Fresh WARP registration | PASS |
| Automatic BBR installation | PASS |
| TCP socket uses BBR | PASS |
| FQ default qdisc | PASS |
| REALITY Target UI | PASS, save/reopen до и после reboot |
| Firefox REALITY Vision | PASS |
| DIRECT/WARP/RU routing | PASS |
| Geodata Auto-Update | PASS, hot reload/failure/rollback |
| Certbot / nginx / PHP | PASS |
| Reboot persistence | PASS |
| External Mac E2E | PASS, 7/7 до и после reboot |

On-host E2E прошёл 26/26 до и 26/26 после reboot. Внешний официальный Xray 26.10.10 для macOS arm64 прошёл 7/7 до и после reboot. Временные клиенты удалены, постоянных клиентов осталось `0`. REALITY Target равен `/run/selfsni/nginx.sock` в API, SQLite DB, реальной веб-форме и runtime JSON; `dest` отсутствует. После штатного UI-save и повторного открытия Target сохранился. [Полное обезличенное evidence v0.3.2 →](reference-v032-e2e.json)

Опубликованный immutable tag `v0.3.2` служил базой, но полный provisioning выполнялся кандидатом с исправлением из текущего PR, как требовал найденный до установки edge case. Поэтому результаты относятся к v0.3.2-derived кандидату и не приписываются неизменённым байтам опубликованного архива.

## iOS Happ/INCY: пользовательский E2E

18 октября 2026 года пользователь проверил Happ и INCY на физическом iPhone. **iOS Happ/INCY — user-verified E2E: VPN connection, applied routing and WARP confirmed.** Подтверждены успешное VPN/VLESS REALITY подключение, применение клиентской и серверной маршрутизации, прохождение выбранного трафика через WARP и нормальная работа обоих приложений.

Это пользовательская проверка, а не CI, эмулятор или тест, выполненный Codex. Она не включает packet capture, отдельные измерения DNS leaks, UDP/QUIC или точную проверку каждой категории GeoSite. Android не проверялся.

## v0.3.1: REALITY Target UI

Patch-релиз заменяет legacy-ключ `realitySettings.dest` на единственный `realitySettings.target` при создании inbound. В 3x-ui v3.10.2 `rawInboundToFormValues()` передаёт raw `streamSettings` прямо в `methods.reset()`, а поле формы связано с `streamSettings.realitySettings.target`.

Проверены четыре независимых уровня после штатного API update, реального сохранения веб-формы, restart x-ui и reboot:

- API: `target=/run/selfsni/nginx.sock`, `dest` отсутствует;
- SQLite DB: то же значение и только `target`;
- веб-редактор: поле «Цель» визуально показывает Unix socket; после «Сохранить изменения» и повторного открытия значение сохранено;
- runtime Xray JSON: `target=/run/selfsni/nginx.sock`, `dest` отсутствует, `xray run -test` проходит.

Веб-редактор штатно нормализовал tag с `inbound-443` на `in-443-tcp`; routing rules глобальны и от inbound tag не зависят. После UI-save прошли 25/25 checks, REALITY fallback и Firefox Vision E2E. Те же 25/25, on-host и внешний E2E прошли после reboot; временные клиенты удалены. [Target evidence →](reference-target-acceptance.json)

Дополнительно после reboot подтверждены `TZ=Etc/UTC` у процесса Xray, cron `0 0 * * *`, outbound `direct` и четыре native geodata assets. Это по-прежнему соответствует 03:00 Europe/Moscow.

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

Локально прошли 22 unittest/bootstrap tests, включая отдельный regression для модели REALITY Target, права APT keyring, точную native geodata-конфигурацию, источники и обязательные protobuf-категории.

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

Ранние исходные отчёты v0.1.0 на Ubuntu 24.04 с PHP 8.3 сохранены без изменений в [history/](history/README.md). Они не используются как актуальное доказательство v0.3.x.

## v0.2.0: отдельная прежняя среда

v0.2.0 проверялся на Ubuntu 24.04.5 LTS amd64 с уже существующим WARP через `--adopt-warp`. В том прогоне регистрация WARP не создавалась заново. Эти результаты не использовались как доказательство совместимости v0.3.0 с Ubuntu 26.

## Границы подтверждения

- Ubuntu 22.04 и arm64 VPS не проверялись отдельным clean install.
- iOS Happ/INCY проверены пользователем на физическом iPhone; Android не проверялся.
- DNS leaks, отдельные UDP/QUIC-сценарии и точное соответствие каждого клиентского правила не измерялись.
- Полный прогон выполнен на кандидате от `v0.3.2` с исправлением минимального BBR preflight из текущего PR; неизменённый опубликованный архив после этого исправления отдельно не разворачивался.
- Подключение с подтверждённого российского source IP и UDP через WARP SOCKS отдельно не проверялись.
- Классификация GeoIP/GeoSite зависит от upstream; установщик и E2E проверяют digest/структуру и все используемые категории.
