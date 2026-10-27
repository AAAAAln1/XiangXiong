#!/usr/bin/env python3
"""evidence.py — 请求证据台账（只读视图，零判断、不发包、不写世界状态）。

## 解决什么

日志实测：LLM 手写 `python -c` 解析 responses/ 332 次、head/sed 窗口化 671 次、
Grep 直搜 results/ 310 次——它在手工重建「我打过哪些请求、各是什么状态/多长/什么字段」。
本工具把那个视图变成四条子命令：

    inventory <target>                         全量滚动台账（状态/方法/体长/端点覆盖）
    list <target> [过滤]                       带过滤的请求清单
    show <target> req_NNN [--json-path/--grep/--bytes]  单条证据的结构化读取
    grep <target> <regex> [--field body|url] [--status]  跨证据搜索

## 边界（与 protocol_scan 同层）

- 只读 responses/req_NNN.{json,txt}；**不发包、不写 memory、不登记 art**
  （查询结果是视图不是证据，登记会污染证据体系，引用 req 原件即可回放）；
- 不解释成功码语义、不判漏洞、不推荐下一步。

## 历史脏数据：只在读侧容错，永不反写 sidecar（旧记录代表历史）

- "000401"（000+3 位码拼接遗物）显示为 401(原000401)；真 "000" 保持 000；
- {} 占位/损坏 JSON 跳过聚合，仅在 inventory 计数；
- 一律以**目录 target** 为准，不读 meta.target（历史里有 target="--experiment-id"）。

大输出（>12KB）人类视图自动落 results/<target>/tmp/queries/ 并头尾回显；--json 不截断文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    from .diff_scan import normalize_status
    from .execute import trim_output
    from .paths import project_root, safe_name
    from .protocol_scan import load_items as _load_items, _endpoint_key
except ImportError:  # tests/ 以 runtime/ 为 sys.path
    from diff_scan import normalize_status
    from execute import trim_output
    from paths import project_root, safe_name
    from protocol_scan import load_items as _load_items, _endpoint_key

PROJECT_ROOT = project_root()
RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(PROJECT_ROOT, "results")
QUERIES_SUBDIR = os.path.join("tmp", "queries")
SHOW_BODY_CHARS = 2048
EMIT_LIMIT = 12_000          # 人类视图终端上限，超出落盘
JSON_HARD_LIMIT = 1_000_000  # --json 输出截断阈值
LIST_DEFAULT_LIMIT = 50
GREP_DEFAULT_LIMIT = 100
URL_DISPLAY = 72
TITLE_DISPLAY = 40
CTYPE_DISPLAY = 28
RED_LINE = "机械台账，不是结论；未差分候选需变异差分后才许收口。"


# ── 路径与装载 ──────────────────────────────────────────────────────────────

def responses_dir(target: str) -> str:
    return os.path.join(RESULTS_DIR, safe_name(target), "responses")


def sidecar_paths(rdir: str):
    """返回 {req_num: json_path}（只看文件名，不解析内容）。"""
    out = {}
    if not os.path.isdir(rdir):
        return out
    for name in os.listdir(rdir):
        m = re.fullmatch(r"req_(\d+)\.json", name)
        if m:
            out[int(m.group(1))] = os.path.join(rdir, name)
    return out


def load(target: str):
    """返回 (items, sidecar_count)。

    items = [(req_num, meta, body), ...]（装载复用 protocol_scan 单一实现）；
    sidecar_count = req_*.json 文件总数（含占位/损坏）。
    """
    rdir = responses_dir(target)
    count = len(sidecar_paths(rdir))
    return _load_items(RESULTS_DIR, target), count


# ── 脏数据容错（只影响显示）────────────────────────────────────────────────

def clean_status(raw) -> str:
    """归一化状态码（显示用）。先过 diff_scan 单一实现，再补 000+3位码 遗物。

    "000401" → "401"；"000" → "000"；正常 3 位码原样；无法解析 → "?"。
    """
    text = str(raw or "").strip()
    normalized = normalize_status(raw)
    if re.fullmatch(r"000([1-5]\d{2})", text):
        return text[-3:]
    return normalized or "?"


def status_pair(raw) -> tuple:
    """返回 (显示码, 原码或None)：与标准归一不同形时附带原码。"""
    text = str(raw or "").strip()
    cleaned = clean_status(raw)
    if re.fullmatch(r"000[1-5]\d{2}", text):
        return cleaned, text
    if cleaned != text and text:
        return cleaned, text
    return cleaned, None


def _to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


# ── 端点聚合（键复用 protocol_scan._endpoint_key；不封项，不写第二份键规则）──

def aggregate_endpoints_full(items: list) -> list:
    """全量端点聚合（protocol_scan 的版本 capped 20；台账给全量）。"""
    groups = {}
    for req_num, meta, _ in items:
        key = _endpoint_key(meta.get("method") or "GET", meta.get("url") or "")
        if key is None:
            continue
        g = groups.setdefault(key, {"url": key, "reqs": 0,
                                    "statuses": Counter(), "single": True})
        g["reqs"] += 1
        g["statuses"][clean_status(meta.get("status"))] += 1
    rows = sorted(groups.values(), key=lambda g: (-g["reqs"], g["url"]))
    return rows


def _median(values: list):
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) // 2


# ── inventory ────────────────────────────────────────────────────────────────

def render_inventory(target: str) -> str:
    rdir = responses_dir(target)
    if not os.path.isdir(rdir):
        return ""  # 调用方按 exit 2 处理
    items, sidecar_count = load(target)
    statuses = Counter()
    methods = Counter()
    lengths = []
    for _, meta, _ in items:
        statuses[clean_status(meta.get("status"))] += 1
        methods[(meta.get("method") or "?").upper()] += 1
        lengths.append(_to_int(meta.get("length")))

    endpoints = aggregate_endpoints_full(items)
    single = [g for g in endpoints if g["reqs"] == 1]
    req_nums = [n for n, _, _ in items]
    max_num = max(sidecar_paths(rdir).keys(), default=0)
    holes = max(0, max_num - sidecar_count)
    corrupt = sidecar_count - len(items)

    lines = [
        "[INVENTORY]（机械聚合，零判断，不发包）" + f" target={target}",
        f"scanned: {len(items)} responses（req_{min(req_nums):03d}..req_{max_num:03d}"
        + (f"，含编号空洞 {holes}" if holes else "")
        + "）",
    ]
    if corrupt > 0:
        lines.append(f"不可解析 sidecar: {corrupt}（占位/损坏，旧记录代表历史）")
    lines.append("状态分布: " + " ".join(f"{s}×{n}" for s, n in sorted(statuses.items())))
    lines.append("方法分布: " + " ".join(f"{m}×{n}" for m, n in sorted(methods.items())))
    if lengths:
        lines.append(f"体长(bytes): min {min(lengths)} / 中位 {_median(lengths)} / max {max(lengths)}")
    lines.append(f"端点（method + 前3段路径，×次数 {{状态集合}}）共 {len(endpoints)} 个:")
    for g in endpoints:
        st = " ".join(f"{s}×{n}" for s, n in sorted(g["statuses"].items()))
        lines.append(f"  {g['url']}  ×{g['reqs']} {{{st}}}")
    lines.append(
        f"覆盖: {len(endpoints) - len(single)} 个多次访问路径 · {len(single)} 个仅请求 1 次"
        "（未差分候选——打过分不等于测过）"
    )
    lines.append(RED_LINE)
    return "\n".join(lines) + "\n"


# ── list ─────────────────────────────────────────────────────────────────────

def _row(req_num: int, meta: dict) -> str:
    display, original = status_pair(meta.get("status"))
    status = display if not original else f"{display}#{original}"
    ctype = (meta.get("content_type") or "").split(";")[0].strip()[:CTYPE_DISPLAY]
    title = (meta.get("title") or "").replace("\n", " ")[:TITLE_DISPLAY]
    url = (meta.get("url") or "")[:URL_DISPLAY]
    return (f"req_{req_num:03d}  {(meta.get('method') or '?'):6s} {status:14s} "
            f"{_to_int(meta.get('length')):>8d}  {ctype:<{CTYPE_DISPLAY}} {title:<{TITLE_DISPLAY}} {url}")


def _filter_items(items, *, exp=None, statuses=None, method=None, url_re=None, since=None):
    out = []
    for req_num, meta, _ in items:
        if exp and meta.get("experiment_id") != exp:
            continue
        if statuses and clean_status(meta.get("status")) not in statuses:
            continue
        if method and (meta.get("method") or "").upper() != method.upper():
            continue
        if url_re and not url_re.search(meta.get("url") or ""):
            continue
        if since is not None and req_num < since:
            continue
        out.append((req_num, meta))
    return out


LIST_HEADER = (f"{'id':9s}  {'method':6s} {'status':14s} {'len':>8s}  {'ctype':28s} "
               f"{'title':40s} url")


def render_list(target, *, exp=None, status_filter=None, method=None, url_pattern=None,
                since=None, limit=LIST_DEFAULT_LIMIT):
    if not os.path.isdir(responses_dir(target)):
        return ""
    items, _ = load(target)
    statuses = {s.strip() for s in status_filter.split(",") if s.strip()} if status_filter else None
    url_re = re.compile(url_pattern) if url_pattern else None
    since_num = None
    if since:
        m = re.fullmatch(r"req_(\d+)", since.strip())
        if not m:
            raise ValueError("--since 只接受 req_NNN 形态")
        since_num = int(m.group(1))
    rows = _filter_items(items, exp=exp, statuses=statuses, method=method,
                         url_re=url_re, since=since_num)
    rows.sort(key=lambda x: x[0], reverse=True)
    truncated = False
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
        truncated = True
    lines = [LIST_HEADER]
    lines.extend(_row(n, m) for n, m in rows)
    if not rows:
        lines.append("[list] 0 条命中（过滤是机械匹配，不代表这些请求不存在）")
    elif truncated:
        lines.append(f"[list] 仅显示最近 {limit} 条；全量加 --all 或 --json")
    return "\n".join(lines) + "\n"


def render_list_json(target, **filters):
    if not os.path.isdir(responses_dir(target)):
        return None
    items, _ = load(target)
    statuses = ({s.strip() for s in filters["status_filter"].split(",") if s.strip()}
                if filters.get("status_filter") else None)
    url_re = re.compile(filters["url_pattern"]) if filters.get("url_pattern") else None
    since_num = None
    if filters.get("since"):
        since_num = int(filters["since"].split("_")[1])
    rows = _filter_items(
        items, exp=filters.get("exp"), statuses=statuses, method=filters.get("method"),
        url_re=url_re, since=since_num)
    rows.sort(key=lambda x: x[0])
    return [{
        "req": f"req_{n:03d}", "method": m.get("method"), "status": clean_status(m.get("status")),
        "length": _to_int(m.get("length")), "content_type": m.get("content_type"),
        "title": m.get("title"), "url": m.get("url"), "experiment_id": m.get("experiment_id"),
    } for n, m in rows]


# ── show ─────────────────────────────────────────────────────────────────────

def _json_path(value, path: str):
    """点路径取值：a.b.0.c（只支持键名与十进制下标，不支持通配/过滤）。"""
    cur = value
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(f"键 {part!r} 不存在")
            cur = cur[part]
        elif isinstance(cur, list):
            if not part.isdigit():
                raise KeyError(f"当前是数组，需要十进制下标，收到 {part!r}")
            idx = int(part)
            if not 0 <= idx < len(cur):
                raise KeyError(f"下标 {idx} 越界（长度 {len(cur)}）")
            cur = cur[idx]
        else:
            raise KeyError(f"{part!r} 无法继续取值（当前是标量）")
    return cur


_BYTES_RE = re.compile(r"^(?:(\d+):(\d*)|:(\d+))$")


def _char_slice(text: str, spec: str) -> str:
    m = _BYTES_RE.fullmatch(spec.strip())
    if not m:
        raise ValueError("--bytes 形态：A:B / A: / :B（字符索引）")
    n = len(text)
    if m.group(3) is not None:  # :B 前缀
        return text[:min(int(m.group(3)), n)]
    a = int(m.group(1))         # A: 或 A:B
    b = n if m.group(2) == "" else int(m.group(2))
    return text[min(a, n):min(b, n)]


def render_show(target, req_id, *, json_path=None, grep_pattern=None, byte_range=None):
    m = re.fullmatch(r"req_(\d+)", req_id.strip())
    if not m:
        raise ValueError("证据 id 必须是 req_NNN 形态")
    num = int(m.group(1))
    rdir = responses_dir(target)
    meta_path = os.path.join(rdir, f"req_{num:03d}.json")
    body_path = os.path.join(rdir, f"req_{num:03d}.txt")
    if not os.path.exists(meta_path):
        raise FileNotFoundError(f"{req_id} 不存在于 {target}")
    with open(meta_path, encoding="utf-8-sig") as handle:
        meta = json.load(handle)
    try:
        with open(body_path, encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    except OSError:
        body = ""

    display, original = status_pair(meta.get("status"))
    head = [
        f"[SHOW] {req_id}（机械读取，不解释含义）",
        f"method: {meta.get('method')}  url: {meta.get('url')}",
        f"status: {display}" + (f"（原{original}）" if original else ""),
        f"length: {_to_int(meta.get('length'))}  duration: {meta.get('duration')}s",
        f"content_type: {meta.get('content_type') or '(空)'}  title: {meta.get('title') or '(空)'}",
        f"experiment: {meta.get('experiment_id') or '(无)'}  saved: responses/{req_id}.txt",
    ]

    text = body
    if json_path:
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            raise ValueError("body 不是合法 JSON，--json-path 不可用（去掉该参数读原文）")
        value = _json_path(parsed, json_path)
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            text = "" if value is None else str(value)
        head.append(f"json_path: {json_path}")
    if byte_range:
        text = _char_slice(text, byte_range)
        head.append(f"bytes: {byte_range}（字符索引，实际 {len(text)} 字符）")
    if grep_pattern:
        rx = re.compile(grep_pattern)
        hits = []
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{i}: {line[:200]}")
            if len(hits) >= 40:
                hits.append("…（匹配超过 40 行，仅显示前 40；要全文去掉 --grep）")
                break
        head.append(f"grep: {grep_pattern}（{len([h for h in hits if not h.startswith('…')])} 行命中）")
        return "\n".join(head + [""] + hits) + "\n"
    return "\n".join(head) + "\n\n" + trim_output(text, SHOW_BODY_CHARS * 2,
                                                  artifact_path=f"responses/{req_id}.txt") + "\n"


# ── grep ─────────────────────────────────────────────────────────────────────

def render_grep(target, pattern, *, field="body", status_filter=None, limit=GREP_DEFAULT_LIMIT):
    if not os.path.isdir(responses_dir(target)):
        return ""
    rx = re.compile(pattern)  # 坏正则由调用方捕
    statuses = {s.strip() for s in status_filter.split(",") if s.strip()} if status_filter else None
    items, _ = load(target)
    hits = []
    for num, meta, body in items:
        if statuses and clean_status(meta.get("status")) not in statuses:
            continue
        if field == "url":
            if rx.search(meta.get("url") or ""):
                hits.append(f"req_{num:03d}: {meta.get('url')}")
        else:
            for i, line in enumerate(body.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"req_{num:03d}:{i}: {line[:200]}")
                    if len(hits) >= limit:
                        break
        if len(hits) >= limit:
            break
    lines = [f"[grep] field={field} pattern={pattern!r}（上限 {limit}）"]
    lines.extend(hits)
    if not hits:
        lines.append("0 条命中（机械匹配，不代表内容不存在）")
    elif len(hits) >= limit:
        lines.append(f"…达到 {limit} 条上限；缩小过滤或用 --json 全量")
    return "\n".join(lines) + "\n"


# ── 大输出落盘（reduction 约定）────────────────────────────────────────────

def _spill_if_large(target: str, cmd: str, text: str) -> str:
    if len(text) <= EMIT_LIMIT:
        return text
    directory = os.path.join(RESULTS_DIR, safe_name(target), *QUERIES_SUBDIR.split("/"))
    os.makedirs(directory, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = os.path.join(directory, f"{ts}_{cmd}.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    rel = os.path.relpath(path, os.path.join(RESULTS_DIR, safe_name(target))).replace("\\", "/")
    head = text[:2000]
    tail = text[-2000:]
    return (f"[SPILL] 输出 {len(text)} 字符超 {EMIT_LIMIT}，已落盘：{rel}\n"
            f"{head}\n...[中段省略；全文见落盘文件]...\n{tail}")


# ── CLI ─────────────────────────────────────────────────────────────────────

HELP = """\
evidence.py — 请求证据台账（只读、不发包、不写账本）

  inventory <target>
      全量台账：状态/方法/体长分布、端点覆盖、仅1次请求的未差分候选
  list <target> [--exp exp-NNN] [--status 200,405] [--method POST]
       [--url~ REGEX] [--since req_500] [--limit N | --all] [--json]
      请求清单（默认最近 50 条倒序）
  show <target> req_NNN [--json-path A.B.0] [--grep REGEX] [--bytes A:B]
      单条证据结构化读取（默认 meta + body 头尾；超长全文路径随回显）
  grep <target> REGEX [--field body|url] [--status 200] [--limit N]
      跨证据搜索（默认 body，上限 100 条）

