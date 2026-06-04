---
title: docker-nginx-brotli 고도화 실행 계획
created: 2026-06-04
updated: 2026-06-04
status: draft-ready
project: woosungchoi/docker-nginx-brotli
github: https://github.com/woosungchoi/docker-nginx-brotli
baseline_commit: 6f8b89b612b9f20a4c56c8ad7a4f6b15c4b6848f
baseline_branch: master
tags:
  - docker
  - nginx
  - brotli
  - github-actions
  - tdd
  - hardening
aliases:
  - docker-nginx-brotli 고도화
  - nginx-http3 이미지 고도화 계획
---

# docker-nginx-brotli 고도화 실행 계획

> **For Hermes:** 구현 시 `test-driven-development` 원칙을 우선 적용합니다. 코드/워크플로 변경은 RED → GREEN → REFACTOR 순서로 진행하고, 각 Phase는 별도 PR로 작게 나눕니다. Docker 이미지 빌드는 시간이 길 수 있으므로 로컬에서는 빠른 단위 테스트/정적 검증을 먼저 통과시킨 뒤 GitHub Actions smoke/publish 검증으로 마무리합니다.

## 한눈 요약

**목표:** `woosungchoi/docker-nginx-brotli`를 “이미 잘 관리되는 이미지 저장소”에서 “테스트 가능한 자동화, 공급망 보안, 릴리스 추적성, 운영 관측성까지 갖춘 이미지 저장소”로 한 단계 고도화합니다.

**현재 기준:**

- 기본 브랜치: `master`
- 기준 커밋: `6f8b89b612b9f20a4c56c8ad7a4f6b15c4b6848f`
- 공개 이미지: `ghcr.io/woosungchoi/nginx-http3:latest`
- 현재 `latest` manifest 플랫폼: `linux/amd64`, `linux/arm64`, `linux/arm/v6`, `linux/arm/v7`
- 최근 `build and publish image`, `smoke-test`, `auto-merge dependency PR` 실행은 성공 이력이 있음
- `SECURITY.md`, 브랜치 보호, secret scanning, push protection, Dependabot security updates는 이미 적용됨

**가장 먼저 처리할 병목:** `scripts/update_versions.py --dry-run`이 현재 Dockerfile의 `ENV NGINX_VERSION=...` 형식을 인식하지 못합니다. 이 때문에 dependency update 자동화가 실제로는 실패할 수 있으므로 Phase 1에서 테스트부터 작성해 바로 잡습니다.

---

## Baseline 재확인 기록

다음 명령으로 현재 상태를 확인했습니다.

```bash
cd /home/openclaw/docker-nginx-brotli
git rev-parse HEAD
python3 scripts/update_versions.py --dry-run
gh run list -R woosungchoi/docker-nginx-brotli --limit 12
gh api /repos/woosungchoi/docker-nginx-brotli/branches/master/protection
docker buildx imagetools inspect ghcr.io/woosungchoi/nginx-http3:latest
```

관찰 결과:

- `python3 scripts/update_versions.py --dry-run` 결과:
  - `ERROR: could not find NGINX_VERSION in Dockerfile`
  - 원인 후보: updater 정규식은 `ENV NGINX_VERSION 1.30.2` 형태만 찾고, Dockerfile은 `ENV NGINX_VERSION=1.30.2` 형태입니다.
- 브랜치 보호:
  - required status check: `docker-smoke`
  - strict: `true`
  - force push/deletion 차단됨
- 보안 설정:
  - Dependabot security updates: enabled
  - secret scanning: enabled
  - secret scanning push protection: enabled
- GHCR manifest:
  - runtime 플랫폼 4종 포함
  - SBOM/provenance attestation 때문에 `unknown/unknown` manifest도 함께 보임. 이는 정상이며 플랫폼 검증에서는 제외해야 합니다.

---

## 전체 Phase 체크리스트

