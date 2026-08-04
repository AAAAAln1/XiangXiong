#!/usr/bin/env python3
"""
Phase 0 baseline capture only (H0-H15).

Does NOT modify session_start / merge logic.
Does NOT create world.py / project.py.
Captures: fixture tree, INPUT_MANIFEST, expected_stdout, merge before/after.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "fixture"
COGNITION_MODULES = (
    "object_model.md",
    "evidence_model.md",
    "experiment_model.md",
    "surface_misalignment.md",
)

MEMORY_FIXTURE = """\
target: phase0.example.test
tech: nginx
port: 443 https
artifact: results/phase0.example.test/responses/req_001.txt
surface: /login form present
surface: /api/v1/search endpoint
cred: demo cookie session=phase0
vuln: reflected xss on /legacy (demo fixture only)
hypothesis: search q parameter may reflect unsanitized
hypothesis [observed]: admin panel may exist at /admin
hypothesis [blocked]: cannot verify auth bypass without credentials
hypothesis [confirmed]: /health returns 200 without auth
hypothesis [contradicted]: /debug was not open on first probe
counter: /old-admin returned 404 repeatedly
dead: /phpmyadmin not reachable
future: later recheck rate limits after creds
"""

OBSERVE_FIXTURE = {
    "version": 3,
    "consecutive_none": 6,
    "last_round": 12,
    "memory_offset": 0,
    "prefix_counts": {"surface": 2, "hypothesis": 4, "vuln": 1},
    "actions": [
        {"fp": "HTTP:GET:phase0.example.test/api", "raw": "python runtime/http_request.py ..."},
        {"fp": "HTTP:GET:phase0.example.test/login", "raw": "python runtime/http_request.py ..."},
        {"fp": "HTTP:GET:phase0.example.test/login", "raw": "python runtime/http_request.py ..."},
        {"fp": "HTTP:GET:phase0.example.test/login", "raw": "python runtime/http_request.py ..."},
        {"fp": "HTTP:GET:phase0.example.test/login", "raw": "python runtime/http_request.py ..."},
    ],
}

EXPERIMENT_JSON = {
    "experiment_id": "exp-001",
    "created_at": "2026-01-01T00:00:00+00:00",
    "hypothesis": "search q may reflect",
    "action": "probe search with marker",
    "closed": True,
    "status": "failed",
    "conclusion": "no reflection in truncated body",
    "observations": [
        {
            "observation": "status 200 length stable",
            "artifact": "responses/req_001.txt",
        }
    ],
}

DELTA_TEXT = """\
DELTA:
surface: /phase0-merge-probe exists
hypothesis: phase0 merge baseline line
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def build_workspace(ws: Path) -> None:
    if ws.exists():
        shutil.rmtree(ws)
    (ws / ".claude").mkdir(parents=True)
    (ws / "scripts").mkdir(parents=True)
    (ws / "runtime").mkdir(parents=True)
    (ws / "cognition").mkdir(parents=True)
    exp_dir = ws / "results" / "phase0.example.test" / "experiments" / "exp-001"
    exp_dir.mkdir(parents=True)

    write_text(ws / ".claude" / "memory.md", MEMORY_FIXTURE)
    write_text(ws / ".observe_state.json", json.dumps(OBSERVE_FIXTURE, ensure_ascii=False, indent=2) + "\n")
    write_text(exp_dir / "experiment.json", json.dumps(EXPERIMENT_JSON, ensure_ascii=False, indent=2) + "\n")
    # report absent, lessons absent — do not create

    for name in COGNITION_MODULES:
        src = ROOT / "cognition" / name
        shutil.copy2(src, ws / "cognition" / name)

    # Point session_start / merge at this workspace by copying scripts with same relative layout
    shutil.copy2(ROOT / "scripts" / "session_start.py", ws / "scripts" / "session_start.py")
    shutil.copy2(ROOT / "runtime" / "merge.py", ws / "runtime" / "merge.py")
    # merge may import nothing else; session_start is standalone


def capture_stdout(ws: Path) -> str:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(ws / "scripts" / "session_start.py")],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"session_start failed rc={proc.returncode}\n{proc.stderr}")
    return proc.stdout


def capture_merge(ws: Path) -> tuple[str, str, str]:
    before = (ws / ".claude" / "memory.md").read_text(encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(ws / "runtime" / "merge.py")],
        cwd=str(ws),
        env=env,
        input=DELTA_TEXT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    after = (ws / ".claude" / "memory.md").read_text(encoding="utf-8")
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit(f"merge failed rc={proc.returncode}\n{log}")
    return before, after, log


