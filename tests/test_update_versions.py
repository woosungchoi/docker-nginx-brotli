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
