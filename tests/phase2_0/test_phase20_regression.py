#!/usr/bin/env python3
"""Phase 2.0 regression (single world projection source) — section order contract only.

v0.3: PostToolUse assembler 投影已删除；此文件只保留 compile() 段序契约
(WORLD → RECENT EXPERIMENTS → HYPOTHESES → RESUME → ARTIFACTS)。
"""

from __future__ import annotations

import os
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
RUNTIME_FILES = ("paths.py", "world.py", "project.py", "merge.py")


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
    for name in COGNITION:
        shutil.copy2(FIX / "cognition" / name, ws / "cognition" / name)
    for name in RUNTIME_FILES:
        shutil.copy2(ROOT / "runtime" / name, ws / "runtime" / name)
    shutil.copy2(ROOT / "scripts" / "session_start.py", ws / "scripts" / "session_start.py")
    return ws


def _run(py_path: str, ws: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(py_path)],
        cwd=str(ws),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


class TestSectionOrderContract(unittest.TestCase):
    def test_compile_section_order_contract(self):
        """WORLD < RECENT EXPERIMENTS < HYPOTHESES < RESUME < ARTIFACTS."""
        with tempfile.TemporaryDirectory(prefix="xx_p20o_") as tmp:
            ws = _build_ws(Path(tmp))
            proc = _run(str(ws / "runtime" / "project.py"), ws)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = proc.stdout
            markers = (
                "[WORLD MODEL]",
                "[RECENT EXPERIMENTS]",
                "HYPOTHESES",
                "[RESUME]",
                "[ARTIFACTS]",
            )
            idx = [out.index(m) for m in markers]
            self.assertEqual(idx, sorted(idx), f"section order broken: {idx}")


if __name__ == "__main__":
    unittest.main()
