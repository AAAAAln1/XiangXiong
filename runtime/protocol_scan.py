#!/usr/bin/env python3
"""
protocol_scan.py — 机械协议面聚合（层 2 补充：把"协议解码"变成一条命令）。

背景：grok 时代模型会手动 python 聚合响应目录——读 JS 配置键、跨全部响应
提取 API 路径、找表单字段、关联跨域。近期模型把这个动作丢成了"端点打卡"。
本工具把这些聚合算法机械化，任何模型都可用一条命令拿到协议语法事实。

零判断（与 diff_scan 同层）：
- 只做正则/机械聚合，不解释成功码语义、不判好坏、不推荐下一步
- 不读 memory、不写 memory、不改世界模型（单主人不变）
- 输出是"事实清单"，怎么用由 Agent 决定

借鉴 SnowEyes 的写法：分类结果集（endpoints/js_config/forms/cookies/finger/
secrets/js_files/cross_hosts）+ 正则缓存 + 黑名单噪音过滤。

数据源：results/<target>/responses/req_NNN.json（meta）+ req_NNN.txt（body）。

输出协议（CLI）：
  python runtime/protocol_scan.py <target>   → 打印 [PROTOCOL] 段
库接口：
  scan_protocol(results_root, target) -> dict
  format_protocol(data)               -> str

原则：这是"便宜下一跳"（模型想理解协议时一条命令），不是 Router / 点菜。
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from urllib.parse import urlparse

# status 归一化与 diff_scan 同源：两处各写一份正是本缺陷的来源（T1-H 只修了读方之一）。
# 单一实现，避免再次漂移。
try:
    from .diff_scan import normalize_status
    from .paths import safe_name as safe_target
except ImportError:
    from diff_scan import normalize_status
    from paths import safe_name as safe_target

ENDPOINT_MAX = 20
SECTION_MAX = 15
PATH_SEG_LIMIT = 3          # 路径只留前 3 段做聚合键，太长无聚合意义

# ── 正则缓存（SnowEyes regexCache 写法）──
_re_cache = {}


def _rx(pattern, flags=0):
    """编译并缓存正则。热路径避免重复 compile。"""
    key = (pattern, flags)
    if key not in _re_cache:
        _re_cache[key] = re.compile(pattern, flags)
    return _re_cache[key]


def target_root(results_root: str, target: str) -> str:
    return os.path.join(results_root, safe_target(target))


def load_items(results_root: str, target: str) -> list:
    """公开口：按 req 号升序返回 [(req_num, meta, body), ...]（供 evidence 台账复用）。

    与 _scan_meta_files 同源——证据读取只有这一份装载实现，禁止 evidence 再写
    一份 glob+json.load（副本漂移学费已交过四次）。注意：空 {} 占位/损坏 sidecar
    在此被跳过；占位与损坏的**计数**由 evidence 自行扫目录得到，不混入本列表。
    """
    return _scan_meta_files(os.path.join(target_root(results_root, target), "responses"))


def _load_meta(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None


def _load_body(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _scan_meta_files(responses_dir: str) -> list:
    """按 req 号升序收集全部 meta。返回 [(req_num, meta, body), ...]。"""
    items = []
    for fname in sorted(os.listdir(responses_dir)):
        m = _rx(r"req_(\d+)\.json").fullmatch(fname)
        if not m:
            continue
        meta = _load_meta(os.path.join(responses_dir, fname))
        if not meta:
            continue
        req_num = int(m.group(1))
        body_path = os.path.join(responses_dir, f"req_{req_num:03d}.txt")
        items.append((req_num, meta, _load_body(body_path)))
    return items


def _endpoint_key(method: str, url: str) -> str | None:
    """聚合键：METHOD scheme://host/path 前 3 段。不可解析返回 None。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    netloc = (parsed.netloc or "").lower()
    if not netloc:
        return None
    segs = [s for s in parsed.path.split("/") if s][:PATH_SEG_LIMIT]
    path = "/" + "/".join(segs)
    return f"{method.upper()} {parsed.scheme}://{netloc}{path}"


