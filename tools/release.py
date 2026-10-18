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
(root / 'dist/release-notes.md').write_text(f'''Preview-релиз Bob Auto Xray {a.tag}.

3x-ui + VLESS TCP REALITY Vision Self-SNI, nginx/PHP portal, selective WARP, native Geodata Auto-Update, TCP BBR с FQ и отдельные профили Happ/INCY.

Исправлен BBR preflight для минимальных образов без modinfo: read-only probe распознаёт tcp_bbr в дереве модулей запущенного ядра, а kmod устанавливается до возможного modprobe. Исправление покрыто regression-тестом.

Кандидат из PR #6 прошёл clean install Ubuntu 26.04.1 amd64, новую регистрацию WARP, BBR/FQ, REALITY Target в API/DB/web editor/runtime, geodata hot reload/rollback, внешний Vision-клиент и reboot: 26/26 on-host и 7/7 external checks до и после перезагрузки. Этот E2E выполнен на кандидате PR #6, а не на опубликованном архиве {a.tag}. Happ/INCY проверены пользователем на физическом iPhone; Android, DNS leaks и отдельные UDP/QUIC-сценарии требуют проверки.

## Установка от root на чистой VPS

```bash
bash <(curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' https://github.com/{a.repo}/releases/download/{a.tag}/install.sh)
```

Bootstrap закрепляет URL и SHA256 архива. В assets опубликован SHA256SUMS; модель доверия и проверка вручную описаны в документации.

- [README](https://github.com/{a.repo}/blob/{a.tag}/README.md)
- [Отчёт о тестировании](https://github.com/{a.repo}/blob/{a.tag}/docs/TESTING.md)
- [Архитектура](https://github.com/{a.repo}/blob/{a.tag}/docs/ARCHITECTURE.md)

SHA256 payload: `{checksum}`
SHA256 bootstrap: `{bootstrap_sha}`
''')
print('Release bootstrap:', installer)
print('Payload SHA256:', checksum)
print('Bootstrap SHA256:', bootstrap_sha)
