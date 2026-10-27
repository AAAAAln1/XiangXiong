"""记账后回吐决策摘要（2026-09-18）。

## 为什么需要它

投影**只在 SessionStart 注入一次**，而一次真实执行是 40 分钟 / 101 次 Bash /
若干条 DELTA —— 账本一直在变，投影从头到尾没变。调用方于是在**自知过期**的视图上
做决策：`[NEXT]` 里的项可能早已闭合、新写的项根本不在里面。

刷新机制其实一直都有（`python runtime/project.py`），缺的是**在那个时刻告诉它有**。
本文件锁死"什么时候说、说什么"。

## 判据是机械的，且刻意不是"整个摘要变没变"

| 这次记账 | 下一步变了吗 | 回吐 |
|---|---|---|
| 只加一条 `surface:`（最高频） | 没变 | **不打扰** |
| 新增 / 闭合 / 改状态一条 hypothesis | 变了 | 回吐 |
| 加 / 改 `goal:` `subgoal:`，或换 target | 变了 | 回吐 |
| 加 `[sweep]` 行 | 变了（进派发队列首位） | 回吐 |

`surface:` 那条尤其重要：它是最高频的记账动作，而且它会改变"未解释观察数"
（形状行的一部分）。如果判据取整个摘要，最高频的动作就会触发回吐 → 噪声。
`test_surface_only_does_not_echo_even_though_shape_changed` 专门锁这一点。

## 锁死的不变量

1. 判据是 `[GOAL]` + `[NEXT]` 的**逐字节**比较，与形状计数无关
2. 回吐正文与 SessionStart 投影**逐字节同源**（共用 `digest.py`）——
   不允许出现"两个版本的 `[NEXT]`"
3. 回吐里必须给出看全文的指针（`project.py`）
4. 旧的局部提示（`warn_sweep_queue`）已删除：两套提示 = 两个版本的队列
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import digest, graph, project  # noqa: E402
from runtime.world import parse as world_parse  # noqa: E402

TARGET = "digest.example.test"
BASE = [f"target: {TARGET}\n"]

HYP_A = "hypothesis [observed]: 已知 /api/admin → 无 Authorization GET → 期望 200（req_001）\n"


class _Workspace:
    """临时 world 文件 + env 覆盖（`paths.world_active()` 的唯一开关）。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="xx_digest_")
        self.world = os.path.join(self.dir, "memory.md")
        self._saved = os.environ.get("XIANGXIONG_WORLD_ACTIVE")

    def write(self, lines):
        with open(self.world, "w", encoding="utf-8") as handle:
            handle.writelines(lines)

    def read(self):
        with open(self.world, "r", encoding="utf-8") as handle:
            return handle.readlines()

    def activate(self):
        os.environ["XIANGXIONG_WORLD_ACTIVE"] = self.world

    def merge(self, delta_lines):
        """跑一次真实的 merge CLI，返回 stderr。"""
        delta = os.path.join(self.dir, "_delta.txt")
        with open(delta, "w", encoding="utf-8") as handle:
            handle.write("DELTA:\n" + "".join(delta_lines))
        env = dict(os.environ)
        env["XIANGXIONG_WORLD_ACTIVE"] = self.world
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "runtime", "merge.py"),
             "--delta-file", delta],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        self.assert_zero(proc)
        return proc.stderr

    @staticmethod
    def assert_zero(proc):
        if proc.returncode != 0:
            raise AssertionError(f"merge 退出码 {proc.returncode}\n{proc.stderr}")

    def cleanup(self):
        if self._saved is None:
            os.environ.pop("XIANGXIONG_WORLD_ACTIVE", None)
        else:
            os.environ["XIANGXIONG_WORLD_ACTIVE"] = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)


class DigestTestCase(unittest.TestCase):
    def setUp(self):
        self.ws = _Workspace()
        self.addCleanup(self.ws.cleanup)

    def zone_of(self, lines):
        self.ws.write(lines)
        self.ws.activate()
        return digest.format_decision_zone(world_parse(lines))


