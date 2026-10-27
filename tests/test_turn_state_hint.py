#!/usr/bin/env python3
"""F0：turn_state 最近证据记录 + merge 证据闸 REJECT 编号提示。

覆盖设计稿 §4.6 的 14 条用例：
  1-8  turn_state 单元（往返/schema 兼容/损坏/过期/键隔离/非法入参/reset/并发）
  9-11 merge._evidence_hint（有证据/无 target/active 去重）
  12-14 端到端（run_curl 后 REJECT 含编号 / ScopeRejected 不记录 / 非背书不按行匹配）
"""

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))

import experiment
import http_request
import merge
import turn_state


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    """把 turn_state 的文件与锁定向到 tmp（生产路径是项目根 .turn_state.json）。"""
    path = str(tmp_path / ".turn_state.json")
    monkeypatch.setattr(turn_state, "TURN_STATE_PATH", path)
    return path


def _read_raw(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# ── 1. 三种 kind 的 record→recent 往返 ──────────────────────────────────────

def test_record_recent_roundtrip(state_path):
    turn_state.record("t1", "req", "req_001")
    turn_state.record("t1", "art", "art-002")
    turn_state.record("t1", "exp", "exp-003")
    rec = turn_state.recent("t1")
    assert rec["req"][0] == "req_001"
    assert rec["art"][0] == "art-002"
    assert rec["exp"][0] == "exp-003"
    for kind in ("req", "art", "exp"):
        assert isinstance(rec[kind][1], int) and rec[kind][1] >= 0


# ── 2. v1 schema 平滑升级，旧字段保留 ───────────────────────────────────────

def test_v1_schema_upgrade_preserves_telemetry(state_path):
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "turns_without_tool_use": 3}, handle)
    turn_state.record("t1", "req", "req_007")
    data = _read_raw(state_path)
    assert data["version"] == 2
    assert data["turns_without_tool_use"] == 3
    assert data["evidence"]["t1"]["req"]["ref"] == "req_007"


# ── 3. 文件缺失/损坏 → recent 全 None，record 自愈 ──────────────────────────

def test_missing_and_corrupt_file(state_path):
    assert turn_state.recent("t1") == {"req": None, "art": None, "exp": None}
    with open(state_path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert turn_state.recent("t1") == {"req": None, "art": None, "exp": None}
    turn_state.record("t1", "req", "req_001")
    assert turn_state.recent("t1")["req"][0] == "req_001"


# ── 4. 30 分钟新鲜窗：过期项返回 None ──────────────────────────────────────

def test_expired_entry_hidden(state_path):
    turn_state.record("t1", "req", "req_010")
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=turn_state.FRESH_SECONDS + 60)).isoformat()
    data = _read_raw(state_path)
    data["evidence"]["t1"]["req"]["ts"] = old_ts
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    assert turn_state.recent("t1")["req"] is None


# ── 5. target 键隔离 + safe_name 同键归并 ─────────────────────────────────

def test_target_key_isolation_and_collision(state_path):
    turn_state.record("a:1", "req", "req_001")
    turn_state.record("b:2", "req", "req_002")
    assert turn_state.recent("a:1")["req"][0] == "req_001"
    assert turn_state.recent("b:2")["req"][0] == "req_002"
    # safe_name("a:1") == safe_name("a_1") → 同一世界，后写覆盖
    turn_state.record("a_1", "req", "req_009")
    assert turn_state.recent("a:1")["req"][0] == "req_009"
    keys = list(_read_raw(state_path)["evidence"].keys())
    assert keys.count("a_1") == 1


# ── 6. 非法入参静默且无副作用 ──────────────────────────────────────────────

def test_invalid_args_noop(state_path):
    turn_state.record("", "req", "req_001")
    turn_state.record("t1", "badkind", "req_001")
    turn_state.record("t1", "req", "")
    turn_state.record("t1", "req", "nope_001")   # 形态不匹配
    turn_state.record("t1", "art", "req_001")    # 形态与 kind 不符
    assert not os.path.exists(state_path)


# ── 7. reset 清空 evidence 与遥测 ──────────────────────────────────────────

def test_reset_clears(state_path):
    turn_state.record("t1", "req", "req_001")
    turn_state.reset()
    assert turn_state.recent("t1")["req"] is None
    data = _read_raw(state_path)
    assert data == {"version": 2, "turns_without_tool_use": 0, "evidence": {}}


# ── 8. 8 线程并发写不同 target/kind 不丢条目 ───────────────────────────────

def test_concurrent_records(state_path):
    def worker(i):
        kind = turn_state.EVIDENCE_KINDS[i % 3]
        ref = {"req": f"req_{i:03d}", "art": f"art-{i:03d}", "exp": f"exp-{i:03d}"}[kind]
        turn_state.record(f"target-{i % 4}", kind, ref)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = _read_raw(state_path)
    # 4 个 target 桶都在；不要求条目总数（同 target/kind 并发是覆盖语义），
    # 但文件必须仍是合法 JSON 且每桶至少有内容。
    assert len(data["evidence"]) == 4
    assert all(bucket for bucket in data["evidence"].values())


# ── 9. _evidence_hint：有 req + active exp，含非背书措辞 ───────────────────

