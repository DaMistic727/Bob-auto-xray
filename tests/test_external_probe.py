"""Probe classification with real loopback sockets/TLS, plus bounded failure injection.
These are local transport regressions, not proof of VPS Internet isolation.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from selfsni.exposure import probe_panel_port


class ProbeFailures(unittest.TestCase):
    def test_explicit_refusal_is_pass_from_this_observation_point(self):
        with patch('selfsni.exposure.socket.create_connection', side_effect=ConnectionRefusedError()):
            result = probe_panel_port('192.0.2.1', 21000, 'panel.test')
        self.assertEqual((result['status'], result['tcp']), ('PASS', 'REFUSED'))

    def test_tcp_timeout_stays_inconclusive(self):
        with patch('selfsni.exposure.socket.create_connection', side_effect=TimeoutError('secret-url')):
            result = probe_panel_port('192.0.2.1', 21000, 'panel.test')
        self.assertEqual((result['status'], result['tcp']), ('INCONCLUSIVE', 'TIMEOUT'))
        self.assertNotIn('secret-url', json.dumps(result))

    def test_other_socket_error_is_not_pass(self):
        with patch('selfsni.exposure.socket.create_connection', side_effect=OSError('api-token')):
            result = probe_panel_port('192.0.2.1', 21000, 'panel.test')
        self.assertEqual(result['status'], 'INCONCLUSIVE')
        self.assertNotIn('api-token', json.dumps(result))


class LocalTransportProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('openssl'):
            raise unittest.SkipTest('OpenSSL unavailable; real local TLS regressions NOT TESTED')
        cls.directory = tempfile.TemporaryDirectory(prefix='bob-probe-cert-')
        cls.root = Path(cls.directory.name)
        cls.cert, cls.key = cls.root/'cert.pem', cls.root/'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '1', '-subj', '/CN=panel.test', '-addext',
                        'subjectAltName=DNS:panel.test', '-keyout', str(cls.key),
                        '-out', str(cls.cert)], check=True, capture_output=True)
        cls.fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cls.cert.read_text())).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @contextlib.contextmanager
    def listener(self, mode='tls'):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        server.settimeout(3)
        done = threading.Event()
        observed = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        context.set_servername_callback(lambda sock, name, ctx: observed.append(name))
        def serve():
            try:
                with server.accept()[0] as raw:
                    raw.settimeout(2)
                    if mode == 'intercept':
                        raw.recv(4096)  # Acknowledge TCP, then swallow ClientHello.
                        done.wait(2)
                    elif mode == 'plain':
                        raw.recv(4096)
                        raw.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                    else:
                        with context.wrap_socket(raw, server_side=True):
                            pass
            except (OSError, ssl.SSLError):
                pass
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            yield server.getsockname()[1], observed
        finally:
            done.set()
            thread.join(4)
            server.close()
            self.assertFalse(thread.is_alive(), 'Test listener was not cleaned up')

    def trust(self):
        return ssl.create_default_context(cafile=str(self.cert))

    def test_real_connection_refused(self):
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))  # Reserved, deliberately not listening.
            result = probe_panel_port('127.0.0.1', reserved.getsockname()[1], 'panel.test')
        if result['tcp'] == 'TIMEOUT':
            self.assertEqual(result['status'], 'INCONCLUSIVE')
            self.skipTest('Local refusal is filtered/intercepted; real refusal NOT VERIFIED here')
        self.assertEqual((result['status'], result['tcp']), ('PASS', 'REFUSED'))

    def test_verified_listener_fails_and_sends_correct_sni(self):
        with self.listener() as (port, observed):
            result = probe_panel_port('127.0.0.1', port, 'panel.test', context=self.trust(),
                                      expected_certificate_sha256=self.fingerprint)
        self.assertEqual((result['status'], result['tls']), ('FAIL', 'VERIFIED'))
        self.assertEqual(observed, ['panel.test'])

    def test_trusted_tls_interceptor_with_different_certificate_is_inconclusive(self):
        with self.listener() as (port, _):
            result = probe_panel_port('127.0.0.1', port, 'panel.test', context=self.trust(),
                                      expected_certificate_sha256='0'*64)
        self.assertEqual((result['status'], result['tls']),
                         ('INCONCLUSIVE', 'VERIFIED_DIFFERENT_CERTIFICATE'))

    def test_valid_tls_without_vps_fingerprint_does_not_prove_panel_exposure(self):
        with self.listener() as (port, _):
            result = probe_panel_port('127.0.0.1', port, 'panel.test', context=self.trust())
        self.assertEqual(result['status'], 'INCONCLUSIVE')

    def test_tcp_connect_then_failed_tls_stays_inconclusive(self):
        with self.listener('plain') as (port, _):
            result = probe_panel_port('127.0.0.1', port, 'panel.test')
        self.assertEqual((result['status'], result['tcp'], result['tls']),
                         ('INCONCLUSIVE', 'CONNECTED', 'FAILED'))

    def test_wrong_certificate_name_is_not_proof_of_panel_reachability(self):
        with self.listener() as (port, _):
            result = probe_panel_port('127.0.0.1', port, 'wrong.test', context=self.trust())
        self.assertEqual((result['status'], result['tls']), ('INCONCLUSIVE', 'UNVERIFIED_CERTIFICATE'))

    def test_untrusted_certificate_stays_inconclusive(self):
        with self.listener() as (port, _):
            result = probe_panel_port('127.0.0.1', port, 'panel.test')
        self.assertEqual((result['status'], result['tls']), ('INCONCLUSIVE', 'UNVERIFIED_CERTIFICATE'))

    def test_probable_interception_cannot_be_pass_or_fail(self):
        with self.listener('intercept') as (port, _):
            result = probe_panel_port('127.0.0.1', port, 'panel.test', timeout=0.2)
        self.assertEqual((result['status'], result['tcp'], result['tls']),
                         ('INCONCLUSIVE', 'CONNECTED', 'TIMEOUT'))

    def test_verification_cannot_be_disabled(self):
        insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        with self.listener('intercept') as (port, _):
            result = probe_panel_port('127.0.0.1', port, 'panel.test', context=insecure)
        self.assertEqual(result['reason'], 'TLS_VERIFICATION_REQUIRED')
        self.assertEqual(result['status'], 'INCONCLUSIVE')
