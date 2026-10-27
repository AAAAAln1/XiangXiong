#!/usr/bin/env python3
"""
diff_scan.py — 机械响应差分扫描（层 2：外部差分检测）。

零攻击知识，只做机械物理量比对：
- 按 (method, scheme://host/path) 分组（去 query/fragment/尾斜杠）
- 组内 http 状态码 ≥2 种 → 候选差分
- 或同状态码但体长差 ≥2 倍 → 候选差分
- 不判含义、不判好坏、不提取字段、不读 memory

输出协议：
  CLI: python runtime/diff_scan.py <target>   → 打印可读 [DIFFS] 段
  库:  scan_diffs(results_root[, target])     → [ {method,url,reqs,statuses,lengths}, ... ]
       group_max_reqs_by_target(results_root) → {target: max_req}（增量水位线用）

原则：只把机械差分「拿给模型看」，不自动写 memory（World Model 单主人不变）。
"""

import json
import os
import re
import sys
from urllib.parse import urlparse

from datetime import datetime, timezone

try:
    from .paths import safe_name as safe_target
except ImportError:
    from paths import safe_name as safe_target

DIFF_MAX_GROUPS = 8
LENGTH_RATIO_THRESHOLD = 2.0

# 传输层失败码（000 = 无响应），不是目标差分
SKIP_STATUSES = {"000"}


