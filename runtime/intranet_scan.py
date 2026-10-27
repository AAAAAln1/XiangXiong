#!/usr/bin/env python3
"""intranet_scan.py — 内网协议产物聚合（零判断，对齐 protocol_scan 定位）。

输入：results/<target>/artifacts/index.json 中 kind=port_scan/host_alive/service_enum
      的落盘物（execute 登记时从命令的 -oX/-oN 产物机械提取，或 stdout 全文）。
输出（stdout，不写 memory）：
  [HOST MAP]   host → open ports → service/version（每主机带来源 art）
  [可抄行]     surface: host <ip> (...) ports open (art-NNN) —— LLM 审阅后自行 merge

产物 host_map 自身登记为 kind=host_map 的 artifact（sha256 锚定）；投影 [TERRAIN]
计数只认 index.json 登记的 host_map 并校验 sha256，防手改自由文件污染地形。

零攻击知识：不评级、不推荐下一步、不判「这台是域控」。nmap XML 之外的解析格式
（nxc/bloodhound）按实战真实遇到的格式逐个加，不预设计。

用法：
  python runtime/intranet_scan.py <target>          # 打印 [HOST MAP] + 可抄行
  python runtime/intranet_scan.py <target> --json   # 机器可读
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

try:
    from .paths import project_root, safe_name
except ImportError:
    from paths import project_root, safe_name

PROJECT_ROOT = project_root()
RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(PROJECT_ROOT, "results")
MAP_SUBDIR = "hostmaps"

SCAN_KINDS = ("port_scan", "host_alive", "service_enum")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def artifacts_dir(target: str) -> str:
    return os.path.join(RESULTS_DIR, safe_name(target), "artifacts")


def hostmaps_dir(target: str) -> str:
    d = os.path.join(RESULTS_DIR, safe_name(target), MAP_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _load_art_index(target: str) -> dict:
    p = os.path.join(artifacts_dir(target), "index.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _artifact_path(target: str, art_id: str, file_name: str) -> str:
    return os.path.join(artifacts_dir(target), file_name)


def parse_nmap_xml(text: str) -> dict:
    """nmap -oX XML → {ip: {ports: [(port, proto, service, version)], hostname: str}}。

    机械解析：只取结构化字段。解析失败由调用方降级到文本正则。
    """
    hosts = {}
    root = ET.fromstring(text)
    for host in root.iter("host"):
        addr_el = host.find("address[@addrtype='ipv4']")
        if addr_el is None:
            addr_el = host.find("address")
        if addr_el is None:
            continue
        ip = addr_el.get("addr", "")
        entry = {"ports": [], "hostname": ""}
        hn = host.find("hostnames/hostname")
        if hn is not None:
            entry["hostname"] = hn.get("name", "")
        for p in host.iter("port"):
            state_el = p.find("state")
            if state_el is None or state_el.get("state") != "open":
                continue
            svc = p.find("service")
            service = svc.get("name", "") if svc is not None else ""
            product = svc.get("product", "") if svc is not None else ""
            version = svc.get("version", "") if svc is not None else ""
            entry["ports"].append((
                p.get("portid", ""),
                p.get("protocol", "tcp"),
                service,
                " ".join(x for x in (product, version) if x),
            ))
        hosts[ip] = entry
    return hosts


# grepable/文本降级：行首 "Host: 10.0.0.1 ()\tStatus: Up" 与 "Host: 10.0.0.1 () Ports: 80/open/tcp//http//..."
_NMAP_HOST_LINE = re.compile(r"Host:\s*([0-9.]+).*?Ports:\s*(.+)")
_NMAP_PORT_FIELD = re.compile(
    r"(\d+)/open/(tcp|udp)/[^/]*/([^/]*)/[^/]*/([^/]*)/")


def parse_nmap_text(text: str) -> dict:
    hosts = {}
    for line in text.splitlines():
        m = _NMAP_HOST_LINE.search(line)
        if not m:
            continue
        ip, ports_blob = m.group(1), m.group(2)
        entry = hosts.setdefault(ip, {"ports": [], "hostname": ""})
        for pm in _NMAP_PORT_FIELD.finditer(ports_blob):
            entry["ports"].append((pm.group(1), pm.group(2), pm.group(3).strip(),
                                  pm.group(4).strip()))
    return hosts


def merge_host_maps(existing: dict, new: dict) -> dict:
    for ip, entry in new.items():
        cur = existing.setdefault(ip, {"ports": [], "hostname": ""})
        if entry.get("hostname") and not cur["hostname"]:
            cur["hostname"] = entry["hostname"]
        seen = {tuple(p[:2]) for p in cur["ports"]}
        for port in entry["ports"]:
            if tuple(port[:2]) not in seen:
                cur["ports"].append(port)
                seen.add(tuple(port[:2]))
    return existing


def build_host_map(target: str) -> tuple:
    """扫描 index 中 port_scan/host_alive/service_enum 产物 → 聚合 host_map。

    返回 (host_map, source_arts)。零产物 → ({}, [])。
    """
    index = _load_art_index(target)
    host_map = {}
    sources = []
    for art_id, meta in sorted(index.items()):
        if meta.get("kind") not in SCAN_KINDS:
            continue
        file_name = meta.get("path") or ""
        p = _artifact_path(target, art_id, file_name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        parsed = {}
        if "<?xml" in text[:200] or "<nmaprun" in text[:500]:
            try:
                parsed = parse_nmap_xml(text)
            except ET.ParseError:
                parsed = parse_nmap_text(text)
        else:
            parsed = parse_nmap_text(text)
        if parsed:
            merge_host_maps(host_map, parsed)
            sources.append(art_id)
    return host_map, sources


def latest_verified_host_map(target: str) -> dict | None:
    """投影用：取最新 kind=host_map 的 art，**校验 sha256 通过**才返回其 JSON。

    手改 hostmap 文件 → 哈希不符 → 返回 None（投影拒显数字，防伪造覆盖率锚点）。
    信任级别与 req_NNN 产物同级（防意外污染，不防恶意自欺）。
    """
    import hashlib
    index = _load_art_index(target)
    candidates = [(art_id, meta) for art_id, meta in index.items()
                  if meta.get("kind") == "host_map"]
    if not candidates:
        return None
    # register_artifact 写 created（ISO 字符串可排序）；无 created 退到 art 序号
    candidates.sort(key=lambda kv: kv[1].get("created", ""), reverse=True)
    for art_id, meta in candidates:
        file_name = meta.get("path") or ""
        p = os.path.join(hostmaps_dir(target), os.path.basename(file_name))
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            continue
        if digest != meta.get("sha256"):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
    return None


def register_host_map(target: str, host_map: dict, sources: list) -> str:
    """host_map 落盘并登记为 art-NNN（sha256 锚定，投影据此校验）。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    file_name = f"hostmap-{ts}.json"
    p = os.path.join(hostmaps_dir(target), file_name)
    payload = {
        "generated": now_iso(),
        "target": target,
        "sources": sources,
        "host_count": len(host_map),
        "hosts": host_map,
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    # 复用 execute 的登记（art index 单一写者，含锁）
    try:
        from .execute import register_artifact
    except ImportError:
        from execute import register_artifact
    return register_artifact(target, p, source_cmd="intranet_scan", kind="host_map")


def format_output(target: str, host_map: dict, sources: list, art_id: str) -> str:
    lines = [f"[HOST MAP] {target} · {len(host_map)} 主机 · 来源 {', '.join(sources) or '无'} · {art_id}"]
    for ip in sorted(host_map):
        e = host_map[ip]
        label = f" ({e['hostname']})" if e.get("hostname") else ""
        ports = ", ".join(
            f"{port}/{proto}" + (f" {svc}" if svc else "")
            + (f" {ver}" if ver else "")
            for port, proto, svc, ver in sorted(e["ports"], key=lambda x: int(x[0]) if x[0].isdigit() else 0)
        ) or "无开放端口记录"
        lines.append(f"  {ip}{label}: {ports}")
    lines.append("")
    lines.append("[可抄行]（审阅后自行 merge；不自动入账）")
    for ip in sorted(host_map):
        e = host_map[ip]
        if not e["ports"]:
            continue
        ports = ",".join(sorted({p[0] for p in e["ports"]},
                                key=lambda x: int(x) if x.isdigit() else 0))
        hn = f" ({e['hostname']})" if e.get("hostname") else ""
        lines.append(f"surface: host {ip}{hn} {ports} open ({art_id})")
    return "\n".join(lines)


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="内网协议产物聚合（零判断）")
    p.add_argument("target")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    host_map, sources = build_host_map(args.target)
    if not host_map:
        print("[HOST MAP] 无可用扫描产物（先让 nmap 带 -oX 落盘到 results/<t>/，由 execute 登记）")
        sys.exit(0)
    art_id = register_host_map(args.target, host_map, sources)
    if args.json:
        print(json.dumps({"art": art_id, "sources": sources, "hosts": host_map},
                         ensure_ascii=False, indent=2))
    else:
        print(format_output(args.target, host_map, sources, art_id))


if __name__ == "__main__":
    main()
