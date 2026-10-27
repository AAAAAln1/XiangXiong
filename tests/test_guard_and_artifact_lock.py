#!/usr/bin/env python3
"""P0-d：memory_guard 新保护文件/白名单 + register_artifact 并发锁。"""

import importlib.util
import json
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "runtime"))

_spec = importlib.util.spec_from_file_location(
    "memory_guard", os.path.join(ROOT, "scripts", "memory_guard.py"))
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

import execute


# ── memory_guard：Write/Edit 路径保护 ────────────────────────────────────────

def test_protected_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "RESULTS_DIR", os.path.normcase(str(tmp_path / "results")))
    for rel in (
        os.path.join("eng-1", "scope.json"),
        os.path.join("eng-1", "credentials", "index.json"),
        os.path.join("eng-1", "credentials", "c-003.txt"),
        os.path.join("eng-1", "artifacts", "index.json"),
        os.path.join("eng-1", "memory.md"),
    ):
        assert guard._is_protected_path(str(tmp_path / "results" / rel)), rel


def test_unprotected_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "RESULTS_DIR", os.path.normcase(str(tmp_path / "results")))
    # 凭据输入临时文件、扫描产物、脚本不保护
    for rel in (
        os.path.join("eng-1", "credentials", "_in.txt"),
        os.path.join("eng-1", "artifacts", "art-001.txt"),
        os.path.join("eng-1", "scripts", "scan.py"),
    ):
        assert not guard._is_protected_path(str(tmp_path / "results" / rel)), rel


def test_bash_protected_regex():
    assert guard._bash_touches_protected("type results/eng-1/scope.json")
    assert guard._bash_touches_protected("cat > results/eng-1/credentials/c-002.txt")
    assert guard._bash_touches_protected("echo x >> results/e/credentials/index.json")


def test_new_tools_whitelisted_segments():
    # 新工具命令段本身豁免，不触保护路径
    assert not guard._is_memory_write(
        "Bash", {"command": "python runtime/scope.py init eng-1 --allow 10.0.0.0/24"})
    assert not guard._is_memory_write(
        "Bash", {"command": "python runtime/credentials.py put eng-1 --from-file x.txt"})
    # 命令链他段仍检：新工具段 + 裸写 scope.json → deny
    assert guard._is_memory_write(
        "Bash",
        {"command": "python runtime/scope.py check eng-1 --ip 10.0.0.1 && echo x > results/eng-1/scope.json"})


# ── register_artifact 并发锁 ─────────────────────────────────────────────────

def test_concurrent_register_no_lost_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(execute, "RESULTS_DIR", str(tmp_path))
    errors = []
    ids = []
    lock = threading.Lock()

    def worker(i):
        try:
            p = tmp_path / f"out{i}.txt"
            p.write_text(f"output-{i}", encoding="utf-8")
            aid = execute.register_artifact("eng", str(p), source_cmd=f"cmd{i}", kind="stdout")
            with lock:
                ids.append(aid)
        except Exception as e:  # noqa
            with lock:
                errors.append(repr(e))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errors
    assert len(ids) == 16 and len(set(ids)) == 16
    index = json.load(open(tmp_path / "eng" / "artifacts" / "index.json", encoding="utf-8"))
    assert len(index) == 16
