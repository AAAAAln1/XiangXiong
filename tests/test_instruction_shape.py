"""静态指令的形状不变量（2026-09-18 CLAUDE.md 认知负荷审计）。

## 为什么审的是"形状"而不是"字数"

宿主模型是 DeepSeek V4 Flash（13B 激活），它每轮真正读两块东西：
**动态投影**（`runtime/project.py`，约 4,800 字符）+ **静态指令**
（`CLAUDE.md` + `@cognition/*.md`，经宿主随 system prompt 注入）。

投影上一轮已审完（`tests/test_projection_structure.py`）。这一轮审静态指令，
量了三个维度，结果如下 —— **其中一条假设被量测直接证伪，记在这里以免下次重开**：

| 维度 | 量测 | 结论 |
|---|---|---|
| 规模 | CLAUDE.md 7,061 → **6,731** 字符；cognition 3,002 | 宿主对 memory 文件**无硬上限**，**规模本身不是约束** |
| 重复 | 近重复对 28 组 / 1,585 字符（15.7%） | 真问题，但多为"同一纪律的两种 register"，**只能删逐字重复** |
| 读取模式 | 契约型 71% / 查阅型 1% / 叙述型 26% | **「命令语法混在契约里」被证伪**：可照抄的调用片段总共只有 616 字符（8.7%） |

被证伪的那条同样重要：`docs/改造方案_v1.md` 已记过一次"按句密度删 CLAUDE.md"
是反向指标，本轮又量出"把命令语法外移"最多省 8.7% 且要冒"照抄会失败"的风险
（Flash 会照着抄命令，藏起来它不会去找）。两条路都不走。

## 本轮真正落地的改动（唯一一处结构性错位）

**报告的格式契约原先每轮都在场，却只在写报告那一刻有用。**

`CLAUDE.md` 里那条 643 字符的 report 格式 spec（一洞一节 / 每节必填字段 /
每步附可回放的原始请求包 / 证据绑定 / lessons ≤250 字）实测占静态负载的 6.4%、
占 CLAUDE.md 的 9.1%，但它只在**收口**被读到 —— 这是纯粹的注意力税。

改法与既有的 `bounty_criteria.md` **完全同构**（该文件已被这套机制证明可用：
CLAUDE.md 强制 Read → 跨会话 13 次实际读取）：

1. 正文外移到 `knowledge/report_spec.md`（含收口自检清单）
2. `CLAUDE.md` 留一行指针，且指针**与 `bounty_criteria.md` 并列** —— 收口时一次 Read 两件事
3. 投影 `[ARTIFACTS]` 段头也带同一指针：它正好显示 `report.md: absent`，
   是"我该写报告了"这个念头的**触发点**，把 Read 降到零回忆成本

## 另外两处修复

- **`CLAUDE.md` 内自相矛盾**：「记账」节说"通过 stdin 走 `merge.py`"，
  而「Runtime」节说走 `--delta-file`。同一文件两处推荐不同命令 → 读者要犹豫。
  统一为 `--delta-file`（零 shell 转义，即被证实可用的那条）。
- **hook 的 deny 文案指向已被证伪的命令**（P0）：`scripts/memory_guard.py` 在
  agent 试图直接写 memory 时拦下它，却给出一条**静默记 0 条**的命令
  （`echo "DELTA:\\n..." | merge.py`）。这是最坏的组合：在最需要兜底的时刻
  递上一个"看起来成功"的动作。文案已改为 `--delta-file`，并由本文件锁死。

## 锁死的不变量

1. `CLAUDE.md` 内**不得**有逐字重复句（归一化相似度 ≥ 0.95）
2. `CLAUDE.md` ↔ cognition **不得**有逐字重复句（同一阈值）
3. 每轮静态负载有预算：`CLAUDE.md` ≤ `INSTRUCTION_BUDGET_CHARS`
4. 关键契约句**必须**在场（防"清理动作不分保护与杂乱"重演 —— 见
   `docs/改造方案_v1.md` 的 1.4 实例 3）
5. 报告格式正文**不在** `CLAUDE.md`，在 `knowledge/report_spec.md`，且被两处指向
6. `CLAUDE.md` 与 `memory_guard` 文案**都不得**再教 `echo "DELTA:...`
7. `memory_guard` 必须放行 `--delta-file` 落盘路径（推荐工作流不能被自己拦死）
"""

import difflib
import json
import os
import re
import subprocess
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

CLAUDE_MD = os.path.join(PROJECT_ROOT, "CLAUDE.md")
COGNITION_DIR = os.path.join(PROJECT_ROOT, "cognition")
REPORT_SPEC = os.path.join(PROJECT_ROOT, "knowledge", "report_spec.md")

