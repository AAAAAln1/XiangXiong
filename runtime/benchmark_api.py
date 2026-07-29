#!/usr/bin/env python3
"""
benchmark_api.py — TSec Benchmark API 机械客户端。

只负责调用评测平台接口与打印响应，不理解题目、不判断漏洞、不决定下一步。
配置来源优先级：环境变量 BENCHMARK_BASE_URL / BENCHMARK_TOKEN，其次 .benchmark.json。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, ".benchmark.json")
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
DEFAULT_TIMEOUT = 20


def load_config() -> dict:
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)

    base_url = os.environ.get("BENCHMARK_BASE_URL") or config.get("base_url")
    token = os.environ.get("BENCHMARK_TOKEN") or config.get("token")
    vpn_config = os.environ.get("BENCHMARK_VPN_CONFIG") or config.get("vpn_config")

    if not base_url or not token:
        raise SystemExit("缺少 BENCHMARK_BASE_URL 或 BENCHMARK_TOKEN，请设置环境变量或 .benchmark.json")

    return {
        "base_url": base_url.rstrip("/"),
        "token": token,
        "vpn_config": vpn_config,
        "benchmark_id": config.get("benchmark_id"),
    }


def request(config: dict, method: str, path: str, query: dict = None, body: dict = None) -> dict:
    query = query or {}
    url = config["base_url"] + path
    if query:
        url += "?" + urllib.parse.urlencode(query)

    data = None
    headers = {"BENCHMARK_TOKEN": config["token"]}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return {"status": resp.status, "body": parse_json(raw)}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return {"status": e.code, "body": parse_json(raw)}
    except urllib.error.URLError as e:
        return {"status": "ERROR", "body": {"error": str(e)}}


def parse_json(raw: str):
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return raw


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_id(value: str) -> str:
    return value.replace(":", "_").replace("/", "_").replace("\\", "_")


def benchmark_id(config: dict) -> str:
    return (
        os.environ.get("BENCHMARK_ID")
        or config.get("benchmark_id")
        or f"tsecbench-{datetime.now(timezone.utc):%Y-%m-%d}"
    )


def run_dir(config: dict) -> str:
    return os.path.join(RESULTS_DIR, "benchmarks", safe_id(benchmark_id(config)))


def redact(value, secrets: tuple):
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    return value


def archive_call(config: dict, command: str, method: str, path: str, result: dict,
                 unique_code: str = None, flag: str = None) -> None:
    timestamp = now_iso()
    root = run_dir(config)
    logs_dir = os.path.join(root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    secrets = (config.get("token"), flag)
    entry = {
        "timestamp": timestamp,
        "command": command,
        "request": {"method": method, "path": path},
        "response": {
            "status": result.get("status"),
            "body": redact(result.get("body"), secrets),
        },
    }
    if unique_code is not None:
        entry["unique_code"] = unique_code
    with open(os.path.join(logs_dir, "api.jsonl"), "a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if command == "list":
        snapshot = {"timestamp": timestamp, **redact(result, secrets)}
        score_path = os.path.join(root, "score.json")
        temp_path = score_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(snapshot, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, score_path)


def call_and_archive(config: dict, command: str, method: str, path: str,
                     query: dict = None, body: dict = None, unique_code: str = None,
                     flag: str = None) -> dict:
    result = request(config, method, path, query=query, body=body)
    archive_call(config, command, method, path, result, unique_code=unique_code, flag=flag)
    return result


def print_result(result: dict):
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="TSec Benchmark API 客户端")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("config", help="显示当前配置摘要（不显示 token）")
    sub.add_parser("list", help="列出题目")

    start = sub.add_parser("start", help="启动题目容器")
    start.add_argument("unique_code")

    hint = sub.add_parser("hint", help="获取提示（会扣分）")
    hint.add_argument("unique_code")

    submit = sub.add_parser("submit", help="提交 flag")
    submit.add_argument("unique_code")
    submit.add_argument("flag")

    close = sub.add_parser("close", help="关闭题目容器")
    close.add_argument("unique_code")

    args = parser.parse_args()
    config = load_config()

    if args.command == "config":
        print(json.dumps({
            "base_url": config["base_url"],
            "token": "***" + config["token"][-8:],
            "vpn_config": config.get("vpn_config"),
            "vpn_config_exists": bool(config.get("vpn_config") and os.path.exists(config["vpn_config"])),
        }, ensure_ascii=False, indent=2))
    elif args.command == "list":
        print_result(call_and_archive(config, "list", "GET", "/openapi/v1/challenges"))
    elif args.command == "start":
        print_result(call_and_archive(
            config, "start", "POST", "/openapi/v1/challenges/start",
            query={"unique_code": args.unique_code}, unique_code=args.unique_code,
        ))
    elif args.command == "hint":
        print_result(call_and_archive(
            config, "hint", "GET", "/openapi/v1/challenges/hint",
            query={"unique_code": args.unique_code}, unique_code=args.unique_code,
        ))
    elif args.command == "submit":
        print_result(call_and_archive(
            config, "submit", "POST", "/openapi/v1/challenges/submit",
            body={"unique_code": args.unique_code, "flag": args.flag},
            unique_code=args.unique_code, flag=args.flag,
        ))
    elif args.command == "close":
        print_result(call_and_archive(
            config, "close", "POST", "/openapi/v1/challenges/close",
            query={"unique_code": args.unique_code}, unique_code=args.unique_code,
        ))


if __name__ == "__main__":
    main()
