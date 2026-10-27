#!/usr/bin/env python3
"""P1：evidence.py 请求台账（inventory/list/show/grep + 脏数据容错 + 落盘 + 只读红线）。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))

import evidence


@pytest.fixture
def world(tmp_path, monkeypatch):
    """铺一个含正常 + 全部脏形态 sidecar 的 target，返回 target 名。"""
    results = tmp_path / "results"
    monkeypatch.setattr(evidence, "RESULTS_DIR", str(results))
    target = "ev.example"
    rdir = results / evidence.safe_name(target) / "responses"
    rdir.mkdir(parents=True)

    def put(num, meta, body=""):
        with open(rdir / f"req_{num:03d}.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        with open(rdir / f"req_{num:03d}.txt", "w", encoding="utf-8") as f:
            f.write(body)

    put(1, {"target": "ev.example", "method": "GET", "url": "https://ev.example/api/user/list",
             "status": "200", "length": 100, "content_type": "application/json",
             "title": "", "duration": 0.1, "experiment_id": "exp-001"},
        '{"data": {"token": "abc", "items": [{"id": 7}]}}')
    put(2, {"method": "POST", "url": "https://ev.example/api/auth/login",
             "status": "401", "length": 42, "content_type": "text/html; charset=utf-8",
             "title": "Login", "duration": 0.2, "experiment_id": "exp-001"},
        "<html><title>Login</title>denied</html>")
    put(3, {"method": "GET", "url": "https://ev.example/api/debug/dump",
             "status": "500", "length": 999, "content_type": "text/plain",
             "title": "", "duration": 1.3, "experiment_id": None},
        "stacktrace public/certificates secret")
    # 脏形态 1：000+3 位码拼接遗物 + meta.target 脏值
    put(4, {"target": "--experiment-id", "method": "GET", "url": "https://ev.example/old",
             "status": "000405", "length": 0, "content_type": "", "title": "",
             "duration": 8.0, "experiment_id": None}, "")
    # 脏形态 2：真 000（超时）
    put(5, {"method": "GET", "url": "https://ev.example/dead",
             "status": "000", "length": 0, "content_type": "", "title": "",
             "duration": 5.0, "experiment_id": None}, "")
    # 脏形态 3：{} 占位
    with open(rdir / "req_006.json", "w", encoding="utf-8") as f:
        f.write("{}")
    # 脏形态 4：损坏 JSON
    with open(rdir / "req_007.json", "w", encoding="utf-8") as f:
        f.write("{broken")
    # 编号空洞：没有 req_008
    put(9, {"method": "GET", "url": "https://ev.example/api/user/list?page=2",
             "status": "200", "length": 120, "content_type": "application/json",
             "title": "", "duration": 0.1, "experiment_id": None},
        '{"data": {"token": "def"}}')
    return target


# ── 脏数据显示容错 ──────────────────────────────────────────────────────────

def test_clean_status_forms():
    assert evidence.clean_status("200") == "200"
    assert evidence.clean_status("000") == "000"
    assert evidence.clean_status("000401") == "401"
    assert evidence.clean_status("000599") == "599"
    assert evidence.clean_status("") == "?"
    display, original = evidence.status_pair("000405")
    assert display == "405" and original == "000405"
    display, original = evidence.status_pair("200")
    assert display == "200" and original is None


def test_sidecar_files_never_modified(world):
    """任何读操作后 sidecar 不得被反写（脏数据只在读侧容错）。"""
    target = world
    rdir = evidence.responses_dir(target)
    before = {n: open(os.path.join(rdir, n), "rb").read()
              for n in os.listdir(rdir)}
    evidence.render_inventory(target)
    evidence.render_list(target)
    evidence.render_grep(target, "api")
    evidence.render_show(target, "req_001", json_path="data.token")
    after = {n: open(os.path.join(rdir, n), "rb").read() for n in os.listdir(rdir)}
    assert before == after


# ── inventory ────────────────────────────────────────────────────────────────

def test_inventory_distributions_and_coverage(world):
    out = evidence.render_inventory(world)
    assert "200×2" in out and "401×1" in out and "500×1" in out
    assert "000×1" in out            # 真 000 保持
    assert "405" in out              # 000405 按 405 聚合
    assert "GET×5" in out and "POST×1" in out
    assert "min 0" in out and "中位" in out and "max 999" in out
    # /api/user/list 两次（req1+req9），其余各 1 次
    assert "仅请求 1 次" in out and "未差分候选" in out
    assert "机械台账，不是结论" in out
    assert "不可解析 sidecar: 2" in out  # {} + 损坏
    assert "编号空洞 1" in out           # 缺 req_008


def test_inventory_endpoint_aggregation_matches_shared_key(world):
    """端点键必须与 protocol_scan 同一份（同 URL 不同 query 归并）。"""
    out = evidence.render_inventory(world)
    # req1 与 req9 路径同为 /api/user/list（query 被前3段键忽略）→ ×2 同组
    assert "GET https://ev.example/api/user/list  ×2" in out


# ── list ─────────────────────────────────────────────────────────────────────

def test_list_filters(world):
    out = evidence.render_list(world, status_filter="200", limit=None)
    assert "req_001" in out and "req_009" in out and "req_002" not in out
    out2 = evidence.render_list(world, method="POST", limit=None)
    assert "req_002" in out2 and "req_001" not in out2
    out3 = evidence.render_list(world, exp="exp-001", limit=None)
    assert "req_001" in out3 and "req_002" in out3 and "req_003" not in out3
    out4 = evidence.render_list(world, url_pattern=r"debug", limit=None)
    assert "req_003" in out4
    out5 = evidence.render_list(world, since="req_005", limit=None)
    assert "req_005" in out5 and "req_009" in out5 and "req_001" not in out5


def test_list_default_limit_and_empty(world):
    out = evidence.render_list(world)
    assert "最近 50" not in out  # 样本 <50 不提示截断
    empty = evidence.render_list(world, status_filter="302", limit=None)
    assert "0 条命中" in empty


def test_list_since_bad_form(world):
    with pytest.raises(ValueError):
        evidence.render_list(world, since="9", limit=None)


def test_list_json(world):
    rows = evidence.render_list_json(world, exp=None, status_filter="401", method=None,
                                     url_pattern=None, since=None)
    assert len(rows) == 1 and rows[0]["req"] == "req_002" and rows[0]["status"] == "401"


# ── show ─────────────────────────────────────────────────────────────────────

def test_show_json_path_nested_and_index(world):
    out = evidence.render_show(world, "req_001", json_path="data.token")
    assert "abc" in out
    out2 = evidence.render_show(world, "req_001", json_path="data.items.0.id")
    assert "7" in out2


def test_show_json_path_errors(world):
    with pytest.raises(KeyError):
        evidence.render_show(world, "req_001", json_path="data.nope")
    with pytest.raises(KeyError):
        evidence.render_show(world, "req_001", json_path="data.items.x")
    with pytest.raises(ValueError):
        evidence.render_show(world, "req_002", json_path="data")  # body 非 JSON


def test_show_bytes_forms(world):
    text = evidence.render_show(world, "req_003", byte_range="0:10")
    assert "stacktrace" in text and "certificates" not in text
    tail = evidence.render_show(world, "req_003", byte_range="25:")
    assert "secret" in tail
    prefix = evidence.render_show(world, "req_003", byte_range=":10")
    assert "stacktrace" in prefix
    with pytest.raises(ValueError):
        evidence.render_show(world, "req_003", byte_range="10")
    # 越界自动收敛不报错
    evidence.render_show(world, "req_003", byte_range="0:99999")


def test_show_grep_with_line_numbers(world):
    out = evidence.render_show(world, "req_003", grep_pattern=r"public/\w+")
    assert "public/certificates" in out and "\n1: " in out


def test_show_missing_and_bad_id(world):
    with pytest.raises(FileNotFoundError):
        evidence.render_show(world, "req_050")
    with pytest.raises(ValueError):
        evidence.render_show(world, "xxx-1")


# ── grep ─────────────────────────────────────────────────────────────────────

def test_grep_body_and_url(world):
    out = evidence.render_grep(world, r"public/certificates")
    assert "req_003:1:" in out
    out_url = evidence.render_grep(world, r"debug", field="url")
    assert "req_003" in out_url and "http" in out_url
    out_status = evidence.render_grep(world, r".", status_filter="200", limit=100)
    assert "req_001" in out_status and "req_002" not in out_status
    empty = evidence.render_grep(world, r"ZZZ_NOT_PRESENT_ZZZ")
    assert "0 条命中" in empty


def test_grep_bad_regex(world):
    import re
    with pytest.raises(re.error):
        evidence.render_grep(world, r"[unterminated")


# ── 落盘 spill ───────────────────────────────────────────────────────────────

def test_spill_writes_file_with_head_tail(world, monkeypatch):
    monkeypatch.setattr(evidence, "EMIT_LIMIT", 200)
    raw = evidence.render_inventory(world)
    assert len(raw) > 200  # 前提：fixture 输出确实超限
    out = evidence._spill_if_large(world, "inventory", raw)
    assert "[SPILL]" in out and "tmp/queries" in out and "中段省略" in out
    qdir = os.path.join(evidence.RESULTS_DIR, evidence.safe_name(world), "tmp", "queries")
    spilled = [n for n in os.listdir(qdir) if n.endswith("_inventory.txt")]
    assert spilled
    # 落盘文件是全量
    full = open(os.path.join(qdir, spilled[0]), encoding="utf-8").read()
    assert len(full) > 200 and "端点" in full


# ── 只读红线 ─────────────────────────────────────────────────────────────────

def test_no_subprocess_import():
    src = open(evidence.__file__, encoding="utf-8").read()
    assert "subprocess" not in src
    assert "urllib.request" not in src


def test_missing_target_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "RESULTS_DIR", str(tmp_path / "results"))
    assert evidence.render_inventory("nope.world") == ""
    assert evidence.render_list("nope.world") == ""
    assert evidence.render_grep("nope.world", "x") == ""


# ── CLI 退出码 ───────────────────────────────────────────────────────────────

def test_cli_missing_target_returns_2(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence, "RESULTS_DIR", str(tmp_path / "results"))
    assert evidence.main(["inventory", "nope"]) == 2
    assert evidence.main(["grep", "nope", "x"]) == 2


def test_cli_grep_bad_regex_returns_2(world, monkeypatch):
    assert evidence.main(["grep", world, "[bad"]) == 2
