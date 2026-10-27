#!/usr/bin/env python3
"""
merge.py — 读取 LLM 输出的 DELTA，对 memory.md 执行纯 CRUD。

原则：
- LLM 不直接写 memory.md。只输出 DELTA。
- merge.py 只做机械操作：ADD / UPDATE / DELETE，以及 target 边界隔离。
- Evidence Gate（机械，claim 级）：[confirmed]/[contradicted] hypothesis 与 vuln:/counter:
  行必须带可解析的 req_NNN/exp-NNN 引用，否则拒绝该行。只保证可追溯，不判断真假。
- 禁止任何"智能"逻辑——不判断根域、不继承 hypothesis、不推理漏洞。
- Runtime 不长脑子。merge.py 是纯机械操作。

World boundary（机械）：
- Active memory 有且只能有一个 active target（一个 world）。
- DELTA 写入新 target: 且与现有 active 不一致时：
  archive(old) → clear(active) → init(new) → 再应用本批其余 ops。
- 不判断公司/根域/是否该继承；跨 target 知识只在 results/<target>/memory.md。

用法：
  echo "DELTA:
  surface: /api/login 存在认证机制
  hypothesis: 认证可被绕过 [0.6]" | python runtime/merge.py
"""

import json
import os
import re
import sys
from contextlib import contextmanager

# 强制 stdin/stdout/stderr 使用 UTF-8，绕过 Windows GBK 系统编码
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 允许环境覆盖（与 scope/credentials/execute/intranet_scan 同一变量；detached worker 继承）
RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(_PROJECT_ROOT, "results")
TURN_STATE_PATH = os.path.join(_PROJECT_ROOT, ".turn_state.json")

try:
    from . import digest
    from . import turn_state
    from .filelock import locked
    from .graph import EVIDENCE_BLOCK_RE, anchors as _graph_anchors, parse as graph_parse
    from .graph import classify as graph_classify, node_id as graph_node_id
    from .paths import world_active, safe_name
    from .world import parse as world_parse
except ImportError:
    import digest
    import turn_state
    from filelock import locked
    from graph import EVIDENCE_BLOCK_RE, anchors as _graph_anchors, parse as graph_parse
    from graph import classify as graph_classify, node_id as graph_node_id
    from paths import world_active, safe_name
    from world import parse as world_parse


# ── memory 事务锁 ────────────────────────────────────────────────────────────
# memory 是**读-改-写**，而写它的有两条路径：本文件的 delta 应用，与
# `experiment.close --memory-update-file` 的写回。两条路径都 read_memory → 改 →
# write_memory，中间没有任何互斥 —— 而"记一笔账 + 关一个实验"是 agent 极常见的
# 同一消息两次调用（并行 Bash），于是后写的那次会把先写的那次整个盖掉（lost update）。
# 加锁实现与 experiment 的实验编号锁、http_request 的请求号锁同一个（filelock.py）。
MEMORY_LOCK_NAME = ".memory.lock"


def memory_lock_path() -> str:
    """memory 事务锁的位置：与 active memory 同目录（谁换 target 都锁同一处）。"""
    return os.path.join(os.path.dirname(world_active()), MEMORY_LOCK_NAME)


@contextmanager
def memory_transaction():
    """把 memory 的「读 → 改 → 写」包成一次互斥事务。

    临界区只做内存里的纯变换 + 一次原子写，不含任何 IO 等待，故持锁时间极短
    （filelock 用 mtime 判陈旧，要求临界区短，见其 docstring）。
    """
    path = memory_lock_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with locked(path):
        yield

# 合法前缀白名单。ADD 操作须以此列表中的任一前缀开头，否则拒绝写入。
# 注意：此列表与 ARCHITECTURE.md §6 + CLAUDE.md 保持同步。
# 认知对象（6 个）：surface, hypothesis, counter, vuln, cred, dead
# 目标档案（4 个）：target, tech, port, artifact
# 特殊标签：future（延后动作/战略传承；投影为 [FRONTIER] 段的 next:，不参与闸门）
# 搜索定向（2 个）：goal（完成条件）/ subgoal（阶段里程碑）。
#   投影首段 [GOAL]；没有它账本只有待办队列、没有终止条件。不参与证据闸门。
# 状态标记（hypothesis 可选）：[observed] [confirmed] [contradicted] [blocked] [unknown]
#   [sweep] = 批量机械验证（枚举/爆破/限流/逐条比对）：未闭合、不需证据，
#             投影单列进 [SWEEP] 段作为待派队列（规格 §8.0g）。
#   例如：hypothesis [observed]: 已知 /api/user/list → 无 Authorization 头 GET → 期望 200 而非 401
#        （因果链写法：<出发事实> → <操作> → <期望得到的事实>，规格 §8.0i）
VALID_PREFIXES = [
    "target:", "tech:", "port:", "artifact:",
    "surface:", "vuln:", "cred:",
    "hypothesis:", "hypothesis [", "counter:", "dead:", "dead [",
    "future:",
    # 搜索的终止条件与阶段里程碑（Cairn_Y 的 Goal/Sub Goal）。
    # 没有它们，账本只有"待办队列"而没有"完成条件"——agent 不会收工。
    "goal:", "subgoal:",
]


def _is_valid_line(line: str) -> bool:
    """检查行是否以合法前缀开头且含内容（严格单行 prefix: content 格式）。

    事务边界防御：拒绝「裸 prefix 行」（如 ``vuln:`` 后无内容）。
    半截 DELTA（session 中断在块结构中段）的典型特征就是裸 prefix 行。
    """
    stripped = line.strip()
    if not stripped:
        return False
    for p in VALID_PREFIXES:
        if stripped.startswith(p):
            # 必须有内容：prefix 之后至少 1 个非空字符
            content = stripped[len(p):].strip()
            if not content:
                print(f"[merge] REJECTED (naked prefix, no content): {stripped[:60]}", file=sys.stderr)
                return False
            return True
    return False


def read_memory() -> list:
    """读取 active world memory 全部行（路径经 paths.world_active）。

    容忍首行 UTF-8 BOM（PowerShell 的 `Set-Content -Encoding UTF8` 会写 BOM）：
    否则第一行变成 `\\ufefftarget: …`，target 解析失败 → 证据引用校验与
    DELETE/UPDATE 的首行匹配全部静默失准。
    """
    path = world_active()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if lines and lines[0].startswith("\ufeff"):
        lines[0] = lines[0].lstrip("\ufeff")
    return lines


def write_memory(lines: list):
    """写回 active world memory（路径经 paths.world_active）。"""
    path = world_active()
    content = "".join(lines).rstrip("\n") + "\n" if lines else ""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def parse_delta(text: str) -> list:
    """
    解析 DELTA: 块，提取所有操作行。
    操作格式：
      surface: <内容>          → ADD
      hypothesis: <内容>       → ADD
      assessment: <内容>       → ADD (替换旧 assessment 见 apply_delta)
      counter: <内容>          → ADD
      [REMOVE] surface: <前缀> → DELETE
      [UPDATE] surface: <前缀> = <新内容> → UPDATE
    """
    ops = []
    in_delta = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.upper().startswith("DELTA:"):
            in_delta = True
            continue
        if not in_delta:
            continue
        if not stripped:
            continue

        action = "ADD"
        content_line = stripped

        # 检测 [REMOVE] 和 [UPDATE] 指令
        if stripped.upper().startswith("[REMOVE]"):
            action = "DELETE"
            content_line = stripped[len("[REMOVE]"):].strip()
        elif stripped.upper().startswith("[UPDATE]"):
            action = "UPDATE"
            content_line = stripped[len("[UPDATE]"):].strip()

        ops.append({"action": action, "line": content_line})

    return ops


