#!/usr/bin/env python3
"""Benchmark VPN 的机械准备、状态检查与启动入口。"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

try:
    from runtime import benchmark_api
except ImportError:
    import benchmark_api

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, ".benchmark.json")
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
DEFAULT_NETWORK = "10.0.128.0/18"
DEFAULT_GATEWAY = "10.254.0.1"
DEFAULT_WAIT_SECONDS = 60
COMMON_OPENVPN_PATHS = (r"C:\Program Files\OpenVPN\bin\openvpn.exe",)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(config_arg: Optional[str] = None) -> Dict:
    file_config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            file_config = json.load(file)

    source = config_arg or os.environ.get("BENCHMARK_VPN_CONFIG") or file_config.get("vpn_config")
    if not source:
        raise ValueError("缺少 VPN 配置：请使用 --config、BENCHMARK_VPN_CONFIG 或 .benchmark.json vpn_config")

    if config_arg or os.environ.get("BENCHMARK_VPN_CONFIG"):
        source = os.path.abspath(os.path.expanduser(os.path.expandvars(source)))
    else:
        source = os.path.abspath(os.path.join(os.path.dirname(CONFIG_PATH), source))

    return {
        "vpn_config": source,
        "benchmark_id": file_config.get("benchmark_id"),
        "vpn_network": file_config.get("vpn_network", DEFAULT_NETWORK),
        "vpn_gateway": file_config.get("vpn_gateway", DEFAULT_GATEWAY),
    }


def vpn_dir(config: Dict) -> str:
    benchmark_id = benchmark_api.safe_id(benchmark_api.benchmark_id(config))
    return os.path.join(RESULTS_DIR, "benchmarks", benchmark_id, "vpn")


def find_openvpn() -> str:
    candidates = []
    configured = os.environ.get("OPENVPN_EXE")
    if configured:
        candidates.append(configured)
    candidates.extend(COMMON_OPENVPN_PATHS)
    for candidate in candidates:
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(candidate)))
        if os.path.isfile(path):
            return path
    raise FileNotFoundError("未找到 openvpn.exe；请设置 OPENVPN_EXE 或安装 OpenVPN")


def read_openvpn_version(executable: str) -> Tuple[str, Tuple[int, ...]]:
    completed = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10, check=False,
    )
    output = completed.stdout or completed.stderr
    match = re.search(r"OpenVPN\s+([0-9]+(?:\.[0-9]+)+)", output, re.IGNORECASE)
    if not match:
        raise RuntimeError("无法读取 OpenVPN 版本")
    version = match.group(1)
    return version, tuple(int(part) for part in version.split("."))


def prepare_compatibility(content: str, version: Tuple[int, ...]) -> str:
    if version[:2] > (2, 5):
        return content

    result = []
    data_ciphers_index = None
    cipher_present = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            result.append(raw_line)
            continue
        parts = stripped.split()
        directive = parts[0].lower()
        if directive == "compat-mode":
            continue
        if directive == "ignore-unknown-option":
            options = [option for option in parts[1:] if option.lower() != "compat-mode"]
            if options:
                indent = raw_line[:len(raw_line) - len(raw_line.lstrip())]
                result.append(indent + "ignore-unknown-option " + " ".join(options))
            continue
        if directive == "data-ciphers":
            values = " ".join(parts[1:])
            ciphers = [item for item in re.split(r"[:,]", values) if item]
            if "AES-128-CBC" not in {item.upper() for item in ciphers}:
                values = values + (":" if values else "") + "AES-128-CBC"
            indent = raw_line[:len(raw_line) - len(raw_line.lstrip())]
            result.append(indent + "data-ciphers " + values)
            data_ciphers_index = len(result) - 1
            continue
        if directive == "cipher":
            cipher_present = True
        result.append(raw_line)

    if data_ciphers_index is None and not cipher_present:
        result.append("cipher AES-128-CBC")
    newline = "\r\n" if "\r\n" in content else "\n"
    return newline.join(result) + (newline if content.endswith(("\n", "\r")) else "")


def prepare(config: Dict) -> Dict:
    source = config["vpn_config"]
    if not os.path.isfile(source):
        raise FileNotFoundError("VPN 配置文件不存在：{}".format(source))
    executable = find_openvpn()
    version_text, version_tuple = read_openvpn_version(executable)
    with open(source, "rb") as file:
        source_bytes = file.read()
    content = source_bytes.decode("utf-8-sig")
    prepared_content = prepare_compatibility(content, version_tuple)

    output_dir = vpn_dir(config)
    os.makedirs(output_dir, exist_ok=True)
    prepared_path = os.path.join(output_dir, "client.ovpn")
    with open(prepared_path, "w", encoding="utf-8", newline="") as file:
        file.write(prepared_content)

    metadata = {
        "timestamp": now_iso(),
        "source_path": os.path.abspath(source),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "openvpn_executable": executable,
        "openvpn_version": version_text,
        "prepared_path": prepared_path,
    }
    metadata_path = os.path.join(output_dir, "metadata.json")
    with open(metadata_path + ".tmp", "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)
    os.replace(metadata_path + ".tmp", metadata_path)
    return {"ok": True, "command": "prepare", **metadata}


def route_facts(network: str, gateway: Optional[str] = None) -> Dict:
    if os.name != "nt":
        return {
            "route_present": False,
            "gateway_present": False if gateway else None,
            "network": network,
            "gateway": gateway,
            "error": "仅支持 Windows route print",
        }
    completed = subprocess.run(
        ["route.exe", "print"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10, check=False,
    )
    if completed.returncode != 0:
        return {
            "route_present": False,
            "gateway_present": False if gateway else None,
            "network": network,
            "gateway": gateway,
            "error": completed.stderr.strip() or "route print 执行失败",
        }
    address, prefix_text = network.split("/", 1)
    prefix = int(prefix_text)
    mask_value = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    mask = ".".join(str((mask_value >> shift) & 0xFF) for shift in (24, 16, 8, 0))
    matching_lines = [line for line in completed.stdout.splitlines() if address in line and mask in line]
    return {
        "route_present": bool(matching_lines),
        "gateway_present": any(gateway in line for line in matching_lines) if gateway else None,
        "network": network,
        "gateway": gateway,
        "error": None,
    }


def status(config: Dict) -> Dict:
    return {"ok": True, "command": "status", "facts": route_facts(
        config.get("vpn_network", DEFAULT_NETWORK), config.get("vpn_gateway"),
    )}


def route_ready(facts: Dict) -> bool:
    return facts["route_present"] and facts.get("gateway_present") is not False


def same_config_running(executable: str, prepared_path: str) -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=[IO.Path]::GetFullPath($env:XIANGXIONG_VPN_CONFIG);"
        "$found=Get-CimInstance Win32_Process -Filter \"Name='openvpn.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($p,[StringComparison]::OrdinalIgnoreCase)-ge 0 };"
        "if($found){exit 0}else{exit 1}"
    )
    environment = os.environ.copy()
    environment["XIANGXIONG_VPN_CONFIG"] = prepared_path
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
        env=environment,
    )
    return completed.returncode == 0


def start_elevated(executable: str, prepared_path: str, log_path: str) -> Dict:
    script = (
        "$a=@('--config',('\"'+$env:XIANGXIONG_VPN_CONFIG+'\"'),'--log',"
        "('\"'+$env:XIANGXIONG_VPN_LOG+'\"'));"
        "try{$p=Start-Process -FilePath $env:XIANGXIONG_OPENVPN_EXE -ArgumentList $a "
        "-Verb RunAs -PassThru -ErrorAction Stop;"
        "[Console]::Out.Write($p.Id);exit 0}catch{[Console]::Error.Write($_.Exception.Message);exit 1}"
    )
    environment = os.environ.copy()
    environment.update({
        "XIANGXIONG_OPENVPN_EXE": executable,
        "XIANGXIONG_VPN_CONFIG": prepared_path,
        "XIANGXIONG_VPN_LOG": log_path,
    })
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            env=environment,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        return {"started": False, "error": "管理员启动不可用或 UAC 未完成：{}".format(error)}
    if completed.returncode != 0:
        return {"started": False, "error": "管理员启动失败或 UAC 被拒绝：{}".format(completed.stderr.strip())}
    return {"started": True, "pid": int(completed.stdout.strip()) if completed.stdout.strip().isdigit() else None}


def ensure(config: Dict, wait_seconds: int = DEFAULT_WAIT_SECONDS, poll_interval: float = 1.0) -> Dict:
    initial = status(config)["facts"]
    if route_ready(initial):
        return {"ok": True, "command": "ensure", "started": False, "facts": initial}

    try:
        prepared = prepare(config)
    except (OSError, UnicodeError, ValueError, RuntimeError) as error:
        return {"ok": False, "command": "ensure", "started": False, "error": str(error), "facts": initial}

    executable = prepared["openvpn_executable"]
    prepared_path = prepared["prepared_path"]
    if same_config_running(executable, prepared_path):
        launch = {"started": False, "already_running": True}
    else:
        launch = start_elevated(executable, prepared_path, os.path.join(vpn_dir(config), "vpn.log"))
        if not launch["started"]:
            return {"ok": False, "command": "ensure", **launch, "facts": initial}

    deadline = time.monotonic() + max(0, wait_seconds)
    facts = initial
    while True:
        facts = status(config)["facts"]
        if route_ready(facts):
            return {"ok": True, "command": "ensure", **launch, "facts": facts}
        if time.monotonic() >= deadline:
            return {
                "ok": False, "command": "ensure", **launch, "facts": facts,
                "error": "等待目标路由超时（{} 秒）".format(wait_seconds),
            }
        time.sleep(poll_interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark VPN Runtime")
    parser.add_argument("--config", help="原始 .ovpn 配置路径")
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT_SECONDS, help="ensure 最长等待秒数")
    parser.add_argument("--network", help="目标网段，默认 10.0.128.0/18")
    parser.add_argument("--gateway", help="可选 VPN 网关；传空字符串可禁用检查")
    parser.add_argument("command", choices=("prepare", "status", "ensure"))
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
        if args.network:
            config["vpn_network"] = args.network
        if args.gateway is not None:
            config["vpn_gateway"] = args.gateway or None
        if args.command == "prepare":
            result = prepare(config)
        elif args.command == "status":
            result = status(config)
        else:
            result = ensure(config, args.wait_seconds)
    except (OSError, UnicodeError, ValueError, RuntimeError) as error:
        result = {"ok": False, "command": args.command, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
