"""LLM 人体工程学回归锁（2026-09-25）。

## 这批改动的来源

对 9/17–9/24 的 **20 个真实会话**做了行为量化，四条最贵的摩擦（都有实测数字）：

1. **契约要读源码**：`http_request.py --help` 只有 105 字符，逼得 3/9 个会话各读一次
   全文（单条 21,436 字符进上下文）。
2. **收口要复述长文本**：`[REMOVE]` 要求逐字符前缀，而 LLM 会在两次 DELTA 之间改写
   （`判断`→`判定`）；失败提示又把原因举例成"状态标记"，把它引向错误修法。
   实测残留 3 条"幽灵前沿"（同一假设同时在 [FRONTIER] 与 [SETTLED]）。
3. **实验槽共享**：子 transcript 里 `已有 active experiment` 15–39 次/会话；
   还出现 3 次 `deactivate` 把别人正在用的实验静默踢下线（请求归因悬空 2.5 分钟）。
4. **证据通道被 curl 习惯打败**：`-o nul` / `-w "S:%{http_code}"` 覆盖了工具自己的
   `-o/-w`，实测 14%–57% 的请求保存体为空、status 字段被格式串污染。

## 锁死的不变量

1. `[REMOVE] h:xxxxxx`（投影里印的 ID）必须能直接删行；不再要求复述原文
2. 删失败必须给：最近行 + 首个不同字符位置 + **可直接复制的 `[REMOVE] h:id`**
3. `create --activate` 撞槽**不再抛错**：返回 `active=false` + 一行 `--experiment-id` 补救动作
4. `--worker` 槽隔离：不同 worker 不互相冲突；`deactivate --worker` 不动别人的槽
5. `-o/-w/-D/-I` 等选项被忽略（`-I` 转 `-X HEAD`）并显式提示，证据必须仍然落盘
6. `--help` 必须是自足契约（含被忽略选项清单与去证据的坑）
7. 投影：`[GOAL]` 首行声明归属（换目标先写 DELTA）；`[FINDINGS]` 逐条 brief；
   可执行段（[UNEXPLAINED]/[DIFFS]）不得比背景段先被牺牲
8. `[TERRAIN]` 给 `vuln_without_exp` 机械计数（vuln 无实验背书）
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import digest, experiment, http_request, merge, project  # noqa: E402
from runtime.graph import parse as graph_parse  # noqa: E402
from runtime.world import parse as world_parse  # noqa: E402

PY = sys.executable
TARGET = "ergollm.example.test"

WORLD = (
    f"target: {TARGET}\n"
    "goal: 判定沙箱文件是否匿名可达\n"
    "surface: /api/core/ai/sandbox/file 存在 (req_001)\n"
    "hypothesis [observed]: /api/core/ai/sandbox/file 以 action 为判别式, 判定沙箱读写是否匿名可达 (req_866)\n"
    "hypothesis [observed]: 另一条无关假设: 登录接口可枚举 (req_002)\n"
    "counter: CORS 非反射 (req_003)\n"
    "vuln: /api/admin 未授权读用户列表 (req_004)\n"
    "future: 恢复测试时先跑 397 路径扫描\n"
)


def _run_cli(args, env_extra=None):
    env = dict(os.environ, PYTHONUTF8="1")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PY, *args], cwd=PROJECT_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )


class ErgoBase(unittest.TestCase):
    """把 RESULTS_DIR / world 都指到临时目录：不碰真实 results/ 与 .claude/memory.md。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xx_ergollm_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._orig_merge_results = merge.RESULTS_DIR
        self._orig_exp_results = experiment.RESULTS_DIR
        self._orig_world_active = merge.world_active
        merge.RESULTS_DIR = self.tmp
        experiment.RESULTS_DIR = self.tmp
        os.makedirs(os.path.join(self.tmp, TARGET, "responses"), exist_ok=True)
        # 证据闸门要解析 exp-NNN：预置一个实验目录，让 [contradicted] 行能带 exp 引用。
        os.makedirs(os.path.join(self.tmp, TARGET, "experiments", "exp-001"), exist_ok=True)
        for ref in ("req_001", "req_002", "req_003", "req_004", "req_866", "req_1016"):
            with open(os.path.join(self.tmp, TARGET, "responses", ref + ".txt"),
                      "w", encoding="utf-8") as handle:
                handle.write("HTTP/1.1 200 OK\n\n{}\n")
        self.world = os.path.join(self.tmp, "memory.md")
        with open(self.world, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(WORLD)
        self._orig_project_world = project.world_active
        merge.world_active = lambda: self.world
        project.world_active = lambda: self.world
        self.addCleanup(self._restore)

    def _restore(self):
        merge.RESULTS_DIR = self._orig_merge_results
        experiment.RESULTS_DIR = self._orig_exp_results
        merge.world_active = self._orig_world_active
        project.world_active = self._orig_project_world

    def _lines(self):
        with open(self.world, encoding="utf-8") as handle:
            return handle.read().splitlines(keepends=True)

    def _hypothesis_id(self, needle):
        """只挑 hypothesis 行（surface/counter 行也含同一路径，别拿错）。"""
        for node in graph_parse(self._lines()).nodes:
            if node.raw.startswith("hypothesis") and needle in node.raw:
                return node.id
        self.fail(f"fixture 里找不到含 {needle} 的 hypothesis 行")

    def _apply(self, delta_body):
        """把 DELTA 文本喂给 merge（捕获 stdout/stderr）。"""
        ops = merge.parse_delta("DELTA:\n" + delta_body)
        lines = self._lines()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            lines, stats = merge.apply_delta(lines, ops)
        with open(self.world, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(lines))
        return stats, out.getvalue(), err.getvalue()


class TestRemoveById(ErgoBase):
    """不变量 1/2：按 ID 删除 + 失败给可直接复制的补救行。"""

    def test_remove_by_projection_id(self):
        target_id = self._hypothesis_id("sandbox/file")
        stats, out, err = self._apply(f"[REMOVE] {target_id}\n")
        self.assertEqual(stats["DELETE"], 1, err)
        body = "\n".join(self._lines())
        self.assertNotIn("以 action 为判别式", body)
        self.assertIn("另一条无关假设", body, "不能连带删错行")

    def test_paraphrase_failure_gives_diff_position_and_id(self):
        """实测失败形态：正文被改写（判定/判断）。必须给出差异位置 + 可复制的 ID。"""
        target_id = self._hypothesis_id("sandbox/file")
        _, _, err = self._apply(
            "[REMOVE] hypothesis: /api/core/ai/sandbox/file 以 action 为判别式, "
            "判**定**沙箱读写是否匿名可达 (req_866)\n"
        )
        self.assertIn("DELETE FAILED", err)
        self.assertIn("最接近的是第", err)
        self.assertIn("字符起不同", err)
        self.assertIn(f"[REMOVE] {target_id}", err, "必须给出可直接复制的 ID")
        self.assertIn("另一条无关假设", "\n".join(self._lines()), "失败时不得误删")

    def test_unknown_id_lists_same_kind_ids(self):
        _, _, err = self._apply("[REMOVE] h:000000\n")
        self.assertIn("不在账本里", err)
        self.assertIn("同类现有 ID", err)

    def test_high_confidence_belief_still_protected(self):
        _, _, err = self._apply("[REMOVE] hypothesis [contradicted]: whatever (exp-001)\n")
        self.assertIn("REJECTED DELETE", err)

    def test_stale_candidate_prints_copyable_id(self):
        """终态新行触发 STALE CANDIDATE 时，旧行的 ID 必须随提示给出。"""
        old_id = self._hypothesis_id("sandbox/file")
        os.makedirs(os.path.join(self.tmp, TARGET, "experiments", "exp-001"), exist_ok=True)
        _, _, err = self._apply(
            "hypothesis [contradicted]: 匿名可达 /api/core/ai/sandbox/file 不成立 (exp-001, req_1016)\n"
        )
        self.assertIn("STALE CANDIDATE", err)
        self.assertIn(f"[REMOVE] {old_id}", err)


class TestExperimentSlots(unittest.TestCase):
    """不变量 3/4：撞槽不报错 + worker 槽隔离。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xx_ergoslot_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._orig = experiment.RESULTS_DIR
        experiment.RESULTS_DIR = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        experiment.RESULTS_DIR = self._orig

    def test_activate_conflict_returns_hint_not_error(self):
        first = experiment.create_experiment(TARGET, hypothesis="H1", activate=True)
        self.assertTrue(first.get("active"))
        second = experiment.create_experiment(TARGET, hypothesis="H2", activate=True)
        self.assertFalse(second.get("active"), "撞槽时不得抢，也不得抛错")
        self.assertEqual(second.get("active_slot"), first["experiment_id"])
        self.assertIn("--experiment-id", second.get("hint", ""))

    def test_worker_slots_do_not_conflict(self):
        a = experiment.create_experiment(TARGET, hypothesis="HA", activate=True, worker="h:aaaaaa")
        b = experiment.create_experiment(TARGET, hypothesis="HB", activate=True, worker="h:bbbbbb")
        self.assertTrue(a.get("active"))
        self.assertTrue(b.get("active"), "不同 worker 各有自己的槽")
        self.assertEqual(experiment.resolve_experiment_id(TARGET, worker="h:aaaaaa"),
                         a["experiment_id"])
        self.assertEqual(experiment.resolve_experiment_id(TARGET, worker="h:bbbbbb"),
                         b["experiment_id"])

    def test_deactivate_does_not_touch_others_slot(self):
        foreign = experiment.create_experiment(TARGET, hypothesis="HF", activate=True, worker="h:aaaaaa")
        result = experiment.deactivate_experiment(TARGET, worker="h:bbbbbb")
        self.assertFalse(result.get("removed"), "不得动别人的槽")
        self.assertEqual(experiment.resolve_experiment_id(TARGET, worker="h:aaaaaa"),
                         foreign["experiment_id"], "别人的槽必须还在")
        self.assertEqual(experiment.resolve_experiment_id(TARGET, worker="h:bbbbbb"), None)

    def test_deactivate_notes_foreign_global_slot(self):
        """自己的槽空着、而别处的槽有人 —— 必须说明（归因混乱的常见来源）。"""
        experiment.create_experiment(TARGET, hypothesis="HG", activate=True)
        result = experiment.deactivate_experiment(TARGET, worker="h:bbbbbb")
        self.assertFalse(result.get("removed"))
        self.assertIn("全局槽", result.get("note", ""))

    def test_worker_is_recorded_in_metadata(self):
        made = experiment.create_experiment(TARGET, hypothesis="HW", activate=True, worker="h:cccccc")
        meta = experiment.load_experiment(TARGET, made["experiment_id"])
        self.assertEqual(meta.get("worker"), "h:cccccc")

    def test_explicit_id_still_wins_over_worker_slot(self):
        made = experiment.create_experiment(TARGET, hypothesis="HX", activate=True, worker="h:dddddd")
        self.assertEqual(
            experiment.resolve_experiment_id(TARGET, "exp-042", worker="h:dddddd"), "exp-042")
        self.assertEqual(
            experiment.resolve_experiment_id(TARGET, worker="h:dddddd"), made["experiment_id"])


class TestCaptureBreakingArgs(unittest.TestCase):
    """不变量 5：curl 习惯不再毁掉证据。"""

    def test_output_flag_is_dropped_with_note(self):
        clean, notes = http_request.strip_capture_breaking_args(
            ["-o", "nul", "--max-time", "8", "http://x/"])
        self.assertEqual(clean, ["--max-time", "8", "http://x/"])
        self.assertTrue(any("忽略 -o" in n for n in notes))

    def test_write_out_is_dropped(self):
        clean, notes = http_request.strip_capture_breaking_args(
            ["-w", "S:%{http_code} L:%{size_download}", "-s", "http://x/"])
        self.assertNotIn("-w", clean)
        self.assertNotIn("%{http_code}", clean)
        self.assertTrue(notes)

    def test_head_is_converted_not_dropped(self):
        clean, notes = http_request.strip_capture_breaking_args(["-I", "http://x/"])
        self.assertEqual(clean, ["-X", "HEAD", "http://x/"])
        self.assertTrue(any("-X HEAD" in n for n in notes))

    def test_plain_request_untouched(self):
        args = ["-X", "POST", "-H", "Content-Type: application/json", "-d", "{}", "http://x/"]
        clean, notes = http_request.strip_capture_breaking_args(args)
        self.assertEqual(clean, args)
        self.assertEqual(notes, [])

    def test_dump_header_and_config_dropped(self):
        clean, _ = http_request.strip_capture_breaking_args(
            ["-D", "h.txt", "-K", "cfg", "--trace", "t.txt", "http://x/"])
        self.assertEqual(clean, ["http://x/"])

    def test_long_form_output_equals_form(self):
        clean, notes = http_request.strip_capture_breaking_args(["--output=/dev/null", "http://x/"])
        self.assertEqual(clean, ["http://x/"])
        self.assertTrue(notes)


class TestHttpRequestHelp(unittest.TestCase):
    """不变量 6：`--help` 自足（含被忽略选项清单 + 证据落盘说明）。"""

    def test_help_lists_traps(self):
        proc = _run_cli(["runtime/http_request.py", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        for needle in ("--experiment-id", "--session", "--worker", "-o/--output",
                       "会被**忽略并提示**", "req_NNN", "[EXECUTION]"):
            self.assertIn(needle, out, f"help 缺 {needle}")
        self.assertGreater(len(out), 900, "help 不能还是一行 stub")

    def test_bare_invocation_prints_help(self):
        proc = _run_cli(["runtime/http_request.py"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("用法", proc.stdout)

    def test_experiment_help_explains_slots(self):
        proc = _run_cli(["runtime/experiment.py", "--help"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("槽位", proc.stdout)
        self.assertIn("--worker", proc.stdout)


class TestProjectionErgonomics(ErgoBase):
    """不变量 7/8：归属声明、FINDINGS 压缩、可执行段优先、vuln 背书计数。"""

    def test_goal_header_declares_ownership(self):
        view = world_parse(self._lines())
        first = digest.format_goal_section(view).splitlines()[0]
        self.assertIn(f"target={TARGET}", first)
        self.assertIn("本投影只属于", first)
        self.assertIn("DELTA: target:", first)

    def test_findings_are_briefed(self):
        view = world_parse(self._lines())
        out = project.format_findings_section(view)
        self.assertIn("[FINDINGS] (1)", out)
        self.assertIn("未授权读用户列表", out)

    def test_actionable_sections_survive_longer_than_history(self):
        order = list(project.SECTION_TRIM_ORDER)
        self.assertLess(order.index("EXCLUDED"), order.index("DIFFS"),
                        "最长的历史段必须先被牺牲")
        self.assertLess(order.index("WORLD MODEL"), order.index("DIFFS"),
                        "可执行段不该比背景段先死")
        self.assertLess(order.index("SETTLED"), order.index("UNEXPLAINED"))
        self.assertEqual(order[-1], "FINDINGS", "交付物仍最后被牺牲")

    def test_terrain_reports_vuln_without_experiment(self):
        out = project.format_terrain_section(graph_parse(self._lines()))
        self.assertIn("vuln_without_exp 1", out)

    def test_terrain_omits_count_when_backed(self):
        lines = WORLD.replace("vuln: /api/admin 未授权读用户列表 (req_004)",
                              "vuln: /api/admin 未授权读用户列表 (exp-009, req_004)")
        out = project.format_terrain_section(graph_parse(lines.splitlines(keepends=True)))
        self.assertNotIn("vuln_without_exp", out)

    def test_diffs_recovery_pointer_is_executable(self):
        self.assertIn("python runtime/diff_scan.py", project.SECTION_RECOVERY["DIFFS"])


class TestBomTolerance(ErgoBase):
    """BOM 不得让投影静默丢掉 target。

    实测（2026-09-25 冒烟）：用 PowerShell `Set-Content -Encoding UTF8` 写的 world
    首行带 BOM → 投影首行回落到 `target=(none)`，"我在测哪个站"整条信息消失；
    merge 侧同类坑已于 2026-09-18 修（tests/test_cli_ergonomics.py 的 BOM 用例）。
    """

    def _write_bom_world(self):
        with open(self.world, "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + WORLD.encode("utf-8"))

    def test_projection_keeps_target(self):
        self._write_bom_world()
        view = world_parse(project.strip_bom(project._read_memory_lines()))
        self.assertEqual(view.target, TARGET)
        first = digest.format_goal_section(view).splitlines()[0]
        self.assertIn(f"target={TARGET}", first)
        self.assertNotIn("target=(none)", first)

    def test_merge_reads_target_for_evidence_check(self):
        self._write_bom_world()
        lines = merge.read_memory()
        self.assertTrue(lines[0].startswith("target:"), repr(lines[0][:20]))


class TestMergeDigestWiring(ErgoBase):
    """回吐（merge 后的决策摘要）必须与投影同源，且 STALE 提示带可复制 ID。"""

    def test_digest_goal_matches_projection(self):
        view = world_parse(self._lines())
        self.assertEqual(digest.format_goal_section(view),
                         digest.format_decision_zone(view).split("[NEXT]")[0])


# ── 2026-09-25 代码审计修复（11 项）的回归锁 ──────────────────────────────
# 每条对应审计表里的一项；注释标号便于回查。


class TestAuditDescribeSlot(ErgoBase):
    """审计 #1：describe(worker) 只看自己的槽，不把全局槽说成自己的。"""

    def test_worker_query_never_claims_global_slot(self):
        made = experiment.create_experiment(TARGET, hypothesis="GLOBAL-H", activate=True)
        got = experiment.describe_active_experiment(TARGET, worker="h:aaaaaa")
        self.assertIsNone(got.get("active"), "别人的实验不得报成自己的槽")
        self.assertEqual(got.get("slot"), "h_aaaaaa")
        self.assertEqual(got.get("global_active"), made["experiment_id"],
                         "只读窥探全局槽，让读者知道别处有人")

    def test_worker_query_returns_own_slot(self):
        own = experiment.create_experiment(TARGET, hypothesis="OWN-H", activate=True,
                                           worker="h:aaaaaa")
        got = experiment.describe_active_experiment(TARGET, worker="h:aaaaaa")
        self.assertEqual(got.get("active"), own["experiment_id"])
        self.assertIsNone(got.get("global_active"))

    def test_global_query_unchanged(self):
        made = experiment.create_experiment(TARGET, hypothesis="G", activate=True)
        got = experiment.describe_active_experiment(TARGET)
        self.assertEqual(got.get("active"), made["experiment_id"])
        self.assertEqual(got.get("slot"), "global")
        self.assertNotIn("global_active", got)


class TestAuditCloseWorker(ErgoBase):
    """审计 #2：close 支持 --worker；stale 槽不再造成假冲突；不误关别人的实验。"""

    def _observe(self, exp_id):
        """Experiment First Contract：没有 observation 的实验关不掉。"""
        experiment.append_observation(TARGET, exp_id, "审计测试观测 obs-1")

    def test_close_without_id_uses_own_slot(self):
        own = experiment.create_experiment(TARGET, hypothesis="OWN", activate=True,
                                           worker="h:aaaaaa")
        others = experiment.create_experiment(TARGET, hypothesis="OTHERS", activate=True)
        self._observe(own["experiment_id"])
        closed = experiment.close_experiment(TARGET, conclusion="c", status="success",
                                             worker="h:aaaaaa")
        self.assertEqual(closed["experiment_id"], own["experiment_id"], "关的必须是自己的")
        meta = experiment.load_experiment(TARGET, others["experiment_id"])
        self.assertFalse(meta["closed"], "别人的实验必须原样保留")
        self.assertIsNone(experiment._slot_holder(TARGET, "h:aaaaaa"), "自己的槽被清")

    def test_close_without_id_and_empty_slot_is_explicit(self):
        others = experiment.create_experiment(TARGET, hypothesis="OTHERS", activate=True)
        with self.assertRaises(ValueError) as ctx:
            experiment.close_experiment(TARGET, conclusion="c", status="success",
                                        worker="h:aaaaaa")
        self.assertIn("槽 h_aaaaaa", str(ctx.exception))
        got = experiment.describe_active_experiment(TARGET)
        self.assertEqual(got.get("active"), others["experiment_id"], "不得误关全局槽实验")

    def test_stale_slot_no_false_conflict(self):
        """close 后残留的 closed 槽文件不得挡住下一次 activate（审计实测的假冲突）。"""
        own = experiment.create_experiment(TARGET, hypothesis="OWN", activate=True,
                                           worker="h:aaaaaa")
        self._observe(own["experiment_id"])
        experiment.close_experiment(TARGET, conclusion="c", status="success",
                                    worker="h:aaaaaa")
        # 人为再写回 stale 槽，模拟"deactivate 没清干净"的历史残留
        slot = experiment.slot_path(TARGET, "h:aaaaaa")
        with open(slot, "w", encoding="utf-8") as handle:
            handle.write(own["experiment_id"])
        again = experiment.create_experiment(TARGET, hypothesis="OWN2", activate=True,
                                             worker="h:aaaaaa")
        self.assertTrue(again.get("active"), "closed 的旧实验不得占用槽位")
        self.assertFalse(os.path.exists(slot) and open(slot).read().strip() == own["experiment_id"])


class TestAuditHelpAnywhere(unittest.TestCase):
    """审计 #3：help/版本类参数任意位置拦截，不许透传 curl 污染证据。"""

    def setUp(self):
        # http_request 的证据目录在**仓库** results/ 下；若拦截失效会在这里留脏文件，
        # 先清干净保证断言反映"本次调用没发请求"。
        self.repo_target = os.path.join(PROJECT_ROOT, "results", TARGET)
        shutil.rmtree(self.repo_target, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo_target, True)

    def test_target_then_help_prints_help_without_request(self):
        for flag in ("--help", "-h", "-V", "--manual"):
            with self.subTest(flag=flag):
                proc = _run_cli(["runtime/http_request.py", TARGET, flag])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("http_request.py", proc.stdout)
                self.assertNotIn("Usage: curl", proc.stdout, "不得透传给 curl")
        self.assertFalse(
            os.path.isdir(self.repo_target) and os.listdir(self.repo_target),
            "help 形态不得产生任何证据文件（没发请求）",
        )

    def test_help_text_promises_now_true(self):
        self.assertTrue(
            any(a in http_request.CURL_STDOUT_NOISE for a in ("--help", "-V"))
        )


class TestAuditAttachedOptions(unittest.TestCase):
    """审计 #4：粘连短选项 -w%{...} / -Df / -oFILE 被拦（否则 status 污染静默复发）。"""

    def test_attached_write_out_dropped(self):
        clean, notes = http_request.strip_capture_breaking_args(
            ["-w%{http_code}", "http://x/"])
        self.assertEqual(clean, ["http://x/"])
        self.assertTrue(any("粘连" in n for n in notes))

    def test_attached_dump_and_output_dropped(self):
        clean, _ = http_request.strip_capture_breaking_args(["-Dhdr.txt", "-oout.html", "http://x/"])
        self.assertEqual(clean, ["http://x/"])

    def test_plain_short_flags_untouched(self):
        args = ["-sS", "-X", "POST", "-d", "{}", "-k", "http://x/"]
        clean, notes = http_request.strip_capture_breaking_args(args)
        self.assertEqual(clean, args)
        self.assertEqual(notes, [])


class TestAuditUpdatePath(ErgoBase):
    """审计 #5/#6/#8：UPDATE 提示措辞、按 ID 更新、正文含 = 的切分。"""

    def test_update_failure_hint_uses_update_verb_and_id(self):
        # 用与 fixture 行高相似的改写（同 DELETE 路径的实测失败形态），
        # 确保走"最近行 + 按 ID 改"提示，而不是相似度兜底。
        _, _, err = self._apply(
            "[UPDATE] hypothesis [observed]: /api/core/ai/sandbox/file 以 action 为判别式, "
            "判**定**沙箱读写是否匿名可达 (req_866) = surface: 新行 (req_001)\n"
        )
        self.assertIn("UPDATE FAILED", err)
        self.assertIn("按 ID 改", err, "措辞必须是'改'")
        self.assertNotIn("按 ID 删（复制这一行）", err, "不得再出现 DELETE 措辞")
        self.assertIn("[UPDATE] h:", err, "补救行必须是可复制的 UPDATE 形态")

    def test_update_by_stable_id(self):
        target_id = self._hypothesis_id("sandbox/file")
        stats, _, err = self._apply(
            f"[UPDATE] {target_id} = hypothesis [observed]: 改写后的沙箱假设 (req_866)\n"
        )
        self.assertEqual(stats["UPDATE"], 1, err)
        body = "\n".join(self._lines())
        self.assertIn("改写后的沙箱假设", body)
        self.assertNotIn("以 action 为判别式", body)

    def test_update_claim_containing_equals(self):
        """正文含 `alg=none` 不得被当成分隔符切开（审计 #8，存量缺陷）。"""
        with open(self.world, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("hypothesis [observed]: JWT alg=none 签名可绕过 (req_001)\n")
        stats, _, err = self._apply(
            "[UPDATE] hypothesis [observed]: JWT alg=none 签名可绕过 = "
            "hypothesis [contradicted]: JWT alg=none 签名校验有效 (exp-001, req_001)\n"
        )
        self.assertEqual(stats["UPDATE"], 1, err)
        self.assertIn("签名校验有效", "\n".join(self._lines()))

    def test_update_with_separator_but_invalid_right_fails_loud(self):
        """有 = 但右侧拼不成合法行 → 显式 UPDATE FAILED 且 NOOP，不许退化 ADD。"""
        stats, _, err = self._apply("[UPDATE] hypothesis: 旧假设 = 没有前缀的新内容\n")
        self.assertEqual(stats["UPDATE"], 0)
        self.assertEqual(stats.get("ADD"), 0, "有分隔符的坏 UPDATE 不得静默追加")
        self.assertIn("UPDATE FAILED", err)


class TestAuditGuardSegments(unittest.TestCase):
    """审计 #9：memory_guard 白名单按命令链切段，链尾写保护文件必须 deny。"""

    @staticmethod
    def _module():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "memory_guard_under_test",
            os.path.join(PROJECT_ROOT, "scripts", "memory_guard.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _deny(self, command):
        return self._module()._is_memory_write("Bash", {"command": command})

    def test_chained_write_after_whitelisted_tool_denied(self):
        self.assertTrue(
            self._deny("python runtime/merge.py --delta-file d.txt && echo hacked > .claude/memory.md"),
            "链尾写 .claude/memory.md 必须拦",
        )

    def test_plain_merge_allowed(self):
        self.assertFalse(
            self._deny("python runtime/merge.py --delta-file results/t/scripts/_delta.txt")
        )

    def test_cd_then_merge_allowed(self):
        self.assertFalse(self._deny('cd "F:/x" && python runtime/merge.py --delta-file d.txt'))

    def test_chained_read_of_memory_denied(self):
        self.assertTrue(
            self._deny("python runtime/merge.py --delta-file d.txt && cat results/t/memory.md"),
            "与单命令口径一致：bash 不读保护文件（读用 Read 工具）",
        )


class TestAuditRecentLabel(ErgoBase):
    """审计 #10：RECENT 段头与状态如实（不再把未关闭实验标成"已完成"）。"""

    def setUp(self):
        super().setUp()
        self._orig_results_dir = project._results_dir
        project._results_dir = lambda: self.tmp
        self.addCleanup(lambda: setattr(project, "_results_dir", self._orig_results_dir))

    def _write_exp(self, exp_id, *, closed, status):
        path = os.path.join(self.tmp, TARGET, "experiments", exp_id)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "experiment.json"), "w", encoding="utf-8") as handle:
            json.dump({"experiment_id": exp_id, "target": TARGET,
                       "created_at": "2026-09-25T00:00:00+00:00",
                       "hypothesis": "H", "closed": closed, "status": status,
                       "observations": [], "conclusion": "c" if closed else None},
                      handle)

    def test_open_experiment_not_labelled_done(self):
        self._write_exp("exp-001", closed=False, status="unknown")
        out = project._format_recent_experiments(TARGET)
        self.assertNotIn("— 已完成", out)
        self.assertIn("最近实验", out)
        self.assertIn("[open]", out)

    def test_closed_experiment_keeps_status(self):
        self._write_exp("exp-002", closed=True, status="contradicted")
        out = project._format_recent_experiments(TARGET)
        self.assertIn("[contradicted]", out)


class TestAuditDeactivateDoc(ErgoBase):
    """审计 #11：deactivate 守卫语义与 docstring 一致（守卫只在带 worker 时生效）。"""

    def test_guard_only_applies_with_worker(self):
        import inspect
        doc = inspect.getdoc(experiment.deactivate_experiment)
        self.assertIn("仅在带 --worker 时可判定", doc)
        self.assertIn("全局槽管理行为", doc)


# ── 禁止问人（2026-09-25 用户确认方案 B+C）的回归锁 ───────────────────────
# 目标：agent 完全按已知信息测完直接交 report，不发问（AskUserQuestion 与文本请示）。
# 提示层改语义 + hook 机械兜底 + report 强制边界清单，三层各锁一条。


class TestNoAskContract(unittest.TestCase):
    """三层契约：CLAUDE.md 禁令、settings hook 挂载、deny 脚本可执行且文案=替代动作。"""

    def _read(self, rel):
        with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_claude_md_forbids_asking_with_alternatives(self):
        doc = self._read("CLAUDE.md")
        self.assertIn("**禁止问人**", doc)
        self.assertIn("hypothesis [blocked]", doc, "缺条件的替代动作必须在场")
        self.assertIn("默认不测+记边界", doc, "scope 争议的替代动作必须在场")
        self.assertNotIn("时问人", doc, "旧的'允许问人'清单不得残留")

    def test_settings_hook_registered_in_both_files(self):
        for rel in (".claude/settings.local.json", ".claude/settings.example.json"):
            raw = self._read(rel)
            settings = json.loads(raw)
            matchers = [
                entry.get("matcher", "")
                for entry in settings.get("hooks", {}).get("PreToolUse", [])
            ]
            self.assertTrue(any("AskUserQuestion" in m for m in matchers),
                            f"{rel} 缺 AskUserQuestion deny hook")
            self.assertIn("ask_user_denied.py", raw)

    def test_deny_hook_outputs_actionable_reason(self):
        import subprocess as sp
        proc = sp.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "ask_user_denied.py")],
            input='{"tool_name":"AskUserQuestion","tool_input":{}}',
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        decision = payload["hookSpecificOutput"]["permissionDecision"]
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(decision, "deny")
        for needle in ("blocked", "默认不测", "report"):
            self.assertIn(needle, reason, "deny 理由必须是可执行的替代动作")


