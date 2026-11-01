# VPS E2E: Bob Doctor (PR #11)

Обезличенный отчёт. Домены, IP, credentials, секретные пути, UUID и ключи не публикуются. Методика — [DOCTOR-E2E.md](DOCTOR-E2E.md); пошаговые вердикты, статусы каждой проверки и оракулы — [e2e-pr11-doctor.json](e2e-pr11-doctor.json). Исполнимый kit (snapshot, assert, shim, probe_faults) и полные архивы артефактов хранятся вне репозитория; ниже указаны их SHA256.

## Кандидат и среда

| Поле | Значение |
|---|---|
| PR / commit | DaMistic727/Bob-auto-xray#11 · `4428fd8b180bc35dbc519d77e20c2008d8e42a24` |
| Архив / bootstrap SHA256 (локально = VPS) | `a6c1828139ce3dfcffe8cec6f447e0b256b564726f9440a781678a96144bdd71` / `36c2184d77c1c7e53e281dc5ebc3f5d34795f41b13eec606ff6f1dd722f790a2`; повторная локальная сборка побайтно совпала, `setup.sh` сверил SHA256 и файлы коммита на каждой ноде |
| ОС / ядро / systemd / nginx | Ubuntu 26.04.1 LTS amd64 · D: `7.0.0-30-generic`, A/B/C: `7.0.0-42-generic` · systemd 259 · nginx 1.28.3 |
| Ноды | D да · A да · B да · C да · E нет |
| Дата | 2026-11-01 |
| Отклонения от плана | см. ниже |

### Отклонения

1. **Вход по паролю отключён** drop-in `/etc/ssh/sshd_config.d/00-bob-e2e.conf` (перебор паролей ботами). Пути Bob не затронуты; preflight читает только порты из `sshd -T`.
2. **Одна VPS для всех нод.** D, A, B, C выполнены последовательно на одной одноразовой VPS с переустановкой ОС между нодами; host-ключ после каждой переустановки сверялся с консолью провайдера. D, A, B — один и тот же FQDN, уже использовавшийся с Let's Encrypt (план требовал новый). C — новый FQDN (одна A-запись, без AAAA).
3. **cloud-init на свежей ОС.** Первая установка A стартовала, пока cloud-init выполнял `apt-get dist-upgrade`: обновление `openssh-server` остановило `ssh.service` и удалило `/run/sshd`, preflight кандидата завершился `sshd exited 255` (state и lock не созданы). Для A, B, C перед установкой: ожидание `cloud-init status --wait` и reboot в обновлённое ядро `7.0.0-42`.
4. **Дефект kit-v1, исправлен до зачётных прогонов A.** `snapshot.py` читал `/swapfile` (2 GiB) целиком в память на VPS с 2 GiB RAM: memory pressure `full avg300 ≈ 53%`, зависание и panic `warp-svc` (systemd restart, `NRestarts=1`), плюс недетерминированный хэш активного swap. Первая серия A (R01–R04) отброшена и сохранена отдельно. Kit-v2: для `/swapfile` сравниваются mode/uid/size/активность в `swapon`, остальные файлы хэшируются потоково; добавлен тест (selftest 10/10). Снимок — 1,1 с вместо ~2 мин. D не затронут: v0.3.4 не создаёт swap.
5. **C, первая попытка.** На прежнем FQDN Let's Encrypt вернул `429 rateLimited` (5 сертификатов на идентичный набор имён за 168 ч). Установка остановилась в `FAILED/CERTIFICATE` после успешного WARP adopt; установщик корректно классифицировал отказ, указал время повтора и сохранил прошлые этапы. Зачётная C выполнена на переустановленной ОС с новым FQDN; первая попытка в результаты не входит.

## Результаты по нодам

### D · опубликованный v0.3.4, Doctor кандидата из архива и bootstrap (kit-v1, swap отсутствует)

| Run | Profile | Mode | Flavour | overallStatus | Verdict | Failed / inconclusive |
|---|---|---|---|---|---|---|
| L01-local | D | local | basic | DEGRADED | PASS | — |
| L02-local-deep | D | local | deep | DEGRADED | PASS | — |
| L03-bootstrap | D | bootstrap | basic | DEGRADED | PASS | — |
| L04-bootstrap-deep | D | bootstrap | deep | DEGRADED | PASS | — |
| L05-bootstrap-negatives | — | bootstrap negatives | — | — | PASS | — |
| L06-faults | — | faults | — | — | PASS | — |
| L07-local-after-reboot | D | local | basic | UNHEALTHY | PASS | — |

### A · кандидат, новый WARP (kit-v2)

| Run | Profile | Mode | Flavour | overallStatus | Verdict | Failed / inconclusive |
|---|---|---|---|---|---|---|
| R01-bob | A | bob | basic | HEALTHY | PASS | — |
| R02-bob-deep | A | bob | deep | HEALTHY | PASS | — |
| R03-local | A | local | basic | HEALTHY | PASS | — |
| R04-local-deep | A | local | deep | HEALTHY | PASS | — |
| R05-bootstrap | A | bootstrap | basic | HEALTHY | PASS | — |
| R06-bob-repeat | A | bob | basic | HEALTHY | PASS | — |
| R07-faults | — | faults | — | — | PASS | — |
| R08-bob-after-reboot | A | bob | basic | UNHEALTHY | PASS | — |
| R09-bob-deep-after-reboot | A | bob | deep | UNHEALTHY | PASS | — |

### B · кандидат `--no-warp` (kit-v2)

