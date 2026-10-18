#!/usr/bin/env python3
"""Build a deterministic source release; ship digest out of band."""
import gzip
import hashlib
import io
import re
from pathlib import Path
import tarfile

root=Path(__file__).resolve().parents[1]
version=(root/'VERSION').read_text().strip()
if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version):
    raise SystemExit('VERSION must contain MAJOR.MINOR.PATCH')
out=root/'dist'
out.mkdir(exist_ok=True)
files=[root/'install.sh',root/'versions.lock.json']
for name in ['src','templates','profiles','vendor','tools','tests','docs','assets','.github']:
    files.extend(p for p in (root/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
for name in ['README.md','LICENSE','VERSION','CHANGELOG.md','CONTRIBUTING.md','SECURITY.md','THIRD_PARTY_NOTICES.md','.gitignore','.gitattributes','.editorconfig']:
    if (root/name).exists(): files.append(root/name)
buffer=io.BytesIO()
with tarfile.open(fileobj=buffer,mode='w') as tf:
    for p in sorted(files):
        info=tf.gettarinfo(str(p),arcname='selfsni/'+str(p.relative_to(root)))
        info.uid=info.gid=0
        info.uname=info.gname='root'
        info.mtime=0
        with p.open('rb') as f: tf.addfile(info,f)
archive=out/f'selfsni-{version}.tar.gz'
with archive.open('wb') as f:
    with gzip.GzipFile(filename='',mode='wb',fileobj=f,mtime=0) as gz: gz.write(buffer.getvalue())
sha=hashlib.sha256(archive.read_bytes()).hexdigest()
(archive.with_suffix('.gz.sha256')).write_text(sha+'  '+archive.name+'\n')
print(archive)
print(sha)
