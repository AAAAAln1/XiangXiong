#!/usr/bin/env python3
"""
assembler.py — Phase 2.0 Host Event client (PostTool).

Was: Host + independent memory parse/render (second world mouth).
Now: thin adapter → Runtime world access + single world projection.

  world_active() → world.parse() → project section renderers
  (format_world_section / format_unknown_sections)

CONTRACT remains a host contract attachment (not World / not WorldView).
RECENT keeps PostTool window/display protocol (observation attachment).

Does not call project.compile() full text (not a Context Optimizer).
"""

from __future__ import annotations

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

CLAUDE_PATH = os.path.join(_PROJECT_ROOT, "CLAUDE.md")

# Host contract attachment only — not world projection (Phase 2.0).
COGNITION_CONTRACT_FILES = (
    "object_model.md",
    "evidence_model.md",
)

RECENT_EXPERIMENT_LIMIT = 5
MAX_CONTRACT_CHARS = 3000
MAX_OBSERVATION_CHARS = 200


def _import_runtime():
    try:
        from runtime.paths import world_active
        from runtime.world import parse as world_parse
        from runtime.project import (
            experiments_dir_for_target,
            format_unknown_sections,
            format_world_section,
        )
    except ImportError:
        runtime_dir = os.path.join(_PROJECT_ROOT, "runtime")
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        from paths import world_active  # type: ignore
        from world import parse as world_parse  # type: ignore
        from project import (  # type: ignore
            experiments_dir_for_target,
            format_unknown_sections,
            format_world_section,
        )
    return (
        world_active,
        world_parse,
        experiments_dir_for_target,
        format_world_section,
        format_unknown_sections,
    )


def load_recent_experiments_posttool(target: str, limit: int = RECENT_EXPERIMENT_LIMIT) -> list:
    """
    PostTool RECENT window (legacy assembler protocol).

    Uses shared experiments_dir_for_target (no third root).
    Selection: sorted exp-* names, last N — not SessionStart active/time sort.
    """
    import json

    (
        _wa,
        _wp,
        experiments_dir_for_target,
        _fws,
        _fus,
    ) = _import_runtime()

    root = experiments_dir_for_target(target)
    if not os.path.isdir(root):
        return []

    experiment_ids = sorted(
        name
        for name in os.listdir(root)
        if name.startswith("exp-") and os.path.isdir(os.path.join(root, name))
    )
    recent_ids = experiment_ids[-limit:]

    experiments = []
    for experiment_id in recent_ids:
        path = os.path.join(root, experiment_id, "experiment.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        experiments.append(_summarize_experiment_posttool(experiment_id, metadata))
    return experiments


def _summarize_experiment_posttool(experiment_id: str, metadata: dict) -> dict:
    summary = {
        "id": experiment_id,
        "created_at": metadata.get("created_at", ""),
        "hypothesis": metadata.get("hypothesis", ""),
        "status": metadata.get("status", "unknown"),
        "closed": metadata.get("closed", False),
        "last_observation": "",
        "conclusion": "",
    }
    observations = metadata.get("observations") or []
    if observations and isinstance(observations[-1], dict):
        obs_text = observations[-1].get("observation", "")
        summary["last_observation"] = obs_text[:MAX_OBSERVATION_CHARS]
    if metadata.get("conclusion"):
        summary["conclusion"] = metadata["conclusion"][:MAX_OBSERVATION_CHARS]
    return summary


def shorten(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "..."


def render_recent_posttool(experiments: list) -> str:
    """Legacy PostTool [RECENT OBSERVATION] display (not SessionStart RECENT EXPERIMENTS)."""
    if not experiments:
        return "[RECENT OBSERVATION]\n(none)\n"

    lines = ["[RECENT OBSERVATION]"]
    for exp in experiments:
        fields = []
        fields.append(f"id: {exp['id']}")
        fields.append(f"closed: {str(exp['closed']).lower()}")
        if exp["hypothesis"]:
            fields.append(f"hypothesis: {shorten(exp['hypothesis'])}")
        if exp["last_observation"]:
            fields.append(f"observation: {shorten(exp['last_observation'])}")
        if exp["conclusion"]:
            fields.append(f"conclusion: {shorten(exp['conclusion'])}")
        fields.append(f"status: {exp['status']}")
        lines.append("  " + " | ".join(fields))
    lines.append("")
    return "\n".join(lines)


def load_contract() -> str:
    """Host contract attachment only — not world.parse / not WorldView."""
    parts = []
    if os.path.exists(CLAUDE_PATH):
        with open(CLAUDE_PATH, "r", encoding="utf-8") as f:
            parts.append(f.read())

    cognition_dir = os.path.join(_PROJECT_ROOT, "cognition")
    for name in COGNITION_CONTRACT_FILES:
        path = os.path.join(cognition_dir, name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                parts.append(f"--- cognition/{name} ---\n{text}")
            except (OSError, UnicodeError):
                pass

    full = "\n\n".join(parts)
    if len(full) > MAX_CONTRACT_CHARS:
        full = full[:MAX_CONTRACT_CHARS] + "\n...[contract truncated]"
    return full


def render_contract() -> str:
    lines = ["[CONTRACT]"]
    lines.append(load_contract())
    lines.append("")
    return "\n".join(lines)


def assemble(target: str | None = None) -> str:
    """
    PostTool context assembly.

    World: Runtime section renderers only (format_world_section + format_unknown_sections).
    RECENT: PostTool protocol (shared experiment dir).
    CONTRACT: host attachment, isolated from world.
    """
    (
        world_active,
        world_parse,
        _exp_dir,
        format_world_section,
        format_unknown_sections,
    ) = _import_runtime()

    path = world_active()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = []
    view = world_parse(lines)
    # Single world projection source (same section renderers as SessionStart).
    unknowns = format_unknown_sections(view)
    world_text = format_world_section(view) + (unknowns if unknowns else "")

    resolved_target = target or view.target
    if not resolved_target:
        return "[ERROR] no target in active world and no target argument\n"

    recent = load_recent_experiments_posttool(resolved_target)

    parts = [
        world_text.rstrip("\n"),
        "",
        render_recent_posttool(recent).rstrip("\n"),
        "",
        render_contract().rstrip("\n"),
        "",
    ]
    _ = _exp_dir
    return "\n".join(parts) + "\n"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    target = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        print(assemble(target), end="")
    except Exception as e:
        print(f"[assembler] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
