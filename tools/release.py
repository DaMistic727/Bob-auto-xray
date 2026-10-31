#!/usr/bin/env python3
"""Build the release archive and a bootstrap pinned to its exact SHA256."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--repo', default='DaMistic727/Bob-auto-xray')
p.add_argument('--tag', required=True)
a = p.parse_args()
version = (root / 'VERSION').read_text().strip()
if a.tag != 'v' + version or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', a.repo):
    raise SystemExit('Tag must match VERSION; repo must be owner/name')
subprocess.run([sys.executable, str(root / 'tools/package.py')], check=True)
archive = root / 'dist' / f'selfsni-{version}.tar.gz'
checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
url = f'https://github.com/{a.repo}/releases/download/{a.tag}/{archive.name}'
bootstrap = (root / 'install.sh').read_text()
anchor = ': "${SELFSNI_RELEASE_URL:?Set an immutable HTTPS release archive URL}"'
if bootstrap.count(anchor) != 1:
    raise SystemExit('Bootstrap template changed; review release generator')
defaults = f': "${{SELFSNI_RELEASE_URL:={url}}}"\n: "${{SELFSNI_RELEASE_SHA256:={checksum}}}"\n'
bootstrap = bootstrap.replace(anchor, defaults + anchor)
installer = root / 'dist/install.sh'
installer.write_text(bootstrap)
installer.chmod(0o755)
bootstrap_sha = hashlib.sha256(installer.read_bytes()).hexdigest()
(root / 'dist/SHA256SUMS').write_text(f'{checksum}  {archive.name}\n{bootstrap_sha}  install.sh\n')
# Release-specific changes come from the matching changelog section, never
# from a previous release's hard-coded test narrative.
changelog = (root / 'CHANGELOG.md').read_text()
section = re.search(r'^## v' + re.escape(version) + r'[^\n]*\n(.*?)(?=^## |\Z)',
                    changelog, re.M | re.S)
if section is None:
    raise SystemExit('VERSION has no release section in CHANGELOG.md')
(root / 'dist/release-notes.md').write_text(f'''# Bob Auto Xray {a.tag} · Preview

{section.group(1).strip()}

## Проверки и ограничения

CI проверяет Python 3.10/3.12 unittest, real Nginx harness, pinned 3x-ui API/GeoData harness, compileall,
Bash syntax, ShellCheck и две воспроизводимые release-сборки с SHA256.
Release workflow повторяет локальные проверки перед публикацией assets.
Фактические результаты и границы проверок: [docs/TESTING.md](https://github.com/{a.repo}/blob/{a.tag}/docs/TESTING.md).

Чистая установка и reboot финального {a.tag} будут проверены вручную владельцем
после публикации. VPS в ходе подготовки этого релиза не использовались.
IPv6 persistence после reboot не подтверждена: явный запрет генерации link-local
и fail-closed проверки не являются доказательством устранения исторического P1.
Независимые внешний Vision, изоляция внутреннего порта, реальные мобильные
клиенты и продление сертификата этой сборки также NOT TESTED.
Bob Doctor (PR #11) не включён. Старые установки автоматически не мигрируют.

## Установка от root на чистой VPS

```bash
bash <(curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' https://github.com/{a.repo}/releases/download/{a.tag}/install.sh)
```

Bootstrap закрепляет immutable URL и SHA256 payload; SHA256SUMS опубликован в assets.
[README](https://github.com/{a.repo}/blob/{a.tag}/README.md) ·
[Архитектура](https://github.com/{a.repo}/blob/{a.tag}/docs/ARCHITECTURE.md)

SHA256 payload: `{checksum}`
SHA256 bootstrap: `{bootstrap_sha}`
''')
print('Release bootstrap:', installer)
print('Payload SHA256:', checksum)
print('Bootstrap SHA256:', bootstrap_sha)
