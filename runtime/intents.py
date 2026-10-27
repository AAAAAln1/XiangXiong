#!/usr/bin/env python3
"""intents.py — 前沿认领协议（L2 层）。

学 Cairn 的 claim / lease / heartbeat 语义（`server/services.py:112-120` 互斥认领、
`:222-237` 心跳超时自动释放），但用**文件系统**实现，不引入 Server / Dispatcher：

    认领 = O_CREAT|O_EXCL 创建 intents/<id>.claim  ← 原子；不存在"读-改-写"竞态
    心跳 = touch 该文件（mtime 即 last_heartbeat）
    释放 = unlink（仅持有者可为）
    过期 = mtime 超过 lease → 可被抢占

**零判断**：不选 intent、不排序、不评估哪个值得做、不生成 intent。
只做两件事：**互斥**与**租约**。谁能拿、拿了做什么，全在 LLM。

intent-id 来自投影中的前沿行（`h:xxxxxx` / `f:xxxxxx`，见 `project.py`）——
即"前沿项的 ID 就是 intent 的 ID"，不引入第二套编号，避免两套编号的同步缺陷。
文件名需去掉 `:`（Windows 非法字符），故落盘为 `h-xxxxxx.claim`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    from .graph import safe_id, unsafe_id
    from .paths import safe_name as safe_target
except ImportError:
    from graph import safe_id, unsafe_id
    from paths import safe_name as safe_target

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(PROJECT_ROOT, "results")

CLAIM_SUFFIX = ".claim"
DEFAULT_LEASE = 900  # 秒；与 deepdive 默认 timeout 对齐


def intents_dir(target: str) -> str:
    return os.path.join(RESULTS_DIR, safe_target(target), "intents")


def claim_path(target: str, intent_id: str) -> str:
    """intent-id → claim 文件路径。消毒见 `graph.safe_id`（单一实现）。"""
    return os.path.join(intents_dir(target), safe_id(intent_id) + CLAIM_SUFFIX)


def _read_holder(path: str):
    """返回持有者；文件不存在返回 None，空文件返回 ""。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return ""


def _age_seconds(path: str) -> float:
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return 0.0


def _touch(path: str) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def claim(target: str, intent_id: str, worker: str, lease: int = DEFAULT_LEASE) -> dict:
    """认领 intent。已持有则续约；被他人持有且未过期则拒绝；过期则可抢占。

    最多重试一次：允许"删除过期锁 → 重建"的竞态。
    """
    path = claim_path(target, intent_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, worker.encode("utf-8"))
            os.close(fd)
            return {"ok": True, "status": "claimed", "intent": intent_id,
                    "worker": worker, "lease_s": lease}
        except FileExistsError:
            holder = _read_holder(path)
            age = _age_seconds(path)
            if holder == worker:
                _touch(path)
                return {"ok": True, "status": "renewed", "intent": intent_id,
                        "worker": worker, "lease_s": lease}
            if age <= lease:
                return {"ok": False, "status": "held", "intent": intent_id,
                        "holder": holder, "age_s": round(age, 1), "lease_s": lease}
            if attempt == 1:
                # 抢占竞态失败：对手先建成功，此时它的 age≈0 必在 lease 内。
                return {"ok": False, "status": "preempt_lost", "intent": intent_id,
                        "holder": _read_holder(path)}
            try:
                os.remove(path)
            except OSError:
                pass
    return {"ok": False, "status": "unreachable", "intent": intent_id}


def heartbeat(target: str, intent_id: str, worker: str) -> dict:
    """刷新租约。仅持有者可为 —— 否则等于替别人续命。"""
    path = claim_path(target, intent_id)
    holder = _read_holder(path)
    if holder is None:
        return {"ok": False, "status": "not_claimed", "intent": intent_id}
    if holder != worker:
        return {"ok": False, "status": "held_by_other", "intent": intent_id,
                "holder": holder}
    _touch(path)
    return {"ok": True, "status": "renewed", "intent": intent_id, "worker": worker}


def release(target: str, intent_id: str, worker: str) -> dict:
    """释放。仅持有者可为。

    已知窄竞态：读取持有者与 unlink 之间，另一进程可能抢占成功，
    导致误删其 claim。窗口极小，且租约到期后可自动恢复，故不引入 CAS。
    """
    path = claim_path(target, intent_id)
    holder = _read_holder(path)
    if holder is None:
        return {"ok": True, "status": "not_claimed", "intent": intent_id}
    if holder != worker:
        return {"ok": False, "status": "held_by_other", "intent": intent_id,
                "holder": holder}
    try:
        os.remove(path)
    except OSError as error:
        return {"ok": False, "status": "remove_failed", "intent": intent_id,
                "error": str(error)}
    return {"ok": True, "status": "released", "intent": intent_id, "worker": worker}


def _unsafe_id(safe_name: str) -> str:
    """claim 文件名 → intent-id（`h-8a01be` → `h:8a01be`）。"""
    return unsafe_id(safe_name)


def list_claims(target: str, lease: int = DEFAULT_LEASE) -> list:
    """列出该 target 的全部认领（含过期判定）。纯 IO 投影。"""
    root = intents_dir(target)
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(CLAIM_SUFFIX):
            continue
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        age = _age_seconds(path)
        out.append({
            "intent": _unsafe_id(name[: -len(CLAIM_SUFFIX)]),
            "worker": _read_holder(path),
            "age_s": round(age, 1),
            "expired": age > lease,
        })
    return out


def claimable(frontier_ids: list, target: str, lease: int = DEFAULT_LEASE) -> list:
    """给定投影里的前沿 ID 列表，返回当前可认领的子集（机械差集）。"""
    held = {
        c["intent"]
        for c in list_claims(target, lease)
        if not c["expired"]
    }
    return [i for i in frontier_ids if i not in held]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="前沿认领协议（互斥 + 租约，零判断）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_claim = sub.add_parser("claim", help="认领一个 intent")
    p_claim.add_argument("target")
    p_claim.add_argument("intent_id")
    p_claim.add_argument("--worker", required=True)
    p_claim.add_argument("--lease", type=int, default=DEFAULT_LEASE)

    p_beat = sub.add_parser("heartbeat", help="刷新租约（仅持有者）")
    p_beat.add_argument("target")
    p_beat.add_argument("intent_id")
    p_beat.add_argument("--worker", required=True)

    p_release = sub.add_parser("release", help="释放（仅持有者）")
    p_release.add_argument("target")
    p_release.add_argument("intent_id")
    p_release.add_argument("--worker", required=True)

    p_status = sub.add_parser("status", help="列出该 target 的全部认领")
    p_status.add_argument("target")
    p_status.add_argument("--lease", type=int, default=DEFAULT_LEASE)

    args = parser.parse_args()

    if args.command == "claim":
        result = claim(args.target, args.intent_id, args.worker, args.lease)
    elif args.command == "heartbeat":
        result = heartbeat(args.target, args.intent_id, args.worker)
    elif args.command == "release":
        result = release(args.target, args.intent_id, args.worker)
    else:
        result = {"ok": True, "claims": list_claims(args.target, args.lease)}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
