"""投影预算不变量（宿主硬上限回归）。

## 为什么需要这组测试

宿主 Claude Code 对 SessionStart hook 的 `additionalContext` 有**硬上限**：
超出即不再注入原文，只注入前 2,000 字符预览
（claude-code 2.1.x：`Vz8(additionalContext, hookId, "additionalContext")`，
缺省阈值 `pP4 = 10000`，预览长度 `KL6 = 2000`）。

2026-09-17 首次真实执行踩到了它：
投影 10,408 字符 → 宿主只注入前 2,000 字符 → 那正好全是 `[COGNITION]`
→ `[TERRAIN]`/`[FRONTIER]`/`[EXCLUDED]`/`[WORLD MODEL]` 一个都不可见
→ agent 开局没有可认领项 → 认领 / 实验 / 深挖全部归零。

即：**投影超上限时，agent 看到的不是"缩小的世界"，而是"没有世界"。**

## 锁死的不变量

1. `compile()` 的返回值恒 `<= PROJECTION_BUDGET_CHARS`，且该值 `<` 宿主上限。
2. 裁剪必须显式：被裁的段要写明丢了多少行、全文在哪（不得静默丢失）。
3. 决策段在重压下仍必须存活：[TERRAIN] / [FRONTIER] / [EXCLUDED] / [COVERAGE]。
4. 裁剪顺序固定：先裁定局与历史，最后才动前沿。
"""

import os
import re
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import project  # noqa: E402
from runtime.graph import parse as graph_parse  # noqa: E402
from runtime.world import parse as world_parse  # noqa: E402

# 宿主实测常量（claude-code 2.1.x cli.js）。改动它们之前先重测宿主。
HOST_HOOK_INJECTION_CAP = 10000
HOST_PREVIEW_CHARS = 2000


def _big_world(n=400):
    """远超任何预算的人造世界：定局、前沿、排除、观察全都有量。"""
    lines = ["target: budget.example.test\n"]
    for i in range(n):
        lines.append(f"surface: /api/v1/obj{i} 返回 200 (req_{i:04d})\n")
        lines.append(f"cred: token{i} for obj{i}\n")
        lines.append(f"hypothesis [observed]: /api/v1/obj{i} 是否可枚举\n")
        lines.append(f"hypothesis [confirmed]: /api/v1/obj{i} 边界已确认 (req_{i:04d})\n")
        lines.append(f"counter: /api/v1/obj{i} 参数无影响 (req_{i:04d})\n")
        lines.append(f"dead: /api/v1/old{i} 不可达 (req_{i:04d})\n")
        lines.append(f"future: 拿到凭据后重测 obj{i} 限流\n")
    return lines


class TestHostCapAlignment(unittest.TestCase):
    def test_budget_is_strictly_below_host_cap(self):
        self.assertLess(
            project.PROJECTION_BUDGET_CHARS,
            HOST_HOOK_INJECTION_CAP,
            "投影预算必须严格小于宿主上限，否则超限时只注入预览 = 世界不可见",
        )

    def test_budget_leaves_real_headroom(self):
        """不得贴边上限：宿主按 UTF-16 计长，且世界会持续增长。"""
        self.assertLessEqual(
            project.PROJECTION_BUDGET_CHARS,
            HOST_HOOK_INJECTION_CAP * 9 // 10,
            "预算不得贴边宿主上限，至少留 10% 余量",
        )

    def test_preview_would_have_shown_only_static_constitution(self):
        """事故复现：静态宪法曾排在最前，超限后预览里只有它。

        要保护的不变量是"**首段必须是世界状态**"，不是"首段必须是 TERRAIN"——
        §8.0i 之后首段是 [GOAL]（方向先于形状）。故这里断言首段属于动态段集合，
        并继续断言静态宪法不在投影里（它已移到 CLAUDE.md，见 §8.0f）。
        """
        projection = project.compile()
        self.assertNotIn("[COGNITION]", projection)
        self.assertNotIn("[ANTI-COMPRESSION]", projection)
        first = projection.lstrip().splitlines()[0]
        self.assertTrue(
            first.startswith(("[GOAL]", "[TERRAIN]", "[FINDINGS]")),
            f"投影首段必须是世界状态（方向/形状/交付物），实际是: {first[:60]}",
        )

    def test_goal_is_the_very_first_section(self):
        """[GOAL] 必须排在 [TERRAIN] 之前：其余每段回答"怎么走"，只有它回答"去哪"。"""
        lines = _big_world()
        sections = project._compose_sections(world_parse(lines), graph_parse(lines))
        names = [name for name, _ in sections]
        self.assertIn("GOAL", names)
        self.assertLess(names.index("GOAL"), names.index("TERRAIN"),
                        "目标必须出现在形状之前")


