# v0.3.5 Preview · проверки 31 октября 2026

## Выполнено локально

- Python 3.12 и 3.15: **173 tests, 172 PASS, 1 SKIP** каждый. Skip — реальный connection-refused probe: окружение macOS фильтрует отказ соединения. Классификация отказа дополнительно проверена детерминированными mocks. Тест не ослаблялся.
- IPv6: 12/12, включая изменение ens3 после применения, fail-closed и повторное применение, link-local, чужой generation policy, legacy upgrade/rollback, IPv4 SSH/AAAA guards и отказы sysctl/networkctl.
- Panel: существующие API/bootstrap/TLS/path/UFW/security регрессии; добавлен отказ readback для неправильных port/path и стандартной пары admin/admin. TLS сертификат/ключ проверяются до service marker.
- WARP install/adopt/skip проверены mocks; настоящий install не запускался. Routing, resume, GeoData, swap и BBR регрессии сохранены.
- compileall, Bash syntax для bootstrap и двух vendor shell scripts, ShellCheck bootstrap, `git diff --check`: PASS.
- gzip compresslevel=9 задан явно: найдена и исправлена зависимость digest от изменившегося default Python 3.15; проверен header XFL=2 и идентичность сборок Python 3.12/3.15.
- Две сборки из отдельных чистых экспортов дерева: идентичные SHA256SUMS и побайтово идентичные payload/bootstrap/notes; проверены SHA256, archive VERSION=0.3.5, отсутствие Doctor, небезопасных archive members и локальных state/key/debug файлов. Сгенерированный bootstrap содержит release URL v0.3.5 и SHA256 именно этого payload; ShellCheck/Bash syntax: PASS.
- Проверка целевых secret patterns в src/docs/README/CHANGELOG: совпадений нет. Скриншоты из истории PR #12 просмотрены: адресная строка/секретный путь, credentials и клиентские ключи отсутствуют.
- Сравнение с актуальным main: versions.lock.json, routing.py, install_state.py, warp_menu.py и bbr.py не меняются. PR #10/swap сохранён. PR #11/Doctor отсутствует.

## CI и публикация

Локально Nginx отсутствует; real Nginx harness выполняется в Ubuntu GitHub Actions на Python 3.10/3.12, по 7 групп на job. Результат относится только к конкретному SHA workflow. До тега обязательно дождаться успешного CI объединённого main. Релиз публикует существующий Release workflow после повторных проверок. Проверить опубликованные assets и сравнить их digest с локальными сборками.

Ссылки на прогоны финального кода следует смотреть по SHA тега [в GitHub Actions](https://github.com/DaMistic727/Bob-auto-xray/actions). Этот документ не переносит исторический зелёный CI PR #12 на новый commit.

## Границы

Реального Nginx локально нет; поддерживаемый Python 3.10 проверяется в CI. VPS, SSH, installer на development host, reboot и изменения firewall/network реальных машин не выполнялись. Чистую установку и reboot финального v0.3.5 проверяет владелец после публикации.

IPv6 persistence **NOT VERIFIED**: явный `IPv6LinkLocalAddressGenerationMode=none` — документированное усиление, а не доказательство устранения исторического P1. Модель ловит позднюю перезапись, но не устанавливает автора исторической записи и не доказывает persistence Linux/networkd. См. [разбор](IPV6-PERSISTENCE.md), [полный список NOT TESTED и ручной E2E](TESTING.md).
