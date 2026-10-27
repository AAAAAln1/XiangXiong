#!/usr/bin/env python3
"""scope.py — 授权范围机械闸（内网演练边界）。

设计（2026-10-06 P0-c，LLM 体验优先）：
- scope.json 是**授权声明文件**（不是世界认知，不进账本）：允许/排除的网段与主机；
- http_request 第一层（URL/host 结构化判定，越界 REJECT，不发包）；
- execute 第二层（命令串 + 引用脚本内的 IP/CIDR 字面量 best-effort 静态检查）；
- 纯机械：ipaddress 包含判定 + 严格正则字面量提取，零业务语义、零 DNS 信任。

诚实边界：拦不住运行时动态目标（脚本运行时读出的 IP、DNS 反弹、工具内部外联）。
闸是 defense-in-depth；授权合法性最终责任在合同层与使用者。

用法：
  python runtime/scope.py init <engagement> --allow 10.0.0.0/24,10.0.1.2/32 [--deny 10.0.0.1]
  python runtime/scope.py check <target> --ip 10.0.0.5 [--port 445]
  python runtime/scope.py position <target> --at "10.0.0.5 via SSH" --art art-001
  python runtime/scope.py exists <target>
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone

try:
    from .paths import project_root, safe_name
except ImportError:
    from paths import project_root, safe_name

RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(project_root(), "results")
SCOPE_NAME = "scope.json"

MAX_ENTRIES = 16
MAX_ENTRY_CHARS = 49

# 环回/链路本地/组播/本网络：机械放行（工具本地通信）
_ALWAYS_ALLOW_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("0.0.0.0/8"),
]

# 严格 IPv4 字面量（四段 0-255），避免版本号误伤
STRICT_IPV4_RE = re.compile(
    r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?:/\d{1,2})?(?![\d])"
)
# 私网判定的网段
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def scope_path(target: str) -> str:
    return os.path.join(RESULTS_DIR, safe_name(target), SCOPE_NAME)


def has_scope(target: str) -> bool:
    return os.path.isfile(scope_path(target))


def creds_count(target: str) -> int:
    """该 target 凭据库条数（投影 [SCOPE] 用；不读值）。"""
    p = os.path.join(RESULTS_DIR, safe_name(target), "credentials", "index.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return len(data) if isinstance(data, dict) else 0
    except (OSError, json.JSONDecodeError):
        return 0


def load_scope(target: str) -> dict | None:
    p = scope_path(target)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


# ── 字面量提取（execute 第二层用；纯机械）────────────────────────────────────

def extract_ip_literals(text: str) -> list:
    """从任意文本提取严格 IPv4 字面量（可带 /prefix）。只提取，不判语义。"""
    return STRICT_IPV4_RE.findall(text or "")


def is_private_literal(token: str) -> bool:
    """token 可以是 IP 或 CIDR 字面量；任一地址在私网即 True。非法形态 False。"""
    try:
        if "/" in token:
            net = ipaddress.ip_network(token, strict=False)
            return net.version == 4 and any(net.version == n.version and net.subnet_of(n) for n in _PRIVATE_NETS)
        ip = ipaddress.ip_address(token)
        return any(ip in n for n in _PRIVATE_NETS)
    except ValueError:
        return False


def _parse_entry(entry: str):
    """单条 allow/deny → (network|None, hostname|None)。"""
    entry = entry.strip()
    if not entry:
        return None
    # IP/CIDR
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?", entry):
        return ipaddress.ip_network(entry, strict=False) if "/" in entry \
            else ipaddress.ip_network(entry + "/32", strict=False)
    # 主机名
    if re.fullmatch(r"[A-Za-z0-9_.\-]+", entry) and any(c.isalpha() for c in entry):
        return ("host", entry.lower())
    raise ValueError(f"无法识别的 scope 条目（需 IP/CIDR/主机名）: {entry}")


def _validate_entries(entries: list) -> list:
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"scope 条目最多 {MAX_ENTRIES} 条（超出请拆 engagement）")
    parsed = []
    for e in entries:
        if len(e) > MAX_ENTRY_CHARS:
            raise ValueError(f"scope 条目过长（>{MAX_ENTRY_CHARS} 字符）: {e[:30]}")
        parsed.append(_parse_entry(e))
    return parsed


# ── 核心判定 ─────────────────────────────────────────────────────────────────

def _always_allowed(ip) -> bool:
    return any(ip in n for n in _ALWAYS_ALLOW_NETS)


def evaluate(target: str, ip_str: str) -> dict:
    """对一个具体 IPv4 做授权判定。

    返回 {decision: allow|deny|out_of_scope|no_scope|local, matched: str}
    - deny 优先于 allow；
    - 环回/链路本地等 → local（机械放行）；
    - 无 scope 文件：私网 → no_scope（调用方应 REJECT），公网 → 调用方维持原路径（本函数返回 no_scope）。
    """
    ip = ipaddress.ip_address(ip_str)
    if _always_allowed(ip):
        return {"decision": "local", "matched": "loopback/linklocal"}
    scope = load_scope(target)
    if scope is None:
        return {"decision": "no_scope", "matched": ""}

    for raw in scope.get("deny", []):
        parsed = _parse_entry(raw)
        if isinstance(parsed, ipaddress._BaseNetwork) and ip in parsed:
            return {"decision": "deny", "matched": raw}
        if isinstance(parsed, tuple):
            # 主机名条目不参与 IP 判定（DNS 路径单独处理）
            continue
    for raw in scope.get("allow", []):
        parsed = _parse_entry(raw)
        if isinstance(parsed, ipaddress._BaseNetwork) and ip in parsed:
            return {"decision": "allow", "matched": raw}
    return {"decision": "out_of_scope", "matched": ""}


def evaluate_hostname(target: str, host: str, resolved_ips: list | None = None) -> dict:
    """主机名判定：精确匹配 allow 主机名才放行；解析出私网 IP 时按 IP 规则复核。"""
    host = host.strip().lower().rstrip(".")
    scope = load_scope(target)
    if scope is None:
        return {"decision": "no_scope", "matched": ""}
    allow_hosts = {e.lower() for e in scope.get("allow", [])
                   if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?", e.strip())}
    deny_hosts = {e.lower() for e in scope.get("deny", [])
                  if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?", e.strip())}
    if host in deny_hosts:
        return {"decision": "deny", "matched": host}
    if host in allow_hosts:
        return {"decision": "allow", "matched": host}
    # 主机名未声明：若解析到私网地址，全部需在 allow 网段内（任一命中 deny 即拒）
    if resolved_ips:
        decisions = [evaluate(target, ip)["decision"] for ip in resolved_ips]
        if any(d == "deny" for d in decisions):
            return {"decision": "deny", "matched": "resolved"}
        if decisions and all(d in ("allow", "local") for d in decisions):
            return {"decision": "allow", "matched": "resolved"}
        return {"decision": "out_of_scope", "matched": ""}
    return {"decision": "out_of_scope", "matched": ""}


def resolve_hostname_ips(host: str) -> list:
    """DNS 解析为 IPv4 列表（失败返回空，调用方不替 curl 决策）。"""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
        return sorted({i[4][0] for i in infos})
    except OSError:
        return []


def reject_message(target: str, token: str, decision: dict) -> str:
    scope = load_scope(target) or {}
    allow = ",".join(scope.get("allow", [])) or "（无）"
    deny = ",".join(scope.get("deny", [])) or "（无）"
    return (
        f"[SCOPE GATE] REJECT: {token} {decision['decision']}"
        f"（{'命中 ' + decision['matched'] if decision['matched'] else '不在授权范围'}；未执行任何动作）。\n"
        f"  授权: {allow}   排除: {deny}\n"
        f"  若该目标确已授权：python runtime/scope.py init <engagement> --allow <CIDR...>\n"
        f"  若非授权：记 hypothesis [blocked]: 需要 {token} 授权，然后换面。"
    )


# ── CLI 写操作 ───────────────────────────────────────────────────────────────

def init_scope(target: str, allow: list, deny: list) -> dict:
    allow_p = _validate_entries(allow)
    deny_p = _validate_entries(deny)
    os.makedirs(os.path.dirname(scope_path(target)), exist_ok=True)
    payload = {
        "engagement": target,
        "allow": allow,
        "deny": deny,
        "declared_at": now_iso(),
        "position": {"at": "本机", "via": None, "since": None, "art": None},
        "position_history": [],
    }
    tmp = scope_path(target) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, scope_path(target))
    return payload


def update_position(target: str, at: str, art: str | None) -> dict:
    scope = load_scope(target)
    if scope is None:
        raise FileNotFoundError(f"{target} 无 scope.json；先 init")
    old = scope.get("position") or {}
    history = scope.get("position_history") or []
    if old.get("at"):
        history.append(old)
    scope["position"] = {"at": at, "via": None, "since": now_iso(), "art": art}
    scope["position_history"] = history[-20:]
    tmp = scope_path(target) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(scope, f, ensure_ascii=False, indent=2)
    os.replace(tmp, scope_path(target))
    return scope["position"]


def _split_csv(value: str) -> list:
    return [v.strip() for v in re.split(r"[,，;；]", value) if v.strip()]


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="授权范围机械闸")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init")
    pi.add_argument("target")
    pi.add_argument("--allow", required=True)
    pi.add_argument("--deny", default="")

    pc = sub.add_parser("check")
    pc.add_argument("target")
    pc.add_argument("--ip")
    pc.add_argument("--host")

    pp = sub.add_parser("position")
    pp.add_argument("target")
    pp.add_argument("--at", required=True)
    pp.add_argument("--art")

    pe = sub.add_parser("exists")
    pe.add_argument("target")

    args = p.parse_args()

    if args.cmd == "init":
        try:
            init_scope(args.target, _split_csv(args.allow), _split_csv(args.deny))
        except ValueError as e:
            print(f"[scope] FAILED: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"[scope] 已写入 {scope_path(args.target)}")
    elif args.cmd == "check":
        if args.ip:
            print(json.dumps(evaluate(args.target, args.ip), ensure_ascii=False))
        elif args.host:
            ips = resolve_hostname_ips(args.host)
            print(json.dumps(evaluate_hostname(args.target, args.host, ips), ensure_ascii=False))
    elif args.cmd == "position":
        try:
            pos = update_position(args.target, args.at, args.art)
            print(f"[scope] 立足点更新：{pos['at']} ({pos['since'][:16]})")
        except FileNotFoundError as e:
            print(f"[scope] FAILED: {e}", file=sys.stderr)
            sys.exit(2)
    elif args.cmd == "exists":
        sys.exit(0 if has_scope(args.target) else 1)


if __name__ == "__main__":
    main()