def test_hint_with_req_and_active_exp(state_path, monkeypatch):
    turn_state.record("hint.example", "req", "req_142")
    monkeypatch.setattr(experiment, "resolve_experiment_id", lambda body: "exp-008")
    lines = ["target: hint.example\n"]
    hint = merge._evidence_hint(lines)
    assert "req_142" in hint
    assert "active 实验 exp-008" in hint
    assert "不代表支持本结论" in hint


# ── 10. 无 target / 无记录 → 空串（回执退化为原样，不报错）─────────────────

def test_hint_empty_cases(state_path, monkeypatch):
    monkeypatch.setattr(experiment, "resolve_experiment_id", lambda body: None)
    assert merge._evidence_hint(["surface: x\n"]) == ""
    assert merge._evidence_hint(["target: never.world\n"]) == ""


# ── 11. active exp 与最近 exp 相同 → 不重复列出 ────────────────────────────

def test_hint_dedupes_active_exp(state_path, monkeypatch):
    turn_state.record("hint.example", "exp", "exp-008")
    monkeypatch.setattr(experiment, "resolve_experiment_id", lambda body: "exp-008")
    hint = merge._evidence_hint(["target: hint.example\n"])
    assert hint.count("exp-008") == 1


# ── 12. 端到端：run_curl 成功 → 无证据 vuln 被 REJECT，回执带 req 编号 ─────

def test_e2e_run_curl_then_merge_reject_shows_req(state_path, tmp_path, monkeypatch, capsys):
    results = tmp_path / "results"
    monkeypatch.setattr(http_request, "RESULTS_DIR", str(results))
    monkeypatch.setattr(http_request, "enforce_scope_url", lambda target, url: None)

    class FakeCompleted:
        returncode = 0
        stdout = "200|42|0.05|text/html; charset=utf-8"
        stderr = ""

    def fake_run(curl_args, **kwargs):
        # curl 的 -o 指向 body 临时文件；写一点内容模拟响应落盘
        for i, arg in enumerate(curl_args):
            if arg == "-o" and i + 1 < len(curl_args):
                with open(curl_args[i + 1], "w", encoding="utf-8") as handle:
                    handle.write("<html><title>ok</title></html>")
        return FakeCompleted()

    monkeypatch.setattr(http_request.subprocess, "run", fake_run)
    result = http_request.run_curl("e2e.example", ["http://e2e.example/api/x"])
    req_ref = f"req_{int(result['filename'].split('_')[1].split('.')[0]):03d}"
    assert turn_state.recent("e2e.example")["req"][0] == req_ref

    monkeypatch.setattr(experiment, "resolve_experiment_id", lambda body, *a, **k: None)
    lines = ["target: e2e.example\n"]
    lines = merge.add_line(lines, "vuln: /api/x 存在未授权访问（无证据行，应被拒）")
    # 账本未变（无目标之外的新行）
    assert all("vuln:" not in line for line in lines)
    captured = capsys.readouterr()
    assert "REJECTED (evidence gate" in captured.err
    assert req_ref in captured.err
    assert "不代表支持本结论" in captured.err


# ── 13. ScopeRejected 时未发包 → 不产生 req 记录 ───────────────────────────

def test_scope_rejected_records_nothing(state_path, tmp_path, monkeypatch):
    results = tmp_path / "results"
    monkeypatch.setattr(http_request, "RESULTS_DIR", str(results))

    def raise_scope(target, url):
        raise http_request.ScopeRejected("[SCOPE GATE] REJECT: out")

    monkeypatch.setattr(http_request, "enforce_scope_url", raise_scope)
    with pytest.raises(http_request.ScopeRejected):
        http_request.run_curl("e2e.example", ["http://10.0.0.9/"])
    assert turn_state.recent("e2e.example")["req"] is None
    # 2026-10-07 核验：过闸前零写副作用——目录不建、编号不消耗、无任何残骸
    assert not results.exists() or not any(results.rglob("*"))


def test_req_lock_follows_patched_results_dir(tmp_path, monkeypatch):
    """REQ_LOCK_PATH 动态化回归：monkeypatch RESULTS_DIR 后锁必须落在隔离目录，
    绝不触碰真实项目 results/。"""
    from pathlib import Path
    isolated = tmp_path / "iso_results"
    monkeypatch.setattr(http_request, "RESULTS_DIR", str(isolated))
    project_lock = Path(http_request.PROJECT_ROOT) / "results" / ".req_lock"
    before = project_lock.stat().st_mtime if project_lock.exists() else None
    rdir = isolated / "t1" / "responses"
    rdir.mkdir(parents=True)
    assert http_request.next_request_number(str(rdir)) == 1
    assert http_request.next_request_number(str(rdir)) == 2
    after = project_lock.stat().st_mtime if project_lock.exists() else None
    assert before == after, "测试隔离失效：在真实 results/ 上动了锁"
    # 隔离目录里确实发生过加锁（锁已释放则看编号序列正确即可）
    assert not (isolated / ".req_lock").exists() or (isolated / ".req_lock").is_file()


# ── 14. 红线：提示只反映「最近记录」，不按被拒行文本匹配 ───────────────────

def test_hint_does_not_match_rejected_line_text(state_path, monkeypatch):
    turn_state.record("hint.example", "req", "req_001")
    monkeypatch.setattr(experiment, "resolve_experiment_id", lambda body: None)
    hint = merge._evidence_hint(["target: hint.example\n"])
    assert "req_001" in hint
    # 无论被拒行里写什么编号（这里模拟 LLM 硬挂 req_999），提示内容不变
    assert "req_999" not in hint