# ── Evidence Gate（v0.3.1 World Model Mutation Contract）──
# claim 级约束：进入 world model 的 confirmed/contradicted 结论必须可追溯。
# 仅校验行内 req_NNN/exp-NNN 引用的存在性与可解析性；不检查内容、不判断 claim 真伪。
# observed/blocked/unknown/sweep 是探索态，不设证据要求（探索自由不受限）。
EVIDENCE_REQUIRED_HYPOTHESIS_STATES = ("confirmed", "contradicted")
# dead [reason]: 同样需要证据（不可达结论必须可回放）；"dead [" 必须在 "dead:" 之外单列，
# 因为下游闸是 startswith 元组匹配，"dead [filtered]:" 不匹配 "dead:"。
EVIDENCE_REQUIRED_PREFIXES = ("vuln:", "counter:", "dead:", "dead [")

# dead 拓扑三态（2026-10-06 P0-b，内网）。Runtime 只携带状态不解释含义；
# 三态是扫描器原生分类（open/closed/filtered），不是新发明的认知概念。
#   filtered    = 从我当前位置到不了（ACL/丢包），换立足点后必须重估
#   closed      = 主机可达但端口关闭，不重试
#   unreachable = 主机/网段无路由，族级收口
DEAD_REASONS = ("filtered", "closed", "unreachable")
DEAD_REASON_RE = re.compile(r"^dead\s+\[(\w+)\]\s*:")


# ── 证据引用完整性（账本 FK 约束）──
# 只校验引用可解析（req 响应文件 / exp 实验目录 / art index 登记），红线：不检查内容、不判断是否证明。
EVIDENCE_REF_RE = re.compile(r"req_\d+|exp-\d+|art-\d+")


def _evidence_reference_check(line: str, lines: list):
    """L2: 引用的 req/exp 是否可解析。返回不可解析的引用列表；全部可解析返回空。
    F.5-③: world 目录经 resolve_world_dir 统一解析（兼容历史多套命名），
    不再假定 target_safe_name 是唯一目录身份。"""
    refs = EVIDENCE_REF_RE.findall(line)
    if not refs:
        return []
    target = ""
    for l in lines:
        if l.strip().startswith("target:"):
            target = l.strip()[len("target:"):].strip()
            break
    if not target:
        return []
    safe = resolve_world_dir(target)
    base = os.path.join(RESULTS_DIR, safe)
    unresolved = []
    for ref in refs:
        if ref.startswith("req_"):
            resp = os.path.join(base, "responses")
            if not (os.path.exists(os.path.join(resp, ref + ".txt"))
                    or os.path.exists(os.path.join(resp, ref + ".json"))):
                unresolved.append(ref)
        elif ref.startswith("exp-"):
            if not os.path.isdir(os.path.join(base, "experiments", ref)):
                unresolved.append(ref)
        elif ref.startswith("art-"):
            index_file = os.path.join(base, "artifacts", "index.json")
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    index = json.load(f)
                if ref not in index:
                    unresolved.append(ref)
            except (OSError, json.JSONDecodeError):
                unresolved.append(ref)
    return unresolved


def _warn_evidence_integrity(line: str, lines: list) -> None:
    """L2 HIGH WARN：引用不可解析。L3（可解析）静默停止，不再深入。"""
    unresolved = _evidence_reference_check(line, lines)
    if unresolved:
        print(
            f"[merge] WARN HIGH 证据引用不可解析: {', '.join(unresolved)} — {line.strip()[:80]}",
            file=sys.stderr,
        )


def evidence_gate(line: str, lines: list) -> tuple:
    """Evidence Gate：confirmed/contradicted hypothesis 与 vuln:/counter: 行
    必须带 ≥1 个 exp-NNN 引用（confirmed/contradicted 必须通过 experiment），
    且引用可解析（experiments 目录存在）。
    红线：只保证 traceability，不判断 claim 真伪。返回 (ok, reason)。"""
    stripped = line.strip()
    requires = False
    state_match = re.match(r"^hypothesis \[(\w+)\]", stripped)
    if state_match:
        state = state_match.group(1)
        requires = state in EVIDENCE_REQUIRED_HYPOTHESIS_STATES
        # confirmed/contradicted 必须有 exp-NNN（Experiment First Contract）
        if state in ("confirmed", "contradicted"):
            refs = EVIDENCE_REF_RE.findall(stripped)
            has_exp = any(ref.startswith("exp-") for ref in refs)
            if not has_exp:
                return False, f"{state} 必须通过 experiment（需要 exp-NNN）；observed/blocked 可用 req_NNN 快速探测"
    elif stripped.startswith(EVIDENCE_REQUIRED_PREFIXES):
        requires = True
    if not requires:
        return True, ""
    # dead [reason] 的 reason 白名单（在查证据前先拦非法态，回执给三选一）
    m = DEAD_REASON_RE.match(stripped)
    if m and m.group(1) not in DEAD_REASONS:
        return False, f"dead 原因必须是 {DEAD_REASONS} 之一（filtered=位置不可达/closed=端口关/unreachable=无路由）"
    refs = EVIDENCE_REF_RE.findall(stripped)
    if not refs:
        return False, "缺证据引用（confirmed/contradicted 需 exp-NNN，vuln/counter/dead 需 req/exp/art）"
    unresolved = _evidence_reference_check(stripped, lines)
    if unresolved:
        return False, f"证据引用不可解析: {', '.join(dict.fromkeys(unresolved))}"
    return True, ""


def _age_zh(age_seconds: int) -> str:
    """秒数 → 中文短时长（纯机械格式化）。"""
    if age_seconds < 120:
        return f"{age_seconds}秒"
    if age_seconds < 3600:
        return f"{age_seconds // 60}分钟"
    return f"{age_seconds // 3600}小时"


