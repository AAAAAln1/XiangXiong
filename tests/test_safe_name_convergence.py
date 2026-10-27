#!/usr/bin/env python3
"""P0-0 收敛锁：target→目录转换全仓只有 paths.safe_name 一个实现。

防的事故：8 处历史副本漂移（EVIDENCE_BLOCK_RE / normalize_status 已两次因此翻车）。
"""

import inspect
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))

import paths
import merge
import deepdive
import diff_scan
import execute
import experiment
import http_request
import intents
import protocol_scan
import project


CASES = [
    ("10.0.0.1", "10.0.0.1"),
    # 三连 replace：: → _，两个 / → _，故 :// 变成 ___
    ("https://a.b.com/path", "https___a.b.com_path"),
    ("10.0.0.1:8443", "10.0.0.1_8443"),
    ("a\\b\\c", "a_b_c"),
    ("intranet-acme-2026q4", "intranet-acme-2026q4"),
]


def test_safe_name_basic():
    for raw, expected in CASES:
        assert paths.safe_name(raw) == expected


def test_seven_modules_delegate_to_same_impl():
    """各模块的 safe_target 必须就是 paths.safe_name（别名导入），不得是本地重写。"""
    assert deepdive.safe_target is paths.safe_name
    assert diff_scan.safe_target is paths.safe_name
    assert execute.safe_target is paths.safe_name
    assert experiment.safe_target is paths.safe_name
    assert http_request.safe_target is paths.safe_name
    assert intents.safe_target is paths.safe_name
    assert protocol_scan.safe_target is paths.safe_name


def test_project_wrapper_and_merge_wrapper_equivalent():
    for raw, expected in CASES:
        assert project._target_safe_name(raw) == expected
    # merge.target_safe_name 吃 body（先取 identity）
    assert merge.target_safe_name("https://a.b.com/x y") == "a.b.com"
    assert merge.target_safe_name("10.0.0.1:80 label") == "10.0.0.1_80"


def test_no_replace_copy_remains():
    """除 paths.safe_name 本体与其显式包装（merge.target_safe_name/project._target_safe_name）外，
    任何函数源码不得再含三连 replace 转换。别名 import 的 getsource 会穿透到 paths 本体，
    故先以函数对象同一性排除。"""
    wrappers = {merge.target_safe_name, project._target_safe_name, paths.safe_name}
    offenders = []
    for mod in (deepdive, diff_scan, execute, experiment, http_request,
                intents, protocol_scan, project, merge):
        for _name, fn in inspect.getmembers(mod, inspect.isfunction):
            if fn in wrappers:
                continue
            src = inspect.getsource(fn)
            if '.replace(":", "_")' in src and '.replace("/", "_")' in src:
                offenders.append(f"{mod.__name__}.{_name}")
    assert offenders == [], f"发现 safe_target 副本: {offenders}"
