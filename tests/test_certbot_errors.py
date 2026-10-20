import unittest

from selfsni.certbot_errors import explain


class CertbotErrorTests(unittest.TestCase):
    def test_exact_set_limit_converts_real_utc_time(self):
        output = 'too many certificates (5) already issued for this exact set of identifiers; retry after 2026-10-19 21:24:13 UTC'
        self.assertIn('20.10.2026, 00:24:13 МСК', explain(output))

    def test_rate_limit_without_time_does_not_invent_one(self):
        self.assertNotIn('Повторить попытку можно после:', explain('rate limit exceeded'))

    def test_common_validation_failures(self):
        self.assertIn('DNS', explain('NXDOMAIN looking up A'))
        self.assertIn('CAA', explain('CAA record forbids issuance'))
        self.assertIn('Порт 80', explain('connection refused on port 80'))
        self.assertIn('HTTP', explain('unauthorized challenge failed'))


if __name__ == '__main__':
    unittest.main()
