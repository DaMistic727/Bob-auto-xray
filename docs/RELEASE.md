# Релизы и установка

Репозиторий: [DaMistic727/Bob-auto-xray](https://github.com/DaMistic727/Bob-auto-xray). Release assets публикуются в [Releases](https://github.com/DaMistic727/Bob-auto-xray/releases).

## Versioned bootstrap

```bash
bash <(curl -fsSL --proto '=https' https://github.com/DaMistic727/Bob-auto-xray/releases/download/v0.4.3/install.sh)
```

Эта команда запускается от root на чистой поддерживаемой VPS. Она использует bootstrap **конкретного релиза**, а не плавающей ветки. Опубликованный `install.sh` содержит URL и SHA256 своего payload archive. Проверка digest выполняется до распаковки и исполнения модулей; archive traversal, symlinks и devices запрещены.

Bootstrap получен по HTTPS от GitHub. Встроенный SHA256 проверяет соответствие архива этому bootstrap, но не является отдельной подписью владельца. Доверие к самому bootstrap зависит от источника его получения и GitHub-аккаунта. Для предварительного просмотра и ручной проверки:

```bash
curl -fL --proto '=https' -o install.sh https://github.com/DaMistic727/Bob-auto-xray/releases/download/v0.4.3/install.sh
curl -fL --proto '=https' -o SHA256SUMS https://github.com/DaMistic727/Bob-auto-xray/releases/download/v0.4.3/SHA256SUMS
# Сравните SHA256 с доверенным значением релиза; просмотр кода не требует root.
sha256sum install.sh
less install.sh
bash ./install.sh
```

SHA256SUMS, полученный с того же источника, помогает обнаружить повреждение, но не заменяет независимую проверку происхождения.

## Локальный архив

Скачайте `selfsni-0.4.3.tar.gz`, проверьте его SHA256, распакуйте в `/root`:

```bash
tar -xzf selfsni-0.4.3.tar.gz -C /root
bash /root/selfsni/install.sh
```

Обновление только CLI `bob` на уже установленной ноде из того же архива (provision не запускается): `bash /root/selfsni/install.sh cli-upgrade --check`, затем без `--check`.

При локальном запуске из полноценного release directory загрузка модулей не требуется. Содержимое vendor/profiles дополнительно сверяется с `versions.lock.json`.

## WARP и флаги установки

Скопируйте и распакуйте проверенный release archive, затем:

```bash
bash /root/selfsni/install.sh --domain vpn.example.com --adopt-warp
bash /root/selfsni/install.sh --domain vpn.example.com --no-warp
bash /root/selfsni/install.sh --domain vpn.example.com
```

Без флагов выбирается новый WARP. `--adopt-warp` устанавливает или проверяет совместимое официальное меню, затем вызывает только read-only `server-menu warp health --port 40000 --json` и требует active/Connected, proxy mode, loopback SOCKS5 и реальный `warp=on`; несовместимый WARP не меняется. `--no-warp` не устанавливает и не проверяет WARP, не создаёт его outbound и не меняет уже установленный сторонний WARP. Оба флага взаимоисключающие. Мастер предлагает adopt только при обнаружении активного существующего WARP; через CLI режим можно выбрать явно.

Параметры: `--domain`, `--email`, `--ssh-port`, `--adopt-warp`, `--no-warp`, `--preflight-only`. При non-interactive запуске `--domain` обязателен. `NO_COLOR=1` отключает ANSI-цвета. `--preflight-only` строго read-only.

Повтор после сбоя и ограничения resume — в [OPERATIONS.md](OPERATIONS.md#повтор-после-сбоя-установки).

## Сборка разработчиком

Версия хранится в `VERSION`. Воспроизводимая сборка исходного архива:

```bash
python3 tools/package.py
```

Для полного набора release assets:

```bash
python3 tools/release.py --tag "v$(cat VERSION)"
(cd dist && sha256sum -c SHA256SUMS)
```

Результат: `dist/selfsni-<VERSION>.tar.gz`, `install.sh` с закреплённым digest, `SHA256SUMS` и release notes. Два запуска на одинаковых файлах дают одинаковые SHA256. Права внутри архива нормализованы до 0644/0755 по executable bit Git; локальные права 0600 для очищенных отчётов не меняют digest. В архив входят исходники, документация, artwork и upstream licenses; `.git`, research checkout, локальные кэши, state/DB и credentials не включаются. Опубликованные acceptance reports очищены от адресов личной VPS и boot IDs.

Тег `v<VERSION>` запускает workflow **Release**: тесты → ShellCheck → сборка → проверка SHA256 → публикация preview. Изменять уже опубликованный тег/asset не следует; для исправлений выпускается новая версия.

## VPS-Server-Menu: актуальная main и исторический pin

С v0.3.8 Bob получает актуальную официальную `Diman6267/VPS-Server-Menu/main` при установке в режимах install/adopt. [`versions.lock.json`](../versions.lock.json) задаёт репозиторий, но больше не содержит menu commit/SHA256. Bob проверяет архив и checkout до APT; официальный installer создаёт команды, а локальная main отслеживает origin/main для штатного updater (пункт 9). Фактические SHA и время сохраняются в root-only state `menu_revision`.

Dirty/diverged/неизвестный checkout отклоняется без reset/clean; старый detached checkout и форк автоматически не мигрируют. Для checkout применяется локальный `core.filemode=false`, поскольку официальный installer меняет executable bits. Это не разрешает изменения содержимого или посторонние файлы. Xray, 3x-ui, начальные GeoData и клиентские профили остаются закреплёнными; серверные GeoData затем обновляются штатно.

Официальная main изменяема: следующая ревизия может быть несовместима. Bob в таком случае прекращает установку без скрытого fallback к историческому pin. Проверка конкретной ревизии в CI не гарантирует совместимость будущих изменений upstream.

### История v0.3.4

В v0.3.4 в `versions.lock.json` были закреплены официальный VPS-Server-Menu commit `3ed5166bbc5b4fb4cc3bb26616bc4db3bbe2734c` и SHA256 codeload-архива `9d7a2aced39fe206a9cb4cd46481ee6876471458a63988b9fca152238fa7ec29`. Его 25 файлов побайтно сверены с отдельным Git checkout этого commit. Тогда Bob проверял архив до системных изменений и отказывал при неизвестном origin/HEAD/изменённом checkout. Миграция форка выполняется только вручную. Smoke E2E #1 подтвердил новую установку официального WARP, Certbot, REALITY Vision и on-host acceptance 27/27 на чистой одноразовой Ubuntu 26.04.1 amd64; владелец отдельно проверил VPN и WARP вручную. Позднее добавленное отключение IPv6 прошло reboot-тест на уже установленной VPS. Чистая установка с этим изменением, независимый автоматизированный внешний E2E финального кандидата v0.3.4 и reboot сразу после его чистой установки **не были проверены**. Это был Preview release; ограничения подробно описаны в [отчёте](TESTING.md).

Эти результаты относятся к v0.3.4. Проверки v0.4.0 и ограничения v0.4.3 приведены отдельно в [TESTING.md](TESTING.md).

## Acceptance

Локальные команды — в [CONTRIBUTING.md](../CONTRIBUTING.md). На одноразовой VPS с установленным проектом:

```bash
PYTHONPATH=/root/selfsni/src python3 -m selfsni.acceptance --e2e
PYTHONPATH=/root/selfsni/src python3 -m selfsni.acceptance --geodata-e2e
```

`--e2e` временно создаёт Vision client через актуальный clients API и удаляет его в `finally`. `--geodata-e2e` временно включает минутный native cron, проверяет четыре загрузки, отказ и rollback, затем восстанавливает production schedule. Обычная установка создаёт постоянного клиента `admin`; E2E создаёт отдельного временного клиента и проверяет сохранность постоянных. Внешний тест `tools/external_acceptance.py` использует SSH key/agent/ControlMaster и официальный совместимый Xray; пароль SSH инструмент не принимает и не сохраняет. Эти действия не запускаются на production VPS.