class TestBudgetEnforcement(unittest.TestCase):
    def test_big_world_fits_budget(self):
        lines = _big_world()
        sections = project._compose_sections(world_parse(lines), graph_parse(lines))
        raw = sum(len(text) for _, text in sections)
        self.assertGreater(raw, project.PROJECTION_BUDGET_CHARS * 3, "测试素材必须真的超预算")
        out = project._enforce_budget(sections, project.PROJECTION_BUDGET_CHARS)
        total = sum(len(text) for _, text in out)
        self.assertLessEqual(total, project.PROJECTION_BUDGET_CHARS)

    def test_trimming_converges_well_below_budget(self):
        """收敛性前提：裁到底的"地板"（段头 + 裁剪标记）必须远低于预算。

        否则某些世界下裁到底仍超预算 —— 那条不变量就成了假的。
        """
        lines = _big_world()
        sections = project._compose_sections(world_parse(lines), graph_parse(lines))
        floor = len("".join(text for _, text in project._enforce_budget(sections, 0)))
        self.assertLess(
            floor,
            project.PROJECTION_BUDGET_CHARS // 4,
            f"裁剪地板 {floor} 距预算 {project.PROJECTION_BUDGET_CHARS} 太近，收敛无余量",
        )

    def test_under_budget_is_untouched(self):
        sections = [("A", "hello\n"), ("B", "world\n")]
        self.assertEqual(project._enforce_budget(sections, 1000), sections)

    def test_trim_is_explicit_never_silent(self):
        """每个被裁的段必须写明：丢了几行 + 全文在哪。"""
        lines = _big_world()
        sections = project._compose_sections(world_parse(lines), graph_parse(lines))
        out = project._enforce_budget(sections, project.PROJECTION_BUDGET_CHARS)
        original = {name: text for name, text in sections}
        trimmed = [(name, text) for name, text in out if text != original[name]]
        self.assertTrue(trimmed, "这个素材下必须有段被裁，否则本测试无效")
        for name, text in trimmed:
            marker = [ln for ln in text.splitlines() if ln.startswith(project._ELISION_PREFIX)]
            self.assertEqual(len(marker), 1, f"{name} 被裁但裁剪标记缺失或重复")
            self.assertIn("lines dropped by projection budget", marker[0])
            self.assertIn(
                project.SECTION_RECOVERY[name],
                marker[0],
                f"{name} 的裁剪标记必须指向可恢复全文的位置",
            )

    def test_every_trimmable_section_has_a_recovery_pointer(self):
        for name in project.SECTION_TRIM_ORDER:
            self.assertIn(name, project.SECTION_RECOVERY, f"{name} 缺恢复指针")

    def test_trim_order_protects_frontier(self):
        """重压下先裁定局/历史，前沿与排除必须排在裁剪顺序最后。"""
        order = list(project.SECTION_TRIM_ORDER)
        self.assertLess(order.index("SETTLED"), order.index("FRONTIER"))
        self.assertLess(order.index("DIFFS"), order.index("FRONTIER"))
        for protected in ("TERRAIN", "COVERAGE", "RESUME", "ARTIFACTS", "TOOLS"):
            self.assertNotIn(protected, order, f"{protected} 是判据段，不该可裁")

    def test_critical_sections_survive_heavy_load(self):
        """世界再大，开局也必须看得到形状、可认领项与排除集。"""
        lines = _big_world()
        sections = project._compose_sections(world_parse(lines), graph_parse(lines))
        out = project._enforce_budget(sections, project.PROJECTION_BUDGET_CHARS)
        text = "".join(body for _, body in out)
        for marker in ("[TERRAIN]", "[FRONTIER]", "[EXCLUDED]"):
            self.assertIn(marker, text, f"{marker} 在重压下被裁掉了")

    def test_trimming_a_section_keeps_its_header(self):
        """裁剪只能丢正文行，段头（含总数）必须留着，否则读者对不上账。"""
        lines = _big_world()
        sections = project._compose_sections(world_parse(lines), graph_parse(lines))
        out = project._enforce_budget(sections, project.PROJECTION_BUDGET_CHARS)
        for (name, text), (_, original) in zip(out, sections):
            self.assertEqual(text.splitlines()[0], original.splitlines()[0], name)


