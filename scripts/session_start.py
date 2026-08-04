#!/usr/bin/env python3
"""
session_start.py — SessionStart hook Adapter (Phase 1.4).

Thin host adapter only:
  hook → runtime.project.compile() → stdout

No independent parse/render. Projection semantics live only in
runtime/project.compile() (H13). Does not interpret the world.
"""

from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def main() -> None:
    try:
        from runtime.project import compile as project_compile
    except ImportError:
        runtime_dir = os.path.join(_PROJECT_ROOT, "runtime")
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        from project import compile as project_compile  # type: ignore

    try:
        sys.stdout.write(project_compile())
    except Exception as e:
        print(f"[session_start] ERROR: {str(e)[:200]}")
    sys.exit(0)


if __name__ == "__main__":
    main()