- [ ] Phase 0 — 작업 기준선 고정 및 문서/테스트 구조 준비
- [ ] Phase 1 — dependency updater 회귀 버그 수정 및 단위 테스트 도입
- [ ] Phase 2 — publish manifest 검증 로직을 재사용 가능한 스크립트로 분리하고 테스트
- [ ] Phase 3 — smoke-test를 “nginx 실행 확인”에서 “기능/모듈/설정 확인”으로 확장
- [ ] Phase 4 — dependency update/auto-merge 워크플로 중복 제거 및 정책 테스트 가능화
- [ ] Phase 5 — 공급망 보안/취약점 스캔/액션 pinning 강화
- [ ] Phase 6 — 릴리스 추적성, 운영 리포트, README 정합성 강화
- [ ] Phase 7 — 최종 Definition of Done 검증 및 운영 Runbook 정리

---

## Phase 0 — 작업 기준선 고정 및 문서/테스트 구조 준비

### 목적

고도화 작업이 한 번에 커지지 않도록 테스트 디렉터리, 검증 명령, PR 분할 기준을 먼저 정합니다.

### Files

- Create: `tests/test_update_versions.py`
- Create: `tests/test_manifest_verifier.py`
- Create: `tests/test_dependency_pr_policy.py`
- Create: `scripts/verify_manifest.py`
- Create: `scripts/dependency_pr_policy.py`
- Modify: `README.md`
- Modify: `.github/workflows/*.yml`

### Checklist

- [ ] `tests/` 디렉터리를 만든다.
- [ ] Python 단위 테스트 실행 명령을 README 또는 개발 문서에 기록한다.
- [ ] Docker 빌드가 필요한 검증과 필요 없는 검증을 분리한다.
- [ ] 각 Phase를 독립 PR로 처리할 수 있게 파일 범위를 제한한다.
- [ ] GitHub Actions 변경 전에는 YAML parser와 actionlint 검증 계획을 포함한다.

### 권장 명령

```bash
python3 -m compileall scripts
python3 -m pytest tests -q
python3 - <<'PY'
import yaml
from pathlib import Path
for path in Path('.github/workflows').glob('*.yml'):
    yaml.safe_load(path.read_text())
    print(f'YAML OK: {path}')
PY
git diff --check
```

예상 결과:

- Python 컴파일 성공
- `pytest` 전체 통과
- 모든 workflow YAML parse 성공
- trailing whitespace 없음

---

## Phase 1 — dependency updater 회귀 버그 수정 및 단위 테스트 도입

### 목적

현재 실패하는 `scripts/update_versions.py --dry-run`을 복구하고, Dockerfile의 `ENV KEY=value` 및 `ENV KEY value` 양쪽 형식을 모두 안전하게 지원합니다.

### 현재 문제

`Dockerfile`은 다음 형식입니다.

```Dockerfile
ENV NGINX_VERSION=1.30.2
ENV PCRE_VERSION=10.47
ENV ZLIB_VERSION=1.3.2
```

하지만 updater의 정규식은 공백 구분 형식만 찾습니다.

```python
re.compile(r"^(ENV\s+NGINX_VERSION\s+)(\S+)(\s*)$", re.MULTILINE)
```

### Files

- Modify: `scripts/update_versions.py`
- Create: `tests/test_update_versions.py`

### TDD Checklist

#### Task 1.1 — 현재 Dockerfile 형식 파싱 테스트 추가

RED:

- [ ] `tests/test_update_versions.py`에 `ENV NGINX_VERSION=1.30.2` 형식 fixture를 만든다.
- [ ] `extract_current_versions()`가 세 버전을 모두 반환해야 한다는 테스트를 작성한다.
- [ ] 테스트를 실행해 현재 실패를 확인한다.

예상 실패:

```text
UpdateError: could not find NGINX_VERSION in Dockerfile
```

GREEN:

- [ ] `VERSION_PATTERNS`를 `ENV KEY=value`와 `ENV KEY value` 모두 지원하도록 최소 수정한다.
- [ ] 새 테스트가 통과하는지 확인한다.

REFACTOR:

- [ ] 정규식이 너무 복잡해지면 `parse_env_assignment(text, key)` helper로 분리한다.
- [ ] error message가 어떤 key를 못 찾았는지 유지되는지 확인한다.

권장 테스트 예시:

```python
from scripts.update_versions import extract_current_versions


def test_extract_current_versions_accepts_equals_env_assignments():
    text = """
ENV NGINX_VERSION=1.30.2
ENV PCRE_VERSION=10.47
ENV ZLIB_VERSION=1.3.2
""".strip()

    assert extract_current_versions(text) == {
        "NGINX_VERSION": "1.30.2",
        "PCRE_VERSION": "10.47",
        "ZLIB_VERSION": "1.3.2",
    }
```

#### Task 1.2 — 기존 공백 구분 형식 호환성 테스트

RED:

- [ ] `ENV NGINX_VERSION 1.30.2` 형식 테스트를 추가한다.
- [ ] 현재 코드가 이미 통과하면 “회귀 보호 테스트”로 유지한다.

GREEN:

- [ ] Phase 1.1 수정 후에도 공백 구분 형식이 깨지지 않게 한다.

REFACTOR:

- [ ] `replace_versions()`가 원래 separator(`=` 또는 space)를 보존하는지 검토한다.

#### Task 1.3 — replace_versions 형식 보존 테스트

RED:

- [ ] `ENV NGINX_VERSION=1.30.2`를 `1.30.3`으로 바꿔도 `ENV NGINX_VERSION=1.30.3` 형식이 유지되는지 테스트한다.
- [ ] 공백 구분 입력은 공백 구분으로 유지되는지 테스트한다.

GREEN:

- [ ] `replace_versions()`에서 capture group을 `prefix`, `separator`, `value`, `suffix`로 나눠 보존한다.

검증 명령:

```bash
python3 -m pytest tests/test_update_versions.py -q
python3 scripts/update_versions.py --dry-run
python3 scripts/update_versions.py --check
```

예상 결과:

- 테스트 통과
- `--dry-run`은 현재/최신 버전 summary를 출력
- `--check`는 업데이트가 없으면 `0`, 있으면 `1`을 반환. 업데이트 가능 상태 자체는 실패가 아니라 신호로 해석

### 완료 기준

- [ ] updater가 현재 Dockerfile을 정상 파싱한다.
- [ ] `ENV KEY=value`/`ENV KEY value` 양쪽 형식에 대한 테스트가 있다.
- [ ] 네트워크 호출 없이 순수 parser/replace 단위 테스트가 가능하다.
- [ ] updater workflow가 다시 신뢰 가능한 상태가 된다.

---

## Phase 2 — publish manifest 검증 로직을 재사용 가능한 스크립트로 분리하고 테스트

### 목적

현재 `.github/workflows/image.yml` 내부에 inline Python으로 들어 있는 manifest platform 검증 로직을 `scripts/verify_manifest.py`로 분리합니다. 이렇게 하면 PR에서 Docker build 없이도 플랫폼 검증 로직을 TDD로 검증할 수 있습니다.

### Files

- Create: `scripts/verify_manifest.py`
- Create: `tests/test_manifest_verifier.py`
- Modify: `.github/workflows/image.yml`
- Modify: `README.md` 또는 `docs/` 문서

### TDD Checklist

#### Task 2.1 — 정상 multi-arch manifest 테스트

RED:

- [ ] `linux/amd64`, `linux/arm64`, `linux/arm/v6`, `linux/arm/v7`가 포함된 OCI index fixture를 만든다.
- [ ] `verify_manifest(manifest, expected_platforms)`가 성공해야 한다는 테스트를 작성한다.
- [ ] 아직 스크립트가 없으므로 import failure 또는 missing function failure를 확인한다.