def _evidence_hint(lines: list) -> str:
    """F0：证据闸 REJECT 时给「最近证据编号」的存在性提示。

    红线（设计稿 §4.1 三重机械约束）：
    - 只读 turn_state 的最近记录 + 全局 active 实验槽；
    - 不按被拒行文本做任何匹配（行里写 req_999 也不影响提示内容）；
    - 固定非背书措辞。无任何可提示内容时返回 ""（回执退化为原样）。
    """
    target_body = extract_active_target(lines)
    if not target_body:
        return ""
    # 记录侧键 = safe_name(工具入参 target)；账本侧可能带 URL 壳/历史目录名，
    # 两个键都试：现存世界目录名（resolve_world_dir）+ 身份转换名（target_safe_name）。
    try:
        keys = [resolve_world_dir(target_body), target_safe_name(target_body)]
    except Exception:
        keys = [target_safe_name(target_body)]
    try:
        rec = turn_state.recent(*keys)
    except Exception:
        rec = {}
    parts = []
    label = {"req": "请求", "art": "执行产物", "exp": "实验"}
    for kind in ("req", "art", "exp"):
        item = rec.get(kind) if rec else None
        if item:
            ref, age = item
            parts.append(f"{label[kind]} {ref}（{_age_zh(age)}前）")
    # active 实验槽：confirmed/contradicted 缺 exp-NNN 时可直接照抄。
    try:
        from experiment import resolve_experiment_id as _resolve_exp
        active_exp = _resolve_exp(target_body) or ""
    except Exception:
        active_exp = ""
    if active_exp and not any(active_exp in part for part in parts):
        parts.append(f"active 实验 {active_exp}")
    if not parts:
        return ""
    return (
        "  最近证据（仅编号存在提示，不代表支持本结论；30 分钟内）: "
        + " · ".join(parts) + "\n"
        "  修法：证据确属本结论 → 行尾加 (req_NNN)/(exp-NNN)；"
        "confirmed/contradicted 须走 experiment；不支持就先 create 实验再测，勿硬挂编号。"
    )


# ── v0.3.1 第三刀：hypothesis ledger update semantics ──
# memory 是账本不是日志：同 target 内同 normalized text 的 hypothesis 行 → 更新（状态升级），
# 不同文本 → 新条目。不是清垃圾，是账本语义。
HYPOTHESIS_PREFIX_RE = re.compile(r"^hypothesis(?:\s+\[[a-zA-Z]+\])?:\s*(.*)$", re.IGNORECASE)


def _normalize_hypothesis_text(line: str) -> str:
    """去掉状态标记、证据引用块、压缩空白——仅用于同 target 账本内比较。"""
    m = HYPOTHESIS_PREFIX_RE.match(line.strip())
    if not m:
        return ""
    text = EVIDENCE_BLOCK_RE.sub("", m.group(1))
    return re.sub(r"\s+", " ", text).strip()


def _hypothesis_ledger_update(lines: list, new_line: str):
    """同 normalized text 的已有 hypothesis 行 → 替换为 new_line（保留新状态+证据）。
    返回被替换行下标；无精确匹配返回 None。"""
    norm = _normalize_hypothesis_text(new_line)
    if not norm:
        return None
    for i, existing in enumerate(lines):
        if not existing.strip().startswith("hypothesis"):
            continue
        if _normalize_hypothesis_text(existing) == norm:
            lines[i] = new_line.rstrip("\n") + "\n"
            return i
    return None


# ── F.5-①/④: hypothesis stale 候选提示（不自动合并）──
#   （锚点提取已迁至 graph.anchors —— 单一实现；memory 无显式边，
#    "相邻"由共享标识符定义，与 graph.neighborhood 共用同一套。）
# 根因：agent 迁移状态时改写文本，全文相等匹配失败 → 旧 observed 行滞留。
# 但锚点只证明「关于同一 endpoint/标识符」，不证明「同一个判断」：
# captcha 可复用 与 SQL 注入 共享 /api/login 却是两条不同假设，自动合并会静默吞假设。
# 判决（Patch 审计）：Runtime 记录 LLM 已决定的状态，不替 LLM 判断世界状态。
#   → 锚点匹配不再自动迁移，只发 STALE CANDIDATE 提示，双行保留，由 LLM 显式 [REMOVE]/UPDATE 收口。
# 保守判据（只用于挑候选，宁多提示不误合）：
#   锚点 = 完整路径（/api/login，非尾段切片，避免 /sso/login 误配）或特定形态标识符
#   （camelCase/snake_case/含数字，如 getDocumentUrlByKey、check_session、searchView）；普通英文词不作锚点。
#   仅当新旧行共享一个「在全部旧 hypothesis 行中恰好出现一次」的锚点，且新行为终态、
#   旧行为非终态（升级方向）→ 提示候选。歧义/无交集/降级 → 不提示。
# 锚点提取的**单一实现**在 graph.anchors（本文件不再各写一份）。
# 此处固定 strict=True：merge 的用途是挑 STALE CANDIDATE 候选，
# 误报会诱导 LLM 合并两条不同假设，故必须宁缺勿滥（邻域投影用宽口径，见 graph）。
def _hypothesis_anchors(norm_text: str) -> frozenset:
    return _graph_anchors(norm_text, strict=True)


def _hypothesis_state(line: str) -> str:
    m = re.match(r"^hypothesis\s*\[(\w+)\]", line.strip())
    return m.group(1) if m else ""


def _hypothesis_stale_candidates(lines: list, new_line: str) -> list:
    """终态新行与非终态旧行共享强锚点 → 返回旧行下标列表。仅用于提示，不合并。
    审计判决：锚点只证明「关于同一 endpoint/标识符」，不证明「同一个判断」
    （/api/login captcha 可复用 vs /api/login SQL 注入 = 两条假设）。
    Runtime 不替 LLM 决定旧假设是否消解：双行保留 + stderr 提示，
    确属同一假设的升级由 LLM 显式 [REMOVE] 旧行或 UPDATE 收口。"""
    new_norm = _normalize_hypothesis_text(new_line)
    if not new_norm:
        return []
    new_anchors = _hypothesis_anchors(new_norm)
    if not new_anchors:
        return []
    new_state = _hypothesis_state(new_line)
    if new_state not in ("confirmed", "contradicted", "blocked"):
        return []  # 非终态新行：不构成 stale 场景

    cands = []
    for i, l in enumerate(lines):
        if not l.strip().startswith("hypothesis"):
            continue
        if _hypothesis_state(l) not in ("", "observed", "unknown", "sweep"):
            continue  # 旧行已是终态：无 stale 可言
        if new_anchors & _hypothesis_anchors(_normalize_hypothesis_text(l)):
            cands.append(i)
    return cands


CRED_GATE_HINT = (
    "cred 行疑似含明文/哈希/密钥；凭据值不进账本与投影。\n"
    "  1) python runtime/credentials.py put <target> --from-file <值文件> --note \"<身份>\"\n"
    "  2) 账本写：cred: c-NNN <身份> @<范围> 来源=<主机/途径> (exp-NNN 或 art-NNN)\n"
    "  3) 用时 Read results/<target>/credentials/c-NNN（投影永不显示值）"
)


def cred_gate(line: str) -> tuple:
    """凭据闸（2026-10-06 P0-a）：cred: 引用行放行；含秘密形态 → REJECT。

    引用行（`cred: c-NNN 身份 ...`）只豁免 user:secret 启发式（标识符/邮箱容易误伤），
    **不豁免无歧义形态**（32/64hex 哈希、JWT、PEM）——引用行里粘贴哈希一样 REJECT。
    检测形态单一实现在 credentials，禁止在此重写正则。
    """
    stripped = line.strip()
    if not stripped.startswith("cred:"):
        return True, ""
    body = stripped[len("cred:"):].strip()
    try:
        from credentials import looks_like_secret, strip_ledger_prefix, SECRET_PATTERNS
    except ImportError:
        from .credentials import looks_like_secret, strip_ledger_prefix, SECRET_PATTERNS
    if re.match(r"^c-\d{3,}\b", body):
        # 引用身份合法；但行内仍不得含哈希/JWT/私钥这类无歧义秘密
        bare = strip_ledger_prefix(stripped)
        if any(pat.search(bare) for pat in SECRET_PATTERNS):
            return False, CRED_GATE_HINT
        return True, ""
    if looks_like_secret(stripped):
        return False, CRED_GATE_HINT
    return True, ""


