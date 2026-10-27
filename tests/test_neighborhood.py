"""邻域投影（探索者的世界视图）回归测试（融合架构 P4）。

设计前提：memory 里**没有显式边**，故"相邻"只能由**共享标识符（锚点）**定义。
本模块的两个消费者误报代价相反，故锚点分两口径：
- strict（merge 挑 STALE CANDIDATE）：宁缺勿滥 —— 误报会诱导 LLM 合并两条不同假设
- broad（邻域）：宁多勿漏 —— **漏报会让邻域为空，机制整体失效**

实测教训（写死在测试里）：严口径下中文笔记的 intent 提取出 **0 个锚点**
（`base-a/pass-find` 无前导斜杠、`1021_publicapi_002` 数字开头），
邻域恒空 → 整个 P4 退化成"给了探索者一堆无关行"。

真实数据覆盖度（194 份 memory / 455 个 edge 节点）：
- 71% 的 edge 能提取出锚点（29% 纯中文表述，无标识符可锚）
- 其中 79% 邻域非空，26% 邻域内含 exclusion
- related: median 2 / mean 4.0 / max 34
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import graph  # noqa: E402
from runtime.merge import _hypothesis_anchors, _normalize_hypothesis_text  # noqa: E402

RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
ACTIVE_MEMORY = os.path.join(PROJECT_ROOT, ".claude", "memory.md")


def _all_memory_files():
    files = []
    if os.path.isdir(RESULTS_DIR):
        for name in sorted(os.listdir(RESULTS_DIR)):
            path = os.path.join(RESULTS_DIR, name, "memory.md")
            if os.path.isfile(path):
                files.append(path)
    if os.path.isfile(ACTIVE_MEMORY):
        files.append(ACTIVE_MEMORY)
    return files


class TestAnchors(unittest.TestCase):
    def test_strict_matches_merge_exactly(self):
        """strict 口径必须与 merge 的既有实现逐字等价（否则 STALE CANDIDATE 会漂移）。

        注意调用域：merge 传入的是 **normalized** 文本（已剥证据块），故在此域上比较。
        """
        samples = [
            "legacy_impl_file_getDocumentUrlByKey 无鉴权返回签名 URL",
            "LegacyApp_getMenuButtonConfig menuId 参数不影响响应内容",
            "/api/base/account/send-token-validator-code 无鉴权",
            "getDocumentUrlByKey 是盲签名器",
            "password 与 login 不构成锚点",
            "check_session 可能被限制访问",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(
                    set(_hypothesis_anchors(text)),
                    set(graph.anchors(text, strict=True)),
                )

    def test_strict_rejects_generic_words(self):
        strict = graph.anchors("password login admin user token name", strict=True)
        self.assertEqual(set(strict), set())

    def test_broad_catches_real_chinese_note_forms(self):
        """三种真实形态：无前导斜杠的路径、数字开头 snake、连字符标识符。"""
        cases = {
            "base-a/pass-find": "base-a/pass-find",
            "1021_publicapi_002": "1021_publicapi_002",
            "send-token-validator-code": "send-token-validator-code",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(expected, graph.anchors(text))

    def test_broad_splits_path_and_snake_but_not_hyphen(self):
        """切段规则：`/` 与 `_` 切，`-` 不切（连字符是标识符的一部分）。"""
        got = graph.anchors("base-a/pass-find")
        self.assertIn("base-a", got, "应切出 base-a（否则邻域对不上别的行）")
        self.assertIn("pass-find", got)
        self.assertIn("send-token-validator-code", graph.anchors("send-token-validator-code"))

    def test_evidence_refs_are_not_anchors(self):
        """`(req_032/051)` 是引用不是内容——不得进锚点（实测曾泄漏 req_062 等）。"""
        got = graph.anchors("getMenuButtonConfig 返回 0000 (req_062 vs req_032/051)")
        leaked = [a for a in got if a.startswith(("req_", "exp-", "art-"))]
        self.assertEqual(leaked, [], f"证据引用泄漏进锚点: {leaked}")

    def test_subsegment_expansion(self):
        """历史案例：`legacy_impl_file_getDocumentUrlByKey` ↔ `getDocumentUrlByKey`。"""
        got = graph.anchors("legacy_impl_file_getDocumentUrlByKey", strict=True)
        self.assertIn("legacy_impl_file_getdocumenturlbykey", got)
        self.assertIn("getdocumenturlbykey", got)


class TestNeighborhood(unittest.TestCase):
    LINES = [
        "target: t\n",
        "surface: /api/login 返回 200，暴露 systemCode (req_001)\n",
        "hypothesis [observed]: /api/login 的 systemCode 是否可枚举\n",
        "counter: /api/login 的 systemCode 参数不影响响应 (req_009)\n",
        "surface: /other 完全无关的东西\n",
        "hypothesis [observed]: 完全不相关的假设，没有任何标识符\n",
    ]

    def setUp(self):
        self.graph = graph.parse(self.LINES)
        self.intent_id = graph.node_id(
            graph.KIND_EDGE, "hypothesis [observed]: /api/login 的 systemCode 是否可枚举"
        )

    def test_unknown_id_returns_none(self):
        self.assertIsNone(graph.neighborhood(self.graph, "h:000000"))

    def test_related_are_anchor_sharers(self):
        nb = graph.neighborhood(self.graph, self.intent_id)
        texts = [n.text for n in nb.related]
        self.assertTrue(any("req_001" in t or "/api/login" in t for t in texts))
        self.assertFalse(any("/other" in t for t in texts), "无关行不得入邻域")

    def test_exclusions_property_filters_exclusion_kind(self):
        nb = graph.neighborhood(self.graph, self.intent_id)
        self.assertTrue(nb.exclusions, "同锚点的 counter 应作为 exclusion 出现")
        self.assertTrue(all(n.kind == graph.KIND_EXCLUSION for n in nb.exclusions))

    def test_boundary_is_declared(self):
        """必须声明界外数量——否则局部视图会永久藏住远端连接。"""
        nb = graph.neighborhood(self.graph, self.intent_id)
        ided = sum(1 for n in self.graph.nodes if n.id)
        self.assertEqual(nb.outside, ided - len(nb.related) - 1)
        self.assertGreater(nb.outside, 0)

    def test_limit_is_respected(self):
        nb = graph.neighborhood(self.graph, self.intent_id, limit=1)
        self.assertLessEqual(len(nb.related), 1)

    def test_unaligned_intent_has_empty_neighborhood(self):
        """纯中文、无标识符的假设提不出锚点 → 邻域为空。这是**已知限制**，非 bug。"""
        g = graph.parse(["hypothesis [observed]: 完全不相关的假设\n", "surface: x\n"])
        iid = g.by_kind(graph.KIND_EDGE)[0].id
        nb = graph.neighborhood(g, iid)
        self.assertEqual(set(nb.anchors), set())
        self.assertEqual(nb.related, ())


class TestRenderNeighborhood(unittest.TestCase):
    def setUp(self):
        self.graph = graph.parse(TestNeighborhood.LINES)
        self.iid = graph.node_id(
            graph.KIND_EDGE, "hypothesis [observed]: /api/login 的 systemCode 是否可枚举"
        )

    def test_render_is_terse_and_complete(self):
        out = graph.render_neighborhood(graph.neighborhood(self.graph, self.iid))
        for marker in ("[NEIGHBORHOOD ", "intent: ", "anchors: ", "excluded (", "related (", "outside: "):
            self.assertIn(marker, out)
        self.assertNotIn("建议", out)
        self.assertNotIn("should", out.lower())

    def test_render_lists_ids_for_citation(self):
        nb = graph.neighborhood(self.graph, self.iid)
        out = graph.render_neighborhood(nb)
        for node in nb.exclusions:
            self.assertIn(node.id, out)

    def test_empty_sections_say_none(self):
        """无锚点时三处都应是 `(none)`：anchors / excluded / related。"""
        g = graph.parse(["hypothesis [observed]: no anchors here at all\n"])
        iid = g.by_kind(graph.KIND_EDGE)[0].id
        nb = graph.neighborhood(g, iid)
        self.assertEqual(set(nb.anchors), set())
        out = graph.render_neighborhood(nb)
        self.assertEqual(out.count("(none)"), 3)

    def test_none_intent_renders_empty(self):
        self.assertEqual(graph.render_neighborhood(graph.Neighborhood(None, frozenset(), (), 0)), "")


class TestSafeId(unittest.TestCase):
    def test_roundtrip(self):
        for node_id in ("h:8a01be", "f:4419c2", "o:270b43"):
            self.assertEqual(graph.unsafe_id(graph.safe_id(node_id)), node_id)

    def test_no_colon_in_filename(self):
        self.assertNotIn(":", graph.safe_id("h:8a01be"))


class TestRealDataMechanism(unittest.TestCase):
    """机制必须在真实数据上**不退化**——这是 P4 的验收核心。

    实测（194 份 memory / 455 edge）：71% 有锚点，其中 79% 邻域非空。
    门槛留出余量，但足以抓住"邻域恒为空"这类整体失效。
    """

    def test_mechanism_is_not_degenerate(self):
        files = _all_memory_files()
        if not files:
            self.skipTest("无 results/ memory 文件可测（fixture 环境）")
        total = with_anchor = nonempty = with_exclusion = 0
        for path in files:
            with open(path, "r", encoding="utf-8") as handle:
                g = graph.parse(handle.readlines())
            for node in g.by_kind(graph.KIND_EDGE):
                total += 1
                if not graph.anchors(node.text):
                    continue
                with_anchor += 1
                nb = graph.neighborhood(g, node.id)
                if nb.related:
                    nonempty += 1
                if nb.exclusions:
                    with_exclusion += 1
        self.assertGreater(total, 0)
        self.assertGreaterEqual(with_anchor / total, 0.5, "过半 edge 应有锚点")
        self.assertGreaterEqual(nonempty / with_anchor, 0.5, "邻域非空率不得退化")
        self.assertGreater(with_exclusion, 0, "真实数据里应存在带同区排除的 intent")


class TestExplorerContextIsolation(unittest.TestCase):
    """探索者的上下文边界（2026-09-18 重定义）。

    派发从"自建嵌套 claude 进程"改成宿主 Task 工具（`.claude/agents/deepdive.md`），
    隔离语义随之变化，这里锁死新的两条边界：

    1. **世界视图隔离**（要保护的）：Task 子 agent 不触发 SessionStart，拿不到主会话的
       世界投影；它的全部世界视图 = 邻域切片（有界）。契约必须写明"不要读全量 memory"。
    2. **cognition 不再隔离**（有意接受）：`runAgent` 默认 `omitClaudeMd=false`，
       子 agent 随 CLAUDE.md 拿到 cognition。探索者同样需要那套认识论，
       而「可选读等于没有」要求它必须是强制注入。
    3. **不许回退**：deepdive.py 里不得再出现自建嵌套会话（那会退回阻塞派发，
       实测派发次数 0）。
    """

    AGENT_MD = os.path.join(PROJECT_ROOT, ".claude", "agents", "deepdive.md")

    def setUp(self):
        with open(self.AGENT_MD, encoding="utf-8") as handle:
            self.contract = handle.read()

    def test_contract_frontmatter_is_parseable_by_host(self):
        """宿主 `nh4()` 要求 name 与 description 都在 frontmatter 里，缺一则整个 agent 被丢弃。

        解析失败是**静默**的：主 agent 的 system prompt 里不会出现这个 agent，
        于是"派发"这个选项根本不存在——正是本轮要修的病。
        所以这里用真 YAML 解析，而不是字符串包含。
        """
        self.assertTrue(self.contract.startswith("---\n"), "必须有 frontmatter")
        head = self.contract.split("---", 2)[1]
        try:
            import yaml
        except ImportError:  # pragma: no cover - 依赖缺失时退回结构检查
            self.assertIn("name: deepdive", head)
            self.assertIn("description:", head)
            self.assertIn("tools:", head)
            return
        data = yaml.safe_load(head)
        self.assertIsInstance(data, dict, "frontmatter 必须是 YAML 映射")
        self.assertEqual(data["name"], "deepdive")
        self.assertIsInstance(data["description"], str)
        self.assertGreater(len(data["description"]), 40, "description 太短，主 agent 无法据此判断何时派")
        tools = [t.strip() for t in str(data["tools"]).split(",")]
        self.assertTrue({"Bash", "Read"} <= set(tools), "探索者至少要能发包(Bash)和读邻域(Read)")

    def test_agent_md_lives_where_host_scans(self):
        """宿主只在 `.claude/agents/` 下发现项目级 agent（HL8 的扫描路径）。"""
        self.assertTrue(self.AGENT_MD.replace("\\", "/").endswith(".claude/agents/deepdive.md"))

    def test_description_is_forward_looking_not_stall_based(self):
        """description 会出现在主 agent 的 system prompt 里，是它唯一的派发线索。

        旧版写的是"同一假设 ≥3 次工具调用仍未闭合"——一个**永不成立**的追溯条件，
        实测 63K 字符推理里 deepdive 出现 0 次。判据必须是前视的规模/重复性判断。
        """
        head = self.contract.split("---", 2)[1].lower()
        self.assertNotIn("≥3 tool calls", head, "不得退回「卡住才派」的追溯条件")
        self.assertNotIn("stays unresolved after", head)
        for signal in ("repeating", "parallel", "one message"):
            self.assertIn(signal, head, f"description 缺少前视判据关键词: {signal}")

    def test_contract_requires_neighborhood_as_sole_world_view(self):
        self.assertIn("deepdive.py", self.contract)
        self.assertIn("neighborhood", self.contract)
        self.assertIn("complete", self.contract.lower())

    def test_contract_forbids_reading_full_memory(self):
        self.assertIn("Do not read the full `memory.md`", self.contract)

    def test_contract_enforces_single_writer(self):
        """L3 单写者：memory 只有主代理写。子 agent 并发写会互相覆盖（merge 无锁）。"""
        self.assertIn("memory.md", self.contract)
        self.assertIn("single writer", self.contract)
        self.assertIn("--memory-update", self.contract)

    def test_contract_requires_anchored_evidence(self):
        for anchor in ("exp-NNN", "req_NNN", "art-NNN"):
            self.assertIn(anchor, self.contract)

    def test_dispatch_is_not_a_nested_claude_process(self):
        """防回退：不许再用子进程起嵌套 claude —— 那是阻塞派发、派发次数为 0 的根源。"""
        source = os.path.join(PROJECT_ROOT, "runtime", "deepdive.py")
        with open(source, encoding="utf-8") as handle:
            code = handle.read()
        for banned in ("subprocess", "Popen(", "communicate(", "claude\"", "claude'"):
            self.assertNotIn(banned, code, f"deepdive.py 残留派发机制: {banned}")

    def test_deepdive_module_has_no_dispatch_side_effects(self):
        """模块职责单一：只剪邻域，不派发、不认领、不建实验。"""
        from runtime import deepdive
        for banned in ("run_nested_claude", "build_prompt", "validate_experiment"):
            self.assertFalse(hasattr(deepdive, banned), f"残留派发职责: {banned}")
        for kept in ("write_neighborhood", "memory_path_for", "safe_target"):
            self.assertTrue(hasattr(deepdive, kept), f"丢失核心职责: {kept}")


if __name__ == "__main__":
    unittest.main()
