#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
# Local verified release: bash /path/to/release/install.sh
# Remote: download this bootstrap and supply a release URL + independently
# obtained SHA256; never download an unsigned checksum beside an unknown script.
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f "$script_dir/versions.lock.json" && -f "$script_dir/src/selfsni/provision.py" ]]; then
    export PYTHONDONTWRITEBYTECODE=1
    export PYTHONPATH="$script_dir/src"
    exec python3 -m selfsni.provision "$@"
fi
: "${SELFSNI_RELEASE_URL:?Set an immutable HTTPS release archive URL}"
: "${SELFSNI_RELEASE_SHA256:?Set the independently verified release SHA256}"
[[ $SELFSNI_RELEASE_URL == https://* ]] || exit 2
[[ $SELFSNI_RELEASE_SHA256 =~ ^[a-f0-9]{64}$ ]] || exit 2
work=$(mktemp -d)
download_pid=''
cleanup() {
    if [[ -n $download_pid ]]; then
        kill "$download_pid" 2>/dev/null || true
        wait "$download_pid" 2>/dev/null || true
    fi
    rm -r -- "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
printf 'Загрузка проверенного архива релиза...\n' >&2
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 180 \
    "$SELFSNI_RELEASE_URL" -o "$work/release.tar.gz" &
download_pid=$!
if [[ -t 2 ]]; then
    ticks=0
    while kill -0 "$download_pid" 2>/dev/null; do
        printf '\rЗагрузка продолжается... %02d:%02d' "$((ticks / 60))" "$((ticks % 60))" >&2
        sleep 1
        ticks=$((ticks + 1))
    done
    printf '\r%50s\r' '' >&2
fi
wait "$download_pid"
download_pid=''
printf '%s  %s\n' "$SELFSNI_RELEASE_SHA256" "$work/release.tar.gz" | sha256sum -c - >/dev/null
printf 'Архив загружен, SHA256 подтверждён.\n' >&2
python3 - "$work" <<'PY'
from pathlib import Path
import sys, tarfile
root = Path(sys.argv[1]).resolve()
with tarfile.open(root / 'release.tar.gz') as tf:
    for m in tf.getmembers():
        path = (root / m.name).resolve()
        if root not in path.parents or m.issym() or m.islnk() or m.isdev():
            raise SystemExit('Unsafe release archive')
    tf.extractall(root)
PY
bash "$work/selfsni/install.sh" "$@"
