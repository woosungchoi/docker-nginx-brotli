from __future__ import annotations

import argparse
import base64
import hashlib
import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Self
from unittest.mock import MagicMock, patch

from scripts.cleanup_dockerhub_tags import (
    CleanupError,
    DockerHubClient,
    GHCRArchiveClient,
    ManifestBackup,
    _require_mutation_confirmations,
    apply_plan,
    build_plan,
    load_approved_plan,
    manifest_backups_from_rows,
    manifest_backups_to_rows,
    recover_report,
    restore_inventory,
    run,
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
        raw_body: bytes | None = None,
    ) -> None:
        self.body = raw_body if raw_body is not None else (
            b"" if payload is None else json.dumps(payload).encode()
        )
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
        self.inject_keep_drift_before_delete = False
        self.inject_unplanned_alias_before_delete = False
        self.list_calls = 0

    def list_tags(self) -> list[dict[str, str]]:
        self.list_calls += 1
        if self.list_calls == 2 and self.inject_keep_drift_before_delete:
            for item in self.tags:
                if item["name"] == "latest":
                    item["digest"] = DIGEST_OLD_ONE
        if self.list_calls == 2 and self.inject_unplanned_alias_before_delete:
            self.tags.append(tag("3333333", DIGEST_OLD_ONE))
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

    def restore_tag(self, name: str, backup: ManifestBackup) -> None:
        self.tags = [item for item in self.tags if item["name"] != name]
        self.tags.append(tag(name, backup.digest))


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

    def test_loads_only_an_exact_hash_bound_dry_run_plan(self) -> None:
        plan = build_plan(
            self.inventory,
            expected_tag="abcdef1",
            expected_digest=DIGEST_CURRENT,
            archive_digests=self.archives,
        )
        report = {"mode": "dry-run", "status": "planned", **plan}
        encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approved-plan.json"
            path.write_bytes(encoded)
            digest = hashlib.sha256(encoded).hexdigest()

            self.assertEqual(load_approved_plan(path, digest), plan)
            with self.assertRaisesRegex(CleanupError, "hash mismatch"):
                load_approved_plan(path, "0" * 64)

    def test_manifest_backup_report_round_trip_is_exact(self) -> None:
        content = b'{"schemaVersion":2}'
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        backups = {
            digest: ManifestBackup(
                digest=digest,
                media_type="application/vnd.oci.image.index.v1+json",
                content=content,
            )
        }

        rows = manifest_backups_to_rows(backups)

        self.assertEqual(manifest_backups_from_rows(rows), backups)


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

    def test_rechecks_live_inventory_immediately_before_each_delete(self) -> None:
        for attribute in (
            "inject_keep_drift_before_delete",
            "inject_unplanned_alias_before_delete",
        ):
            with self.subTest(attribute=attribute):
                client = FakeDockerHubClient(self.inventory)
                setattr(client, attribute, True)

                with self.assertRaisesRegex(CleanupError, "live inventory drifted"):
                    apply_plan(client, self.plan, sleep=lambda _seconds: None)

                self.assertEqual(client.deleted_digests, [])

    def test_rolls_back_original_inventory_after_partial_failure(self) -> None:
        client = FakeDockerHubClient(self.inventory)
        client.inject_drift_after_delete = True
        rollback_calls: list[dict[str, str]] = []

        def rollback(original: Mapping[str, str]) -> None:
            rollback_calls.append(dict(original))
            client.tags = [dict(item) for item in self.inventory]

        with self.assertRaisesRegex(CleanupError, "live inventory drifted"):
            apply_plan(
                client,
                self.plan,
                rollback=rollback,
                sleep=lambda _seconds: None,
            )

        expected_original = dict(sorted((row["name"], row["digest"]) for row in self.inventory))
        self.assertEqual(rollback_calls, [expected_original])
        self.assertEqual(client.list_tags(), self.inventory)

    def test_digest_delete_preserves_a_concurrently_retagged_candidate(self) -> None:
        client = FakeDockerHubClient(self.inventory)
        client.retag_before_delete = True

        with self.assertRaisesRegex(CleanupError, "live inventory drifted"):
            apply_plan(client, self.plan, sleep=lambda _seconds: None)

        self.assertIn(tag("1111111", DIGEST_RACED), client.tags)
        self.assertNotIn(DIGEST_RACED, client.deleted_digests)

    def test_restore_inventory_recreates_missing_and_drifted_tags(self) -> None:
        client = FakeDockerHubClient(
            [
                tag("latest", DIGEST_RACED),
                tag("abcdef1", DIGEST_CURRENT),
            ]
        )
        backups = {
            digest: ManifestBackup(
                digest=digest,
                media_type="application/vnd.oci.image.index.v1+json",
                content=b"test-only",
            )
            for digest in {DIGEST_CURRENT, DIGEST_OLD_ONE, DIGEST_OLD_TWO}
        }
        original = {row["name"]: row["digest"] for row in self.inventory}

        restored = restore_inventory(
            client,
            original,
            backups,
            stable_reads=2,
            attempts=6,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(restored, dict(sorted(original.items())))

    def test_restore_inventory_never_deletes_an_unplanned_tag(self) -> None:
        client = FakeDockerHubClient([*self.inventory, tag("3333333", DIGEST_OLD_ONE)])
        backups = {
            digest: ManifestBackup(
                digest=digest,
                media_type="application/vnd.oci.image.index.v1+json",
                content=b"test-only",
            )
            for digest in {DIGEST_CURRENT, DIGEST_OLD_ONE, DIGEST_OLD_TWO}
        }
        original = {row["name"]: row["digest"] for row in self.inventory}

        with self.assertRaisesRegex(CleanupError, "unexpected tag"):
            restore_inventory(
                client,
                original,
                backups,
                sleep=lambda _seconds: None,
            )

        self.assertIn(tag("3333333", DIGEST_OLD_ONE), client.tags)


class HttpClientTests(unittest.TestCase):
    @patch("scripts.cleanup_dockerhub_tags.urllib.request.urlopen")
    def test_dockerhub_list_tags_uses_registry_native_inventory(
        self,
        urlopen: MagicMock,
    ) -> None:
        urlopen.side_effect = [
            StubResponse({"token": registry_token(["pull"])}),
            StubResponse(
                {
                    "name": "woosungchoi/docker-nginx-brotli",
                    "tags": ["latest", "1111111"],
                }
            ),
            StubResponse(headers={"Docker-Content-Digest": DIGEST_OLD_ONE}),
            StubResponse(headers={"Docker-Content-Digest": DIGEST_CURRENT}),
        ]

        rows = DockerHubClient().list_tags()

        self.assertEqual(rows, [tag("1111111", DIGEST_OLD_ONE), tag("latest", DIGEST_CURRENT)])
        self.assertEqual(urlopen.call_count, 4)
        tags_request = urlopen.call_args_list[1].args[0]
        self.assertIn("registry-1.docker.io/v2/woosungchoi/docker-nginx-brotli/tags/list", tags_request.full_url)
        self.assertTrue(tags_request.get_header("Authorization").startswith("Bearer header."))

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
    def test_captures_exact_manifest_bytes_for_rollback(self, urlopen: MagicMock) -> None:
        manifest = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json"}'
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        urlopen.return_value = StubResponse(
            status=200,
            headers={
                "Content-Type": "application/vnd.oci.image.index.v1+json",
                "Docker-Content-Digest": digest,
            },
            raw_body=manifest,
        )
        client = DockerHubClient()
        client.token = registry_token(["delete", "pull", "push"])

        backup = client.capture_manifest(digest)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.endswith(f"/manifests/{digest}"))
        self.assertEqual(
            backup,
            ManifestBackup(
                digest=digest,
                media_type="application/vnd.oci.image.index.v1+json",
                content=manifest,
            ),
        )

    @patch("scripts.cleanup_dockerhub_tags.urllib.request.urlopen")
    def test_restores_a_tag_from_exact_manifest_bytes(self, urlopen: MagicMock) -> None:
        manifest = b'{"schemaVersion":2}'
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        urlopen.return_value = StubResponse(
            status=201,
            headers={"Docker-Content-Digest": digest},
        )
        client = DockerHubClient()
        client.token = registry_token(["delete", "pull", "push"])

        client.restore_tag(
            "1111111",
            ManifestBackup(
                digest=digest,
                media_type="application/vnd.docker.distribution.manifest.list.v2+json",
                content=manifest,
            ),
        )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertTrue(request.full_url.endswith("/manifests/1111111"))
        self.assertEqual(request.data, manifest)
        self.assertEqual(
            request.get_header("Content-type"),
            "application/vnd.docker.distribution.manifest.list.v2+json",
        )

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

    @patch("scripts.cleanup_dockerhub_tags.urllib.request.urlopen")
    def test_ghcr_archive_lookup_verifies_digest_address(self, urlopen: MagicMock) -> None:
        urlopen.side_effect = [
            StubResponse({"token": "ghcr-token"}),
            StubResponse(headers={"Docker-Content-Digest": DIGEST_OLD_ONE}),
        ]

        GHCRArchiveClient().verify_digest(DIGEST_OLD_ONE)

        request = urlopen.call_args_list[1].args[0]
        self.assertTrue(request.full_url.endswith(f"/manifests/{DIGEST_OLD_ONE}"))
        self.assertEqual(request.get_method(), "HEAD")


