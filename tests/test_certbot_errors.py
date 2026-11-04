import unittest

from selfsni.certbot_errors import explain


class CertbotErrorTests(unittest.TestCase):
    def test_exact_set_limit_converts_real_utc_time(self):
        output = 'too many certificates (5) already issued for this exact set of identifiers; retry after 2026-10-19 21:24:13 UTC'
        self.assertIn('20.10.2026, 00:24:13 МСК', explain(output))

    def test_rate_limit_without_time_does_not_invent_one(self):
        self.assertNotIn('Повторить попытку можно после:', explain('rate limit exceeded'))

    def test_service_busy_is_overload_not_a_domain_limit(self):
        message = explain('urn:ietf:params:acme:error:rateLimited :: There were too many requests of a '
                          'given type :: Service busy; retry later.')
        self.assertIn('временно перегружены', message)
        self.assertNotIn('лимит повторных', message)

    def test_rate_limits_are_named_by_their_kind(self):
        self.assertIn('неудачных', explain('rateLimited :: too many failed authorizations (5) in the last 1h0m0s'))
        self.assertIn('частоту запросов', explain('rateLimited :: too many new registrations (10) from this IP'))
        self.assertIn('лимит повторных', explain('too many certificates (5) already issued for this exact set'))

    def test_common_validation_failures(self):
        self.assertIn('DNS', explain('NXDOMAIN looking up A'))
        self.assertIn('CAA', explain('CAA record forbids issuance'))
        self.assertIn('Порт 80', explain('connection refused on port 80'))
        self.assertIn('HTTP', explain('unauthorized challenge failed'))


if __name__ == '__main__':
    unittest.main()
