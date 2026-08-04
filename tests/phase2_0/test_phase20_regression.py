#!/usr/bin/env python3
"""Phase 2.0 regression (single world projection source) — post-fix.

Covers:
- compile() section order contract (WORLD → RECENT → OPEN/STALLED → RESUME → ARTIFACTS)
- assembler real launch in isolated workspace (context/ + runtime/ + CLAUDE.md + fixtures)
- assembler world section == project world section (same renderer source)
- no second world parser / hardcoded memory path in assembler
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
    (ws / "context").mkdir(parents=True)
    (ws / "cognition").mkdir(parents=True)
    exp_dst = ws / "results" / "phase0.example.test" / "experiments"
    shutil.copytree(FIX / "experiments", exp_dst)
    (ws / ".claude" / "memory.md").write_text(
        (FIX / "memory.md").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    (ws / ".observe_state.json").write_text(
        (FIX / "observe_state.json").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    for name in COGNITION:
        shutil.copy2(FIX / "cognition" / name, ws / "cognition" / name)
    for name in RUNTIME_FILES:
        shutil.copy2(ROOT / "runtime" / name, ws / "runtime" / name)
    shutil.copy2(ROOT / "scripts" / "session_start.py", ws / "scripts" / "session_start.py")
    shutil.copy2(ROOT / "context" / "assembler.py", ws / "context" / "assembler.py")
    shutil.copy2(ROOT / "CLAUDE.md", ws / "CLAUDE.md")
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


class TestPhase2_0(unittest.TestCase):
    def test_single_world_source(self):
        src = (ROOT / "context" / "assembler.py").read_text(encoding="utf-8")
        self.assertNotIn("extract_world_model", src)
        self.assertNotIn("read_memory", src)  # 不再硬编码
        self.assertNotIn("render_world", src)
        self.assertIn("world_active", src)
        self.assertIn("world_parse", src)
        self.assertIn("format_world_section", src)

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

    def test_assembler_real_launch_and_world_same_source(self):
        with tempfile.TemporaryDirectory(prefix="xx_p20r_") as tmp:
            ws = _build_ws(Path(tmp))
            # compile world projection (source of truth)
            sys.path.insert(0, str(ws / "runtime"))
            from world import parse as world_parse  # type: ignore  # noqa: E402
            from project import format_world_section, format_unknown_sections  # type: ignore  # noqa: E402

            lines = (ws / ".claude" / "memory.md").read_text(encoding="utf-8").splitlines()
            view = world_parse(lines)
            unk = format_unknown_sections(view)
            expected_world = format_world_section(view) + (unk if unk else "")

            # real PostTool launch
            proc = _run(str(ws / "context" / "assembler.py"), ws)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = proc.stdout
            self.assertIn("phase0.example.test", out)
            self.assertIn("[WORLD MODEL]", out)
            self.assertIn("HYPOTHESES", out)
            self.assertIn("[RECENT OBSERVATION]", out)
            self.assertIn("[CONTRACT]", out)

            # assembler world section == project world section (same renderer source)
            got_world = out[: out.index("[RECENT OBSERVATION]")].rstrip("\n")
            exp_world = expected_world.rstrip("\n")
            self.assertEqual(got_world, exp_world)


if __name__ == "__main__":
    unittest.main()
