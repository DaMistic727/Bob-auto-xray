"""Bounded external port evidence; a TCP proxy may acknowledge connect locally."""
import hashlib
import socket
import ssl


def probe_panel_port(ipv4, port, domain, timeout=5, context=None, expected_certificate_sha256=None):
    """Never send HTTP, credentials or the secret path to an uncertain endpoint.

    PASS means an explicit refusal from this observation point, not a claim
    about every Internet route. Timeouts and failed/unverified TLS stay unknown.
    FAIL additionally requires the certificate fingerprint obtained over SSH
    from the VPS: a locally trusted TLS interceptor must not impersonate it.
    """
    result = {'status': 'INCONCLUSIVE', 'tcp': 'NOT_VERIFIED',
              'tls': 'NOT_ATTEMPTED', 'reason': 'TCP_ERROR'}
    try:
        raw = socket.create_connection((ipv4, port), timeout=timeout)
    except ConnectionRefusedError:
        return dict(result, status='PASS', tcp='REFUSED', reason='EXPLICIT_TCP_REFUSAL')
    except TimeoutError:
        return dict(result, tcp='TIMEOUT', reason='TCP_TIMEOUT')
    except OSError:
        return result
    with raw:
        result['tcp'] = 'CONNECTED'
        try:
            tls = context or ssl.create_default_context()
            if not tls.check_hostname or tls.verify_mode != ssl.CERT_REQUIRED:
                return dict(result, reason='TLS_VERIFICATION_REQUIRED')
            with tls.wrap_socket(raw, server_hostname=domain) as peer:
                if expected_certificate_sha256 is None:
                    return dict(result, tls='VERIFIED', reason='TLS_IDENTITY_NOT_CORRELATED')
                if hashlib.sha256(peer.getpeercert(binary_form=True)).hexdigest() != expected_certificate_sha256:
                    return dict(result, tls='VERIFIED_DIFFERENT_CERTIFICATE', reason='TLS_IDENTITY_NOT_CORRELATED')
                return dict(result, status='FAIL', tls='VERIFIED',
                            reason='EXTERNAL_TLS_LISTENER_VERIFIED')
        except ssl.SSLCertVerificationError:
            return dict(result, tls='UNVERIFIED_CERTIFICATE', reason='TLS_IDENTITY_NOT_VERIFIED')
        except TimeoutError:
            return dict(result, tls='TIMEOUT', reason='CONNECTED_WITHOUT_VERIFIED_TLS')
        except (OSError, ssl.SSLError):
            return dict(result, tls='FAILED', reason='CONNECTED_WITHOUT_VERIFIED_TLS')
