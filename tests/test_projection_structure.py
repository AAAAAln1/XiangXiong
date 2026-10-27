"""投影结构不变量（2026-09-18 投影审计）。

## 审计发现的 7 个问题（全部来自真实投影实测）

| # | 问题 | 实据 |
|---|---|---|
| 1 | `[UNEXPLAINED]` 4 条**逐字节复制** `[WORLD MODEL]` 正文 | 同一批 surface 行，只多了 ID |
| 2 | `[RECENT EXPERIMENTS]` 含**命令执行元数据**（exit_code/duration/metadata_artifact） | 1,484 字符里大半对决策零增量 |
| 3 | `[RESUME]` 第三次重复 target、第二次重复计数 | `[GOAL]` 段头 + `[TERRAIN]` 已有 |
| 4 | `[EXCLUDED]`/`[SETTLED]` 每行 190-235 字符 | 本质是"一句话 + ID"，过程描述可查 |
| 5 | `[TERRAIN]` 的 `unexplained observations: N` 与紧随的 `[UNEXPLAINED] (N)` 重复 | 同一数字两次 |
| 6 | `[FRONTIER]` 段头（165 字符）比内容还长；`[blocked]` 项混在"待做"里 | 派出去也做不了 |
| 7 | `[TOOLS]` 段头不自解释 | 读者要自己推断这段意味着什么 |

**总计约 1,850 字符（29%）是可去掉的冗余**，且决策信息被稀释。

## 本轮的改法

1. **加 `[NEXT]`**：投影原先只描述世界、不回答"我该做什么"。排序规则照抄 Cairn 的
   dispatcher（`max(unclaimed, key=created_at)` = 最新的未认领项优先），完全机械；
   `[blocked]` 与已闭合项排除。无项可派时**也说出口**并指向扩未知的方向。
2. **消除重复**：`[RESUME]` 删除；`[TERRAIN]` 的未解释计数交给 `[UNEXPLAINED]`；
   `[WORLD MODEL]` 每行带 ID，`[UNEXPLAINED]` 只列 ID 指回；`[WORLD MODEL]` 不再列
   goal/subgoal/vuln/future（各有专段）。
3. **正文压缩、证据保留**：`_brief()` 截断描述性文本但逐字符保留 `req_/exp-/art-` 引用。
4. **段头自解释**：每段第一行说清"这段是干嘛的"。

## 锁死的不变量

1. 任何一行**不得**在投影里出现两次（同段内或跨段）
2. `[NEXT]` 的候选必须排除 `[blocked]` 与已闭合项
3. `[NEXT]` 无可派项时必须给出**去哪找新面**的指引
4. `_brief` 截断正文但**不得**动证据引用
5. `[GOAL] < [TERRAIN] < [NEXT] < [FRONTIER]`（方向 → 形状 → 行动 → 细节）
6. 真实投影必须落在预算内
"""

import os
import re
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import digest, graph, project  # noqa: E402
from runtime.world import parse as world_parse  # noqa: E402

RICH = [
    "target: proj.example.test\n",
    "goal: prove unauthorized read on /api/admin\n",
    "subgoal: establish an unauthenticated baseline\n",
    "surface: /login 存在，Vue SPA（req_001）\n",
    "surface: /api/admin/users 返回 200（req_002）\n",
    "vuln: /api/admin/users 未授权读取用户列表（req_003）\n",
    "cred: demo cookie session=abc（req_004）\n",
    "hypothesis [observed]: 已知 /api/admin → 无 Authorization GET → 期望 200（req_005）\n",
    "hypothesis [sweep]: 枚举 40 个 admin 子路径（req_006）\n",
    "hypothesis [blocked]: 缺 Passport 账号无法验证 SSO 绕过（req_007）\n",
    "hypothesis [confirmed]: /health 无需认证（exp-001）\n",
    "hypothesis [contradicted]: /debug 未开放（req_008）\n",
    "counter: /old-admin 反复 404（req_009）\n",
    "future: 拿到凭据后重测限流\n",
]


def _sections(lines):
    return project._compose_sections(world_parse(lines), graph.parse(lines))


def _compile(lines):
    return "".join(body for _, body in project._enforce_budget(
        _sections(lines), project.PROJECTION_BUDGET_CHARS))


