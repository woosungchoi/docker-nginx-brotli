#!/usr/bin/env python3
"""Exercise the final image and the combined recommended configs on loopback ports.

Requires docker, curl with HTTP2+Brotli, openssl, and tests/requirements.txt.
All keys are disposable local fixtures. No test assets are copied into an image.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import json
import socket
import ssl
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from aioquic.asyncio import QuicConnectionProtocol, connect
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated, ProtocolNegotiated

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr}{result.stdout}")
    return result


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run("docker", *args, check=check)


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_response(status: str, protocol: str, body: bytes, expected: bytes, wanted: str) -> None:
    require(status == "200", f"expected 200, received {status}")
    require(protocol == wanted, f"expected HTTP/{wanted}, received {protocol}")
    require(hashlib.sha256(body).digest() == hashlib.sha256(expected).digest(), "response body mismatch")


class H3Client(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http = H3Connection(self._quic)
        self.headers = []
        self.body = bytearray()
        self.done = asyncio.get_running_loop().create_future()
        self.alpn = None

    def quic_event_received(self, event):
        if isinstance(event, ProtocolNegotiated):
            self.alpn = event.alpn_protocol
        if isinstance(event, ConnectionTerminated) and not self.done.done():
            self.done.set_exception(RuntimeError(f"QUIC terminated: {event.error_code}"))
        for h3_event in self.http.handle_event(event):
            if isinstance(h3_event, HeadersReceived):
                self.headers.extend(h3_event.headers)
            elif isinstance(h3_event, DataReceived):
                self.body.extend(h3_event.data)
            if getattr(h3_event, "stream_ended", False) and not self.done.done():
                self.done.set_result(None)


async def h3_get(port: int, certificate: Path, expected: bytes) -> None:
    config = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, server_name="localhost")
    config.load_verify_locations(str(certificate))
    async with connect("127.0.0.1", port, configuration=config, create_protocol=H3Client) as client:
        stream = client._quic.get_next_available_stream_id()
        client.http.send_headers(stream, [(b":method", b"GET"), (b":scheme", b"https"),
                                         (b":authority", b"localhost"), (b":path", b"/fixture.txt")], end_stream=True)
        client.transmit()
        await asyncio.wait_for(client.done, 20)
        require(client.alpn == "h3", "HTTP/3 ALPN was not h3")
        headers = dict(client.headers)
        assert_response(headers[b":status"].decode(), "3", bytes(client.body), expected, "3")


def available_port() -> int:
    with socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        tcp.bind(("127.0.0.1", 0))
        port = tcp.getsockname()[1]
        udp.bind(("127.0.0.1", port))
        return port


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--platform", default="linux/amd64")
    args = parser.parse_args()
    name = "nginx-smoke-" + uuid.uuid4().hex[:16]
    base = ["--platform", args.platform]
    with tempfile.TemporaryDirectory(prefix="nginx-smoke-") as tmp:
        fixture = Path(tmp)
        fixture.chmod(0o755)
        html = fixture / "html"
        html.mkdir()
        content = b"nginx HTTP2 HTTP3 dynamic Brotli fixture\n" * 2048
        (html / "fixture.txt").write_bytes(content)
        slow = b"graceful response fixture\n" * 65536
        (html / "slow.txt").write_bytes(slow)
        cert = fixture / "localhost.pem"
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(fixture / "localhost.key"),
            "-out", str(cert), "-days", "1", "-sha256", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost")
        recommended = fixture / "nginx.conf"
        recommended.write_text((ROOT / "nginx.conf").read_text().replace("worker_processes auto;", "worker_processes 2;"))
        h3 = fixture / "h3.nginx.conf"
        h3_text = (ROOT / "h3.nginx.conf").read_text().replace("  location / {", "  location = /slow.txt {\n    root /usr/share/nginx/html;\n    limit_rate 128k;\n  }\n\n  location / {")
        h3.write_text(h3_text)
        mounts = ["-v", f"{recommended}:/etc/nginx/nginx.conf:ro", "-v", f"{h3}:/etc/nginx/conf.d/h3.nginx.conf:ro",
                  "-v", f"{cert}:/etc/ssl/localhost.pem:ro", "-v", f"{fixture / 'localhost.key'}:/etc/ssl/private/localhost.key:ro",
                  "-v", f"{html}:/usr/share/nginx/html:ro"]
        require(json.loads(docker("image", "inspect", args.image).stdout)[0]["Config"]["StopSignal"] == "SIGQUIT", "incorrect stop signal")
        docker("run", "--rm", *base, args.image, "nginx", "-t")
        default_body = docker("run", "--rm", *base, args.image, "cat", "/usr/share/nginx/html/index.html").stdout.encode()
        default_port = available_port()
        docker("run", "-d", "--name", name + "-default", *base, "-p", f"127.0.0.1:{default_port}:80", args.image)
        try:
            for _ in range(30):
                result = run("curl", "-sS", "--max-time", "2", "-w", "\n%{http_code}", f"http://127.0.0.1:{default_port}/", check=False)
                if result.returncode == 0:
                    body, status = result.stdout.rsplit("\n", 1)
                    assert_response(status, "1.1", body.encode(), default_body, "1.1")
                    break
                time.sleep(1)
            else:
                raise AssertionError("default server never responded")
        finally:
            docker("rm", "-f", name + "-default", check=False)
        runtime_check = r'''set -eu
for binary in /usr/sbin/nginx /usr/sbin/nginx-debug /usr/local/bin/envsubst; do
  output=$(ldd "$binary" 2>&1)
  if printf '%s' "$output" | grep -E 'Error loading|Error relocating|not found'; then exit 1; fi
done
test -z "$(find /etc/ssl/private -type f)"
for module in /usr/lib/nginx/modules/*.so; do
  output=$(ldd "$module" 2>&1 || true)
  if printf '%s' "$output" | grep -E 'Error loading shared library'; then exit 1; fi
  case "$module" in *-debug.so) binary=nginx-debug ;; *) binary=nginx ;; esac
  printf 'load_module %s;\nevents {}\nhttp {}\n' "$module" > /tmp/module-test.conf
  "$binary" -t -c /tmp/module-test.conf
done
test "$(find /usr/lib/nginx/modules -name '*.so' | wc -l)" -eq 12
'''
        docker("run", "--rm", *base, args.image, "sh", "-c", runtime_check)
        combined = docker("run", "--rm", *base, *mounts, args.image, "nginx", "-T").stdout
        require("listen 443 quic reuseport;" in combined and "load_module /usr/lib/nginx/modules/ngx_http_brotli_filter_module.so;" in combined, "combined configs were not loaded")
        require("ssl_early_data off;" in combined and "ssl_protocols TLSv1.2 TLSv1.3;" in combined, "safe TLS defaults missing")
        # Both intentional faults must be rejected by the same config checker.
        h3.write_text(h3_text + "\ninvalid_smoke_directive;\n")
        require(docker("run", "--rm", *base, *mounts, args.image, "nginx", "-t", check=False).returncode != 0, "invalid h3 config passed")
        h3.write_text(h3_text)
        recommended.write_text("\n".join(line for line in recommended.read_text().splitlines() if not line.startswith("load_module")))
        require(docker("run", "--rm", *base, *mounts, args.image, "nginx", "-t", check=False).returncode != 0, "missing Brotli modules passed")
        recommended.write_text((ROOT / "nginx.conf").read_text().replace("worker_processes auto;", "worker_processes 2;"))
        port = available_port()
        http_port = available_port()
        docker("run", "-d", "--name", name, *base, *mounts, "-p", f"127.0.0.1:{port}:443/tcp",
               "-p", f"127.0.0.1:{port}:443/udp", "-p", f"127.0.0.1:{http_port}:80", args.image)
        try:
            url = f"https://localhost:{port}"
            for _ in range(30):
                result = run("curl", "-ksS", "--max-time", "2", "-o", "/dev/null", "-w", "%{http_code}", url + "/fixture.txt", check=False)
                if result.returncode == 0 and result.stdout == "200":
                    break
                time.sleep(1)
            else:
                raise AssertionError("server did not return fixture 200")
            redirect = run("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", f"http://127.0.0.1:{http_port}/fixture.txt")
            require(redirect.stdout == "301", "HTTP redirect mismatch")
            def curl_get(path: str, *options: str):
                output = fixture / "body"
                headers = fixture / "headers"
                result = run("curl", "--cacert", str(cert), "--max-time", "20", "-sS", *options, "-D", str(headers),
                             "-o", str(output), "-w", "%{http_code} %{http_version}", url + path)
                status, protocol = result.stdout.split()
                return status, protocol, output.read_bytes(), headers.read_text()
            status, protocol, body, _ = curl_get("/fixture.txt", "--http2")
            assert_response(status, protocol, body, content, "2")
            status, protocol, body, headers = curl_get("/fixture.txt", "--http2", "--compressed", "-H", "Accept-Encoding: br")
            require("content-encoding: br" in headers.lower(), "dynamic Brotli encoding missing")
            assert_response(status, protocol, body, content, "2")
            # Validate h2 ALPN explicitly, as well as the HTTP2 response above.
            ctx = ssl.create_default_context(cafile=str(cert))
            ctx.set_alpn_protocols(["h2"])
            with socket.create_connection(("127.0.0.1", port)) as raw, ctx.wrap_socket(raw, server_hostname="localhost") as tls:
                require(tls.selected_alpn_protocol() == "h2", "h2 ALPN missing")
            for flag in ("-tls1_2", "-tls1_3"):
                result = subprocess.run(["openssl", "s_client", "-connect", f"127.0.0.1:{port}", "-servername", "localhost",
                                         "-CAfile", str(cert), flag, "-brief"], input="", capture_output=True, text=True, timeout=10)
                require(result.returncode == 0 and "Protocol version: TLSv1." in result.stderr, f"{flag} failed")
            for flag in ("-tls1", "-tls1_1"):
                result = subprocess.run(["openssl", "s_client", "-connect", f"127.0.0.1:{port}", "-servername", "localhost", flag,
                                         "-cipher", "ALL:@SECLEVEL=0", "-brief"], input="", capture_output=True, text=True, timeout=10)
                require(result.returncode != 0 and "alert protocol version" in result.stderr, f"legacy {flag} was not rejected by server")
            asyncio.run(h3_get(port, cert, content))
            # A missing file cannot satisfy assert_response; verify the live 404 too.
            time.sleep(3)
            status, _, _, _ = curl_get("/missing.txt", "--http2")
            require(status == "404", "missing fixture did not return 404")
            time.sleep(3)
            statuses = [run("curl", "-ksS", "-o", "/dev/null", "-w", "%{http_code}", url + "/fixture.txt").stdout for _ in range(40)]
            require("429" in statuses and "200" in statuses, "service rate limit was not enforced")
            time.sleep(3)
            connection = http.client.HTTPSConnection("localhost", port, context=ssl.create_default_context(cafile=str(cert)), timeout=30)
            connection.request("GET", "/slow.txt")
            response = connection.getresponse()
            require(response.status == 200, "slow fixture status mismatch")
            prefix = response.read(1024)
            stopper = subprocess.Popen(["docker", "stop", "--time", "30", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(1)
            require(stopper.poll() is None, "container stopped before active request completed")
            # Listener must drain while the established response completes.
            new_request = run("curl", "-ksS", "--max-time", "2", "-o", "/dev/null", url + "/fixture.txt", check=False)
            require(new_request.returncode != 0, "server accepted a new request during drain")
            received = prefix + response.read()
            connection.close()
            require(received == slow, "graceful stop truncated active response")
            stopper.communicate(timeout=35)
            require(stopper.returncode == 0, "docker stop failed")
            state = json.loads(docker("inspect", name).stdout)[0]["State"]
            require(state["ExitCode"] == 0 and not state["OOMKilled"], "container did not exit gracefully")
            print(f"PASS {args.platform}: modules/default/combined, negative configs, 200/body, br, h2/h3, TLS, rate limit, graceful stop")
        except Exception:
            print(docker("logs", name, check=False).stdout)
            raise
        finally:
            docker("rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
