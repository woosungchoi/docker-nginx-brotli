#!/usr/bin/env python3
"""Refresh reviewed Dockerfile source/base input pins atomically.

Track stable NGINX, PCRE2 and zlib versions with matching downloaded SHA256s,
external module commits, and the official Alpine digest on the existing release
branch. APK repository contents are rolling; see README for that limitation.
Use --dry-run to resolve inputs without writing, or --check to detect drift.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

NGINX_DOWNLOAD_URL = "https://nginx.org/download/"
PCRE2_RELEASE_URL = "https://api.github.com/repos/PCRE2Project/pcre2/releases/latest"
ZLIB_RELEASE_URL = "https://api.github.com/repos/madler/zlib/releases/latest"
DOCKERFILE_PATH = Path(__file__).resolve().parents[1] / "Dockerfile"
USER_AGENT = "docker-nginx-brotli-version-updater/1.0"
TIMEOUT_SECONDS = 30

VERSION_PATTERNS = {
    "NGINX_VERSION": re.compile(r"^(ENV\s+NGINX_VERSION(?:\s+|=))(\S+)(\s*)$", re.MULTILINE),
    "PCRE_VERSION": re.compile(r"^(ENV\s+PCRE_VERSION(?:\s+|=))(\S+)(\s*)$", re.MULTILINE),
    "ZLIB_VERSION": re.compile(r"^(ENV\s+ZLIB_VERSION(?:\s+|=))(\S+)(\s*)$", re.MULTILINE),
}

PIN_KEYS = ("NGINX_VERSION", "PCRE_VERSION", "ZLIB_VERSION",
            "NGINX_SHA256", "PCRE_SHA256", "ZLIB_SHA256",
            "BROTLI_COMMIT", "HEADERS_MORE_COMMIT", "COOKIE_FLAG_COMMIT", "ALPINE_IMAGE")
PIN_PATTERNS = {key: re.compile(rf"^({'ARG' if key == 'ALPINE_IMAGE' else 'ENV'}\s+{key}=)(\S+)$", re.MULTILINE)
                for key in PIN_KEYS}
MODULE_REPOS = {"BROTLI_COMMIT": "google/ngx_brotli",
                "HEADERS_MORE_COMMIT": "openresty/headers-more-nginx-module",
                "COOKIE_FLAG_COMMIT": "AirisX/nginx_cookie_flag_module"}


def extract_pins(text: str) -> dict[str, str]:
    pins = {}
    for key, pattern in PIN_PATTERNS.items():
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise UpdateError(f"expected exactly one {key} pin")
        pins[key] = matches[0].group(2)
    validate_pins(pins)
    return pins


def validate_pins(pins: dict[str, str]) -> None:
    for key in PIN_KEYS:
        value = pins[key]
        pattern = (r"[0-9]+\.[0-9]+(?:\.[0-9]+)?" if key.endswith("VERSION") else
                   r"[0-9a-f]{64}" if key.endswith("SHA256") else
                   r"alpine:[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}" if key == "ALPINE_IMAGE" else
                   r"[0-9a-f]{40}")
        if not re.fullmatch(pattern, value):
            raise UpdateError(f"invalid {key} pin")


def replace_pins(text: str, pins: dict[str, str]) -> str:
    validate_pins(pins)
    extract_pins(text)
    for key, value in pins.items():
        text = PIN_PATTERNS[key].sub(lambda m: m.group(1) + value, text)
    return text


def archive_checksum(url: str) -> str:
    # Anonymous downloads only; credentials are restricted to the GitHub API.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
    except urllib.error.URLError as exc:
        raise UpdateError(f"archive download failed: {url}: {exc}") from exc
    return digest.hexdigest()


def resolve_pins(current: dict[str, str], versions: dict[str, str]) -> dict[str, str]:
    pins = current | versions
    urls = {
        "NGINX_SHA256": f"https://nginx.org/download/nginx-{versions['NGINX_VERSION']}.tar.gz",
        "PCRE_SHA256": f"https://github.com/PCRE2Project/pcre2/releases/download/pcre2-{versions['PCRE_VERSION']}/pcre2-{versions['PCRE_VERSION']}.tar.gz",
        "ZLIB_SHA256": f"https://github.com/madler/zlib/releases/download/v{versions['ZLIB_VERSION']}/zlib-{versions['ZLIB_VERSION']}.tar.gz",
    }
    for key, url in urls.items():
        pins[key] = archive_checksum(url)
    for key, repo in MODULE_REPOS.items():
        pins[key] = fetch_json(f"https://api.github.com/repos/{repo}/commits/HEAD")["sha"]
    alpine_tag = current["ALPINE_IMAGE"].split("@")[0]
    try:
        output = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", alpine_tag], text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise UpdateError("could not resolve official Alpine manifest digest") from exc
    match = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})$", output, re.MULTILINE)
    if not match:
        raise UpdateError("missing Alpine manifest digest")
    pins["ALPINE_IMAGE"] = alpine_tag + "@" + match.group(1)
    validate_pins(pins)
    return pins


class UpdateError(RuntimeError):
    pass


def fetch_text(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    if urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise UpdateError(f"failed to fetch {url}: {exc}") from exc


def fetch_json(url: str) -> dict:
    return json.loads(fetch_text(url))


def parse_semver(version: str) -> tuple[int, ...]:
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


def extract_current_versions(text: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for key, pattern in VERSION_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            raise UpdateError(f"could not find {key} in Dockerfile")
        versions[key] = match.group(2)
    return versions


def replace_versions(text: str, versions: dict[str, str]) -> str:
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


def format_version_summary(
    current: dict[str, str], latest: dict[str, str]
) -> Iterable[str]:
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

    current_pins = extract_pins(original_text)
    latest_pins = resolve_pins(current_pins, latest_versions)
    for key in PIN_KEYS:
        if key not in VERSION_PATTERNS:
            print(f"{key}: {current_pins[key]} -> {latest_pins[key]}")
    changed = current_pins != latest_pins
    if args.check:
        return 1 if changed else 0

    if args.dry_run:
        return 0

    if not changed:
        print("No Dockerfile changes required.")
        return 0

    updated_text = replace_pins(original_text, latest_pins)
    dockerfile_path.write_text(updated_text, encoding="utf-8")
    print(f"Updated {dockerfile_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