# 每轮静态负载预算（**注意力**预算，不是宿主限制）。
# 宿主对 memory 文件没有硬上限（`MAX_MEMORY_CHARACTER_COUNT = 40000` 只是建议阈值，
# 超了仅告警、不截断）；这条线是我们自己设的，判据是"读者每轮要消化多少"。
# 想加内容就得先把等量的东西挪出热路径，让取舍显式化。与 PROJECTION_BUDGET_CHARS 同一思路。
INSTRUCTION_BUDGET_CHARS = 6900

# 逐字重复的判定阈值。0.95 而非 0.9：0.9 档位里混着"同一纪律的两种 register"
# （如 CLAUDE.md「可切换主线，不可因新故事遗忘未闭合 hypothesis」与
# object_model「可换主线，不可因新故事遗忘未闭合 hypothesis」）——
# 那是刻意的前后呼应，删任一侧都会让另一侧失去语境。
VERBATIM_DUP_RATIO = 0.95

# 必须每轮在场的契约句。它们要么是安全边界，要么是"不写清就会照抄失败"的操作契约。
REQUIRED_CONTRACT_STRINGS = (
    # 安全与范围
    "不可协商边界",
    "授权目标 only",
    "禁止裸 curl",
    # 记账通道（唯一让探索可累积的通道）
    "一条 DELTA",
    "--delta-file",
    "禁止裸 Write/Edit memory",
    "REJECT",
    "STALE CANDIDATE",
    # 搜索的终止条件与假设的表达纪律
    "goal:",
    "必须写成因果链",
    # 派发（并行探索的唯一入口）
    'Agent(subagent_type="deepdive"',
    # 反压缩
    "Missing observation ≠ absence",
)

REQUIRED_COGNITION_IMPORTS = (
    "@cognition/object_model.md",
    "@cognition/evidence_model.md",
    "@cognition/experiment_model.md",
    "@cognition/surface_misalignment.md",
)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


# ── 与 scratch/_dup_audit.py 同一算法（审计器与被锁死的测试必须同源）────────────
_SPLIT_RE = re.compile(r"(?<=[。！？])|(?<=[；])|\n+")
_STRIP_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
MIN_UNIT_LEN = 12


def assertion_units(text: str) -> list:
    """→ [(行号, 原文, 归一化)]：逐句切断言单元。"""
    out = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for piece in _SPLIT_RE.split(line):
            piece = piece.strip().lstrip("-*>0123456789. ").strip().strip("`*_ ")
            if not piece:
                continue
            norm = _STRIP_RE.sub("", piece)
            if len(norm) < MIN_UNIT_LEN:
                continue
            out.append((lineno, piece, norm))
    return out


def verbatim_pairs(units_a: list, units_b: list, *, same_file: bool) -> list:
    """返回相似度 ≥ VERBATIM_DUP_RATIO 的单元对。"""
    pairs = []
    for i, (la, pa, na) in enumerate(units_a):
        others = units_a[i + 1:] if same_file else units_b
        for lb, pb, nb in others:
            if same_file and abs(la - lb) <= 1:
                continue
            shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
            if len(shorter) / len(longer) < VERBATIM_DUP_RATIO:
                continue
            ratio = difflib.SequenceMatcher(None, na, nb).ratio()
            if ratio >= VERBATIM_DUP_RATIO:
                pairs.append((ratio, la, pa, lb, pb))
    return pairs


class TestNoVerbatimDuplication(unittest.TestCase):
    """同一条规则说两遍，读者就得判断"这两处是不是一回事、是否一致" —— 纯税。"""

    def test_no_verbatim_duplicate_inside_claude_md(self):
        units = assertion_units(_read(CLAUDE_MD))
        pairs = verbatim_pairs(units, units, same_file=True)
        if pairs:
            detail = "\n".join(
                f"  {r:.2f}  L{a}: {pa[:70]}\n        L{b}: {pb[:70]}"
                for r, a, pa, b, pb in pairs
            )
            self.fail(f"CLAUDE.md 内有逐字重复句：\n{detail}")

    def test_no_verbatim_duplicate_between_claude_md_and_cognition(self):
        """两份文本**同时**注入（`@cognition/*.md` 被 CLAUDE.md 导入），重复即双倍代价。"""
        claude_units = assertion_units(_read(CLAUDE_MD))
        pairs = []
        for name in sorted(os.listdir(COGNITION_DIR)):
            if not name.endswith(".md"):
                continue
            cog_units = assertion_units(_read(os.path.join(COGNITION_DIR, name)))
            for ratio, la, pa, lb, pb in verbatim_pairs(
                claude_units, cog_units, same_file=False
            ):
                pairs.append((ratio, name, la, pa, lb, pb))
        if pairs:
            detail = "\n".join(
                f"  {r:.2f}  CLAUDE.md:{la}: {pa[:66]}\n        {n}:{lb}: {pb[:66]}"
                for r, n, la, pa, lb, pb in pairs
            )
            self.fail(f"CLAUDE.md 与 cognition 有逐字重复句：\n{detail}")