def _aggregate_endpoints(items: list) -> list:
    """按 (method, host, 前3段路径) 聚合，含状态码分布。"""
    groups: dict[str, dict] = {}
    for _, meta, _ in items:
        key = _endpoint_key(meta.get("method") or "GET", meta.get("url") or "")
        if key is None:
            continue
        status = normalize_status(meta.get("status")) or "?"
        if key not in groups:
            groups[key] = {"url": key, "reqs": 0, "statuses": Counter()}
        groups[key]["reqs"] += 1
        groups[key]["statuses"][status] += 1
    ordered = sorted(groups.values(), key=lambda g: -g["reqs"])[:ENDPOINT_MAX]
    for g in ordered:
        g["statuses"] = dict(sorted(g["statuses"].items()))
    return ordered


# ── JS 配置键提取（grok 手动找的那批：window.__* / baseURL / VUE_APP_*）──
JS_CONFIG_PATTERNS = (
    _rx(r"window\.__[A-Za-z0-9_]+"),
    _rx(r"\b(baseURL|apiUrl|serverUrl|baseUrl|API_URL|SERVER_URL|VUE_APP_[A-Z0-9_]+|process\.env\.[A-Z0-9_]+)\s*[:=]\s*[\"']([^\"']{0,120})[\"']"),
    _rx(r"axios\.create\(\s*\{[^}]*?baseURL"),
)
# 与 JS_CONFIG_PATTERNS 同步的快速预检子串（防漏报：pre-filter 必须覆盖全部模式的命中特征）
_JS_CONFIG_HINTS = ("baseURL", "baseUrl", "apiUrl", "serverUrl", "API_URL",
                    "SERVER_URL", "window.__", "VUE_APP_", "process.env", "axios")


def _scan_js_config(bodies: list) -> list:
    hits: dict[str, Counter] = defaultdict(Counter)  # 键 -> {值: 次数}
    for body in bodies:
        if not any(hint in body for hint in _JS_CONFIG_HINTS):
            continue
        for pat in JS_CONFIG_PATTERNS:
            for m in pat.finditer(body):
                text = m.group(0)
                if text.startswith("window.__") or text.startswith("process.env"):
                    key = text
                    value = ""
                elif "axios.create" in text:
                    key = "axios.create(baseURL)"
                    value = ""
                else:
                    key, value = m.group(1), m.group(2)
                hits[key][value] += 1
    out = []
    for key in sorted(hits):
        top_val, top_n = hits[key].most_common(1)[0]
        out.append({"key": key, "value": top_val, "n": top_n})
    return out[:SECTION_MAX]


# ── 表单/输入字段（按 form 块内归属，避免跨 form 串扰）──
_FORM_OPEN = _rx(r"<form\b[^>]*>", re.IGNORECASE)
_FORM_CLOSE = _rx(r"</form\s*>", re.IGNORECASE)
_FORM_ACTION = _rx(r"<form\b[^>]*action=[\"']([^\"']*)[\"']", re.IGNORECASE)
_INPUT_NAME = _rx(r"<(input|select|textarea)\b[^>]*name=[\"']([^\"']*)[\"']", re.IGNORECASE)


def _form_blocks(body: str) -> list:
    """切分 <form>...</form> 块，返回 [(action, 块文本)]。无 action 记 (no-action)。"""
    blocks = []
    open_positions = [m.start() for m in _FORM_OPEN.finditer(body)]
    if not open_positions:
        return blocks
    for i, start in enumerate(open_positions):
        close = _FORM_CLOSE.search(body, start)
        end = close.start() if close else len(body)
        # 若下一个 <form> 出现在 </form> 前（未闭合），用下一个 form 起点截断
        if i + 1 < len(open_positions) and open_positions[i + 1] < end:
            end = open_positions[i + 1]
        block_text = body[start:end]
        action_match = _FORM_ACTION.search(block_text)
        action = action_match.group(1) if action_match else "(no-action)"
        blocks.append((action, block_text))
    return blocks


def _scan_forms(items: list) -> list:
    """聚合表单 action + 字段名，字段从各自 form 块内提取。"""
    forms: dict[str, Counter] = defaultdict(Counter)
    for _, meta, body in items:
        if "text/html" not in (meta.get("content_type") or "") or "<form" not in body.lower():
            continue
        blocks = _form_blocks(body) or [("(no-action)", body)]
        for action, block_text in blocks:
            for m in _INPUT_NAME.finditer(block_text):
                forms[action][m.group(2)] += 1
    out = []
    for action, fields in sorted(forms.items(), key=lambda kv: -sum(kv[1].values())):
        out.append({
            "action": action[:120],
            "fields": [f for f, n in fields.most_common(12)],
        })
    return out[:SECTION_MAX]


