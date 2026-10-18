# Участие в разработке

Изменения обсуждаем через Issues и Pull Requests этого репозитория. Для ошибок и предложений доступны отдельные формы. Сначала опишите наблюдаемое поведение и ожидаемый результат.

## Локальная проверка

Требуются Python 3.10+, Bash, GNU sha256sum и ShellCheck. Python-зависимостей для unit-тестов нет.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
shellcheck install.sh
bash -n install.sh
bash -n vendor/3x-ui/install.sh
bash -n vendor/3x-ui/x-ui.sh
python3 -m compileall -q src tools tests
python3 tools/package.py
```

Не запускайте provisioning, acceptance с `--e2e` или `remote_fixture.py` на рабочем сервере. Эти проверки требуют одноразовой VPS; e2e временно создаёт клиента и перезапускает x-ui.

## Pull Request

- Создайте ветку от `main` и опишите проблему, изменение поведения и проверку.
- Новые тесты должны проверять реальные риски и контракты, а не повторять реализацию.
- При обновлении upstream обновите lock, bundled files, SHA256 и отчёт о совместимости.
- Не добавляйте credentials, private keys, реальные subscription URLs, state/DB, адреса личных серверов или неочищенные логи. Примеры используют `example.com` и документальные IP-диапазоны.
- Не заменяйте проверенные результаты предположениями; отмечайте непроверенные ОС и клиенты.

Для локальной детерминированной сборки запустите `tools/package.py` дважды: digest архива должен совпасть. Порядок выпуска описан в [RELEASE.md](docs/RELEASE.md).
