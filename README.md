# docker-nginx-brotli

Alpine NGINX base image with HTTP/2, HTTP/3, Brotli, headers-more and Cookie-Flag.
GitHub Actions is the supported publisher; legacy Docker Hub hooks remain no-ops.

- Primary: `ghcr.io/woosungchoi/nginx-http3`
- Compatibility mirror: `docker.io/woosungchoi/docker-nginx-brotli`
- Platforms: `linux/amd64`, `linux/arm64`, `linux/arm/v6`, `linux/arm/v7`

## Usage

The default image serves `/usr/share/nginx/html` over HTTP without a bundled private key.
Use both [nginx.conf](nginx.conf) and [h3.nginx.conf](h3.nginx.conf) together for HTTPS/HTTP2/HTTP3. The top-level config loads Brotli and includes the service config. Supply certificates for your domain through read-only mounts:

```bash
docker run --rm --stop-timeout 30 \
  -p 80:80 -p 443:443/tcp -p 443:443/udp \
  --mount type=bind,src="$(pwd)/nginx.conf",dst=/etc/nginx/nginx.conf,readonly \
  --mount type=bind,src="$(pwd)/h3.nginx.conf",dst=/etc/nginx/conf.d/h3.nginx.conf,readonly \
  --mount type=bind,src=/path/to/localhost.pem,dst=/etc/ssl/localhost.pem,readonly \
  --mount type=bind,src=/path/to/localhost.key,dst=/etc/ssl/private/localhost.key,readonly \
  ghcr.io/woosungchoi/nginx-http3:latest
```

HTTP3 needs UDP reachability on the HTTPS port. The example enables TLS 1.2/1.3 and disables early data. Obsolete HTTP2 push and h3-29 advertisements have been removed.
Rate limits inherit from `http` into every included service server: 5 requests/s, burst 10, and 10 connections per client IP, with 429 rejection status. Tune these values for your traffic. Client identity remains the direct peer address; configure narrowly scoped trusted proxies separately when needed.
Docker sends SIGQUIT; examples give active workers 25 seconds to finish. Drain traffic first and allow Docker at least 30 seconds before forced termination.

## Validation and publication

`smoke-test` builds and executes all four published architectures. It loads every deployed module and requires default/combined configs, exact 200/body, dynamic Brotli, h2 ALPN/response, UDP HTTP3-only negotiation, TLS version acceptance/rejection, service rate limiting and an active response during graceful stop. Intentional h3 syntax and Brotli loader faults must fail.
`ci` runs source/lint/security checks and Python safety contracts. On a master push, the publisher requires both source/security and four-architecture runtime validation on that merge commit before updating registry tags. SBOM/provenance, manifest verification, cosign signing and audit-only retention remain enabled.

## Release publishing and Docker Hub mirror setup

The workflow always logs in to GHCR and publishes these GHCR tags:

- `ghcr.io/woosungchoi/nginx-http3:latest`
- `ghcr.io/woosungchoi/nginx-http3:<branch-or-tag>`
- `ghcr.io/woosungchoi/nginx-http3:<short-sha>`

Release images are intentionally tied to immutable commit SHA tags as well as `latest`. After publishing, the workflow verifies that every GHCR and optional Docker Hub mirror tag resolves to the build output digest and includes all expected platforms (`linux/amd64`, `linux/arm64`, `linux/arm/v6`, and `linux/arm/v7`). It also emits BuildKit SBOM/provenance attestations and uses GitHub OIDC keyless cosign signing for the published image digest. GitHub Releases are optional for this image-first repository; if release notes are needed, create a release that references the published image digest and short SHA.

If both repository secrets below are configured and the workflow is running from `refs/heads/master`, it also logs in to Docker Hub and mirrors the release to the legacy Docker Hub repository name:

- `DOCKER_USERNAME`
- `DOCKER_PASSWORD` (a Docker Hub PAT with read and write permission)

Each publish updates these two release references:

- `woosungchoi/docker-nginx-brotli:latest`
- `woosungchoi/docker-nginx-brotli:<current-short-sha>`

After manifest verification and cosign signing, the workflow runs `scripts/cleanup_dockerhub_tags.py` in dry-run mode and uploads the resulting inventory, candidate list, and SHA-256 checksum as audit evidence. It does **not** delete Docker Hub tags, so older seven-character SHA tags can accumulate between manual reviews. Every deletion candidate must remain addressable by exact digest in `ghcr.io/woosungchoi/nginx-http3`; GHCR keeps the historical SHA tags for rollback.

