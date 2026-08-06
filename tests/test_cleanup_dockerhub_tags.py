from __future__ import annotations

import base64
import json
import unittest
from typing import Self
from unittest.mock import MagicMock, patch

from scripts.cleanup_dockerhub_tags import (
    CleanupError,
    DockerHubClient,
    GHCRArchiveClient,
    apply_plan,
    build_plan,
)

DIGEST_CURRENT = "sha256:" + "a" * 64
DIGEST_OLD_ONE = "sha256:" + "b" * 64
DIGEST_OLD_TWO = "sha256:" + "c" * 64
DIGEST_RACED = "sha256:" + "d" * 64


def tag(name: str, digest: str) -> dict[str, str]:
    return {"name": name, "digest": digest}


def registry_token(actions: list[str]) -> str:
    payload = {
        "access": [
            {
                "type": "repository",
                "name": "woosungchoi/docker-nginx-brotli",
                "actions": actions,
            }
        ]
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class StubResponse:
    def __init__(
        self,
        payload: object | None = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = b"" if payload is None else json.dumps(payload).encode()
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class FakeDockerHubClient:
    def __init__(self, tags: list[dict[str, str]]) -> None:
        self.tags = [dict(item) for item in tags]
        self.deleted_digests: list[str] = []
        self.inject_drift_after_delete = False
        self.retag_before_delete = False

    def list_tags(self) -> list[dict[str, str]]:
        return [dict(item) for item in self.tags]

    def delete_digest(self, digest: str) -> None:
        if self.retag_before_delete:
            for item in self.tags:
                if item["digest"] == digest:
                    item["digest"] = DIGEST_RACED
                    break
            self.retag_before_delete = False
        self.tags = [item for item in self.tags if item["digest"] != digest]
        self.deleted_digests.append(digest)
        if self.inject_drift_after_delete and len(self.deleted_digests) == 1:
            self.tags.append(tag("unexpected", DIGEST_OLD_ONE))


class BuildPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inventory = [
            tag("latest", DIGEST_CURRENT),
            tag("abcdef1", DIGEST_CURRENT),
            tag("1111111", DIGEST_OLD_ONE),
            tag("2222222", DIGEST_OLD_TWO),
        ]
        self.archives = {
            "1111111": DIGEST_OLD_ONE,
            "2222222": DIGEST_OLD_TWO,
        }

    def test_builds_exact_keep_and_delete_sets(self) -> None:
        plan = build_plan(
            self.inventory,
            expected_tag="abcdef1",
            expected_digest=DIGEST_CURRENT,
            archive_digests=self.archives,
        )

        self.assertEqual([row["name"] for row in plan["keep"]], ["latest", "abcdef1"])
        self.assertEqual([row["name"] for row in plan["delete"]], ["1111111", "2222222"])
        self.assertEqual(
            plan["delete_groups"],
            [
                {"digest": DIGEST_OLD_ONE, "tags": ["1111111"]},
                {"digest": DIGEST_OLD_TWO, "tags": ["2222222"]},
            ],
        )
        self.assertTrue(all(row["archive_digest"] == row["digest"] for row in plan["delete"]))

    def test_exact_keep_set_is_an_idempotent_noop(self) -> None:
        inventory = [tag("latest", DIGEST_CURRENT), tag("abcdef1", DIGEST_CURRENT)]
        plan = build_plan(
            inventory,
            expected_tag="abcdef1",
            expected_digest=DIGEST_CURRENT,
            archive_digests={},
        )
        client = FakeDockerHubClient(inventory)

        final_inventory = apply_plan(client, plan)

        self.assertEqual(client.deleted_digests, [])
        self.assertEqual([row["name"] for row in final_inventory], ["abcdef1", "latest"])

    def test_rejects_deleting_a_digest_shared_with_a_keep_tag(self) -> None:
        unsafe_inventory = [
            tag("latest", DIGEST_CURRENT),
            tag("abcdef1", DIGEST_CURRENT),
            tag("1111111", DIGEST_CURRENT),
        ]
        with self.assertRaisesRegex(CleanupError, "shares the protected digest"):
            build_plan(
                unsafe_inventory,
                expected_tag="abcdef1",
                expected_digest=DIGEST_CURRENT,
                archive_digests={"1111111": DIGEST_CURRENT},
            )

    def test_groups_tags_that_share_one_old_digest(self) -> None:
        inventory = [
            tag("latest", DIGEST_CURRENT),
            tag("abcdef1", DIGEST_CURRENT),
            tag("1111111", DIGEST_OLD_ONE),
            tag("2222222", DIGEST_OLD_ONE),
        ]
        plan = build_plan(
            inventory,
            expected_tag="abcdef1",
            expected_digest=DIGEST_CURRENT,
            archive_digests={"1111111": DIGEST_OLD_ONE, "2222222": DIGEST_OLD_ONE},
        )
        client = FakeDockerHubClient(inventory)

        final_inventory = apply_plan(client, plan, sleep=lambda _seconds: None)

        self.assertEqual(client.deleted_digests, [DIGEST_OLD_ONE])
        self.assertEqual([row["name"] for row in final_inventory], ["abcdef1", "latest"])

    def test_rejects_invalid_expected_identity(self) -> None:
        with self.assertRaisesRegex(CleanupError, "seven-character lowercase Git SHA"):
            build_plan(
                self.inventory,
                expected_tag="ABCDEF1",
                expected_digest=DIGEST_CURRENT,
                archive_digests=self.archives,
            )

        with self.assertRaisesRegex(CleanupError, "invalid digest"):
            build_plan(
                self.inventory,
                expected_tag="abcdef1",
                expected_digest="sha256:not-a-digest",
                archive_digests=self.archives,
            )

    def test_rejects_unknown_tag_before_deletion(self) -> None:
        with self.assertRaisesRegex(CleanupError, "unexpected tag name"):
            build_plan(
                [*self.inventory, tag("stable", DIGEST_OLD_ONE)],
                expected_tag="abcdef1",
                expected_digest=DIGEST_CURRENT,
                archive_digests=self.archives,
            )

    def test_rejects_keep_digest_drift(self) -> None:
        drifted = [dict(item) for item in self.inventory]
        drifted[0]["digest"] = DIGEST_OLD_ONE

        with self.assertRaisesRegex(CleanupError, "latest digest mismatch"):
            build_plan(
                drifted,
                expected_tag="abcdef1",
                expected_digest=DIGEST_CURRENT,
                archive_digests=self.archives,
            )

    def test_rejects_missing_or_mismatched_archive(self) -> None:
        with self.assertRaisesRegex(CleanupError, "archive set mismatch"):
            build_plan(
                self.inventory,
                expected_tag="abcdef1",
                expected_digest=DIGEST_CURRENT,
                archive_digests={"1111111": DIGEST_OLD_ONE},
            )

        with self.assertRaisesRegex(CleanupError, "archive digest mismatch"):
            build_plan(
                self.inventory,
                expected_tag="abcdef1",
                expected_digest=DIGEST_CURRENT,
                archive_digests={
                    "1111111": DIGEST_OLD_TWO,
                    "2222222": DIGEST_OLD_TWO,
                },
            )

    def test_rejects_duplicate_tag_rows(self) -> None:
        with self.assertRaisesRegex(CleanupError, "duplicate tag"):
            build_plan(
                [*self.inventory, tag("1111111", DIGEST_OLD_ONE)],
                expected_tag="abcdef1",
                expected_digest=DIGEST_CURRENT,
                archive_digests=self.archives,
            )


class ApplyPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inventory = [
            tag("latest", DIGEST_CURRENT),
            tag("abcdef1", DIGEST_CURRENT),
            tag("1111111", DIGEST_OLD_ONE),
            tag("2222222", DIGEST_OLD_TWO),
        ]
        self.plan = build_plan(
            self.inventory,
            expected_tag="abcdef1",
            expected_digest=DIGEST_CURRENT,
            archive_digests={
                "1111111": DIGEST_OLD_ONE,
                "2222222": DIGEST_OLD_TWO,
            },
        )

    def test_rechecks_inventory_and_deletes_only_planned_tags(self) -> None:
        client = FakeDockerHubClient(self.inventory)
        archive_checks: list[tuple[str, str]] = []
        checkpoints: list[list[str]] = []

        final_inventory = apply_plan(
            client,
            self.plan,
            verify_archive=lambda name, digest: archive_checks.append((name, digest)),
            on_deleted=lambda _tags, _digest, remaining: checkpoints.append(sorted(remaining)),
            sleep=lambda _seconds: None,
        )

        self.assertEqual(client.deleted_digests, [DIGEST_OLD_ONE, DIGEST_OLD_TWO])
        self.assertEqual(
            archive_checks,
            [("1111111", DIGEST_OLD_ONE), ("2222222", DIGEST_OLD_TWO)],
        )
        self.assertEqual([row["name"] for row in final_inventory], ["abcdef1", "latest"])
        self.assertEqual(checkpoints, [["2222222", "abcdef1", "latest"], ["abcdef1", "latest"]])

    def test_archive_drift_stops_before_first_delete(self) -> None:
        client = FakeDockerHubClient(self.inventory)

        def reject_archive(_name: str, _digest: str) -> None:
            raise CleanupError("archive digest drifted")

        with self.assertRaisesRegex(CleanupError, "archive digest drifted"):
            apply_plan(
                client,
                self.plan,
                verify_archive=reject_archive,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(client.deleted_digests, [])

    def test_stops_when_live_inventory_drifts_between_deletes(self) -> None:
        client = FakeDockerHubClient(self.inventory)
        client.inject_drift_after_delete = True

        with self.assertRaisesRegex(CleanupError, "live inventory drifted"):
            apply_plan(client, self.plan, sleep=lambda _seconds: None)

        self.assertEqual(client.deleted_digests, [DIGEST_OLD_ONE])

    def test_digest_delete_preserves_a_concurrently_retagged_candidate(self) -> None:
        client = FakeDockerHubClient(self.inventory)
        client.retag_before_delete = True

        with self.assertRaisesRegex(CleanupError, "live inventory drifted"):
            apply_plan(client, self.plan, sleep=lambda _seconds: None)

        self.assertIn(tag("1111111", DIGEST_RACED), client.tags)
        self.assertNotIn(DIGEST_RACED, client.deleted_digests)


class HttpClientTests(unittest.TestCase):
    @patch("scripts.cleanup_dockerhub_tags.urllib.request.urlopen")
    def test_dockerhub_list_tags_follows_valid_pagination(self, urlopen: MagicMock) -> None:
        first_page = StubResponse(
            {
                "count": 2,
                "results": [tag("1111111", DIGEST_OLD_ONE)],
                "next": (
                    "https://hub.docker.com/v2/repositories/woosungchoi/"
                    "docker-nginx-brotli/tags?page_size=100&page=2&ordering=name"
                ),
            }
        )
        second_page = StubResponse(
            {
                "count": 2,
                "results": [tag("latest", DIGEST_CURRENT)],
                "next": None,
            }
        )
        urlopen.side_effect = [first_page, second_page]

        rows = DockerHubClient().list_tags()

        self.assertEqual(rows, [tag("1111111", DIGEST_OLD_ONE), tag("latest", DIGEST_CURRENT)])
        self.assertEqual(urlopen.call_count, 2)

    @patch("scripts.cleanup_dockerhub_tags.urllib.request.urlopen")
    def test_dockerhub_login_and_digest_delete_use_registry_token(
        self,
        urlopen: MagicMock,
    ) -> None:
        urlopen.side_effect = [
            StubResponse({"token": registry_token(["delete", "pull", "push"])}),
            StubResponse(status=202),
        ]
        client = DockerHubClient()

        client.login("woosungchoi", "test-pat")
        client.delete_digest(DIGEST_OLD_ONE)

        login_request = urlopen.call_args_list[0].args[0]
        delete_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(login_request.get_method(), "GET")
        self.assertIn("auth.docker.io/token?", login_request.full_url)
        self.assertIn("repository%3Awoosungchoi%2Fdocker-nginx-brotli%3Apull%2Cpush%2Cdelete", login_request.full_url)
        self.assertTrue(login_request.get_header("Authorization").startswith("Basic "))
        self.assertEqual(delete_request.get_method(), "DELETE")
        self.assertTrue(delete_request.full_url.endswith(f"/manifests/{DIGEST_OLD_ONE}"))
        self.assertTrue(delete_request.get_header("Authorization").startswith("Bearer header."))

    @patch("scripts.cleanup_dockerhub_tags.urllib.request.urlopen")
    def test_dockerhub_delete_rejects_invalid_digest_without_network(
        self,
        urlopen: MagicMock,
    ) -> None:
        client = DockerHubClient()
        client.token = registry_token(["delete", "pull", "push"])

        with self.assertRaisesRegex(CleanupError, "invalid digest"):
            client.delete_digest("latest")

        urlopen.assert_not_called()

    @patch("scripts.cleanup_dockerhub_tags.urllib.request.urlopen")
    def test_dockerhub_login_rejects_token_without_delete_scope(
        self,
        urlopen: MagicMock,
    ) -> None:
        urlopen.return_value = StubResponse({"token": registry_token(["pull", "push"])})

        with self.assertRaisesRegex(CleanupError, "delete scope"):
            DockerHubClient().login("woosungchoi", "test-pat")

    @patch("scripts.cleanup_dockerhub_tags.urllib.request.urlopen")
    def test_ghcr_archive_lookup_returns_registry_digest(self, urlopen: MagicMock) -> None:
        urlopen.side_effect = [
            StubResponse({"token": "ghcr-token"}),
            StubResponse(headers={"Docker-Content-Digest": DIGEST_OLD_ONE}),
        ]

        digest = GHCRArchiveClient().digest_for_tag("1111111")

        manifest_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(digest, DIGEST_OLD_ONE)
        self.assertEqual(manifest_request.get_method(), "HEAD")
        self.assertEqual(manifest_request.get_header("Authorization"), "Bearer ghcr-token")


if __name__ == "__main__":
    unittest.main()
