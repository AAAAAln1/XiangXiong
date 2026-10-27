#!/usr/bin/env python3
"""credentials.py — 凭据库（值不进账本、不进投影）。

设计（2026-10-06 内网改造 P0-a）：
- 凭据值只落 results/<target>/credentials/c-NNN.txt（0600；results/ 本就不入库）；
- 账本 cred: 行只许写引用（cred: c-NNN <身份> @<范围> 来源=...），
  merge 的凭据闸会 REJECT 含明文/哈希/密钥形态的 cred 行；
- 投影只显示「N 组 + list 指针」，需要值时由 agent 显式 Read（读动作留宿主痕迹）。

零判断：只做编号、存取、列目录；不判凭据真伪、不选凭据、不参与推理。

用法：
  python runtime/credentials.py put <target> --from-file <值文件>
        [--kind pw|ntlm|hash|key|token] [--note "<无值的身份描述>"] [--consume]
  python runtime/credentials.py list <target>
  python runtime/credentials.py path <target> c-003
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    from .filelock import locked
    from .paths import safe_name
except ImportError:
    from filelock import locked
    from paths import safe_name

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(PROJECT_ROOT, "results")
CREDS_SUBDIR = "credentials"
INDEX_NAME = "index.json"
ID_LOCK_NAME = ".creds_lock"
VALID_KINDS = ("pw", "ntlm", "hash", "key", "token", "other")

CRED_ID_RE = re.compile(r"^c-\d{3,}$")

# ── 秘密形态检测（单一实现；merge 凭据闸 import 复用，禁止各写一份）───────────
# 高召回优先：宁可误拒让用户改引用写法，不可漏放进投影。
# 形态：连写 user:secret、32/64 hex、LM:NTLM、空 NTLM 前缀、JWT、PEM 私钥。
_HEX32 = r"[0-9a-fA-F]{32}"
_HEX64 = r"[0-9a-fA-F]{64}"
_JWT = r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
# user:secret：左值 user-ish，右值 ≥4 非空白且不含 @ 网段/引用标记
# 注意：账本前缀（cred:/surface:/tech: 等）带冒号，直接对整行跑会把**前缀自身**当成左值、
# 把后面的合法内容（admin@corp.local / svc_x）误判成秘密（2026-10-06 实测复现）。
# 故检测前必须剥账本前缀（strip_ledger_prefix）；本模块与 merge.cred_gate 共用它。
_USER_SECRET = re.compile(r"(?<![\w.-])[A-Za-z0-9_.\-$]{1,40}\s*[:：]\s*\S{4,}")

# 账本合法前缀（剥除用，与 merge.VALID_PREFIXES 保持同语义；含带状态的两种括号形态）
_LEDGER_PREFIX_RE = re.compile(
    r"^\s*(?:target|tech|port|surface|vuln|cred|hypothesis|counter|dead(?:\s*\[[^\]]*\])?"
    r"|dead-summary|future|goal|subgoal|artifact)\s*[:：]\s*"
)


def strip_ledger_prefix(text: str) -> str:
    """剥掉行首的账本前缀。`cred: admin@corp.local` → `admin@corp.local`。"""
    return _LEDGER_PREFIX_RE.sub("", text or "", count=1)

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"aad3b435b51404eeaad3b435b51404ee:" + _HEX32, re.IGNORECASE),
    re.compile(_HEX32 + r":" + _HEX32),
    re.compile(r"(?<![0-9a-fA-F])" + _HEX64 + r"(?![0-9a-fA-F])"),
    re.compile(r"(?<![0-9a-fA-F])" + _HEX32 + r"(?![0-9a-fA-F])"),
    re.compile(_JWT),
]

# 引用/非秘密形态的豁免（先于 user:secret 判定）：c-NNN、exp-/req-/art-、@网段、URL
_REF_PREFIXES = ("c-", "exp-", "req_", "art-")


def looks_like_secret(text: str) -> bool:
    """机械检测文本是否含秘密形态。只做形态匹配，不判语义。

    引用行（cred: c-001 身份 @网段 来源=... (exp-1)）须返回 False；
    含 employee:employee123 这类连写口令须返回 True。

    检测在**剥掉账本前缀之后**进行，否则 `cred:` 的冒号会被当成 user:secret
    的分隔符（left=cred），把 admin@corp.local / svc_x 这类合法内容误判（实测）。
    """
    if not text:
        return False
    body = strip_ledger_prefix(text)
    for pat in SECRET_PATTERNS:
        if pat.search(body):
            return True
    # user:secret 连写：逐个候选排除引用/自然语言形态
    for m in re.finditer(_USER_SECRET, body):
        token = m.group(0)
        parts = re.split(r"\s*[:：]\s*", token, maxsplit=1)
        left, right = parts[0], parts[1]
        left = left.strip().strip("()（），,;；")
        right = right.strip().rstrip("()（），,;；。")
        # 右值是引用/编号/网段/路径 → 非秘密
        if any(right.startswith(p) for p in _REF_PREFIXES):
            continue
        if right.startswith("@") or right.startswith(("http://", "https://", "/", "\\")):
            continue
        # 邮箱形态（@ 后有点分域名、TLD 纯字母）→ 非秘密。
        # Admin@2026 这类带 @ 的密码因 @ 后无点域名/TLD 含数字，不被豁免。
        if re.search(r"@[A-Za-z0-9_.\-]+\.[A-Za-z]{2,}$", right):
            continue
        # 左值为纯数字（端口 80:443 样式）→ 非秘密
        if left.isdigit():
            continue
        # 右值含空格 → 自然语言（"user: 域用户 来源..." 第一冒号后的描述）
        if " " in right or "　" in right:
            continue
        # 左值是中文（身份: 描述）→ 非秘密
        if re.search(r"[一-鿿]", left):
            continue
        # 右值不含任何 ASCII 字母数字（纯中文/标点描述）→ 自然语言，非秘密
        if not re.search(r"[A-Za-z0-9]", right):
            continue
        # 右值 ≥4 位且含字母数字混合或常见口令字符 → 判秘密
        if re.search(r"[A-Za-z]", right) and re.search(r"[0-9!@#$%^&*._\-]", right):
            return True
        if len(right) >= 8:
            return True
    return False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def creds_dir(target: str) -> str:
    return os.path.join(RESULTS_DIR, safe_name(target), CREDS_SUBDIR)


def index_path(target: str) -> str:
    return os.path.join(creds_dir(target), INDEX_NAME)


def _load_index(target: str) -> dict:
    p = index_path(target)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _next_cred_id(index: dict) -> str:
    highest = 0
    for name in index:
        m = re.match(r"^c-(\d+)$", name)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"c-{highest + 1:03d}"


def _write_index(target: str, index: dict) -> None:
    p = index_path(target)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def put_credential(target: str, value_file: str, kind: str, note: str, consume: bool) -> dict:
    if kind not in VALID_KINDS:
        raise ValueError(f"kind 必须是 {VALID_KINDS} 之一")
    if not os.path.isfile(value_file):
        raise FileNotFoundError(f"值文件不存在: {value_file}")
    with open(value_file, "r", encoding="utf-8", errors="replace") as f:
        value = f.read()
    if not value.strip():
        raise ValueError("凭据值为空，拒绝登记")
    note = (note or "").strip()
    if looks_like_secret(note):
        raise ValueError("--note 疑似含秘密值；note 只写身份/来源描述，值只能在文件里")

    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    d = creds_dir(target)
    with locked(os.path.join(d, ID_LOCK_NAME)):
        index = _load_index(target)
        # 同 sha256 去重：返回既有 id，不重复登记
        for cid, meta in index.items():
            if meta.get("sha256") == digest:
                return {"id": cid, "dedup": True}
        cid = _next_cred_id(index)
        value_path = os.path.join(d, cid + ".txt")
        os.makedirs(d, exist_ok=True)
        with open(value_path, "w", encoding="utf-8") as f:
            f.write(value)
        try:
            os.chmod(value_path, 0o600)
        except OSError:
            pass
        index[cid] = {
            "kind": kind,
            "note": note,
            "created": now_iso(),
            "file": cid + ".txt",
            "sha256": digest,
            "size": len(value.encode("utf-8")),
        }
        _write_index(target, index)
    if consume and os.path.isfile(value_file):
        try:
            os.remove(value_file)
        except OSError:
            pass
    return {"id": cid, "dedup": False}


def list_credentials(target: str) -> list:
    out = []
    for cid, meta in sorted(_load_index(target).items()):
        out.append({
            "id": cid,
            "kind": meta.get("kind", ""),
            "note": meta.get("note", ""),
            "created": meta.get("created", ""),
        })
    return out


def credential_path(target: str, cid: str) -> str:
    cid = cid.strip()
    if not CRED_ID_RE.match(cid):
        raise ValueError("凭据 id 必须是 c-NNN 形态")
    index = _load_index(target)
    if cid not in index:
        raise KeyError(f"{cid} 不在 {target} 的凭据目录中")
    return os.path.join(creds_dir(target), index[cid].get("file", cid + ".txt"))


def credential_entry(target: str, cid: str) -> dict:
    """公开口：取 {path, kind}（http_request --cred 注入用，单一 index 读取实现）。"""
    cid = cid.strip()
    if not CRED_ID_RE.match(cid):
        raise ValueError("凭据 id 必须是 c-NNN 形态")
    index = _load_index(target)
    if cid not in index:
        raise KeyError(f"{cid} 不在 {target} 的凭据目录中")
    return {
        "path": os.path.join(creds_dir(target), index[cid].get("file", cid + ".txt")),
        "kind": index[cid].get("kind", "other"),
        "note": index[cid].get("note", ""),
    }


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="凭据库：值不进账本/投影，只存引用")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("put")
    pp.add_argument("target")
    pp.add_argument("--from-file", required=True, dest="from_file")
    pp.add_argument("--kind", default="other")
    pp.add_argument("--note", default="")
    pp.add_argument("--consume", action="store_true", help="登记后删除源值文件（普通删除，非安全擦除）")

    pl = sub.add_parser("list")
    pl.add_argument("target")

    pa = sub.add_parser("path")
    pa.add_argument("target")
    pa.add_argument("cid")

    args = p.parse_args()

    if args.cmd == "put":
        try:
            r = put_credential(args.target, args.from_file, args.kind, args.note, args.consume)
        except (ValueError, FileNotFoundError) as e:
            print(f"[credentials] FAILED: {e}", file=sys.stderr)
            sys.exit(2)
        tag = "（已存在，去重）" if r["dedup"] else ""
        print(f"[credentials] 已登记 {r['id']}{tag}")
        print("账本写：" + f"cred: {r['id']} <身份> @<范围> 来源=<主机/途径> (exp-NNN 或 art-NNN)")
        print("注意：值不进账本/投影；用时 Read results/<target>/credentials/" + r["id"] + ".txt")
    elif args.cmd == "list":
        rows = list_credentials(args.target)
        if not rows:
            print("（无凭据）")
        for r in rows:
            print(f"{r['id']}  [{r['kind']}]  {r['note']}  ({r['created'][:16]})")
        print(f"\n取值路径：python runtime/credentials.py path {args.target} <id>")
    elif args.cmd == "path":
        try:
            print(credential_path(args.target, args.cid))
        except (ValueError, KeyError) as e:
            print(f"[credentials] FAILED: {e}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