class TestReportBoundarySection(unittest.TestCase):
    """方案 C：report 必带边界/未测清单（禁问后"漏没漏"只能从报告看）。"""

    def test_report_spec_requires_boundary_list(self):
        spec = self._read("knowledge/report_spec.md")
        self.assertIn("### 边界与未测清单", spec)
        self.assertIn("hypothesis [blocked]", spec)
        self.assertIn("scope 争议未测", spec)

    def _read(self, rel):
        with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()


# ── 速度修复（2026-09-25 方案 S1/S2）：超时 15→5 + --batch 批量模式 ──────────


class TestSpeedFixes(unittest.TestCase):
    """S1: 默认超时 5s（砍 000 长尾）；S2: --batch 单进程连发（砍单发进程开销）。"""

    def test_default_timeout_is_five_seconds(self):
        self.assertEqual(http_request.CURL_TIMEOUT, 5,
                         "默认超时必须 5s（实测 15s 时单会话 000 长尾 28 分钟）")

    def test_extract_batch_parses_file_and_delay(self):
        batch, delay, rest = http_request.extract_batch(
            ["--batch", "b.txt", "--delay", "0.2", "-X", "POST", "http://x/"])
        self.assertEqual((batch, delay), ("b.txt", 0.2))
        self.assertEqual(rest, ["-X", "POST", "http://x/"])

    def test_extract_batch_defaults_delay(self):
        batch, delay, rest = http_request.extract_batch(["--batch=b.txt"])
        self.assertEqual(batch, "b.txt")
        self.assertEqual(delay, 0.05)
        self.assertEqual(rest, [])

    def test_help_documents_batch_and_timeout(self):
        proc = _run_cli(["runtime/http_request.py", "--help"])
        out = proc.stdout
        self.assertIn("--batch", out)
        self.assertIn("--delay", out)
        self.assertIn("5s", out)
        self.assertIn("差分候选", out, "回执要给差分候选清单（大洞导向）")


