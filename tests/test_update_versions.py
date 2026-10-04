from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Self
from unittest.mock import MagicMock, patch

from scripts.update_versions import fetch_text

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class StubResponse:
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class FetchTextTests(unittest.TestCase):
    @patch("scripts.update_versions.urllib.request.urlopen")
    def test_authenticates_only_github_api_requests(self, urlopen: MagicMock) -> None:
        urlopen.return_value = StubResponse()

        with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}, clear=False):
            fetch_text("https://api.github.com/repos/madler/zlib/releases/latest")
            github_request = urlopen.call_args.args[0]
            fetch_text("https://nginx.org/download/")
            nginx_request = urlopen.call_args.args[0]

        self.assertEqual(github_request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(github_request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(github_request.get_header("X-github-api-version"), "2022-11-28")
        self.assertIsNone(nginx_request.get_header("Authorization"))


class WorkflowIntegrationTests(unittest.TestCase):
    def test_dependency_checks_supply_an_authenticated_github_token(self) -> None:
        ci_workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
        update_workflow = (
            REPOSITORY_ROOT / ".github/workflows/update-versions.yml"
        ).read_text()

        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", ci_workflow)
        self.assertIn(
            "GITHUB_TOKEN: ${{ steps.app-token.outputs.token }}", update_workflow
        )


if __name__ == "__main__":
    unittest.main()


class PinUpdateTests(unittest.TestCase):
    def test_archive_checksum_streams_anonymous_bytes(self):
        import hashlib
        import io
        from scripts.update_versions import archive_checksum
        body = b'official fixture archive' * 200000
        with patch('scripts.update_versions.urllib.request.urlopen', return_value=io.BytesIO(body)) as urlopen:
            with patch.dict(os.environ, {'GITHUB_TOKEN': 'test-token'}, clear=False):
                digest = archive_checksum('https://github.com/madler/zlib/releases/download/v1.3.2/zlib-1.3.2.tar.gz')
        self.assertEqual(digest, hashlib.sha256(body).hexdigest())
        self.assertIsNone(urlopen.call_args.args[0].get_header('Authorization'))

    def test_resolution_failure_leaves_complete_old_pinset_unchanged(self):
        import tempfile
        from scripts.update_versions import UpdateError, main
        original = (REPOSITORY_ROOT / 'Dockerfile').read_text()
        with tempfile.TemporaryDirectory() as directory:
            dockerfile = Path(directory) / 'Dockerfile'
            dockerfile.write_text(original)
            with patch('sys.argv', ['update_versions', '--dockerfile', str(dockerfile)]), \
                 patch('scripts.update_versions.fetch_text', return_value='nginx-1.30.6.tar.gz'), \
                 patch('scripts.update_versions.latest_github_release_version', side_effect=['10.49', '1.3.2']), \
                 patch('scripts.update_versions.resolve_pins', side_effect=UpdateError('upstream unavailable')):
                with self.assertRaises(UpdateError):
                    main()
            self.assertEqual(dockerfile.read_text(), original)

    def test_invalid_digest_resolution_is_rejected(self):
        from scripts.update_versions import UpdateError, extract_pins, resolve_pins
        pins = extract_pins((REPOSITORY_ROOT / 'Dockerfile').read_text())
        versions = {key: pins[key] for key in ('NGINX_VERSION', 'PCRE_VERSION', 'ZLIB_VERSION')}
        with patch('scripts.update_versions.archive_checksum', return_value='f' * 64), \
             patch('scripts.update_versions.fetch_json', return_value={'sha': 'a' * 40}), \
             patch('scripts.update_versions.subprocess.check_output', return_value='Digest: invalid'):
            with self.assertRaises(UpdateError):
                resolve_pins(pins, versions)