def export_fixture(ws: Path, stdout: str, merge_before: str, merge_after: str, merge_log: str) -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # Input copies
    write_text(OUT / "memory.md", (ws / ".claude" / "memory.md").read_text(encoding="utf-8"))
    # Restore pre-merge memory into fixture input (merge after is separate)
    write_text(OUT / "memory.md", merge_before)
    write_text(OUT / "observe_state.json", (ws / ".observe_state.json").read_text(encoding="utf-8"))
    cog_out = OUT / "cognition"
    cog_out.mkdir()
    cognition_sha = {}
    for name in COGNITION_MODULES:
        data = (ws / "cognition" / name).read_bytes()
        (cog_out / name).write_bytes(data)
        cognition_sha[name] = sha256_bytes(data)

    exp_src = ws / "results" / "phase0.example.test" / "experiments"
    shutil.copytree(exp_src, OUT / "experiments")

    write_text(OUT / "expected_stdout.txt", stdout)
    write_text(OUT / "merge_delta.txt", DELTA_TEXT)
    write_text(OUT / "merge_memory_before.md", merge_before)
    write_text(OUT / "merge_memory_after.md", merge_after)
    write_text(OUT / "merge_run_log.txt", merge_log)

    # Section slices for H0 gold (COGNITION / ANTI-COMPRESSION / WORLD / RESUME)
    # WORLD gold = full attention block through unknowns (ends at RESUME),
    # matching Phase0 four-region freeze (COGNITION / ANTI-COMPRESSION / WORLD / RESUME).
    sections = {
        "COGNITION": _slice_section(stdout, "[COGNITION]", "[ANTI-COMPRESSION]"),
        "ANTI-COMPRESSION": _slice_section(stdout, "[ANTI-COMPRESSION]", "[OBSERVE]"),
        "WORLD": _slice_section(stdout, "[WORLD MODEL]", "[RESUME]"),
        "RESUME": _slice_section(stdout, "[RESUME]", "[ARTIFACTS]"),
        "OBSERVE": _slice_section(stdout, "[OBSERVE]", "[WORLD MODEL]"),
        "ARTIFACTS": stdout[stdout.index("[ARTIFACTS]"):] if "[ARTIFACTS]" in stdout else "",
    }
    sec_dir = OUT / "sections"
    sec_dir.mkdir()
    section_sha = {}
    for key, body in sections.items():
        p = sec_dir / f"{key}.txt"
        write_text(p, body)
        section_sha[key] = sha256_bytes(body.encode("utf-8"))

    exp_fp = sha256_bytes(
        json.dumps(EXPERIMENT_JSON, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )

    manifest = {
        "phase": 0,
        "protocol": "H0-H15",
        "purpose": "migration baseline only; no runtime abstraction",
        "captured_by": "tests/phase0/capture_baseline.py",
        "workspace_layout_note": (
            "session_start and merge resolved paths relative to their script parents; "
            "capture ran in an isolated tree mirroring xiangxiong layout"
        ),
        "memory_sha256": sha256_bytes(merge_before.encode("utf-8")),
        "observe_state_sha256": sha256_file(OUT / "observe_state.json"),
        "cognition_files": list(COGNITION_MODULES),
        "cognition_sha256": cognition_sha,
        "experiments_fingerprint": exp_fp,
        "artifacts": {"report.md": False, "lessons.md": False},
        "expected_stdout_sha256": sha256_bytes(stdout.encode("utf-8")),
        "section_sha256": section_sha,
        "merge_memory_before_sha256": sha256_bytes(merge_before.encode("utf-8")),
        "merge_memory_after_sha256": sha256_bytes(merge_after.encode("utf-8")),
        "merge_delta_sha256": sha256_bytes(DELTA_TEXT.encode("utf-8")),
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    write_text(OUT / "INPUT_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    # README for humans — protocol says no proactive docs; minimal pointer only
    write_text(
        OUT / "PHASE0_README.txt",
        "Phase 0 baseline fixture. Do not optimize. Phase 1 must match expected_stdout "
        "and merge_memory_after given the same inputs (see INPUT_MANIFEST.json).\n",
    )


def _slice_section(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    i = text.index(start)
    j = text.index(end, i) if end in text[i:] else len(text)
    return text[i:j]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="xx_phase0_") as tmp:
        ws = Path(tmp) / "xiangxiong_like"
        build_workspace(ws)
        # Capture stdout BEFORE merge mutates memory (projection baseline uses merge_before)
        stdout = capture_stdout(ws)
        merge_before, merge_after, merge_log = capture_merge(ws)
        # Re-capture would differ after merge; keep stdout from pre-merge world
        export_fixture(ws, stdout, merge_before, merge_after, merge_log)
    print(f"Phase 0 fixture written to: {OUT}")
    print(f"expected_stdout_sha256: {sha256_file(OUT / 'expected_stdout.txt')}")


if __name__ == "__main__":
    main()
