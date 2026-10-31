# VPS E2E: resume preflight (PR #17)

Обезличенный отчёт. Домен, IP, credentials, секретные пути и UUID не публикуются. Пошаговые доказательства: [e2e-pr17-resume.json](e2e-pr17-resume.json).

## Кандидат и окружение

| Поле | Значение |
|---|---|
| Commit | `6f71b5fae7fe558a0ee9165229ef8eeb46a945a8` |
| Архив | `selfsni-0.3.8.tar.gz`, SHA256 `f70f670ab6f22d2c2a9d3107d9fb678a9ed26f56f78c0b2b5e932f42de62808a`: детерминированная сборка коммита, совпадение на VPS проверено |
| ОС | Одноразовая чистая Ubuntu 26.04 amd64, ядро 7.0.0-30-generic, systemd 259, `cgroup2fs`, iproute2-6.19.0, nginx 1.28.3 |
| Режим | install: новая регистрация WARP, VPS-Server-Menu `852aae86d88dd694460ae6633bbac929df4b7a07`. Swap `created` |
| Дата | 2026-10-31, один boot, reboot не выполнялся |
| Отклонение | До теста владелец добавил drop-in `sshd_config.d`, отключающий вход по паролю (на хосте шёл перебор паролей). Путь не управляется Bob; preflight читает только порты из `sshd -T` |

## Метод

Production-код не менялся. Bob вызывает `apt-get`, `bash` (официальный installer меню) и `certbot` через `PATH`. Для отдельного запуска установщика в `PATH` добавлялся каталог с шимом, который вызывает ровно один сбой по точному шаблону аргументов. Во всех остальных случаях шим вызывает настоящий бинарник без изменений.

| Сбой | Эффект |
|---|---|
| `apt-install-after-nginx` | Настоящий `apt-get` ставит только nginx, затем код выхода 100 |
| `menu-after-install` | Официальный installer меню отрабатывает полностью, затем код выхода 1 |
| `certbot-dns` | Настоящий certbot не запускается (запросов к ACME нет), выводится NXDOMAIN, код выхода 1 |

Каждый шаг фиксирует до и после запуска обезличенный snapshot: SHA идентичности, SHA результатов этапов, файлы, listener'ы, `/proc`, cgroup. Кроме того, перед запуском вызывается read-only `owned_nginx_http()` из кода кандидата на живом хосте.

## Результаты

| Шаг | Сценарий | Итог |
|---|---|---|
| S0 | `--preflight-only` | PASS: система не изменена |
| S1 | Сбой APT_INSTALL после установки nginx | PASS: `FAILED/APT_INSTALL`, `resume_count=0` |
| N1a | `python3` на :80 при resume с APT_INSTALL | PASS: отказ `Foreign TCP listener on port 80` |
| N2a | `/usr/sbin/nginx` вне `nginx.service` на :80 | PASS: отказ |
| **S2** | Resume с APT_INSTALL при собственном nginx на :80, затем сбой меню | **PASS**: `FAILED/WARP`, `resume_count=1`, `ipv6/bbr/swap` в state |
| N3 | Посторонний файл в `conf.d` | PASS: отказ «Чужая nginx-конфигурация» |
| **S3** | Resume с WARP: реальные меню и WARP, затем сбой certbot | **PASS**: `FAILED/CERTIFICATE`, `resume_count=2` |
| N1b | `python3` на :80 при resume с CERTIFICATE | PASS: отказ (до PR #17 эта стадия разрешала любой процесс) |
| N2b | Поддельный nginx на :80 при resume с CERTIFICATE | PASS: отказ |
| S4 | Повторный resume с CERTIFICATE, снова сбой certbot | PASS: `resume_count=3` |
| **S5** | Финальный resume с настоящим certbot | **PASS**: `HEALTHY_LOCAL`, `resume_count=4`, acceptance 38/38 |
| S6 | Повторный запуск | PASS: отказ, установка уже `HEALTHY_LOCAL` |

Каждый сбой сработал ровно один раз на своём шаге.

Во всех негативных шагах и в S6 `state.json`, `install.log` и маркер `FAILED` остались побайтно неизменными. После каждого негативного шага nginx из пакета возвращался на :80.

## Инварианты

- SHA идентичности (`installation_id`, credentials, порты, `absent_paths`) одинаков от S1 до S6.
- SHA `ipv6`, `bbr` и `swap` не меняются с S2 по S5.
- SHA `menu_revision` не меняется с S3 по S5 и совпадает с `menuRevision` в acceptance.
- `resume_count` растёт строго 0→1→2→3→4.
- Acceptance выполнил проверку `Managed 2 GiB swap, when installed`. До исправления после resume с WARP или CERTIFICATE она молча пропускалась.

## Наблюдаемые свойства nginx на Ubuntu 26.04

- Пакет запускает `nginx.service` сразу после установки. `conf.d` пуст, `sites-enabled/default` — symlink на `sites-available/default`.
- `ss -H -lntp` выводит `0.0.0.0:80` и `[::]:80` с `users:(("nginx",pid=…,fd=…),…)` для master и worker'ов. `MainPID` юнита — master.
- У всех процессов `exe` = `/usr/sbin/nginx`, cgroup = `0::/system.slice/nginx.service`.
- Сокет `[::]:80` продолжает слушать после `disable_ipv6=1`.
- Поддельный nginx имеет тот же `exe`. Отличаются только cgroup (`bob-e2e-fake-nginx.service`) и `MainPID=0`; по ним он и отклонён.

## Вывод и границы

Resume PR #17 на реальной Ubuntu 26.04 amd64: **PASS**, дефектов не найдено.

То, что `origin/main` в S2 отказал бы, а в N1b/N2b пропустил чужой процесс, выведено из кода и RED-результатов unit-тестов. Старая сборка на этой VPS не запускалась.

**NOT TESTED:**
- путь bootstrap-загрузки;
- внешний клиент REALITY/Vision;
- reboot и IPv6 persistence;
- продление сертификата;
- режимы adopt и skip на VPS;
- arm64, Ubuntu 22.04 и 24.04;
- реальные сбои APT/DNS/ACME и прерванный dpkg: сбои внедрялись на уровне команд.
