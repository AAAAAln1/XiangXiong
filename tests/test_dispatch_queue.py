"""派发队列（C：让派发成为记账的副产品）。

## 问题

并行探索跑不出来，根因不是"物理上不能并行"，而是**主 agent 想不起来派**。
实测（会话 `ff04f382`，33 分钟 / 95 次 Bash / 63K 字符推理）：
`deepdive` 出现 0 次、`子 agent` 0 次、`派发` 0 次、`认领` 0 次；
`并行` 出现 3 次，但全部是"我在一个 shell 里同时发几个包"。

Cairn 不靠这个：它的 Dispatcher 循环每 interval 秒扫一次图，
判据是无条件的机械式 `intent.worker is None` → 派。
**它的并行是配置决定的，我们的并行一直是 LLM 自觉。**

## 解法

不要求 LLM 多做一次元决策（"我该不该外包"），而是让**它已经在做的动作**
（写 `hypothesis:`）自动产生一个可派队列，并把动作降到"抄一行"：

1. `hypothesis [sweep]: ...` → 机械进 `[SWEEP]` 段（批量验证类工作的默认执行方式是派发）
2. `[FRONTIER]` / `[SWEEP]` 段头携带**可直接复制的 Task 调用**
3. 判据完全机械：没有"该不该派"的判断，只有"这项还没人做"

## 锁死的不变量

1. sweep 行只进 `[SWEEP]`，不进 `[FRONTIER]` / `[SETTLED]`（三段互斥且完备）
2. 段头模板必须含真实 target 与 intent 占位符 —— 否则"抄一行"不成立
3. `merge` 必须接受 `[sweep]` 且**不要求证据**（它是未闭合的探索态）
4. 重压下 `[SWEEP]` 仍必须存活（它是队列，被裁掉等于队列消失）
"""

import json
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import digest, graph, project  # noqa: E402
from runtime.merge import evidence_gate  # noqa: E402
from runtime.world import parse as world_parse  # noqa: E402

SWEEP_LINES = [
    "target: dispatch.example.test\n",
    "hypothesis [sweep]: 短信验证码接口无限频，需连续 20 次调用比对\n",
    "hypothesis [sweep]: 229 个管理 API 契约逐个探测未授权\n",
    "hypothesis [observed]: /api/user 是否可枚举\n",
    "hypothesis [confirmed]: /health 无需认证 (req_001)\n",
    "counter: /old-admin 反复 404 (req_002)\n",
    "dead: /phpmyadmin 不可达 (req_003)\n",
    "surface: /login 存在\n",
]


class TestSweepIsADispatchQueue(unittest.TestCase):
    def setUp(self):
        self.view = world_parse(SWEEP_LINES)
        self.sweep = project.format_sweep_section(self.view)
        self.frontier = project.format_frontier_section(self.view)

    def test_sweep_rows_land_in_sweep_section(self):
        self.assertIn("[SWEEP] (2 batch)", self.sweep)
        self.assertIn("短信验证码接口无限频", self.sweep)
        self.assertIn("229 个管理 API 契约", self.sweep)

    def test_sweep_rows_do_not_leak_into_frontier_or_settled(self):
        """三段互斥：重复出现会让队列看起来比实际长。"""
        self.assertNotIn("短信验证码", self.frontier)
        self.assertNotIn("229 个管理 API", self.frontier)
        settled = project.format_settled_section(self.view)
        self.assertNotIn("短信验证码", settled)

    def test_sweep_count_matches_rows(self):
        rows = [ln for ln in self.sweep.splitlines() if ln.startswith("  hypothesis:")]
        self.assertEqual(len(rows), 2)

    def test_no_sweep_means_no_section(self):
        self.assertEqual(project.format_sweep_section(world_parse(["target: t\n"])), "")

    def test_every_row_carries_a_claimable_id(self):
        for line in self.sweep.splitlines():
            if line.startswith("  hypothesis:"):
                self.assertRegex(line, r"h:[0-9a-f]{6}$", f"行尾缺可引用 ID: {line}")


