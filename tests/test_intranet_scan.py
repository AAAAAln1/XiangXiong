#!/usr/bin/env python3
"""P1-b：intranet_scan 机械解析/聚合/host_map art 登记 + [SCOPE] 投影。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))

import intranet_scan as iscan
import execute
import scope
import project


NMAP_XML = """<?xml version="1.0"?>
<nmaprun><host>
<status state="up"/>
<address addr="10.0.0.178" addrtype="ipv4"/>
<hostnames><hostname name="dc01"/></hostnames>
<ports>
<port protocol="tcp" portid="445"><state state="open"/><service name="microsoft-ds" product="Microsoft Windows" version=""/></port>
<port protocol="tcp" portid="22"><state state="closed"/><service name="ssh"/></port>
<port protocol="tcp" portid="80"><state state="open"/><service name="http" product="Apache" version="2.4"/></port>
</ports></host>
<host><status state="up"/><address addr="10.0.0.177" addrtype="ipv4"/>
<ports><port protocol="tcp" portid="80"><state state="open"/><service name="http"/></port></ports>
</host></nmaprun>"""


def test_parse_nmap_xml_only_open():
    hosts = iscan.parse_nmap_xml(NMAP_XML)
    assert set(hosts) == {"10.0.0.178", "10.0.0.177"}
    ports = {p[0] for p in hosts["10.0.0.178"]["ports"]}
    assert ports == {"445", "80"}  # closed 的 22 不入
    assert hosts["10.0.0.178"]["hostname"] == "dc01"
    assert ("80", "tcp", "http", "Apache 2.4") in hosts["10.0.0.178"]["ports"]


def test_parse_text_fallback():
    text = ("# Nmap 7.94 scan initiated\n"
            "Host: 10.0.0.5 (router)	Status: Up\n"
            "Host: 10.0.0.5 (router)	Ports: 80/open/tcp//http//, 22/filtered/tcp//ssh//\n")
    hosts = iscan.parse_nmap_text(text)
    assert "10.0.0.5" in hosts
    assert {p[0] for p in hosts["10.0.0.5"]["ports"]} == {"80"}


def test_build_and_register_host_map(tmp_path, monkeypatch):
    monkeypatch.setenv("XIANGXIONG_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(execute, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(iscan, "RESULTS_DIR", str(tmp_path))
    # 先把 XML 登记为 port_scan artifact
    xml_path = tmp_path / "eng" / "artifacts" / "scan.xml"
    xml_path.parent.mkdir(parents=True)
    xml_path.write_text(NMAP_XML, encoding="utf-8")
    art_id = execute.register_artifact("eng", str(xml_path), source_cmd="nmap -oX", kind="port_scan")

    host_map, sources = iscan.build_host_map("eng")
    assert sources == [art_id]
    assert len(host_map) == 2
    map_id = iscan.register_host_map("eng", host_map, sources)
    assert map_id.startswith("art-")
    # 投影 sha 锚定：index 含 host_map，文件可校验
    index = json.load(open(tmp_path / "eng" / "artifacts" / "index.json", encoding="utf-8"))
    assert index[map_id]["kind"] == "host_map"
    out = iscan.format_output("eng", host_map, sources, map_id)
    assert "[HOST MAP]" in out and "[可抄行]" in out
    assert "surface: host 10.0.0.178 (dc01) 80,445 open (" + map_id in out


def test_empty_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("XIANGXIONG_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(iscan, "RESULTS_DIR", str(tmp_path))
    host_map, sources = iscan.build_host_map("nohost")
    assert host_map == {} and sources == []


# ── [SCOPE] 投影 ─────────────────────────────────────────────────────────────

def test_scope_section_rendered_for_engagement(tmp_path, monkeypatch):
    monkeypatch.setenv("XIANGXIONG_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(project, "_results_dir", lambda: str(tmp_path))
    scope.init_scope("intra-x", ["10.0.0.0/24"], ["10.0.0.1"])
    section = project.format_scope_section("intra-x 授权内网演练")
    assert "[SCOPE] intra-x" in section
    assert "10.0.0.0/24" in section and "10.0.0.1" in section
    assert "凭据 0 组" in section
    assert len(section) <= 400


def test_scope_section_absent_for_public(tmp_path, monkeypatch):
    monkeypatch.setenv("XIANGXIONG_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    assert project.format_scope_section("example.com") == ""
    assert project.format_scope_section("") == ""


def test_scope_terrain_capacity_and_sha_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("XIANGXIONG_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(execute, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(iscan, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(scope, "RESULTS_DIR", str(tmp_path))
    scope.init_scope("eng-t", ["10.0.0.0/24"], [])
    # 无 host_map：容量 256 + 未探提示
    s0 = project.format_scope_section("eng-t")
    assert "授权容量 256" in s0 and "未探≠不存在" in s0
    assert len(s0) <= 400
    # 登记一份真实 host_map
    xml_path = tmp_path / "eng-t" / "artifacts" / "scan.xml"
    xml_path.parent.mkdir(parents=True)
    xml_path.write_text(NMAP_XML, encoding="utf-8")
    scan_art = execute.register_artifact("eng-t", str(xml_path), kind="port_scan")
    host_map, sources = iscan.build_host_map("eng-t")
    map_art = iscan.register_host_map("eng-t", host_map, sources)
    s1 = project.format_scope_section("eng-t")
    assert "存活 2（0.8%）" in s1 and "开放端口 3" in s1  # 445,80,80 三开放
    # 篡改 host_map 文件 → sha 不符 → 回退为「未跑聚合」，拒显伪造数字
    hostmaps = list((tmp_path / "eng-t" / "hostmaps").glob("hostmap-*.json"))
    assert hostmaps
    hostmaps[0].write_text(hostmaps[0].read_text(encoding="utf-8").replace('"host_count": 2',
                                                                          '"host_count": 99'),
                           encoding="utf-8")
    s2 = project.format_scope_section("eng-t")
    assert "存活 99" not in s2 and "未跑地形聚合" in s2
