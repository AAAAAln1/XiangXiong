# Xiangxiong（象雄）

黑盒渗透测试用的 **认知 Runtime**，跑在 [Claude Code](https://claude.ai/code) 上。

不是扫描器，也不是「漏洞菜 + 自动点菜」的专家系统。

> 模型往往有搜索与推理能力；失败常来自 **过早压缩世界**——  
> 相似名并成同一对象、未证关系当成已证、局部成功当成目标完成。  
> Runtime 的职责是 **保存事实、投影当前世界、加最小摩擦**，  
> **不**替模型决定下一步、不判洞、不跑 SOP。

授权目标 only。产物与账本落在 `results/<target>/`。

---

## 它解决什么 / 不解决什么

| 要解决 | 不解决 |
|--------|--------|
| 上下文里「当前世界」是什么（对象、未知、反证、实验） | 替你规划整条渗透路径 |
| 工具与状态的机械硬度（HTTP / 实验 / memory 闸） | 自动选漏洞类型 / skill 路由 |
| 减少错误完成信号（report 存在 ≠ 测完） | 保证一定挖到洞 |
| 建模时的反压缩阻尼（相似 ≠ 同一等） | 把经验库塞进每轮上下文 |

**更像成功：** 少把可能性提前删掉、未知边能留住、实验能对准冲突。  
**不是 KPI：** knowledge 读了几次、FC 触发次数、payload 数量。

---

## 四层架构

整个工程只承认四层。不允许靠「再加 Mode / Planner / 知识路由器」变成第五层。

```
Contract   ← CLAUDE.md + cognition/*.md
Knowledge  ← knowledge/*.md
Runtime    ← runtime/ + scripts/ + context/（含 SessionStart 投影）
Artifacts  ← results/
```

### Contract（契约）

- **`CLAUDE.md`**：渗透 Agent 的短合同——工具怎么敲、账怎么记、完成边界、报告槽位。要短、可执行。
- **`cognition/*.md`**：推理镜头（对象 / 证据 / 实验 / 表面错位），**不是**领域操作手册。  
  SessionStart **按固定顺序原样拼接**，Runtime **不**根据目标选型或解释内容。

人 / 维护者读的边界与演进说明在 **`ARCHITECTURE.md`**、**`MAINTAINER.md`**，不要和 Agent 热路径混读。

### Knowledge（先验）

先验有生命周期：

```
实验假设 → frozen 标本 →（跨 target 重复 miss + 可观察行为改善）→ 可进 runtime 的 prior
```

**未证明能改变「世界模型更新行为」的文本，没有资格当 runtime knowledge。**

| 文件 | 状态 | 含义 |
|------|------|------|
| `false_closures.md` | failure prior 库 | 冷知识；「你默认 / 实际 / 还不知道」。**不**被 hook 加载全文 |
| `bounty_criteria.md` | 报告过滤 | 写 `report.md` 时按需 Read；冷，不进 SessionStart |
| `*_objects.md` 等 | **frozen / non-runtime** | 领域切分标本；不 Router、不按栈点菜、不进 SessionStart |

热路径里与「压缩纪律」相关的，是 SessionStart 固定 **6 行** `[ANTI-COMPRESSION]`，**不是**把 `knowledge/` 整目录热起来。

### Runtime（机械层）

| 目录 | 职责 |
|------|------|
| `runtime/` | HTTP、execute、experiment、merge memory、benchmark |
| `scripts/` | SessionStart / observe / stop_check / memory_guard 等 hook |
| `context/` | 可选上下文组装（PostToolUse；只机械加载，不排序不建议） |

原则：

- 只做 IO、状态、有界投影；
- **不**判断漏洞、**不**选下一步、**不**按关键词加载 knowledge；
- 可以改变「模型当前看见什么」，不能替模型思考。

### Artifacts（产物）

`results/<target>/` 保存本场可复查材料：

| 路径 | 内容 |
|------|------|
| `responses/` | HTTP 全文（展示截断 ≠ 全文，以磁盘为准） |
| `experiments/exp-NNN/` | 实验假设、观察、结论 |
| `scripts/` | 本场脚本与 PoC |
| `report.md` | 给人 / 平台看的漏洞报告 |
| `lessons.md` | 短复盘 + Primary Cause |
| `memory.md` | 本 target 归档世界（换 target 时由 merge 机械 archive） |

**Active memory** 在 `.claude/memory.md`（当前唯一热世界）。  
`merge.py` 在 DELTA 出现新 `target:` 时：**archive → clear → init**，不判断公司 / 根域是否该继承。

---

## 目录结构

```
xiangxiong/
├── CLAUDE.md                 # Agent 合同（热）
├── README.md                 # 本文件（人）
├── LICENSE                   # MIT
├── ARCHITECTURE.md           # 开发者宪法 / 边界（人）
├── MAINTAINER.md             # 维护者纪律（人）
├── .claude/                  # settings.example.json；local/memory 不入库
├── cognition/                # 推理镜头（SessionStart 固定加载）
│   ├── object_model.md
│   ├── evidence_model.md
│   ├── experiment_model.md
│   └── surface_misalignment.md
├── knowledge/                # 冷先验 / 标本（默认不进 hook）
│   ├── false_closures.md
│   ├── bounty_criteria.md
│   ├── auth_objects.md
│   ├── frontend_objects.md
│   ├── springboot_objects.md
│   └── cloud_metadata.md
├── runtime/                  # 工具与状态机
│   ├── http_request.py
│   ├── execute.py
│   ├── experiment.py
│   ├── merge.py
│   └── …
├── scripts/                  # SessionStart 等 hook
│   ├── session_start.py
│   ├── observe.py
│   ├── stop_check.py
│   └── memory_guard.py
├── context/                  # 可选上下文组装（机械投影）
├── resources/                # 中性 wordlists 等（非认知层）
├── console/                  # 可选 Chat UI（纯展示，不控流程）
└── results/                  # 按 target 的产物
```

---

## 上下文如何加载（Agent 看见什么）

### 1. Claude Code 读 `CLAUDE.md`

操作合同：HTTP / execute / 实验 / memory 前缀、探索与收口、有 vuln 时如何写 `report.md`。

### 2. SessionStart（`scripts/session_start.py`）

每个会话开始时注入（顺序以脚本为准）：

| 段 | 作用 |
|----|------|
| `[COGNITION]` | 固定四个 cognition 文件；单文件 / 总长有上限 |
| `[ANTI-COMPRESSION]` | **固定 6 行**（见下）；**不**读 `false_closures.md` |
| `[OBSERVE]` | consecutive_none、same_action、stagnation_level |
| `[WORLD MODEL]` | objects / constraints / counters → 实验 → relations 殿后 |
| `[RECENT EXPERIMENTS]` | 当前 target 最近实验摘要 |
| `[RESUME]` | **恢复上下文投影资格**（有 target 且 world 桶非空）——不是「继续 / 停止」判决 |
| `[ARTIFACTS]` | `report.md` / `lessons.md` 是否在磁盘上（present / absent） |

### 3. 其它 hooks

| Hook | 脚本 | 作用 |
|------|------|------|
| PreToolUse Write/Edit | `memory_guard.py` | 禁止直接改 hot memory |
| PostToolUse Bash | `observe.py` | 停滞计数（传感器，不决策） |
| Stop | `stop_check.py` | 投影 stagnation 等级；不强制继续 |
| PostToolUse Bash | `context/assembler.py` | 可选：机械重投影 world（不排序、不建议） |

### 4. `knowledge/` 默认不加载

只有主动 Read 才会看到正文。与「冷标本 / 案例库」定位一致。

---

## 两个重要设计点

### A. 产物完成 ≠ 探索完成

历史上 `report.md` 存在会关掉 `[RESUME]`，等于用 **Artifact** 冒充 **世界已测完**。

现规则：

- `should_resume` **只看**「有 target + memory 里仍有世界状态」；
- `report` / `lessons` **只**出现在 `[ARTIFACTS]`，**不**参与 resume 布尔。

```text
Completion Evidence ≠ Exploration Completion
Object closure ≠ World closure
report 写完 = 导出产物，不是 Target 闭合
```

### B. 压缩纪律是「曝光」，不是「触发器」

`[ANTI-COMPRESSION]` 六行：

```text
Similar identifier ≠ same object.
Related behavior ≠ proven shared boundary.
Successful path ≠ adjacent surface exhausted.
Missing observation ≠ absence.
Supported explanations should constrain the next experiment.
Preserved unknowns do not retain equal priority outside the current explanation boundary.
```

| 是 | 不是 |
|----|------|
| 建对象图时的默认摩擦 + 支持后下一跳约束 | 漏洞技巧库 / planner |
| 低干扰、可少「被调用」 | 每轮强制检查清单 |
| 防错误压扁，也防支持后无关 unknown 等权 | 发现 vuln 后加载 FC 全文 |

验证应看：**高支持解释后下一跳是否偏向确认 / 细化 / 证伪该解释**。  
**不要**用 FC / knowledge 读取次数当成功指标。

---

## Memory 与记账

Memory 回答 **「世界是什么状态」**，不是操作日志或待办。

| 前缀 | 进 WORLD 桶 |
|------|-------------|
| `surface:` / `vuln:` / `cred:` | objects |
| `tech:` / `port:` / `artifact:` | constraints |
| `hypothesis:`（可选 `[observed\|confirmed\|contradicted\|blocked]`） | relations / unknowns |
| `counter:` / `dead:` | counters |
| `target:` | 身份 |
| `future:` | 只进 memory，**不投影** |

- 禁止直接 Write / Edit hot memory；走 `echo "DELTA:…" \| python runtime/merge.py`。
- 每条 &lt;3 行，一次 ≤15 行；有观察 / 冲突 / 结论才写，不是每步填表。
- 换目标：DELTA 写新 `target:` → merge 机械归档旧 world。

错误示例：`surface: 测试了 /admin`  
正确示例：`hypothesis [contradicted]: 存在 admin 后台`

---

## 常用 Runtime 工具

细节以 `CLAUDE.md` 为准。

| 能力 | 入口 |
|------|------|
| HTTP | `python runtime/http_request.py <target> <curl 风格参数…>`（禁止裸 curl 当主路径） |
| 非 HTTP / 脚本 | `python runtime/execute.py <target> --timeout 60 --purpose "…" --cmd "…"`；复杂逻辑先写 `results/<target>/scripts/` |
| 实验 | `create … --activate --hypothesis "…"`；`close … --status success\|failed\|blocked\|unknown` |
| Memory | `echo "DELTA:…" \| python runtime/merge.py` |
| Benchmark | 先 `python runtime/benchmark_vpn.py ensure` |

展示截断 ≠ 磁盘全文：以 `results/<target>/responses/` 为准。

---

## 报告（`report.md`）

给人 / SRC 平台看，**一洞一节**。

- 标题、域名、类型、等级  
- 简述（概况 + 危害；URL / Payload 不进简述）  
- **完整绝对 URL** + 可选功能导航  
- 分步复现 + 关键请求 / 响应 + `req_xxx` / exp  
- 可落地修复  

写报告前问：Impact + Exploitability 够赏金平台收吗？不确定时 Read `knowledge/bounty_criteria.md`。  
纯配置 / 指纹 / 无利用链信息泄露 → 可不写 report（memory 已记录）。  
多洞可同文件；禁止只有摘要表或无证据「可能」。

---

## 使用

```bash
cd xiangxiong
# 首次：复制 hooks 模板为本地配置（勿提交 settings.local.json）
cp .claude/settings.example.json .claude/settings.local.json
# Windows PowerShell 可用：
# Copy-Item .claude\settings.example.json .claude\settings.local.json
claude
```

hooks 由 Claude Code 读取 `.claude/settings.local.json`，模板里是相对路径的 `scripts/` / `context/` 入口。

可选 Console（纯展示，不控流程）：

```bash
# 或 start_console.bat
cd console && pip install -r requirements.txt && python server.py
```

**依赖：** Claude Code CLI、Python 3.8+。  
**范围：** 仅授权目标。作者不对未授权扫描或滥用负责。  
**不要提交：** `results/`、`.claude/memory.md`、本机 `settings.local.json`、cookie / VPN / `.env`（见 `.gitignore`）。

---

## 相关文档（读者分流）

| 读者 | 读什么 |
|------|--------|
| 渗透 Agent | `CLAUDE.md`、SessionStart 投影、cognition |
| 人 / 维护 | 本 README、`ARCHITECTURE.md`、`MAINTAINER.md` |
| 冷先验正文 | `knowledge/*`（不自动进会话） |
| 单场事实 | `results/<target>/` |

改热路径（CLAUDE / cognition / SessionStart 投影）时：  
**站在 LLM 角度——删了下一跳会变吗？不变则删。**  
金句、演进史、给人看的叙事进 ARCHITECTURE / 本文，不灌进 Agent 合同。

---

## 一句话

**象雄 = 在 Claude Code 上给黑盒渗透一个「世界状态 + 最小摩擦」的 Runtime；  
知识与报告是旁路资产，决策仍在模型；  
优化的是过早收缩世界的偏差，不是堆更多漏洞菜单。**
