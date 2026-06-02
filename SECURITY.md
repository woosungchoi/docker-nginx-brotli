# Security Policy

## Supported versions

This repository publishes the `ghcr.io/woosungchoi/nginx-http3` container image from the default branch. The supported image line is the current `latest` image and immutable short-SHA tags produced by GitHub Actions.

Dependency pins are maintained in the Dockerfile and refreshed by the scheduled `update pinned dependency versions` workflow:

- nginx: latest stable branch release
- PCRE2: latest stable GitHub release
- zlib: latest stable GitHub release

Routine dependency updates are validated by the `smoke-test` workflow before merge. nginx stable branch changes require manual review.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub Security Advisories for this repository when available. If advisories are not available to you, contact the maintainer through the GitHub profile linked from this repository.

Please include:

- affected image tag or commit SHA
- reproduction steps or a minimal proof of concept
- expected impact
- relevant upstream CVE or advisory links, if known

Do not open a public issue for a vulnerability until a fix or mitigation is available.

## Maintainer response

The maintainer will triage reports against the Dockerfile, bundled nginx modules, and published image workflow. Confirmed vulnerabilities are fixed by updating pinned dependency versions, adjusting build configuration, or documenting mitigations as appropriate.