class TestDispatchTemplateIsCopyPasteable(unittest.TestCase):
    """段头的价值全在"可复制"：含真实 target、含 intent 占位符、是一条**工具名正确**的调用。

    工具名必须是宿主注册的 `Agent`。`Task` 只是它的兼容别名
    （`Claude-Code-main/src/tools/AgentTool/constants.ts`:
     `AGENT_TOOL_NAME = 'Agent'` / `LEGACY_AGENT_TOOL_NAME = 'Task'`），
     **不是可调用的工具名** —— 写错等于让照抄的一方去调一个不存在的工具。
    实测（会话 71b3f780）主 agent 自己纠正成了 `Agent`，但那是它在替我们擦屁股。
    """

    def setUp(self):
        self.view = world_parse(SWEEP_LINES)

    def test_frontier_header_carries_template(self):
        header = project.format_frontier_section(self.view).splitlines()[0]
        self.assertIn('Agent(subagent_type="deepdive"', header)
        self.assertIn("target=dispatch.example.test", header)
        self.assertIn("intent=<此行的 h:xxxxxx>", header)

    def test_sweep_header_carries_template(self):
        header = project.format_sweep_section(self.view).splitlines()[0]
        self.assertIn('Agent(subagent_type="deepdive"', header)
        self.assertIn("target=dispatch.example.test", header)

    def test_sweep_header_states_default_action(self):
        """批量验证类的默认执行方式是派发，不是主会话串行 —— 这句话必须显式。"""
        header = project.format_sweep_section(self.view).splitlines()[0]
        self.assertIn("默认派发", header)

    def test_no_target_means_no_template(self):
        """没有 target 就没有可派的会话上下文，模板不得凭空出现。"""
        view = world_parse(["hypothesis [sweep]: 枚举 20 个端点\n"])
        self.assertEqual(digest.dispatch_hint(""), "")
        self.assertNotIn("Agent(", project.format_sweep_section(view))
        self.assertNotIn("Agent(", project.format_frontier_section(view))

    def test_template_absent_when_no_dispatchable_section(self):
        view = world_parse(["target: t\n", "hypothesis [confirmed]: x (req_001)\n"])
        self.assertEqual(project.format_frontier_section(view), "")
        self.assertEqual(project.format_sweep_section(view), "")


class TestToolNameIsAgentNotTask(unittest.TestCase):
    """防回退：任何给 LLM 看的派发样板都不得写 `Task(`。

    `Task` 是 legacy 别名，用作**权限规则/钩子**的兼容名 —— 它出现在
    `settings.json` 的 allow 列表里是对的；出现在**调用样板**里是错的。
    """

    def test_projection_never_advertises_task_as_a_tool(self):
        view = world_parse(SWEEP_LINES)
        for text in (project.format_frontier_section(view),
                     project.format_sweep_section(view)):
            self.assertNotIn('Task(subagent_type', text)
            self.assertNotIn("Task(", text)

    def test_claude_md_advertises_the_right_tool_name(self):
        path = os.path.join(PROJECT_ROOT, "CLAUDE.md")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn('Agent(subagent_type="deepdive"', body)
        self.assertNotIn("Task(subagent_type", body)

    def test_permissions_allow_both_names(self):
        """权限规则按 legacy 名匹配，所以两个都留；这里锁死"别只留一个"。"""
        for name in ("settings.local.json", "settings.example.json"):
            path = os.path.join(PROJECT_ROOT, ".claude", name)
            with open(path, encoding="utf-8") as handle:
                allow = json.load(handle)["permissions"]["allow"]
            self.assertIn("Agent", allow, f"{name} 缺 Agent（调用要用它）")
            self.assertIn("Task", allow, f"{name} 缺 Task（legacy 权限匹配名）")


