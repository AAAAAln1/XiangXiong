#!/usr/bin/env python3
"""P1-a：execute head/tail 回显 + 后台 worker 状态机/art 登记不丢。"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))

import execute
import scope


def test_trim_head_tail_keeps_endings_and_path():
    out = execute.trim_output("A" * 3000 + "TAIL_MARKER", max_chars=100, artifact_path="results/x/f.txt")
    assert out.startswith("A" * 50)
    assert "TAIL_MARKER" in out  # 尾部结论保留（prefix-only 会丢）
    assert "results/x/f.txt" in out  # 路径完整
    assert "3011" in out  # 总长度披露


def test_trim_short_unchanged():
    assert execute.trim_output("short", max_chars=100) == "short"


def _args(target, cmd, timeout=60):
    return types.SimpleNamespace(
        target=target, command=cmd, timeout=timeout, name="experiment",
        purpose=None, experiment_id=None, max_output=execute.MAX_OUTPUT_CHARS)


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    """后台 worker 是独立进程：用环境变量让父子进程共用同一临时 results。"""
    monkeypatch.setenv("XIANGXIONG_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(execute, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(execute, "_scope", scope)
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    return tmp_path


def test_background_job_completes_and_registers_art(isolated_results):
    tmp_path = isolated_results
    py = sys.executable
    args = _args("eng-bg", f'"{py}" -c "print(\'bg-hello\')"', timeout=30)
    job_id = execute.start_background(args)
    assert job_id == "exec-001"
    job = execute.wait_job("eng-bg", job_id, 30)
    assert job["status"] == "completed", job
    assert job.get("artifacts"), "art 编号必须在 worker 内回填（detached 不丢登记）"
    index = json.load(open(tmp_path / "eng-bg" / "artifacts" / "index.json", encoding="utf-8"))
    assert job["artifacts"]["stdout"] in index
    assert job["metadata"]


def test_background_rejects_scope_before_spawn(isolated_results):
    tmp_path = isolated_results
    args = _args("lone", "nmap 10.0.99.9")
    with pytest.raises(execute.ScopeRejected):
        execute.start_background(args)
    assert not (tmp_path / "lone" / "jobs").exists() or not any(
        (tmp_path / "lone" / "jobs").iterdir())


def test_background_cancel_long_job(isolated_results):
    py = sys.executable
    args = _args("eng-cancel", f'"{py}" -c "import time; time.sleep(30)"', timeout=60)
    job_id = execute.start_background(args)
    import time as _t
    # 等 worker 进入 running 并拿到 pid
    for _ in range(40):
        j = execute.job_status("eng-cancel", job_id)
        if j.get("pid") and j["status"] == "running":
            break
        _t.sleep(0.25)
    worker_pid = execute._read_job("eng-cancel", job_id)["pid"]
    assert worker_pid
    execute.cancel_job("eng-cancel", job_id)
    # 确定性断言：worker 进程必须真的死掉（树杀有效），而不是仅改状态
    for _ in range(40):
        if not execute._pid_alive(worker_pid):
            break
        _t.sleep(0.25)
    assert not execute._pid_alive(worker_pid), "cancel 后 worker 仍存活（树杀失效）"
    job = execute.wait_job("eng-cancel", job_id, 5)
    assert job["status"] in ("cancelled", "failed", "orphaned")
