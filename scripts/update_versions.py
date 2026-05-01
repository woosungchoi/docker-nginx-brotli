#!/usr/bin/env python3
"""Update Dockerfile dependency pins for nginx stable, PCRE2, and zlib.

This script is intended for local use and for the scheduled GitHub Actions workflow.
It updates the following ENV assignments in the repository Dockerfile:

- NGINX_VERSION: latest nginx stable release from https://nginx.org/download/
- PCRE_VERSION: latest stable PCRE2 release from GitHub
- ZLIB_VERSION: latest stable zlib release from GitHub

Exit codes:
- 0: success, with or without changes
- 1: runtime or validation error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Tuple

NGINX_DOWNLOAD_URL = "https://nginx.org/download/"
PCRE2_RELEASE_URL = "https://api.github.com/repos/PCRE2Project/pcre2/releases/latest"
ZLIB_RELEASE_URL = "https://api.github.com/repos/madler/zlib/releases/latest"
DOCKERFILE_PATH = Path(__file__).resolve().parents[1] / "Dockerfile"
USER_AGENT = "docker-nginx-brotli-version-updater/1.0"
TIMEOUT_SECONDS = 30

VERSION_PATTERNS = {
    "NGINX_VERSION": re.compile(r"^(ENV\s+NGINX_VERSION\s+)(\S+)(\s*)$", re.MULTILINE),
    "PCRE_VERSION": re.compile(r"^(ENV\s+PCRE_VERSION\s+)(\S+)(\s*)$", re.MULTILINE),
    "ZLIB_VERSION": re.compile(r"^(ENV\s+ZLIB_VERSION\s+)(\S+)(\s*)$", re.MULTILINE),
}


class UpdateError(RuntimeError):
    pass


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise UpdateError(f"failed to fetch {url}: {exc}") from exc


def fetch_json(url: str) -> dict:
    return json.loads(fetch_text(url))


def parse_semver(version: str) -> Tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise UpdateError(f"invalid version string: {version}") from exc


def latest_nginx_stable_version(html: str) -> str:
    matches = set(re.findall(r"nginx-(\d+\.\d+\.\d+)\.tar\.gz", html))
    if not matches:
        raise UpdateError("could not find any nginx release versions on nginx.org/download/")

    stable_versions = [
        version for version in matches if parse_semver(version)[1] % 2 == 0
    ]
    if not stable_versions:
        raise UpdateError("could not find any nginx stable releases (even minor series)")

    return max(stable_versions, key=parse_semver)


def latest_github_release_version(url: str, *, prefix_to_strip: str = "") -> str:
    payload = fetch_json(url)
    tag_name = payload.get("tag_name")
    if not tag_name or not isinstance(tag_name, str):
        raise UpdateError(f"GitHub release response missing tag_name for {url}")
    if prefix_to_strip and tag_name.startswith(prefix_to_strip):
        return tag_name[len(prefix_to_strip) :]
    return tag_name


def extract_current_versions(text: str) -> Dict[str, str]:
    versions: Dict[str, str] = {}
    for key, pattern in VERSION_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            raise UpdateError(f"could not find {key} in Dockerfile")
        versions[key] = match.group(2)
    return versions


def replace_versions(text: str, versions: Dict[str, str]) -> str:
    updated = text
    for key, value in versions.items():
        pattern = VERSION_PATTERNS[key]
        updated, count = pattern.subn(
            lambda match, replacement=value: f"{match.group(1)}{replacement}{match.group(3)}",
            updated,
            count=1,
        )
        if count != 1:
            raise UpdateError(f"failed to update {key} in Dockerfile")
    return updated


def format_version_summary(current: Dict[str, str], latest: Dict[str, str]) -> Iterable[str]:
    for key in ("NGINX_VERSION", "PCRE_VERSION", "ZLIB_VERSION"):
        status = "unchanged" if current[key] == latest[key] else "updated"
        yield f"{key}: {current[key]} -> {latest[key]} ({status})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dockerfile",
        type=Path,
        default=DOCKERFILE_PATH,
        help="Path to the Dockerfile to update",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if updates are available without writing changes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved versions without writing changes",
    )
    args = parser.parse_args()

    dockerfile_path = args.dockerfile.resolve()
    original_text = dockerfile_path.read_text(encoding="utf-8")
    current_versions = extract_current_versions(original_text)

    latest_versions = {
        "NGINX_VERSION": latest_nginx_stable_version(fetch_text(NGINX_DOWNLOAD_URL)),
        "PCRE_VERSION": latest_github_release_version(PCRE2_RELEASE_URL, prefix_to_strip="pcre2-"),
        "ZLIB_VERSION": latest_github_release_version(ZLIB_RELEASE_URL, prefix_to_strip="v"),
    }

    for line in format_version_summary(current_versions, latest_versions):
        print(line)

    changed = current_versions != latest_versions
    if args.check:
        return 1 if changed else 0

    if args.dry_run:
        return 0

    if not changed:
        print("No Dockerfile changes required.")
        return 0

    updated_text = replace_versions(original_text, latest_versions)
    dockerfile_path.write_text(updated_text, encoding="utf-8")
    print(f"Updated {dockerfile_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
