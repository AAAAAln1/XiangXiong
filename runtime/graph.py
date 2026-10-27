#!/usr/bin/env python3
"""graph.py — memory 行 → 类型化节点图（结构层，零判断）。

**唯一职责**：把 memory 的行序列机械地变成"带类型、带稳定 ID、保序"的节点序列，
并能无损还原。不做对象归并、不判关系、不排优先级、不选下一步。

为什么是"图"而不是"桶"：节点的**类型**与**身份**分离了存储形状。
`surface:` / `hypothesis [state]:` / `counter:` 各自有 kind 与稳定 ID，
于是可以被**寻址**（引用、认领、切片），而不必每次把整份 memory 读进上下文。
这是"遍历代替重建"的前提（融合架构规格 §1.1 E1）。

**无损是本模块的第一性质**（规格 §8.1）：
    render(parse(lines)) == lines      # 逐字节，对全部已知 memory 成立
故 `Node.raw` 原样保存（含行尾），`render` 只是取回。
这让"迁移到任何新格式"都可以先用断言证明零信息损失。

已知的真实边界（对 194 份 memory 实测，见 §10 测试）：
- 57 个空行 → kind=blank，保留
- 74 行非白名单前缀 → kind=unclassified，保留（其中 71 行是手写 Markdown 归档）
- `dead-summary:` 被 world.py 认作 counter，但**不在** merge 的 VALID_PREFIXES 中
  → 本模块按 exclusion 归类，与投影保持一致
- 2 份文件含多个 `target:` 行 → 全部保留，`target` 取第一个
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

# ── 节点类型 ──
KIND_TARGET = "target"
KIND_GOAL = "goal"                  # goal: / subgoal: —— 搜索的终止条件与阶段里程碑
KIND_OBSERVATION = "observation"    # surface:
KIND_FINDING = "finding"            # vuln:
KIND_ASSET = "asset"                # cred:
KIND_CONSTRAINT = "constraint"      # tech: / port:
KIND_EVIDENCE = "evidence"          # artifact:
KIND_EDGE = "edge"                  # hypothesis / hypothesis [state]:
KIND_EXCLUSION = "exclusion"        # counter: / dead: / dead-summary:
KIND_INTENT = "intent"              # future:
KIND_BLANK = "blank"
KIND_UNCLASSIFIED = "unclassified"

# kind → ID 前缀。ID 需要与投影中的 ID 一致（单一 ID 空间），故此处是唯一定义处。
ID_PREFIX = {
    KIND_GOAL: "g:",
    KIND_OBSERVATION: "o:",
    KIND_FINDING: "v:",
    KIND_ASSET: "c:",
    KIND_CONSTRAINT: "k:",
    KIND_EVIDENCE: "e:",
    KIND_EDGE: "h:",
    KIND_EXCLUSION: "x:",
    KIND_INTENT: "f:",
    KIND_UNCLASSIFIED: "u:",
}

# 稳定展示顺序（计数、遍历用）。不是优先级——只是让输出可复现。
# GOAL 在首位：它是唯一的方向性信息，其余都是"路况"。没有它，路况无从解读。
KIND_ORDER = (
    KIND_GOAL, KIND_OBSERVATION, KIND_FINDING, KIND_ASSET, KIND_CONSTRAINT,
    KIND_EVIDENCE, KIND_EDGE, KIND_EXCLUSION, KIND_INTENT, KIND_UNCLASSIFIED,
)

# 长前缀在前：避免 `dead:` 抢走 `dead-summary:`/`dead [`（虽形态不同，仍按长度排序以防未来加前缀踩坑）。
PREFIX_TO_KIND = (
    ("subgoal:", KIND_GOAL),
    ("hypothesis [", KIND_EDGE),
    ("hypothesis:", KIND_EDGE),
    ("dead-summary:", KIND_EXCLUSION),
    ("dead [", KIND_EXCLUSION),
    ("surface:", KIND_OBSERVATION),
    ("artifact:", KIND_EVIDENCE),
    ("counter:", KIND_EXCLUSION),
    ("future:", KIND_INTENT),
    ("target:", KIND_TARGET),
    ("goal:", KIND_GOAL),
    ("vuln:", KIND_FINDING),
    ("cred:", KIND_ASSET),
    ("tech:", KIND_CONSTRAINT),
    ("port:", KIND_CONSTRAINT),
    ("dead:", KIND_EXCLUSION),
)

_EDGE_STATES = frozenset(
    {"observed", "confirmed", "contradicted", "blocked", "unknown", "sweep"}
)
# terrain() 的 state 展开顺序。**新增 edge state 必须同步这里**：
# 漏一处会让该状态的行不进 terrain，小计小于节点总数
# （tests/test_terrain_projection.py::test_terrain_matches_node_count 会抓住）。
_STATE_ORDER = (None, "observed", "sweep", "confirmed", "contradicted", "blocked", "unknown")
_STATE_RE = re.compile(r"^hypothesis\s*\[\s*([A-Za-z]+)\s*\]")
# 行前缀（含可选状态标记）。`dead-summary:` 这类带连字符的也在内。
_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\s*(?:\[[A-Za-z]+\])?\s*:\s*")
# 证据引用块：`[]` / `()` / `（）` 包裹且内含 req_/exp-/art- 的片段。
#
# **这是全仓库唯一实现。** graph.normalized_claim / merge._normalize_hypothesis_text /
# experiment._sync_memory 的比对 / project._brief 的压缩**必须**共用它。
#
# 为什么必须共用（2026-09-18 实测）：这条正则在三处各写了一份，2026-09-17 只给
# project 那份补上了全角括号 `（）`，graph 与 merge 两份至今只认半角 ——
# 而真实 memory 里的证据**普遍写成全角**（本机 `.claude/memory.md` 的 hypothesis 行
# 3/3 命中）。后果不是"少剥个括号"，而是 `normalized_claim` 的承诺失效：
#
#   它承诺「补证据不改 ID」（见 normalized_claim docstring），但全角证据没被剥掉时，
#   证据文本进了 crc32 → **同一 claim 补一条证据就换一个 ID**。
#   而 ID 是投影里 `intent=h:xxxxxx` 的唯一指称（deepdive / intents.claim 都按它取行），
#   ID 一漂，抄下来的派发指令就指不到东西了。
#
# 同一份副本还喂养 merge 的账本更新：全角证据使"新行 vs 旧行"归一化不相等 →
# `_hypothesis_ledger_update` 静默失配 → 同一假设被 ADD 成第二行（重复行堆积）。
#
# 这正是 filelock.py 顶部记过的那类事故（"protocol_scan 与 diff_scan 各写一份 → 只修了一半"）
# 的第二次发生。tests/test_evidence_block_single_source.py 锁死"只允许一处定义"。
EVIDENCE_BLOCK_RE = re.compile(r"[\[\(（][^\]\)）]*(?:req_|exp-|art-)[^\]\)）]*[\]\)）]")

# ── 锚点：节点"提到了什么" ──
# 全量迁移自 merge.py（原 `_hypothesis_anchors`）。memory 里**没有显式边**，
# 故"相邻"只能由共享标识符定义。merge 的 STALE CANDIDATE 用的就是这套。
_ANCHOR_PATH_RE = re.compile(r"/[A-Za-z0-9_][A-Za-z0-9_\-]*(?:/[A-Za-z0-9_][A-Za-z0-9_\-]*)+")
_ANCHOR_IDENT_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{5,}\b")
_ANCHOR_STOPWORDS = {
    "https", "http", "www", "json", "html", "true", "false", "null",
    "params", "param", "config", "data", "code", "name", "type", "list",
    "post", "get", "url", "api", "app", "id",
}



@dataclass(frozen=True)
class Node:
    """一个 memory 行。`raw` 是唯一真相源（含行尾），其余字段皆机械派生。"""

    kind: str
    id: str
    state: str | None
    raw: str
    index: int

    @property
    def text(self) -> str:
        """去掉行尾的正文（供展示/比较）。"""
        return self.raw.rstrip("\r\n")


@dataclass(frozen=True)
class Graph:
    nodes: tuple

    @property
    def target(self) -> str:
        """第一个 target: 行的值（无则空串）。不判断根域/公司。"""
        for node in self.nodes:
            if node.kind == KIND_TARGET:
                return node.text.split(":", 1)[1].strip()
        return ""

    def by_kind(self, kind: str) -> tuple:
        return tuple(n for n in self.nodes if n.kind == kind)

    def by_id(self, node_id: str) -> tuple:
        """按 ID 取**全部**同身份行。

        ID 是身份，不是行号：归一化文本相同的行共享一个 ID。
        实测三种来源（对 194 份 memory 确认）：
        - 同一声明、证据不同（`(req_096,...)` 有无）→ 刻意同号
        - 原文重复行（同一 surface 写了两遍）
        - merge 的账本更新留下的历史行
        故此处返回元组而非单节点；`get()` 只给第一行。
        """
        return tuple(n for n in self.nodes if n.id and n.id == node_id)

    def get(self, node_id: str):
        """按 ID 取第一个节点（无则 None）。同上：ID 未必唯一。"""
        for node in self.nodes:
            if node.id and node.id == node_id:
                return node
        return None

    def ids(self) -> tuple:
        """全部非空 ID（**含重复**；身份数见 `unique_ids`）。"""
        return tuple(n.id for n in self.nodes if n.id)

    def unique_ids(self) -> tuple:
        return tuple(dict.fromkeys(self.ids()))



def classify(stripped: str) -> str:
    """行 → kind。仅前缀匹配，不解释内容。"""
    if not stripped:
        return KIND_BLANK
    for prefix, kind in PREFIX_TO_KIND:
        if stripped.startswith(prefix):
            return kind
    return KIND_UNCLASSIFIED


def edge_state(stripped: str) -> str | None:
    """hypothesis 行的状态标记；无标记或非法标记返回 None。"""
    match = _STATE_RE.match(stripped)
    if not match:
        return None
    state = match.group(1).lower()
    return state if state in _EDGE_STATES else None


def normalized_claim(stripped: str) -> str:
    """去前缀、去证据块、压缩空白 → 用于身份比较的稳定文本。

    注意：**不改内容**，只用于算 ID。补证据（追加 `req_NNN`）或升级状态
    （`[observed]`→`[confirmed]`）都不改变 ID —— 这正是合并账本按文本匹配的同一前提。
    """
    text = _PREFIX_RE.sub("", stripped.strip(), count=1)
    text = EVIDENCE_BLOCK_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def node_id(kind: str, stripped: str) -> str:
    """机械派生的稳定 ID：`<前缀><crc32(归一化文本) 取 6 hex>`。

    与投影中的 ID 是**同一个**（`project.py` 从本模块取用），不设第二套编号。
    """
    prefix = ID_PREFIX.get(kind)
    if not prefix:
        return ""
    normalized = normalized_claim(stripped)
    if not normalized:
        return ""
    return f"{prefix}{zlib.crc32(normalized.encode('utf-8')) & 0xFFFFFF:06x}"


def _is_specific_identifier(token: str) -> bool:
    """标识符锚点须有特定形态：snake_case / camelCase / 含数字。挡 password、login 类泛词。"""
    low = token.lower()
    if low in _ANCHOR_STOPWORDS:
        return False
    return "_" in token or any(c.isupper() for c in token[1:]) or any(c.isdigit() for c in token)


# 宽口径补充（仅邻域用）：真实中文笔记里常见而严口径漏掉的形态。
#   base-a/pass-find（无前导斜杠的路径）、1021_publicapi_002（数字开头 snake）、
#   send-token-validator-code（连字符标识符）
_BROAD_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-/]{4,}")


def _broad_ok(token: str) -> bool:
    low = token.lower()
    if low in _ANCHOR_STOPWORDS or len(token) < 5:
        return False
    if not any(c.isalpha() for c in token):
        return False
    return any(c in token for c in "_-/") or any(
        c.isupper() for c in token[1:]
    ) or any(c.isdigit() for c in token)


def anchors(text: str, *, strict: bool = False) -> frozenset:
    """节点提到的强标识符集合（小写）。

    两个口径，因为两个消费者的误报代价不同：
    - `strict=True`（merge 的 STALE CANDIDATE 用）：宁缺勿滥 —— 误报会诱导
      LLM 合并两条不同假设。此口径即 merge 原实现，未改一字。
    - `strict=False`（邻域用）：宁多勿漏 —— 误报只是多给一行上下文，
      而漏报会让邻域为空、机制失效（实测：严口径下中文笔记的 intent 提取出 0 锚点）。

    `_` 分隔的标识符同时展开子段（≥6 字符且形态特定），
    覆盖 acme 案：`legacy_impl_file_getDocumentUrlByKey` ↔ `getDocumentUrlByKey`。
    """
    # 先剥证据块：`(req_062 vs req_032/051)` 是引用不是内容。
    # merge 传入的已是 normalized 文本（无证据块），故对它是 no-op、行为不变。
    text = EVIDENCE_BLOCK_RE.sub("", text)

    found = {m.group(0).lower() for m in _ANCHOR_PATH_RE.finditer(text)}
    for match in _ANCHOR_IDENT_RE.finditer(text):
        token = match.group(0)
        if _is_specific_identifier(token):
            found.add(token.lower())
        if "_" in token:
            for part in token.split("_"):
                if len(part) >= 6 and _is_specific_identifier(part):
                    found.add(part.lower())

    if strict:
        return frozenset(found)

    for match in _BROAD_TOKEN_RE.finditer(text):
        token = match.group(0).strip("/")
        if not _broad_ok(token):
            continue
        found.add(token.lower())
        # 只按 `/` 与 `_` 切段，**不切连字符**：`-` 是标识符的一部分
        # （`send-token-validator-code` 整体才有意义），而 `base-a/pass-find`
        # 需要切出 `base-a` 才能与别的行对上（实测：不切段则邻域恒为空）。
        for part in re.split(r"[/_]", token):
            if len(part) >= 5 and _broad_ok(part):
                found.add(part.lower())
    return frozenset(found)


@dataclass(frozen=True)
class Neighborhood:
    """一个节点的局部切片 + **显式边界声明**（规格 §1.1 E1/E2）。

    边界声明是必须的：突破常来自远端连接（A 的 key 被 B 引用），
    只给局部会永久藏住它 —— 故必须告诉读者"界外还有多少"。
    """

    intent: Node | None
    anchors: frozenset
    related: tuple      # 共享 ≥1 锚点（含 exclusion → "这一族已死"）
    outside: int        # 界外节点数

    @property
    def exclusions(self) -> tuple:
        return tuple(n for n in self.related if n.kind == KIND_EXCLUSION)

    @property
    def observations(self) -> tuple:
        return tuple(n for n in self.related if n.kind == KIND_OBSERVATION)


def neighborhood(graph: Graph, node_id: str, *, limit: int = 40) -> Neighborhood | None:
    """按共享锚点取邻域。node_id 不存在返回 None。

    纯机械：不做对象归并、不判关系、不排序（保持 memory 顺序）。
    用**宽口径**锚点：漏报会让邻域为空（机制失效），误报只是多一行上下文。
    """
    intent = graph.get(node_id)
    if intent is None:
        return None
    focus = anchors(intent.text)

    pool = [n for n in graph.nodes if n is not intent and n.id]
    related = tuple(n for n in pool if focus & anchors(n.text))[:limit]
    outside = sum(1 for n in graph.nodes if n.id) - len(related) - 1
    return Neighborhood(
        intent=intent,
        anchors=focus,
        related=related,
        outside=max(outside, 0),
    )



def safe_id(node_id: str) -> str:
    """ID → 文件名安全形式（`:` 在 Windows 非法）。`h:8a01be` → `h-8a01be`。

    单一实现：`intents.py`（claim 文件名）、`deepdive.py`（邻域文件名）共用。
    逆变换见 `unsafe_id`。
    """
    return node_id.replace(":", "-").replace("/", "-").replace("\\", "-")


def unsafe_id(safe: str) -> str:
    """`safe_id` 的逆：`h-8a01be` → `h:8a01be`（按首个 `-` 切分）。"""
    kind, _, digest = safe.partition("-")
    return f"{kind}:{digest}" if digest else safe


def terrain(graph: Graph) -> list:
    """形状计数（judge 的 J1）：`[(kind, state, count), ...]`，按 KIND_ORDER 稳定排序。

    只数，不评估、不排序重要性、不推荐。
    """
    counts = {}
    for node in graph.nodes:
        if not node.id:
            continue
        counts[(node.kind, node.state)] = counts.get((node.kind, node.state), 0) + 1
    return [
        (kind, state, counts[(kind, state)])
        for kind in KIND_ORDER
        for state in _STATE_ORDER
        if (kind, state) in counts
    ]


def unexplained_observations(graph: Graph, *, limit: int = 20) -> tuple:
    """锚点未被任何 edge 提及的 observation —— 即**没有假设解释它**（E5 / J4）。

    规格 §1.1 E5：这是新框架的唯一种子。纯机械（锚点交集为空），不判断"重不重要"。

    要求 observation 自身**有锚点**：没有标识符的行无法判定"是否被解释"，
    硬塞进来只是噪音（实测 29% 的 edge 是纯中文表述，同理适用于 observation）。
    """
    edge_anchors = set()
    for node in graph.by_kind(KIND_EDGE):
        edge_anchors |= anchors(node.text)
    if not edge_anchors:
        return ()
    out = []
    for node in graph.by_kind(KIND_OBSERVATION):
        own = anchors(node.text)
        if own and not (own & edge_anchors):
            out.append(node)
            if len(out) >= limit:
                break
    return tuple(out)


def render_neighborhood(nb: Neighborhood, *, limit: int = 8) -> str:
    """邻域 → 极简文本。无引导语、无建议、不解释为何相邻。

    分两段显示，**excluded 优先**：一族全死是最省时间的信息。
    每行给 ID，便于直接引用（写 counter:/hypothesis 时当锚点）。
    """
    if nb.intent is None:
        return ""
    excluded = nb.exclusions
    others = [n for n in nb.related if n.kind != KIND_EXCLUSION]

    lines = [
        # 实验槽隔离用的 worker 标识就地给出（不新增行数：切片有行数上限，见
        # tests/test_deepdive_slice.py）。探索者是并行跑的，各自 `--activate` 会撞同一个
        # 全局槽（实测 15–39 次/会话的冲突与 3 次抢槽）；用 intent ID 当 worker 即消失。
        f"[NEIGHBORHOOD {nb.intent.id}] worker={nb.intent.id}"
        f"（开实验时带 --worker {nb.intent.id} 隔离自己的槽）",
        f"intent: {nb.intent.text}",
        "anchors: " + (", ".join(sorted(nb.anchors)) or "(none)"),
    ]
    if not nb.related:
        # 空切片必须**显式说出来**。否则读者分不清"邻域真的是空的"和"文件被截断/读错了"，
        # 而这两种情况的正确反应相反：前者要向派发方索取上下文，后者要重跑。
        # 实测（会话 71b3f780）新建的 intent 常常没有共享锚点的邻居 —— 越是批量新工作
        # 越容易撞上，而那正是最该派的活。
        lines.append(
            "empty slice: memory 里没有任何与这条 intent 共享锚点的行。"
            "你的全部世界视图就是上面这一条 intent 本身；"
            "缺少的上下文由派发方在 prompt 里提供（不要改读全量 memory）。"
        )
    lines.append(f"excluded ({len(excluded)}):")
    if excluded[:limit]:
        lines += [f"  {n.id} {n.text}" for n in excluded[:limit]]
    else:
        lines.append("  (none)")
    lines.append(f"related ({len(others)}):")
    if others[:limit]:
        lines += [f"  [{n.kind}] {n.id} {n.text}" for n in others[:limit]]
    else:
        lines.append("  (none)")
    lines.append(f"outside: {nb.outside} nodes not shown")
    return "\n".join(lines) + "\n"


def parse(lines) -> Graph:
    """行序列 → Graph。保序、保原文、不丢行（含空行）。"""
    nodes = []
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        kind = classify(stripped)
        if kind == KIND_BLANK:
            nodes.append(Node(KIND_BLANK, "", None, raw, index))
            continue
        state = edge_state(stripped) if kind == KIND_EDGE else None
        nodes.append(Node(kind, node_id(kind, stripped), state, raw, index))
    return Graph(tuple(nodes))


def render(graph: Graph) -> list:
    """Graph → 行序列。**逐字节还原**（`raw` 原样取回）。"""
    return [node.raw for node in graph.nodes]