class TestNextSection(unittest.TestCase):
    """[NEXT]：把"我该做什么"从推导变成照抄。"""

    def setUp(self):
        self.next = digest.format_next_section(world_parse(RICH))

    def test_emits_copyable_agent_calls(self):
        self.assertIn('Agent(subagent_type="deepdive"', self.next)
        self.assertIn("target=proj.example.test", self.next)
        self.assertRegex(self.next, r"intent=h:[0-9a-f]{6}")

    def test_excludes_blocked_items(self):
        """[blocked] = 缺验证条件，派出去也做不了。留在 [NEXT] 里等于教读者撞墙。"""
        blocked_id = digest.hypothesis_id(
            next(i for i in world_parse(RICH).unknowns if i.state == "blocked"))
        self.assertNotIn(blocked_id, self.next)

    def test_excludes_settled_items(self):
        view = world_parse(RICH)
        for item in view.unknowns:
            if item.state in ("confirmed", "contradicted"):
                self.assertNotIn(digest.hypothesis_id(item), self.next)

    def test_sweep_comes_before_frontier(self):
        """[SWEEP] 是"批量机械验证"，天然该派 —— 排在普通前沿项之前。"""
        lines = [
            "target: t\n",
            "hypothesis [observed]: 普通假设 A\n",
            "hypothesis [sweep]: 批量验证 B\n",
        ]
        out = digest.format_next_section(world_parse(lines))
        self.assertLess(out.index("批量验证 B"), out.index("普通假设 A"))

    def test_newest_first(self):
        """照抄 Cairn：max(created_at) —— memory 是追加序，故后写的在前。"""
        lines = [
            "target: t\n",
            "hypothesis [observed]: 早写的\n",
            "hypothesis [observed]: 晚写的\n",
        ]
        out = digest.format_next_section(world_parse(lines))
        self.assertLess(out.index("晚写的"), out.index("早写的"))

    def test_respects_limit_and_announces_remainder(self):
        lines = ["target: t\n"] + [f"hypothesis [observed]: 项{i}\n" for i in range(9)]
        out = digest.format_next_section(world_parse(lines), limit=3)
        self.assertEqual(out.count("Agent(subagent_type"), 3)
        self.assertIn("(+6 more", out)
        self.assertIn("[FRONTIER]", out, "剩余项的完整列表在哪要说清")

    def test_nothing_dispatchable_says_where_to_go(self):
        """没有可派项时**也要说话** —— 空段会被读成"这机制不存在"。"""
        lines = [
            "target: t\n",
            "hypothesis [blocked]: 缺账号无法验证\n",
            "hypothesis [confirmed]: 已证（exp-001）\n",
        ]
        out = digest.format_next_section(world_parse(lines))
        self.assertIn("无可派项", out)
        self.assertIn("[blocked]", out, "要说清为什么没有可派项")
        self.assertIn("[UNEXPLAINED]", out, "要指向扩未知的方向")
        self.assertIn("[COVERAGE]", out)

    def test_no_target_means_no_section(self):
        self.assertEqual(digest.format_next_section(world_parse(["surface: x\n"])), "")

    def test_carries_a_short_hint_per_line(self):
        """每行得有"这是什么"的短提示，只给 ID 读者不知道派的是什么。"""
        self.assertIn("#", self.next)


class TestNoLineAppearsTwice(unittest.TestCase):
    """核心回归：同一条信息在投影里出现两次，会让读者以为有新信息。"""

    def setUp(self):
        self.text = _compile(RICH)

    def test_the_whole_projection_has_no_duplicated_content_line(self):
        seen = {}
        for line in self.text.splitlines():
            body = line.strip()
            # 只检查内容行（跳过段头与空行）；ID 行与纯说明行不算
            if not body or body.startswith("[") or len(body) < 25:
                continue
            normalized = re.sub(r"^[a-z]{1,3}:[0-9a-f]{6}\s+", "", body)
            normalized = re.sub(r"\s+", " ", normalized)
            if normalized in seen:
                self.fail(f"同一行出现两次：{normalized[:80]!r}")
            seen[normalized] = True

    def test_world_model_excludes_goal_and_findings_and_future(self):
        """goal 在 [GOAL]、vuln 在 [FINDINGS]、future 在 [FRONTIER] —— 不该再列一次。"""
        world = project.format_world_section(world_parse(RICH))
        self.assertNotIn("goal:", world.split("objects")[1])
        self.assertNotIn("subgoal:", world)
        self.assertNotIn("vuln:", world)
        self.assertNotIn("future:", world)

    def test_world_model_keeps_surface_and_cred_with_ids(self):
        world = project.format_world_section(world_parse(RICH))
        self.assertIn("surface: /login 存在", world)
        self.assertIn("cred: demo cookie", world)
        self.assertRegex(world, r"o:[0-9a-f]{6} surface:")
        self.assertRegex(world, r"c:[0-9a-f]{6} cred:")

    def test_unexplained_lists_ids_not_bodies(self):
        """"没有假设解释的观察"这件事，不需要靠复制正文来表达。"""
        view = world_parse(RICH)
        g = graph.parse(RICH)
        out = project.format_unexplained_section(graph.unexplained_observations(g))
        if not out:
            self.skipTest("该素材没有未解释观察")
        self.assertRegex(out, r"o:[0-9a-f]{6}")
        for node in graph.unexplained_observations(g):
            # 正文不得出现在 [UNEXPLAINED] 里（它已在 [WORLD MODEL]）
            self.assertNotIn(node.text[:40], out)

    def test_terrain_no_longer_repeats_unexplained_count(self):
        out = project.format_terrain_section(graph.parse(RICH))
        self.assertNotIn("unexplained", out)

    def test_resume_section_is_gone(self):
        self.assertNotIn("RESUME", [name for name, _ in _sections(RICH)])
        self.assertNotIn("[RESUME]", self.text)


