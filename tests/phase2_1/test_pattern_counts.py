#!/usr/bin/env python3
"""B-variant: observation repetition counter (pure facts, no interpretation).

Covers:
- normalized_host_path strips query and normalizes trailing slash
- normalized_body_hash is deterministic and ignores whitespace
- record_pattern increments on same (request_sig, response_sig)
- different body → different key (conservative; no business extraction)
- cache is bounded
- format_output emits [OBSERVATION REPETITION] only when occurrences >= 2
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime"))

import http_request  # type: ignore  # noqa: E402


class TestSignature(unittest.TestCase):
    def test_normalized_host_path_strips_query(self):
        self.assertEqual(
            http_request.normalized_host_path("https://t.example/app/center/list?callback=cb&a=1"),
            ("t.example", "/app/center/list"),
        )

    def test_normalized_host_path_trailing_slash(self):
        self.assertEqual(
            http_request.normalized_host_path("https://t.example/app/center/list/"),
            ("t.example", "/app/center/list"),
        )

    def test_body_hash_deterministic_and_whitespace_agnostic(self):
        a = http_request.normalized_body_hash('{"a": 1}\n{"b": 2}')
        b = http_request.normalized_body_hash('{"a": 1}{"b": 2}')
        c = http_request.normalized_body_hash('{"a": 1}{"b": 2}')
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_different_body_different_hash(self):
        self.assertNotEqual(
            http_request.normalized_body_hash('{"errno": 1004}'),
            http_request.normalized_body_hash('{"errno": 200}'),
        )


class TestPatternCounts(unittest.TestCase):
    def _tmp_target(self):
        tmp = tempfile.mkdtemp(prefix="xx_p21_")
        old = http_request.RESULTS_DIR
        http_request.RESULTS_DIR = tmp
        return tmp, old

    def test_increments_on_same_signature(self):
        _, old = self._tmp_target()
        try:
            k1, n1 = http_request.record_pattern("t.example", "GET",
                                                  "https://t.example/api?x=1", "200", '{"a":1}')
            k2, n2 = http_request.record_pattern("t.example", "GET",
                                                  "https://t.example/api?x=2", "200", '{"a":1}')
            self.assertEqual(k1, k2)
            self.assertEqual(n1, 1)
            self.assertEqual(n2, 2)
        finally:
            http_request.RESULTS_DIR = old

    def test_different_body_separate_key(self):
        _, old = self._tmp_target()
        try:
            k1, _ = http_request.record_pattern("t.example", "GET",
                                                "https://t.example/api", "200", '{"errno":1004}')
            k2, _ = http_request.record_pattern("t.example", "GET",
                                                "https://t.example/api", "200", '{"errno":200}')
            self.assertNotEqual(k1, k2)
        finally:
            http_request.RESULTS_DIR = old

    def test_status_000_not_counted(self):
        _, old = self._tmp_target()
        try:
            k, n = http_request.record_pattern("t.example", "GET",
                                               "https://t.example/api", "000", "")
            self.assertEqual((k, n), ("", 0))
        finally:
            http_request.RESULTS_DIR = old

    def test_bounded(self):
        _, old = self._tmp_target()
        try:
            prev = http_request.PATTERN_COUNTS_MAX_KEYS
            http_request.PATTERN_COUNTS_MAX_KEYS = 5
            for i in range(20):
                http_request.record_pattern("t.example", "GET",
                                            f"https://t.example/p{i}", "200", f"b{i}")
            counts = http_request.load_pattern_counts("t.example")
            self.assertLessEqual(len(counts), 5)
            http_request.PATTERN_COUNTS_MAX_KEYS = prev
        finally:
            http_request.RESULTS_DIR = old

    def test_discardable_rebuildable(self):
        _, old = self._tmp_target()
        try:
            http_request.record_pattern("t.example", "GET", "https://t.example/a", "200", "x")
            path = http_request.pattern_counts_path("t.example")
            self.assertTrue(os.path.exists(path))
            os.remove(path)  # discardable: runtime continues
            _, n = http_request.record_pattern("t.example", "GET", "https://t.example/a", "200", "x")
            self.assertEqual(n, 1)  # rebuildable from future requests
        finally:
            http_request.RESULTS_DIR = old

    def test_env_switch_disables(self):
        _, old = self._tmp_target()
        env_old = os.environ.get("XIANGXIONG_OBS_REPEAT")
        try:
            os.environ["XIANGXIONG_OBS_REPEAT"] = "0"
            k, n = http_request.record_pattern("t.example", "GET", "https://t.example/a", "200", "x")
            self.assertEqual((k, n), ("", 0))
            self.assertFalse(os.path.exists(http_request.pattern_counts_path("t.example")))
        finally:
            http_request.RESULTS_DIR = old
            if env_old is None:
                os.environ.pop("XIANGXIONG_OBS_REPEAT", None)
            else:
                os.environ["XIANGXIONG_OBS_REPEAT"] = env_old


class TestOutput(unittest.TestCase):
    def test_repetition_line_only_when_ge2(self):
        base = {
            "return_code": 0, "status_code": "200", "method": "GET",
            "url": "https://t.example/api", "body_length": "10", "duration": 0.1,
            "filename": "req_001.txt", "body": '{"a":1}',
            "error": None, "association_error": None, "truncated": False,
        }
        one = http_request.format_output(dict(base, pattern_key="GET:t.example/api:200:abc", pattern_occurrences=1))
        self.assertNotIn("OBSERVATION REPETITION", one)
        many = http_request.format_output(dict(base, pattern_key="GET:t.example/api:200:abc", pattern_occurrences=8))
        self.assertIn("[OBSERVATION REPETITION] key=GET:t.example/api:200:abc occurrences=8", many)


if __name__ == "__main__":
    unittest.main()
