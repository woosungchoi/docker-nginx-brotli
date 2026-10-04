#!/usr/bin/env python3
"""Evaluate App dependency PRs using trusted base code and exact current head checks."""
from __future__ import annotations

import argparse
import json
import subprocess
from scripts.update_versions import PIN_PATTERNS, extract_pins, parse_semver

TRUSTED_AUTHOR = "docker-nginx-brotli-automation[bot]"
REQUIRED_CHECKS = {"docker-smoke", "workflow and source checks", "trivy repository scan"}


def permitted_change(base: str, head: str) -> bool:
    old, new = extract_pins(base), extract_pins(head)
    if parse_semver(old["NGINX_VERSION"])[:2] != parse_semver(new["NGINX_VERSION"])[:2]:
        return False
    if old["ALPINE_IMAGE"].split("@")[0] != new["ALPINE_IMAGE"].split("@")[0]:
        return False
    for key in ("NGINX_VERSION", "PCRE_VERSION", "ZLIB_VERSION"):
        if parse_semver(new[key]) < parse_semver(old[key]):
            return False
        checksum = key.removesuffix("VERSION") + "SHA256"
        if new[key] == old[key] and new[checksum] != old[checksum]:
            return False
        if new[key] != old[key] and new[checksum] == old[checksum]:
            return False
    for pattern in PIN_PATTERNS.values():
        base = pattern.sub(lambda m: m.group(1) + "<pin>", base)
        head = pattern.sub(lambda m: m.group(1) + "<pin>", head)
    return base == head and old != new


def eligible(pr: dict, files: list, checks: list, base: str, head: str) -> bool:
    if not (pr["state"] == "OPEN" and not pr["isDraft"]
            and pr["author"]["login"] == TRUSTED_AUTHOR
            and pr["baseRefName"] == "master"
            and pr["headRefName"] == "ci/update-pinned-versions"
            and not pr["isCrossRepository"] and pr["mergeStateStatus"] == "CLEAN"
            and {"dependencies", "automated pr"} <= {x["name"] for x in pr["labels"]}
            and files == ["Dockerfile"]):
        return False
    latest = {}
    for check in sorted(checks, key=lambda c: c["id"]):
        latest[check["name"]] = check
    if not all(name in latest and latest[name]["status"] == "completed"
               and latest[name]["conclusion"] == "success"
               and latest[name]["head_sha"] == pr["headRefOid"]
               and latest[name]["app"]["slug"] == "github-actions"
               for name in REQUIRED_CHECKS):
        return False
    return permitted_change(base, head)


def gh(*args: str):
    return json.loads(subprocess.check_output(["gh", *args], text=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    args = parser.parse_args()
    fields = "state,isDraft,author,baseRefName,baseRefOid,headRefName,headRefOid,isCrossRepository,mergeStateStatus,labels"
    pr = gh("pr", "view", args.pr, "--repo", args.repo, "--json", fields)
    file_pages = gh("api", f"repos/{args.repo}/pulls/{args.pr}/files", "--paginate", "--slurp")
    files = [f["filename"] for page in file_pages for f in page]
    check_pages = gh("api", f"repos/{args.repo}/commits/{pr['headRefOid']}/check-runs", "--paginate", "--slurp")
    checks = [c for page in check_pages for c in page["check_runs"]]
    def dockerfile(sha: str) -> str:
        return subprocess.check_output(["gh", "api", f"repos/{args.repo}/contents/Dockerfile?ref={sha}",
                                        "-H", "Accept: application/vnd.github.raw+json"], text=True)
    if not eligible(pr, files, checks, dockerfile(pr["baseRefOid"]), dockerfile(pr["headRefOid"])):
        print("Dependency PR requires manual review or successful current-head checks.")
        return
    # GitHub enforces protection and --match-head-commit closes the head-update race.
    subprocess.run(["gh", "pr", "merge", args.pr, "--repo", args.repo, "--squash",
                    "--delete-branch", "--match-head-commit", pr["headRefOid"]], check=True)


if __name__ == "__main__":
    main()
