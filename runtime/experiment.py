#!/usr/bin/env python3
"""目标级实验事实管理。

Runtime 只保存调用方提供的假设、动作、观察与结论，不生成或解释内容。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    from .filelock import locked
    from .graph import EVIDENCE_BLOCK_RE
    from .paths import safe_name as safe_target
    from . import turn_state
except ImportError:
    from filelock import locked
    from graph import EVIDENCE_BLOCK_RE
    from paths import safe_name as safe_target
    import turn_state

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(PROJECT_ROOT, "results")
EXPERIMENT_ID_PATTERN = re.compile(r"^exp-\d{3,}$")
FINAL_STATUSES = {"success", "failed", "blocked", "unknown"}

# 编号分配锁（每 target 一把）。并发探索者会同时建实验，无锁则撞号。
ID_LOCK_NAME = ".id_lock"

# 实验最终状态 → memory hypothesis 行内状态（机械映射，语义由 LLM 的 --status 决定）
MEMORY_STATUS_MAP = {
    "success": "confirmed",
    "failed": "contradicted",
    "blocked": "blocked",
    "unknown": "observed",
}
VALID_MEMORY_STATES = {"confirmed", "contradicted", "blocked", "observed"}

# Interpretation 层：LLM 对 observation 的认知解释（显式 CLI 产生，不由 execute 自动调用）。
# 与 observation（Runtime 物理事实）分离；不参与 Evidence Gate；不自动生成 DELTA。
VALID_CLASSIFICATIONS = ("confirmed", "contradicted", "new_observation", "no_information")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def experiments_dir(target: str) -> str:
    return os.path.join(RESULTS_DIR, safe_target(target), "experiments")


def reserve_experiment_dir(target: str) -> tuple:
    """在跨进程锁内分配编号并**立即创建目录**，返回 (experiment_id, path)。

    占位必须在锁内完成：只锁"扫描 + 加一"不够——释放锁后、目录建好前，
    另一个进程会扫到同一个 max 号，分配出同一个 ID（目录尚不存在，扫不到）。
    与 `http_request.next_request_number` 先写占位文件是同一手法。
    """
    root = experiments_dir(target)
    with locked(os.path.join(root, ID_LOCK_NAME)):
        highest = 0
        if os.path.isdir(root):
            for name in os.listdir(root):
                match = re.match(r"^exp-(\d+)$", name)
                if match:
                    highest = max(highest, int(match.group(1)))
        experiment_id = f"exp-{highest + 1:03d}"
        path = os.path.join(root, experiment_id)
        os.makedirs(path, exist_ok=False)
    return experiment_id, path


def validate_experiment_id(experiment_id: str) -> None:
    if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
        raise ValueError("experiment_id 必须使用 exp-NNN 格式")


def experiment_dir(target: str, experiment_id: str) -> str:
    validate_experiment_id(experiment_id)
    return os.path.join(experiments_dir(target), experiment_id)


def metadata_path(target: str, experiment_id: str) -> str:
    return os.path.join(experiment_dir(target, experiment_id), "experiment.json")


def write_json(path: str, value: dict) -> None:
    temp_path = path + f".{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def active_path(target: str) -> str:
    return os.path.join(experiments_dir(target), ".active")


# ── 实验槽（slot）：一人一份，而不是全目标抢一个 .active ──────────────────────
# 实测（会话 efab1548 / c6d8fa59 / c63bb02b，9/22–9/24）：主 agent 一条消息并发派多个
# deepdive 是常态，而"同一 target 只能有一个 active 实验"让它们在小事上互相踩：
# 子 transcript 里 `已有 active experiment` 出现 15–39 次/会话，并出现 3 次
# `deactivate <target> exp-NNN`（把别人正在用的实验静默踢下线，其间它的请求归因悬空）。
#
# 修法不是"加锁排队"（那是让 LLM 等），而是**让槽位不再共享**：
#   worker=None → 全局槽 `.active`（主 agent 的旧行为，一字未改）
#   worker=W    → 专属槽 `.active.<safe W>`（探索者用自己的 intent ID 当 W）
# 每个 worker 的 create/activate/resolve/deactivate 只碰自己的槽，冲突从"错误"降级为
# "不存在"。W 的来源：deepdive.py 的邻域文件与派发 prompt 都会给出 intent ID。
# worker 标识 → 文件名安全形态。**必须去掉冒号**：worker 用 intent ID（`h:fe2510`），
# 而 `:` 在 Windows 文件名里非法（os.replace 直接 WinError 87 —— 实测踩到）。
WORKER_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def safe_worker(worker: str) -> str:
    """worker 标识 → 文件名安全形态（`h:fe2510` → `h_fe2510`）。"""
    return WORKER_SAFE_RE.sub("_", (worker or "").strip())


def slot_path(target: str, worker: str = None) -> str:
    """槽文件路径。worker 为空 → 全局槽（与旧行为完全一致）。"""
    if not worker:
        return active_path(target)
    return os.path.join(experiments_dir(target), f".active.{safe_worker(worker)}")


def _read_slot(path: str):
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = file.read().strip()
    except OSError:
        return None
    return value or None


def _slot_holder(target: str, worker: str = None):
    """**只**看这个 worker 自己的槽（不回落全局槽），并做 closed 惰性清理。

    与 resolve_experiment_id 的区别有两处，都关键：
      - 激活/冲突判定/describe 用本函数 —— 带 --worker 就是要隔离，别人占着全局槽与我无关；
        请求归因才用 resolve（worker 槽 → 全局槽回落），保持"没自己的槽就跟着全局"的旧行为。
      - 读到 closed 实验 → 删槽文件并返回 None。否则 close 之后槽里留着"自己的、已关闭的
        实验"，下一次 create --activate 会被判"被占用"（2026-09-25 审计实测的假冲突：
        提示让调用方永远改用 --experiment-id，却关不掉那个已关闭实验）。
    """
    path = slot_path(target, worker)
    experiment_id = _read_slot(path)
    if not experiment_id:
        return None
    try:
        validate_experiment_id(experiment_id)
        metadata = load_experiment(target, experiment_id)
    except (OSError, ValueError, json.JSONDecodeError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    if metadata.get("closed"):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return experiment_id


def _write_slot(path: str, experiment_id: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + f".{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        file.write(experiment_id)
    os.replace(temp_path, path)


def activate_experiment(target: str, experiment_id: str, worker: str = None) -> dict:
    metadata = load_experiment(target, experiment_id)
    if metadata.get("closed"):
        raise ValueError("experiment 已关闭")
    current = _slot_holder(target, worker) if worker else resolve_experiment_id(target, worker=worker)
    if current is not None and current != experiment_id:
        raise ValueError(f"已有 active experiment: {current}")
    _write_slot(slot_path(target, worker), experiment_id)
    return {"target": target, "experiment_id": experiment_id, "active": True,
            "slot": safe_worker(worker) or "global"}


def deactivate_experiment(target: str, experiment_id: str = None, worker: str = None) -> dict:
    """清空**自己的**槽。

    归属守卫（仅在带 --worker 时可判定）：worker 槽里挂的实验若由别人创建
    （metadata.worker 存在且不是本人），拒绝下线并说明。不带 --worker 的 deactivate
    是全局槽管理行为（调用方身份不可知），守卫无从判断 —— 与这里一致，只动全局槽。
    实测案例（c6d8fa59 exp-006）：`deactivate <target> exp-NNN` 把别的探索者正在用的
    实验静默踢下线，其间它的请求归因悬空 2.5 分钟。
    """
    path = slot_path(target, worker)
    current = _read_slot(path)
    removed = False
    if current is not None and (experiment_id is None or current == experiment_id):
        owner = ""
        try:
            owner = (load_experiment(target, current) or {}).get("worker") or ""
        except (OSError, json.JSONDecodeError):
            owner = ""
        if owner and worker and safe_worker(owner) != safe_worker(worker):
            print(
                f"[experiment] REJECTED deactivate: 槽 {safe_worker(worker)} 当前挂的是 "
                f"{current}（属于 worker={owner}）。不要下线别人的实验；"
                f"要并行就带 --worker 用自己的槽，或直接在请求上带 --experiment-id。",
                file=sys.stderr,
            )
            return {"target": target, "experiment_id": current, "active": True, "removed": False}
        os.remove(path)
        removed = True
    result = {"target": target, "experiment_id": current, "active": not removed and current is not None,
              "slot": safe_worker(worker) or "global", "removed": removed}
    if not removed and worker:
        # 自己的槽是空的 —— 顺手说清"别处的槽现在挂着谁"，否则读者会以为
        # 自己的请求被记到了那个实验上（实测归因混乱的常见来源）。
        elsewhere = _read_slot(active_path(target))
        if elsewhere:
            result["note"] = (
                f"你的槽（{safe_worker(worker)}）是空的；全局槽现在挂着 {elsewhere}"
                f"（别人的实验）。要隔离就用自己的槽：activate --worker {worker}"
            )
    return result


def resolve_experiment_id(target: str, explicit_id: str = None, worker: str = None):
    """请求归因用：显式 id > 本 worker 的槽 > 全局槽。"""
    if explicit_id is not None:
        return explicit_id
    candidates = [slot_path(target, worker)] if worker else []
    candidates.append(active_path(target))
    for path in candidates:
        experiment_id = _read_slot(path)
        if not experiment_id:
            continue
        try:
            validate_experiment_id(experiment_id)
            metadata = load_experiment(target, experiment_id)
        except (OSError, ValueError, json.JSONDecodeError):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if metadata.get("closed"):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        return experiment_id
    return None


def load_experiment(target: str, experiment_id: str) -> dict:
    path = metadata_path(target, experiment_id)
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def describe_active_experiment(target: str, worker: str = None) -> dict:
    """只读：返回当前 active 实验的 id 与 hypothesis（无则 active=None）。

    用途（deepdive「复用优先」）：派发前需要判断「是否已有 active 实验、其 hypothesis
    是否与本次一致」。此前没有任何读接口，只能手读 .active + experiment.json 再语义比较。
    不判断 hypothesis 是否"相同"——那是调用方的语义工作，这里只交事实。

    槽语义（2026-09-25 审计修正）：
      - 不带 worker → 只看全局槽（主 agent 视角）。
      - 带 worker   → **只看自己的槽**，与本函数原 docstring 承诺一致；
        此前误用 resolve（会回落全局槽）实测把别人的 exp-001 报成 `slot=h_aaaaaa`，
        探索者据此"复用"就会把请求/结论挂到别人的实验上 —— 正是槽隔离要消灭的归因串扰。
        自己槽为空时附 `global_active` 字段（只读窥探、不清理），让读者知道别处有人。

    注意：_slot_holder 在槽指针失效/指向 closed 实验时会顺手清槽（清理无效状态，非改语义）。
    """
    slot = safe_worker(worker) or "global"
    if worker:
        experiment_id = _slot_holder(target, worker)
        if experiment_id is None:
            return {"target": target, "active": None, "hypothesis": None,
                    "created_at": None, "slot": slot,
                    "global_active": _read_slot(active_path(target))}
        try:
            metadata = load_experiment(target, experiment_id)
        except (OSError, json.JSONDecodeError):
            return {"target": target, "active": experiment_id, "hypothesis": None,
                    "created_at": None, "slot": slot,
                    "global_active": _read_slot(active_path(target))}
        return {
            "target": target,
            "active": experiment_id,
            "hypothesis": metadata.get("hypothesis"),
            "created_at": metadata.get("created_at"),
            "slot": slot,
            "worker": metadata.get("worker"),
            "global_active": _read_slot(active_path(target)),
        }
    experiment_id = resolve_experiment_id(target)
    if experiment_id is None:
        return {"target": target, "active": None, "hypothesis": None, "created_at": None,
                "slot": slot}
    try:
        metadata = load_experiment(target, experiment_id)
    except (OSError, json.JSONDecodeError):
        return {"target": target, "active": experiment_id, "hypothesis": None, "created_at": None,
                "slot": slot}
    return {
        "target": target,
        "active": experiment_id,
        "hypothesis": metadata.get("hypothesis"),
        "created_at": metadata.get("created_at"),
        "slot": slot,
        "worker": metadata.get("worker"),
    }


def relative_path(target: str, path: str) -> str:
    target_root = os.path.join(RESULTS_DIR, safe_target(target))
    return os.path.relpath(path, target_root).replace("\\", "/")


def create_experiment(
    target: str,
    hypothesis: str = None,
    action: str = None,
    experiment_id: str = None,
    activate: bool = False,
    related_memory_hypothesis: str = None,
    worker: str = None,
) -> dict:
    """新建实验。

    `--activate` 撞上"别人的槽已有 active"时**不再报错退出**（实测：子 transcript 里
    这条错误出现 15–39 次/会话，每次都要 LLM 自己想补救动作）。现在改为：
    实验照建，`active=false`，并把**补救动作原文**放进返回值 —— 调用方只需要复制一行。

    带 `--worker W` 时不冲突：每个 worker 有自己的槽（见 slot_path）。
    """
    if activate and not hypothesis:
        raise ValueError("active experiment 必须提供 hypothesis")
    conflict_slot = None
    if activate:
        # 带 worker：只看自己的槽（隔离语义，别人占着全局槽与我无关）；
        # 不带 worker：看全局槽（旧行为）。
        conflict_slot = _slot_holder(target, worker) if worker else resolve_experiment_id(target)
    if experiment_id is None:
        experiment_id, path = reserve_experiment_dir(target)
    else:
        path = experiment_dir(target, experiment_id)
        os.makedirs(path, exist_ok=False)

    created_at = now_iso()
    metadata = {
        "experiment_id": experiment_id,
        "target": target,
        "created_at": created_at,
        "closed_at": None,
        "hypothesis": hypothesis,
        "action": action,
        "related_memory_hypothesis": related_memory_hypothesis,
        "worker": worker,
        "observations": [],
        "interpretations": [],
        "conclusion": None,
        "status": "unknown",
        "closed": False,
    }
    write_json(os.path.join(path, "experiment.json"), metadata)

    if hypothesis is not None:
        with open(os.path.join(path, "hypothesis.md"), "w", encoding="utf-8") as file:
            file.write(hypothesis.rstrip() + "\n")

    # F0：登记最近实验编号（closed 后仍可被引用，故 close 不清除记录）。record 全静默。
    turn_state.record(target, "exp", experiment_id)

    result = {
        "experiment_id": experiment_id,
        "path": relative_path(target, path),
    }
    if not activate:
        return result

    if conflict_slot is None:
        activate_experiment(target, experiment_id, worker)
        result.update({"active": True, "slot": safe_worker(worker) or "global"})
        return result

    # 槽被占用 → 不抢、不报错，给出可直接复制的下一步。
    result.update({
        "active": False,
        "active_slot": conflict_slot,
        "hint": (
            f"本次实验已创建但未激活（{conflict_slot} 正被占用）。"
            f"后续请求请显式锚定：--experiment-id {experiment_id}"
            f"（或带 --worker <你的标识> 让本工具管理专属槽）"
        ),
    })
    print(f"[experiment] {result['hint']}", file=sys.stderr)
    return result


def append_observation(
    target: str,
    experiment_id: str,
    observation: str,
    artifact: str = None,
) -> dict:
    metadata = load_experiment(target, experiment_id)
    if metadata.get("closed"):
        raise ValueError("experiment 已关闭")

    obs_id = f"obs-{len(metadata['observations']) + 1:03d}"
    entry = {
        "id": obs_id,
        "recorded_at": now_iso(),
        "observation": observation,
        "artifact": artifact,
    }
    metadata["observations"].append(entry)
    write_json(metadata_path(target, experiment_id), metadata)

    output_path = os.path.join(experiment_dir(target, experiment_id), "output.txt")
    with open(output_path, "a", encoding="utf-8") as file:
        file.write(f"[{entry['recorded_at']}]\n{observation.rstrip()}\n")
        if artifact:
            file.write(f"artifact: {artifact}\n")
        file.write("\n")

    return {
        "experiment_id": experiment_id,
        "observation_id": obs_id,
        "observation_count": len(metadata["observations"]),
    }


def add_interpretation(
    target: str,
    experiment_id: str,
    observation_id: str,
    classification: str,
    hypothesis_changed: bool,
    reason: str = None,
) -> dict:
    """记录 LLM 对某条 observation 的认知解释（Interpretation 层，显式动作）。

    - classification 四值：confirmed / contradicted / new_observation / no_information
    - 只落盘认知判断，不参与 Evidence Gate，不自动生成 DELTA（压缩权在 LLM 的 merge/close）
    - observation 语义不变：interpretation 是独立数组，不污染物理事实
    """
    if classification not in VALID_CLASSIFICATIONS:
        raise ValueError(f"classification 必须是 {', '.join(VALID_CLASSIFICATIONS)} 之一")
    metadata = load_experiment(target, experiment_id)
    if metadata.get("closed"):
        raise ValueError("experiment 已关闭")
    if not any(o.get("id") == observation_id for o in metadata.get("observations", [])):
        raise ValueError(f"observation 不存在: {observation_id}")

    entry = {
        "observation_id": observation_id,
        "classification": classification,
        "hypothesis_changed": bool(hypothesis_changed),
        "reason": reason,
        "created_at": now_iso(),
    }
    metadata.setdefault("interpretations", []).append(entry)
    write_json(metadata_path(target, experiment_id), metadata)

    return {
        "experiment_id": experiment_id,
        "observation_id": observation_id,
        "classification": classification,
        "hypothesis_changed": entry["hypothesis_changed"],
        "interpretation_count": len(metadata["interpretations"]),
    }


def _load_merge_module():
    """按需加载 merge 模块（与 assemble 相同的导入回退模式，无循环依赖）。"""
    try:
        from runtime import merge  # noqa: F401
        return merge
    except ImportError:
        runtime_dir = os.path.join(PROJECT_ROOT, "runtime")
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        import merge  # noqa: F401
        return merge


def _parse_memory_update(raw: str):
    """解析 --memory-update JSON；非法输入返回 None（不阻断 close）。"""
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except (json.JSONDecodeError, TypeError):
        pass
    print(f"[experiment] WARN --memory-update 非合法 JSON: {str(raw)[:80]}", file=sys.stderr)
    return None


def _apply_memory_update(
    lines: list,
    experiment_id: str,
    state: str,
    text: str,
    evidence: list,
    merge,
) -> tuple:
    """纯内存变换：把一条 close 结果写进 lines。**不读盘、不落盘、不取锁。**

    拆出来的理由：`_sync_memory` 必须把「读 → 改 → 写」放进 memory 事务锁里，
    而这段变换本身没有 IO —— 分开后锁的临界区只剩内存操作 + 一次写，持锁更短。

    返回 (lines, hypothesis_line)。
    """
    # 查重 / 替换：同 text 的 hypothesis 行已存在 → 替换状态；否则 ADD
    # confirmed/contradicted 行自动附证据引用（保证含本实验 exp-NNN）——满足 merge Evidence Gate
    _ref_pattern = re.compile(r"^(?:req_\d+|exp-\d+|art-\d+)$")
    evidence_refs = [e for e in evidence if _ref_pattern.fullmatch(e)]
    if state in ("confirmed", "contradicted"):
        evidence_refs.append(experiment_id)
        evidence_refs = list(dict.fromkeys(evidence_refs))
    tail = f" ({', '.join(evidence_refs)})" if evidence_refs else ""
    hypothesis_line = f"hypothesis [{state}]: {text}{tail}"

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("hypothesis [") and "]:" in stripped:
            content = stripped.split("]:", 1)[1].strip()
            # 归一化用 graph.EVIDENCE_BLOCK_RE（**全角 `（）` 也在内**）。
            # 旧实现只认半角 → 全角证据的行匹配不上 → 同一假设被 ADD 成第二行。
            norm = EVIDENCE_BLOCK_RE.sub("", content).strip()
            if content == text or norm == text:
                lines[i] = hypothesis_line + "\n"
                break
    # F.5-④: 精确匹配失败 → ADD + stale 候选提示（锚点≠同一判断，不自动消解旧行）
    lines = merge.add_line(lines, hypothesis_line)

    # evidence 引用：已有含本实验编号的 artifact 行 → 替换；否则 ADD
    if evidence:
        artifact_line = "artifact: " + experiment_id + "; " + ", ".join(evidence)
        replaced_art = False
        for i, line in enumerate(lines):
            if line.strip().startswith("artifact:") and experiment_id in line:
                lines[i] = artifact_line + "\n"
                replaced_art = True
                break
        if not replaced_art:
            lines = merge.add_line(lines, artifact_line)

    return lines, hypothesis_line


def _sync_memory(
    target: str,
    experiment_id: str,
    status: str,
    memory_update: dict,
    artifacts: list,
) -> dict | None:
    """close 后把 LLM 显式声明的探索结果机械写回 memory。

    Runtime 只做三件事：
      1. target 校验（不一致 → 写 results/<safe>/memory.md 归档，不污染 active）
      2. 查重 / 替换（同 text 行已存在 → 更新状态；否则 ADD，见 _apply_memory_update）
      3. 写入 memory
    禁止：从 conclusion 抽取语义、判断漏洞类型、自动生成关系。

    memory_update 结构（由 LLM 提供，见 --memory-update）：
      {"text": "<探索后的世界变化>", "status": "confirmed", "evidence": ["req_747"]}
    text 是探索后的结论（不是 create 时的探索起点假设）。
    status 缺省用实验最终状态映射；evidence 缺省用实验观察里登记的 artifact。

    **这是 memory 的第二条写入路径**（第一条是 merge 的 delta 应用），
    与它共用同一把 `merge.memory_transaction()` 锁 —— 两条路径并发时（agent 常把
    "记一笔账 + 关一个实验"放在同一条消息的两次并行 Bash 调用里）无锁会丢更新。
    写回 active 时还会回吐决策摘要（见 merge.emit_decision_digest）。
    """
    if not memory_update:
        return None
    text = (memory_update.get("text") or "").strip()
    if not text:
        print("[experiment] WARN --memory-update 缺少 text，跳过 memory 同步", file=sys.stderr)
        return None

    state = memory_update.get("status") or MEMORY_STATUS_MAP.get(status, "observed")
    if state not in VALID_MEMORY_STATES:
        state = "observed"

    evidence = memory_update.get("evidence") or artifacts or []
    if isinstance(evidence, str):
        evidence = [evidence]
    evidence = [str(e).strip() for e in evidence if str(e).strip()]

    merge = _load_merge_module()

    with merge.memory_transaction():
        lines = merge.read_memory()
        before_zone = merge.decision_zone(lines)

        active_body = merge.extract_active_target(lines) if hasattr(merge, "extract_active_target") else ""
        active_id = merge.target_identity(active_body) if active_body else ""
        target_id = merge.target_identity(target)

        # 1) target 校验：与 active 不一致 → 落到该 target 归档文件
        dest = None
        if active_id and target_id and active_id != target_id:
            safe = merge.target_safe_name(target)
            dest = os.path.join(RESULTS_DIR, safe, "memory.md")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                with open(dest, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            else:
                lines = []

        # 2) 变换（纯内存）
        lines, hypothesis_line = _apply_memory_update(
            lines, experiment_id, state, text, evidence, merge
        )

        # 3) 写入
        if dest is not None:
            with open(dest, "w", encoding="utf-8") as f:
                f.write("".join(lines))
            echo = False  # 写的是归档，调用方的 active 世界没变 → 不回吐
        else:
            merge.write_memory(lines)
            echo = True

    if echo:
        merge.emit_decision_digest(lines, before_zone)

    return {
        "memory_synced": True,
        "line": hypothesis_line,
        "evidence_count": len(evidence),
        "written_to": "active" if dest is None else "archive",
    }


def close_experiment(
    target: str,
    experiment_id: str = None,
    conclusion: str = None,
    status: str = "unknown",
    memory_update: dict = None,
    worker: str = None,
) -> dict:
    """关闭实验。worker 语义与 create/activate 对齐（2026-09-25 审计补齐）：

      - 不带 worker：全局槽（主 agent 旧行为）——显式 exp-NNN 时 resolve 直接取它。
      - 带 worker 且未给 exp-NNN：**只看自己的槽**（不回落全局）。此前 resolve 不带
        worker，探索者 `close` 不带 id 会：全局槽空 → 报"没有可关闭的 active"（自己的
        实验关不掉）；全局槽有人 → **关掉别人的实验**（实测）。
      - deactivate 带 worker → 关完清自己的槽（此前只清全局，自己槽残留 closed 实验，
        下次 create --activate 被假冲突挡住；_slot_holder 现也会清，此处显式清更快）。
    """
    if status not in FINAL_STATUSES:
        raise ValueError(f"status 必须是 {', '.join(sorted(FINAL_STATUSES))} 之一")

    if experiment_id is None:
        if worker:
            experiment_id = _slot_holder(target, worker)
            if experiment_id is None:
                raise ValueError(
                    f"槽 {safe_worker(worker)} 里没有 active experiment。"
                    f"带 exp-NNN 显式关闭，或先 create/activate --worker {worker}"
                )
        else:
            experiment_id = resolve_experiment_id(target)
    if experiment_id is None:
        raise ValueError("没有可关闭的 active experiment")
    metadata = load_experiment(target, experiment_id)
    
    # Experiment First Contract: experiment 必须有 observations
    observations = metadata.get("observations", [])
    if not observations:
        raise ValueError(
            f"experiment {experiment_id} 没有 observations，无法 close。"
            f"使用 append 添加至少一条 observation 或使用 http_request --experiment-id。"
        )
    
    metadata.update({
        "closed_at": now_iso(),
        "conclusion": conclusion,
        "status": status,
        "closed": True,
    })
    write_json(metadata_path(target, experiment_id), metadata)
    deactivate_experiment(target, experiment_id, worker)

    artifacts = list(dict.fromkeys(
        entry["artifact"] for entry in metadata["observations"] if entry.get("artifact")
    ))
    evidence_path = os.path.join(experiment_dir(target, experiment_id), "evidence.md")
    with open(evidence_path, "w", encoding="utf-8") as file:
        if conclusion is not None:
            file.write(conclusion.rstrip() + "\n\n")
        file.write(f"status: {status}\n")
        if artifacts:
            file.write("\nartifacts:\n")
            for artifact in artifacts:
                file.write(f"- {artifact}\n")

    memory_sync = _sync_memory(target, experiment_id, status, memory_update, artifacts)

    return {
        "experiment_id": experiment_id,
        "status": status,
        "closed": True,
        "evidence": relative_path(target, evidence_path),
        "artifact_count": len(artifacts),
        "memory_sync": memory_sync,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=(
            "目标级实验事实管理。\n"
            "槽位（slot）：不带 --worker 用全局槽 .active（主 agent 的默认）；\n"
            "带 --worker W 用专属槽 .active.<W>（并行探索者各用各的，互不抢）。\n"
            "冲突不报错：create --activate 撞上别人的槽 → 返回 active=false + 一行"
            "「--experiment-id exp-NNN」补救动作，照抄即可。"
        ),
        epilog=(
            "典型用法：\n"
            "  1) 开实验并激活：experiment.py create <target> --hypothesis \"<要判定的问题>\" --activate\n"
            "     （并行探索者加 --worker h:xxxxxx，用你 intent 的 ID）\n"
            "  2) 探测时锚定：http_request.py <target> --experiment-id exp-007 ...\n"
            "  3) 关实验并写回结论：experiment.py close <target> exp-007 --status contradicted \\\n"
            "       --conclusion \"<结论>\" --memory-update-file <json 路径>\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("target")
    create.add_argument("--experiment-id")
    create.add_argument("--hypothesis")
    create.add_argument("--action")
    create.add_argument("--activate", action="store_true")
    create.add_argument("--related-memory-hypothesis")
    create.add_argument("--worker", help="专属槽标识（并行探索者用，如 h:fe2510）")

    activate = subparsers.add_parser("activate")
    activate.add_argument("target")
    activate.add_argument("experiment_id")
    activate.add_argument("--worker", help="只激活到自己的专属槽")

    deactivate = subparsers.add_parser("deactivate")
    deactivate.add_argument("target")
    deactivate.add_argument("experiment_id", nargs="?")
    deactivate.add_argument("--worker", help="只清自己的专属槽（不会动别人的）")

    active = subparsers.add_parser("active", help="只读：查当前 active 实验（供 deepdive 复用优先）")
    active.add_argument("target")
    active.add_argument("--worker", help="查自己的专属槽（缺省查全局槽）")

    append = subparsers.add_parser("append")
    append.add_argument("target")
    append.add_argument("experiment_id")
    append.add_argument("--observation", required=True)
    append.add_argument("--artifact")

    interpret = subparsers.add_parser("interpret")
    interpret.add_argument("target")
    interpret.add_argument("experiment_id")
    interpret.add_argument("--observation-id", required=True)
    interpret.add_argument("--classification", required=True, choices=VALID_CLASSIFICATIONS)
    interpret.add_argument("--hypothesis-changed", action="store_true",
                           help="本次解释是否改变了假设空间（默认 false）")
    interpret.add_argument("--reason")

    close = subparsers.add_parser("close")
    close.add_argument("target")
    close.add_argument("experiment_id", nargs="?")
    close.add_argument("--conclusion")
    close.add_argument("--status", choices=sorted(FINAL_STATUSES), default="unknown")
    close.add_argument("--worker", help="关自己的专属槽实验（与 create/activate 同语义）")
    close.add_argument(
        "--memory-update",
        type=_parse_memory_update,
        metavar='{"text": "...", "status": "confirmed", "evidence": ["req_747"]}',
        help="close 后机械写回 memory 的探索结果（JSON）。text=结论；status=confirmed/"
        "contradicted/blocked/observed（缺省由 --status 映射）；evidence=证据编号数组",
    )
    close.add_argument(
        "--memory-update-file",
        metavar="PATH",
        help="同 --memory-update，但 JSON 从文件读（**推荐**，零 shell 转义）。"
        "先 Write 到 results/<target>/scripts/_exp_close.json。"
        "内联 JSON 在 shell 里一旦引号被吃就只会 WARN 后静默跳过写回。",
    )

    args = parser.parse_args()

    # `--memory-update-file` 覆盖 `--memory-update`：先 Write 后传路径，
    # 避开「JSON 里既有单引号又有双引号」这一整类 shell 转义事故。
    update_file = getattr(args, "memory_update_file", None)
    if update_file:
        try:
            with open(update_file, "r", encoding="utf-8") as handle:
                args.memory_update = _parse_memory_update(handle.read())
        except OSError as exc:
            print(
                f"[experiment] 读不到 --memory-update-file: {update_file}（{exc}）\n"
                f"        本次 close 不会写回 memory（实验本身仍会关闭）。",
                file=sys.stderr,
            )
            args.memory_update = None
    if args.command == "create":
        result = create_experiment(
            args.target, 
            args.hypothesis, 
            args.action, 
            args.experiment_id, 
            args.activate,
            getattr(args, 'related_memory_hypothesis', None),
            getattr(args, 'worker', None),
        )
    elif args.command == "activate":
        result = activate_experiment(args.target, args.experiment_id, getattr(args, "worker", None))
    elif args.command == "deactivate":
        result = deactivate_experiment(args.target, args.experiment_id, getattr(args, "worker", None))
    elif args.command == "active":
        result = describe_active_experiment(args.target, getattr(args, "worker", None))
    elif args.command == "append":
        result = append_observation(args.target, args.experiment_id, args.observation, args.artifact)
    elif args.command == "interpret":
        result = add_interpretation(
            args.target, args.experiment_id, args.observation_id,
            args.classification, args.hypothesis_changed, args.reason,
        )
    else:
        result = close_experiment(
            args.target,
            args.experiment_id,
            args.conclusion,
            args.status,
            getattr(args, "memory_update", None),
            getattr(args, "worker", None),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (FileExistsError, FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
