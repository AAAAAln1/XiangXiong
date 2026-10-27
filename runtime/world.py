#!/usr/bin/env python3
"""
world.py — Phase 1.2 schema parse only (H0–H15).

Reads memory lines into a closed WorldView for later render helpers.
Does not interpret the world, rank, filter, or own belief schema.

True source of each entry is the raw line (H3 lossless).
WorldView is a temporary projection helper, not canonical storage (H11).
"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Optional, Tuple

# Hypothesis optional states already defined by memory contract / merge prefixes.
# `sweep` = 批量机械验证类工作（枚举/爆破/逐条比对）：语义上仍是"未验证的假设"，
# 但它的**正确执行方式是派发给独立 context**，而不是主会话串行做 N 次。
# 这是记账副产品派发的载体（规格 §8.0g）：主 agent 写下一个 [sweep] 行，
# 投影就机械地把它列进可派发队列。
_HYPOTHESIS_STATES = frozenset(
    {"observed", "confirmed", "contradicted", "blocked", "unknown", "sweep"}
)

# dead 拓扑三态（与 merge.DEAD_REASONS 同源语义；此处只做机械携带）。
_DEAD_STATES = frozenset({"filtered", "closed", "unreachable"})
_DEAD_PREFIX_RE = re.compile(r"^dead(?:\s+\[(\w+)\])?:\s*")


def _parse_dead(stripped: str) -> ParsedLine:
    """dead: / dead [reason]: exclusion 行解析。

    关键：prefix 统一归一为 "dead"，reason 进 state；否则 `dead [filtered]:` 会因
    startswith("dead:") 失配而静默落入 unrecognized/objects 桶（投影里排除直接消失，
    不抛异常——本仓库最警惕的静默失败形态，2026-10-06 实测复现）。
    """
    m = _DEAD_PREFIX_RE.match(stripped)
    state = m.group(1) if (m and m.group(1)) else None
    return ParsedLine(raw=stripped, prefix="dead", state=state)


class ParsedLine(NamedTuple):
    """One memory line: raw is the sole recoverable source text."""

    raw: str
    prefix: str
    state: Optional[str] = None


class WorldView(NamedTuple):
    """
    Closed field set only (H7). Maps existing memory schema buckets.
    Not a second belief store (H11). Field set is closed; no extra belief attributes.
    """

    target: str
    objects: Tuple[ParsedLine, ...]
    unknowns: Tuple[ParsedLine, ...]
    constraints: Tuple[ParsedLine, ...]
    counters: Tuple[ParsedLine, ...]


def _strip_line(line: str) -> str:
    return line.rstrip("\n\r")


def _parse_hypothesis(stripped: str) -> ParsedLine:
    """Extract optional [state] metadata; never drop or rewrite raw."""
    state = None
    # Forms: "hypothesis: ..." | "hypothesis [state]: ..."
    if stripped.startswith("hypothesis ["):
        close_i = stripped.find("]")
        if close_i != -1:
            candidate = stripped[len("hypothesis [") : close_i].strip().lower()
            if candidate in _HYPOTHESIS_STATES:
                state = candidate
    return ParsedLine(raw=stripped, prefix="hypothesis", state=state)


def parse(lines: List[str]) -> WorldView:
    """
    Schema bucket parse only.

    - No sort, no filter, no merge of hypotheses, no invented fields.
    - Empty lines skipped (not schema).
    - Unknown prefixes: keep raw under objects without inventing ontology.
    - Order within each bucket = input order.
    """
    target = ""
    objects: List[ParsedLine] = []
    unknowns: List[ParsedLine] = []
    constraints: List[ParsedLine] = []
    counters: List[ParsedLine] = []

    for line in lines:
        stripped = _strip_line(line).strip()
        if not stripped:
            continue

        if stripped.startswith("target:"):
            target = stripped[len("target:") :].strip()
            continue

        if stripped.startswith("surface:") or stripped.startswith("vuln:") or stripped.startswith("cred:"):
            prefix = stripped.split(":", 1)[0]
            objects.append(ParsedLine(raw=stripped, prefix=prefix, state=None))
            continue

        if (
            stripped.startswith("tech:")
            or stripped.startswith("port:")
            or stripped.startswith("artifact:")
        ):
            prefix = stripped.split(":", 1)[0]
            constraints.append(ParsedLine(raw=stripped, prefix=prefix, state=None))
            continue

        if stripped.startswith("hypothesis:") or stripped.startswith("hypothesis ["):
            unknowns.append(_parse_hypothesis(stripped))
            continue
        # bare "hypothesis" without colon is not valid memory schema; keep raw
        if stripped == "hypothesis" or (
            stripped.startswith("hypothesis") and not stripped.startswith("hypothesis:")
            and not stripped.startswith("hypothesis [")
        ):
            objects.append(ParsedLine(raw=stripped, prefix="hypothesis", state=None))
            continue

        if stripped.startswith("counter:"):
            counters.append(ParsedLine(raw=stripped, prefix="counter", state=None))
            continue
        if stripped.startswith("dead") and (
            stripped.startswith("dead:")
            or stripped.startswith("dead [")
            or stripped.startswith("dead-summary:")
        ):
            if stripped.startswith("dead-summary:"):
                counters.append(ParsedLine(raw=stripped, prefix="dead-summary", state=None))
            else:
                counters.append(_parse_dead(stripped))
            continue

        if stripped.startswith("goal:") or stripped.startswith("subgoal:"):
            # 搜索的终止条件 / 阶段里程碑。进 objects（无独立 bucket），
            # 由 project 单独成段 [GOAL] —— 它是唯一的方向性信息。
            prefix = stripped.split(":", 1)[0]
            objects.append(ParsedLine(raw=stripped, prefix=prefix, state=None))
            continue

        if stripped.startswith("future:"):
            # Existing schema tag; no special handling invented — keep raw, bucket objects.
            objects.append(ParsedLine(raw=stripped, prefix="future", state=None))
            continue

        # Unrecognized non-empty line: do not drop (lossless); no new ontology name.
        objects.append(ParsedLine(raw=stripped, prefix="", state=None))

    return WorldView(
        target=target,
        objects=tuple(objects),
        unknowns=tuple(unknowns),
        constraints=tuple(constraints),
        counters=tuple(counters),
    )


def iter_raw_lines(view: WorldView) -> List[str]:
    """
    Recover raw texts from a WorldView for lossless checks.
    target is emitted as a synthetic target: line when non-empty.
    Bucket order: objects, constraints, unknowns, counters (parse order within).
    Not a canonical memory serializer — helper for tests / H3 only.
    """
    out: List[str] = []
    if view.target:
        out.append(f"target: {view.target}")
    for bucket in (view.objects, view.constraints, view.unknowns, view.counters):
        for item in bucket:
            out.append(item.raw)
    return out
