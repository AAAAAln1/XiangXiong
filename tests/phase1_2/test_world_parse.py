#!/usr/bin/env python3
"""Phase 1.2: world.parse lossless + closed WorldView (H3/H7/H11)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime"))

from world import ParsedLine, WorldView, iter_raw_lines, parse  # type: ignore  # noqa: E402

PHASE0_MEMORY = ROOT / "tests" / "phase0" / "fixture" / "memory.md"


class TestWorldViewClosed(unittest.TestCase):
    def test_fields_only_five(self):
        self.assertEqual(
            set(WorldView._fields),
            {"target", "objects", "unknowns", "constraints", "counters"},
        )
        forbidden = {
            "confidence",
            "priority",
            "importance",
            "score",
            "status",
            "category",
            "recommendation",
            "next_action",
        }
        self.assertTrue(forbidden.isdisjoint(WorldView._fields))


class TestLosslessBlocked(unittest.TestCase):
    def test_blocked_raw_preserved(self):
        line = "hypothesis [blocked]: test blocked hypothesis"
        view = parse([line])
        self.assertEqual(len(view.unknowns), 1)
        item = view.unknowns[0]
        self.assertIsInstance(item, ParsedLine)
        self.assertEqual(item.raw, line)
        self.assertEqual(item.state, "blocked")
        self.assertEqual(item.prefix, "hypothesis")
        # render helper recovers original text
        self.assertIn(line, iter_raw_lines(view))
        self.assertEqual(item.raw, "hypothesis [blocked]: test blocked hypothesis")
        self.assertNotEqual(item.raw, "hypothesis: test blocked hypothesis")


class TestMultiPrefix(unittest.TestCase):
    def test_buckets_and_future_kept(self):
        lines = [
            "target: t.example",
            "surface: /a",
            "vuln: x",
            "cred: y",
            "tech: nginx",
            "port: 443",
            "artifact: path",
            "hypothesis: plain",
            "hypothesis [blocked]: needs creds",
            "counter: c1",
            "dead: d1",
            "future: later",
        ]
        view = parse(lines)
        self.assertEqual(view.target, "t.example")
        self.assertEqual([x.raw for x in view.objects], [
            "surface: /a",
            "vuln: x",
            "cred: y",
            "future: later",
        ])
        self.assertEqual([x.raw for x in view.constraints], [
            "tech: nginx",
            "port: 443",
            "artifact: path",
        ])
        self.assertEqual([x.raw for x in view.unknowns], [
            "hypothesis: plain",
            "hypothesis [blocked]: needs creds",
        ])
        self.assertEqual(view.unknowns[1].state, "blocked")
        self.assertIsNone(view.unknowns[0].state)
        self.assertEqual([x.raw for x in view.counters], [
            "counter: c1",
            "dead: d1",
        ])
        # no sort: input order within buckets
        # no filter of blocked
        raws = set(iter_raw_lines(view))
        for line in lines:
            self.assertIn(line if not line.startswith("target:") else "target: t.example", raws)


class TestNoFilterNoSort(unittest.TestCase):
    def test_keeps_confirmed_and_contradicted(self):
        lines = [
            "hypothesis [confirmed]: a",
            "hypothesis [contradicted]: b",
            "hypothesis [observed]: c",
        ]
        view = parse(lines)
        self.assertEqual(len(view.unknowns), 3)
        self.assertEqual(view.unknowns[0].raw, lines[0])
        self.assertEqual(view.unknowns[1].raw, lines[1])
        self.assertEqual(view.unknowns[2].raw, lines[2])
        self.assertEqual(view.unknowns[0].state, "confirmed")
        self.assertEqual(view.unknowns[1].state, "contradicted")

    def test_does_not_invent_open_stalled(self):
        view = parse(["hypothesis [blocked]: x"])
        self.assertFalse(hasattr(view, "stalled"))
        self.assertFalse(hasattr(view, "open_unknowns"))


class TestPhase0MemoryFixture(unittest.TestCase):
    def test_all_nonempty_lines_recoverable(self):
        text = PHASE0_MEMORY.read_text(encoding="utf-8")
        lines = text.splitlines()
        nonempty = [ln.strip() for ln in lines if ln.strip()]
        view = parse(lines)
        recovered = set(iter_raw_lines(view))
        for ln in nonempty:
            if ln.startswith("target:"):
                self.assertEqual(view.target, ln.split(":", 1)[1].strip())
                self.assertIn(f"target: {view.target}", recovered)
            else:
                self.assertIn(ln, recovered, msg=f"lost line: {ln!r}")

    def test_phase0_hypothesis_states(self):
        view = parse(PHASE0_MEMORY.read_text(encoding="utf-8").splitlines())
        by_raw = {u.raw: u.state for u in view.unknowns}
        self.assertIsNone(
            by_raw.get("hypothesis: search q parameter may reflect unsanitized")
        )
        self.assertEqual(
            by_raw["hypothesis [blocked]: cannot verify auth bypass without credentials"],
            "blocked",
        )
        self.assertEqual(
            by_raw["hypothesis [confirmed]: /health returns 200 without auth"],
            "confirmed",
        )
        self.assertEqual(
            by_raw["hypothesis [contradicted]: /debug was not open on first probe"],
            "contradicted",
        )
        self.assertEqual(
            by_raw["hypothesis [observed]: admin panel may exist at /admin"],
            "observed",
        )


if __name__ == "__main__":
    unittest.main()
