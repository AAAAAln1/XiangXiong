#!/usr/bin/env python3
"""verify_shell_paths.py — PowerShell 侧门修复的确定性验证（2026-08-11）。

用合成 hook 输入直接调两个 guard，不依赖模型选哪个 shell 工具。
分两轮：
  轮1 Bash 载荷      → 验"没退化"
  轮2 PowerShell 载荷 → 验"侧门堵上"

为什么不用 live session：模型选shell 通路不可控。若让模型发 Bash 载荷，
只能证明「本来就通的 Bash」，证不了修复。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
LONG = "python -c \"print('x') # " + "A" * 600 + "\""

CASES = [
    ("Bash", "轮1 回归"),
    ("PowerShell", "轮2 侧门"),
]


def run_guard(script, tool_name, command, env_extra=None):
    payload = json.dumps({"tool_name": tool_name,
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([PY, os.path.join(ROOT, "scripts", script)],
                          input=payload, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=ROOT)
    return proc.stdout.strip(), proc.stderr.strip()


def verdict_of(out):
    try:
        obj = json.loads(out) if out else {}
    except json.JSONDecodeError:
        return "NO-OUTPUT", {}
    d = (obj.get("hookSpecificOutput") or {})
    return d.get("permissionDecision", "PASS(无 deny)"), obj


print("=" * 66)
print("inline_guard：两轮验证（XIANGXIONG_INLINE_GUARD=deny）")
print("=" * 66)
inline_ok = True
for tool, label in CASES:
    out, _err = run_guard("inline_guard.py", tool, LONG,
                          {"XIANGXIONG_INLINE_GUARD": "deny"})
    v, obj = verdict_of(out)
    hit = (v == "deny")
    inline_ok &= hit
    print(f"  [{'OK  ' if hit else 'FAIL'}] {label:10s} tool={tool:12s} -> {v}")

print()
print("=" * 66)
print("memory_guard：账本写入保护（两轮都应 deny）")
print("=" * 66)
mem_ok = True
for tool in ("Bash", "PowerShell"):
    cmd = f"echo x >> results/probe.invalid/memory.md"
    out, _err = run_guard("memory_guard.py", tool, cmd)
    v, obj = verdict_of(out)
    hit = (v == "deny")
    mem_ok &= hit
    print(f"  [{'OK  ' if hit else 'FAIL'}] tool={tool:12s} -> {v}")

print()
print("=" * 66)
print(f"结论：inline_guard {'通过' if inline_ok else '未通过'} · "
      f"memory_guard {'通过' if mem_ok else '未通过'}")
print("=" * 66)
sys.exit(0 if (inline_ok and mem_ok) else 1)