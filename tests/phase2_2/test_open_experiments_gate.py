#!/usr/bin/env python3
"""OPEN EXPERIMENTS 闸门：只列未闭合实验。

判据是 `experiment.json` 的 `closed` 字段，**不是** `conclusion.md` 文件存在性。

回归背景：旧判据为「有 experiment.json 且无 conclusion.md」，但 `experiment.py close`
只写 `evidence.md`、**从不写 `conclusion.md`** → 判据恒真 → 实测 129 个实验里
127 个已关闭实验被投影标成"未闭合"。那是 Runtime 制造的、关于自己探索史的假信号。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime"))

import project  # type: ignore  # noqa: E402

TARGET = "gate.example.test"


def _write_experiment(root: Path, exp_id: str, closed: bool) -> Path:
    exp_dir = root / TARGET / "experiments" / exp_id
    exp_dir.mkdir(parents=True)
    (exp_dir / "experiment.json").write_text(
        json.dumps(
            {
                "experiment_id": exp_id,
                "hypothesis": f"h-{exp_id}",
                "closed": closed,
                "status": "unknown",
                "observations": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return exp_dir


class TestOpenExperimentsGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="xx_open_gate_")
        self.root = Path(self._tmp.name)
        self._patch = mock.patch.object(
            project, "_results_dir", lambda: str(self.root)
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_closed_excluded_open_included(self):
        _write_experiment(self.root, "exp-001", closed=True)
        _write_experiment(self.root, "exp-002", closed=False)
        out = project._format_open_experiments(TARGET)
        self.assertNotIn("exp-001", out)
        self.assertIn("exp-002", out)
        self.assertIn("[OPEN EXPERIMENTS]", out)

    def test_all_closed_renders_empty(self):
        _write_experiment(self.root, "exp-001", closed=True)
        self.assertEqual(project._format_open_experiments(TARGET), "")

    def test_missing_conclusion_md_is_not_unclosed(self):
        """反例：没有 conclusion.md 不等于未闭合——唯一判据是 metadata['closed']。"""
        exp_dir = _write_experiment(self.root, "exp-003", closed=True)
        self.assertFalse((exp_dir / "conclusion.md").exists())
        self.assertEqual(project._format_open_experiments(TARGET), "")


if __name__ == "__main__":
    unittest.main()
