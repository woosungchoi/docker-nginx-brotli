#!/usr/bin/env python3
"""Evaluate App dependency PRs using trusted base code and exact current head checks."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from scripts.update_versions import PIN_PATTERNS, extract_pins, parse_semver

TRUSTED_AUTHOR = "app/docker-nginx-brotli-automation"
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
            and pr["author"]["login"] == TRUSTED_AUTHOR and pr["author"].get("is_bot") is True
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


def merge_checked_pr(repo: str, number: str, head: str) -> None:
    # Use the existing App for the merge so its push event triggers normal publication.
    # Read APIs use the workflow GITHUB_TOKEN; no token is printed or written to disk.
    merge_env = os.environ | {"GH_TOKEN": os.environ["DEPENDENCY_MERGE_TOKEN"]}
    subprocess.run(["gh", "pr", "merge", number, "--repo", repo, "--squash",
                    "--delete-branch", "--match-head-commit", head], check=True, env=merge_env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--expected-head")
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
    allowed = eligible(pr, files, checks, dockerfile(pr["baseRefOid"]), dockerfile(pr["headRefOid"]))
    if args.expected_head and args.expected_head != pr["headRefOid"]:
        allowed = False
    if args.check_only:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"eligible={str(allowed).lower()}\nhead={pr['headRefOid']}\n")
        return
    if not allowed:
        print("Dependency PR requires manual review or successful current-head checks.")
        return
    # GitHub enforces protection and --match-head-commit closes the head-update race.
    merge_checked_pr(args.repo, args.pr, pr["headRefOid"])


if __name__ == "__main__":
    main()
