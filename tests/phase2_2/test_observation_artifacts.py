#!/usr/bin/env python3
"""Observation Artifact Contract: path/size/truncated 一阶暴露（物理 only）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime"))

import execute  # type: ignore  # noqa: E402
import http_request  # type: ignore  # noqa: E402

FORBIDDEN_SUBSTR = (
    "conclusion",
    "hypothesis",
    "next_action",
    "should_read",
    "important",
)


class TestExecuteObservationArtifacts(unittest.TestCase):
    def setUp(self):
        self._old_results = execute.RESULTS_DIR
        self._tmp = tempfile.mkdtemp(prefix="xx_p22_exec_")
        execute.RESULTS_DIR = self._tmp

    def tearDown(self):
        execute.RESULTS_DIR = self._old_results

    def _args(self, cmd: str):
        return SimpleNamespace(
            target="t.example",
            name="experiment",
            experiment_id=None,
            purpose="unit",
            timeout=30,
            command=cmd,
        )

    def test_short_stdout_fields(self):
        body = "hello-short"
        result = execute.build_result(self._args(f'python -c "print({body!r}, end=\\"\\")"'))
        self.assertIn("metadata", result)
        self.assertEqual(result["stdout"], body)
        # 一阶：不读 metadata 也有 stdout 文本；全文路径在 metadata.json 的 artifacts
        meta_path = Path(self._tmp) / "t.example" / result["metadata"].replace("/", os.sep)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["artifacts"]["stdout"].split("/")[-1], f"{result['execution_id']}.stdout.txt")
        full_path = Path(self._tmp) / "t.example" / meta["artifacts"]["stdout"].replace("/", os.sep)
        self.assertEqual(full_path.read_text(encoding="utf-8"), body)
        self.assertTrue(full_path.is_file())
        blob = json.dumps(result, ensure_ascii=False)
        for bad in FORBIDDEN_SUBSTR:
            self.assertNotIn(bad, blob)

    def test_long_stdout_truncated_and_artifact(self):
        n = execute.MAX_OUTPUT_CHARS + 500
        # Avoid shell quoting hell: write via python -c with size only
        cmd = (
            f'python -c "import sys; sys.stdout.write(\'x\'*{n})"'
        )
        result = execute.build_result(self._args(cmd))
        # 2026-10-06 P1-a：裁剪改 head/tail（渗透结论常在尾部）+ 全文路径
        self.assertIn("头 2048 + 尾 2048", result["stdout"])
        self.assertIn(str(n), result["stdout"])  # 总长度披露
        self.assertIn(".stdout.txt", result["stdout"])  # 全文路径在回显内
        self.assertLessEqual(len(result["stdout"]), execute.MAX_OUTPUT_CHARS + 200)
        meta_path = Path(self._tmp) / "t.example" / result["metadata"].replace("/", os.sep)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        full_path = Path(self._tmp) / "t.example" / meta["artifacts"]["stdout"].replace("/", os.sep)
        self.assertEqual(len(full_path.read_text(encoding="utf-8")), n)  # 全文未截断
        # 一阶：不读 metadata 也有 truncation 提示；全文路径在 metadata
        self.assertTrue(meta["artifacts"]["stdout"].endswith(".stdout.txt"))
        self.assertNotEqual(meta["artifacts"]["stdout"], result["metadata"])


class TestHttpObservationArtifacts(unittest.TestCase):
    def test_format_always_emits_triple(self):
        base = {
            "return_code": 0,
            "status_code": "200",
            "method": "GET",
            "url": "https://t.example/",
            "body_length": "100",
            "duration": 0.1,
            "filename": "req_001.txt",
            "body": "y" * 50,
            "error": None,
            "association_error": None,
            "truncated": False,
        }
        text = http_request.format_output(base)
        self.assertIn("saved: responses/req_001.txt", text)
        self.assertIn("length: 100 bytes", text)
        self.assertNotIn("body_truncated", text)
        self.assertNotIn("(截断到", text)

    def test_format_truncated_true(self):
        base = {
            "return_code": 0,
            "status_code": "200",
            "method": "GET",
            "url": "https://t.example/",
            "body_length": "9999",
            "duration": 0.1,
            "filename": "req_002.txt",
            "body": "z" * http_request.MAX_BODY_CHARS,
            "error": None,
            "association_error": None,
            "truncated": True,
        }
        text = http_request.format_output(base)
        self.assertIn("saved: responses/req_002.txt", text)
        self.assertIn("(截断到 2048 字符", text)
        self.assertIn("length: 9999 bytes", text)
        for bad in FORBIDDEN_SUBSTR:
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
