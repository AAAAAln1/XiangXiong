#!/usr/bin/env python3
"""toolcheck.py — 本机外部二进制就绪探针（机械，零判断）。

它补的缺口：
  非 HTTP 路径（CTF / 逆向 / 取证 / 枚举）最终都经 execute.py 跑外部二进制，而
  execute.py 只在**跑完之后**才返回 tool_not_found —— 模型要花掉一轮上下文，才知道
  这条方向在本机根本不可执行。本模块把"本机能力边界"提前变成可投影的事实。

边界（刻意不同于 pentest-ai 的 doctor.sh）：
  - 只报**缺失**：本机已存在的二进制不列出。列全量即等于给一份"工具菜单"，
    会被读成推荐；而"缺失"才是会改变下一跳的判据。
  - 不分领域、不排序、不评分、不给安装提示、不按 intent 挑工具（那是 Planner / Router）。
  - 不安装任何东西、不发网络请求：只做 shutil.which 与本地版本号读取。

投影用法：runtime/project.py 的 [TOOLS] 段只调用 missing_binaries()，
  结果是 Structural Derivation（任何实现得到同一结果），不是 Semantic Derivation。
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys

# Runtime 硬依赖：缺失即主路径不可用（http_request.py 经 subprocess 调 curl）。
RUNTIME_REQUIRED = ("curl",)

# 候选探测集：只回答"本机有没有"，不含推荐语义、不分领域、不排序。
# 刻意不做 per-intent 映射 —— 那会把本模块变成工具路由器。
CANDIDATE_BINARIES = (
    # HTTP / Web
    "curl",
    "httpx",
    "ffuf",
    "gobuster",
    "feroxbuster",
    "sqlmap",
    "nuclei",
    "nikto",
    "whatweb",
    "subfinder",
    "jq",
    "openssl",
    # 网络与 DNS
    "nmap",
    "dig",
    "whois",
    # 非 HTTP（本地取证 / 逆向 / CTF）
    "file",
    "strings",
    "xxd",
    "binwalk",
    "exiftool",
    "objdump",
    "readelf",
    "radare2",
    "tshark",
    "volatility3",
    "hashcat",
    "john",
    "steghide",
    "zsteg",
    "apktool",
    "adb",
)


def missing_binaries(candidates=None) -> list:
    """返回本机不存在的二进制名（保持声明顺序：不排序、不评分、不分类）。

    Args:
        candidates: 覆盖候选集（默认 CANDIDATE_BINARIES）；只用于测试与调用方复用。

    Returns:
        缺失的二进制名列表。
    """
    if candidates is None:
        candidates = CANDIDATE_BINARIES
    return [name for name in candidates if shutil.which(name) is None]


def present_binaries(candidates=None) -> list:
    """返回本机存在的二进制名（供人侧审计使用；投影不使用它）。"""
    if candidates is None:
        candidates = CANDIDATE_BINARIES
    return [name for name in candidates if shutil.which(name) is not None]


def audit() -> dict:
    """全量审计：只陈述存在性与解释器版本，不做可用性判断、不给结论。"""
    return {
        "python": platform.python_version(),
        "runtime_required": {
            name: ("present" if shutil.which(name) else "missing")
            for name in RUNTIME_REQUIRED
        },
        "candidates_total": len(CANDIDATE_BINARIES),
        "present": present_binaries(),
        "missing": missing_binaries(),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="本机外部二进制就绪审计（只报告，不安装、不推荐）"
    )
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    report = audit()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(0)

    print("象雄外部工具就绪审计（只报告，不安装、不推荐）")
    print("")
    print(f"解释器: python {report['python']}")
    print("Runtime 硬依赖")
    for name, state in report["runtime_required"].items():
        print(f"  {name}: {state}")
    print("")
    print(f"候选二进制（{report['candidates_total']} 个，仅列缺失）")
    if report["missing"]:
        for name in report["missing"]:
            print(f"  missing: {name}")
    else:
        print("  （无缺失）")
    print("")
    print(f"缺失 {len(report['missing'])}/{report['candidates_total']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