GREEN:

- [ ] `scripts/verify_manifest.py`에 `collect_runtime_platforms()`와 `verify_expected_platforms()`를 최소 구현한다.

REFACTOR:

- [ ] CLI entrypoint는 함수 테스트 이후에 얇게 추가한다.

#### Task 2.2 — attestation `unknown/unknown` 무시 테스트

RED:

- [ ] `unknown/unknown` attestation manifest가 포함된 fixture를 만든다.
- [ ] runtime platform 결과에는 `unknown/unknown`이 포함되지 않아야 한다.

GREEN:

- [ ] platform `os` 또는 `architecture`가 `unknown`이면 제외한다.

#### Task 2.3 — 누락 플랫폼 실패 메시지 테스트

RED:

- [ ] `linux/arm/v7`이 빠진 fixture를 만든다.
- [ ] CLI가 exit code `1`을 반환하고 stderr에 `missing expected platforms: linux/arm/v7`을 출력해야 한다.

GREEN:

- [ ] CLI wrapper에서 stdin JSON과 `EXPECTED_PLATFORMS` env를 읽어 검증한다.

검증 명령:

```bash
python3 -m pytest tests/test_manifest_verifier.py -q
EXPECTED_PLATFORMS="linux/amd64 linux/arm64 linux/arm/v6 linux/arm/v7" \
  docker buildx imagetools inspect --raw ghcr.io/woosungchoi/nginx-http3:latest \
  | python3 scripts/verify_manifest.py
```

### Workflow 변경 Checklist

- [ ] `.github/workflows/image.yml` inline Python 생성 블록을 제거한다.
- [ ] workflow에서 `python3 scripts/verify_manifest.py`를 호출한다.
- [ ] `EXPECTED_PLATFORMS`는 workflow env 한 곳에서만 정의한다.
- [ ] digest equality 검증과 platform 검증을 분리 유지한다.
- [ ] retry loop는 유지하되 실패 시 raw manifest stdout/stderr를 출력한다.

### 완료 기준

- [ ] manifest parser가 단위 테스트로 보호된다.
- [ ] workflow YAML이 짧아지고 유지보수가 쉬워진다.
- [ ] 현재 GHCR latest manifest를 대상으로 실제 검증이 통과한다.

---

## Phase 3 — smoke-test를 기능/모듈/설정 확인으로 확장

### 목적

현재 smoke-test는 `nginx -v`와 `nginx -t`만 확인합니다. 고도화 후에는 “이미지가 정말 필요한 모듈과 설정을 포함하는지”를 빠르게 검증해야 합니다.

### Files

- Modify: `.github/workflows/smoke-test.yml`
- Create: `tests/smoke/` 또는 `scripts/smoke_image.sh`
- Modify: `nginx.conf`
- Modify: `h3.nginx.conf` 필요 시

### TDD/검증 Checklist

Docker 이미지 자체는 Python 단위 테스트보다 GitHub Actions smoke가 핵심입니다. 다만 shell script로 분리하면 로컬에서도 재현 가능합니다.

#### Task 3.1 — smoke shell script 추가

RED:

- [ ] `scripts/smoke_image.sh nginx-http3:smoke`를 workflow에서 호출하도록 먼저 변경한다.
- [ ] 아직 파일이 없어 workflow/local 실행이 실패하는 것을 확인한다.

GREEN:

- [ ] `scripts/smoke_image.sh`를 만들고 기존 `nginx -v`, `nginx -t` 검증만 포함한다.

REFACTOR:

- [ ] image tag를 인자로 받고, 없으면 usage를 출력하게 한다.

#### Task 3.2 — 빌드 옵션/모듈 검증 추가

Checklist:

- [ ] `nginx -V` 출력에 다음 옵션이 포함되는지 확인한다.
  - [ ] `--with-http_v2_module`
  - [ ] `--with-http_v3_module`
  - [ ] `--with-pcre-jit`
  - [ ] `--add-dynamic-module=/usr/src/ngx_brotli` 또는 brotli module 흔적
  - [ ] `headers-more-nginx-module`
  - [ ] `nginx_cookie_flag_module`
- [ ] 동적 모듈 디렉터리 `/usr/lib/nginx/modules` 존재 확인
- [ ] `nginx-debug` binary 존재 확인
- [ ] 기본 인증서 파일 존재 확인

권장 명령:

```bash
docker run --rm nginx-http3:smoke nginx -V 2>&1 | tee /tmp/nginx-version.txt
grep -- '--with-http_v3_module' /tmp/nginx-version.txt
grep -- '--with-pcre-jit' /tmp/nginx-version.txt
docker run --rm nginx-http3:smoke test -x /usr/sbin/nginx-debug
docker run --rm nginx-http3:smoke test -d /usr/lib/nginx/modules
```

#### Task 3.3 — 실제 HTTP 응답 smoke

Checklist:

- [ ] 컨테이너를 백그라운드로 띄운다.
- [ ] `curl -I http://127.0.0.1:<port>/`로 `200` 또는 예상 status를 확인한다.
- [ ] access/error log가 Docker stdout/stderr로 연결되는지 확인한다.
- [ ] 테스트 종료 시 컨테이너 cleanup trap을 둔다.

권장 script 패턴:

```bash
container_id="$(docker run -d -p 8080:80 nginx-http3:smoke)"
trap 'docker rm -f "$container_id" >/dev/null 2>&1 || true' EXIT
for attempt in $(seq 1 20); do
  if curl -fsSI http://127.0.0.1:8080/ >/tmp/headers.txt; then
    break
  fi
  sleep 1
done
grep -E '^HTTP/.* 200' /tmp/headers.txt
```

### 완료 기준

- [ ] smoke-test가 단순 실행 가능성뿐 아니라 핵심 기능 플래그를 검증한다.
- [ ] 실패 시 어떤 모듈/기능이 빠졌는지 로그만 보고 알 수 있다.
- [ ] dependency PR auto-merge 전에 기능 회귀가 잡힌다.

---

## Phase 4 — dependency update/auto-merge 워크플로 중복 제거 및 정책 테스트 가능화

### 목적

현재 `update-versions.yml`과 `auto-merge-dependency-pr.yml`에 nginx stable branch 변경 여부 판단 로직이 중복되어 있습니다. 정책을 스크립트로 분리해 테스트 가능하게 만들고, 자동 merge 경로를 단순화합니다.

### Files

- Create: `scripts/dependency_pr_policy.py`
- Create: `tests/test_dependency_pr_policy.py`
- Modify: `.github/workflows/update-versions.yml`
- Modify: `.github/workflows/auto-merge-dependency-pr.yml`

### TDD Checklist

#### Task 4.1 — nginx branch 변경 판단 함수 테스트

RED:

- [ ] `is_nginx_stable_branch_change("1.30.2", "1.30.3") == False` 테스트
- [ ] `is_nginx_stable_branch_change("1.30.2", "1.32.0") == True` 테스트
- [ ] 잘못된 버전 문자열이면 manual review가 필요하다는 결과를 반환하는 테스트

GREEN:

- [ ] `scripts/dependency_pr_policy.py`에 순수 함수 구현

REFACTOR:

- [ ] shell에서 awk로 처리하던 정책을 Python CLI로 감싼다.

#### Task 4.2 — PR label/author gate 문서화 및 검증

Checklist:

- [ ] 자동 merge 대상은 다음 조건을 모두 만족해야 한다.
  - [ ] author: `github-actions[bot]`
  - [ ] label: `dependencies`
  - [ ] label: `automated pr`
  - [ ] base: `master`
  - [ ] head: `ci/update-pinned-versions`