| Run | Profile | Mode | Flavour | overallStatus | Verdict | Failed / inconclusive |
|---|---|---|---|---|---|---|
| R01-bob | B | bob | basic | HEALTHY | PASS | — |
| R02-bob-deep | B | bob | deep | HEALTHY | PASS | — |
| R03-local | B | local | basic | HEALTHY | PASS | — |
| R04-local-deep | B | local | deep | HEALTHY | PASS | — |
| R05-bootstrap | B | bootstrap | basic | HEALTHY | PASS | — |
| R06-bob-repeat | B | bob | basic | HEALTHY | PASS | — |
| R07-faults | — | faults | — | — | PASS | — |
| R08-bob-after-reboot | B | bob | basic | UNHEALTHY | PASS | — |
| R09-bob-deep-after-reboot | B | bob | deep | UNHEALTHY | PASS | — |

### C · официальный `cloudflare-warp` (proxy 127.0.0.1:40000, `warp=on`) + кандидат `--adopt-warp`

| Run | Profile | Mode | Flavour | overallStatus | Verdict | Failed / inconclusive |
|---|---|---|---|---|---|---|
| R01-bob | C | bob | basic | HEALTHY | PASS | — |
| R02-bob-deep | C | bob | deep | HEALTHY | PASS | — |
| R03-local | C | local | basic | HEALTHY | PASS | — |
| R04-local-deep | C | local | deep | HEALTHY | PASS | — |
| R05-bootstrap | C | bootstrap | basic | HEALTHY | PASS | — |
| R06-bob-repeat | C | bob | basic | HEALTHY | PASS | — |
| R07-faults | — | faults | — | — | PASS | — |
| R08-bob-after-reboot | C | bob | basic | UNHEALTHY | PASS | — |
| R09-bob-deep-after-reboot | C | bob | deep | UNHEALTHY | PASS | — |

Официальный клиент `warp-cli 2026.8.2115.0` из `pkg.cloudflareclient.com`: новая регистрация, `mode proxy`, `proxy port 40000`, listener только `127.0.0.1:40000`, trace через SOCKS5 `warp=on`, напрямую `warp=off`. Установка кандидата: `HEALTHY_LOCAL`, `warp_mode=adopt`, `resume_count=0`; `warp-svc` за всю серию не перезапускался (`NRestarts=0`).

## Наблюдения

| Факт | A | B | C | D |
|---|---|---|---|---|
| `state['doctor_cli']` | `installed`, `/usr/local/bin/bob` | `installed`, `/usr/local/bin/bob` | `installed`, `/usr/local/bin/bob` | n/a; `bob` и `/usr/local/lib/bob-auto-xray` не появились ни в одном прогоне |
| IPv6 до reboot (оракул → Doctor) | 0 адресов, all disabled → `PASS` | то же → `PASS` | то же → `PASS` | 0 адресов, all disabled → `WARN` (networkd drop-in старого формата v0.3.4) |
| IPv6 после reboot (оракул → Doctor) | link-local на `ens3`, не all disabled → `FAIL`, итог `UNHEALTHY` | то же | то же | то же |
| Хэши WARP `reg.json` / `settings.json` неизменны | да, все прогоны | n/a | да, все прогоны (adopt-регистрация оператора не тронута) | да |
| WARP `conf.json` неизменен | да (kit-v2) | n/a | да | да |
| PHP-сессии после `--deep` | +1 за каждый deep-прогон | +1 | +1…+3 | +1 |
| Bootstrap | stderr — 2 строки прогресса, 1 загрузка через shim | то же | то же | то же; 4 негативных случая — Doctor не запущен, загрузок 0/1/1/0 |
| `probe_faults` | 8/8 | 8/8 | 8/8 | 4/4 применимых, 4 сценария схемы HTTPS/443 пропущены |
| Стабильность ID по режимам | да | да | да | да |

PHP-сессии в strict-сравнение не входят: их создаёт и вход `deep.panel`, и внешние обращения к странице-заглушке, поэтому прирост между прогонами возможен без участия Doctor.

Секреты: в артефактах нет FQDN, IP и путей панели; из строковых значений state в артефактах встречаются только несекретные `doctor_cli.status`, `last_completed_stage`, `php_service`, `xray_sha256`.

## Вывод

- E2E Doctor: **PASS** — на обязательных A и D и на рекомендуемых B и C все зачётные прогоны PASS, INCONCLUSIVE нет, ID стабильны по режимам и повторам, контролируемое состояние не менялось, утечек нет.
- Находки:
  - После reboot IPv6 снова включается на `ens3` (link-local) на v0.3.4 и на кандидате 0.3.8 — известный блокер; Doctor честно даёт `FAIL` (R08/R09, L07).
  - P3 (установщик, вне PR #11): preflight не ждёт завершения cloud-init/dpkg; на свежей VPS это даёт непонятный отказ `sshd exited 255`.
  - Kit: дефект kit-v1 с хэшированием `/swapfile` (исправлен, см. отклонение 4).
- **NOT TESTED:** E (v0.3.6), продление сертификата, внешний VLESS-клиент, реальная доставка bootstrap по сети (только shim), arm64, Ubuntu 22.04/24.04.

SHA256 архивов артефактов (хранятся вне Git, права 0600): D `d50bcc1c11b15b929cc1903bae233fa427b726bdcc11b594a2fb6d11ed33088f` · A `0f1edf2d19fcfd9d5e17fd1791b72aa7939e3540e2f089f30224238e308ec04f` · A kit-v1 (отброшено) `bb3eaf44f18a070bd64e430064355f701172a5d64d0bddf70998d7f7fbdfb86b` · B `84bb9b14e4e41954969bdcb13f974a90dd51c57e63dbbee4ef91a82ae5f9591c` · C `9a74f0619389a88a442b874728ac3167a838cea0bfe975c7232894ea2934a9f3`