def _engagement_hint(new_line: str, lines: list) -> None:
    """surface 行含私网字面量但当前 target 无 scope.json → 当场 WARN（不阻断）。

    把「engagement 约定（主机写进行内、先声明 scope）」的检测从事后提前到记账当场。
    纯机械：严格 IP 正则 + scope 文件存在性；不解析语义、不判是否真在打内网。
    单个内网 Web 站（不跨主机）可忽略。
    """
    stripped = new_line.strip()
    if not stripped.startswith("surface:"):
        return
    try:
        from scope import extract_ip_literals, is_private_literal, has_scope
    except ImportError:
        from .scope import extract_ip_literals, is_private_literal, has_scope
    target = extract_active_target(lines)
    if not target:
        return
    if has_scope(target):
        return
    for token in extract_ip_literals(stripped):
        if is_private_literal(token):
            print(
                "[merge] WARN: surface 行含内网地址但当前 target 无 scope.json。\n"
                "  若这是授权内网演练：python runtime/scope.py init <engagement> --allow <CIDR...>\n"
                "  （主机/端口/凭据写进行内，target 用 engagement 名；见 docs/内网改造设计_v1）\n"
                "  若是单个内网 Web 站（不跨主机）：可忽略本提示。",
                file=sys.stderr,
            )
            return


# 行长 WARN（P1-b）：投影密度保护。结论行 >240 字符会在投影 brief(130) 后挤占同段其他
# claim 的可见位。WARN 不 REJECT（遗产兼容）；过程细节应放进引用的 exp/req/art 全文。
LONG_LINE_WARN_CHARS = 240
_LONG_LINE_EXEMPT_PREFIXES = ("surface:",)  # surface 是事实行，允许较长技术描述


def _long_line_warn(new_line: str) -> None:
    stripped = new_line.strip()
    if any(stripped.startswith(p) for p in _LONG_LINE_EXEMPT_PREFIXES):
        return
    if len(stripped) > LONG_LINE_WARN_CHARS:
        print(
            f"[merge] WARN: 该行 {len(stripped)} 字符（>{LONG_LINE_WARN_CHARS}）；"
            "结论应 ≤2 行，过程/边界放进它引用的 exp-NNN/req/art 全文。",
            file=sys.stderr,
        )


def add_line(lines: list, new_line: str):
    """ADD：追加到 memory 末尾。非 prefix: content 格式的行拒绝写入。
    hypothesis 行：精确匹配（同 normalized text）→ 状态更新；否则追加，
    并在存在共享锚点的非终态旧行时提示 stale 候选（不自动合并，消解由 LLM 显式收口）。
    Evidence Gate：confirmed/contradicted 与 vuln:/counter:/dead 无可解析证据引用 → 拒绝该行。
    Cred Gate：cred: 含明文/哈希/密钥 → 拒绝该行（值只许进 credentials/）。"""
    if not _is_valid_line(new_line):
        preview = new_line.strip()[:60]
        print(f"[merge] REJECTED (invalid prefix): {preview}", file=sys.stderr)
        return lines
    ok, reason = cred_gate(new_line)
    if not ok:
        print(f"[merge] REJECTED (cred gate): {new_line.strip()[:80]}\n{reason}", file=sys.stderr)
        return lines
    ok, reason = evidence_gate(new_line, lines)
    if not ok:
        print(f"[merge] REJECTED (evidence gate: {reason}): {new_line.strip()[:80]}", file=sys.stderr)
        hint = _evidence_hint(lines)
        if hint:
            print(hint, file=sys.stderr)
        return lines
    _engagement_hint(new_line, lines)
    _long_line_warn(new_line)
    if new_line.strip().startswith("hypothesis"):
        if _hypothesis_ledger_update(lines, new_line) is not None:
            return lines
        for idx in _hypothesis_stale_candidates(lines, new_line):
            stale_id = node_id_for_line(lines[idx].strip())
            print(
                f"[merge] STALE CANDIDATE: hypothesis 行 {idx + 1} 与新终态行共享锚点"
                f"（旧行状态 {_hypothesis_state(lines[idx]) or 'observed'}）。"
                f"未自动合并——锚点只表示同一对象，不表示同一判断。"
                + (
                    f"若新行确为旧假设的结论：直接复制这一行收口 → [REMOVE] {stale_id}"
                    if stale_id else
                    "若新行确为旧假设的结论，请显式 [REMOVE] 旧行；否则双行并存。"
                ),
                file=sys.stderr,
            )
    lines.append(new_line.rstrip("\n") + "\n")
    return lines


def delete_line(lines: list, prefix: str):
    """DELETE：按 **稳定 ID** 或**逐字符前缀**删除。

    两种形态（都是机械匹配，不猜意图）：
      `[REMOVE] h:361030`       ← 推荐：ID 从投影里直接复制（[FRONTIER]/[WORLD MODEL]/
                                  [UNEXPLAINED] 每行尾部都有）。ID 是归一化文本派生的，
                                  所以补证据、升级状态都不会改号。
      `[REMOVE] hypothesis [observed]: 原文…`  ← 旧形态，仍支持。

    高置信 belief (confirmed/contradicted) 与 vuln 不可删除（Memory Mutation Contract）。

    未匹配到任何行 → **显式失败**，并且给出三个补救信息（实测这三样才是 LLM 真正需要的）：
      1. 最接近的那一行（difflib）与**首个不同字符的位置**——原提示只举例"状态标记"，
         而实际差异常在正文中段（`判断` vs `判定`），照那个提示改会改错方向；
      2. 那一行的**可直接复制的 ID**（`[REMOVE] h:xxxxxx`）；
      3. 若给的是 ID 但不存在，列出同类 ID 供挑选。
    """
    prefix = (prefix or "").strip()
    # 形态一：稳定 ID（`h:361030` / `o:42bfd6` …）
    if STABLE_ID_RE.fullmatch(prefix):
        node = graph_parse(lines).get(prefix)
        if node is None:
            same_kind = [
                n.id for n in graph_parse(lines).nodes
                if n.id and n.id[:2] == prefix[:2]
            ][:8]
            print(
                f"[merge] DELETE FAILED: ID '{prefix}' 不在账本里（ID 随文本派生，"
                f"改写文本会换号）。同类现有 ID：{' '.join(same_kind) or '(无)'}。"
                f"或改用逐字符前缀删除（先 grep -n 看原文）。",
                file=sys.stderr,
            )
            return lines
        if node.raw.strip().startswith(("vuln:",)):
            print(f"[merge] REJECTED DELETE (不可删除 vulnerability): {prefix}", file=sys.stderr)
            return lines
        if node.raw.strip().startswith(("hypothesis [confirmed]", "hypothesis [contradicted]")):
            print(f"[merge] REJECTED DELETE (不可删除高置信 belief): {prefix}", file=sys.stderr)
            return lines
        kept = [l for l in lines if not (l.strip() == node.raw.strip())]
        if len(kept) == len(lines):
            print(f"[merge] DELETE FAILED: ID {prefix} 解析到了节点但行未删除（原文可能有尾随空白差异）",
                  file=sys.stderr)
        return kept

    # 形态二：逐字符前缀（旧形态，行为不变）
    if prefix.startswith("hypothesis [confirmed]") or prefix.startswith("hypothesis [contradicted]"):
        print(f"[merge] REJECTED DELETE (不可删除高置信 belief): {prefix}", file=sys.stderr)
        return lines
    if prefix.startswith("vuln:"):
        print(f"[merge] REJECTED DELETE (不可删除 vulnerability): {prefix}", file=sys.stderr)
        return lines
    kept = [l for l in lines if not l.strip().startswith(prefix)]
    if len(kept) == len(lines):
        for hint in _nearest_line_hints(lines, prefix):
            print(hint, file=sys.stderr)
    return kept