- [ ] nginx stable branch 변경이면 comment만 남기고 merge하지 않는다.
- [ ] patch-level nginx/PCRE2/zlib 업데이트는 smoke 통과 후 merge 가능하다.

#### Task 4.3 — workflow 단순화

Checklist:

- [ ] `update-versions.yml`은 PR 생성과 smoke dispatch까지만 담당하게 줄일지 결정한다.
- [ ] 실제 auto-merge는 `auto-merge-dependency-pr.yml` 하나가 담당하게 한다.
- [ ] 중복된 `Check whether nginx stable branch changed` shell block을 제거한다.
- [ ] workflow_dispatch fallback은 유지한다.

검증 명령:

```bash
python3 -m pytest tests/test_dependency_pr_policy.py -q
python3 - <<'PY'
import yaml
from pathlib import Path
for path in Path('.github/workflows').glob('*.yml'):
    yaml.safe_load(path.read_text())
    print(f'YAML OK: {path}')
PY
```

### 완료 기준

- [ ] auto-merge 정책이 단위 테스트로 설명된다.
- [ ] workflow 중복이 줄어든다.
- [ ] 자동 merge와 manual review 경계가 README에 명확히 남는다.

---

## Phase 5 — 공급망 보안/취약점 스캔/액션 pinning 강화

### 목적

이미 SBOM/provenance/cosign signing은 적용되어 있습니다. 다음 단계는 빌드 입력과 workflow dependency 자체의 신뢰성을 강화하는 것입니다.

### Files

- Modify: `.github/workflows/smoke-test.yml`
- Modify: `.github/workflows/image.yml`
- Create: `.github/workflows/security-scan.yml` 또는 기존 workflow에 job 추가
- Modify: `README.md`

### Checklist

#### 5.1 GitHub Actions version 정합성

- [ ] `actions/checkout`, `docker/setup-buildx-action`, `docker/build-push-action`, `docker/login-action`, `sigstore/cosign-installer` 버전을 한 번에 점검한다.
- [ ] 동일 액션의 major 버전이 workflow마다 불필요하게 다른지 확인한다.
- [ ] Renovate/Dependabot for GitHub Actions 도입 여부를 결정한다.
- [ ] SHA pinning까지 갈지, major/minor pinning으로 둘지 정책을 문서화한다.

#### 5.2 이미지 취약점 스캔

권장 접근:

- PR smoke 단계에서는 빠른 scanner를 report-only로 시작합니다.
- publish 후에는 GHCR digest 기준으로 스캔 결과 artifact/summary를 남깁니다.
- 처음부터 fail gate로 걸면 Alpine/nginx ecosystem의 transient CVE 때문에 자동화가 자주 막힐 수 있으므로 baseline을 먼저 만듭니다.

후보:

- Trivy: `aquasecurity/trivy-action`
- Grype: Anchore scan action
- Docker Scout: Docker Hub/GHCR 연동 정책에 따라 선택

Checklist:

- [ ] `CRITICAL`/`HIGH` 기준을 report-only로 먼저 실행한다.
- [ ] false positive와 fixed version availability를 구분한다.
- [ ] fix 가능한 CRITICAL만 fail로 승격할지 결정한다.
- [ ] scan 결과를 GitHub Actions summary에 남긴다.

#### 5.3 cosign 검증 문서

현재 workflow는 sign을 수행합니다. 사용자가 검증할 수 있는 명령을 README에 추가하면 신뢰성이 올라갑니다.

문서 예시:

```bash
cosign verify \
  --certificate-identity-regexp 'https://github.com/woosungchoi/docker-nginx-brotli/.github/workflows/image.yml@refs/heads/master' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/woosungchoi/nginx-http3@sha256:<digest>
```

### TDD/검증 기준

Workflow 보안 스캔은 완전한 TDD보다는 “정적 검증 + dry-run 가능 범위 + 실제 CI 실행”이 현실적입니다.