class TestSweepSurvivesMergeAndParse(unittest.TestCase):
    """`[sweep]` 必须是被 schema 承认的状态，否则它会静默退化成无状态 hypothesis。"""

    def test_world_parse_recognizes_sweep(self):
        view = world_parse(["hypothesis [sweep]: 枚举端点\n"])
        self.assertEqual(view.unknowns[0].state, "sweep")

    def test_graph_parse_recognizes_sweep(self):
        g = graph.parse(["hypothesis [sweep]: 枚举端点\n"])
        node = g.by_kind(graph.KIND_EDGE)[0]
        self.assertEqual(node.state, "sweep")

    def test_terrain_counts_sweep_as_edge_state(self):
        g = graph.parse(SWEEP_LINES)
        rows = dict(((k, s), n) for k, s, n in graph.terrain(g))
        self.assertEqual(rows[(graph.KIND_EDGE, "sweep")], 2)

    def test_state_order_covers_every_edge_state(self):
        """terrain 的 state 展开顺序必须覆盖全部 edge state。

        漏一个是真 bug 且是静默的：那类行不进 terrain，小计小于节点总数，
        而投影的 [TERRAIN] 是判据 —— 对不上账读者会误判世界形状。
        """
        covered = set(graph._STATE_ORDER) - {None}
        missing = graph._EDGE_STATES - covered
        self.assertFalse(missing, f"terrain 漏计这些 state: {sorted(missing)}")

    def test_evidence_gate_does_not_require_proof_for_sweep(self):
        """sweep 是未闭合的探索态：要求证据会把"待验证的批量工作"挡在账本外。"""
        ok, reason = evidence_gate("hypothesis [sweep]: 枚举 229 个契约", [])
        self.assertTrue(ok, reason)

    def test_evidence_gate_still_requires_proof_for_confirmed(self):
        """对照组：不得因为放开 sweep 而放松 confirmed（防止闸门整体失效）。"""
        ok, reason = evidence_gate("hypothesis [confirmed]: 存在未授权", [])
        self.assertFalse(ok)
        self.assertIn("exp-NNN", reason)

    def test_sweep_is_not_a_terminal_state(self):
        """sweep 可被后续终态取代（raise 到 confirmed/contradicted 是正常出口）。"""
        self.assertNotIn("sweep", digest.SETTLED_STATES)


class TestSweepSurvivesBudgetPressure(unittest.TestCase):
    """队列被预算裁掉 = 队列消失。它是最后才该被动的东西。"""

    def test_sweep_is_trimmed_last(self):
        order = list(project.SECTION_TRIM_ORDER)
        for earlier in ("SETTLED", "DIFFS", "UNEXPLAINED", "WORLD MODEL", "EXCLUDED", "FRONTIER"):
            self.assertLess(order.index(earlier), order.index("SWEEP"),
                            f"{earlier} 应在 SWEEP 之前被牺牲")

    def test_sweep_has_recovery_pointer(self):
        self.assertIn("SWEEP", project.SECTION_RECOVERY)

    def test_sweep_survives_a_huge_world(self):
        lines = ["target: budget.example.test\n"]
        for i in range(400):
            lines.append(f"surface: /api/v1/obj{i} 返回 200 (req_{i:04d})\n")
            lines.append(f"hypothesis [observed]: /api/v1/obj{i} 是否可枚举\n")
            lines.append(f"hypothesis [confirmed]: /api/v1/obj{i} 已确认 (req_{i:04d})\n")
        lines.append("hypothesis [sweep]: 枚举 400 个端点\n")
        sections = project._compose_sections(world_parse(lines), graph.parse(lines))
        out = project._enforce_budget(sections, project.PROJECTION_BUDGET_CHARS)
        text = "".join(body for _, body in out)
        self.assertIn("[SWEEP]", text, "重压下待派队列被裁掉了")
        self.assertLessEqual(len(text), project.PROJECTION_BUDGET_CHARS)

    def test_composed_sweep_sits_right_after_frontier(self):
        """段序：frontier（要探索的）→ sweep（要派的）→ excluded（别碰的）。"""
        sections = project._compose_sections(
            world_parse(SWEEP_LINES), graph.parse(SWEEP_LINES)
        )
        names = [name for name, _ in sections]
        self.assertLess(names.index("FRONTIER"), names.index("SWEEP"))
        self.assertLess(names.index("SWEEP"), names.index("EXCLUDED"))


if __name__ == "__main__":
    unittest.main()
