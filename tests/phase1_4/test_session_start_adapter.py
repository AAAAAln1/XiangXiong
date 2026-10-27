#!/usr/bin/env python3
"""Phase 1.4: session_start is Adapter-only; stdout == compile() == Phase0 gold."""

from __future__ import annotations

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
RUNTIME_FILES = ("paths.py", "world.py", "graph.py", "digest.py", "filelock.py",
                 "turn_state.py", "project.py", "merge.py")


def _build_ws(tmp: Path) -> Path:
    ws = tmp / "w"
    (ws / ".claude").mkdir(parents=True)
    (ws / "scripts").mkdir(parents=True)
    (ws / "runtime").mkdir(parents=True)
    (ws / "cognition").mkdir(parents=True)
    shutil.copytree(FIX / "experiments", ws / "results" / "phase0.example.test" / "experiments")
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
    for name in RUNTIME_FILES:
        shutil.copy2(ROOT / "runtime" / name, ws / "runtime" / name)
    shutil.copy2(ROOT / "scripts" / "session_start.py", ws / "scripts" / "session_start.py")
    return ws


class TestAdapterOnly(unittest.TestCase):
    def test_session_start_has_no_projection_logic(self):
        src = (ROOT / "scripts" / "session_start.py").read_text(encoding="utf-8")
        banned = (
            r"extract_world_model",
            r"WORLD MODEL",
            r"ANTI-COMPRESSION",
            r"OPEN UNKNOWNS",
            r"FRONTIER",
            r"SETTLED",
            r"load_cognition",
            r"render_cognition",
            r"load_recent_experiments",
            r"should_resume",
            r"def parse\b",
            r"WorldView",
        )
        for pat in banned:
            self.assertIsNone(re.search(pat, src), msg=f"projection logic leaked: {pat}")
        self.assertIn("project_compile", src)
        self.assertIn("compile", src)

    def test_session_start_equals_compile_equals_phase0(self):
        expected = (FIX / "expected_stdout.txt").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="xx_p14_") as tmp:
            ws = _build_ws(Path(tmp))
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            ss = subprocess.run(
                [sys.executable, str(ws / "scripts" / "session_start.py")],
                cwd=str(ws),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            self.assertEqual(ss.returncode, 0, ss.stderr)
            pc = subprocess.run(
                [sys.executable, str(ws / "runtime" / "project.py")],
                cwd=str(ws),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            self.assertEqual(pc.returncode, 0, pc.stderr)
            self.assertEqual(ss.stdout, pc.stdout)
            self.assertEqual(ss.stdout, expected)
            self.assertEqual(pc.stdout, expected)


if __name__ == "__main__":
    unittest.main()
