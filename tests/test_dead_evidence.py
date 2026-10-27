#!/usr/bin/env python3
"""P0-b：dead 证据闸 + 拓扑三态（merge/world/graph/project 四处一致）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))

import merge
import world
import graph
import project
import pytest


# ── merge：闸 ────────────────────────────────────────────────────────────────

def test_dead_requires_evidence():
    lines = ["target: eng-1\n"]
    ok, reason = merge.evidence_gate("dead: 10.0.0.5:445 不可达", lines)
    assert not ok and "缺证据" in reason
    ok, reason = merge.evidence_gate("dead [filtered]: 10.0.0.5:445 ACL 丢包", lines)
    assert not ok


def test_dead_valid_reasons(tmp_path, monkeypatch):
    # 准备可解析的 art 证据
    monkeypatch.setattr(merge, "RESULTS_DIR", str(tmp_path))
    art_dir = tmp_path / "eng-1" / "artifacts"
    art_dir.mkdir(parents=True)
    (art_dir / "index.json").write_text(
        '{"art-001": {"sha256": "x", "size": 1, "kind": "stdout"}}', encoding="utf-8")
    lines = ["target: eng-1\n"]
    for reason in merge.DEAD_REASONS:
        ok, _ = merge.evidence_gate(f"dead [{reason}]: 10.0.0.5:445 x (art-001)", lines)
        assert ok, reason


def test_dead_invalid_reason_rejected():
    lines = ["target: eng-1\n"]
    ok, reason = merge.evidence_gate("dead [foo]: x (art-001)", lines)
    assert not ok and "filtered" in reason and "closed" in reason and "unreachable" in reason


def test_bare_dead_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(merge, "RESULTS_DIR", str(tmp_path))
    art_dir = tmp_path / "eng-1" / "artifacts"
    art_dir.mkdir(parents=True)
    (art_dir / "index.json").write_text(
        '{"art-002": {"sha256": "x", "size": 1, "kind": "stdout"}}', encoding="utf-8")
    lines = ["target: eng-1\n"]
    ok, _ = merge.evidence_gate("dead: 10.0.0.5:445 不可达 (art-002)", lines)
    assert ok


def test_unresolvable_dead_evidence_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(merge, "RESULTS_DIR", str(tmp_path))
    (tmp_path / "eng-1").mkdir()
    lines = ["target: eng-1\n"]
    ok, reason = merge.evidence_gate("dead [closed]: 10.0.0.5:22 关闭 (art-999)", lines)
    assert not ok and "art-999" in reason


# ── world：分桶（静默掉桶回归锁）─────────────────────────────────────────────

def test_world_dead_reasons_bucket_to_counters():
    lines = [
        "target: eng-1\n",
        "dead [filtered]: 10.0.0.5:445 ACL 丢包 (art-001)\n",
        "dead [closed]: 10.0.0.5:22 关闭 (art-002)\n",
        "dead [unreachable]: 10.0.2.0/24 无路由 (art-003)\n",
        "dead: 10.0.0.6:80 不可达 (art-004)\n",
        "counter: 族级排除 (exp-001)\n",
    ]
    view = world.parse(lines)
    raws = {c.raw: c for c in view.counters}
    assert len(view.counters) == 5
    f = raws["dead [filtered]: 10.0.0.5:445 ACL 丢包 (art-001)"]
    assert f.prefix == "dead" and f.state == "filtered"
    assert raws["dead [closed]: 10.0.0.5:22 关闭 (art-002)"].state == "closed"
    assert raws["dead [unreachable]: 10.0.2.0/24 无路由 (art-003)"].state == "unreachable"
    assert raws["dead: 10.0.0.6:80 不可达 (art-004)"].prefix == "dead"
    assert raws["dead: 10.0.0.6:80 不可达 (art-004)"].state is None
    # 关键：不得有 dead 行落 objects 桶
    assert not any(o.prefix == "" and o.raw.startswith("dead [") for o in view.objects)


# ── graph：kind 归类 ─────────────────────────────────────────────────────────

def test_graph_classifies_dead_reasons_as_exclusion():
    g = graph.parse([
        "dead [filtered]: x (art-001)\n",
        "dead [closed]: y (art-002)\n",
        "dead: z (art-003)\n",
    ])
    kinds = {n.raw.split(" ")[0] + (n.raw.split(" ")[1] if n.raw.startswith("dead [") else ""): n.kind
             for n in g.nodes}
    assert all(n.kind == graph.KIND_EXCLUSION for n in g.nodes)


# ── project：投影分组（有状态分组 / 无状态逐字节不变）────────────────────────

def test_excluded_projection_legacy_unchanged_when_no_states():
    view = world.parse([
        "target: eng-1\n",
        "counter: 某族排除 (exp-001)\n",
        "dead: 10.0.0.5:445 不可达 (art-001)\n",
    ])
    out = project.format_excluded_section(view)
    assert out.startswith("[EXCLUDED] (2) — 已排除方向，勿重试（详情见 memory）")
    assert "filtered" not in out  # 无三态时不出现新标签


def test_excluded_projection_grouped_with_states():
    view = world.parse([
        "target: eng-1\n",
        "counter: 某族排除 (exp-001)\n",
        "dead [filtered]: 10.0.0.5:445 ACL 丢包 (art-001)\n",
        "dead [closed]: 10.0.0.5:22 关闭 (art-002)\n",
        "dead [unreachable]: 10.0.2.0/24 无路由 (art-003)\n",
    ])
    out = project.format_excluded_section(view)
    assert "[EXCLUDED] (4)" in out
    assert "filtered" in out and "closed" in out and "unreachable" in out
    assert "换立足点" in out and "勿重试" in out and "族级收口" in out
    assert "counter / 其他排除 (1)" in out
