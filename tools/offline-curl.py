#!/usr/bin/python3
"""Network-free curl adapter for the unmodified, pinned official installer.
Only resources pre-fetched and verified against versions.lock.json are served.
"""
import json
import os
from pathlib import Path
import shutil
import sys

args = sys.argv[1:]
cache = json.loads(Path(os.environ['SELFSNI_CURL_MAP']).read_text())
urls = [x for x in args if x.startswith('https://')]
if len(urls) != 1 or urls[0] not in cache:
    sys.exit(22)
source = Path(cache[urls[0]])
output, writeout = None, None
for i, arg in enumerate(args):
    if arg == '-o' or (arg.startswith('-') and not arg.startswith('--') and arg.endswith('o')):
        output = args[i + 1]
    if arg == '-w':
        writeout = args[i + 1]
if output and output != '/dev/null':
    shutil.copyfile(source, output)
elif not output:
    sys.stdout.buffer.write(source.read_bytes())
if writeout:
    sys.stdout.write(writeout.replace('%{http_code}', '200'))
