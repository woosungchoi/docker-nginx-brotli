import unittest
from scripts.runtime_smoke import assert_response


class ResponseContractTests(unittest.TestCase):
    def test_only_exact_success_protocol_and_body_pass(self):
        assert_response('200', '3', b'fixture', b'fixture', '3')
        for status in ['301', '403', '404', '500']:
            with self.subTest(status=status), self.assertRaises(AssertionError):
                assert_response(status, '3', b'fixture', b'fixture', '3')
        with self.assertRaises(AssertionError):
            assert_response('200', '1.1', b'fixture', b'fixture', '3')
        with self.assertRaises(AssertionError):
            assert_response('200', '3', b'wrong', b'fixture', '3')