class TestStaticLoadBudget(unittest.TestCase):
    def test_claude_md_within_budget(self):
        text = _read(CLAUDE_MD)
        self.assertLessEqual(
            len(text), INSTRUCTION_BUDGET_CHARS,
            f"CLAUDE.md 是 {len(text)} 字符，超预算 {INSTRUCTION_BUDGET_CHARS}。"
            "它每轮都在场：要加内容就把等量的东西挪进 knowledge/（收口才读的那类），"
            "而不是把预算调大。",
        )


class TestRequiredContractsInPlace(unittest.TestCase):
    """防"清理动作不分保护与杂乱"重演（docs/改造方案_v1.md 1.4 实例 3）。"""

    def setUp(self):
        self.text = _read(CLAUDE_MD)

    def test_every_required_contract_string_present(self):
        missing = [s for s in REQUIRED_CONTRACT_STRINGS if s not in self.text]
        self.assertEqual(missing, [], f"这些契约句不在场了：{missing}")

    def test_cognition_imports_present(self):
        for name in REQUIRED_COGNITION_IMPORTS:
            self.assertIn(name, self.text, f"{name} 未导入 → 该认知模型不再注入")

    def test_hypothesis_causal_chain_rule_present(self):
        """因果链是 hypothesis 能被机械执行的前提，且是最容易被"精简"掉的一句。"""
        self.assertIn("→", self.text)
        self.assertIn("因果链", self.text)


class TestReportSpecIsExternalized(unittest.TestCase):
    """格式契约只有在写报告那一刻需要 —— 所以它不该在热路径上，但必须在手边。"""

    def setUp(self):
        self.claude = _read(CLAUDE_MD)
        self.spec = _read(REPORT_SPEC)

    def test_pointer_from_claude_md(self):
        self.assertIn("knowledge/report_spec.md", self.claude)

    def test_pointer_sits_next_to_bounty_criteria(self):
        """收口是一次 Read 两个问题（格式 + 过滤），不该要求读两遍指令。"""
        self.assertIn("knowledge/bounty_criteria.md", self.claude)

    def test_pointer_from_projection_artifacts_section(self):
        """投影侧指针：`[ARTIFACTS]` 段正好显示 report.md 的状态，是写报告的触发点。"""
        from runtime import project

        body = dict(project._compose_sections(_parse_target_lines()))["ARTIFACTS"]
        self.assertIn("report_spec.md", body)
        self.assertIn("report_spec.md", body.splitlines()[0],
                      "[ARTIFACTS] 段头要自解释（与本轮其余段同一原则）")

    def test_format_body_is_no_longer_in_claude_md(self):
        """正文搬走了才算真的搬走 —— 留一份在 CLAUDE.md 里等于没搬。"""
        for moved in ("每节必填", "Primary Cause", "Burp Repeater"):
            self.assertNotIn(moved, self.claude,
                             f"「{moved}」应只在 knowledge/report_spec.md")

    def test_spec_keeps_the_hard_won_constraints(self):
        """这些是被真实返工换来的：报告必须能逐洞复现、逐洞评级。"""
        for required in ("一洞一节", "完整绝对 URL", "原始请求包", "Burp Repeater",
                         "禁止", "Primary Cause", "req_", "exp-"):
            self.assertIn(required, self.spec, f"report_spec.md 缺「{required}」")

    def test_spec_says_when_to_read_it(self):
        self.assertIn("Read", self.spec)
        self.assertIn("收口", self.spec)

    def test_artifacts_section_is_never_trimmed(self):
        """指针所在段必须永不被投影预算裁掉，否则指针会消失。"""
        from runtime import project

        self.assertNotIn("ARTIFACTS", project.SECTION_TRIM_ORDER)


def _parse_target_lines():
    from runtime.world import parse as world_parse

    return world_parse(["target: spec.example.test\n", "surface: /login\n"])