STABLE_ID_RE = re.compile(r"^[a-z]{1,3}:[0-9a-f]{6}$")


def _first_diff_index(left: str, right: str) -> int:
    """两行的首个不同字符位置（都相同则返回短者长度）。"""
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return min(len(left), len(right))


def _nearest_line_hints(lines: list, prefix: str, *, limit: int = 2,
                        action: str = "DELETE") -> list:
    """DELETE/UPDATE 失败时给的三件事：最近行、差异位置、可直接复制的补救行。

    只做机械相似度（difflib），不做语义判断——它替代的是"让人去 grep 原文"这一步。
    `action` 决定补救行的措辞与形态（2026-09-25 审计：此前用字符串 replace 复用
    DELETE 模板，产出过「→ 直接按 ID **删**…: [UPDATE] h:xxxxxx」这种自相矛盾文案，
    把读者引向又一次失败 —— 正是本函数要消灭的那类误导）。
    """
    import difflib

    is_update = action == "UPDATE"
    verb = "改" if is_update else "删"
    best = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = difflib.SequenceMatcher(None, prefix, stripped).ratio()
        # 同前缀族优先（`hypothesis...` 只跟 hypothesis 比）
        family = prefix.split(":", 1)[0].split("[", 1)[0].strip()
        if family and not stripped.startswith(family):
            continue
        best.append((ratio, index, stripped))
    best.sort(reverse=True)
    if not best:
        return [
            f"[merge] {action} FAILED: 没有以 '{prefix[:60]}' 开头的行（账本里也没有同族行）。"
            f"用 `grep -n` 或投影里的 ID 重新取一次原文。"
        ]
    out = []
    for ratio, index, stripped in best:
        if ratio < 0.5:
            continue  # 相似度太低的行不是"你想动的那条"，列出来只会误导
        pos = _first_diff_index(prefix, stripped)
        node = graph_parse(lines).get(node_id_for_line(stripped))
        protected = stripped.startswith(("vuln:", "hypothesis [confirmed]", "hypothesis [contradicted]"))
        if node is None:
            id_hint = "（该行无 ID，需用逐字符前缀）"
        elif protected and not is_update:
            id_hint = (f"该行是终态/vuln，不可 [REMOVE]（要改内容用 [UPDATE] "
                       f"{node.id} = <新行>）")
        elif is_update:
            id_hint = f"[UPDATE] {node.id} = <带前缀的新行>"
        else:
            id_hint = f"[REMOVE] {node.id}"
        out.append(
            f"[merge] {action} FAILED: 没有以 '{prefix[:60]}' 开头的行。"
            f"最接近的是第 {index + 1} 行（相似度 {ratio:.2f}，第 {pos} 字符起不同）：\n"
            f"    {stripped[:160]}\n"
            f"    你的: {prefix[pos:pos + 60]!r}\n"
            f"    账本: {stripped[pos:pos + 60]!r}\n"
            f"→ 直接按 ID {verb}（复制这一行）： {id_hint}"
        )
        if len(out) >= limit:
            break
    if not out:
        return [
            f"[merge] {action} FAILED: 没有以 '{prefix[:60]}' 开头的行（同族行相似度都 <0.5，"
            f"说明你想{verb}的那条与账本措辞差得多）。用 `grep -n` 或投影里的 ID 重新取一次原文。"
        ]
    return out


def node_id_for_line(stripped: str) -> str:
    """一行的稳定 ID（kind 由 graph.classify 判定）。空 ID（blank/target 等）返回 ''。"""
    return graph_node_id(graph_classify(stripped), stripped)


def update_line(lines: list, prefix: str, new_content: str):
    """UPDATE：替换匹配行。两种匹配形态（2026-09-25 审计补齐对称性）：
      - `h:361030`     → 按稳定 ID 定位（与 delete_line 对称）；
                         失败提示此前恰恰推荐 `[UPDATE] h:id = 新行`，却无处可执行。
      - 逐字符前缀     → 旧行为不变。
    新内容必须符合 prefix: content 格式。Evidence Gate 同 add_line。
    Memory Mutation Contract：不可将 confirmed/contradicted 降级为 observed。"""
    if not _is_valid_line(new_content):
        preview = new_content.strip()[:60]
        print(f"[merge] REJECTED UPDATE (invalid prefix): {preview}", file=sys.stderr)
        return lines
    ok, reason = evidence_gate(new_content, lines)
    if not ok:
        print(f"[merge] REJECTED UPDATE (evidence gate: {reason}): {new_content.strip()[:80]}", file=sys.stderr)
        hint = _evidence_hint(lines)
        if hint:
            print(hint, file=sys.stderr)
        return lines

    # 检查高置信 belief 的状态降级（confirmed/contradicted → observed）
    new_match = re.match(r"^hypothesis \[(\w+)\]", new_content.strip())
    target_line = None
    if STABLE_ID_RE.fullmatch(prefix):
        node = graph_parse(lines).get(prefix)
        if node is None:
            print(
                f"[merge] UPDATE FAILED: ID '{prefix}' 不在账本里（ID 随文本派生，改写文本会换号）。",
                file=sys.stderr,
            )
            return lines
        target_line = node.raw.strip()
    if target_line is not None:
        raw_start = target_line
    else:
        raw_start = prefix
    if raw_start.startswith("hypothesis [confirmed]") or raw_start.startswith("hypothesis [contradicted]"):
        if new_match and new_match.group(1) == "observed":
            print(f"[merge] REJECTED UPDATE (不可降级高置信 belief → observed): {prefix}", file=sys.stderr)
            return lines

    if target_line is not None:
        for i, line in enumerate(lines):
            if line.strip() == target_line:
                lines[i] = new_content.rstrip("\n") + "\n"
                return lines
        print(f"[merge] UPDATE FAILED: ID {prefix} 解析到了节点但行未找到", file=sys.stderr)
        return lines

    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = new_content.rstrip("\n") + "\n"
            return lines
    # F.5-②: 定位失败 → 显式拒绝，禁止静默 fallthrough → ADD。
    # 静默追加会同时制造 stale 旧行与伪成功（UPDATE 计数说谎）。
    # 与 DELETE 同样给"最近行 + 差异位置 + 可复制 ID"，且用 UPDATE 自己的措辞
    #（此前 replace 复用 DELETE 模板 → "按 ID 删: [UPDATE] ..." 自相矛盾）。
    for hint in _nearest_line_hints(lines, prefix, action="UPDATE"):
        print(hint, file=sys.stderr)
    return lines