class TestBatchEndToEnd(unittest.TestCase):
    """run_batch 端到端：单进程连发，每发独立落盘，末尾出非 200/404 差分候选。"""

    @classmethod
    def setUpClass(cls):
        import http.server
        import socketserver
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/ok"):
                    body = b"OK"
                    self.send_response(200)
                elif self.path.startswith("/secret"):
                    body = b"SENSITIVE"
                    self.send_response(403)
                else:
                    body = b"nope"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_batch_writes_every_response_and_reports_anomalies(self):
        import io
        import contextlib
        base = tempfile.mkdtemp(prefix="xx_batch_")
        self.addCleanup(shutil.rmtree, base, True)
        target = "batch-target.test"
        batch_file = os.path.join(base, "b.txt")
        with open(batch_file, "w", encoding="utf-8") as handle:
            handle.write("# 注释行应被跳过\n")
            handle.write(f"GET http://127.0.0.1:{self.port}/ok\n")
            handle.write(f"GET http://127.0.0.1:{self.port}/missing\n")
            handle.write(f"GET http://127.0.0.1:{self.port}/secret\n")

        orig_results = http_request.RESULTS_DIR
        http_request.RESULTS_DIR = base
        self.addCleanup(setattr, http_request, "RESULTS_DIR", orig_results)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = http_request.run_batch(target, batch_file, delay=0)
        out = buf.getvalue()

        resp_dir = os.path.join(base, target, "responses")
        files = [f for f in os.listdir(resp_dir) if f.endswith(".txt")]
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(files), 3, f"每发一个独立证据文件: {files}")
        self.assertIn("[batch done]", out)
        self.assertIn("200×1", out)
        self.assertIn("404×1", out)
        self.assertIn("[batch 差分候选]", out)
        self.assertIn("403", out, "非 200/404 必须被点名")
        self.assertIn("/secret", out)
        # 状态元数据必须干净（不被 -w 格式污染）
        for name in os.listdir(resp_dir):
            if name.endswith(".json"):
                meta = json.load(open(os.path.join(resp_dir, name), encoding="utf-8"))
                self.assertIn(meta["status"], ("200", "404", "403"), meta)


