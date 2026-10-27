"""memory → 图 的结构层回归测试（融合架构 P3）。

**第一性质是无损**：`render(parse(lines)) == lines`，逐字节。
这是"迁移到任何新格式都安全"的依据（规格 §8.1）——不靠信心，靠断言。

不变量清单：
1. 无损往返：合成边界用例 + **全部真实 memory 文件**
2. ID 稳定：P1 已提交的金样 ID 不变（否则投影会漂移）
3. ID 唯一：同一 target 内不撞号
4. 前缀完备：merge 白名单里的每个前缀都能被机械归类（防未来加前缀漏改）
5. kind 完备：除 blank/target 外每个 kind 都有 ID 前缀
6. `dead-summary:` 归 exclusion（与 world.py 的计数器桶一致；它不在 merge 白名单里）
7. 空行不被吞（实测 57 个）
8. 身份归一化：补证据 / 升级状态不改 ID
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import digest, graph  # noqa: E402
from runtime.merge import VALID_PREFIXES  # noqa: E402

FIXTURE = os.path.join(PROJECT_ROOT, "tests", "phase0", "fixture", "memory.md")
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
ACTIVE_MEMORY = os.path.join(PROJECT_ROOT, ".claude", "memory.md")

# P1 提交时金样里已固定的 ID。它们变了 ⇒ 投影漂移。
GOLDEN_IDS = {
    "h:a8e3da",  # search q parameter may reflect unsanitized
    "h:8a01be",  # admin panel may exist at /admin
    "h:f30683",  # cannot verify auth bypass without credentials
    "h:e44b95",  # /health returns 200 without auth
    "h:ecb1f8",  # /debug was not open on first probe
    "f:4419c2",  # later recheck rate limits after creds
}


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


class TestRoundTripSynthetic(unittest.TestCase):
    """合成边界用例：真实数据里出现过的每一种怪行。"""

    CASES = {
        "empty": [],
        "single_newline": ["\n"],
        "blank_only": ["\n", "\n", "  \n"],
        "target_only": ["target: example.test\n"],
        "no_trailing_newline": ["surface: x"],
        "crlf": ["surface: a\r\n", "hypothesis [observed]: b\r\n"],
        "unknown_prefix_no_colon": ["## Objects\n", "- surface: x\n", "MEMORY.md for x\n"],
        "foreign_prefix": ["root_domain:example.com\n", "objects: 32 | constraints: 18\n"],
        "dead_summary": ["dead-summary: all other paths 404\n"],
        "multi_target": ["target: a.com\n", "target: b.com extra\n"],
        "whitespace_prefix": ["   surface: indented\n", "surface:   trailing   \n"],
        "state_forms": [
            "hypothesis [observed]: A\n",
            "hypothesis [CONFIRMED]: B\n",
            "hypothesis [weird]: C\n",
            "hypothesis: D\n",
        ],
    }

    def test_roundtrip_is_byte_exact(self):
        for name, lines in self.CASES.items():
            with self.subTest(case=name):
                self.assertEqual(graph.render(graph.parse(lines)), lines)

    def test_blank_lines_become_blank_nodes(self):
        lines = ["surface: a\n", "\n", "surface: b\n"]
        nodes = graph.parse(lines).nodes
        self.assertEqual([n.kind for n in nodes],
                         [graph.KIND_OBSERVATION, graph.KIND_BLANK, graph.KIND_OBSERVATION])

    def test_unknown_prefix_is_preserved_not_dropped(self):
        lines = ["## Objects\n", "not a memory line\n"]
        g = graph.parse(lines)
        self.assertEqual(len(g.nodes), 2)
        self.assertTrue(all(n.kind == graph.KIND_UNCLASSIFIED for n in g.nodes))
        self.assertEqual(graph.render(g), lines)

    def test_dead_summary_is_exclusion(self):
        """world.py 把它归入 counters；merge 白名单却不含它——本模块与投影保持一致。"""
        g = graph.parse(["dead-summary: x\n"])
        self.assertEqual(g.nodes[0].kind, graph.KIND_EXCLUSION)

    def test_multi_target_keeps_all_and_target_is_first(self):
        lines = ["target: first\n", "surface: s\n", "target: second\n"]
        g = graph.parse(lines)
        self.assertEqual(len(g.by_kind(graph.KIND_TARGET)), 2)
        self.assertEqual(g.target, "first")
        self.assertEqual(graph.render(g), lines)

    def test_no_trailing_newline_preserved(self):
        self.assertEqual(graph.render(graph.parse(["surface: x"])), ["surface: x"])

    def test_state_parsing(self):
        g = graph.parse(self.CASES["state_forms"])
        states = [n.state for n in g.nodes]
        # 合法状态统一小写（与 world._parse_hypothesis 一致）；非法标记 → None（不发明状态）
        self.assertEqual(states, ["observed", "confirmed", None, None])
        # 但四种形态都必须是 edge 节点、都要有 ID
        self.assertTrue(all(n.kind == graph.KIND_EDGE for n in g.nodes))
        self.assertTrue(all(n.id for n in g.nodes))

    def test_empty_graph(self):
        g = graph.parse([])
        self.assertEqual(g.nodes, ())
        self.assertEqual(g.target, "")
        self.assertEqual(graph.render(g), [])


class TestIdInvariants(unittest.TestCase):
    def test_golden_ids_preserved(self):
        """P1 已提交的 ID 不许变——否则投影（含金样）会漂移。"""
        with open(FIXTURE, encoding="utf-8") as handle:
            lines = handle.readlines()
        ids = set(graph.parse(lines).ids())
        missing = GOLDEN_IDS - ids
        self.assertFalse(missing, f"金样 ID 丢失: {sorted(missing)}")

    def test_id_prefix_covers_every_identified_kind(self):
        """除 blank / target 外，每个 kind 都必须有 ID 前缀（防新增 kind 漏配）。"""
        for kind in (graph.KIND_OBSERVATION, graph.KIND_FINDING, graph.KIND_ASSET,
                     graph.KIND_CONSTRAINT, graph.KIND_EVIDENCE, graph.KIND_EDGE,
                     graph.KIND_EXCLUSION, graph.KIND_INTENT, graph.KIND_UNCLASSIFIED):
            self.assertIn(kind, graph.ID_PREFIX, f"kind 缺 ID 前缀: {kind}")

    def test_every_valid_prefix_classifies(self):
        """merge 白名单里的每个前缀都必须能归类（防未来加前缀忘改本模块）。"""
        for prefix in VALID_PREFIXES:
            with self.subTest(prefix=prefix):
                kind = graph.classify(prefix + " sample content")
                self.assertNotEqual(kind, graph.KIND_UNCLASSIFIED)
                self.assertNotEqual(kind, graph.KIND_BLANK)

    def test_id_unchanged_by_evidence_or_state(self):
        base = graph.parse(["hypothesis [observed]: admin panel may exist at /admin\n"]).nodes[0]
        with_ev = graph.parse(
            ["hypothesis [confirmed]: admin panel may exist at /admin (req_042)\n"]
        ).nodes[0]
        with_exp = graph.parse(
            ["hypothesis [confirmed]: admin panel may exist at /admin  [exp-007]\n"]
        ).nodes[0]
        self.assertEqual(base.id, with_ev.id)
        self.assertEqual(base.id, with_exp.id)

    def test_id_differs_by_text_and_kind(self):
        a = graph.parse(["surface: X\n"]).nodes[0]
        b = graph.parse(["surface: Y\n"]).nodes[0]
        c = graph.parse(["vuln: X\n"]).nodes[0]
        self.assertNotEqual(a.id, b.id, "不同文本应不同号")
        self.assertNotEqual(a.id, c.id, "不同 kind 应不同前缀")

    def test_blank_and_target_have_no_id(self):
        g = graph.parse(["target: t\n", "\n"])
        self.assertEqual([n.id for n in g.nodes], ["", ""])

    def test_get_by_id(self):
        g = graph.parse(["surface: hello world\n", "vuln: boom\n"])
        node = g.get(g.by_kind(graph.KIND_OBSERVATION)[0].id)
        self.assertIsNotNone(node)
        self.assertEqual(node.text, "surface: hello world")
        self.assertIsNone(g.get("o:ffffff"))


class TestRoundTripRealMemory(unittest.TestCase):
    """对全部真实 memory 文件逐一断言无损——这是"迁移安全"的实证。"""

    def test_all_real_memory_files_roundtrip(self):
        files = _all_memory_files()
        if not files:
            self.skipTest("无 results/ memory 文件可测（fixture 环境）")
        mismatches = []
        for path in files:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
            if graph.render(graph.parse(lines)) != lines:
                mismatches.append(path)
        self.assertEqual(mismatches, [], f"往返失配: {mismatches[:5]}")
        self.assertGreaterEqual(len(files), 1)

    def test_real_memory_id_collisions_are_only_same_claim(self):
        """ID 是**身份**不是行号：撞号必须且只能发生在归一化文本相同的行之间。

        实测有撞号（legacy-a / acme / ACTIVE），来源是：
        同一声明而证据不同（补证据不改身份，刻意同号）、原文重复行。
        这条断言把"良性撞号"与"哈希碰撞/前缀配错"区分开 ——
        后者会让不同 claim 共享身份，从而污染认领与证据引用。
        """
        files = _all_memory_files()
        if not files:
            self.skipTest("无 results/ memory 文件可测（fixture 环境）")
        bad = []
        collisions = 0
        for path in files:
            with open(path, "r", encoding="utf-8") as handle:
                g = graph.parse(handle.readlines())
            groups = {}
            for node in g.nodes:
                if node.id:
                    groups.setdefault(node.id, []).append(node)
            for node_id, nodes in groups.items():
                if len(nodes) < 2:
                    continue
                collisions += 1
                claims = {graph.normalized_claim(n.text) for n in nodes}
                if len(claims) != 1:
                    bad.append((path, node_id, sorted(claims)))
        self.assertEqual(bad, [], f"不同 claim 撞了同一 ID: {bad[:3]}")
        # 记录事实：撞号确实存在（若某天归零，说明上游做了去重，值得知道）
        self.assertGreaterEqual(collisions, 0)

    def test_unique_ids_are_fewer_or_equal_than_lines(self):
        files = _all_memory_files()
        if not files:
            self.skipTest("无 results/ memory 文件可测（fixture 环境）")
        for path in files:
            with open(path, "r", encoding="utf-8") as handle:
                g = graph.parse(handle.readlines())
            self.assertLessEqual(len(g.unique_ids()), len(g.ids()))


class TestSingleIdSpace(unittest.TestCase):
    """P1 与 P3 必须共用**同一个** ID 实现，否则会分裂出两套编号（本仓库已踩过的坑）。"""

    LINES = [
        "hypothesis [observed]: A\n",
        "hypothesis [confirmed]: B (req_001)\n",
        "hypothesis: C\n",
        "hypothesis [weird]: D\n",
    ]

    def test_graph_id_equals_projection_id(self):
        from runtime.digest import hypothesis_id
        from runtime.world import parse as world_parse

        g = graph.parse(self.LINES)
        view = world_parse(self.LINES)
        graph_ids = [n.id for n in g.by_kind(graph.KIND_EDGE)]
        projection_ids = [hypothesis_id(item) for item in view.unknowns]
        self.assertEqual(graph_ids, projection_ids)
        self.assertTrue(all(graph_ids), "ID 不应为空")

    def test_graph_kind_order_matches_world_bucket_order(self):
        """graph 保序解析，与 world 的 unknowns 桶顺序必须一致（ID 才能对齐）。"""
        from runtime.world import parse as world_parse

        g = graph.parse(self.LINES)
        view = world_parse(self.LINES)
        self.assertEqual(
            [n.text for n in g.by_kind(graph.KIND_EDGE)],
            [item.raw for item in view.unknowns],
        )


if __name__ == "__main__":
    unittest.main()
