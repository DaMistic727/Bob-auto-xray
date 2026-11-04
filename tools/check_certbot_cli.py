"""CI, inside ubuntu:24.04 / ubuntu:26.04 with the packaged certbot (run as root).

Proves that the distribution's certbot accepts Bob's renewal dry-run command
line and that its packaged cli.ini sets no hooks. No ACME request is made: the
lineage does not exist, so certbot stops after parsing the arguments.
"""
import re
import subprocess
from pathlib import Path

from selfsni.provision import CERTBOT_CLI_INI, RENEW_HOOK, renewal_command

version = subprocess.run(['certbot', '--version'], capture_output=True, text=True, check=True)
print((version.stdout + version.stderr).strip())
hook = Path(RENEW_HOOK)
hook.parent.mkdir(parents=True, exist_ok=True)
hook.write_text('#!/bin/sh\nexit 0\n')
hook.chmod(0o700)
result = subprocess.run(renewal_command('ci.invalid'), capture_output=True, text=True, timeout=120)
output = result.stdout + result.stderr
print(output.strip())
assert result.returncode != 0, 'certbot unexpectedly renewed a lineage that does not exist'
assert not re.search(r'unrecognized arguments|error: argument|usage:', output), 'argument rejected'
assert 'No certificate found with name ci.invalid' in output, 'certbot did not reach lineage lookup'
for path in map(Path, CERTBOT_CLI_INI):
    if path.exists():
        text = path.read_text()
        print(f'{path}:\n{text.strip()}')
        assert not re.search(r'^\s*(?:pre|post|deploy|renew)[-_]hook\s*=', text, re.M | re.I), \
            f'{path} sets certbot hooks; check_renewal_config() would refuse every installation'
print('PASS: packaged certbot accepts the Bob renewal dry-run command line')