# ── Cookie 名（body 痕迹 + session jar）──
_SET_COOKIE_RE = _rx(r"\bSet-Cookie:\s*([A-Za-z0-9_\-\.]+)\s*=", re.IGNORECASE)


def _jar_cookie_names(sessions_dir: str) -> list:
    """解析 Netscape cookie jar：tab 分隔，第 6 列（index 5）才是 cookie 名。

    兼容 #HttpOnly_ 前缀行（HttpOnly cookie 是会话 cookie 的主要形态，
    不能跳过）。每行格式：
      [HttpOnly_]<domain>\t<include_subdomains>\t<path>\t<secure>\t<expiry>\t<name>\t<value>
    """
    names = set()
    if not os.path.isdir(sessions_dir):
        return []
    for fname in os.listdir(sessions_dir):
        if not fname.endswith(".jar"):
            continue
        try:
            with open(os.path.join(sessions_dir, fname), "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("# Netscape") or line.startswith("# https"):
                        continue
                    # 剥离 #HttpOnly_ 前缀后按 tab 切分
                    cleaned = line[11:] if line.startswith("#HttpOnly_") else line
                    fields = cleaned.split("\t")
                    if len(fields) >= 6 and fields[5]:
                        names.add(fields[5])
        except OSError:
            continue
    return sorted(names)


def _scan_cookies(items: list, sessions_dir: str) -> list:
    names = Counter()
    for _, _, body in items:
        for m in _SET_COOKIE_RE.finditer(body):
            names[m.group(1)] += 1
    for name in _jar_cookie_names(sessions_dir):
        if name not in names:
            names[name] = 0
    return [{"name": n, "n": names[n]} for n in sorted(names)][:SECTION_MAX]


# ── 技术栈指纹（SnowEyes FINGER.PATTERNS 写法：类 + 正则 + 描述）──
FINGER_PATTERNS = (
    # (class, pattern, description)
    ("webpack", r"webpackJsonp|__webpack_require__", "webpack 打包"),
    ("vite", r"__vite__|/@vite/client", "vite 打包"),
    ("react", r"react-dom|__REACT_DEVTOOLS_GLOBAL_HOOK__|data-reactroot", "React 前端"),
    ("vue", r"vue-loader|data-v-[a-f0-9]{8}|Vue\.createApp|new Vue\(", "Vue 前端"),
    ("angular", r"ng-app|ng-controller|angular\.module", "Angular 前端"),
    ("django", r"csrfmiddlewaretoken", "Django (Python)"),
    ("laravel", r"laravel_session|Laravel", "Laravel (PHP)"),
    ("thinkphp", r"thinkphp|_thinkphp", "ThinkPHP (PHP)"),
    ("yii", r"YII_CSRF|yii\.js|generator.*Yii", "Yii (PHP)"),
    ("wordpress", r"wp-content|wp-includes", "WordPress"),
    ("java", r"JSESSIONID", "Java (Servlet)"),
    ("aspnet", r"ASP\.NET_SessionId|__VIEWSTATE|ViewState", "ASP.NET"),
    ("nginx", r"Server:\s*nginx", "Nginx"),
    ("cloudflare", r"cdnjs\.cloudflare\.com|cloudflare", "Cloudflare CDN"),
    ("jsdelivr", r"cdn\.jsdelivr\.net", "jsDelivr CDN"),
    ("jquery", r"jquery[-.][0-9]|jQuery\.fn\.", "jQuery"),
)


def _scan_fingerprint(bodies: list) -> list:
    """机械指纹匹配，只报"存在哪个特征"，不判好坏。"""
    found: dict[str, dict] = {}
    for body in bodies:
        for name, pattern, desc in FINGER_PATTERNS:
            if name in found:
                continue
            if _rx(pattern, re.IGNORECASE).search(body):
                found[name] = {"name": name, "description": desc}
    # 按预设顺序稳定输出（保留 FINGER_PATTERNS 顺序）
    return [found[name] for name, _, _ in FINGER_PATTERNS if name in found]


# ── 敏感标识机械匹配（SnowEyes ID_KEY 写法：平台密钥/JWT）──
SECRET_PATTERNS = (
    # (name, pattern)
    ("jwt", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._-]{10,}\.[A-Za-z0-9._-]{5,}"),
    ("aws", r"AKIA[0-9A-Z]{16}"),
    ("aliyun", r"LTAI[A-Za-z\d]{12,30}"),
    ("google", r"AIza[0-9A-Za-z_\-]{35}"),
    ("tencent", r"AKID[A-Za-z\d]{13,40}"),
    ("github", r"(?:ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9_]{36,255}"),
    ("wechat", r"\bwx[a-z0-9]{15,18}\b"),
)


def _scan_secrets(bodies: list) -> list:
    """机械匹配 JWT/云厂商密钥/JDK token，只报告命中类型与来源 req，不取值。"""
    found: dict[str, Counter] = defaultdict(Counter)
    for req_num, meta, body in items_or_bodies(bodies):
        for name, pattern in SECRET_PATTERNS:
            if _rx(pattern).search(body):
                found[name][f"req_{req_num:03d}"] += 1
    return [{"name": n, "reqs": ",".join(sorted(c))} for n, c in sorted(found.items())]


def items_or_bodies(bodies):
    """兼容：传入 [(req, meta, body)] 或 [body, ...] 都按含编号处理。"""
    for item in bodies:
        if isinstance(item, tuple):
            yield item[0], item[1], item[2]
        else:
            yield 0, {}, item


# ── JS 文件清单（协议材料：被引用的业务 JS）──
_SCRIPT_SRC = _rx(r"<script[^>]+src=[\"']([^\"']+\.js)[\"']", re.IGNORECASE)
_IMPORT_JS = _rx(r"(?:import|require)\s*\(?[\"']([^\"']+\.js)[\"']", re.IGNORECASE)


def _scan_js_files(bodies: list) -> list:
    """聚合 script src / import 引用的 .js 文件，去重计数。"""
    files: Counter = Counter()
    for _, _, body in items_or_bodies(bodies):
        for m in _SCRIPT_SRC.finditer(body):
            files[m.group(1)] += 1
        for m in _IMPORT_JS.finditer(body):
            files[m.group(1)] += 1
    return [{"file": f, "n": n} for f, n in files.most_common(SECTION_MAX)]


# ── 跨域引用（grok 的"跨域关联"：body 里出现的目标外域名）──
_URL_HOST_RE = _rx(r"https?://([A-Za-z0-9\-\.]+)")
# 噪音域黑名单（SnowEyes DOMAIN.BLACKLIST 写法）：DTD/标准站引用无关联价值
_NOISE_HOSTS = {
    "w3.org", "www.w3.org", "w3c.org", "www.w3c.org", "schema.org",
    "json-schema.org", "example.com", "localhost", "www.apache.org",
    "www.mozilla.org", "developer.mozilla.org", "validator.w3.org",
    "www.w3schools.com", "www.iana.org", "xml.org", "dublincore.org",
    "purl.org", "purl.oclc.org", "www.ietf.org", "tools.ietf.org",
    "www.whatwg.org", "html.spec.whatwg.org",
}


def _scan_cross_hosts(items: list, target_hosts: set) -> list:
    """聚合 body 中出现的 https?://host。

    - 排除目标自身 host（完全匹配）
    - 同根域子域（如 svr.example.com vs gs.example.com）保留并打标 (同根域)
      ——黑盒渗透里同根域子域是最高价值关联面，不能排除
    - 噪音标准站（w3.org 等）过滤
    """
    refs: dict[str, Counter] = defaultdict(Counter)
    root_hosts = {h.split(".", 1)[1] for h in target_hosts if "." in h}
    for _, _, body in items:
        for m in _URL_HOST_RE.finditer(body):
            host = m.group(1).lower()
            if host in target_hosts:
                continue
            if host in _NOISE_HOSTS:
                continue
            refs[host]["all"] += 1
            # 标记同根域：host 是某 target 子域（如 svr.x.com 对 gs.x.com）
            if any(host.endswith("." + rh) for rh in root_hosts):
                refs[host]["same_root"] += 1
    out = []
    for host, c in sorted(refs.items(), key=lambda kv: -kv[1]["all"]):
        entry = {"host": host, "n": c["all"]}
        if c["same_root"]:
            entry["same_root"] = True
        out.append(entry)
    return out[:SECTION_MAX]


def _target_hosts(items: list) -> set:
    hosts = set()
    for _, meta, _ in items:
        try:
            hosts.add((urlparse(meta.get("url") or "").netloc or "").lower())
        except ValueError:
            continue
    return hosts


def scan_protocol(results_root: str, target: str) -> dict:
    """扫描一个 target 的 responses/，返回协议事实聚合。"""
    root = target_root(results_root, target)
    responses_dir = os.path.join(root, "responses")
    if not os.path.isdir(responses_dir):
        return {"error": f"no responses dir: {os.path.relpath(responses_dir)}"}
    items = _scan_meta_files(responses_dir)
    if not items:
        return {"error": "no req_*.json metadata found"}
    bodies = [b for _, _, b in items]
    hosts = _target_hosts(items)
    sessions_dir = os.path.join(root, "sessions")
    return {
        "target": target,
        "scanned": len(items),
        "req_range": f"req_{items[0][0]:03d}..req_{items[-1][0]:03d}",
        "endpoints": _aggregate_endpoints(items),
        "js_config": _scan_js_config(bodies),
        "forms": _scan_forms(items),
        "cookies": _scan_cookies(items, sessions_dir),
        "fingerprint": _scan_fingerprint(bodies),
        "secrets": _scan_secrets(items),
        "js_files": _scan_js_files(items),
        "cross_hosts": _scan_cross_hosts(items, hosts),
    }


def format_protocol(data: dict) -> str:
    if data.get("error"):
        return f"[PROTOCOL]\n  (scan error: {data['error']})\n"
    lines = ["[PROTOCOL] (机械聚合，无判断)", f"scanned: {data['scanned']} responses ({data['req_range']})", ""]

    lines.append(f"endpoints ({len(data['endpoints'])}):")
    if not data["endpoints"]:
        lines.append("  (none)")
    for g in data["endpoints"]:
        statuses = " ".join(f"{s}×{n}" for s, n in g["statuses"].items())
        lines.append(f"  {g['url']}  [{g['reqs']} req] {statuses}")
    lines.append("")

    lines.append(f"js_config ({len(data['js_config'])}):")
    if not data["js_config"]:
        lines.append("  (none)")
    for c in data["js_config"]:
        if c["value"]:
            lines.append(f"  {c['key']} = {c['value']}")
        else:
            lines.append(f"  {c['key']}")
    lines.append("")

    lines.append(f"forms ({len(data['forms'])}):")
    if not data["forms"]:
        lines.append("  (none)")
    for f in data["forms"]:
        lines.append(f"  action={f['action']} | fields: {','.join(f['fields'])}")
    lines.append("")

    lines.append(f"cookies ({len(data['cookies'])}):")
    if not data["cookies"]:
        lines.append("  (none)")
    lines.append("  " + ", ".join(c["name"] for c in data["cookies"]))
    lines.append("")

    lines.append(f"fingerprint ({len(data['fingerprint'])}):")
    if not data["fingerprint"]:
        lines.append("  (none)")
    for f in data["fingerprint"]:
        lines.append(f"  {f['name']} — {f['description']}")
    lines.append("")

    lines.append(f"secrets ({len(data['secrets'])}):")
    if not data["secrets"]:
        lines.append("  (none)")
    for s in data["secrets"]:
        lines.append(f"  {s['name']}: {s['reqs']}")
    lines.append("")

    lines.append(f"js_files ({len(data['js_files'])}):")
    if not data["js_files"]:
        lines.append("  (none)")
    for f in data["js_files"]:
        lines.append(f"  {f['file']} ({f['n']})")
    lines.append("")

    lines.append(f"cross_hosts ({len(data['cross_hosts'])}):")
    if not data["cross_hosts"]:
        lines.append("  (none)")
    for h in data["cross_hosts"]:
        mark = " (同根域)" if h.get("same_root") else ""
        lines.append(f"  {h['host']} ({h['n']} refs){mark}")
    lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print("用法: python runtime/protocol_scan.py <target>")
        return 1
    results_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
    data = scan_protocol(results_root, sys.argv[1])
    print(format_protocol(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
