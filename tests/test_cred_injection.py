#!/usr/bin/env python3
"""P2：--cred c-NNN 注入（头映射/掩码/错误形态/未发包）。"""

import http.server
import io
import contextlib
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))

import credentials
import http_request


@pytest.fixture(autouse=True)
def isolate_turn_state(tmp_path, monkeypatch):
    """集成测试走完整 run_curl → turn_state.record，隔离到 tmp，不碰项目根状态。"""
    import turn_state
    monkeypatch.setattr(turn_state, "TURN_STATE_PATH", str(tmp_path / ".turn_state.json"))


@pytest.fixture
def cred_ws(tmp_path, monkeypatch):
    base = tmp_path / "results"
    monkeypatch.setattr(credentials, "RESULTS_DIR", str(base))
    monkeypatch.setattr(http_request, "RESULTS_DIR", str(base))
    target = "cred-target.test"
    return str(base), target


def _put(base, target, cid, kind, value, note="x"):
    cdir = os.path.join(base, target, "credentials")
    os.makedirs(cdir, exist_ok=True)
    open(os.path.join(cdir, f"{cid}.txt"), "w", encoding="utf-8").write(value)
    index_path = os.path.join(cdir, "index.json")
    index = {}
    if os.path.exists(index_path):
        index = json.load(open(index_path, encoding="utf-8"))
    index[cid] = {"kind": kind, "note": note, "created": "t",
                  "file": f"{cid}.txt", "sha256": "x", "size": len(value)}
    json.dump(index, open(index_path, "w", encoding="utf-8"), ensure_ascii=False)


def test_extract_cred_forms():
    specs, rest = http_request.extract_cred(
        ["--cred", "c-001", "--cred=c-002:Cookie", "http://x/"])
    assert specs == ["c-001", "c-002:Cookie"]
    assert rest == ["http://x/"]
    with pytest.raises(ValueError):
        http_request.extract_cred(["--cred"])


def test_token_default_bearer_prefix(cred_ws):
    base, target = cred_ws
    _put(base, target, "c-001", "token", "abcdef0123456789")
    pairs, notes = http_request.resolve_cred_headers(target, ["c-001"])
    assert pairs == [["-H", "Authorization: Bearer abcdef0123456789"]]
    assert "c-001" in notes[0] and "Authorization" in notes[0]


def test_token_existing_scheme_not_doubled(cred_ws):
    base, target = cred_ws
    _put(base, target, "c-002", "token", "Basic dXNlcjpwYXNz")
    pairs, _ = http_request.resolve_cred_headers(target, ["c-002"])
    assert pairs[0][1] == "Authorization: Basic dXNlcjpwYXNz"


def test_key_default_header(cred_ws):
    base, target = cred_ws
    _put(base, target, "c-003", "key", "AKIA123")
    pairs, _ = http_request.resolve_cred_headers(target, ["c-003"])
    assert pairs[0][1] == "X-API-Key: AKIA123"


def test_non_http_kinds_require_explicit_header(cred_ws):
    base, target = cred_ws
    _put(base, target, "c-004", "pw", "user:pass")
    with pytest.raises(http_request.CredError, match="无机械 HTTP 头形态"):
        http_request.resolve_cred_headers(target, ["c-004"])
    pairs, _ = http_request.resolve_cred_headers(target, ["c-004:Cookie"])
    assert pairs[0][1].startswith("Cookie: user:pass")


def test_bad_id_and_missing_cid(cred_ws):
    base, target = cred_ws
    with pytest.raises(http_request.CredError, match="c-NNN"):
        http_request.resolve_cred_headers(target, ["token-1"])
    with pytest.raises(http_request.CredError, match="c-999"):
        http_request.resolve_cred_headers(target, ["c-999"])


def test_multiline_value_rejected(cred_ws):
    base, target = cred_ws
    _put(base, target, "c-005", "token", "line1\nline2\n")
    with pytest.raises(http_request.CredError, match="非空行"):
        http_request.resolve_cred_headers(target, ["c-005"])


def test_empty_and_oversized_rejected(cred_ws):
    base, target = cred_ws
    _put(base, target, "c-006", "token", "   \n  \n")
    with pytest.raises(http_request.CredError, match="非空行"):
        http_request.resolve_cred_headers(target, ["c-006"])
    _put(base, target, "c-007", "token", "x" * 4097)
    with pytest.raises(http_request.CredError, match="4096"):
        http_request.resolve_cred_headers(target, ["c-007"])


@pytest.fixture(scope="module")
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


def test_single_request_redacts_sidecar(cred_ws, server):
    base, target = cred_ws
    _put(base, target, "c-010", "token", "secrettoken-xyz")
    pairs, notes = http_request.resolve_cred_headers(target, ["c-010"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = http_request._execute_request_args(
            target, [f"http://127.0.0.1:{server}/"], extra_headers=pairs,
            extra_notes=notes)
    out = buf.getvalue()
    assert result["status_code"] == "200"
    # sidecar args 被掩码
    resp_dir = os.path.join(base, target, "responses")
    metas = [f for f in os.listdir(resp_dir) if f.endswith(".json")]
    blob = json.dumps([json.load(open(os.path.join(resp_dir, f), encoding="utf-8")) for f in metas],
                      ensure_ascii=False)
    assert "secrettoken-xyz" not in blob
    assert "Authorization: [REDACTED]" in blob


def test_cred_error_happens_before_any_request(cred_ws, server):
    """取值失败：零 req 残骸（不产生 req 编号）。"""
    base, target = cred_ws
    rdir = os.path.join(base, target, "responses")
    with pytest.raises(http_request.CredError):
        http_request.resolve_cred_headers(target, ["c-404"])
    assert not os.path.isdir(rdir) or not os.listdir(rdir)
