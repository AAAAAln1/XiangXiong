"""CLI 人机工程：把「静默失败」变成「显式失败」。

## 背景

宿主模型是 DeepSeek V4 Flash（284B 总参 / **13B 激活**）。官方描述：
「在简单任务上与 V4-Pro 旗鼓相当，但在**高难度任务上仍有差距**」、
「较小的参数规模自然地在……**最复杂的 agentic 工作流**上稍显落后」。

结论：**不能假设模型会自己补全没写清的东西**。凡是"照抄会失败"的地方都是真 bug。

## 本轮实测到的 bug（Git Bash 5.3.9，agent 的真实 shell）

CLAUDE.md 原先教的记账写法：

```
echo "DELTA:\n..." | python runtime/merge.py
```

对照实验（同一 world，同一条 goal 行）：

| 写法 | merge 输出 | 世界行数 |
|---|---|---|
| `echo "DELTA:\\n..."` | **完全无输出** | 2 → **2（没变）** |
| `printf 'DELTA:\\n...\\n'` | `[merge] ADD:1` | 2 → 3 |

shell 的 echo **不解释转义**（bash 默认、PowerShell 的 echo 是 Write-Output），
整段 DELTA 挤成一行 → `parse_delta` 析出 0 个操作 → `exit(0)`。
**看起来成功，实际一条都没记** —— 而记账是"探索产出可累积"的唯一通道。

## 修复方向

不是"猜用户意图去修复"（会引入隐式行为），而是：
1. 给一条**零转义**的路径：`--delta-file <path>`（DELTA 先 Write 落盘）
2. 把静默失败**变成显式警告**（`warn_lf_escape_trap`）
3. 同理给 `experiment.py close` 加 `--memory-update-file`
4. 投影首屏带上 `target`（原先埋在第 5 段，实测 1,128 字符处）

## 锁死的不变量

1. `echo` 那条路**必须**产生警告，不得再静默 exit 0
2. `--delta-file` 必须真的写入世界（文件路径零转义）
3. `--delta-file` 读不到文件 → exit 2 + 可执行提示
4. 无参数时的 stdin 通道保持兼容
5. `[GOAL]` 段头必须带 target
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import digest, merge, project  # noqa: E402
from runtime.world import parse as world_parse  # noqa: E402

PY = sys.executable
BASE_WORLD = "target: ergo.example.test\nsurface: /x 存在 (req_001)\n"


def _run(args, stdin=None, world=None, env_extra=None):
    env = dict(os.environ, PYTHONUTF8="1")
    if world:
        env["XIANGXIONG_WORLD_ACTIVE"] = world
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PY, *args], cwd=PROJECT_ROOT, input=stdin, capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env,
    )


def _find_bash():
    """Claude Code 在 Windows 上按 `git 路径/../../bin/bash.exe` 找 bash。"""
    git = shutil.which("git")
    if git:
        candidate = os.path.normpath(os.path.join(git, "..", "..", "bin", "bash.exe"))
        if os.path.isfile(candidate):
            return candidate
    for path in (r"F:\Git\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
        if os.path.isfile(path):
            return path
    found = shutil.which("bash")
    return found


class TestDeltaFileMode(unittest.TestCase):
    """零转义路径：DELTA 先 Write 落盘，再传路径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xx_ergo_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.world = os.path.join(self.tmp, "memory.md")
        with open(self.world, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_WORLD)

    def _world_text(self):
        with open(self.world, encoding="utf-8") as handle:
            return handle.read()

    def test_delta_file_writes_to_world(self):
        delta = os.path.join(self.tmp, "delta.txt")
        with open(delta, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("DELTA:\ngoal: 证明未授权访问\nsurface: /y 存在 (req_002)\n")
        proc = _run(["runtime/merge.py", "--delta-file", delta], world=self.world)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ADD:2", proc.stderr)
        body = self._world_text()
        self.assertIn("goal: 证明未授权访问", body)
        self.assertIn("surface: /y 存在", body)

    def test_delta_file_handles_quotes_and_backslashes_verbatim(self):
        """这正是内联写法会崩的地方：内容含单引号、双引号、反斜杠。"""
        payload = "DELTA:\ngoal: prove it's C:\\Windows\\System32 reachable\n"
        delta = os.path.join(self.tmp, "delta.txt")
        with open(delta, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        proc = _run(["runtime/merge.py", "--delta-file", delta], world=self.world)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ADD:1", proc.stderr)
        self.assertIn("it's C:\\Windows\\System32", self._world_text())

    def test_delta_file_dash_reads_stdin(self):
        proc = _run(["runtime/merge.py", "--delta-file", "-"],
                    stdin="DELTA:\ngoal: via stdin dash\n", world=self.world)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ADD:1", proc.stderr)

    def test_missing_file_exits_2_with_actionable_hint(self):
        proc = _run(["runtime/merge.py", "--delta-file", os.path.join(self.tmp, "nope.txt")],
                    world=self.world)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("读不到 DELTA 文件", proc.stderr)
        self.assertIn("Write", proc.stderr, "提示必须说清怎么补救")

    def test_delta_file_without_path_exits_2(self):
        proc = _run(["runtime/merge.py", "--delta-file"], world=self.world)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("需要一个路径", proc.stderr)

    def test_bare_stdin_still_works(self):
        """兼容：既有调用方（测试/脚本）不带参数走 stdin。"""
        proc = _run(["runtime/merge.py"], stdin="DELTA:\ngoal: via bare stdin\n", world=self.world)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ADD:1", proc.stderr)

    def test_unknown_subcommand_still_exits_2(self):
        proc = _run(["runtime/merge.py", "nope"], world=self.world)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("未知子命令", proc.stderr)


class TestLfEscapeTrapIsLoud(unittest.TestCase):
    """核心回归：`echo "DELTA:\\n..."` 那条路必须**出声**。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xx_trap_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.world = os.path.join(self.tmp, "memory.md")
        with open(self.world, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_WORLD)

    def test_single_line_with_literal_backslash_n_warns(self):
        proc = _run(["runtime/merge.py"], stdin="DELTA:\\ngoal: squeezed into one line\\n",
                    world=self.world)
        self.assertEqual(proc.returncode, 0, "仍应 0（不阻断），但必须警告")
        self.assertIn("WARNING", proc.stderr)
        self.assertIn("echo", proc.stderr)
        self.assertIn("--delta-file", proc.stderr, "警告必须给出正确写法")

    def test_warns_but_does_not_write(self):
        _run(["runtime/merge.py"], stdin="DELTA:\\ngoal: squeezed\\n", world=self.world)
        with open(self.world, encoding="utf-8") as handle:
            self.assertNotIn("squeezed", handle.read(), "挤成一行时确实写不进去")

    def test_normal_multiline_is_silent(self):
        proc = _run(["runtime/merge.py"], stdin="DELTA:\ngoal: normal\n", world=self.world)
        self.assertNotIn("WARNING", proc.stderr)

    def test_single_line_without_backslash_n_is_silent(self):
        """单行但无 `\\n`（真的只有一个 token）不该被误报。"""
        self.assertEqual(_capture_warn("DELTA:goal: x"), "")

    def test_empty_input_is_silent(self):
        proc = _run(["runtime/merge.py"], stdin="", world=self.world)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr.strip(), "")


class TestBomTrapIsLoud(unittest.TestCase):
    """UTF-8 BOM 会让 `--delta-file` **静默写 0 条** —— 同 echo 陷阱一类。

    实测（PowerShell 5.1，本机）：`Set-Content -Encoding UTF8` 写出的文件首字节是
    `EF BB BF`；而 `"\\ufeffDELTA:".strip()` **不会**去掉 `\\ufeff`（它不是 Python 的
    空白字符）→ `startswith("DELTA:")` 为假 → 0 个操作 → `exit 0`、无输出。
    而 `--delta-file` 正是 CLAUDE.md 推荐给 agent 的唯一记账路径。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xx_bom_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.world = os.path.join(self.tmp, "memory.md")
        with open(self.world, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_WORLD)

    def _delta_with_bom(self, text):
        path = os.path.join(self.tmp, "delta_bom.txt")
        with open(path, "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + text.encode("utf-8"))
        return path

    def test_bom_file_still_writes(self):
        """BOM 必须被容忍：内容是对的，就该写进去。"""
        delta = self._delta_with_bom("DELTA:\ngoal: BOM 也要能记\n")
        proc = _run(["runtime/merge.py", "--delta-file", delta], world=self.world)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.world, encoding="utf-8") as handle:
            self.assertIn("BOM 也要能记", handle.read())

    def test_bom_on_stdin_also_tolerated(self):
        proc = _run(["runtime/merge.py"], stdin="\ufeffDELTA:\ngoal: stdin BOM\n",
                    world=self.world)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.world, encoding="utf-8") as handle:
            self.assertIn("stdin BOM", handle.read())

    def test_zero_operations_is_never_silent(self):
        """核心守卫：解析出 0 个操作时**必须出声**（原先静默 exit 0）。"""
        delta = os.path.join(self.tmp, "delta_plain.txt")
        with open(delta, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("surface: /x 存在\n")          # 忘了 DELTA: 行
        proc = _run(["runtime/merge.py", "--delta-file", delta], world=self.world)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("0 个操作", proc.stderr)
        self.assertIn("surface: /x 存在", proc.stderr, "要把读到的第一行回显出来")
        with open(self.world, encoding="utf-8") as handle:
            self.assertNotIn("surface: /x 存在\n", handle.read(), "确实没写进去")

    def test_truly_empty_input_stays_silent(self):
        """空输入不是"失败"，不该打扰。"""
        proc = _run(["runtime/merge.py"], stdin="   \n\n", world=self.world)
        self.assertEqual(proc.stderr.strip(), "")


def _capture_warn(text):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        merge.warn_lf_escape_trap(text)
    return buf.getvalue()


class TestWarnHelperDirectly(unittest.TestCase):
    def test_plain_text_no_warning(self):
        self.assertEqual(_capture_warn("surface: /x\nhypothesis: y\n"), "")

    def test_literal_trap_warns(self):
        self.assertIn("WARNING", _capture_warn("DELTA:\\ngoal: x\\n"))

    def test_real_newlines_are_fine(self):
        self.assertEqual(_capture_warn("DELTA:\ngoal: x\n"), "")

    def test_backslash_n_present_but_also_real_newlines(self):
        """已经有真换行时就别误报（内容里出现字面 \\n 是合法的）。"""
        self.assertEqual(_capture_warn("DELTA:\nsurface: C:\\new\\npath\n"), "")


class TestMemoryUpdateFileMode(unittest.TestCase):
    """`experiment.py close --memory-update-file`：躲开 JSON 引号地狱。"""

    def test_helper_parses_file_json(self):
        from runtime.experiment import _parse_memory_update
        payload = '{"text": "it\'s fine", "status": "observed", "evidence": ["req_001"]}'
        parsed = _parse_memory_update(payload)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["text"], "it's fine")

    def test_cli_exposes_the_flag(self):
        proc = _run(["runtime/experiment.py", "close", "--help"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--memory-update-file", proc.stdout)
        self.assertIn("零 shell 转义", proc.stdout)

    def test_missing_update_file_warns_and_continues(self):
        """文件读不到 → 明确说"本次不会写回"，而不是安静地跳过。"""
        tmp = tempfile.mkdtemp(prefix="xx_muf_")
        self.addCleanup(shutil.rmtree, tmp, True)
        proc = _run([
            "runtime/experiment.py", "close", "ergo.example.test", "exp-001",
            "--conclusion", "x", "--status", "unknown",
            "--memory-update-file", os.path.join(tmp, "nope.json"),
        ], world=os.path.join(tmp, "memory.md"))
        self.assertIn("读不到 --memory-update-file", proc.stderr)
        self.assertIn("不会写回 memory", proc.stderr)


class TestGoalHeaderCarriesTarget(unittest.TestCase):
    """首屏必须能回答"我在测哪个站"。"""

    def test_declared(self):
        view = world_parse(["target: t.example.test\n", "goal: g\n"])
        header = digest.format_goal_section(view).splitlines()[0]
        self.assertIn("target=t.example.test", header)

    def test_undeclared(self):
        header = digest.format_goal_section(
            world_parse(["target: t.example.test\n", "surface: x\n"])).splitlines()[0]
        self.assertIn("target=t.example.test", header)

    def test_no_target_says_none(self):
        header = digest.format_goal_section(world_parse(["surface: x\n"])).splitlines()[0]
        self.assertIn("target=(none)", header)

    def test_live_projection_starts_with_target(self):
        """真实投影首行必须同时给出 target 与目标状态。"""
        text = project.compile()
        if not text.strip():
            self.skipTest("无活跃 world")
        first = text.lstrip().splitlines()[0]
        self.assertTrue(first.startswith("[GOAL]"), first[:80])
        self.assertIn("target=", first)


class TestRealShellAccountingPath(unittest.TestCase):
    """在 agent 的真实 shell（Git Bash）里跑通两条路。

    这条测试是本次修复的**根因回归**：它同时断言
    「文件路径成功写入」与「echo 路径不出声地失败」——
    后者一旦变成静默成功，这里就会失败。
    """

    def setUp(self):
        self.bash = _find_bash()
        if not self.bash:
            self.skipTest("未找到 bash（Claude Code 在 Windows 需要 git-bash）")
        self.tmp = tempfile.mkdtemp(prefix="xx_shell_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.world = os.path.join(self.tmp, "memory.md")
        with open(self.world, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_WORLD)

    def _bash(self, script):
        env = dict(os.environ, XIANGXIONG_WORLD_ACTIVE=self.world, PYTHONUTF8="1")
        return subprocess.run(
            [self.bash, "-c", script], cwd=PROJECT_ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=env,
        )

    def _world_lines(self):
        with open(self.world, encoding="utf-8") as handle:
            return len(handle.read().strip().splitlines())

    def test_echo_does_not_expand_and_now_warns(self):
        """已确认：bash 的 echo 不解释 \\n —— 而我们把它变成了显式警告。"""
        before = self._world_lines()
        proc = self._bash(r'''echo "DELTA:\ngoal: via echo" | python runtime/merge.py''')
        self.assertEqual(self._world_lines(), before, "挤成一行，写不进去")
        self.assertIn("WARNING", proc.stderr + proc.stdout)

    def test_delta_file_path_actually_writes(self):
        """零转义路径：两次工具调用（Write + Bash）换一次确定成功。"""
        delta = os.path.join(self.tmp, "delta.txt")
        with open(delta, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("DELTA:\ngoal: 证明 /api/admin 未授权\n")
        before = self._world_lines()
        proc = self._bash(f'''python runtime/merge.py --delta-file "{delta}"''')
        self.assertIn("ADD:1", proc.stderr + proc.stdout, proc.stderr)
        self.assertEqual(self._world_lines(), before + 1)
        with open(self.world, encoding="utf-8") as handle:
            self.assertIn("证明 /api/admin 未授权", handle.read())

    def test_printf_path_also_works(self):
        """短 DELTA 的备选：printf 会解释 \\n。"""
        before = self._world_lines()
        proc = self._bash(r'''printf 'DELTA:\ngoal: via printf\n' | python runtime/merge.py''')
        self.assertIn("ADD:1", proc.stderr + proc.stdout)
        self.assertEqual(self._world_lines(), before + 1)


class TestClaudeMdTeachesTheWorkingPath(unittest.TestCase):
    """契约文档必须教那条**能工作**的路，并点明那条不能工作的。"""

    def setUp(self):
        with open(os.path.join(PROJECT_ROOT, "CLAUDE.md"), encoding="utf-8") as handle:
            self.doc = handle.read()

    def test_teaches_delta_file(self):
        self.assertIn("--delta-file", self.doc)

    def test_warns_against_echo(self):
        """警告必须留下，但**不必**逐字复述那条坏命令。

        2026-09-18 静态负载审计：CLAUDE.md 是每轮都读的热路径，
        把坏命令原文抄一遍是纯查阅型负担；坏命令的完整形态与后果由
        `merge.py` 的运行时警告（`warn_lf_escape_trap`，见本文件上半）
        在真正踩到时打印。热路径只留判据本身。
        """
        self.assertIn("echo", self.doc)
        self.assertIn("静默记 0 条", self.doc, "必须点明后果，否则读者会继续用 echo")
        # 但坏命令**不得**被当成推荐写法出现在任何一行里（tests/test_instruction_shape.py 锁死）

    def test_teaches_memory_update_file(self):
        self.assertIn("--memory-update-file", self.doc)


if __name__ == "__main__":
    unittest.main()
