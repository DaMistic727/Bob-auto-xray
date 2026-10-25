# Релизы и установка

Репозиторий: [DaMistic727/Bob-auto-xray](https://github.com/DaMistic727/Bob-auto-xray). Release assets публикуются в [Releases](https://github.com/DaMistic727/Bob-auto-xray/releases).

## Versioned bootstrap

```bash
bash <(curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' https://github.com/DaMistic727/Bob-auto-xray/releases/download/v0.3.4/install.sh)
```

Эта команда запускается от root на чистой поддерживаемой VPS. Она использует bootstrap **конкретного релиза**, а не плавающей ветки. Опубликованный `install.sh` содержит URL и SHA256 своего payload archive. Проверка digest выполняется до распаковки и исполнения модулей; archive traversal, symlinks и devices запрещены.

Bootstrap получен по HTTPS от GitHub. Встроенный SHA256 проверяет соответствие архива этому bootstrap, но не является отдельной подписью владельца. Доверие к самому bootstrap зависит от источника его получения и GitHub-аккаунта. Для предварительного просмотра и ручной проверки:

```bash
curl --fail --location --proto '=https' --proto-redir '=https' -o install.sh https://github.com/DaMistic727/Bob-auto-xray/releases/download/v0.3.4/install.sh
curl --fail --location --proto '=https' --proto-redir '=https' -o SHA256SUMS https://github.com/DaMistic727/Bob-auto-xray/releases/download/v0.3.4/SHA256SUMS
# Сравните SHA256 с доверенным значением релиза; просмотр кода не требует root.
sha256sum install.sh
less install.sh
bash ./install.sh
```

SHA256SUMS, полученный с того же источника, помогает обнаружить повреждение, но не заменяет независимую проверку происхождения.

## Локальный архив

Скачайте `selfsni-0.3.4.tar.gz`, проверьте его SHA256, распакуйте в `/root`:

```bash
tar -xzf selfsni-0.3.4.tar.gz -C /root
bash /root/selfsni/install.sh
```

При локальном запуске из полноценного release directory загрузка модулей не требуется. Содержимое vendor/profiles дополнительно сверяется с `versions.lock.json`.

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

В `versions.lock.json` закреплены официальный VPS-Server-Menu commit `3ed5166bbc5b4fb4cc3bb26616bc4db3bbe2734c` и SHA256 codeload-архива `9d7a2aced39fe206a9cb4cd46481ee6876471458a63988b9fca152238fa7ec29`. Его 25 файлов побайтно сверены с отдельным Git checkout этого commit. Bob проверяет архив до системных изменений и отказывает при неизвестном origin/HEAD/изменённом checkout. Миграция форка выполняется только вручную. Smoke E2E #1 подтвердил новую установку официального WARP, Certbot, REALITY Vision и on-host acceptance 27/27 на чистой одноразовой Ubuntu 26.04.1 amd64; владелец отдельно проверил VPN и WARP вручную. Позднее добавленное отключение IPv6 прошло reboot-тест на уже установленной VPS. Чистая установка с этим изменением, независимый автоматизированный внешний E2E финального кандидата и reboot сразу после его чистой установки **не проверены**. Это Preview release; ограничения подробно описаны в [отчёте](TESTING.md).

## Acceptance

Локальные команды — в [CONTRIBUTING.md](../CONTRIBUTING.md). На одноразовой VPS с установленным проектом:

```bash
PYTHONPATH=/root/selfsni/src python3 -m selfsni.acceptance --e2e
PYTHONPATH=/root/selfsni/src python3 -m selfsni.acceptance --geodata-e2e
```

`--e2e` временно создаёт Vision client через актуальный clients API и удаляет его в `finally`. `--geodata-e2e` временно включает минутный native cron, проверяет четыре загрузки, отказ и rollback, затем восстанавливает production schedule. Обычная установка не создаёт клиентов. Внешний тест `tools/external_acceptance.py` использует SSH key/agent/ControlMaster и официальный совместимый Xray; пароль SSH инструмент не принимает и не сохраняет. Эти действия не запускаются на production VPS.