class TestSpeedPromptContract(unittest.TestCase):
    """CLAUDE.md 侧：超时改 5s、batch 指引、大洞四面句必须在场（每轮热路径）。"""

    def setUp(self):
        with open(os.path.join(PROJECT_ROOT, "CLAUDE.md"), encoding="utf-8") as handle:
            self.doc = handle.read()

    def test_timeout_default_in_help_not_contract(self):
        # 2026-10-07 P2：默认超时是工具行为，从热路径合同移入 --help（HELP_TEXT）。
        self.assertNotIn("超时 5s", self.doc)
        proc = _run_cli(["runtime/http_request.py", "--help"])
        self.assertIn("默认 5s", proc.stdout)

    def test_sweep_batch_cred_hint_in_doc(self):
        # P2：合同只背「何时用+命令名」；模板枚举走 sweep，精确多行走 --batch <file|->。
        self.assertIn("--sweep", self.doc)
        self.assertIn("--batch <file|->", self.doc)
        self.assertIn("--cred c-NNN", self.doc)
        self.assertNotIn("同类 ≥5 发用 `--batch <file>`", self.doc)

    def test_no_attack_surface_plan_list(self):
        """2026-09-25 用户裁定：预设攻击面清单 = plan/认知引导，违反"不规定先扫哪类面"。
        AI 在 goal 驱动下自然会测这些面，规则层不许代它排面序。"""
        self.assertNotIn("大洞四面", self.doc)
        self.assertNotIn("匿名期大洞", self.doc)


if __name__ == "__main__":
    unittest.main()