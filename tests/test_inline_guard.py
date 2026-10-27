#!/usr/bin/env python3
"""P3：inline_guard 超长内联检测（heredoc/-c、切段豁免、warn/deny、fail-open）。"""

import importlib.util
import io
import json
import os
import sys
import contextlib

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "inline_guard", os.path.join(ROOT, "scripts", "inline_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()


def _heredoc(terminator="PY", n=600):
    return f"python - <<'{terminator}'\n" + "x = 1\n" * (n // 6) + f"\n{terminator}\n"


# ── 检测规则 ─────────────────────────────────────────────────────────────────

def test_heredoc_long_detected_any_terminator():
    assert guard.detect_heredoc(_heredoc("PY"))[0] == "heredoc"
    assert guard.detect_heredoc(_heredoc("EOF"))[0] == "heredoc"
    assert guard.detect_heredoc(_heredoc("ENDPY"))[0] == "heredoc"


def test_heredoc_short_not_detected():
    cmd = "python - <<'PY'\nprint(1)\nPY\n"
    assert guard.detect_heredoc(cmd) is None


def test_heredoc_unclosed_conservative():
    cmd = "python - <<'PY'\n" + "x=1\n" * 200  # 无闭合
    assert guard.detect_heredoc(cmd) is not None


def test_runtime_stdin_heredoc_exempt():
    # 合法记账路径：merge 从 heredoc stdin 读 DELTA，必须豁免
    cmd = "python runtime/merge.py - <<'EOF'\n" + "surface: x\n" * 100 + "\nEOF\n"
    assert guard.evaluate(cmd) is None


def test_pyc_long_and_short():
    long_code = "import json; " + "print(1); " * 80
    assert guard.detect(f'python -c "{long_code}"')[0] == "pyc"
    assert guard.detect('python -c "print(1)"') is None


def test_pyc_single_quote_and_bare():
    long_code = "x = 1\n" * 80
    assert guard.detect(f"python3 -c '{long_code}'") is not None
    assert guard.detect("python -c " + "print(1) " * 200) is not None


def test_non_python_forms_pass():
    assert guard.detect("python scripts/x.py") is None
    assert guard.detect("dotnet run") is None
    assert guard.detect("curl http://x/") is None
    assert guard.detect("python runtime/http_request.py t http://x/") is None


def test_chain_only_inline_segment_matches():
    cmd = "cd x && " + _heredoc()
    assert guard.evaluate(cmd) is not None


def test_runtime_segment_exempt_but_sibling_checked():
    # runtime 工具段豁免；同链另一段超长内联仍命中
    cmd = "python runtime/merge.py --delta-file x && " + _heredoc()
    found = guard.evaluate(cmd)
    assert found is not None and found[0] == "heredoc"
    # 只有 runtime 段 → 放行
    assert guard.evaluate("python runtime/evidence.py inventory t") is None


def test_threshold_boundary():
    # 构造恰好 400 / 401 的 -c 载荷
    assert guard.detect('python -c "' + "a" * 400 + '"') is None
    assert guard.detect('python -c "' + "a" * 401 + '"') is not None


def test_windows_quoted_heredoc_form():
    cmd = 'python - <<\\"PY\\"\n' + "x=1\n" * 200 + "\nPY\n"
    assert guard.detect_heredoc(cmd) is not None


# ── warn / deny 行为 ─────────────────────────────────────────────────────────

def _hook_payload(command, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("XIANGXIONG_INLINE_GUARD", mode)
    monkeypatch.setattr(guard, "MODE", mode)
    monkeypatch.setattr(guard, "LOG_PATH", str(tmp_path / "_inline_guard.log"))
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def test_warn_mode_logs_and_does_not_deny(tmp_path, monkeypatch, capsys):
    payload = _hook_payload(_heredoc(), tmp_path, monkeypatch, "warn")
    sys.stdin = io.StringIO(payload)
    guard.main()
    out, err = capsys.readouterr()
    assert "WARN" in err and "deny" not in out.lower()
    log = open(str(tmp_path / "_inline_guard.log"), encoding="utf-8").read()
    entry = json.loads(log)
    assert entry["shape"] == "heredoc" and entry["length"] > 400
    # 日志只留命令哈希，不留原文/载荷
    assert "cmd_sha1" in entry and len(entry["cmd_sha1"]) == 12
    assert "x = 1" not in log and "preview" not in entry


def test_deny_mode_emits_decision_with_two_actions(tmp_path, monkeypatch, capsys):
    cmd = "cd results/foo.com && " + _heredoc()
    payload = _hook_payload(cmd, tmp_path, monkeypatch, "deny")
    sys.stdin = io.StringIO(payload)
    guard.main()
    out, err = capsys.readouterr()
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "foo.com" in reason and "Write" in reason and "execute.py" in reason


def test_deny_generic_target_when_unknown(tmp_path, monkeypatch, capsys):
    payload = _hook_payload(_heredoc(), tmp_path, monkeypatch, "deny")
    sys.stdin = io.StringIO(payload)
    guard.main()
    out, _ = capsys.readouterr()
    assert "<target>" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


# ── fail-open ────────────────────────────────────────────────────────────────

def test_fail_open_bad_stdin(capsys):
    sys.stdin = io.StringIO("{not json")
    guard.main()  # 不抛
    out, _ = capsys.readouterr()
    assert out == ""


def test_non_bash_tool_ignored(tmp_path, monkeypatch, capsys):
    payload = json.dumps({"tool_name": "Write", "tool_input": {"command": _heredoc()}})
    sys.stdin = io.StringIO(payload)
    guard.main()
    out, _ = capsys.readouterr()
    assert out == ""


# ── PowerShell 侧门（2026-08-11）──────────────────────────────────────────────
# 本机存在第二条 shell 通路 PowerShell（首字母大写）。修复前它既不在 settings
# 的 hook matcher，也不在 permissions.allow（那里写的是小写 s 的
# `Powershell(*)`），因此绕开全部 hook —— 一条 latent 侧门。
# 全量会话普查：123 文件 / Bash 6553 次 / PowerShell 8 次，且 8 次全部来自探针
# 自身 → latent 缺口而非 observed 漏管。但Stage 1 大量 spawn headless session，
# 模型选通路的倾向可能改变，故按「通路存在即看守」处理。

@pytest.mark.parametrize("tool", ["Bash", "PowerShell", "Powershell"])
def test_both_shell_paths_are_guarded(tool, tmp_path, monkeypatch, capsys):
    """两条 shell 通路都必须被拦 —— 侧门修复的核心断言。

    注意 MODE 必须 monkeypatch 到模块属性上：它在 import 时读env，
    只setenv 不生效（这正是第一次写这组测试失败的原因）。
    """
    monkeypatch.setenv("XIANGXIONG_INLINE_GUARD", "deny")
    monkeypatch.setattr(guard, "MODE", "deny")
    monkeypatch.setattr(guard, "LOG_PATH", str(tmp_path / "g.log"))
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": _heredoc()}})
    sys.stdin = io.StringIO(payload)
    guard.main()
    out, _ = capsys.readouterr()
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_shell_short_command_still_passes(tool, tmp_path, monkeypatch, capsys):
    """短命令不应被拦 —— 证明修复没有把 Bash 通路一起堵死（无退化）。"""
    monkeypatch.setenv("XIANGXIONG_INLINE_GUARD", "deny")
    monkeypatch.setattr(guard, "MODE", "deny")
    monkeypatch.setattr(guard, "LOG_PATH", str(tmp_path / "g.log"))
    payload = json.dumps({"tool_name": tool,
                          "tool_input": {"command": "python runtime/http_request.py"}})
    sys.stdin = io.StringIO(payload)
    guard.main()
    out, _ = capsys.readouterr()
    assert out == ""


def test_shell_tools_constant_is_shared():
    """SHELL_TOOLS 必须来自 hook_patterns 单一实现，不许各自硬编码。

    hook_patterns 的 docstring 已记录五处副本漂移学费，不该造第六处。
    """
    from hook_patterns import SHELL_TOOLS
    assert SHELL_TOOLS == frozenset({"Bash", "PowerShell", "Powershell"})
    assert guard.SHELL_TOOLS is SHELL_TOOLS


def test_unknown_shell_tool_still_ignored(tmp_path, monkeypatch, capsys):
    """未知工具名仍应 fail-open（不放行未知 shell，避免误伤非 shell 工具）。"""
    monkeypatch.setenv("XIANGXIONG_INLINE_GUARD", "deny")
    monkeypatch.setattr(guard, "MODE", "deny")
    payload = json.dumps({"tool_name": "Zsh", "tool_input": {"command": _heredoc()}})
    sys.stdin = io.StringIO(payload)
    guard.main()
    out, _ = capsys.readouterr()
    assert out == ""