Destructive automation is intentionally disabled. Docker Hub manifest deletion by digest removes every tag currently referencing that digest, while Docker Hub provides no atomic operation that both freezes and conditionally validates the complete tag-reference set. A workflow concurrency group and an operator acknowledgement cannot fence Docker Hub UI/API clients or other external writers. Automation must remain audit-only until a registry-native conditional operation or a technically enforced transaction-wide writer fence exists.

If either Docker Hub secret is missing, Docker Hub mirroring and its retention plan are skipped while GHCR publishing continues. A configured username other than the fixed `woosungchoi` namespace fails before publishing. The image workflow is triggered only by pushes to `master`; it has no branch-selectable manual trigger.

## Automated input updates

The Dockerfile pins the official Alpine multi-architecture manifest digest, all three archive versions/SHA256 checksums, and all external module commits. Brotli's recursive submodule uses the gitlink in its pinned parent commit. All GitHub Actions use immutable SHA references.
`scripts/update_versions.py` tracks the latest even-minor stable NGINX release, PCRE2 and zlib releases, resolves matching downloaded archive checksums, refreshes module commits, and refreshes the Alpine digest within the existing release branch. It validates the complete pin set before writing. The build checks every archive before source compilation.
APK repositories remain rolling within Alpine 3.23; source/base pinning does not promise byte-for-byte reproduction of package resolution. The final image records installed versions at `/usr/share/nginx/apk-runtime.txt`; SBOM/provenance capture publication evidence. An immutable APK mirror would be separate infrastructure work.

The weekly updater opens `ci/update-pinned-versions` PRs using the configured GitHub App (`docker-nginx-brotli-automation[bot]`; existing App ID/private-key repository settings). It does not enable a second auto-merge path.
The merge workflow uses that same existing App credential after eligibility checks so a successful dependency merge triggers normal publication. `gh` represents this App author as `app/docker-nginx-brotli-automation`.
The trusted default-branch policy in `scripts/dependency_policy.py` is the single merge path. It requires the exact App author, same-repository dependency head, master base, both dependency labels, and changes only to recognized Dockerfile pins. Source, security and aggregated four-architecture smoke checks must all succeed on the current head. Missing, skipped, cancelled, neutral and stale results block merging. GitHub protection still applies; the merge command matches the checked head commit.

- Automatic: non-downgrade updates within the same NGINX stable branch, matching version/checksum changes, recognized module commits and same-branch Alpine digest updates.
- Manual: NGINX/Alpine release-branch changes, unchanged-version archive drift, or changes outside the pin allowlist.

```bash
python3 scripts/update_versions.py --dry-run
python3 scripts/update_versions.py --check
```

## Local validation

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r tests/requirements.txt
docker build -t nginx-http3:local .
.venv/bin/python scripts/runtime_smoke.py nginx-http3:local --platform linux/amd64
.venv/bin/python -m unittest discover -s tests -v
```

Use your native platform or an emulator in your own test environment. CI uses native amd64/arm64 and QEMU for arm/v6 and arm/v7. Smoke uses disposable local certificates and isolated loopback-only containers. Keys never enter the build context or published image. The hash-locked aioquic HTTP3 client has no HTTP1/HTTP2 fallback.

## Features

- HTTP/2, HTTP/3 and TLS 1.2/1.3 with Alpine OpenSSL
- Dynamic/static Brotli, headers-more and Cookie-Flag
- PCRE2 JIT and pinned zlib sources
- XSLT, image filter, GeoIP and Perl dynamic modules, with load/dependency checks

### Opting into early data

Keep `ssl_early_data off` unless your application has a replay policy. TLS early data can be replayed. If explicitly enabling it, reject replay-sensitive requests with 425 when `$ssl_early_data` is set, pass `Early-Data: $ssl_early_data` to upstream applications, and apply application-side replay protection before accepting state changes. Verify your TLS backend and clients; the default CI contract tests early data off.

References: [NGINX HTTP3](https://nginx.org/en/docs/http/ngx_http_v3_module.html), [Brotli dynamic loading](https://github.com/google/ngx_brotli#dynamically-loaded), [NGINX signals](https://nginx.org/en/docs/control.html), [TLS early data](https://nginx.org/en/docs/http/ngx_http_ssl_module.html#ssl_early_data).
