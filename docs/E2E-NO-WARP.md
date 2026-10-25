# VPS E2E: `--no-warp` and `--adopt-warp`

Status: `--no-warp` installation with pinned Xray 26.10.21, reboot, external REALITY and corrected native GeoData rollback **PASSED** on a disposable Ubuntu 26.04.1 VPS. Controlled APT and Certbot command failures and subsequent resume also **PASSED**. `--adopt-warp` clean installation with pre-existing official WARP, unchanged registration/settings/PID during install, external REALITY/WARP routing and reboot **PASSED** on a separately reset VPS. A real Certbot duplicate-certificate limit was observed on an earlier adopt attempt, but resume after that real limit and a real APT outage remain **NOT TESTED**. See [TESTING.md](TESTING.md) for evidence. Run remaining cases only on fresh disposable Ubuntu 26.04.1 VPS images. Keep the two modes on separate fresh images. Do not use a production server. Record UTC timestamps, Bob commit, boot ID, installed versions, and redacted evidence. Never publish `/var/lib/selfsni/state.json`, private keys, or panel credentials.

## Access and preparation

1. Confirm in the VPS control panel that the instance is disposable and may be reimaged, have UFW changed, and be rebooted. Verify the current SSH ED25519 fingerprint through that panel before accepting a changed key. Use strict SSH host checking.
2. Point one unproxied DNS A record exclusively to the VPS public IPv4. Confirm TCP 80 and 443 are reachable from the internet and that Certbot rate limits permit a production certificate. Do not install on a host with existing nginx, 3x-ui, Bob, or another service on required ports.
3. Transfer an archive of the reviewed Bob commit and local changes to `/root/selfsni` over the verified SSH connection; preserve the source tree and `versions.lock.json`. Check its SHA256 on both ends. Do not use a floating VPS-Server-Menu branch or fork. The `install` WARP mode remains intentionally blocked while upstream PR #6 is unmerged.
4. On the VPS, record `cat /etc/os-release`, `uname -a`, `cat /proc/sys/kernel/random/boot_id`, `ss -H -lntp`, `systemctl is-active warp-svc`, and the DNS A answer before installation. The no-WARP image should have no Cloudflare WARP package or service.

## Fresh no-WARP image

Run from `/root/selfsni` as root, substituting the verified domain:

```sh
export PYTHONPATH=/root/selfsni/src
python3 -m selfsni.provision --domain vpn.example.com --no-warp --preflight-only
python3 -m selfsni.provision --domain vpn.example.com --no-warp
python3 -m selfsni.acceptance --e2e --geodata-e2e
```

Save the command exit codes and redacted report. Check `state.json` has `warp_mode=skip`, `status=HEALTHY_LOCAL`, and `xui_version=v3.10.2`. Run `xray run -test -config /usr/local/x-ui/bin/config.json` with the installed binary named in `state.json`. Check that runtime outbounds and routes have no `warp-outbound`, and `systemctl cat x-ui` has no `warp-svc.service`. Confirm no Cloudflare package, WARP keyring/repository added by Bob, WARP listener, or WARP service dependency. Verify nginx, Certbot certificate and renewal dry run, native GeoData sources and scheduled reload, BBR/FQ, UFW, Fail2ban `sshd` and `3x-ipl`, panel and Xray versions, DNS resolution, HTTPS decoy, and public TCP/443. The on-host acceptance covers most of these; retain separate evidence for the remaining package/network checks.

Run `tools/external_acceptance.py` on an **independent** machine with a verified SSH connection, a compatible Xray binary, `--host root@<VPS-IP>`, `--control-path <verified-control-socket>`, `--known-hosts <verified-known-hosts>`, `--xray <local-xray-path>`, and `--output <redacted-report-path>`. It creates and removes a temporary REALITY client and checks HTTPS, DNS through the tunnel, DIRECT egress, blocked destinations, and TCP/443. If no independent client is available, mark external REALITY **NOT TESTED**.

Record the boot ID, reboot the disposable VPS, wait for SSH to return, and require a changed boot ID. Repeat on-host acceptance, actual Xray config test, service checks, TCP/443, HTTPS decoy, DNS, DIRECT egress, and external REALITY test. Confirm zero permanent clients remain. A successful install before reboot does not cover this step.

## Adopt image

Restore a fresh disposable image. Install and verify an operator-owned WARP separately: active `warp-svc`, proxy mode on loopback port 40000, `Connected`, and a SOCKS Cloudflare trace with `warp=on`. Record service MainPID, package version, registration file hash (keep the file private), settings JSON hash, and unit state before Bob. Then run preflight, installation, and on-host acceptance with `--adopt-warp`. The Bob installer must not call registration, connect, mode/port settings, service restart, or global APT upgrade. Compare PID and service start time during installation; compare registration/settings hashes and package version both after installation and after reboot. PID and start time normally change at reboot. Repeat acceptance and external client tests after reboot. A pre-existing WARP that is inactive or incompatible must fail before Bob makes system changes.

## Failure and resume matrix

Use independent disposable snapshots for each injected failure. Capture `state.json` stage/status and root-only diagnostic log; restore network and DNS before retry. Do not weaken validation or edit the state to force success.

| Case | Expected result |
| --- | --- |
| APT update network failure | Bounded retries, `FAILED/APT_UPDATE`, no later managed components; same-mode retry resumes with credentials unchanged. |
| APT package failure | Explicit APT category; only a proven safe early stage may resume. Partial/ambiguous package installation must be rejected. |
| DNS absent or wrong A | Read-only preflight failure before state, lock, or APT. |
| Certbot unreachable/ACME error | `FAILED/CERTIFICATE`; after the cause is fixed, same-domain/same-mode retry reuses identity and ports. |
| `--adopt-warp` with inactive or incompatible WARP | Refusal with no WARP mutation; verify PID, registration and settings remain unchanged. |
| Re-run after `HEALTHY_LOCAL` | Refusal without reinstallation. |
| Resume with changed WARP mode or domain | Refusal without modifying saved identity. |

For every case, classify each check as **PASSED**, **FAILED**, or **NOT TESTED**, include command exit status and redacted evidence, and leave production-ready unclaimed until both full images, failure cases, external client, and post-reboot checks pass.