class TestEchoGate(DigestTestCase):
    """什么时候说 —— 判据机械，且能逐条复述。"""

    def test_new_hypothesis_echoes(self):
        self.ws.write(BASE)
        err = self.ws.merge([HYP_A])
        self.assertIn("决策摘要", err)
        self.assertIn("[NEXT]", err)

    def test_surface_only_does_not_echo(self):
        """最高频的记账动作不该打扰 —— 它不改"下一步做什么"。"""
        self.ws.write(BASE)
        err = self.ws.merge(["surface: /login 存在，Vue SPA（req_001）\n"])
        self.assertNotIn("决策摘要", err)
        self.assertIn("ADD:1", err, "行本身要照常写进去")

    def test_surface_only_does_not_echo_even_though_shape_changed(self):
        """关键：形状行会因新 surface 而变（未解释 +1），但判据不看它。

        若判据取整个摘要，这条会回吐 —— 于是最高频动作全部触发回吐。
        （需要一条已存在的 hypothesis，未解释计数才有"被解释"这个基准。）
        """
        base = BASE + [HYP_A]
        self.ws.write(base)
        self.ws.activate()
        before_zone = digest.format_decision_zone(world_parse(base))

        added = "surface: /api/other 存在（req_002）\n"
        after_lines = base + [added]

        # 前置事实：形状确实变了（未解释 0 → 1）
        self.assertNotEqual(
            digest.format_world_shape(world_parse(base), graph.parse(base)),
            digest.format_world_shape(world_parse(after_lines), graph.parse(after_lines)),
        )
        # 而判据不变
        self.assertEqual(digest.format_decision_zone(world_parse(after_lines)), before_zone)
        # 端到端：所以不回吐
        self.assertNotIn("决策摘要", self.ws.merge([added]))

    def test_sweep_line_echoes(self):
        """旧的 `warn_sweep_queue` 只覆盖这一种；现在由摘要统一覆盖。"""
        self.ws.write(BASE)
        err = self.ws.merge(["hypothesis [sweep]: 枚举 20 个 admin 子路径\n"])
        self.assertIn("决策摘要", err)
        self.assertIn("sweep 1", err)

    def test_goal_line_echoes(self):
        self.ws.write(BASE)
        err = self.ws.merge(["goal: 证明 /api/admin 存在未授权读取\n"])
        self.assertIn("决策摘要", err)
        self.assertIn("goal: 证明", err)

    def test_removing_a_hypothesis_echoes(self):
        """删掉一条假设 → 它离开派发队列 → 下一步变了。"""
        self.ws.write(BASE + [HYP_A])
        err = self.ws.merge(["[REMOVE] " + HYP_A.strip() + "\n"])
        self.assertIn("决策摘要", err)
        self.assertIn("无可派项", err)

    def test_blocking_a_hypothesis_echoes(self):
        """标成 [blocked] → 它被移出可派队列（派了也做不了）→ 回吐并说明原因。"""
        self.ws.write(BASE + [HYP_A])
        err = self.ws.merge([
            "hypothesis [blocked]: 已知 /api/admin → 无 Authorization GET → 期望 200\n"
        ])
        self.assertIn("决策摘要", err)
        self.assertIn("[blocked]", err)
        self.assertIn("无可派项", err)

    def test_target_change_echoes(self):
        self.ws.write(BASE)
        err = self.ws.merge(["target: other.example.test\n"])
        self.assertIn("决策摘要", err)
        self.assertIn("other.example.test", err)

    def test_rejected_line_does_not_echo(self):
        """全被闸门拒掉 → 世界没变 → 不打扰。"""
        self.ws.write(BASE)
        err = self.ws.merge(["vuln: /x 存在越权\n"])  # 缺证据引用 → REJECT
        self.assertIn("REJECTED", err)
        self.assertNotIn("决策摘要", err)