class TestEchoTrapIsNotTaughtAnywhere(unittest.TestCase):
    """`echo "DELTA:\\n..."` 不许作为**推荐**写法出现在 agent 读的任何地方。

    范围限定在 agent 侧（`CLAUDE.md` / `knowledge/report_spec.md`）：
    README 是给人看的，里面保留"坏命令长什么样"是正确的（见下一条）。
    """

    def test_claude_md_does_not_teach_the_trap(self):
        self.assertNotIn('echo "DELTA', _read(CLAUDE_MD))

    def test_report_spec_does_not_teach_the_trap(self):
        self.assertNotIn('echo "DELTA', _read(REPORT_SPEC))

    def test_claude_md_records_recommended_merge_command_consistently(self):
        """同一文件里两处推荐不同命令 → 读者犹豫。记账通道只认 `--delta-file`。"""
        text = _read(CLAUDE_MD)
        self.assertIn("--delta-file", text)
        self.assertNotIn("通过 stdin 走", text)

    def test_readme_command_table_recommends_the_working_path(self):
        """README 的命令表曾长期推荐 `echo ... | merge.py`（那条静默记 0 条）。

        它是人读的，所以允许保留坏命令的形态作对照 —— 但**推荐位**必须是能用的那条。
        """
        readme = _read(os.path.join(PROJECT_ROOT, "README.md"))
        self.assertIn("--delta-file", readme)
        memory_rows = [ln for ln in readme.splitlines()
                       if ln.startswith("| Memory |")]
        self.assertTrue(memory_rows, "README 缺少 Memory 命令行")
        self.assertIn("--delta-file", memory_rows[0])
        self.assertNotIn("echo", memory_rows[0])


class TestMemoryGuardHandsOverAWorkingCommand(unittest.TestCase):
    """hook 的 deny 文案是 agent 卡住时唯一的指路 —— 它必须指向被证实可用的那条路。

    历史 bug：文案教 `echo "DELTA:\\n<lines>" | python runtime/merge.py`，
    而那条命令在 Git Bash / PowerShell 下静默记 0 条。
    """

    def _run_guard(self, payload: dict):
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "memory_guard.py")],
            cwd=PROJECT_ROOT,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    @staticmethod
    def _decision(stdout: str):
        if not stdout.strip():
            return None, ""
        return (
            json.loads(stdout)["hookSpecificOutput"]["permissionDecision"],
            json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_direct_write_to_memory_is_denied(self):
        decision, reason = self._decision(self._run_guard({
            "tool_name": "Write",
            "tool_input": {"file_path": os.path.join(PROJECT_ROOT, ".claude", "memory.md")},
        }))
        self.assertEqual(decision, "deny")

    def test_deny_reason_uses_delta_file_not_echo(self):
        _, reason = self._decision(self._run_guard({
            "tool_name": "Write",
            "tool_input": {"file_path": os.path.join(PROJECT_ROOT, ".claude", "memory.md")},
        }))
        self.assertIn("--delta-file", reason)
        self.assertIn("merge.py", reason)
        self.assertNotIn("echo", reason.lower(),
                         "deny 文案不得再教那条静默失败的 echo 写法")

    def test_deny_reason_covers_experiment_close(self):
        _, reason = self._decision(self._run_guard({
            "tool_name": "Write",
            "tool_input": {"file_path": os.path.join(PROJECT_ROOT, ".claude", "memory.md")},
        }))
        self.assertIn("--memory-update-file", reason)

    def test_recommended_workflow_is_not_blocked(self):
        """推荐工作流要写 DELTA 落盘 + 跑 merge --delta-file；它不能被自己拦死。"""
        write_delta = self._run_guard({
            "tool_name": "Write",
            "tool_input": {"file_path": os.path.join(
                PROJECT_ROOT, "results", "t.example.test", "scripts", "_delta.txt")},
        })
        self.assertEqual(write_delta.strip(), "", "写 _delta.txt 不该被拦")

        run_merge = self._run_guard({
            "tool_name": "Bash",
            "tool_input": {"command": "python runtime/merge.py --delta-file "
                                      "results/t.example.test/scripts/_delta.txt"},
        })
        self.assertEqual(run_merge.strip(), "", "merge --delta-file 不该被拦")

    def test_archived_memory_is_denied(self):
        decision, _ = self._decision(self._run_guard({
            "tool_name": "Write",
            "tool_input": {"file_path": os.path.join(
                PROJECT_ROOT, "results", "t.example.test", "memory.md")},
        }))
        self.assertEqual(decision, "deny")

    def test_benign_write_is_allowed(self):
        self.assertEqual(self._run_guard({
            "tool_name": "Write",
            "tool_input": {"file_path": os.path.join(
                PROJECT_ROOT, "results", "t.example.test", "report.md")},
        }).strip(), "")


if __name__ == "__main__":
    unittest.main()
