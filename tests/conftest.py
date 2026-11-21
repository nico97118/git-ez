# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local TLS fixtures shared by the provider and SDK integration tests."""

import os
import ssl
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


def pytest_collection_modifyitems(items):
    """Keep both CI selections exhaustive and disjoint as tests are added."""
    for item in items:
        categories = [name for name in ("unit", "integration") if item.get_closest_marker(name)]
        if len(categories) != 1:
            raise pytest.UsageError(
                f"{item.nodeid} must have exactly one unit or integration marker"
            )


@pytest.fixture
def ca_bundle(tmp_path: Path) -> Path:
    """Create a self-signed certificate trusted only by this test."""
    bundle = tmp_path / "ca.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(tmp_path / "ca.key"),
            "-out",
            str(bundle),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return bundle


@pytest.fixture
def https_server(ca_bundle: Path) -> Iterator[Callable[[bytes], tuple[str, Path]]]:
    """Serve fixed JSON over TLS, then close sockets and join server threads."""
    servers: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    def start(body: bytes) -> tuple[str, Path]:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(ca_bundle), str(ca_bundle.with_suffix(".key")))
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        try:
            server.socket = context.wrap_socket(server.socket, server_side=True)
        except BaseException:
            server.server_close()
            raise
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"https://127.0.0.1:{server.server_address[1]}", ca_bundle

    try:
        yield start
    finally:
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join()


@pytest.fixture(scope="session")
def git_config_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("git-config") / "global.gitconfig"
    path.write_text("")
    return path


@pytest.fixture(autouse=True)
def isolated_git_settings(monkeypatch, git_config_file):
    """Tests choose their Git policies rather than inheriting the developer's hooks or keys."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config_file))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in list(os.environ):
        if name in ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT") or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_AUTHOR_", "GIT_COMMITTER_")
        ):
            monkeypatch.delenv(name)


@pytest.fixture
def model_server():
    """An OpenAI-compatible local server driving the real agent through edit and finish."""
    import json
    import shlex
    import sys
    from types import SimpleNamespace

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(payload)
            if len(calls) == 1:
                name = "execute"
                arguments = {
                    "command": shlex.join(
                        [
                            sys.executable,
                            "-B",
                            "-c",
                            "from pathlib import Path; Path('file.txt').write_text('combined\\n')",
                        ]
                    )
                }
            else:
                name = "finish"
                arguments = {"rationale": "Preserve both intentions", "confidence": 0.95}
            body = json.dumps(
                {
                    "id": "scripted-response",
                    "object": "chat.completion",
                    "created": 1,
                    "model": payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": f"call-{len(calls)}",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(arguments),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            endpoint=f"http://127.0.0.1:{server.server_address[1]}/v1", calls=calls
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