退出码：0 正常；2 参数/目标错误。
"""


def _fail(message: str) -> int:
    print(f"[evidence] {message}\n{HELP}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "inventory":
            p = argparse.ArgumentParser()
            p.add_argument("target")
            a = p.parse_args(rest)
            out = render_inventory(a.target)
            if not out:
                return _fail(f"{a.target} 没有 responses/（先有请求才有台账）")
            print(_spill_if_large(a.target, "inventory", out))
            return 0

        if cmd == "list":
            p = argparse.ArgumentParser()
            p.add_argument("target")
            p.add_argument("--exp")
            p.add_argument("--status", dest="status_filter")
            p.add_argument("--method")
            p.add_argument("--url~", dest="url_pattern")
            p.add_argument("--since")
            grp = p.add_mutually_exclusive_group()
            grp.add_argument("--limit", type=int, default=LIST_DEFAULT_LIMIT)
            grp.add_argument("--all", action="store_true")
            p.add_argument("--json", action="store_true")
            a = p.parse_args(rest)
            if a.json:
                payload = render_list_json(
                    a.target, exp=a.exp, status_filter=a.status_filter, method=a.method,
                    url_pattern=a.url_pattern, since=a.since)
                if payload is None:
                    return _fail(f"{a.target} 没有 responses/")
                blob = json.dumps(payload, ensure_ascii=False, indent=2)
                if len(blob) > JSON_HARD_LIMIT:
                    directory = os.path.join(RESULTS_DIR, safe_name(a.target),
                                             *QUERIES_SUBDIR.split("/"))
                    os.makedirs(directory, exist_ok=True)
                    path = os.path.join(directory, "list_latest.json")
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(blob)
                    print(json.dumps({"truncated": True, "file": path,
                                      "rows_returned": len(payload)}, ensure_ascii=False))
                else:
                    print(blob)
                return 0
            out = render_list(
                a.target, exp=a.exp, status_filter=a.status_filter, method=a.method,
                url_pattern=a.url_pattern, since=a.since,
                limit=None if a.all else a.limit)
            if not out:
                return _fail(f"{a.target} 没有 responses/")
            print(_spill_if_large(a.target, "list", out))
            return 0

        if cmd == "show":
            p = argparse.ArgumentParser()
            p.add_argument("target")
            p.add_argument("req_id")
            p.add_argument("--json-path", dest="json_path")
            p.add_argument("--grep", dest="grep_pattern")
            p.add_argument("--bytes", dest="byte_range")
            a = p.parse_args(rest)
            print(render_show(a.target, a.req_id, json_path=a.json_path,
                              grep_pattern=a.grep_pattern, byte_range=a.byte_range))
            return 0

        if cmd == "grep":
            p = argparse.ArgumentParser()
            p.add_argument("target")
            p.add_argument("pattern")
            p.add_argument("--field", choices=("body", "url"), default="body")
            p.add_argument("--status", dest="status_filter")
            p.add_argument("--limit", type=int, default=GREP_DEFAULT_LIMIT)
            a = p.parse_args(rest)
            out = render_grep(a.target, a.pattern, field=a.field,
                              status_filter=a.status_filter, limit=a.limit)
            if not out:
                return _fail(f"{a.target} 没有 responses/")
            print(_spill_if_large(a.target, "grep", out))
            return 0

        return _fail(f"未知子命令: {cmd}")
    except re.error as exc:
        return _fail(f"正则编译失败: {exc}")
    except (ValueError, FileNotFoundError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
