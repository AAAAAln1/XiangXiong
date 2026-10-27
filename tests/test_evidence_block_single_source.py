"""证据块正则必须**只有一份实现**，且认全角括号（2026-09-18）。

## 发现的 bug（真实账本实测）

同一条正则原先在三处各写了一份：

| 位置 | 用途 | 2026-09-17 补全角了吗 |
|---|---|---|
| `project._EVIDENCE_CHUNK_RE` | `brief()` 压缩时保住证据 | ✅ 补了 |
| `graph._EVIDENCE_BLOCK_RE` | `normalized_claim()` → **node_id** | ❌ 没补 |
| `merge.EVIDENCE_BLOCK_RE` | `_normalize_hypothesis_text()` → 账本更新 | ❌ 没补 |

而真实 memory 里的证据**普遍写成全角**（本机 `.claude/memory.md` 的 hypothesis 行
**3/3 命中**）。于是 `graph.normalized_claim` 的承诺失效 —— 它的 docstring 写着
「补证据（追加 `req_NNN`）或升级状态都不改变 ID」，但全角证据没被剥掉时，
证据文本进了 crc32 → **同一 claim 补一条证据就换一个 ID**。

ID 不是装饰：它是投影里 `intent=h:xxxxxx` 的唯一指称（`deepdive.py` / `intents.claim`
都按它取行）。ID 一漂，抄下来的派发指令就指不到东西。

同一份副本还喂养 merge 的账本更新：全角证据使新旧行归一化不相等 →
`_hypothesis_ledger_update` 静默失配 → 同一假设被 ADD 成第二行（重复行堆积，
投影里一条假设同时出现在 `[FRONTIER]` 与 `[SETTLED]`）。

这是 `filelock.py` 顶部记过的那类事故的第二次发生
（"`protocol_scan` 与 `diff_scan` 各写一份 → 只修了一半"）。

## 锁死的不变量

1. `[]` / `()` / `（）` 三种包裹形态都剥；`req_` / `exp-` / `art-` 三种引用都算证据
2. `node_id` 对"同 claim、不同证据"**稳定**（半角与全角都要稳定）
3. 状态升级不改号
4. `anchors()` 不把证据编号当锚点（全角也要剥）
5. merge 的账本更新在全角证据下**必须 fire**（替换而非追加）
6. 源码里该正则**只允许出现一次**（`runtime/graph.py`）
"""

import os
import re
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import graph, merge  # noqa: E402

CLAIM = "已知 /api/admin → 无 Authorization 头 GET → 期望 200 而非 401"


class TestRegexStripsEveryEvidenceForm(unittest.TestCase):
    def strip(self, text):
        return graph.EVIDENCE_BLOCK_RE.sub("", text)

    def test_square_brackets(self):
        self.assertEqual(self.strip(f"{CLAIM} [req_001]").strip(), CLAIM)

    def test_halfwidth_parens(self):
        self.assertEqual(self.strip(f"{CLAIM} (req_001, exp-002)").strip(), CLAIM)

    def test_fullwidth_parens(self):
        """真实 memory 的常见形态 —— 修复前这一条会失败。"""
        self.assertEqual(self.strip(f"{CLAIM}（req_039/040/041, exp-001）").strip(), CLAIM)

    def test_artifact_refs_are_evidence_too(self):
        """`art-NNN` 与 req/exp 一样是证据：进 ID 同样会改号。"""
        self.assertEqual(self.strip(f"{CLAIM}（art-007）").strip(), CLAIM)

    def test_mixed_forms_in_one_line(self):
        line = f"{CLAIM}（req_001） [exp-002] (art-003)"
        self.assertEqual(self.strip(line).strip(), CLAIM)


class TestNodeIdIsStableAcrossEvidenceChanges(unittest.TestCase):
    """ID 是身份，不是行号 —— 「补证据不改号」在四种形态下都必须成立。"""

    def test_id_unchanged_when_fullwidth_evidence_appended(self):
        before = f"hypothesis [observed]: {CLAIM}（req_001）"
        after = f"hypothesis [observed]: {CLAIM}（req_001/002/003, exp-009）"
        self.assertEqual(graph.node_id(graph.KIND_EDGE, before),
                         graph.node_id(graph.KIND_EDGE, after))

    def test_id_unchanged_for_every_bracket_form(self):
        base = graph.node_id(graph.KIND_EDGE, f"hypothesis [observed]: {CLAIM}")
        for suffix in ("（req_001）", "(req_001)", "[req_001]", "（art-001）"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    base,
                    graph.node_id(graph.KIND_EDGE, f"hypothesis [observed]: {CLAIM}{suffix}"),
                )

    def test_id_unchanged_when_state_upgrades(self):
        self.assertEqual(
            graph.node_id(graph.KIND_EDGE, f"hypothesis [observed]: {CLAIM}（req_001）"),
            graph.node_id(graph.KIND_EDGE, f"hypothesis [confirmed]: {CLAIM}（req_001, exp-001）"),
        )

    def test_id_changes_when_the_claim_itself_changes(self):
        """反向：真的要区分两条不同假设 —— 否则 ID 就没用了。"""
        self.assertNotEqual(
            graph.node_id(graph.KIND_EDGE, f"hypothesis [observed]: {CLAIM}"),
            graph.node_id(graph.KIND_EDGE, "hypothesis [observed]: 另一条完全不同的假设"),
        )

    def test_falsification_halfwidth_only_regex_would_break_id(self):
        """反证：把正则退回"只认半角"，上面那条稳定性立刻失效。

        这条同时是"这个 bug 真的存在"的证据 —— 不是推测。
        """
        legacy = re.compile(r"[\[\(][^\]\)]*(?:req_|exp-)[^\]\)]*[\]\)]")
        before = f"hypothesis [observed]: {CLAIM}（req_001）"
        after = f"hypothesis [observed]: {CLAIM}（req_002）"

        def legacy_id(line):
            text = graph._PREFIX_RE.sub("", line.strip(), count=1)
            text = legacy.sub("", text)
            text = re.sub(r"\s+", " ", text).strip()
            import zlib

            return zlib.crc32(text.encode("utf-8")) & 0xFFFFFF

        self.assertNotEqual(legacy_id(before), legacy_id(after),
                            "半角正则下证据没被剥掉 → 换证据就换 ID（这就是原 bug）")
        self.assertEqual(graph.node_id(graph.KIND_EDGE, before),
                         graph.node_id(graph.KIND_EDGE, after),
                         "统一后的正则下 ID 稳定")