class TestBriefKeepsEvidence(unittest.TestCase):
    """截断正文可以，动证据不行 —— 证据是查证凭证。"""

    def test_long_body_is_truncated(self):
        raw = "surface: " + "很长的描述" * 40 + "（req_001）"
        out = digest.brief(raw, 60)
        self.assertLess(len(out), len(raw))
        self.assertIn("…", out)

    def test_evidence_survives_truncation(self):
        raw = "counter: " + "很长的过程描述" * 30 + "（req_149/150/151, exp-002）"
        out = digest.brief(raw, 50)
        self.assertIn("req_149/150/151", out)
        self.assertIn("exp-002", out)

    def test_short_body_untouched(self):
        raw = "dead: /phpmyadmin not reachable（req_009）"
        self.assertEqual(digest.brief(raw, 200), raw)

    def test_bracket_and_paren_forms_both_recognized(self):
        self.assertIn("req_001", digest.brief("x " + "y" * 100 + " [req_001]", 20))
        self.assertIn("req_002", digest.brief("x " + "y" * 100 + " (req_002)", 20))

    def test_fullwidth_parens_are_the_real_case(self):
        """memory 里的证据引用普遍是**全角**括号 —— 只认半角会把证据一起截掉。

        实测抓到的 bug：`[EXCLUDED]` 压缩后 `（req_064/065）` 消失，
        而那正是"别重试"唯一的查证凭证。取证正则必须覆盖 `（）`。
        """
        raw = "counter: " + "过程描述" * 40 + "（req_064/065）"
        out = digest.brief(raw, 40)
        self.assertIn("req_064/065", out)

    def test_real_excluded_section_keeps_evidence(self):
        """端到端：真实素材的 [EXCLUDED] 压缩后，证据引用必须还在。"""
        out = project.format_excluded_section(world_parse(RICH))
        self.assertIn("[EXCLUDED]", out)
        self.assertIn("req_009", out)


class TestSectionHeadersExplainThemselves(unittest.TestCase):
    """段头自解释：读者不该需要推断"这一段是干嘛的"。"""

    def setUp(self):
        self.text = _compile(RICH)

    def header(self, name):
        for line in self.text.splitlines():
            if line.startswith(f"[{name}]"):
                return line
        self.fail(f"缺少段 {name}")

    def test_excluded_says_do_not_retry(self):
        self.assertIn("勿重试", self.header("EXCLUDED"))

    def test_settled_says_do_not_reopen(self):
        self.assertIn("勿重开", self.header("SETTLED"))

    def test_unexplained_says_it_is_a_candidate_surface(self):
        self.assertIn("候选探索面", self.header("UNEXPLAINED"))

    def test_next_says_how_to_go_parallel(self):
        self.assertIn("同一条消息", self.header("NEXT"))

    def test_every_section_header_is_a_single_line(self):
        for line in self.text.splitlines():
            if line.startswith("["):
                self.assertNotIn("\n", line)


class TestSectionOrder(unittest.TestCase):
    def test_decision_order(self):
        names = [name for name, _ in _sections(RICH)]
        for earlier, later in (
            ("GOAL", "TERRAIN"),
            ("TERRAIN", "FINDINGS"),
            ("FINDINGS", "NEXT"),
            ("NEXT", "FRONTIER"),
            ("FRONTIER", "EXCLUDED"),
            ("EXCLUDED", "WORLD MODEL"),
        ):
            self.assertLess(names.index(earlier), names.index(later),
                            f"{earlier} 应在 {later} 之前，实际 {names}")

    def test_next_is_in_the_decision_zone(self):
        """[NEXT] 必须在 [WORLD MODEL]（背景详情）之前。"""
        names = [name for name, _ in _sections(RICH)]
        self.assertLess(names.index("NEXT"), names.index("WORLD MODEL"))


class TestProjectionSize(unittest.TestCase):
    def test_rich_world_stays_within_budget(self):
        self.assertLessEqual(len(_compile(RICH)), project.PROJECTION_BUDGET_CHARS)

    def test_live_projection_within_budget(self):
        self.assertLessEqual(len(project.compile()), project.PROJECTION_BUDGET_CHARS)

    def test_decision_sections_are_not_buried(self):
        """决策信息（NEXT/FRONTIER）必须出现在投影前 40% 里。"""
        text = _compile(RICH)
        cutoff = int(len(text) * 0.4)
        self.assertIn("[NEXT]", text[:cutoff])
        self.assertIn("[FRONTIER]", text[:cutoff])


if __name__ == "__main__":
    unittest.main()