- [ ] YAML parser 통과
- [ ] actionlint 통과
- [ ] PR에서 scanner job이 실행되고 결과 summary가 남음
- [ ] publish workflow는 기존 digest/platform 검증을 계속 통과

### 완료 기준

- [ ] 이미지 취약점 스캔 결과가 PR 또는 publish run에서 확인 가능하다.
- [ ] 액션 버전 관리 정책이 문서화된다.
- [ ] cosign verify 방법이 README에 추가된다.

---

## Phase 6 — 릴리스 추적성, 운영 리포트, README 정합성 강화

### 목적

사용자가 “지금 어떤 이미지가 어떤 커밋/설정으로 빌드됐는지”를 빠르게 확인할 수 있게 합니다.

### Files

- Modify: `.github/workflows/image.yml`
- Modify: `README.md`
- Create: `docs/release-runbook.md`
- Create: `docs/verification.md`

### Checklist

#### 6.1 Publish summary 강화

- [ ] workflow summary에 다음을 출력한다.
  - [ ] GHCR tags
  - [ ] Docker Hub mirror tags가 활성화됐는지 여부
  - [ ] digest
  - [ ] expected platforms
  - [ ] cosign signing 대상 repository digest
  - [ ] SBOM/provenance 활성화 여부
- [ ] summary가 실패 시에도 원인 파악에 도움이 되도록 verify step 전후로 나뉘어 있다.

#### 6.2 Release note 정책

- [ ] GitHub Release를 필수로 할지, image-first repo라서 optional로 유지할지 결정한다.
- [ ] optional 유지 시 README에 “short SHA tag + digest가 릴리스 추적 단위”라고 명시한다.
- [ ] 수동 release를 만들 때 포함할 template을 `docs/release-runbook.md`에 기록한다.

#### 6.3 README claim 최신화

현재 README에는 오래된 이미지/기능 증빙 이미지가 일부 남아 있습니다. 다음을 점검합니다.

- [ ] HTTP/2 Server Push는 최신 nginx/browser ecosystem에서 의미가 달라졌으므로 claim 표현을 재검토한다.
- [ ] TLS 1.3/0-RTT screenshot이 오래된 경우 “historical proof”인지 “현재 검증”인지 구분한다.
- [ ] Docker Hub mirror placeholder `<dockerhub-user>`를 실제 운영값 또는 “optional mirror”로 더 명확히 표기한다.
- [ ] `latest` 외 short SHA tag 사용법을 추가한다.

### TDD/검증 기준

문서 중심 Phase지만, 가능하면 docs assertion을 둡니다.

- [ ] README에 `ghcr.io/woosungchoi/nginx-http3`가 포함된다.
- [ ] README에 `cosign verify` 또는 verification 문서 링크가 포함된다.
- [ ] workflow summary에 digest/platform 문구가 포함되는지 grep 기반 검증을 추가한다.

권장 명령:

```bash
grep -q 'ghcr.io/woosungchoi/nginx-http3' README.md
grep -q 'cosign verify' README.md docs/verification.md
grep -q 'GITHUB_STEP_SUMMARY' .github/workflows/image.yml
git diff --check
```

### 완료 기준

- [ ] 사용자가 digest, tag, platform, signature 검증 방법을 README에서 찾을 수 있다.
- [ ] publish run만 봐도 어떤 이미지가 배포됐는지 알 수 있다.
- [ ] 오래된 claim과 현재 보장 범위가 구분된다.

---

## Phase 7 — 최종 Definition of Done 검증 및 운영 Runbook 정리

### 목적

각 Phase가 끝난 후에도 전체 저장소가 “운영 가능한 상태”인지 최종 확인합니다.

### Final DoD Checklist

#### 로컬 검증

