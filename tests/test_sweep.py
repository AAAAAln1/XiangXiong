#!/usr/bin/env python3
"""P2：--sweep 模板展开 / resume / 差分矩阵 / scope 整批预检 / 红线不记账。"""

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

import http_request


# ── 展开规则（纯函数，不起服务）──────────────────────────────────────────────

def test_cartesian_order_and_count():
    order, rows = http_request.expand_sweep_rows(
        "https://h/api/{a}/{b}",
        {"a": ["x", "y"], "b": ["1", "2"]}, [])
    assert order == ["a", "b"]
    assert len(rows) == 4
    urls = [args[-1] for _, args in rows]
    assert urls == [
        "https://h/api/x/1", "https://h/api/x/2",
        "https://h/api/y/1", "https://h/api/y/2"]


def test_inline_multi_w_merge_dedup():
    template, inline, wordlists, rerun, rest = http_request.extract_sweep(
        ["--sweep", "http://h/{a}", "-w", "a:x,y", "-w", "a:y,z"])
    assert not rest and rerun is False
    bindings = http_request.build_sweep_bindings(inline, wordlists)
    assert bindings["a"] == ["x", "y", "z"]
    _, rows = http_request.expand_sweep_rows(template, bindings, [])
    assert len(rows) == 3


def test_wordlist_file(tmp_path):
    wl = tmp_path / "tokens.txt"
    wl.write_text("# 注释\nadmin\n\nconfig\n", encoding="utf-8")
    bindings = http_request.build_sweep_bindings([], [f"a:{wl}"])
    assert bindings["a"] == ["admin", "config"]


def test_url_and_data_interpolation():
    _, rows = http_request.expand_sweep_rows(
        "https://h/api/{a}", {"a": ["x"]},
        ["-X", "POST", "-H", "Content-Type: application/json",
         "-d", '{"p":"{a}"}'])
    args = rows[0][1]
    assert args[-1] == "https://h/api/x"
    assert '{"p":"x"}' in args


def test_header_braces_rejected():
    with pytest.raises(ValueError, match="-H"):
        http_request.expand_sweep_rows(
            "http://h/{a}", {"a": ["x"]}, ["-H", "X-T: {a}"])


def test_unbound_and_extra_variables_rejected():
    with pytest.raises(ValueError, match="未绑定"):
        http_request.expand_sweep_rows("http://h/{a}/{b}", {"a": ["x"]}, [])
    with pytest.raises(ValueError, match="不存在的变量"):
        http_request.expand_sweep_rows("http://h/{a}", {"a": ["x"], "b": ["y"]}, [])


def test_shared_args_must_not_contain_url():
    with pytest.raises(ValueError, match="模板即 URL"):
        http_request.expand_sweep_rows(
            "http://h/{a}", {"a": ["x"]}, ["http://h/extra"])


def test_expansion_cap():
    huge = {f"v{i}": ["a", "b"] for i in range(13)}  # 2^13 = 8192
    template = "http://h/" + "/".join(f"{{v{i}}}" for i in range(13))
    with pytest.raises(ValueError, match="5000"):
        http_request.expand_sweep_rows(template, huge, [])


# ── 指纹稳定性 ───────────────────────────────────────────────────────────────

def test_fingerprint_order_insensitive_value_sensitive():
    t = "http://h/{a}"
    b = {"a": ["x"]}
    fp1 = http_request._sweep_fingerprint(t, b, ["-X", "POST", "-H", "X: 1"])
    fp2 = http_request._sweep_fingerprint(t, b, ["-H", "X: 1", "-X", "POST"])
    assert fp1 == fp2
    fp3 = http_request._sweep_fingerprint(t, {"a": ["y"]}, ["-X", "POST"])
    assert fp3 != fp1


# ── 真实服务端到端 ───────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/ok"):
                body, code = b"BASELINE", 200
            elif self.path.startswith("/api/boom"):
                body, code = b"X" * 5000, 500
            elif self.path.startswith("/api/deny"):
                body, code = b"NO", 403
            else:
                body, code = b"nope", 404
            self.send_response(code)
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


