import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.verify_nginx_source import FINGERPRINT, VerificationError, verify_pair


class SignatureContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = Path(self.directory.name) / 'source.tar.gz'
        self.archive.write_bytes(b'public source fixture')
        self.signature = Path(self.directory.name) / 'source.tar.gz.asc'
        self.signature.write_text('public detached signature fixture')
        self.checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_bad_checksum_stops_before_any_key_access(self):
        with patch('scripts.verify_nginx_source.subprocess.run') as command:
            with self.assertRaises(VerificationError):
                verify_pair(self.archive, self.signature, '0' * 64)
            command.assert_not_called()

    def test_pinned_fingerprint_and_disposable_homedir_required(self):
        valid = subprocess.CompletedProcess([], 0, f'[GNUPG:] VALIDSIG {FINGERPRINT} rest {FINGERPRINT}\n', '')
        imported = subprocess.CompletedProcess([], 0, '', '')
        with patch('scripts.verify_nginx_source.subprocess.run', side_effect=[imported, valid]) as command:
            verify_pair(self.archive, self.signature, self.checksum)
            for call in command.call_args_list:
                args = call.args[0]
                self.assertIn('--homedir', args)
                self.assertNotIn('--trust-model', args)
                self.assertTrue(args[args.index('--homedir') + 1].startswith(tempfile.gettempdir()))

    def test_invalid_signature_and_untrusted_fingerprint_rejected(self):
        imported = subprocess.CompletedProcess([], 0, '', '')
        for result in [subprocess.CompletedProcess([], 1, '', 'bad signature'),
                       subprocess.CompletedProcess([], 0, '[GNUPG:] VALIDSIG attacker rest attacker\n', ''),
                       subprocess.CompletedProcess([], 0, '', '')]:
            with patch('scripts.verify_nginx_source.subprocess.run', side_effect=[imported, result]):
                with self.assertRaises(VerificationError):
                    verify_pair(self.archive, self.signature, self.checksum)
