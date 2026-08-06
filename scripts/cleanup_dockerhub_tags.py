#!/usr/bin/env python3
"""Retain only ``latest`` and the current source-SHA tag on Docker Hub.

The script is fail-closed: it accepts only seven-character lowercase Git SHA
tags, verifies every deletion candidate has an exact-digest copy on GHCR, and
deletes the archived manifest by digest rather than by mutable tag name. It
re-reads the complete live tag set before each deletion and while the registry
change converges.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

DOCKERHUB_NAMESPACE = "woosungchoi"
DOCKERHUB_REPOSITORY = "docker-nginx-brotli"
DOCKERHUB_API_BASE = "https://hub.docker.com"
DOCKER_REGISTRY_AUTH = "https://auth.docker.io/token"
DOCKER_REGISTRY_BASE = "https://registry-1.docker.io"
GHCR_REPOSITORY = "woosungchoi/nginx-http3"
GHCR_REF_PREFIX = f"ghcr.io/{GHCR_REPOSITORY}"
USER_AGENT = "docker-nginx-brotli-tag-cleanup/1.0"
TIMEOUT_SECONDS = 30
DELETE_CONFIRMATION = "DELETE-OLD-DOCKERHUB-SHA-TAGS"
SHA_TAG_PATTERN = re.compile(r"^[0-9a-f]{7}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


class CleanupError(RuntimeError):
    pass


class TagClient(Protocol):
    def list_tags(self) -> list[dict[str, str]]: ...

    def delete_digest(self, digest: str) -> None: ...


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

    for digest, names in normalized_groups:
        for name in names:
            if verify_archive is not None:
                verify_archive(name, digest)

        before_delete = dict(expected_remaining)
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


class DockerHubClient:
    def __init__(self) -> None:
        self.token: str | None = None

    @staticmethod
    def _request_json(request: urllib.request.Request) -> object:
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise CleanupError(f"Docker Hub API returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise CleanupError(f"Docker Hub API request failed: {type(error).__name__}") from None

    def list_tags(self) -> list[dict[str, str]]:
        next_url = (
            f"{DOCKERHUB_API_BASE}/v2/repositories/"
            f"{DOCKERHUB_NAMESPACE}/{DOCKERHUB_REPOSITORY}/tags"
            "?page_size=100&page=1&ordering=name"
        )
        seen_urls: set[str] = set()
        rows: list[dict[str, str]] = []
        advertised_count: int | None = None

        while next_url:
            parsed = urllib.parse.urlparse(next_url)
            if parsed.scheme != "https" or parsed.netloc != "hub.docker.com":
                raise CleanupError("Docker Hub pagination returned an unexpected URL")
            if next_url in seen_urls or len(seen_urls) >= 100:
                raise CleanupError("Docker Hub pagination loop detected")
            seen_urls.add(next_url)

            payload = self._request_json(
                urllib.request.Request(next_url, headers={"User-Agent": USER_AGENT})
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise CleanupError("Docker Hub tag response has an invalid schema")
            count = payload.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise CleanupError("Docker Hub tag response has an invalid count")
            if advertised_count is None:
                advertised_count = count
            elif count != advertised_count:
                raise CleanupError("Docker Hub tag count changed during pagination")

            for row in payload["results"]:
                if not isinstance(row, dict):
                    raise CleanupError("Docker Hub tag response contains an invalid row")
                name = row.get("name")
                if not isinstance(name, str) or not name:
                    raise CleanupError("Docker Hub tag response contains an invalid name")
                digest = _validate_digest(row.get("digest"), context=name)
                rows.append({"name": name, "digest": digest})

            next_value = payload.get("next")
            if next_value is not None and not isinstance(next_value, str):
                raise CleanupError("Docker Hub tag response has an invalid next URL")
            next_url = next_value or ""

        if advertised_count != len(rows):
            raise CleanupError(
                f"Docker Hub tag count mismatch: advertised={advertised_count}, received={len(rows)}"
            )
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
        if "delete" not in self._registry_actions(token):
            raise CleanupError("Docker registry token is missing delete scope")
        self.token = token

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
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-tag", required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
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
        archive_digests = {
            name: archive_client.digest_for_tag(name) for name in delete_names
        }
        plan = build_plan(
            initial_tags,
            expected_tag=args.expected_tag,
            expected_digest=args.expected_digest,
            archive_digests=archive_digests,
        )
        report.update(plan)
        report["status"] = "planned"
        write_report(args.report, report)

        print(f"dockerhub_tag_cleanup_mode={report['mode']}")
        print(f"keep_tags=latest,{args.expected_tag}")
        print(f"delete_count={len(delete_names)}")
        print("ghcr_archive_verification=ok")

        if not args.apply:
            print("dockerhub_mutation=none")
            return 0
        if args.confirm != DELETE_CONFIRMATION:
            raise CleanupError("apply confirmation phrase is missing or invalid")

        username = os.environ.get("DOCKER_USERNAME", "")
        password = os.environ.get("DOCKER_PASSWORD", "")
        hub.login(username, password)
        report["status"] = "applying"
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

        final_inventory = apply_plan(
            hub,
            plan,
            verify_archive=verify_archive,
            on_deleted=checkpoint,
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
        report["status"] = "failed"
        report["error"] = str(error)
        write_report(args.report, report)
        raise


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except CleanupError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