class RunContractTests(unittest.TestCase):
    def test_apply_refuses_before_network_without_a_hash_bound_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                recover=False,
                expected_tag="abcdef0",
                expected_digest=DIGEST_CURRENT,
                report=Path(directory) / "report.json",
                apply=True,
                approved_plan=None,
                approved_plan_sha256="",
                confirm="DELETE-OLD-DOCKERHUB-SHA-TAGS",
                writer_freeze="I-CONFIRM-DOCKERHUB-WRITERS-ARE-FROZEN",
            )
            with self.assertRaisesRegex(CleanupError, "hash-bound approved plan"):
                run(args)

    def test_mutation_requires_an_explicit_writer_freeze(self) -> None:
        args = argparse.Namespace(
            confirm="DELETE-OLD-DOCKERHUB-SHA-TAGS",
            writer_freeze="",
        )
        with self.assertRaisesRegex(CleanupError, "writer-freeze"):
            _require_mutation_confirmations(args)

    def test_recovery_refuses_to_roll_back_a_successful_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text(json.dumps({"mode": "apply", "status": "applied"}))
            args = argparse.Namespace(
                apply=False,
                report=report,
                confirm="DELETE-OLD-DOCKERHUB-SHA-TAGS",
                writer_freeze="I-CONFIRM-DOCKERHUB-WRITERS-ARE-FROZEN",
            )
            with self.assertRaisesRegex(CleanupError, "not eligible for recovery"):
                recover_report(args)

    @patch("scripts.cleanup_dockerhub_tags.DockerHubClient")
    def test_durable_dry_run_baseline_does_not_undo_a_completed_cleanup(
        self,
        client_type: MagicMock,
    ) -> None:
        current_content = b"current manifest"
        old_content = b"old manifest"
        current_digest = "sha256:" + hashlib.sha256(current_content).hexdigest()
        old_digest = "sha256:" + hashlib.sha256(old_content).hexdigest()
        expected_tag = "abcdef0"
        original = {
            "latest": current_digest,
            expected_tag: current_digest,
            "1111111": old_digest,
        }
        backups = {
            current_digest: ManifestBackup(
                current_digest,
                "application/vnd.oci.image.index.v1+json",
                current_content,
            ),
            old_digest: ManifestBackup(
                old_digest,
                "application/vnd.oci.image.index.v1+json",
                old_content,
            ),
        }
        client = client_type.return_value
        client.list_tags.return_value = [
            tag("latest", current_digest),
            tag(expected_tag, current_digest),
        ]

        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "plan.json"
            report_path.write_text(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "status": "planned",
                        "dockerhub_repository": "woosungchoi/docker-nginx-brotli",
                        "expected_tag": expected_tag,
                        "expected_digest": current_digest,
                        "rollback_inventory": [
                            {"name": name, "digest": digest}
                            for name, digest in sorted(original.items())
                        ],
                        "rollback_manifests": manifest_backups_to_rows(backups),
                    }
                )
            )
            args = argparse.Namespace(
                apply=False,
                report=report_path,
                confirm="DELETE-OLD-DOCKERHUB-SHA-TAGS",
                writer_freeze="I-CONFIRM-DOCKERHUB-WRITERS-ARE-FROZEN",
            )

            self.assertEqual(recover_report(args), 0)
            client.restore_tag.assert_not_called()
            recovered = json.loads(report_path.read_text())
            self.assertEqual(
                recovered["recovery_status"],
                "not_needed_final_inventory",
            )


class WorkflowIntegrationTests(unittest.TestCase):
    def test_publish_job_only_builds_a_non_destructive_plan(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/image.yml").read_text()
        build_job, cleanup_job = workflow.split("\n  cleanup:\n", maxsplit=1)

        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertIn("Plan Docker Hub retention", build_job)
        self.assertNotIn("--apply", build_job)
        self.assertIn("--apply", cleanup_job)

    def test_cleanup_job_uses_same_trusted_run_and_environment_gate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/image.yml").read_text()

        self.assertNotIn("repository_dispatch:", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertIn("environment: dockerhub-cleanup", workflow)
        self.assertIn("group: publish-image", workflow)
        self.assertIn("needs: build", workflow)
        self.assertIn("--approved-plan", workflow)
        self.assertIn("--approved-plan-sha256", workflow)
        self.assertIn("I-CONFIRM-DOCKERHUB-WRITERS-ARE-FROZEN", workflow)


if __name__ == "__main__":
    unittest.main()