class TestTrimSection(unittest.TestCase):
    BODY = "".join(f"  row {i:03d} padding padding\n" for i in range(20))
    TEXT = "[X] (20)\n" + BODY + "\n"

    def test_shrinks_to_allowed(self):
        got = project._trim_section(self.TEXT, 150, "memory.md")
        self.assertIsNotNone(got)
        self.assertLessEqual(len(got), 150)
        self.assertLess(len(got), len(self.TEXT), "必须真的变短")
        self.assertTrue(got.startswith("[X] (20)\n"), "段头（含总数）必须保留")
        self.assertIn("lines dropped by projection budget", got)
        self.assertIn("memory.md", got)
        kept = len([ln for ln in got.splitlines() if ln.startswith("  row")])
        reported = int(re.search(r"\+(\d+) lines dropped", got).group(1))
        self.assertEqual(reported, 20 - kept, "标记里的行数必须等于真正被丢的正文行数")

    def test_dropped_count_matches_line_loss(self):
        got = project._trim_section(self.TEXT, 150, "memory.md")
        marker = [ln for ln in got.splitlines() if ln.startswith(project._ELISION_PREFIX)][0]
        self.assertEqual(marker.count("lines dropped"), 1)
        self.assertRegex(marker, r"\+\d+ lines dropped")

    def test_unreachable_allowed_falls_back_to_minimal(self):
        """allowed 小到连最小形态都放不进 → 返回最小形态（仍短于原文），由下一段继续。"""
        got = project._trim_section(self.TEXT, 10, "memory.md")
        self.assertIsNotNone(got)
        self.assertLess(len(got), len(self.TEXT))
        self.assertEqual(len([ln for ln in got.splitlines() if ln.startswith("  row")]), 0)

    def test_header_only_section_is_not_trimmable(self):
        self.assertIsNone(project._trim_section("[X] (1)\n  a\n\n", 0, "memory.md"))

    def test_single_line_section_is_not_trimmable(self):
        self.assertIsNone(project._trim_section("[X] (1)\n  a\n", 1, "memory.md"))

    def test_already_trimmed_is_not_retrimmed(self):
        text = "[X] (3)\n  a\n  ... +2 lines dropped by projection budget; full: m\n\n"
        self.assertIsNone(project._trim_section(text, 5, "memory.md"))

    def test_never_returns_something_longer(self):
        """标记本身有成本：裁出来反而更长时必须放弃（否则会有无进展循环）。"""
        text = "[X] (2)\n  a\n  b\n\n"
        self.assertIsNone(project._trim_section(text, 1, "x" * 500))


class TestRealWorldBudget(unittest.TestCase):
    def test_compile_always_within_budget(self):
        self.assertLessEqual(len(project.compile()), project.PROJECTION_BUDGET_CHARS)


class TestStaticConstitutionTransport(unittest.TestCase):
    """静态宪法改由 CLAUDE.md 承载后，必须真的还在。

    宿主对 hook 的 additionalContext 有硬上限（超出即退化为预览）；
    对 `CLAUDE.md` 这类 memory 文件**没有硬上限** —— `MAX_MEMORY_CHARACTER_COUNT = 40000`
    的注释是 "Recommended"，只喂给 `/doctor` 的告警与状态栏提示，**不截断**。
    所以不变量由 hook 换成 CLAUDE.md 承载，而不是被删掉。
    """

    CLAUDE_MD = os.path.join(PROJECT_ROOT, "CLAUDE.md")
    COGNITION_MODULES = (
        "object_model.md",
        "evidence_model.md",
        "experiment_model.md",
        "surface_misalignment.md",
    )
    ANTI_COMPRESSION = (
        "Supported explanations should constrain the next experiment.",
        "Similar identifier ≠ same object.",
        "Related behavior ≠ proven shared boundary.",
        "Successful path ≠ adjacent surface exhausted.",
        "Missing observation ≠ absence.",
        "Blocked surface ≠ excluded surface.",
        "Preserved unknowns do not retain equal priority outside the current explanation boundary.",
    )

    def setUp(self):
        with open(self.CLAUDE_MD, encoding="utf-8") as handle:
            self.text = handle.read()

    def test_cognition_modules_are_imported_by_claude_md(self):
        for name in self.COGNITION_MODULES:
            self.assertIn(f"@cognition/{name}", self.text, f"{name} 不再被 CLAUDE.md 导入")

    def test_imported_cognition_files_exist_and_are_non_empty(self):
        for name in self.COGNITION_MODULES:
            path = os.path.join(PROJECT_ROOT, "cognition", name)
            self.assertTrue(os.path.isfile(path), f"缺失 cognition/{name}")
            self.assertGreater(os.path.getsize(path), 0, f"cognition/{name} 是空文件")

    def test_anti_compression_lines_survived_the_move(self):
        for line in self.ANTI_COMPRESSION:
            self.assertIn(line, self.text, "反压缩纪律在搬运中丢失")

    def test_claude_md_stays_below_host_warning_threshold(self):
        """40,000 是宿主的**建议**阈值（claude-code `MAX_MEMORY_CHARACTER_COUNT`），
        超了不截断、只在 `/doctor` 与状态栏告警。这里当**自查线**用：
        真超了说明热路径该减负了（判据与依据见 tests/test_instruction_shape.py）。
        """
        self.assertLess(len(self.text), 40000, "CLAUDE.md 超宿主建议阈值")

    def test_non_negotiable_boundary_survived(self):
        """不可协商边界句是热路径约束（授权不覆盖的六类动作），精简时不得静默丢掉。"""
        self.assertIn("不可协商边界", self.text)
        self.assertIn("生命维持", self.text)


if __name__ == "__main__":
    unittest.main()
