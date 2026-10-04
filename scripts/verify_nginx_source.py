#!/usr/bin/env python3
"""Verify official NGINX archives using SHA256 and the pinned upstream release key.

Public key source: https://nginx.org/en/pgp_keys.html -> keys/pluknet.key.
Only disposable GnuPG homes are used; no user keyring or trust settings are read.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_KEY = ROOT / 'keys/nginx-release.asc'
FINGERPRINT = 'D6786CE303D9A9022998DC6CC8464D549AF75C0A'


class VerificationError(RuntimeError):
    pass


def verify_pair(archive: Path, signature: Path, checksum: str) -> None:
    if hashlib.sha256(archive.read_bytes()).hexdigest() != checksum:
        raise VerificationError('NGINX archive checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='nginx-public-key-') as directory:
        command = ['gpg', '--homedir', directory, '--batch', '--no-tty']
        imported = subprocess.run([*command, '--import', str(RELEASE_KEY)], capture_output=True, text=True, timeout=30)
        if imported.returncode:
            raise VerificationError('could not import pinned public release key')
        verified = subprocess.run([*command, '--status-fd', '1', '--verify', str(signature), str(archive)],
                                  capture_output=True, text=True, timeout=30)
        records = [line.split() for line in verified.stdout.splitlines() if line.startswith('[GNUPG:] VALIDSIG ')]
        if verified.returncode or len(records) != 1 or records[0][-1] != FINGERPRINT:
            raise VerificationError('NGINX detached signature verification failed')


def verify_nginx_release(version: str, checksum: str, *, negative_test: bool = False) -> None:
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or not re.fullmatch(r'[a-f0-9]{64}', checksum):
        raise VerificationError('invalid NGINX version/checksum')
    with tempfile.TemporaryDirectory(prefix='nginx-source-') as directory:
        archive, signature = Path(directory) / 'nginx.tar.gz', Path(directory) / 'nginx.tar.gz.asc'
        for path, suffix in [(archive, '.tar.gz'), (signature, '.tar.gz.asc')]:
            request = urllib.request.Request(f'https://nginx.org/download/nginx-{version}{suffix}',
                                             headers={'User-Agent': 'docker-nginx-brotli-source-verifier/1.0'})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    path.write_bytes(response.read())
            except urllib.error.URLError as exc:
                raise VerificationError(f'official NGINX source download failed: {exc}') from exc
        verify_pair(archive, signature, checksum)
        if negative_test:
            # Match the modified payload checksum to prove the detached signature rejects it.
            archive.write_bytes(archive.read_bytes() + b'corrupt source fixture')
            modified_checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            try:
                verify_pair(archive, signature, modified_checksum)
            except VerificationError as exc:
                if str(exc) != 'NGINX detached signature verification failed':
                    raise
            else:
                raise VerificationError('modified archive unexpectedly passed signature verification')
    print(f'NGINX {version}: SHA256 and upstream detached signature verified')


def main() -> None:
    from scripts.update_versions import extract_pins
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-test', action='store_true')
    args = parser.parse_args()
    pins = extract_pins((ROOT / 'Dockerfile').read_text())
    verify_nginx_release(pins['NGINX_VERSION'], pins['NGINX_SHA256'], negative_test=args.negative_test)


if __name__ == '__main__':
    main()
