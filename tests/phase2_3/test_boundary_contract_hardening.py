#!/usr/bin/env python3
"""
Boundary Contract Hardening — regression only (no new product features).

Locks:
1) Turn telemetry stays event-layer (not WORLD / SessionStart / assembler).
2) decision:block is historical debt (conflict_reason only); not fed by
   turn / stagnation / pattern / artifact.
3) .turn_state / .observe_state / .pattern_counts are not merge World sources.

Invariants: Observation ≠ Fact; Record ≠ Interpretation; Interpretation ≠ Control.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
STOP_PATH = ROOT / "scripts" / "stop_check.py"
SESSION_START = ROOT / "scripts" / "session_start.py"
ASSEMBLER = ROOT / "context" / "assembler.py"
PROJECT = ROOT / "runtime" / "project.py"
MERGE = ROOT / "runtime" / "merge.py"
OBSERVE = ROOT / "scripts" / "observe.py"
HTTP_REQ = ROOT / "runtime" / "http_request.py"

TURN_KEYS = frozenset({"turn_tool_use_count", "turns_without_tool_use"})
FORBIDDEN_TELEMETRY_WORDS = (
    "idle",
    "stuck",
    "health",
    "productivity",
    "agent_state",
    "confidence",
    "watch",
)


def _load_stop():
    spec = importlib.util.spec_from_file_location("stop_check_hardening", STOP_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _write_jsonl(path: Path, rows: list) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _asst(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _run_stop(mod, transcript: Path) -> tuple[list, str]:
    stdin = json.dumps({"transcript_path": str(transcript)})
    buf = io.StringIO()
    with mock.patch.object(mod.sys, "stdin", io.StringIO(stdin)):
        with mock.patch.object(mod.sys, "stdout", buf):
            try:
                mod.main()
            except SystemExit as exc:
                assert exc.code in (0, None)
    raw = buf.getvalue()
    parsed = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            parsed.append(json.loads(line))
    return parsed, raw


def _strip_comments(src: str) -> str:
    lines = []
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        if "#" in ln:
            ln = ln[: ln.index("#")]
        lines.append(ln)
    return "\n".join(lines)


class TestTurnEventLayerOnly(unittest.TestCase):
    """turn_state must not enter compile / SessionStart / assembler."""

    def test_project_compile_source_has_no_turn_hooks(self):
        src = PROJECT.read_text(encoding="utf-8")
        for needle in (
            "turn_state",
            "turns_without_tool_use",
            "turn_tool_use_count",
            "TURN_STATE",
            ".turn_state.json",
        ):
            self.assertNotIn(needle, src, f"project.py must not reference {needle}")

    def test_session_start_only_calls_compile(self):
        src = SESSION_START.read_text(encoding="utf-8")
        self.assertIn("project_compile", src)
        for needle in ("turn_state", "turns_without_tool_use", "turn_tool_use_count"):
            self.assertNotIn(needle, src)

    def test_assembler_source_has_no_turn_hooks(self):
        src = ASSEMBLER.read_text(encoding="utf-8")
        for needle in (
            "turn_state",
            "turns_without_tool_use",
            "turn_tool_use_count",
            ".turn_state.json",
        ):
            self.assertNotIn(needle, src)

    def test_compile_output_ignores_on_disk_turn_state(self):
        sys.path.insert(0, str(ROOT / "runtime"))
        import project as project_mod  # type: ignore

        with tempfile.TemporaryDirectory(prefix="xx_hard_compile_") as tmp:
            mem = Path(tmp) / "memory.md"
            mem.write_text(
                "target: harden.example\nsurface: only-world\n",
                encoding="utf-8",
            )
            # Poison files next to real project root would not be read if
            # compile uses project_root(); we still assert output keys + patch observe.
            os.environ["XIANGXIONG_WORLD_ACTIVE"] = str(mem)
            real_obs = project_mod._load_observe_state

            def fake_obs():
                return {
                    "consecutive_none": 1,
                    "last_round": 1,
                    "actions": [{"fp": "HTTP:GET:x", "round": 1}],
                }

            project_mod._load_observe_state = fake_obs  # type: ignore
            try:
                out = project_mod.compile()
            finally:
                project_mod._load_observe_state = real_obs  # type: ignore
                os.environ.pop("XIANGXIONG_WORLD_ACTIVE", None)

            self.assertNotIn("turns_without_tool_use", out)
            self.assertNotIn("turn_tool_use_count", out)
            self.assertNotIn("idle", out.lower())
            self.assertIn("harden.example", out)

    def test_stop_turn_payload_whitelist(self):
        with tempfile.TemporaryDirectory(prefix="xx_hard_wl_") as tmp:
            sc = _load_stop()
            sc.TURN_STATE_PATH = os.path.join(tmp, ".turn_state.json")
            sc.STATE_PATH = os.path.join(tmp, ".observe_state.json")
            Path(sc.STATE_PATH).write_text(
                json.dumps({"version": 3, "consecutive_none": 0, "actions": []}),
                encoding="utf-8",
            )
            tr = Path(tmp) / "t.jsonl"
            _write_jsonl(tr, [_user("u"), _asst("no tools")])
            parsed, raw = _run_stop(sc, tr)
            self.assertTrue(parsed)
            turn = next(p for p in parsed if "turn_tool_use_count" in p)
            self.assertEqual(set(turn.keys()), TURN_KEYS)
            self.assertEqual(turn["turn_tool_use_count"], 0)
            self.assertIsInstance(turn["turns_without_tool_use"], int)
            low = json.dumps(turn).lower()
            for w in FORBIDDEN_TELEMETRY_WORDS:
                self.assertNotIn(w, low)
            self.assertNotIn("stagnation_level", turn)
            self.assertNotIn("decision", turn)


class TestBlockDebtIsolation(unittest.TestCase):
    """decision:block only via conflict_reason; not turn/stagnation/pattern/artifact."""

    def test_single_executable_block_near_conflict_reason(self):
        src = STOP_PATH.read_text(encoding="utf-8")
        exec_src = _strip_comments(src)
        hits = list(
            re.finditer(r'["\']decision["\']\s*:\s*["\']block["\']', exec_src)
        )
        self.assertEqual(len(hits), 1, "exactly one executable decision:block")
        pos = hits[0].start()
        self.assertIn("conflict_reason", exec_src[max(0, pos - 500) : pos])

    def test_section_2a_onward_has_no_decision_block(self):
        src = STOP_PATH.read_text(encoding="utf-8")
        idx = src.find("# --- (2a) turn event telemetry")
        self.assertGreater(idx, 0)
        after = _strip_comments(src[idx:])
        self.assertIsNone(
            re.search(r'["\']decision["\']\s*:\s*["\']block["\']', after)
        )

    def test_no_if_telemetry_block_patterns_in_stop(self):
        """Static ban: telemetry vars must not gate decision/block."""
        src = STOP_PATH.read_text(encoding="utf-8")
        # AST-scan if-tests that mention both turn counters and block-ish control
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            cond = ast.dump(node.test)
            body = ast.dump(node)
            if "turn_tool_use" in cond or "turns_without_tool" in cond:
                if "block" in body and "decision" in body:
                    bad.append(cond[:120])
            if "stagnation_level" in cond or "compute_level" in cond:
                if '"block"' in body or "'block'" in body:
                    bad.append(cond[:120])
            if "pattern" in cond.lower() and "block" in body and "decision" in body:
                bad.append(cond[:120])
        self.assertEqual(bad, [], f"telemetry must not gate block: {bad}")

    def test_high_stagnation_and_turn_do_not_block(self):
        with tempfile.TemporaryDirectory(prefix="xx_hard_blk_") as tmp:
            sc = _load_stop()
            sc.TURN_STATE_PATH = os.path.join(tmp, ".turn_state.json")
            sc.STATE_PATH = os.path.join(tmp, ".observe_state.json")
            Path(sc.STATE_PATH).write_text(
                json.dumps(
                    {
                        "version": 3,
                        "consecutive_none": 20,
                        "actions": [{"fp": "HTTP:GET:z", "round": i} for i in range(12)],
                        "prefix_counts": {"vuln": 0},
                    }
                ),
                encoding="utf-8",
            )
            tr = Path(tmp) / "t.jsonl"
            _write_jsonl(tr, [_user("x"), _asst("plain text no contract breach")])
            parsed, raw = _run_stop(sc, tr)
            self.assertTrue(all(p.get("decision") != "block" for p in parsed))
            self.assertTrue(any("turn_tool_use_count" in p for p in parsed))
            self.assertTrue(any("stagnation_level" in p for p in parsed))

    def test_only_conflict_phrases_block(self):
        with tempfile.TemporaryDirectory(prefix="xx_hard_cf_") as tmp:
            sc = _load_stop()
            sc.TURN_STATE_PATH = os.path.join(tmp, ".turn_state.json")
            sc.STATE_PATH = os.path.join(tmp, ".observe_state.json")
            Path(sc.STATE_PATH).write_text(
                json.dumps({"version": 3, "consecutive_none": 0, "actions": []}),
                encoding="utf-8",
            )
            tr = Path(tmp) / "t.jsonl"
            sc.save_turn_state(3)
            _write_jsonl(tr, [_user("q"), _asst("需要我继续吗？")])
            parsed, raw = _run_stop(sc, tr)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0].get("decision"), "block")
            self.assertIn("reason", parsed[0])
            self.assertNotIn("turn_tool_use_count", raw)
            self.assertEqual(sc.load_turn_state()["turns_without_tool_use"], 3)

    def test_http_pattern_and_artifact_strings_not_in_stop_enforcement(self):
        """stop_check must not import or branch on pattern_counts / artifact paths."""
        src = STOP_PATH.read_text(encoding="utf-8")
        self.assertNotIn("pattern_counts", src)
        self.assertNotIn("OBSERVATION REPETITION", src)
        self.assertNotIn("stdout_artifact", src)
        self.assertNotIn("body_artifact", src)
        self.assertNotIn("pattern_occurrences", src)


class TestTelemetryFilesNotWorldSources(unittest.TestCase):
    """Caches must not be accepted as memory merge inputs."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "runtime"))
        import merge as merge_mod  # type: ignore

        self.merge = merge_mod

    def test_valid_prefixes_exclude_telemetry_and_cache_keys(self):
        banned = (
            "turns_without_tool_use:",
            "turn_tool_use_count:",
            "consecutive_none:",
            "same_action_count:",
            "stagnation_level:",
            "pattern_counts:",
            "pattern_key:",
            "pattern_occurrences:",
            "observe_state:",
            "turn_state:",
        )
        joined = " ".join(self.merge.VALID_PREFIXES)
        for b in banned:
            self.assertFalse(
                any(p.startswith(b.rstrip(":")) or p == b for p in self.merge.VALID_PREFIXES),
                f"{b} must not be a VALID_PREFIX",
            )
            # also no prefix that equals the bare name
            bare = b.rstrip(":")
            self.assertNotIn(bare + ":", joined.replace(" ", ""))

    def test_merge_rejects_telemetry_lines_keeps_surface(self):
        with tempfile.TemporaryDirectory(prefix="xx_hard_merge_") as tmp:
            mem = Path(tmp) / "memory.md"
            mem.write_text("target: t.example\n", encoding="utf-8")
            os.environ["XIANGXIONG_WORLD_ACTIVE"] = str(mem)
            self.merge.TURN_STATE_PATH = str(Path(tmp) / ".turn_state.json")
            self.merge.OBSERVE_STATE_PATH = str(Path(tmp) / ".observe_state.json")
            try:
                text = """DELTA:
surface: real-surface
turns_without_tool_use: 5
turn_tool_use_count: 0
consecutive_none: 9
same_action_count: 3
stagnation_level: critical
pattern_counts: {\"x\":1}
pattern_key: GET:h/p:200:deadbeef
pattern_occurrences: 8
observe_state: leak
turn_state: leak
"""
                ops = self.merge.parse_delta(text)
                lines = self.merge.read_memory()
                lines = self.merge.apply_delta(lines, ops)
                self.merge.write_memory(lines)
                body = mem.read_text(encoding="utf-8")
                self.assertIn("real-surface", body)
                for leak in (
                    "turns_without_tool_use",
                    "turn_tool_use_count",
                    "consecutive_none",
                    "same_action_count",
                    "stagnation_level",
                    "pattern_counts",
                    "pattern_key",
                    "pattern_occurrences",
                    "observe_state:",
                    "turn_state:",
                ):
                    self.assertNotIn(leak, body)
            finally:
                os.environ.pop("XIANGXIONG_WORLD_ACTIVE", None)

    def test_is_valid_line_rejects_cache_filenames_as_prefixes(self):
        for line in (
            ".turn_state.json: 1",
            ".observe_state.json: 1",
            ".pattern_counts.json: 1",
            "turn_state: x",
            "observe_state: x",
        ):
            self.assertFalse(self.merge._is_valid_line(line), line)

    def test_merge_source_does_not_read_pattern_or_turn_as_delta_input(self):
        src = MERGE.read_text(encoding="utf-8")
        # may reset turn_state on target change — allowed; must not parse those files as DELTA
        self.assertNotIn("pattern_counts", src)
        self.assertIn("TURN_STATE_PATH", src)  # reset only
        # ensure no json.load of pattern
        self.assertNotIn("pattern_counts.json", src)

    def test_observe_does_not_write_memory(self):
        src = OBSERVE.read_text(encoding="utf-8")
        self.assertNotIn("write_memory", src)
        self.assertNotIn("merge.py", src)
        # observe reads memory watermark only
        self.assertIn("MEMORY_PATH", src)


class TestNoNewEnforcementFromCaches(unittest.TestCase):
    """Cross-file: pattern/artifact modules don't feed stop block."""

    def test_http_request_has_no_decision_block(self):
        src = HTTP_REQ.read_text(encoding="utf-8")
        self.assertNotIn("decision", src)
        self.assertNotIn('"block"', src)

    def test_stop_does_not_import_http_or_execute(self):
        src = STOP_PATH.read_text(encoding="utf-8")
        self.assertNotIn("http_request", src)
        self.assertNotIn("execute", src)
        self.assertNotIn("import merge", src)


if __name__ == "__main__":
    unittest.main()