def _valid_line_shape(text: str) -> bool:
    """`prefix: content` 形状检查（不打印，供分隔符选择这类内联判断用）。"""
    stripped = text.strip()
    if not stripped:
        return False
    return any(
        stripped.startswith(p) and stripped[len(p):].strip()
        for p in VALID_PREFIXES
    )


def _split_update(line_text: str):
    """切 `[UPDATE] <旧行> = <新行>` / `→`。返回 (prefix, new_content) 或 None。

    切点选择：**取第一个"右侧能构成合法新行"的分隔符**，而不是首个分隔符。
    claim 正文里的 `=`/`→`（`alg=none`、`key=value`、`A→B` 是安全表述的常态）曾让
    re.split(maxsplit=1) 从正文中间切开 → 新内容没了 prefix → REJECTED UPDATE，
    含 `=` 的 UPDATE 永远失败（2026-09-25 审计实测，存量缺陷随本批一并修）。
    """
    for match in re.finditer(r"\s*[=→]\s*", line_text):
        right = line_text[match.end():]
        if _valid_line_shape(right):
            return line_text[:match.start()].strip(), right.strip()
    return None


def apply_delta(lines: list, ops: list) -> tuple:
    """将解析后的操作应用到 memory 行列表。返回 `(lines, stats)`。

    `stats` 统计的是**真正生效**的操作数，不是 `ops` 的条数：
    被证据闸门拒绝、前缀没匹配到目标、重复写入 —— 都不计入，改记 `NOOP`。
    旧实现 `sum(1 for o in ops if ...)` 直接数 ops，于是 `DELETE:1` 可能一行都没删，
    LLM 据此以为收口完成（实测踩到，见 `delete_line`）。
    `stats["added"]` 保留生效的 ADD 原文，供调用方做后续提示（如 sweep 队列更新）。
    """
    stats: dict = {"ADD": 0, "UPDATE": 0, "DELETE": 0, "NOOP": 0, "added": []}
    for op in ops:
        action = op["action"]
        line_text = op["line"]
        before = list(lines)
        effective = action

        if action == "ADD":
            lines = add_line(lines, line_text)
            _warn_evidence_integrity(line_text, lines)
        elif action == "DELETE":
            lines = delete_line(lines, line_text)
        elif action == "UPDATE":
            # UPDATE 格式: "surface: /api/login = surface: /api/login 新内容"（右侧须是完整行）
            parts = _split_update(line_text)
            if parts is not None:
                lines = update_line(lines, parts[0], parts[1])
                _warn_evidence_integrity(parts[1], lines)
            elif re.search(r"\s*[=→]\s*", line_text):
                # 有分隔符但没有任何切点让右侧构成合法新行 → 显式失败、计入 NOOP。
                # 不许退化成 ADD：静默追加会同时制造 stale 旧行与伪成功计数
                #（F.5-② 的老教训，见 update_line docstring）。
                print(
                    f"[merge] UPDATE FAILED: 分隔符（=/→）右侧不是合法新行"
                    f"（须以 {' / '.join(VALID_PREFIXES[:4])} … 开头）: {line_text[-80:]}",
                    file=sys.stderr,
                )
            else:
                # 完全没有分隔符 → 退化为 ADD（按实际动作计数，否则 UPDATE 计数又说谎）
                effective = "ADD"
                lines = add_line(lines, line_text)
                _warn_evidence_integrity(line_text, lines)

        if lines != before:
            stats[effective] = stats.get(effective, 0) + 1
            if effective == "ADD":
                stats["added"].append(line_text)
        else:
            stats["NOOP"] += 1

    return lines, stats


def merge_stats(total: dict, part: dict) -> dict:
    """把一次 apply 的 stats 并入累计（target boundary 会把 ops 拆成两批）。"""
    for key, value in part.items():
        if key == "added":
            total.setdefault("added", []).extend(value)
        else:
            total[key] = total.get(key, 0) + value
    return total


def extract_active_target(lines: list) -> str:
    """Active memory 只应有一条 target:；取第一条非空 body。"""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("target:"):
            return stripped[len("target:"):].strip()
    return ""


def target_identity(body: str) -> str:
    """机械身份：target body 第一个空白分隔 token，小写。不判断根域/公司。"""
    if not body:
        return ""
    token = body.split()[0].strip().lower()
    # 去掉常见 URL 壳，只比 host-ish 段
    for prefix in ("https://", "http://"):
        if token.startswith(prefix):
            token = token[len(prefix):]
    token = token.split("/")[0]
    return token


def target_safe_name(body: str) -> str:
    """target body → 目录名：先取机械身份再走唯一纯转换 paths.safe_name。

    与 paths.safe_name 的分工：本函数吃**整行 body**（可能带 https:// 前缀/路径），
    safe_name 只做无 IO 字符转换。路径拼接场景直接用 safe_name，勿调本函数。"""
    identity = target_identity(body) or "unknown"
    return safe_name(identity)


# ── F.5-③: world 目录身份解析（target: → 磁盘目录的唯一映射）──
# 根因：results/ 存在多套历史命名（host identity、全 URL safe_target、agent 标签），
# FK 校验只查 target_safe_name(identity) 一种 → 「磁盘存在但报不存在」
# （历史案例：440 个响应在 results/acme/，校验查 results/acme.example.com/）。
# 解析规则（机械、保守）：按候选顺序取第一个「含实际证据子目录」的目录；
# 无证据目录时退回第一个存在的目录；全都不存在则返回规范名（供创建）。
def world_dir_candidates(body: str) -> list:
    """target: body → results/ 子目录候选名（去重、保序）。"""
    identity = target_identity(body) or ""
    raw = (body or "").strip()
    cands = []
    if identity:
        cands.append(target_safe_name(body))
        first_label = identity.split(".")[0]
        if first_label and first_label != identity:
            cands.append(safe_name(first_label))
    if raw:
        raw_safe = safe_name(raw)
        cands.append(raw_safe)
        cands.append(raw_safe.rstrip("_"))
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out or ["unknown"]


def _has_evidence(d: str) -> bool:
    return os.path.isdir(d) and any(
        os.path.isdir(os.path.join(d, sub))
        for sub in ("responses", "experiments", "artifacts")
    )


