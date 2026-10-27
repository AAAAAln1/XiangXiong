"""目标（GOAL）与交付物（FINDINGS）：投影的方向盘与计数器。

## 背景

Cairn_Y 相对 Cairn 的两处结构增补（作者原文）：

> **Goal** 描述项目的完成条件，即搜索的终止条件。也存在可以动态增删的 **Sub Goal**。
>
> **Finding** 是 Cairn_Y 引入的一个很重要的概念……渗透测试和代码审计**所需要的产物
> 更多是搜索过程中发现的漏洞**，而不是像"完成所有功能点测试与漏洞挖掘"这种最终 Goal。

对照象雄的两个洞：

1. **没有 Goal。** `[FRONTIER]` / `[SWEEP]` 是待办队列，不是完成条件。实测那次真实执行
   （会话 `71b3f780`）42 分钟 / 101 次 Bash / 199K token 峰值，**全程没有任何收工信号**，
   最后靠用户停下。没有 Goal 的搜索不会自己结束。
2. **`vuln:` 混在 `[WORLD MODEL]` 里**，与 `surface:` / `cred:` 并列 —— 交付物没有被
   结构性地标出来，读者得自己从一堆事实里认出"哪些能交付"。

## 修复

- 新前缀 `goal:` / `subgoal:`，kind=`goal`，ID `g:`，投影**首段** `[GOAL]`
- `[GOAL]` 未声明时**不静默**，给一行可直接抄的写法（与 `[SWEEP]` 段头给派发样板同理）
- `vuln:` 移出 WORLD MODEL，单独成段 `[FINDINGS]`
- `[GOAL]` 与 `[FINDINGS]` 构成唯一一对读数量：要什么 / 拿到了什么

## 锁死的不变量

1. `[GOAL]` 必须排在 `[TERRAIN]` **之前**（方向先于路况），且静止宪法不得回到首段
2. 没有声明 goal 时，`[GOAL]` 段仍然出现并给出写法 —— 空段会被读成"机制不存在"
3. `vuln:` 只出现在 `[FINDINGS]`，**不得**再出现在 `[WORLD MODEL]`
4. `[FINDINGS]` 为空时不占位（"还没挖到"是正常状态，不需要催）
5. `[FINDINGS]` 最后才被预算裁掉（它是交付物）
6. `goal` / `subgoal` 必须被全部四层（graph / world / merge / project）一致识别
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

GOAL_LINES = [
    "target: goal.example.test\n",
    "goal: confirm unauthorized access to /api/admin with differential evidence\n",
    "subgoal: establish an unauthenticated baseline\n",
    "subgoal: enumerate the admin API surface\n",
    "vuln: /api/x 未授权可读配置 (req_007)\n",
    "surface: /login 存在 (req_001)\n",
    "cred: demo cookie session=abc\n",
    "hypothesis [observed]: 已知 /api/user/list → 无 Authorization 头 GET → 期望 200 而非 401\n",
]

NO_GOAL_LINES = [
    "target: goal.example.test\n",
    "surface: /login 存在 (req_001)\n",
]


class TestGoalSection(unittest.TestCase):
    def setUp(self):
        self.view = world_parse(GOAL_LINES)
        self.section = digest.format_goal_section(self.view)

    def test_lists_goal_and_subgoals(self):
        self.assertIn("1 goal, 2 subgoal", self.section)
        self.assertIn("confirm unauthorized access", self.section)
        self.assertIn("subgoal: establish an unauthenticated baseline", self.section)
        self.assertIn("subgoal: enumerate the admin API surface", self.section)

    def test_header_carries_target(self):
        """段头必须带 target：执行任何命令都要它，而它原先埋在第 5 段。

        实测首屏 1,128 字符处才首次出现 target —— 首屏给不出"我在测哪个站"，
        读者得先翻页才能动手。
        """
        self.assertIn("target=goal.example.test", self.section.splitlines()[0])

    def test_goal_comes_before_subgoal(self):
        self.assertLess(self.section.index("goal: confirm"),
                        self.section.index("subgoal:"))

    def test_undeclared_goal_is_not_silent(self):
        """未声明时必须给写法 —— 空段会被读成"这机制不存在"。"""
        section = digest.format_goal_section(world_parse(NO_GOAL_LINES))
        self.assertIn("未声明完成条件", section)
        self.assertIn("goal: <", section)
        self.assertIn("subgoal: <", section)

    def test_undeclared_goal_still_occupies_a_section(self):
        """未声明不代表可以消失：它必须仍出现在装配结果里。"""
        lines = NO_GOAL_LINES
        sections = project._compose_sections(world_parse(lines), graph.parse(lines))
        names = [name for name, _ in sections]
        self.assertIn("GOAL", names)

    def test_subgoal_only_is_valid(self):
        section = digest.format_goal_section(world_parse([
            "target: t\n", "subgoal: 先拿到基线\n",
        ]))
        self.assertIn("0 goal, 1 subgoal", section)


class TestGoalIsTheFirstSection(unittest.TestCase):
    def test_goal_precedes_terrain(self):
        sections = project._compose_sections(world_parse(GOAL_LINES), graph.parse(GOAL_LINES))
        names = [name for name, _ in sections]
        self.assertEqual(names[0], "GOAL", f"首段必须是 GOAL，实际 {names[:4]}")
        self.assertLess(names.index("GOAL"), names.index("TERRAIN"))

    def test_goal_survives_even_under_extreme_budget(self):
        """目标被裁掉 = 搜索失去终止条件。它是最后一个该被动的东西。"""
        lines = ["target: budget.example.test\n", "goal: 找到 RCE\n"]
        for i in range(500):
            lines.append(f"surface: /api/v1/obj{i} 返回 200 (req_{i:04d})\n")
            lines.append(f"hypothesis [observed]: /api/v1/obj{i} → 无认证 GET → 期望 200\n")
        sections = project._compose_sections(world_parse(lines), graph.parse(lines))
        out = project._enforce_budget(sections, project.PROJECTION_BUDGET_CHARS)
        text = "".join(body for _, body in out)
        self.assertIn("找到 RCE", text, "极端预算下目标被裁掉了")
        self.assertLessEqual(len(text), project.PROJECTION_BUDGET_CHARS)

    def test_goal_is_never_in_trim_order(self):
        self.assertNotIn("GOAL", project.SECTION_TRIM_ORDER,
                         "GOAL 不得进入裁剪列表")


class TestFindingsSection(unittest.TestCase):
    def setUp(self):
        self.view = world_parse(GOAL_LINES)
        self.findings = project.format_findings_section(self.view)
        self.world = project.format_world_section(self.view)

    def test_findings_collects_vuln_lines(self):
        self.assertIn("[FINDINGS] (1)", self.findings)
        self.assertIn("/api/x 未授权可读配置", self.findings)

    def test_vuln_no_longer_in_world_model(self):
        """核心回归：交付物埋在 objects 里等于要求读者自己认出来。"""
        self.assertNotIn("vuln:", self.world)
        self.assertNotIn("/api/x 未授权可读配置", self.world)

    def test_world_model_keeps_surface_and_cred(self):
        self.assertIn("surface: /login 存在", self.world)
        self.assertIn("cred: demo cookie", self.world)

    def test_empty_findings_do_not_occupy_space(self):
        """「还没挖到」是正常状态，不需要催。"""
        self.assertEqual(project.format_findings_section(world_parse(NO_GOAL_LINES)), "")

    def test_findings_survive_budget(self):
        lines = ["target: f.example.test\n"]
        for i in range(600):
            lines.append(f"surface: /api/v1/obj{i} (req_{i:04d})\n")
        lines.append("vuln: /api/admin 未授权读取用户列表 (req_9999)\n")
        sections = project._compose_sections(world_parse(lines), graph.parse(lines))
        out = project._enforce_budget(sections, project.PROJECTION_BUDGET_CHARS)
        text = "".join(body for _, body in out)
        self.assertIn("[FINDINGS]", text, "交付物被预算裁掉了")
        self.assertIn("/api/admin 未授权", text)

    def test_findings_is_trimmed_last(self):
        order = list(project.SECTION_TRIM_ORDER)
        for earlier in ("SETTLED", "DIFFS", "WORLD MODEL", "FRONTIER", "SWEEP"):
            self.assertLess(order.index(earlier), order.index("FINDINGS"),
                            f"{earlier} 应在 FINDINGS 之前被牺牲")

    def test_findings_sits_between_terrain_and_frontier(self):
        sections = project._compose_sections(world_parse(GOAL_LINES), graph.parse(GOAL_LINES))
        names = [name for name, _ in sections]
        self.assertLess(names.index("TERRAIN"), names.index("FINDINGS"))
        self.assertLess(names.index("FINDINGS"), names.index("FRONTIER"))


class TestSchemaRecognisesGoalEverywhere(unittest.TestCase):
    """四层必须一致：graph / world / merge / project。漏一层就是静默丢行。"""

    def test_graph_kind_and_id_prefix(self):
        g = graph.parse(GOAL_LINES)
        goals = g.by_kind(graph.KIND_GOAL)
        self.assertEqual(len(goals), 3, "1 goal + 2 subgoal")
        self.assertEqual(graph.ID_PREFIX[graph.KIND_GOAL], "g:")
        for node in goals:
            self.assertTrue(node.id.startswith("g:"), f"ID 前缀错误: {node.id}")

    def test_world_parse_buckets_them_as_objects(self):
        view = world_parse(GOAL_LINES)
        prefixes = {item.prefix for item in view.objects}
        self.assertIn("goal", prefixes)
        self.assertIn("subgoal", prefixes)

    def test_merge_accepts_goal_lines(self):
        ok, reason = evidence_gate("goal: 找到一个 RCE", [])
        self.assertTrue(ok, reason)
        ok, reason = evidence_gate("subgoal: 拿到管理员凭据", [])
        self.assertTrue(ok, reason)

    def test_merge_rejects_naked_goal_prefix(self):
        """事务边界防御：裸 `goal:` 不得写入（半截 DELTA 的典型特征）。"""
        from runtime.merge import _is_valid_line
        self.assertFalse(_is_valid_line("goal:\n"))
        self.assertFalse(_is_valid_line("subgoal:   \n"))
        self.assertTrue(_is_valid_line("goal: 找到 RCE\n"))

    def test_goal_lines_are_not_evidence_gated(self):
        """goal 不是断言，不需要证据引用（它不会被 REJECT）。"""
        ok, _ = evidence_gate("goal: 完成所有功能点测试", [])
        self.assertTrue(ok)

    def test_terrain_counts_goal(self):
        g = graph.parse(GOAL_LINES)
        rows = dict(((kind, state), n) for kind, state, n in graph.terrain(g))
        self.assertEqual(rows[(graph.KIND_GOAL, None)], 3)

    def test_goal_lines_roundtrip_losslessly(self):
        """无损是第一性质：goal 行不能被 normalize 掉。"""
        g = graph.parse(GOAL_LINES)
        self.assertEqual("".join(graph.render(g)), "".join(GOAL_LINES))


class TestStepSemanticsContract(unittest.TestCase):
    """Step 的因果链写法（Cairn_Y：Step 是"推动世界状态演化的因果过程"）。

    这只能靠契约约束，不能机械校验 —— 所以这里锁死"契约里必须有这条要求"，
    防止后续有人把这段删掉，让 hypothesis 退回"愿望"语气。
    """

    def test_claude_md_requires_causal_chain(self):
        path = os.path.join(PROJECT_ROOT, "CLAUDE.md")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("因果链", body, "CLAUDE.md 必须要求 hypothesis 写因果链")
        self.assertIn("<出发事实>", body)
        self.assertIn("<做什么操作>", body)
        self.assertIn("<期望得到什么事实>", body)
        self.assertIn("愿望", body, "必须点明「愿望写法」是错的")

    def test_claude_md_declares_goal_prefix(self):
        path = os.path.join(PROJECT_ROOT, "CLAUDE.md")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("`goal`", body)
        self.assertIn("`subgoal`", body)
        self.assertIn("第一件事", body, "必须说清 goal 的时机（接手任务时）")

    def test_merge_documents_causal_chain(self):
        path = os.path.join(PROJECT_ROOT, "runtime", "merge.py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("因果链", body)


class TestProjectionCarriesGoalEndToEnd(unittest.TestCase):
    """真实 compile()：goal/findings 必须出现在最终注入文本里，且顺序正确。"""

    def _compile_lines(self, lines):
        sections = project._compose_sections(world_parse(lines), graph.parse(lines))
        return "".join(body for _, body in project._enforce_budget(
            sections, project.PROJECTION_BUDGET_CHARS))

    def test_goal_and_findings_present_in_order(self):
        text = self._compile_lines(GOAL_LINES)
        self.assertIn("[GOAL]", text)
        self.assertIn("[FINDINGS]", text)
        self.assertLess(text.index("[GOAL]"), text.index("[TERRAIN]"))
        self.assertLess(text.index("[TERRAIN]"), text.index("[FINDINGS]"))
        self.assertLess(text.index("[FINDINGS]"), text.index("[FRONTIER]"))
        self.assertNotIn("[COGNITION]", text)

    def test_goal_section_content_is_actionable(self):
        text = self._compile_lines(GOAL_LINES)
        self.assertIn("confirm unauthorized access", text)
        self.assertIn("subgoal: establish an unauthenticated baseline", text)

    def test_missing_goal_projection_prompts_for_one(self):
        text = self._compile_lines(NO_GOAL_LINES)
        self.assertIn("未声明完成条件", text)
        self.assertIn("goal: <", text)


class TestRealWorldStillCompiles(unittest.TestCase):
    """真实 memory（若存在）在新增两段后仍必须落在预算内。"""

    def test_live_projection_within_budget(self):
        projection = project.compile()
        self.assertLessEqual(
            len(projection), project.PROJECTION_BUDGET_CHARS,
            "真实投影超出预算",
        )

    def test_live_projection_starts_with_goal(self):
        projection = project.compile()
        if not projection.strip():
            self.skipTest("无活跃 world")
        self.assertTrue(projection.lstrip().startswith("[GOAL]"))


if __name__ == "__main__":
    unittest.main()