- [ ] `python3 -m pytest tests -q` 통과
- [ ] `python3 -m compileall scripts` 통과
- [ ] `python3 scripts/update_versions.py --dry-run` 정상 summary 출력
- [ ] workflow YAML parse 통과
- [ ] `git diff --check` 통과
- [ ] 가능하면 `actionlint` 통과

#### Docker 검증

- [ ] 로컬 또는 GitHub Actions에서 Docker image build 성공
- [ ] `scripts/smoke_image.sh nginx-http3:smoke` 통과
- [ ] `nginx -V` 핵심 compile flag 확인
- [ ] `nginx -t` 통과
- [ ] HTTP 응답 smoke 통과

#### GitHub Actions 검증

- [ ] PR `smoke-test / docker-smoke` 성공
- [ ] default branch merge 후 `build and publish image` 성공
- [ ] publish digest/platform verification 성공
- [ ] cosign signing 성공
- [ ] security scan 결과가 summary/artifact로 남음

#### 운영/문서 검증

- [ ] README usage와 실제 publish workflow tag 정책이 일치
- [ ] Docker Hub mirror가 optional임이 명확함
- [ ] dependency auto-merge/manual-review 정책이 명확함
- [ ] release/verification runbook이 존재
- [ ] rollback 또는 실패 시 대응 절차가 기록됨

---

## 추천 PR 분할

### PR 1 — updater 테스트/버그 수정

목표:

- `scripts/update_versions.py` parser 회귀 수정
- `tests/test_update_versions.py` 추가

검증:

```bash
python3 -m pytest tests/test_update_versions.py -q
python3 scripts/update_versions.py --dry-run
```

### PR 2 — manifest verifier 스크립트화

목표:

- `scripts/verify_manifest.py` 추가
- inline Python 제거
- manifest tests 추가

검증:

```bash
python3 -m pytest tests/test_manifest_verifier.py -q
EXPECTED_PLATFORMS="linux/amd64 linux/arm64 linux/arm/v6 linux/arm/v7" \
  docker buildx imagetools inspect --raw ghcr.io/woosungchoi/nginx-http3:latest \
  | python3 scripts/verify_manifest.py
```

### PR 3 — smoke-test 확장

목표:

- `scripts/smoke_image.sh` 추가
- module/config/http response smoke 강화

검증:

```bash
docker build -t nginx-http3:smoke .
scripts/smoke_image.sh nginx-http3:smoke
```

### PR 4 — dependency PR policy 정리

목표:

- policy script + tests 추가
- auto-merge workflow 중복 제거

검증:

```bash
python3 -m pytest tests/test_dependency_pr_policy.py -q
python3 - <<'PY'
import yaml
from pathlib import Path
for path in Path('.github/workflows').glob('*.yml'):
    yaml.safe_load(path.read_text())
PY
```

### PR 5 — security scan/release docs

목표:

- vulnerability scan report-only 도입
- cosign verify 문서화
- publish summary 강화

검증:

```bash
git diff --check
grep -q 'cosign verify' README.md docs/verification.md
grep -q 'GITHUB_STEP_SUMMARY' .github/workflows/image.yml
```

---

## Top 3 다음 행동

1. **PR 1부터 시작:** `update_versions.py`가 현재 Dockerfile을 못 읽는 문제를 TDD로 수정합니다.
2. **PR 2에서 inline Python 제거:** manifest 검증 로직을 스크립트화해 workflow 유지보수성을 높입니다.
3. **PR 3에서 smoke-test 강화:** dependency auto-merge 전에 nginx compile flag와 실제 HTTP 응답까지 확인합니다.

---

## 관련 링크

- Repository: https://github.com/woosungchoi/docker-nginx-brotli
- GHCR image: `ghcr.io/woosungchoi/nginx-http3:latest`
- Publish workflow: `.github/workflows/image.yml`
- Smoke workflow: `.github/workflows/smoke-test.yml`
- Dependency updater: `scripts/update_versions.py`
- Security policy: `SECURITY.md`
