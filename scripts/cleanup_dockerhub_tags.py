#!/usr/bin/env python3
"""Plan or apply retention of ``latest`` and the current source-SHA tag.

Dry-run is the default. Apply requires a hash-bound dry-run plan, an explicit
writer-freeze acknowledgement, exact GHCR digest archives, and an authenticated
Docker Registry token with pull/push/delete capability. Inventory comes from the
registry rather than Docker Hub's eventually consistent UI/API count. Before
mutation, exact manifest bytes are journaled for rollback; the complete live tag
set is rechecked immediately before every digest deletion.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DOCKERHUB_NAMESPACE = "woosungchoi"
DOCKERHUB_REPOSITORY = "docker-nginx-brotli"
DOCKER_REGISTRY_AUTH = "https://auth.docker.io/token"
DOCKER_REGISTRY_BASE = "https://registry-1.docker.io"
GHCR_REPOSITORY = "woosungchoi/nginx-http3"
GHCR_REF_PREFIX = f"ghcr.io/{GHCR_REPOSITORY}"
USER_AGENT = "docker-nginx-brotli-tag-cleanup/1.0"
TIMEOUT_SECONDS = 30
DELETE_CONFIRMATION = "DELETE-OLD-DOCKERHUB-SHA-TAGS"
WRITER_FREEZE_CONFIRMATION = "I-CONFIRM-DOCKERHUB-WRITERS-ARE-FROZEN"
SHA_TAG_PATTERN = re.compile(r"^[0-9a-f]{7}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
MANIFEST_ACCEPT = ", ".join(sorted(MANIFEST_MEDIA_TYPES))
PLAN_KEYS = (
    "schema_version",
    "dockerhub_repository",
    "archive_repository",
    "expected_tag",
    "expected_digest",
    "inventory",
    "keep",
    "delete",
    "delete_groups",
)


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManifestBackup:
    digest: str
    media_type: str
    content: bytes


class TagClient(Protocol):
    def list_tags(self) -> list[dict[str, str]]: ...

    def delete_digest(self, digest: str) -> None: ...


class RollbackClient(TagClient, Protocol):
    def restore_tag(self, name: str, backup: ManifestBackup) -> None: ...


def _validate_digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not DIGEST_PATTERN.fullmatch(value):
        raise CleanupError(f"invalid digest for {context}")
    return value


def canonical_inventory(tags: object) -> dict[str, str]:
    if not isinstance(tags, list):
        raise CleanupError("tag inventory is not a list")

    inventory: dict[str, str] = {}
    for row in tags:
        if not isinstance(row, dict):
            raise CleanupError("tag inventory contains a non-object row")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise CleanupError("tag inventory contains an invalid name")
        if name in inventory:
            raise CleanupError(f"duplicate tag in inventory: {name}")
        inventory[name] = _validate_digest(row.get("digest"), context=name)
    return dict(sorted(inventory.items()))


def inventory_rows(inventory: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"name": name, "digest": inventory[name]} for name in sorted(inventory)]


def _validated_inventory(
    tags: object,
    *,
    expected_tag: str,
    expected_digest: str,
) -> tuple[dict[str, str], list[str]]:
    if not SHA_TAG_PATTERN.fullmatch(expected_tag):
        raise CleanupError("expected tag must be a seven-character lowercase Git SHA")
    expected_digest = _validate_digest(expected_digest, context="expected image")
    inventory = canonical_inventory(tags)

    unexpected = sorted(
        name for name in inventory if name != "latest" and not SHA_TAG_PATTERN.fullmatch(name)
    )
    if unexpected:
        raise CleanupError("unexpected tag name(s): " + ", ".join(unexpected))

    for keep_name in ("latest", expected_tag):
        if keep_name not in inventory:
            raise CleanupError(f"required keep tag is missing: {keep_name}")
        if inventory[keep_name] != expected_digest:
            raise CleanupError(
                f"{keep_name} digest mismatch: expected {expected_digest}, got {inventory[keep_name]}"
            )

    delete_names = sorted(set(inventory) - {"latest", expected_tag})
    return inventory, delete_names


def build_plan(
    tags: object,
    *,
    expected_tag: str,
    expected_digest: str,
    archive_digests: Mapping[str, str],
) -> dict[str, object]:
    inventory, delete_names = _validated_inventory(
        tags,
        expected_tag=expected_tag,
        expected_digest=expected_digest,
    )

    if set(archive_digests) != set(delete_names):
        missing = sorted(set(delete_names) - set(archive_digests))
        extra = sorted(set(archive_digests) - set(delete_names))
        raise CleanupError(
            "archive set mismatch"
            f"; missing={','.join(missing) or '-'}; extra={','.join(extra) or '-'}"
        )

    delete_rows: list[dict[str, str]] = []
    groups_by_digest: dict[str, list[str]] = {}
    for name in delete_names:
        if inventory[name] == expected_digest:
            raise CleanupError(f"deletion tag {name} shares the protected digest")
        archive_digest = _validate_digest(archive_digests[name], context=f"GHCR archive {name}")
        if archive_digest != inventory[name]:
            raise CleanupError(
                f"archive digest mismatch for {name}: "
                f"Docker Hub={inventory[name]}, GHCR={archive_digest}"
            )
        delete_rows.append(
            {
                "name": name,
                "digest": inventory[name],
                "archive_ref": f"{GHCR_REF_PREFIX}:{name}",
                "archive_digest": archive_digest,
            }
        )
        groups_by_digest.setdefault(inventory[name], []).append(name)

    delete_groups = [
        {"digest": digest, "tags": sorted(groups_by_digest[digest])}
        for digest in sorted(groups_by_digest)
    ]

    return {
        "schema_version": 2,
        "dockerhub_repository": f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}",
        "archive_repository": GHCR_REF_PREFIX,
        "expected_tag": expected_tag,
        "expected_digest": expected_digest,
        "inventory": inventory_rows(inventory),
        "keep": [
            {"name": "latest", "digest": expected_digest},
            {"name": expected_tag, "digest": expected_digest},
        ],
        "delete": delete_rows,
        "delete_groups": delete_groups,
    }


def load_approved_report(path: Path, expected_sha256: str) -> dict[str, object]:
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        raise CleanupError("approved plan hash is invalid")
    try:
        encoded = path.read_bytes()
    except OSError as error:
        raise CleanupError(f"approved plan could not be read: {type(error).__name__}") from None
    actual_sha256 = hashlib.sha256(encoded).hexdigest()
    if actual_sha256 != expected_sha256:
        raise CleanupError("approved plan hash mismatch")
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError:
        raise CleanupError("approved plan is not valid JSON") from None
    if not isinstance(payload, dict):
        raise CleanupError("approved plan is not an object")
    if payload.get("mode") != "dry-run" or payload.get("status") != "planned":
        raise CleanupError("approved plan is not a completed dry-run plan")
    missing = [key for key in PLAN_KEYS if key not in payload]
    if missing:
        raise CleanupError("approved plan is missing field(s): " + ", ".join(missing))
    return payload


def load_approved_plan(path: Path, expected_sha256: str) -> dict[str, object]:
    payload = load_approved_report(path, expected_sha256)
    return {key: payload[key] for key in PLAN_KEYS}


def manifest_backups_to_rows(
    backups: Mapping[str, ManifestBackup],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for digest in sorted(backups):
        backup = backups[digest]
        if digest != backup.digest:
            raise CleanupError("manifest backup mapping key does not match its digest")
        _validate_digest(digest, context="manifest backup")
        if backup.media_type not in MANIFEST_MEDIA_TYPES:
            raise CleanupError(f"manifest backup media type is invalid for {digest}")
        if "sha256:" + hashlib.sha256(backup.content).hexdigest() != digest:
            raise CleanupError(f"manifest backup content mismatch for {digest}")
        rows.append(
            {
                "digest": digest,
                "media_type": backup.media_type,
                "content_base64": base64.b64encode(backup.content).decode("ascii"),
            }
        )
    return rows


def manifest_backups_from_rows(rows: object) -> dict[str, ManifestBackup]:
    if not isinstance(rows, list):
        raise CleanupError("manifest backup report is not a list")
    backups: dict[str, ManifestBackup] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "digest",
            "media_type",
            "content_base64",
        }:
            raise CleanupError("manifest backup report contains an invalid row")
        digest = _validate_digest(row.get("digest"), context="manifest backup report")
        media_type = row.get("media_type")
        encoded = row.get("content_base64")
        if not isinstance(media_type, str) or media_type not in MANIFEST_MEDIA_TYPES:
            raise CleanupError(f"manifest backup media type is invalid for {digest}")
        if not isinstance(encoded, str):
            raise CleanupError(f"manifest backup content is invalid for {digest}")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise CleanupError(f"manifest backup content is invalid for {digest}") from None
        if digest in backups:
            raise CleanupError(f"duplicate manifest backup for {digest}")
        backup = ManifestBackup(digest=digest, media_type=media_type, content=content)
        if "sha256:" + hashlib.sha256(content).hexdigest() != digest:
            raise CleanupError(f"manifest backup content mismatch for {digest}")
        backups[digest] = backup
    return backups


def _require_inventory(
    actual_tags: object,
    expected: Mapping[str, str],
    *,
    context: str,
) -> dict[str, str]:
    actual = canonical_inventory(actual_tags)
    expected_dict = dict(sorted(expected.items()))
    if actual != expected_dict:
        raise CleanupError(
            f"live inventory drifted {context}; "
            f"expected={json.dumps(expected_dict, sort_keys=True)}, "
            f"actual={json.dumps(actual, sort_keys=True)}"
        )
    return actual


def _wait_for_inventory(
    client: TagClient,
    *,
    before: Mapping[str, str],
    expected: Mapping[str, str],
    context: str,
    attempts: int,
    sleep: Callable[[float], None],
) -> dict[str, str]:
    before_dict = dict(sorted(before.items()))
    expected_dict = dict(sorted(expected.items()))
    for attempt in range(attempts):
        actual = canonical_inventory(client.list_tags())
        if actual == expected_dict:
            return actual

        expected_is_intact = all(actual.get(name) == digest for name, digest in expected_dict.items())
        actual_only_contains_predelete_rows = all(
            before_dict.get(name) == digest for name, digest in actual.items()
        )
        if not expected_is_intact or not actual_only_contains_predelete_rows:
            raise CleanupError(
                f"live inventory drifted {context}; "
                f"expected={json.dumps(expected_dict, sort_keys=True)}, "
                f"actual={json.dumps(actual, sort_keys=True)}"
            )
        if attempt + 1 < attempts:
            sleep(5)

    raise CleanupError(
        f"Docker Hub deletion did not converge {context}; "
        f"expected={json.dumps(expected_dict, sort_keys=True)}"
    )


def apply_plan(
    client: TagClient,
    plan: Mapping[str, object],
    *,
    verify_archive: Callable[[str, str], None] | None = None,
    on_deleted: Callable[[list[str], str, Mapping[str, str]], None] | None = None,
    rollback: Callable[[Mapping[str, str]], None] | None = None,
    convergence_attempts: int = 24,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, str]]:
    if convergence_attempts < 1:
        raise CleanupError("convergence attempts must be positive")

    inventory = canonical_inventory(plan.get("inventory"))
    delete_rows = plan.get("delete")
    delete_groups = plan.get("delete_groups")
    keep_rows = plan.get("keep")
    if (
        not isinstance(delete_rows, list)
        or not isinstance(delete_groups, list)
        or not isinstance(keep_rows, list)
    ):
        raise CleanupError("plan delete groups/rows or keep rows are invalid")

    keep_inventory = canonical_inventory(keep_rows)
    delete_inventory = canonical_inventory(delete_rows)
    planned_delete_inventory = {
        name: digest for name, digest in inventory.items() if name not in keep_inventory
    }
    if delete_inventory != planned_delete_inventory:
        raise CleanupError("plan deletion rows do not equal inventory minus keep set")

    normalized_groups: list[tuple[str, list[str]]] = []
    grouped_names: set[str] = set()
    for group in delete_groups:
        if not isinstance(group, dict):
            raise CleanupError("plan contains an invalid digest deletion group")
        digest = _validate_digest(group.get("digest"), context="deletion group")
        tags = group.get("tags")
        if not isinstance(tags, list) or not tags:
            raise CleanupError("plan contains an empty or invalid digest deletion group")
        normalized_tags: list[str] = []
        for name in tags:
            if not isinstance(name, str) or name in grouped_names:
                raise CleanupError("plan contains an invalid or duplicate grouped tag")
            if delete_inventory.get(name) != digest:
                raise CleanupError(f"deletion group digest mismatch for tag: {name}")
            grouped_names.add(name)
            normalized_tags.append(name)
        if digest in keep_inventory.values():
            raise CleanupError("plan attempts to delete a protected manifest digest")
        normalized_groups.append((digest, sorted(normalized_tags)))

    if grouped_names != set(delete_inventory):
        raise CleanupError("digest deletion groups do not cover the exact delete set")

    expected_remaining = dict(inventory)
    _require_inventory(client.list_tags(), expected_remaining, context="before apply")
    mutation_attempted = False

    try:
        for digest, names in normalized_groups:
            for name in names:
                if verify_archive is not None:
                    verify_archive(name, digest)

            _require_inventory(
                client.list_tags(),
                expected_remaining,
                context=f"immediately before deleting digest {digest}",
            )
            before_delete = dict(expected_remaining)
            mutation_attempted = True
            client.delete_digest(digest)
            for name in names:
                del expected_remaining[name]
            if on_deleted is not None:
                on_deleted(names, digest, dict(expected_remaining))
            _wait_for_inventory(
                client,
                before=before_delete,
                expected=expected_remaining,
                context=f"after deleting digest {digest}",
                attempts=convergence_attempts,
                sleep=sleep,
            )

        if expected_remaining != keep_inventory:
            raise CleanupError("post-delete inventory does not equal the exact keep set")
        return inventory_rows(expected_remaining)
    except BaseException as error:
        if mutation_attempted and rollback is not None:
            try:
                rollback(inventory)
            except BaseException as rollback_error:
                raise CleanupError(
                    f"cleanup failed ({error}); rollback also failed ({rollback_error})"
                ) from rollback_error
        raise


def restore_inventory(
    client: RollbackClient,
    original: Mapping[str, str],
    backups: Mapping[str, ManifestBackup],
    *,
    stable_reads: int = 3,
    attempts: int = 24,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    if stable_reads < 1 or attempts < stable_reads:
        raise CleanupError("rollback retry settings are invalid")

    expected = dict(sorted(original.items()))
    for name, digest in expected.items():
        if name != "latest" and not SHA_TAG_PATTERN.fullmatch(name):
            raise CleanupError(f"rollback inventory contains an unexpected tag: {name}")
        _validate_digest(digest, context=f"rollback inventory {name}")
        if digest not in backups:
            raise CleanupError(f"rollback manifest backup is missing for {digest}")

    stable = 0
    for attempt in range(attempts):
        actual = canonical_inventory(client.list_tags())
        unexpected = sorted(set(actual) - set(expected))
        if unexpected:
            raise CleanupError(
                "rollback stopped rather than deleting unexpected tag(s): " + ", ".join(unexpected)
            )

        if actual == expected:
            stable += 1
            if stable >= stable_reads:
                return actual
        else:
            stable = 0
            for name, digest in expected.items():
                if actual.get(name) != digest:
                    client.restore_tag(name, backups[digest])

        if attempt + 1 < attempts:
            sleep(5)

    raise CleanupError("Docker Hub rollback did not converge to the original inventory")


class DockerHubClient:
    def __init__(self) -> None:
        self.token: str | None = None
        self.pull_token: str | None = None

    @staticmethod
    def _request_json(request: urllib.request.Request) -> object:
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise CleanupError(f"Docker Hub API returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise CleanupError(f"Docker Hub API request failed: {type(error).__name__}") from None

    def _get_pull_token(self) -> str:
        if self.token is not None:
            return self.token
        if self.pull_token is not None:
            return self.pull_token
        repository = f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}"
        query = urllib.parse.urlencode(
            {
                "service": "registry.docker.io",
                "scope": f"repository:{repository}:pull",
            }
        )
        payload = self._request_json(
            urllib.request.Request(
                f"{DOCKER_REGISTRY_AUTH}?{query}",
                headers={"User-Agent": USER_AGENT},
            )
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise CleanupError("Docker registry anonymous login response is missing a token")
        if "pull" not in self._registry_actions(token):
            raise CleanupError("Docker registry anonymous token is missing pull scope")
        self.pull_token = token
        return token

    def list_tags(self) -> list[dict[str, str]]:
        repository = f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}"
        token = self._get_pull_token()
        request = urllib.request.Request(
            f"{DOCKER_REGISTRY_BASE}/v2/{repository}/tags/list?n=1000",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = json.load(response)
                link = response.headers.get("Link")
        except urllib.error.HTTPError as error:
            raise CleanupError(f"Docker registry tag inventory returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise CleanupError(
                f"Docker registry tag inventory failed: {type(error).__name__}"
            ) from None

        if link:
            raise CleanupError("Docker registry tag inventory exceeds the 1000-tag safety limit")
        if not isinstance(payload, dict) or payload.get("name") != repository:
            raise CleanupError("Docker registry tag inventory has an invalid repository")
        names = payload.get("tags")
        if names is None:
            names = []
        if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
            raise CleanupError("Docker registry tag inventory has an invalid tag list")
        if len(names) != len(set(names)):
            raise CleanupError("Docker registry tag inventory contains a duplicate tag")

        rows: list[dict[str, str]] = []
        for name in sorted(names):
            encoded_name = urllib.parse.quote(name, safe="")
            manifest_request = urllib.request.Request(
                f"{DOCKER_REGISTRY_BASE}/v2/{repository}/manifests/{encoded_name}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": MANIFEST_ACCEPT,
                    "User-Agent": USER_AGENT,
                },
                method="HEAD",
            )
            try:
                with urllib.request.urlopen(
                    manifest_request,
                    timeout=TIMEOUT_SECONDS,
                ) as response:
                    digest = response.headers.get("Docker-Content-Digest")
            except urllib.error.HTTPError as error:
                raise CleanupError(
                    f"Docker registry manifest lookup failed for {name}: HTTP {error.code}"
                ) from None
            except (urllib.error.URLError, TimeoutError) as error:
                raise CleanupError(
                    f"Docker registry manifest lookup failed for {name}: {type(error).__name__}"
                ) from None
            rows.append({"name": name, "digest": _validate_digest(digest, context=name)})

        canonical_inventory(rows)
        return rows

    @staticmethod
    def _registry_actions(token: str) -> set[str]:
        parts = token.split(".")
        if len(parts) != 3:
            raise CleanupError("Docker registry token is not a JWT")
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
        except (ValueError, json.JSONDecodeError):
            raise CleanupError("Docker registry token has invalid claims") from None
        if not isinstance(claims, dict):
            raise CleanupError("Docker registry token has invalid claims")
        repository = f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}"
        for entry in claims.get("access", []):
            if (
                isinstance(entry, dict)
                and entry.get("type") == "repository"
                and entry.get("name") == repository
                and isinstance(entry.get("actions"), list)
            ):
                return {action for action in entry["actions"] if isinstance(action, str)}
        return set()

    def login(self, username: str, password: str) -> None:
        if username != DOCKERHUB_NAMESPACE:
            raise CleanupError("Docker Hub username does not match the repository namespace")
        if not password:
            raise CleanupError("Docker Hub PAT is missing")

        repository = f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}"
        query = urllib.parse.urlencode(
            {
                "service": "registry.docker.io",
                "scope": f"repository:{repository}:pull,push,delete",
            }
        )
        basic = base64.b64encode(f"{username}:{password}".encode()).decode()
        request = urllib.request.Request(
            f"{DOCKER_REGISTRY_AUTH}?{query}",
            headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT},
        )
        payload = self._request_json(request)
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise CleanupError("Docker registry login response is missing a token")
        if not {"pull", "push", "delete"}.issubset(self._registry_actions(token)):
            raise CleanupError("Docker registry token is missing pull, push, or delete scope")
        self.token = token
        self.pull_token = token

    def capture_manifest(self, digest: str) -> ManifestBackup:
        digest = _validate_digest(digest, context="Docker Hub manifest backup")
        token = self._get_pull_token()

        repository = f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}"
        request = urllib.request.Request(
            f"{DOCKER_REGISTRY_BASE}/v2/{repository}/manifests/{digest}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": MANIFEST_ACCEPT,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                content = response.read()
                media_type = (response.headers.get("Content-Type") or "").split(";", 1)[0]
                response_digest = response.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as error:
            raise CleanupError(
                f"Docker Hub manifest backup failed for {digest}: HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise CleanupError(
                f"Docker Hub manifest backup failed for {digest}: {type(error).__name__}"
            ) from None

        if media_type not in MANIFEST_MEDIA_TYPES:
            raise CleanupError(f"Docker Hub returned an unsupported manifest media type for {digest}")
        if response_digest is not None and response_digest != digest:
            raise CleanupError(f"Docker Hub manifest backup digest header mismatch for {digest}")
        content_digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if content_digest != digest:
            raise CleanupError(f"Docker Hub manifest backup content mismatch for {digest}")
        return ManifestBackup(digest=digest, media_type=media_type, content=content)

    def restore_tag(self, name: str, backup: ManifestBackup) -> None:
        if name != "latest" and not SHA_TAG_PATTERN.fullmatch(name):
            raise CleanupError(f"refusing to restore an unexpected tag name: {name}")
        if self.token is None:
            raise CleanupError("Docker Hub client is not authenticated")
        digest = _validate_digest(backup.digest, context=f"rollback tag {name}")
        if backup.media_type not in MANIFEST_MEDIA_TYPES:
            raise CleanupError(f"rollback manifest media type is invalid for {name}")
        if "sha256:" + hashlib.sha256(backup.content).hexdigest() != digest:
            raise CleanupError(f"rollback manifest content mismatch for {name}")

        repository = f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}"
        encoded_name = urllib.parse.quote(name, safe="")
        request = urllib.request.Request(
            f"{DOCKER_REGISTRY_BASE}/v2/{repository}/manifests/{encoded_name}",
            data=backup.content,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": backup.media_type,
                "User-Agent": USER_AGENT,
            },
            method="PUT",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status not in {201, 202}:
                    raise CleanupError(
                        f"unexpected Docker Hub rollback status for {name}: {response.status}"
                    )
        except urllib.error.HTTPError as error:
            raise CleanupError(f"Docker Hub rollback failed for {name}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise CleanupError(
                f"Docker Hub rollback request failed for {name}: {type(error).__name__}"
            ) from None

    def delete_digest(self, digest: str) -> None:
        digest = _validate_digest(digest, context="Docker Hub deletion")
        if self.token is None:
            raise CleanupError("Docker Hub client is not authenticated")

        repository = f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}"
        request = urllib.request.Request(
            f"{DOCKER_REGISTRY_BASE}/v2/{repository}/manifests/{digest}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": MANIFEST_ACCEPT,
                "User-Agent": USER_AGENT,
            },
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status != 202:
                    raise CleanupError(
                        f"unexpected Docker Hub digest-delete status for {digest}: {response.status}"
                    )
        except urllib.error.HTTPError as error:
            raise CleanupError(
                f"Docker Hub digest delete failed for {digest}: HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise CleanupError(
                f"Docker Hub digest delete request failed for {digest}: {type(error).__name__}"
            ) from None


class GHCRArchiveClient:
    def __init__(self) -> None:
        self.token: str | None = None

    def _get_token(self) -> str:
        if self.token is not None:
            return self.token
        query = urllib.parse.urlencode(
            {"service": "ghcr.io", "scope": f"repository:{GHCR_REPOSITORY}:pull"}
        )
        request = urllib.request.Request(
            f"https://ghcr.io/token?{query}",
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            raise CleanupError(f"GHCR token request returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise CleanupError(f"GHCR token request failed: {type(error).__name__}") from None
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise CleanupError("GHCR token response is missing a token")
        self.token = token
        return token

    def digest_for_tag(self, name: str) -> str:
        if not SHA_TAG_PATTERN.fullmatch(name):
            raise CleanupError(f"refusing to inspect a non-SHA archive tag: {name}")
        tag = urllib.parse.quote(name, safe="")
        request = urllib.request.Request(
            f"https://ghcr.io/v2/{GHCR_REPOSITORY}/manifests/{tag}",
            headers={
                "Accept": MANIFEST_ACCEPT,
                "Authorization": f"Bearer {self._get_token()}",
                "User-Agent": USER_AGENT,
            },
            method="HEAD",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                digest = response.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as error:
            raise CleanupError(f"GHCR archive lookup failed for {name}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise CleanupError(
                f"GHCR archive lookup failed for {name}: {type(error).__name__}"
            ) from None
        return _validate_digest(digest, context=f"GHCR archive {name}")

    def verify_digest(self, digest: str) -> None:
        digest = _validate_digest(digest, context="GHCR digest archive")
        request = urllib.request.Request(
            f"https://ghcr.io/v2/{GHCR_REPOSITORY}/manifests/{digest}",
            headers={
                "Accept": MANIFEST_ACCEPT,
                "Authorization": f"Bearer {self._get_token()}",
                "User-Agent": USER_AGENT,
            },
            method="HEAD",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                resolved = response.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as error:
            raise CleanupError(
                f"GHCR digest archive lookup failed for {digest}: HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise CleanupError(
                f"GHCR digest archive lookup failed for {digest}: {type(error).__name__}"
            ) from None
        if resolved != digest:
            raise CleanupError(f"GHCR digest archive mismatch for {digest}")


def write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-tag", default="")
    parser.add_argument("--expected-digest", default="")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--approved-plan", type=Path)
    parser.add_argument("--approved-plan-sha256", default="")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-freeze", default="")
    return parser.parse_args()


def _require_mutation_confirmations(args: argparse.Namespace) -> None:
    if args.confirm != DELETE_CONFIRMATION:
        raise CleanupError("apply confirmation phrase is missing or invalid")
    if args.writer_freeze != WRITER_FREEZE_CONFIRMATION:
        raise CleanupError("Docker Hub writer-freeze confirmation is missing or invalid")


def _read_report(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_bytes())
    except OSError as error:
        raise CleanupError(f"cleanup report could not be read: {type(error).__name__}") from None
    except json.JSONDecodeError:
        raise CleanupError("cleanup report is not valid JSON") from None
    if not isinstance(payload, dict):
        raise CleanupError("cleanup report is not an object")
    return payload


def recover_report(args: argparse.Namespace) -> int:
    if args.apply:
        raise CleanupError("--apply and --recover cannot be combined")
    _require_mutation_confirmations(args)
    report = _read_report(args.report)
    mode = report.get("mode")
    status = report.get("status")
    if mode not in {"apply", "dry-run"}:
        raise CleanupError("cleanup report is not eligible for recovery")
    if mode == "apply" and status == "applied":
        raise CleanupError("cleanup report is not eligible for recovery")
    if mode == "dry-run" and status != "planned":
        raise CleanupError("cleanup report is not an approved recovery baseline")
    if report.get("dockerhub_repository") != f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}":
        raise CleanupError("cleanup report repository does not match the fixed Docker Hub target")

    expected_tag = report.get("expected_tag")
    expected_digest = report.get("expected_digest")
    if not isinstance(expected_tag, str) or not isinstance(expected_digest, str):
        raise CleanupError("cleanup report expected identity is invalid")
    rollback_rows = report.get("rollback_inventory")
    original, _ = _validated_inventory(
        rollback_rows,
        expected_tag=expected_tag,
        expected_digest=expected_digest,
    )
    backups = manifest_backups_from_rows(report.get("rollback_manifests"))
    missing = sorted(set(original.values()) - set(backups))
    if missing:
        raise CleanupError("cleanup report is missing rollback manifest(s): " + ", ".join(missing))

    hub = DockerHubClient()
    hub.login(os.environ.get("DOCKER_USERNAME", ""), os.environ.get("DOCKER_PASSWORD", ""))
    live, _ = _validated_inventory(
        hub.list_tags(),
        expected_tag=expected_tag,
        expected_digest=expected_digest,
    )
    final_inventory = {"latest": expected_digest, expected_tag: expected_digest}
    if mode == "dry-run" and original != final_inventory and live == final_inventory:
        report["recovery_status"] = "not_needed_final_inventory"
        write_report(args.report, report)
        print("dockerhub_rollback=not-needed")
        return 0
    report["recovery_status"] = "running"
    write_report(args.report, report)
    try:
        restored = restore_inventory(hub, original, backups)
    except CleanupError as error:
        report["recovery_status"] = "failed"
        report["recovery_error"] = str(error)
        write_report(args.report, report)
        raise
    report["status"] = "recovered"
    report["recovery_status"] = "completed"
    report["remaining"] = inventory_rows(restored)
    write_report(args.report, report)
    print("dockerhub_rollback=completed")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.apply or args.recover:
        raise CleanupError(
            "destructive Docker Hub cleanup is disabled because the registry cannot "
            "atomically fence the complete tag reference set"
        )
    if args.recover:
        return recover_report(args)
    if not args.expected_tag or not args.expected_digest:
        raise CleanupError("expected tag and digest are required")
    if args.apply and (
        args.approved_plan is None or not args.approved_plan_sha256
    ):
        raise CleanupError("apply requires an exact hash-bound approved plan")

    report: dict[str, object] = {
        "schema_version": 2,
        "mode": "apply" if args.apply else "dry-run",
        "status": "initializing",
        "deleted": [],
        "deleted_digests": [],
    }
    write_report(args.report, report)

    try:
        hub = DockerHubClient()
        initial_tags = hub.list_tags()
        _, delete_names = _validated_inventory(
            initial_tags,
            expected_tag=args.expected_tag,
            expected_digest=args.expected_digest,
        )
        archive_client = GHCRArchiveClient()
        archive_digests: dict[str, str] = {}
        for name in delete_names:
            digest = archive_client.digest_for_tag(name)
            archive_client.verify_digest(digest)
            archive_digests[name] = digest
        plan = build_plan(
            initial_tags,
            expected_tag=args.expected_tag,
            expected_digest=args.expected_digest,
            archive_digests=archive_digests,
        )
        approved_report: dict[str, object] | None = None
        if args.apply:
            if args.approved_plan is None:
                raise CleanupError("approved plan path is missing")
            approved_report = load_approved_report(
                args.approved_plan,
                args.approved_plan_sha256,
            )
            approved_plan = {key: approved_report[key] for key in PLAN_KEYS}
            if approved_plan != plan:
                raise CleanupError("live plan does not exactly match the approved plan")
            report["approved_plan_sha256"] = args.approved_plan_sha256
        report.update(plan)
        original_inventory = canonical_inventory(plan["inventory"])
        if approved_report is None:
            backups = {
                digest: hub.capture_manifest(digest)
                for digest in sorted(set(original_inventory.values()))
            }
        else:
            approved_inventory = canonical_inventory(approved_report.get("rollback_inventory"))
            if approved_inventory != original_inventory:
                raise CleanupError("approved rollback inventory does not match the live plan")
            backups = manifest_backups_from_rows(approved_report.get("rollback_manifests"))
            missing_backups = sorted(set(original_inventory.values()) - set(backups))
            if missing_backups:
                raise CleanupError(
                    "approved plan is missing rollback manifest(s): "
                    + ", ".join(missing_backups)
                )
            for digest, backup in backups.items():
                if hub.capture_manifest(digest) != backup:
                    raise CleanupError(f"approved manifest backup drifted for {digest}")
        report["rollback_inventory"] = inventory_rows(original_inventory)
        report["rollback_manifests"] = manifest_backups_to_rows(backups)
        report["rollback_status"] = "ready"
        report["status"] = "planned"
        write_report(args.report, report)

        print(f"dockerhub_tag_cleanup_mode={report['mode']}")
        print(f"keep_tags=latest,{args.expected_tag}")
        print(f"delete_count={len(delete_names)}")
        print("ghcr_archive_verification=ok")

        if not args.apply:
            print("dockerhub_mutation=none")
            return 0
        _require_mutation_confirmations(args)

        username = os.environ.get("DOCKER_USERNAME", "")
        password = os.environ.get("DOCKER_PASSWORD", "")
        hub.login(username, password)
        report["status"] = "armed"
        write_report(args.report, report)

        def checkpoint(
            names: list[str],
            digest: str,
            remaining: Mapping[str, str],
        ) -> None:
            deleted = report["deleted"]
            deleted_digests = report["deleted_digests"]
            if not isinstance(deleted, list) or not isinstance(deleted_digests, list):
                raise CleanupError("report deletion fields are invalid")
            deleted.extend(names)
            deleted_digests.append({"digest": digest, "tags": names})
            report["remaining"] = inventory_rows(remaining)
            write_report(args.report, report)

        def verify_archive(name: str, expected_digest: str) -> None:
            current_digest = archive_client.digest_for_tag(name)
            if current_digest != expected_digest:
                raise CleanupError(
                    f"archive digest drifted before deleting manifest for {name}: "
                    f"expected={expected_digest}, actual={current_digest}"
                )
            archive_client.verify_digest(expected_digest)

        def rollback(original: Mapping[str, str]) -> None:
            report["status"] = "rolling_back"
            report["rollback_status"] = "running"
            write_report(args.report, report)
            restored = restore_inventory(hub, original, backups)
            report["status"] = "rolled_back"
            report["rollback_status"] = "completed"
            report["remaining"] = inventory_rows(restored)
            write_report(args.report, report)

        report["status"] = "applying"
        write_report(args.report, report)
        final_inventory = apply_plan(
            hub,
            plan,
            verify_archive=verify_archive,
            on_deleted=checkpoint,
            rollback=rollback,
        )
        report["status"] = "applied"
        report["final_inventory"] = final_inventory
        report["remaining"] = final_inventory
        write_report(args.report, report)
        deleted = report["deleted"]
        if not isinstance(deleted, list):
            raise CleanupError("report deleted field is invalid")
        print(f"deleted_count={len(deleted)}")
        deleted_digests = report["deleted_digests"]
        if not isinstance(deleted_digests, list):
            raise CleanupError("report deleted_digests field is invalid")
        print(f"deleted_digest_count={len(deleted_digests)}")
        print("dockerhub_exact_keep_set=ok")
        return 0
    except CleanupError as error:
        if report.get("rollback_status") == "completed":
            report["status"] = "failed_rolled_back"
        else:
            report["status"] = "failed"
        report["error"] = str(error)
        write_report(args.report, report)
        raise


def main() -> int:
    def handle_termination(signum: int, _frame: object) -> None:
        raise CleanupError(f"received termination signal {signum}")

    signal.signal(signal.SIGTERM, handle_termination)
    signal.signal(signal.SIGINT, handle_termination)
    args = parse_args()
    try:
        return run(args)
    except CleanupError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