@pytest.fixture(autouse=True)
def isolate_turn_state(tmp_path, monkeypatch):
    """集成测试走完整 run_curl → turn_state.record，隔离到 tmp，不碰项目根状态。"""
    import turn_state
    monkeypatch.setattr(turn_state, "TURN_STATE_PATH", str(tmp_path / ".turn_state.json"))


@pytest.fixture
def target_ws(monkeypatch):
    base = tempfile.mkdtemp(prefix="xx_sweep_")
    monkeypatch.setattr(http_request, "RESULTS_DIR", base)
    yield "sweep-target.test", base
    shutil.rmtree(base, True)


def test_sweep_executes_writes_evidence_and_matrix(server, target_ws):
    target, base = target_ws
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = http_request.run_sweep(
            target, f"http://127.0.0.1:{server}/api/{{a}}",
            {"a": ["ok1", "ok2", "boom", "deny", "missing"]}, [], 0)
    out = buf.getvalue()
    assert rc == 0, out
    resp_dir = os.path.join(base, target, "responses")
    assert len([f for f in os.listdir(resp_dir) if f.endswith(".txt")]) == 5
    assert "[sweep done] 5 发" in out
    # 矩阵：500 与 403 都在基线之外被点名，基线（200×2）沉底
    assert "500" in out and "403" in out and "基线簇" in out
    assert "机械差分，不是漏洞" in out
    # TSV 与状态文件
    sdir = os.path.join(base, target, "sweeps")
    tsv = [f for f in os.listdir(sdir) if f.endswith(".tsv")]
    assert len(tsv) == 1
    state = json.load(open(os.path.join(sdir, [f for f in os.listdir(sdir) if f.endswith(".json")][0]),
                          encoding="utf-8"))
    assert state["status"] == "done" and len(state["done"]) == 5


def test_sweep_resume_and_rerun(server, target_ws):
    target, base = target_ws
    template = f"http://127.0.0.1:{server}/api/{{a}}"
    bindings = {"a": ["ok1", "ok2", "boom"]}
    with contextlib.redirect_stdout(io.StringIO()):
        http_request.run_sweep(target, template, bindings, [], 0)
    before = len([f for f in os.listdir(os.path.join(base, target, "responses")) if f.endswith(".txt")])

    # 已完成：默认不重发，直接回汇总，零新证据
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = http_request.run_sweep(target, template, bindings, [], 0)
    after = len([f for f in os.listdir(os.path.join(base, target, "responses")) if f.endswith(".txt")])
    assert rc == 0 and after == before
    assert "本次执行 0" in buf.getvalue()

    # --rerun：旧状态备份，全量重发（多出 3 个 req 文件）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = http_request.run_sweep(target, template, bindings, [], 0, rerun=True)
    final = len([f for f in os.listdir(os.path.join(base, target, "responses")) if f.endswith(".txt")])
    assert rc == 0 and final == before + 3
    sdir = os.path.join(base, target, "sweeps")
    assert any(".done-" in f for f in os.listdir(sdir))


def test_sweep_resume_partial(server, target_ws, monkeypatch):
    """中断（state 里有部分 done）→ 续跑只发差额。"""
    target, base = target_ws
    template = f"http://127.0.0.1:{server}/api/{{a}}"
    bindings = {"a": ["ok1", "ok2", "boom", "deny"]}
    # 完整跑一遍，拿到 fp 与一条 done 记录，然后伪造「只完成 2 发」的状态
    with contextlib.redirect_stdout(io.StringIO()):
        http_request.run_sweep(target, template, dict(bindings), [], 0)
    sdir = os.path.join(base, target, "sweeps")
    state_file = next(f for f in os.listdir(sdir) if f.endswith(".json"))
    path = os.path.join(sdir, state_file)
    state = json.load(open(path, encoding="utf-8"))
    keys = list(state["done"])
    state["done"] = {k: state["done"][k] for k in keys[:2]}
    state["status"] = "running"
    json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    before = len([f for f in os.listdir(os.path.join(base, target, "responses")) if f.endswith(".txt")])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = http_request.run_sweep(target, template, dict(bindings), [], 0)
    out = buf.getvalue()
    assert rc == 0 and "续跑 2/4" in out
    after = len([f for f in os.listdir(os.path.join(base, target, "responses")) if f.endswith(".txt")])
    assert after == before + 2