def _dir_declares_target(dirpath: str, identity: str) -> bool:
    """降级候选（label / URL-safe）须通过身份确认：目录内 memory.md 的 target identity
    与查询 identity 一致才接受；无 memory.md 视为纯证据目录，接受。
    审计修复 R2：防止 a.b.com 误认 a.c.com 世界的归档目录 'a'（首标签碰撞）。"""
    mem = os.path.join(dirpath, "memory.md")
    if not os.path.isfile(mem):
        return True
    try:
        with open(mem, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("target:"):
                    return target_identity(s[len("target:"):].strip()) == identity
    except OSError:
        return False
    return True  # 无 target 行 → 不阻断（证据目录优先）


def resolve_world_dir(body: str) -> str:
    """target: body → 实际 world 目录名（results/ 下第一层）。纯机械存在性判断。
    审计修复 R2：identity 候选（target_safe_name）最高优先；降级到 label/URL-safe
    候选（含无证据 fallback 循环）一律须通过 _dir_declares_target 身份确认，
    防跨世界首标签碰撞——错误世界的空壳/归档目录不得冒充当前 world。"""
    cands = world_dir_candidates(body)
    identity = target_identity(body)
    for c in cands:
        d = os.path.join(RESULTS_DIR, c)
        if not _has_evidence(d):
            continue
        if c == cands[0] or _dir_declares_target(d, identity):
            return c
    for c in cands:
        d = os.path.join(RESULTS_DIR, c)
        if os.path.isdir(d) and (c == cands[0] or _dir_declares_target(d, identity)):
            return c
    return cands[0]


def archive_active_memory(lines: list, old_body: str) -> str:
    """将当前热 memory 落到 results/<safe>/memory.md。返回归档路径。"""
    safe = target_safe_name(old_body)
    dest_dir = os.path.join(RESULTS_DIR, safe)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "memory.md")
    text = "".join(lines).rstrip("\n") + "\n"
    # 若已有归档更长则保留更长（防覆盖更完整世界）；否则写当前
    if os.path.exists(dest):
        try:
            with open(dest, "r", encoding="utf-8") as f:
                existing = f.read()
            if len(existing) > len(text):
                print(
                    f"[merge] archive keep longer existing: {safe} ({len(existing)}>{len(text)})",
                    file=sys.stderr,
                )
                return dest
        except OSError:
            pass
    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[merge] archived active world → {dest}", file=sys.stderr)
    return dest


def reset_turn_state_on_target_change() -> None:
    """换 target：机械清零 turn 状态（遥测 + F0 最近证据编号）。

    薄封装：唯一写入实现是 turn_state.reset（v2 schema）；本函数保留名字
    以免调用点断裂。"""
    turn_state.reset()


def reset_turn_state_on_target_boundary() -> None:
    """换 target 时清零 turn telemetry（机械；不解释业务）。"""
    reset_turn_state_on_target_change()


def split_ops_at_first_target(ops: list):
    """在第一个 ADD target: 处切开：before / target_op / after。"""
    for i, op in enumerate(ops):
        if op["action"] != "ADD":
            continue
        line = op["line"].strip()
        if line.startswith("target:") and line[len("target:"):].strip():
            before = ops[:i]
            after = ops[i + 1:]
            # 丢掉 after 中后续 target: ADD，避免再叠
            after = [
                o for o in after
                if not (o["action"] == "ADD" and o["line"].strip().startswith("target:"))
            ]
            return before, op, after
    return ops, None, []


def apply_target_boundary(lines: list, ops: list) -> tuple:
    """
    机械 world boundary：
    1) 先把第一个 target: 之前的 ops 应用到当前 world
    2) 若 target 身份变化：archive → clear → init(new)
    3) 再把 target: 之后的 ops 应用到新 world
    返回 (lines, stats, did_transition)。
    stats 跨 boundary 两侧累积（见 apply_delta）。
    """
    before, target_op, after = split_ops_at_first_target(ops)
    stats: dict = {"ADD": 0, "UPDATE": 0, "DELETE": 0, "NOOP": 0, "added": []}
    if target_op is None:
        lines, part = apply_delta(lines, ops)
        return lines, merge_stats(stats, part), False

    # 旧 world 先收 before（同批里写在新 target 之前的事实仍属旧站）
    lines, part = apply_delta(lines, before)
    merge_stats(stats, part)

    new_line = target_op["line"].strip()
    new_body = new_line[len("target:"):].strip()
    new_id = target_identity(new_body)
    old_body = extract_active_target(lines)
    old_id = target_identity(old_body)

    if not old_id:
        print(f"[merge] init active target: {new_id}", file=sys.stderr)
        lines = [new_line + "\n"]
        lines, part = apply_delta(lines, after)
        return lines, merge_stats(stats, part), True

    if new_id == old_id:
        print(f"[merge] same active target ({new_id}); drop duplicate target: line", file=sys.stderr)
        lines, part = apply_delta(lines, after)
        return lines, merge_stats(stats, part), False

    print(
        f"[merge] TARGET BOUNDARY: {old_id} → {new_id} (archive+clear+init)",
        file=sys.stderr,
    )
    archive_active_memory(lines, old_body)
    reset_turn_state_on_target_boundary()
    lines = [new_line + "\n"]
    lines, part = apply_delta(lines, after)
    return lines, merge_stats(stats, part), True


def decision_zone(lines: list) -> str:
    """「去哪 + 做什么」两段（`[GOAL]` + `[NEXT]`）。回吐的机械判据就是它变没变。"""
    return digest.format_decision_zone(world_parse(lines))


def emit_decision_digest(lines: list, before_zone: str) -> None:
    """记账后回吐决策摘要 —— **只在「下一步」真的变了时**。

    为什么需要（一次实测的教训）：投影**只在 SessionStart 注入一次**，而一次真实执行是
    40 分钟 / 101 次 Bash / 若干条 DELTA —— 账本一直在变，投影从头到尾没变。
    调用方于是在**自知过期**的视图上做决策：`[NEXT]` 里的项可能早已闭合、新写的项
    根本不在里面。刷新机制其实一直都有（`python runtime/project.py`），
    但没有任何东西在**那个时刻**告诉它有 —— 这就是那个缺失的提示。

    判据完全机械，只比 `[GOAL]` + `[NEXT]` 前后是否逐字节相同：

    | 这次记账 | 下一步变了吗 | 回吐 |
    |---|---|---|
    | 只加一条 `surface:`（最高频） | 没变 | **不打扰** |
    | 新增 / 闭合 / 改状态一条 hypothesis | 变了 | 回吐 |
    | 加/改 `goal:` `subgoal:`，或换 target | 变了 | 回吐 |

    回吐正文与 SessionStart 投影**逐字节同源**（共用 `digest.py`），所以不存在
    "两个版本的 `[NEXT]`"；`tests/test_merge_digest.py` 逐字节断言这一点。

    刻意**不含** `[WORLD MODEL]` / `[SETTLED]` / `[EXCLUDED]` 正文 —— 那些是背景与历史，
    要查账时按段头给的全文指针跑一次 `project.py`，不该在每次记账后灌一遍。
    """
    view = world_parse(lines)
    if digest.format_decision_zone(view) == before_zone:
        return
    print(
        "[merge] 决策摘要（世界已变；要被裁段的全文再跑 python runtime/project.py）\n"
        + digest.format_decision_digest(view, graph_parse(lines)),
        file=sys.stderr,
    )