class TestAnchorsIgnoreEvidenceRefs(unittest.TestCase):
    def test_fullwidth_evidence_is_not_an_anchor(self):
        with_evidence = graph.anchors(f"{CLAIM}（req_039/040/041）")
        without = graph.anchors(CLAIM)
        self.assertEqual(with_evidence, without)

    def test_evidence_ids_never_leak_into_anchor_set(self):
        found = graph.anchors("surface: /api/x（req_001, exp-002, art-003）")
        for leaked in ("req_001", "exp-002", "art-003"):
            self.assertNotIn(leaked, found)


class TestMergeLedgerUpdateFiresOnFullwidthEvidence(unittest.TestCase):
    """账本更新的核心前提：新旧行归一化后必须相等。"""

    def test_normalize_ignores_fullwidth_evidence(self):
        old = f"hypothesis [observed]: {CLAIM}（req_001）"
        new = f"hypothesis [observed]: {CLAIM}（req_002/003）"
        self.assertEqual(merge._normalize_hypothesis_text(old),
                         merge._normalize_hypothesis_text(new))

    def test_update_replaces_instead_of_appending(self):
        """全角证据下：同 claim 的新行必须**替换**旧行，而不是变成第二行。

        修复前这里会 ADD —— 于是一条假设同时出现在 [FRONTIER] 与 [SETTLED]。
        """
        old = f"hypothesis [observed]: {CLAIM}（req_001）\n"
        lines = ["target: ledger.example.test\n", old]
        new = f"hypothesis [observed]: {CLAIM}（req_002）"

        out = merge.add_line(list(lines), new)
        self.assertEqual(len(out), len(lines), "应替换，不该多出一行")
        self.assertIn("req_002", out[-1])
        self.assertEqual(sum(1 for l in out if l.startswith("hypothesis")), 1)

    def test_falsification_halfwidth_normalize_would_append(self):
        """反证：半角正则下同一场景会追加第二行（重复行堆积）。"""
        legacy = re.compile(r"[\[\(][^\]\)]*(?:req_|exp-)[^\]\)]*[\]\)]")
        old = f"hypothesis [observed]: {CLAIM}（req_001）"
        new = f"hypothesis [observed]: {CLAIM}（req_002）"
        norm = lambda line: legacy.sub("", merge.HYPOTHESIS_PREFIX_RE.match(line).group(1)).strip()
        self.assertNotEqual(norm(old), norm(new),
                            "半角正则下视为两条不同假设 → 旧行滞留 + 新行追加")


class TestSingleImplementationGuard(unittest.TestCase):
    """同一份正则只允许存在一处 —— 这是防"再修一半"的唯一机械手段。"""

    def _sources(self):
        runtime = os.path.join(PROJECT_ROOT, "runtime")
        out = []
        for name in sorted(os.listdir(runtime)):
            if name.endswith(".py"):
                path = os.path.join(runtime, name)
                out.append((name, open(path, encoding="utf-8").read()))
        return out

    def test_evidence_block_regex_defined_only_in_graph(self):
        needle = "re.compile(r\"[\\[\\(（][^\\]\\)）]*(?:req_|exp-|art-)"
        hits = [name for name, src in self._sources() if needle in src]
        self.assertEqual(hits, ["graph.py"], f"证据块正则出现了多份：{hits}")

    def test_nobody_recompiles_a_legacy_copy(self):
        """半角专用副本（缺 `（` 的字符类）一律不允许出现。"""
        needle = 're.compile(r"[\\[\\(][^\\]\\)]*(?:req_|exp-)'
        hits = [name for name, src in self._sources() if needle in src]
        self.assertEqual(hits, [], f"发现未同步的旧副本：{hits}")

    def test_consumers_all_use_the_shared_one(self):
        for module, attr in ((merge, "EVIDENCE_BLOCK_RE"), (graph, "EVIDENCE_BLOCK_RE")):
            with self.subTest(module=module.__name__):
                self.assertIs(getattr(module, attr), graph.EVIDENCE_BLOCK_RE)

    def test_experiment_uses_the_shared_one(self):
        from runtime import experiment

        self.assertIs(experiment.EVIDENCE_BLOCK_RE, graph.EVIDENCE_BLOCK_RE)

    def test_project_uses_the_shared_one_for_brief(self):
        """`brief` 已迁到 digest；它用的必须是同一份。"""
        from runtime import digest

        src = open(os.path.join(PROJECT_ROOT, "runtime", "digest.py"),
                   encoding="utf-8").read()
        self.assertIn("EVIDENCE_BLOCK_RE", src)
        self.assertIn("from .graph import", src)
        self.assertIsInstance(digest.brief(f"{CLAIM}（req_001）", 200), str)


if __name__ == "__main__":
    unittest.main()