def test_sweep_scope_reject_zero_artifacts(server, target_ws, monkeypatch):
    target, base = target_ws

    def deny(target_, url):
        raise http_request.ScopeRejected("[SCOPE GATE] REJECT: out")

    monkeypatch.setattr(http_request, "enforce_scope_url", deny)
    with contextlib.redirect_stdout(io.StringIO()):
        rc = http_request.run_sweep(
            target, f"http://127.0.0.1:{server}/api/{{a}}",
            {"a": ["ok1", "ok2"]}, [], 0)
    assert rc == 2
    rdir = os.path.join(base, target, "responses")
    assert not os.path.isdir(rdir) or not [f for f in os.listdir(rdir) if f.endswith(".txt")]


def test_sweep_with_cred_masks_every_sidecar(server, target_ws, monkeypatch):
    target, base = target_ws
    cred_dir = os.path.join(base, target, "credentials")
    os.makedirs(cred_dir, exist_ok=True)
    index = {"c-001": {"kind": "token", "note": "测试token", "created": "t",
                       "file": "c-001.txt", "sha256": "x", "size": 9}}
    json.dump(index, open(os.path.join(cred_dir, "index.json"), "w", encoding="utf-8"))
    open(os.path.join(cred_dir, "c-001.txt"), "w", encoding="utf-8").write("secrettoken123")
    import credentials
    monkeypatch.setattr(credentials, "RESULTS_DIR", base)

    pairs, notes = http_request.resolve_cred_headers(target, ["c-001"])
    assert pairs[0][1] == "Authorization: Bearer secrettoken123"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = http_request.run_sweep(
            target, f"http://127.0.0.1:{server}/api/{{a}}",
            {"a": ["ok1", "ok2"]}, [], 0, extra_headers=pairs, cred_note=notes[0])
    out = buf.getvalue()
    assert rc == 0
    assert out.count("cred: c-001") == 1  # 整轮只提示一次
    assert "secrettoken123" not in out
    resp_dir = os.path.join(base, target, "responses")
    for name in os.listdir(resp_dir):
        if name.endswith(".json"):
            meta = json.load(open(os.path.join(resp_dir, name), encoding="utf-8"))
            blob = json.dumps(meta, ensure_ascii=False)
            assert "secrettoken123" not in blob


# ── 矩阵聚类边界 ─────────────────────────────────────────────────────────────

def _rec(status, length, url="http://h/x", title=""):
    return {"status": status, "length": length, "url": url, "title": title,
            "filename": "req_001.txt", "method": "GET"}


def test_matrix_length_cluster_boundaries():
    # 差 16 字节 → 同簇（单组即基线簇）
    records = [_rec("200", 100, f"http://h/{i}") for i in range(3)] + [_rec("200", 116)]
    out = http_request.render_sweep_matrix(records)
    assert "基线簇 200 len=100-116 (4, 100%)" in out
    # 差 17 字节 → 分裂（等计数时其中一个被标为基线簇，另一个前置点名）
    records2 = [_rec("200", 100), _rec("200", 117)]
    out2 = http_request.render_sweep_matrix(records2)
    assert "len=100" in out2 and "len=117 (1)" in out2
    assert "基线簇 200 len=100 (1, 50%)" in out2


def test_matrix_ordering_5xx_before_auth_before_baseline():
    records = [_rec("200", 10)] * 10 + [_rec("403", 10), _rec("500", 10), _rec("405", 10)]
    out = http_request.render_sweep_matrix(records)
    p500, p403, p405, pbase = (out.index("500"), out.index("403"),
                               out.index("405"), out.index("基线簇"))
    assert p500 < p403 < pbase and p405 < pbase


def test_matrix_empty():
    assert "无成功响应" in http_request.render_sweep_matrix([])


# ── --batch - 从 stdin 读 ────────────────────────────────────────────────────

def test_batch_dash_reads_stdin(monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("# 注释\nGET http://a/1\n\nGET http://a/2\n"))
    lines = http_request._lines_from_source("-")
    assert lines == ["GET http://a/1", "GET http://a/2"]