def cmd_archive(argv: list) -> None:
    """`python runtime/merge.py archive [target]` —— 归档**当前活跃 world**。

    为什么需要：归档原先**只在 target 边界**触发。会话结束时想留档，只能换 target
    或自己写脚本（实测：agent 为此现写了一个 `archive_memory.py` 再调 execute.py 跑）。
    """
    with memory_transaction():
        lines = read_memory()
        active_body = extract_active_target(lines)
        if not active_body:
            print("[merge] archive FAILED: 热 memory 里没有 target: 行", file=sys.stderr)
            sys.exit(1)
        if len(argv) > 1 and target_identity(argv[1]) != target_identity(active_body):
            print(
                f"[merge] archive FAILED: 请求的 target({argv[1]}) 与热 memory({active_body}) 不一致；"
                f"归档只对**当前活跃 world** 生效（切 target 用 DELTA 里的 target: 行）。",
                file=sys.stderr,
            )
            sys.exit(1)
        dest = archive_active_memory(lines, active_body)
    print(dest)


def _read_stdin_text() -> str:
    """优先用 reconfigure 后的 text stream；不可用则从 buffer 手动解码。"""
    try:
        return sys.stdin.read()
    except UnicodeDecodeError:
        raw = sys.stdin.buffer.read()
        return raw.decode("utf-8", errors="replace")


def warn_lf_escape_trap(text: str) -> None:
    """把「shell 不解释 \\n」的静默失败变成显式警告。

    `echo "DELTA:\\n..."` 在任何主流 shell 里都**不会**把 `\\n` 变成换行
    （bash 的 echo 默认不解释转义，PowerShell 的 echo 是 Write-Output 也不解释），
    于是整个 DELTA 挤成一行 → `parse_delta` 解析出 0 个操作 → 进程 `exit(0)`。

    也就是说：**看起来成功，实际一条都没记**。
    实测（Git Bash 5.3.9，agent 的真实 shell）：
        echo   "DELTA:\\ngoal: x"  → 无任何输出，世界未变
        printf "DELTA:\\ngoal: x\\n" → [merge] ADD:1，世界已变

    这里不尝试"猜"用户意图去修复它（那会引入新的隐式行为），
    只把它变成一条可见的失败提示 —— 静默的成功比明确的失败危险得多。
    """
    stripped = text.strip()
    if "\n" in stripped or "\\n" not in stripped:
        return
    print(
        "[merge] WARNING: 输入是单行且含字面 `\\n` —— shell 的 echo 不解释转义，"
        "DELTA 没有真的分行，本次写入 0 条（此前是静默 exit 0）。\n"
        "        改用：python runtime/merge.py --delta-file <path>   （推荐，零转义）\n"
        "        或：  printf 'DELTA:\\n<行1>\\n<行2>\\n' | python runtime/merge.py",
        file=sys.stderr,
    )


def _strip_bom(text: str) -> str:
    """去掉开头的 BOM。

    为什么必须在**读入边界**处理（2026-09-18 实测）：PowerShell 5.1 的
    `Out-File` / `Set-Content -Encoding UTF8` 默认写 BOM（实测首字节 `EF BB BF`），
    而 `"\\ufeffDELTA:".strip()` **不会**去掉 `\\ufeff` —— 它不是 Python 的空白字符。
    于是 `startswith("DELTA:")` 为假 → 解析出 **0 个操作** → 静默写 0 条、`exit 0`。

    与 §8.0j 那条 echo 陷阱同一类：**看起来成功，实际什么都没记**。
    而 `--delta-file` 正是 CLAUDE.md 推荐给 agent 的唯一记账路径。

    只在边界清一次，不让每个解析器各自记得。
    """
    return text.lstrip("\ufeff")


def warn_no_operations(text: str) -> None:
    """解析出 0 个操作时**出声**。

    原先 `if not ops: sys.exit(0)` 是静默的 —— 而静默成功比明确失败危险得多：
    调用方以为记上了，账本其实没变。这条同时兜住"内容里根本没有 `DELTA:` 行"
    （BOM、复制粘贴、写错文件、写成了别的格式）这一整类事故，
    它们原先全部表现为"无输出、exit 0"。
    """
    body = [line.strip() for line in text.splitlines() if line.strip()]
    if not body:
        return
    print(
        "[merge] WARNING: 读到了内容但解析出 **0 个操作**，世界未变。\n"
        f"        第一行是：{body[0][:80]}\n"
        "        检查：(1) 是否有以 `DELTA:` 开头的一行；(2) 每行前缀是否在合法列表里"
        "（surface/hypothesis/counter/dead/vuln/cred/tech/port/artifact/goal/subgoal/future/target）。",
        file=sys.stderr,
    )


def main():
    argv = sys.argv[1:]
    if argv:
        sub = argv[0]
        if sub == "archive":
            cmd_archive(argv)
            return
        if sub in ("--delta-file", "-f"):
            if len(argv) < 2:
                print("[merge] --delta-file 需要一个路径（`-` 表示 stdin）", file=sys.stderr)
                sys.exit(2)
            source = argv[1]
            if source == "-":
                text = _read_stdin_text()
            else:
                try:
                    with open(source, "r", encoding="utf-8") as handle:
                        text = handle.read()
                except OSError as exc:
                    print(
                        f"[merge] 读不到 DELTA 文件: {source}（{exc}）\n"
                        f"        先把 DELTA 用 Write 落盘（推荐 results/<target>/scripts/_delta.txt），再跑本命令。",
                        file=sys.stderr,
                    )
                    sys.exit(2)
        else:
            print(
                f"[merge] 未知子命令: {sub}（支持 archive、--delta-file <path>；"
                f"不带参数时从 stdin 读）",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        text = _read_stdin_text()
    text = _strip_bom(text)

    if not text.strip():
        sys.exit(0)

    warn_lf_escape_trap(text)

    ops = parse_delta(text)
    if not ops:
        # 静默 exit(0) 会让"一个操作都没解析出来"看起来像成功 —— 出声（见 warn_no_operations）。
        warn_no_operations(text)
        sys.exit(0)

    # 读 → 改 → 写必须在同一把锁内：`experiment.close` 的写回是另一条同构路径，
    # 两条路径并发时（agent 常把"记账 + 关实验"放在同一消息里）后写者会盖掉先写者。
    with memory_transaction():
        lines = read_memory()
        before_zone = decision_zone(lines)
        lines, stats, transitioned = apply_target_boundary(lines, ops)
        write_memory(lines)

    boundary = " BOUNDARY:1" if transitioned else ""
    noop = f" NOOP:{stats['NOOP']}" if stats["NOOP"] else ""
    # 计数是**真正生效**的操作数，不是 ops 条数（见 apply_delta）：
    # 旧实现会让 `DELETE:1` 在一行都没删时照样报出来，LLM 据此以为收口完成。
    print(
        f"[merge] ADD:{stats['ADD']} UPDATE:{stats['UPDATE']} DELETE:{stats['DELETE']}{noop}{boundary}",
        file=sys.stderr,
    )
    # 世界变了才回吐（判据机械：只比 [GOAL]+[NEXT] 前后是否相同）。
    # 原先这里只对 `[sweep]` 行提示一次 —— 那是同一个洞的局部堵法，见 emit_decision_digest。
    emit_decision_digest(lines, before_zone)


if __name__ == "__main__":
    main()
