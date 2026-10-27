#!/usr/bin/env python3
"""P0-a：credentials 库 + merge 凭据闸。"""

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))

import credentials as creds
import merge


SECRET_LINES = [
    "cred: employee:employee123 @10.0.0.0/24",
    "cred: admin:Admin@2026 来源=后台",
    "cred: svc:31d6cfe0d16ae931b73c59d7e0c089c0",
    "cred: aaa:31d6cfe0d16ae931b73c59d7e0c089c0:31d6cfe0d16ae931b73c59d7e0c089c0",
    "cred: token eyJhbGciOiJIUzI1NiJ9.eyJhIjox.xx-yy_zz",
    "cred: key -----BEGIN RSA PRIVATE KEY-----",
    # 带 @ 的真密码：@ 后无点域名，不被邮箱豁免放过
    "cred: a:p@ss.word123 强口令样例",
    # 引用形态行里粘贴哈希仍算秘密（值不能进账本，哪怕带了 c-id）
    "cred: c-001 31d6cfe0d16ae931b73c59d7e0c089c0 哈希泄露",
]
SAFE_LINES = [
    "cred: c-001 svc_backup 域用户 @10.0.0.0/24 来源=10.0.0.178 a-08 (exp-012)",
    "cred: c-002 员工测试账号 来源=a-04 (art-003)",
    "cred: 仅有身份描述无值的记录 待获取",
    # 2026-10-06 回归：账本前缀自身不得被当成 user:secret 的左值
    "cred: admin@corp.local",
    "cred: user@corp.local 来源 a-01",
    "cred: svc_x @10.0.0.0/24 来源=x (exp-001)",
]


def test_email_shape_not_secret():
    """邮箱（含 note 文本里的）不是秘密；Admin@2026 这类无点域名的带@密码仍是。"""
    assert not creds.looks_like_secret("note: admin@corp.local 联系邮箱")
    assert creds.looks_like_secret("cred: admin:Admin@2026")


def test_secret_detection():
    for line in SECRET_LINES:
        assert creds.looks_like_secret(line), line


def test_safe_lines_not_secret():
    for line in SAFE_LINES:
        assert not creds.looks_like_secret(line), line


def test_cred_gate_reject_and_accept():
    for line in SECRET_LINES:
        ok, reason = merge.cred_gate(line)
        assert not ok and "credentials.py put" in reason
    for line in SAFE_LINES:
        ok, _ = merge.cred_gate(line)
        assert ok


def test_non_cred_lines_unaffected():
    ok, _ = merge.cred_gate("surface: /api/login 200 (req_001)")
    assert ok


def test_put_list_path_dedup_and_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "RESULTS_DIR", str(tmp_path))
    vf = tmp_path / "v.txt"
    vf.write_text("employee123", encoding="utf-8")
    r1 = creds.put_credential("eng-1", str(vf), "pw", "员工账号", consume=False)
    assert r1["id"] == "c-001" and not r1["dedup"]
    # 同值去重
    vf2 = tmp_path / "v2.txt"
    vf2.write_text("employee123", encoding="utf-8")
    r2 = creds.put_credential("eng-1", str(vf2), "pw", "同值", consume=False)
    assert r2["id"] == "c-001" and r2["dedup"]
    # list 不含值
    rows = creds.list_credentials("eng-1")
    assert len(rows) == 1 and all("employee123" not in json.dumps(r, ensure_ascii=False) for r in rows)
    # path 指向真实文件
    p = creds.credential_path("eng-1", "c-001")
    assert open(p, encoding="utf-8").read() == "employee123"
    # 非法 id / 不存在 id
    with pytest.raises(ValueError):
        creds.credential_path("eng-1", "c001")
    with pytest.raises(KeyError):
        creds.credential_path("eng-1", "c-999")


def test_note_with_secret_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "RESULTS_DIR", str(tmp_path))
    vf = tmp_path / "v.txt"
    vf.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        creds.put_credential("eng-1", str(vf), "pw", "口令 admin:Admin@2026", False)


def test_concurrent_put_no_id_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "RESULTS_DIR", str(tmp_path))
    errors = []
    ids = []
    lock = threading.Lock()

    def worker(i):
        try:
            vf = tmp_path / f"v{i}.txt"
            vf.write_text(f"secret-value-{i:03d}", encoding="utf-8")
            r = creds.put_credential("eng-c", str(vf), "pw", f"n{i}", False)
            with lock:
                ids.append(r["id"])
        except Exception as e:  # noqa
            with lock:
                errors.append(repr(e))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert len(ids) == 12 and len(set(ids)) == 12
    index = json.load(open(creds.index_path("eng-c"), encoding="utf-8"))
    assert len(index) == 12


def test_add_line_rejects_secret_cred():
    lines = ["target: example.com\n"]
    out = merge.add_line(lines, "cred: admin:Admin@2026 来源=x")
    assert out == lines  # 未写入
    out2 = merge.add_line(lines, "cred: c-001 管理员账号 来源=x")
    assert any(l.startswith("cred:") for l in out2)