def target_responses_dir(results_root: str, target: str) -> str:
    return os.path.join(results_root, safe_target(target), "responses")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_meta(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None


def _group_key(meta: dict) -> tuple | None:
    """(method, netloc+path)。URL 缺失或无法解析 → None（跳过）。"""
    url = meta.get("url")
    if not url:
        return None
    try:
        parsed = urlparse(url)
        netloc = (parsed.netloc or "").lower()
        path = parsed.path.rstrip("/") or "/"
    except ValueError:
        return None
    if not netloc:
        return None
    method = (meta.get("method") or "GET").upper()
    return (method, f"{parsed.scheme}://{netloc}{path}")


_STATUS_CODE_RE = re.compile(r"\b(\d{3})\b")


def normalize_status(value) -> str:
    """把 status 归一化为裸 3 位状态码。

    写方（http_request.py:417）当前写的是裸码；但 07-28 → 08-29 期间产生的旧数据里，
    status 存的是整段响应头（'HTTP/1.1 200 OK\\nDate: ... \\n'）。
    未归一化时，每个 status 字符串因 Date 头而唯一 →
    _is_diff_group 的「状态码 ≥2 种」恒真 → [DIFFS] 退化成"重复请求检测器"。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit() and len(text) == 3:
        return text
    first_line = text.splitlines()[0]
    match = _STATUS_CODE_RE.search(first_line)
    return match.group(1) if match else text


def _is_diff_group(group: dict) -> bool:
    entries = group["entries"]
    if len(entries) < 2:
        return False
    statuses = {e["status"] for e in entries} - SKIP_STATUSES
    if len(statuses) >= 2:
        return True
    lengths = [e["length"] for e in entries if e["length"] > 0]
    if len(lengths) >= 2:
        hi, lo = max(lengths), min(lengths)
        if lo > 0 and hi / lo >= LENGTH_RATIO_THRESHOLD:
            return True
    return False


def _result_maps(results_root: str) -> dict:
    """扫描全部 target responses，返回 {target: {n: meta}}。确定性的目录/编号序。"""
    targets: dict[str, dict[str, dict]] = {}
    if not os.path.isdir(results_root):
        return targets
    for name in sorted(os.listdir(results_root)):
        responses = os.path.join(results_root, name, "responses")
        if not os.path.isdir(responses):
            continue
        metas: dict[str, dict] = {}
        for fname in os.listdir(responses):
            m = re.fullmatch(r"req_(\d+)\.json", fname)
            if not m:
                continue
            meta = _load_meta(os.path.join(responses, fname))
            if not meta or not meta.get("url"):
                continue
            metas[m.group(1)] = meta
        if metas:
            targets[name] = metas
    return targets


def _group_metas(metas: dict[str, dict]) -> tuple[list, dict]:
    """把个体请求聚为 (key → group)，返回 (有序 group 列表, 各 target 最大 req 号)。
    组内条目按 req 编号升序。"""
    groups: dict[tuple, dict] = {}
    max_req = 0
    for req_num in sorted(metas, key=lambda n: int(n)):
        meta = metas[req_num]
        key = _group_key(meta)
        if key is None:
            continue
        if key not in groups:
            groups[key] = {"method": key[0], "url": key[1], "entries": []}
        try:
            length = int(meta.get("length") or 0)
        except ValueError:
            length = 0
        groups[key]["entries"].append({
            "req": int(req_num),
            "status": normalize_status(meta.get("status")),
            "length": length,
        })
        max_req = max(max_req, int(req_num))
    # 保持首次出现顺序（排序后的 req 序）
    ordered = [g for g in groups.values() if _is_diff_group(g)]
    return ordered, max_req


def scan_diffs(results_root: str, target: str | None = None) -> list[dict]:
    """返回候选差分组（最多 DIFF_MAX_GROUPS 条）。target 为空扫全部。"""
    if target is not None:
        # 单 target 短路：只扫该目标目录，不扫全量（results/ 可能有数百目标）
        responses = target_responses_dir(results_root, target)
        metas: dict[str, dict] = {}
        if os.path.isdir(responses):
            for fname in os.listdir(responses):
                m = re.fullmatch(r"req_(\d+)\.json", fname)
                if not m:
                    continue
                meta = _load_meta(os.path.join(responses, fname))
                if not meta or not meta.get("url"):
                    continue
                metas[m.group(1)] = meta
        groups, _ = _group_metas(metas)
        groups.sort(key=lambda g: max(e["req"] for e in g["entries"]))
        return groups[:DIFF_MAX_GROUPS]

    all_metas = _result_maps(results_root)
    diff_groups: list[dict] = []
    for name, metas in all_metas.items():
        if target is not None and name != safe_target(target):
            continue
        groups, _ = _group_metas(metas)
        diff_groups.extend(groups)
    diff_groups.sort(key=lambda g: max(e["req"] for e in g["entries"]))
    return diff_groups[:DIFF_MAX_GROUPS]


def format_diffs(diff_groups: list[dict]) -> str:
    """可读输出段。"""
    if not diff_groups:
        return "[DIFFS]\n  (none)\n"
    lines = ["[DIFFS]"]
    for g in diff_groups:
        entries = g["entries"]
        req_nums = [e["req"] for e in entries]
        lengths = [e["length"] for e in entries if e["length"] > 0]
        # 每种状态给一个代表编号：可直接跳到该行为的一个实例，比枚举全量编号有用
        first_by_status: dict[str, int] = {}
        for e in entries:
            first_by_status.setdefault(e["status"], e["req"])
        statuses = ",".join(
            "%s(req_%d)" % (status, first_by_status[status])
            for status in sorted(first_by_status)
        )
        # 计数 + 范围，而不是全量枚举：组内条目数无上限（实测单组 14,778 条）
        req_range = (
            "req_%d..req_%d" % (min(req_nums), max(req_nums)) if req_nums else "-"
        )
        length_range = (
            "%d..%d" % (min(lengths), max(lengths)) if lengths else "0"
        )
        lines.append(f"  {g['method']} {g['url']}")
        lines.append(
            f"    reqs: {len(entries)} ({req_range}) "
            f"| statuses: {statuses} | lengths: {length_range}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
    target = sys.argv[1] if len(sys.argv) > 1 else None
    diffs = scan_diffs(root, target)
    print(format_diffs(diffs))
    return 0


if __name__ == "__main__":
    sys.exit(main())