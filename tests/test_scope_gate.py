#!/usr/bin/env python3
"""P0-c：scope 授权闸（scope.py 判定 / http 第一层 / execute 第二层 / merge WARN）。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))

import scope
import execute
import http_request
import merge


@pytest.fixture
def engaged(tmp_path, monkeypatch):
    # scope.RESULTS_DIR = <root>/results，execute.PROJECT_ROOT = <root>，与生产布局一致
    root = tmp_path / "proj"
    results = root / "results"
    monkeypatch.setattr(scope, "RESULTS_DIR", str(results))
    monkeypatch.setattr(http_request, "_scope", scope)
    monkeypatch.setattr(execute, "_scope", scope)
    monkeypatch.setattr(execute, "PROJECT_ROOT", str(root))
    monkeypatch.setattr(execute, "RESULTS_DIR", str(results))
    scope.init_scope("eng-1", ["10.0.0.0/24", "10.0.1.16/32"], ["10.0.0.1"])
    return results


# ── scope.evaluate 判定 ──────────────────────────────────────────────────────

def test_allow_deny_out_of_scope(engaged):
    assert scope.evaluate("eng-1", "10.0.0.50")["decision"] == "allow"
    assert scope.evaluate("eng-1", "10.0.1.16")["decision"] == "allow"
    assert scope.evaluate("eng-1", "10.0.0.1")["decision"] == "deny"  # deny 优先
    assert scope.evaluate("eng-1", "10.0.2.5")["decision"] == "out_of_scope"


def test_loopback_local(engaged):
    assert scope.evaluate("eng-1", "127.0.0.1")["decision"] == "local"
    assert scope.evaluate("eng-1", "169.254.1.1")["decision"] == "local"


def test_no_scope_private_vs_public(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    assert scope.evaluate("eng-x", "10.0.0.9")["decision"] == "no_scope"
    assert scope.evaluate("eng-x", "8.8.8.8")["decision"] == "no_scope"  # 公网由调用方维持原路径


def test_hostname_declared(engaged):
    scope.init_scope("eng-2", ["dc01.corp.local"], [])
    assert scope.evaluate_hostname("eng-2", "dc01.corp.local")["decision"] == "allow"
    assert scope.evaluate_hostname("eng-2", "other.corp.local")["decision"] == "out_of_scope"


def test_literal_extraction_strict():
    text = "nmap 10.0.0.50 and 10.0.1.0/24, version 1.2.3.4567, 8.8.8.8"
    toks = scope.extract_ip_literals(text)
    assert "10.0.0.50" in toks and "10.0.1.0/24" in toks and "8.8.8.8" in toks
    assert not any("1.2.3.4567" in t for t in toks)


def test_private_detection():
    assert scope.is_private_literal("10.0.0.0/24")
    assert scope.is_private_literal("172.16.5.1")
    assert scope.is_private_literal("192.168.1.1")
    assert not scope.is_private_literal("8.8.8.8")


def test_init_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        scope.init_scope("e", ["10.0.0." + str(i) for i in range(20)], [])
    with pytest.raises(ValueError):
        scope.init_scope("e", ["10.0.0.0/24" + "x" * 50], [])
    with pytest.raises(ValueError):
        scope.init_scope("e", ["not an entry !"], [])


# ── http 第一层 ──────────────────────────────────────────────────────────────

def test_http_gate_rejects_out_of_scope(engaged):
    with pytest.raises(http_request.ScopeRejected):
        http_request.enforce_scope_url("eng-1", "http://10.0.2.5/")
    with pytest.raises(http_request.ScopeRejected):
        http_request.enforce_scope_url("eng-1", "http://10.0.0.1/admin")


def test_http_gate_allows_in_scope(engaged):
    http_request.enforce_scope_url("eng-1", "http://10.0.0.50/")
    http_request.enforce_scope_url("eng-1", "https://10.0.1.16:8443/login")


def test_http_gate_private_without_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(http_request, "_scope", scope)
    with pytest.raises(http_request.ScopeRejected):
        http_request.enforce_scope_url("lone", "http://192.168.0.5/")


def test_http_gate_public_without_scope_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(http_request, "_scope", scope)
    monkeypatch.setattr(scope, "resolve_hostname_ips", lambda h: [])  # 不做真实 DNS
    # 公网 IP 直接放行
    http_request.enforce_scope_url("site", "https://example.com/")


# ── execute 第二层 ───────────────────────────────────────────────────────────

def test_execute_gate_command_literals(engaged):
    execute.enforce_scope_command("eng-1", "nmap -sT 10.0.0.50")  # 放行
    with pytest.raises(execute.ScopeRejected):
        execute.enforce_scope_command("eng-1", "nmap 10.0.2.9")
    with pytest.raises(execute.ScopeRejected):
        execute.enforce_scope_command("eng-1", "nmap 10.0.0.1")


def test_execute_gate_no_scope_private(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(execute, "_scope", scope)
    with pytest.raises(execute.ScopeRejected):
        execute.enforce_scope_command("lone", "curl http://10.0.0.1/")
    # 无目标命令、无 IP → 放行
    execute.enforce_scope_command("lone", "whoami")


def test_execute_gate_reads_referenced_script(engaged):
    # engaged 返回 <root>/results；命令中的相对形态 results/eng-1/scripts/scan.py
    # 会被拼到 execute.PROJECT_ROOT(<root>) 下读取
    scripts = engaged / "eng-1" / "scripts"
    scripts.mkdir(parents=True)
    sp = scripts / "scan.py"
    sp.write_text("import os\nos.system('nmap 10.0.2.9')\n", encoding="utf-8")
    cmd = "python results/eng-1/scripts/scan.py"
    with pytest.raises(execute.ScopeRejected):
        execute.enforce_scope_command("eng-1", cmd)
    # 授权段内 → 放行
    sp.write_text("print('10.0.0.20')\n", encoding="utf-8")
    execute.enforce_scope_command("eng-1", cmd)


# ── merge engagement WARN ────────────────────────────────────────────────────

def test_merge_engagement_warn(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    import importlib
    importlib.reload(merge)
    # 无 scope + 私网 surface → WARN
    lines = ["target: lone\n"]
    merge.add_line(lines, "surface: host 10.0.1.16:80 open")
    err = capsys.readouterr().err
    assert "scope.py init" in err


def test_merge_no_warn_for_public(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    import importlib
    importlib.reload(merge)
    lines = ["target: example.com\n"]
    merge.add_line(lines, "surface: 1.2.3.4 是 CDN 节点 (req_001)")
    err = capsys.readouterr().err
    assert "scope.py init" not in err
