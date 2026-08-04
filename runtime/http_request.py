#!/usr/bin/env python3
"""
http_request.py — 黑盒 HTTP 请求工具（瞎子）。

三不原则：
  CAN:   发请求、算长度、截断、写文件、返回 [META] + [BODY]
  CAN:   解析 curl -w 的物理量 (http_code, size_download)
  CANNOT: 判断状态码含义或提取、解释响应字段
  CANNOT: 出现基于响应内容或状态码的业务判断

输出协议：
  [META]   Status: {code} | Length: {bytes} (Truncated to 2048) | Saved: responses/req_NNN.txt
  [BODY]   {截断后的安全内容}

用法：
  python runtime/http_request.py <target> [--experiment-id exp-NNN] <curl_args...>
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

try:
    from .experiment import append_observation, resolve_experiment_id
except ImportError:
    from experiment import append_observation, resolve_experiment_id

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
RESPONSES_SUBDIR = "responses"
MAX_BODY_CHARS = 2048
CURL_TIMEOUT = 15
SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "set-cookie", "token", "x-api-key", "x-auth-token"}

# Observation repetition cache (tool observation cache only).
# - discardable / rebuildable
# - does NOT participate in merge / world projection / memory schema
PATTERN_COUNTS_FILENAME = ".pattern_counts.json"
PATTERN_COUNTS_MAX_KEYS = 200


def safe_target(target: str) -> str:
    return target.replace(":", "_").replace("/", "_").replace("\\", "_")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(target: str) -> str:
    path = os.path.join(RESULTS_DIR, safe_target(target), RESPONSES_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def next_request_number(save_dir: str) -> int:
    highest = 0
    for name in os.listdir(save_dir):
        match = re.fullmatch(r"req_(\d+)\.(?:txt|json)", name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def relative_result_path(target: str, path: str) -> str:
    target_root = os.path.join(RESULTS_DIR, safe_target(target))
    return os.path.relpath(path, target_root).replace("\\", "/")


def extract_experiment_id(args: list) -> tuple:
    remaining = []
    experiment_id = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--experiment-id":
            if index + 1 >= len(args):
                raise ValueError("--experiment-id 缺少值")
            experiment_id = args[index + 1]
            index += 2
            continue
        if arg.startswith("--experiment-id="):
            experiment_id = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return experiment_id, remaining


def is_sensitive_header(name: str) -> bool:
    normalized = name.strip().lower()
    return normalized in SENSITIVE_HEADERS or "token" in normalized or "api-key" in normalized


def redact_args(args: list) -> list:
    redacted = list(args)
    index = 0
    while index < len(redacted):
        arg = redacted[index]
        if arg in {"-H", "--header"} and index + 1 < len(redacted):
            header = redacted[index + 1]
            name, separator, _ = header.partition(":")
            if separator and is_sensitive_header(name):
                redacted[index + 1] = f"{name}: [REDACTED]"
            index += 2
            continue
        if arg.startswith("--header="):
            header = arg.split("=", 1)[1]
            name, separator, _ = header.partition(":")
            if separator and is_sensitive_header(name):
                redacted[index] = f"--header={name}: [REDACTED]"
        elif arg in {"-b", "--cookie", "-u", "--user"} and index + 1 < len(redacted):
            redacted[index + 1] = "[REDACTED]"
            index += 1
        elif arg.startswith("--cookie=") or arg.startswith("--user="):
            redacted[index] = arg.split("=", 1)[0] + "=[REDACTED]"
        index += 1
    return redacted


def normalize_curl_args(args: list) -> list:
    normalized = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--method":
            if index + 1 >= len(args):
                raise ValueError("--method 缺少值")
            normalized.extend(["-X", args[index + 1]])
            index += 2
            continue
        if arg.startswith("--method="):
            normalized.extend(["-X", arg.split("=", 1)[1]])
            index += 1
            continue
        if arg == "--timeout":
            if index + 1 >= len(args):
                raise ValueError("--timeout 缺少值")
            normalized.extend(["--max-time", args[index + 1]])
            index += 2
            continue
        if arg.startswith("--timeout="):
            normalized.extend(["--max-time", arg.split("=", 1)[1]])
            index += 1
            continue
        normalized.append(arg)
        index += 1
    return normalized


def extract_method_url(args: list) -> tuple:
    method = None
    url = None
    index = 0
    options_with_value = {
        "-A", "--user-agent", "-b", "--cookie", "-d", "--data", "--data-raw",
        "--data-binary", "-e", "--referer", "-F", "--form", "-H", "--header",
        "-o", "--output", "-u", "--user", "-x", "--proxy", "-X", "--request",
        "--connect-timeout", "--max-time", "-w", "--write-out",
    }
    while index < len(args):
        arg = args[index]
        if arg in {"-X", "--request"} and index + 1 < len(args):
            method = args[index + 1]
            index += 2
            continue
        if arg.startswith("--request="):
            method = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "--url" and index + 1 < len(args):
            url = args[index + 1]
            index += 2
            continue
        if arg.startswith("--url="):
            url = arg.split("=", 1)[1]
            index += 1
            continue
        if arg in options_with_value:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        url = arg
        index += 1
    if method is None:
        data_prefixes = ("--data=", "--data-raw=", "--data-binary=", "--form=")
        has_data = any(
            arg in {"-d", "--data", "--data-raw", "--data-binary", "-F", "--form"}
            or arg.startswith(data_prefixes)
            for arg in args
        )
        method = "POST" if has_data else "GET"
    return method, url


def write_metadata(path: str, metadata: dict) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)


# ── Observation repetition (pure facts; no interpretation) ──

def normalized_host_path(url: str) -> tuple:
    """host + normalized path (query/fragment stripped, trailing slash dropped).

    Observation signature only — never maps to meaning.
    """
    try:
        parts = urlsplit(url if url else "")
    except ValueError:
        return "", ""
    host = parts.netloc or ""
    path = parts.path or "/"
    path = re.sub(r"/+", "/", path)
    if path != "/":
        path = path.rstrip("/")
    if not path:
        path = "/"
    return host, path


def normalized_body_hash(body: str) -> str:
    """Mechanical body fingerprint: whitespace-collapsed + sha1 short.

    Does NOT extract errno/field names/error types (no business structure).
    """
    collapsed = re.sub(r"\s+", "", body or "")
    return hashlib.sha1(collapsed.encode("utf-8", errors="replace")).hexdigest()[:8]


def pattern_counts_path(target: str) -> str:
    target_root = os.path.join(RESULTS_DIR, safe_target(target))
    return os.path.join(target_root, PATTERN_COUNTS_FILENAME)


def load_pattern_counts(target: str) -> dict:
    path = pattern_counts_path(target)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def save_pattern_counts(target: str, counts: dict) -> None:
    path = pattern_counts_path(target)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(counts, file, ensure_ascii=False)


def record_pattern(target: str, method: str, url: str, status_code: str, body: str) -> tuple:
    """Increment occurrences for (request_sig, response_sig); return (key, occurrences).

    Facts only: counts how many times the same mechanical signature occurred.
    Bounded; discardable; never interpreted.
    Disabled via XIANGXIONG_OBS_REPEAT=0 (A/B experiment control).
    """
    if os.environ.get("XIANGXIONG_OBS_REPEAT", "1") == "0":
        return "", 0
    host, path = normalized_host_path(url)
    if not host or not status_code or status_code == "000":
        return "", 0
    key = f"{method}:{host}{path}:{status_code}:{normalized_body_hash(body)}"
    counts = load_pattern_counts(target)
    occurrences = counts.get(key, 0) + 1
    counts[key] = occurrences
    # bounded cache: drop oldest key when exceeding limit
    if len(counts) > PATTERN_COUNTS_MAX_KEYS:
        oldest = next(iter(counts), None)
        if oldest is not None:
            del counts[oldest]
    save_pattern_counts(target, counts)
    return key, occurrences


def run_curl(target: str, args: list, experiment_id: str = None) -> dict:
    """执行 curl，保存完整响应 body 与机械执行 metadata。"""
    args = normalize_curl_args(args)
    semantic_experiment_id = resolve_experiment_id(target, experiment_id)
    save_dir = ensure_dir(target)
    number = next_request_number(save_dir)
    filename = f"req_{number:03d}.txt"
    metadata_filename = f"req_{number:03d}.json"
    save_path = os.path.join(save_dir, filename)
    metadata_path = os.path.join(save_dir, metadata_filename)
    body_path = save_path + ".body.tmp"
    method, url = extract_method_url(args)
    started = time.time()

    curl_args = [
        "curl", "--max-time", str(CURL_TIMEOUT), "-s", "-o", body_path,
        "-w", "%{http_code}|%{size_download}|%{time_total}",
    ]
    curl_args.extend(args)

    status_code = "000"
    body_length = "0"
    duration = 0.0
    return_code = None
    stderr = ""
    full_body = ""
    error = None
    try:
        result = subprocess.run(
            curl_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CURL_TIMEOUT + 5,
        )
        return_code = result.returncode
        stderr = result.stderr or ""
        parts = (result.stdout or "").strip().split("|")
        status_code = parts[0] if parts and parts[0] else "000"
        body_length = parts[1] if len(parts) > 1 and parts[1] else "0"
        try:
            duration = float(parts[2]) if len(parts) > 2 else round(time.time() - started, 3)
        except ValueError:
            duration = round(time.time() - started, 3)
        if return_code != 0:
            detail = stderr.strip() or ("请求超时" if return_code == 28 else "无错误详情")
            error = f"curl 失败 (退出码 {return_code}): {detail}"
    except subprocess.TimeoutExpired as exc:
        duration = round(time.time() - started, 3)
        stderr_value = exc.stderr or ""
        stderr = stderr_value.decode("utf-8", errors="replace") if isinstance(stderr_value, bytes) else stderr_value
        error = f"curl 超时 ({CURL_TIMEOUT}s)"
    except OSError as exc:
        duration = round(time.time() - started, 3)
        stderr = str(exc)
        error = str(exc)

    if os.path.exists(body_path):
        with open(body_path, "r", encoding="utf-8", errors="replace") as file:
            full_body = file.read()
        os.replace(body_path, save_path)
    else:
        with open(save_path, "w", encoding="utf-8", errors="replace") as file:
            file.write("")

    body_reference = relative_result_path(target, save_path)
    metadata_reference = relative_result_path(target, metadata_path)
    metadata = {
        "target": target,
        "timestamp": now_iso(),
        "args": redact_args(args),
        "method": method,
        "url": url,
        "status": status_code,
        "length": body_length,
        "duration": duration,
        "return_code": return_code,
        "stderr": stderr,
        "response_body": body_reference,
        "experiment_id": semantic_experiment_id,
    }
    write_metadata(metadata_path, metadata)

    association_error = None
    if semantic_experiment_id:
        observation = (
            f"method: {method}\nurl: {url}\nstatus: {status_code}\n"
            f"length: {body_length}\nduration: {duration}s\nreturn_code: {return_code}\n"
            f"body_artifact: {body_reference}\nmetadata_artifact: {metadata_reference}"
        )
        try:
            append_observation(target, semantic_experiment_id, observation, metadata_reference)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            association_error = (
                f"无法关联实验 {semantic_experiment_id}: {exc}; "
                f"请求记录已保存至 {metadata_reference}"
            )

    truncated = len(full_body) > MAX_BODY_CHARS
    pattern_key, pattern_occurrences = record_pattern(target, method, url, status_code, full_body)
    return {
        "method": method,
        "url": url,
        "status_code": status_code,
        "body_length": body_length,
        "duration": duration,
        "return_code": return_code,
        "stderr": stderr,
        "truncated": truncated,
        "body": full_body[:MAX_BODY_CHARS],
        "filename": filename,
        "metadata_filename": metadata_filename,
        "error": error,
        "association_error": association_error,
        "pattern_key": pattern_key,
        "pattern_occurrences": pattern_occurrences,
    }


def format_output(result: dict) -> str:
    """
    输出协议 v2：区分 runtime_error / transport_error / target_response
    
    段落：
      [EXECUTION] — curl 执行状态
      [REQUEST]   — 实际发送的请求摘要
      [RESPONSE]  — 目标响应摘要
      [BODY]      — 响应体
    """
    # 判断 target_contact
    return_code = result.get("return_code", 0)
    status_code = result.get("status_code", "000")
    target_contact = (return_code == 0 and status_code != "000")
    
    # 判断 error_type
    error = result.get("error")
    error_type = None
    if error:
        if "超时" in error or return_code == 28:
            error_type = "transport_error"
        elif return_code == 6:  # DNS
            error_type = "transport_error"
        elif return_code == 7:  # 连接失败
            error_type = "transport_error"
        elif return_code != 0:
            error_type = "runtime_error"
    
    # [EXECUTION]
    execution = [
        "[EXECUTION]",
        f"return_code: {return_code}",
        f"duration: {result.get('duration', 0)}s",
        f"target_contact: {str(target_contact).lower()}",
    ]
    if error:
        execution.append(f"error_type: {error_type or 'unknown'}")
        execution.append(f"error_detail: {error}")
    
    # [REQUEST]
    request_section = [
        "[REQUEST]",
        f"method: {result.get('method', 'UNKNOWN')}",
        f"url: {result.get('url', 'UNKNOWN')}",
    ]
    
    # [RESPONSE]
    body_artifact = f"responses/{result['filename']}" if result.get("filename") else ""
    try:
        body_size = int(result.get("body_length") or 0)
    except (TypeError, ValueError):
        body_size = len(result.get("body") or "")
    body_truncated = bool(result.get("truncated"))

    if target_contact:
        response_section = [
            "[RESPONSE]",
            f"status: {status_code}",
            f"length: {result['body_length']} bytes",
            f"saved: {body_artifact}",
            # Observation 物理元数据（一阶）：与 execute 同合同 {path,size,truncated}
            f"body_artifact: {body_artifact}",
            f"body_size: {body_size}",
            f"body_truncated: {str(body_truncated).lower()}",
        ]
        # Pure fact: how many times this mechanical response signature occurred.
        occurrences = result.get("pattern_occurrences", 0)
        key = result.get("pattern_key", "")
        if occurrences >= 2 and key:
            response_section.append(f"[OBSERVATION REPETITION] key={key} occurrences={occurrences}")
    else:
        response_section = [
            "[RESPONSE]",
            "(未收到响应)",
        ]
        if body_artifact:
            response_section.extend(
                [
                    f"body_artifact: {body_artifact}",
                    f"body_size: {body_size}",
                    f"body_truncated: {str(body_truncated).lower()}",
                ]
            )

    # [BODY] 预览≠全文；全文在 body_artifact
    body_display = result["body"] if result.get("body") else "(空响应体)" if target_contact else "(请求失败)"
    body_section = ["[BODY]"]
    if body_truncated and body_artifact:
        body_section.append(
            f"(截断到 {MAX_BODY_CHARS} 字符，full={body_artifact} size={body_size})"
        )
    body_section.append(body_display)
    
    return "\n".join(execution + [""] + request_section + [""] + response_section + [""] + body_section)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(sys.argv) < 3:
        print("用法: python runtime/http_request.py <target> [--experiment-id exp-NNN] <curl_args...>")
        sys.exit(1)

    target = sys.argv[1]
    try:
        experiment_id, curl_args = extract_experiment_id(sys.argv[2:])
        curl_args = normalize_curl_args(curl_args)
    except ValueError as error:
        print(f"[META]\nStatus: ERROR | Error: {error}\n[BODY]\n(请求失败)")
        sys.exit(1)

    if "-k" not in curl_args and "--insecure" not in curl_args:
        curl_args.insert(0, "-k")
    result = run_curl(target, curl_args, experiment_id)

    try:
        print(format_output(result))
    except UnicodeEncodeError:
        result["body"] = result.get("body", "").encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        print(format_output(result))
    if result.get("error") or result.get("association_error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
