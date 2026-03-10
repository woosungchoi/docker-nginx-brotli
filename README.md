# docker-nginx-brotli

Alpine Linux image with nginx `latest` with, TLSv1.3, 0-RTT, brotli, NJS, Cookie-Flag support. All built on the bleeding edge. Built on the edge, for the edge.

Images for this are available on [Docker Hub](https://hub.docker.com/r/woosungchoi/docker-nginx-brotli).

## Architecture support

The Docker Hub autobuild hooks are configured to publish a single multi-arch manifest for `latest` containing both `linux/amd64` and `linux/arm64` from one `buildx --platform ... --push` invocation. That avoids the older branch-split flow where each branch pushed only one architecture and the final tag could lose one platform. The build hook only publishes from the release branch (`master`/`main`) so stale architecture-specific branches do not overwrite `latest`.

This assumes the Docker Hub autobuild environment supports `docker buildx` and a privileged `binfmt` helper container.

## Usage

**Docker Hub:** `docker pull woosungchoi/docker-nginx-brotli`

This is a base image like the default _nginx_ image. It is meant to be used as a drop-in replacement for the nginx base image.

Best practice example Nginx configs are available in this repo. See [_nginx.conf_](nginx.conf) and [_h3.nginx.conf_](h3.nginx.conf).

Example:

```Dockerfile
# Base Nginx HTTP/2 Image
FROM woosungchoi/docker-nginx-brotli:latest

# Copy your certs.
COPY localhost.key /etc/ssl/private/
COPY localhost.pem /etc/ssl/

# Copy your configs.
COPY nginx.conf /etc/nginx/
COPY h3.nginx.conf /etc/nginx/conf.d/
```

**NOTE**: Please note that you need a valid [CA](https://en.wikipedia.org/wiki/Certificate_authority) signed certificate for the client to upgrade you to HTTP/2. [Let's Encrypt](https://letsencrypt.org/) is a option for getting a free valid CA signed certificate.

## Automated version pin updates

The Dockerfile pins `NGINX_VERSION`, `PCRE_VERSION`, and `ZLIB_VERSION` on purpose.
A scheduled GitHub Actions workflow now checks for upstream releases and opens a pull request when those pins need to move.

Key files:

- `scripts/update_versions.py` - resolves the latest supported versions and rewrites the Dockerfile when needed
- `.github/workflows/update-versions.yml` - runs weekly on a schedule and on manual dispatch, then opens or updates a PR via `peter-evans/create-pull-request`

### What gets tracked

- **NGINX:** latest **mainline** release only, parsed from `https://nginx.org/download/`
  - The updater looks for `nginx-X.Y.Z.tar.gz` entries and keeps only versions where the **minor** number `Y` is odd.
  - Example: `1.29.x` is mainline, `1.28.x` is stable.
- **PCRE2:** latest stable GitHub release from `PCRE2Project/pcre2`
- **zlib:** latest stable GitHub release from `madler/zlib`

### How PR creation works

The workflow does not auto-merge anything.
If the updater changes the Dockerfile, GitHub Actions commits those changes to a dedicated branch (`ci/update-pinned-versions`) and opens or refreshes a pull request against the default branch using the standard `GITHUB_TOKEN`.

That means the usual GitHub Actions defaults should be enough as long as repository settings allow workflows to create branches and pull requests.

### Local validation

You can inspect what the updater would do without editing files:

```bash
python3 scripts/update_versions.py --dry-run
```

To fail when updates are available (useful for local checks):

```bash
python3 scripts/update_versions.py --check
```

## Features

- HTTP/2 (with Server Push)
- BoringSSL (Google's flavor of OpenSSL)
- TLS 1.3 **with 0-RTT support**
- Brotli compression
- [headers-more-nginx-module](https://github.com/openresty/headers-more-nginx-module)
- [NJS](https://www.nginx.com/blog/introduction-nginscript/)
- [nginx_cookie_flag_module](https://www.nginx.com/products/nginx/modules/cookie-flag/)
- PCRE latest with [JIT compilation](http://nginx.org/en/docs/ngx_core_module.html#pcre_jit) enabled
- zlib latest
- Alpine Linux (total size of **10 MB** compressed)

## HTTP/2 with Server Push

![alt](https://user-images.githubusercontent.com/7084995/67162942-654ff300-f337-11e9-9dc0-6d7a915d517c.png)

## TLS v1.3

![ssllabs](https://user-images.githubusercontent.com/7084995/67164526-89b4cb00-f349-11e9-87a2-d2dc81610ed4.png)

### 0-RTT Proof

![tls-0-rtt](https://user-images.githubusercontent.com/7084995/67163692-08a50600-f340-11e9-830c-c8a11c824a1f.png)

### Testing 0-RTT

```bash
host=domain.example.com # Replace your domain.
echo -e "GET / HTTP/1.1\r\nHost: $host\r\nConnection: close\r\n\r\n" > request.txt
openssl s_client -connect $host:443 -tls1_3 -sess_out session.pem -ign_eof < request.txt
openssl s_client -connect $host:443 -tls1_3 -sess_in session.pem -early_data request.txt
```