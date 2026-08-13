# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the transport layer.

The value objects and the multipart encoder are covered directly.
``UrllibTransport`` gets exercised against a real loopback HTTP
server, because the whole point of the seam is that everything else
gets faked; the one real implementation must be proven against real
sockets (status passthrough, redirect following, error conversion).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from gratisgis_client.transport import (
    TransportError,
    TransportRequest,
    TransportResponse,
    UrllibTransport,
    encode_multipart,
)


class TestTransportResponse:
    def test_json_parses_body(self) -> None:
        response = TransportResponse(status=200, body=b'{"ok": true}')
        assert response.json() == {"ok": True}

    def test_json_raises_valueerror_on_garbage(self) -> None:
        response = TransportResponse(status=200, body=b"<html>nope</html>")
        with pytest.raises(ValueError):
            response.json()

    def test_json_raises_valueerror_on_empty_body(self) -> None:
        response = TransportResponse(status=200, body=b"")
        with pytest.raises(ValueError):
            response.json()

    def test_text_replaces_undecodable_bytes(self) -> None:
        response = TransportResponse(status=200, body=b"ok\xff")
        assert response.text == "ok�"

    def test_header_lookup_is_case_insensitive(self) -> None:
        response = TransportResponse(
            status=200, headers={"Content-Type": "application/json"}
        )
        assert response.header("content-type") == "application/json"
        assert response.header("CONTENT-TYPE") == "application/json"
        assert response.header("x-missing") is None


class TestEncodeMultipart:
    def test_content_type_carries_the_body_boundary(self) -> None:
        content_type, body = encode_multipart({}, {"file": ("a.bin", b"x", "application/octet-stream")})
        assert content_type.startswith("multipart/form-data; boundary=")
        boundary = content_type.split("boundary=", 1)[1]
        assert f"--{boundary}\r\n".encode() in body
        assert body.endswith(f"--{boundary}--\r\n".encode())

    def test_file_part_has_disposition_type_and_bytes(self) -> None:
        _, body = encode_multipart(
            {}, {"file": ("parcels.gpkg", b"GPKG-BYTES", "application/geopackage+sqlite3")}
        )
        assert b'Content-Disposition: form-data; name="file"; filename="parcels.gpkg"' in body
        assert b"Content-Type: application/geopackage+sqlite3" in body
        assert b"\r\n\r\nGPKG-BYTES\r\n" in body

    def test_plain_fields_are_encoded_without_content_type(self) -> None:
        _, body = encode_multipart({"mode": "replace"}, {})
        assert b'Content-Disposition: form-data; name="mode"\r\n\r\nreplace\r\n' in body

    def test_boundary_is_unique_per_call(self) -> None:
        ct1, _ = encode_multipart({}, {})
        ct2, _ = encode_multipart({}, {})
        assert ct1 != ct2

    def test_hostile_filename_cannot_inject_headers(self) -> None:
        _, body = encode_multipart(
            {}, {"file": ('evil".gpkg\r\nX-Injected: 1', b"x", "application/octet-stream")}
        )
        assert b"\r\nX-Injected" not in body
        assert b'filename="evil%22.gpkgX-Injected: 1"' in body


class _LoopbackHandler(BaseHTTPRequestHandler):
    """Tiny real server: /json 200, /missing 404 with a JSON body,
    /redirect 302 to /json, /echo-header reflects a request header."""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/json":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/missing":
            body = b'{"message": "not here"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/echo-header":
            value = self.headers.get("X-Probe", "")
            body = value.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_PUT(self) -> None:
        # /put-echo reports what the server actually received, so the
        # streaming-body test can pin both the payload integrity and
        # the framing (Content-Length vs chunked).
        if self.path != "/put-echo":
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        received = self.rfile.read(length)
        import hashlib
        import json

        body = json.dumps(
            {
                "receivedBytes": len(received),
                "sha256": hashlib.sha256(received).hexdigest(),
                "contentLength": self.headers.get("Content-Length"),
                "transferEncoding": self.headers.get("Transfer-Encoding"),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def loopback_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


class TestUrllibTransport:
    def test_200_json_round_trip(self, loopback_url: str) -> None:
        transport = UrllibTransport()
        response = transport.send(
            TransportRequest(method="GET", url=f"{loopback_url}/json", timeout=5.0)
        )
        assert response.status == 200
        assert response.json() == {"ok": True}
        assert response.header("content-type") == "application/json"
        assert response.url == f"{loopback_url}/json"

    def test_404_is_a_response_not_an_exception(self, loopback_url: str) -> None:
        # Status interpretation belongs to PortalHttp; the transport
        # must hand back error statuses with their bodies intact.
        transport = UrllibTransport()
        response = transport.send(
            TransportRequest(method="GET", url=f"{loopback_url}/missing", timeout=5.0)
        )
        assert response.status == 404
        assert response.json() == {"message": "not here"}

    def test_redirect_is_followed_and_final_url_reported(self, loopback_url: str) -> None:
        # Discovery's canonicalization depends on the final URL after
        # redirects, not the requested one.
        transport = UrllibTransport()
        response = transport.send(
            TransportRequest(method="GET", url=f"{loopback_url}/redirect", timeout=5.0)
        )
        assert response.status == 200
        assert response.json() == {"ok": True}
        assert response.url == f"{loopback_url}/json"

    def test_request_headers_are_sent(self, loopback_url: str) -> None:
        transport = UrllibTransport()
        response = transport.send(
            TransportRequest(
                method="GET",
                url=f"{loopback_url}/echo-header",
                headers={"X-Probe": "hello"},
                timeout=5.0,
            )
        )
        assert response.body == b"hello"

    def test_streaming_file_body_arrives_intact_with_content_length(
        self, loopback_url: str, tmp_path: Path
    ) -> None:
        # The raster publish streams multi-GB files through an open
        # reader instead of buffering bytes. The explicit
        # Content-Length must suppress urllib's chunked fallback,
        # because S3-style presigned PUT targets reject chunked.
        import hashlib

        payload = b"tile-bytes-" * 10_000
        path = tmp_path / "upload.bin"
        path.write_bytes(payload)

        transport = UrllibTransport()
        with open(path, "rb") as fh:
            response = transport.send(
                TransportRequest(
                    method="PUT",
                    url=f"{loopback_url}/put-echo",
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(payload)),
                    },
                    body=fh,
                    timeout=5.0,
                )
            )
        assert response.status == 200
        echoed = response.json()
        assert echoed["receivedBytes"] == len(payload)
        assert echoed["sha256"] == hashlib.sha256(payload).hexdigest()
        assert echoed["contentLength"] == str(len(payload))
        assert echoed["transferEncoding"] is None

    def test_connection_refused_raises_transport_error(self) -> None:
        # Bind a port and close it so nothing listens there; the port
        # stays refused for the immediate reuse window.
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        transport = UrllibTransport()
        with pytest.raises(TransportError):
            transport.send(
                TransportRequest(
                    method="GET", url=f"http://127.0.0.1:{dead_port}/", timeout=5.0
                )
            )
