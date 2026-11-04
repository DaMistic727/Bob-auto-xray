"""Files that keep nginx and x-ui on the current Let's Encrypt certificate.

Shared by the installer and Bob Doctor, so both judge the same content.
"""
RENEW_HOOK = '/etc/letsencrypt/renewal-hooks/deploy/50-selfsni'
# Installed up to v0.4.2; still a working hook on existing nodes.
LEGACY_HOOK_SCRIPT = '#!/bin/sh\nset -eu\nnginx -t\nsystemctl reload nginx\nsystemctl restart x-ui\n'
# The stamp is written only after every TLS user has loaded the certificate.
RELOAD_STAMP = '/var/lib/selfsni/tls-reloaded'
RENEW_HOOK_SCRIPT = LEGACY_HOOK_SCRIPT + 'touch ' + RELOAD_STAMP + '\n'
RETRY_DROPIN = '/etc/systemd/system/certbot.service.d/50-selfsni.conf'


def retry_dropin(domain, live='/etc/letsencrypt/live', stamp=RELOAD_STAMP, hook=RENEW_HOOK):
    """certbot.service drop-in: rerun the hook after every certbot run while needed.

    Certbot runs a deploy hook once, right after a renewal, and only logs its
    failure; the next renewal is ~60 days away. While the live certificate is
    newer than the last successful reload, each run of certbot.timer (twice a
    day) repeats the hook, and a failure leaves certbot.service failed.
    ExecStopPost also runs when certbot itself fails on another lineage.
    """
    # systemd turns $$ into $ before /bin/sh sees the command.
    return ('[Service]\n'
            '# Bob Auto Xray: reload nginx and x-ui until they use the renewed certificate.\n'
            f"ExecStopPost=/bin/sh -c 'cert=$$(readlink -f {live}/{domain}/fullchain.pem); "
            f'if [ "$$cert" -nt {stamp} ]; then exec {hook}; fi\'\n')
