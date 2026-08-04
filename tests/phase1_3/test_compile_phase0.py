#!/usr/bin/env python3
"""Phase 1.3: compile() ≡ Phase0 expected_stdout (H0/H8/H9)."""

from __future__ import annotations

import inspect
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "phase0" / "fixture"
COGNITION = (
    "object_model.md",
    "evidence_model.md",
    "experiment_model.md",
    "surface_misalignment.md",
)


def _build_ws(tmp: Path) -> Path:
    ws = tmp / "w"
    (ws / ".claude").mkdir(parents=True)
    (ws / "scripts").mkdir(parents=True)
    (ws / "runtime").mkdir(parents=True)
    (ws / "cognition").mkdir(parents=True)
    exp_dst = ws / "results" / "phase0.example.test" / "experiments"
    shutil.copytree(FIX / "experiments", exp_dst)
    (ws / ".claude" / "memory.md").write_text(
        (FIX / "memory.md").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    (ws / ".observe_state.json").write_text(
        (FIX / "observe_state.json").read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    for name in COGNITION:
        shutil.copy2(FIX / "cognition" / name, ws / "cognition" / name)
    for name in ("paths.py", "world.py", "project.py", "merge.py"):
        shutil.copy2(ROOT / "runtime" / name, ws / "runtime" / name)
    shutil.copy2(ROOT / "scripts" / "session_start.py", ws / "scripts" / "session_start.py")
    return ws


class TestCompileAPI(unittest.TestCase):
    def test_no_parameters(self):
        sys.path.insert(0, str(ROOT))
        from runtime.project import compile as project_compile

        sig = inspect.signature(project_compile)
        self.assertEqual(list(sig.parameters), [])

    def test_forbidden_tokens_absent_in_source(self):
        src = (ROOT / "runtime" / "project.py").read_text(encoding="utf-8")
        # Strip frozen anti-compression string literals (contain word "priority").
        src_code = re.sub(
            r'ANTI_COMPRESSION_LINES\s*=\s*\([\s\S]*?\)\n',
            "ANTI_COMPRESSION_LINES = ()\n",
            src,
            count=1,
        )
        banned = (
            r"\bfocus\s*=",
            r"\bpriority\s*=",
            r"\brank\s*=",
            r"\bnext_action\b",
            r"\brecommend\b",
            r"Context Optimizer",
            r"Context Manager",
            r"Context Engineering",
            r"\boptimize\b",
        )
        for pat in banned:
            self.assertIsNone(
                re.search(pat, src_code, re.IGNORECASE),
                msg=f"banned pattern present: {pat}",
            )

    def test_no_transcript_reads(self):
        src = (ROOT / "runtime" / "project.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("chat history", src)
        self.assertNotIn("tool history", src)
        self.assertNotIn("conversation log", src)
        self.assertNotIn("transcript_path", src)
        self.assertNotIn("read_transcript", src)

    def test_single_extract_via_world_parse(self):
        src = (ROOT / "runtime" / "project.py").read_text(encoding="utf-8")
        self.assertIn("world_parse", src)
        self.assertNotIn("def _extract_world_model", src)
        self.assertIn("from .world import", src)


class TestPhase0Parity(unittest.TestCase):
    def test_compile_matches_expected_stdout(self):
        expected = (FIX / "expected_stdout.txt").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="xx_p13_") as tmp:
            ws = _build_ws(Path(tmp))
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            # Default paths: memory at ws/.claude/memory.md via copied paths.project_root
            # paths.project_root is parent of runtime/ inside ws → correct.
            proc = subprocess.run(
                [sys.executable, str(ws / "runtime" / "project.py")],
                cwd=str(ws),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, expected)

    def test_session_start_adapter_matches(self):
        expected = (FIX / "expected_stdout.txt").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="xx_p13a_") as tmp:
            ws = _build_ws(Path(tmp))
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                [sys.executable, str(ws / "scripts" / "session_start.py")],
                cwd=str(ws),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, expected)

    def test_key_sections_present(self):
        expected = (FIX / "expected_stdout.txt").read_text(encoding="utf-8")
        for marker in (
            "[COGNITION]",
            "[ANTI-COMPRESSION]",
            "[WORLD MODEL]",
            "[RESUME]",
            "[OBSERVE]",
            "[ARTIFACTS]",
        ):
            self.assertIn(marker, expected)


if __name__ == "__main__":
    unittest.main()