class TestEchoContent(DigestTestCase):
    """说什么。"""

    def test_echo_carries_copyable_dispatch_call(self):
        self.ws.write(BASE)
        err = self.ws.merge([HYP_A])
        self.assertIn('Agent(subagent_type="deepdive"', err)
        self.assertIn(f"target={TARGET}", err)
        self.assertRegex(err, r"intent=h:[0-9a-f]{6}")

    def test_echo_points_at_the_full_projection(self):
        """摘要不是投影的替代品 —— 要看全文得知道在哪看。"""
        self.ws.write(BASE)
        err = self.ws.merge([HYP_A])
        self.assertIn("python runtime/project.py", err)

    def test_echo_carries_world_shape(self):
        self.ws.write(BASE)
        err = self.ws.merge([HYP_A])
        self.assertIn("可派 1", err)
        self.assertIn("闭合 0", err)

    def test_echo_does_not_dump_background_sections(self):
        """背景与历史不进回吐 —— 那要查账时看全文。"""
        self.ws.write(BASE + ["counter: /old-admin 反复 404（req_009）\n"])
        err = self.ws.merge([HYP_A])
        self.assertIn("决策摘要", err)
        self.assertNotIn("[WORLD MODEL]", err)
        self.assertNotIn("[SETTLED]", err)
        self.assertNotIn("[EXCLUDED]", err)


class TestEchoIsByteIdenticalToProjection(DigestTestCase):
    """核心守卫：回吐与投影的 `[GOAL]`/`[NEXT]` 必须逐字节相同。

    两处各写一份渲染是这套架构翻过两次车的模式（见 `digest.py` 顶部）。
    """

    RICH = BASE + [
        "goal: 证明 /api/admin 存在未授权读取\n",
        "subgoal: 先建立未认证基线\n",
        "surface: /login 存在，Vue SPA（req_001）\n",
        "hypothesis [observed]: 已知 /api/admin → 无 Authorization GET → 期望 200（req_002）\n",
        "hypothesis [sweep]: 枚举 40 个 admin 子路径\n",
        # 全角证据（真实 memory 的常见形态）
        "hypothesis [contradicted]: /debug 未开放（req_003, exp-001）\n",
        "future: 拿到凭据后重测限流\n",
    ]

    def test_zone_equals_the_sum_of_projection_sections(self):
        self.ws.write(self.RICH)
        self.ws.activate()
        view = world_parse(self.RICH)
        sections = dict(project._compose_sections(view, graph.parse(self.RICH)))
        self.assertEqual(
            sections["GOAL"] + sections["NEXT"],
            digest.format_decision_zone(view),
        )

    def test_merge_echo_contains_the_very_same_zone(self):
        """端到端：merge 吐出来的那两段，与投影里的那两段逐字节相同。"""
        self.ws.write(BASE)
        err = self.ws.merge(self.RICH[1:])  # 除 target 外的全部内容
        self.assertIn("决策摘要", err)
        # 写盘后用同一份 world 算投影的决策区
        self.ws.activate()
        view = world_parse(self.ws.read())
        zone = digest.format_decision_zone(view)
        self.assertIn(zone, err, "回吐里的 [GOAL]/[NEXT] 必须与投影逐字节一致")

    def test_ids_are_stable_across_echo_and_projection(self):
        """ID 是指称：回吐给的 `intent=h:xxxxxx` 必须能在投影里指到同一行。"""
        self.ws.write(self.RICH)
        self.ws.activate()
        view = world_parse(self.RICH)
        zone = digest.format_decision_zone(view)
        ids = set(re.findall(r"h:[0-9a-f]{6}", zone))
        self.assertTrue(ids)
        projection = project.compile()
        for intent_id in ids:
            self.assertIn(intent_id, projection)


class TestLegacyHintIsGone(unittest.TestCase):
    def test_warn_sweep_queue_removed(self):
        """留着它 = 两个版本的队列（一个说 [SWEEP]，一个说 [NEXT]）。"""
        from runtime import merge

        self.assertFalse(hasattr(merge, "warn_sweep_queue"))
        src = open(os.path.join(PROJECT_ROOT, "runtime", "merge.py"),
                   encoding="utf-8").read()
        self.assertNotIn("SWEEP_LINE_PREFIX", src)

    def test_claude_md_tells_the_agent_the_digest_exists(self):
        """回吐会出现在 tool 输出里；不说明的话它可能被当噪声跳过。"""
        claude = open(os.path.join(PROJECT_ROOT, "CLAUDE.md"), encoding="utf-8").read()
        self.assertIn("决策摘要", claude)


if __name__ == "__main__":
    unittest.main()
