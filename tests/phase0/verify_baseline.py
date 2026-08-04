#!/usr/bin/env python3
"""Re-run capture inputs against current scripts; compare to frozen fixture (Phase 0)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixture"
COGNITION_MODULES = (
    "object_model.md",
    "evidence_model.md",
    "experiment_model.md",
    "surface_misalignment.md",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    manifest = json.loads((FIX / "INPUT_MANIFEST.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="xx_phase0_v_") as tmp:
        ws = Path(tmp) / "w"
        (ws / ".claude").mkdir(parents=True)
        (ws / "scripts").mkdir(parents=True)
        (ws / "runtime").mkdir(parents=True)
        (ws / "cognition").mkdir(parents=True)
        exp = ws / "results" / "phase0.example.test" / "experiments"
        shutil.copytree(FIX / "experiments", exp)
        (ws / ".claude" / "memory.md").write_text(
            (FIX / "memory.md").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
        (ws / ".observe_state.json").write_text(
            (FIX / "observe_state.json").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
        for name in COGNITION_MODULES:
            shutil.copy2(FIX / "cognition" / name, ws / "cognition" / name)
        shutil.copy2(ROOT / "scripts" / "session_start.py", ws / "scripts" / "session_start.py")
        # Adapter needs full projection stack (Phase 1.4): paths/world/project/merge
        for name in ("paths.py", "world.py", "project.py", "merge.py"):
            shutil.copy2(ROOT / "runtime" / name, ws / "runtime" / name)

        out = subprocess.run(
            [sys.executable, str(ws / "scripts" / "session_start.py")],
            cwd=str(ws),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert out.returncode == 0, out.stderr
        got = out.stdout
        exp_stdout = (FIX / "expected_stdout.txt").read_text(encoding="utf-8")
        if got != exp_stdout:
            print("FAIL: stdout mismatch")
            print("got sha", sha256(got.encode("utf-8")))
            print("exp sha", manifest["expected_stdout_sha256"])
            sys.exit(1)
        if sha256(got.encode("utf-8")) != manifest["expected_stdout_sha256"]:
            print("FAIL: stdout sha vs manifest")
            sys.exit(1)

        delta = (FIX / "merge_delta.txt").read_text(encoding="utf-8")
        m = subprocess.run(
            [sys.executable, str(ws / "runtime" / "merge.py")],
            cwd=str(ws),
            input=delta,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert m.returncode == 0, m.stderr + m.stdout
        after = (ws / ".claude" / "memory.md").read_text(encoding="utf-8")
        exp_after = (FIX / "merge_memory_after.md").read_text(encoding="utf-8")
        if after != exp_after:
            print("FAIL: merge after mismatch")
            sys.exit(1)
        if sha256(after.encode("utf-8")) != manifest["merge_memory_after_sha256"]:
            print("FAIL: merge after sha vs manifest")
            sys.exit(1)

    print("Phase 0 verify OK")
    print("stdout_sha256", manifest["expected_stdout_sha256"])
    print("merge_after_sha256", manifest["merge_memory_after_sha256"])


if __name__ == "__main__":
    main()
