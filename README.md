# 象雄（Xiangxiong）

黑盒渗透测试用的 **认知 Runtime**，跑在 [Claude Code](https://claude.ai/code) 上。

不是扫描器，也不是「漏洞菜 + 自动点菜」的专家系统。  
**象雄是研究型框架。** 合法授权与测试范围由使用者自行负责。

> 模型往往有搜索与推理能力；失败常来自 **过早压缩世界**——  
> 相似名并成同一对象、未证关系当成已证、局部成功当成目标完成。  
> Runtime 的职责是 **保存事实、投影当前世界、加最小摩擦**，  
> **不**替模型决定下一步、不判洞、不跑 SOP。

仅限授权目标。产物与账本在本地 `results/<target>/`（**不入库**）。

---

## 快速开始

### 前置条件

- **Python 3.8+**（Runtime 只用标准库，无第三方依赖）
- **[Claude Code CLI](https://claude.ai/code)**（宿主执行环境）

### 1. 安装

```bash
git clone https://github.com/AAAAAln1/XiangXiong.git
cd XiangXiong
# 可选：安装测试依赖
pip install -r requirements.txt
```

### 2. 配置 hooks（一次性）

```bash
# 复制 hooks 模板为本地配置（settings.local.json 不入库）
cp .claude/settings.example.json .claude/settings.local.json
# Windows PowerShell：
# Copy-Item .claude\settings.example.json .claude\settings.local.json
```

### 3. 验证安装

```bash
# 跑回归测试（65 个用例，覆盖解析/投影/边界契约）
python -m pytest tests/ -q
```

### 4. 第一次使用

```bash
# 在项目根目录启动 Claude Code
claude
# 输入一个授权目标，例如：
#   测试 https://example.com/ 这个站点
```

Agent 启动时 hooks 自动注入世界投影（`[WORLD MODEL]` / `[HYPOTHESES]` / `[RESUME]` 等），工具调用后自动记账与刷新上下文。产物落在 `results/<target>/`。

> **注意：** 测试前确认目标已获授权；`results/`、`.claude/memory.md`、`settings.local.json` 均不入库。

---

## 设计哲学

多数 Agent 框架在编码 **工作流**。  
象雄探索的是另一条路：在不确定环境里 **保住世界状态**。

| 原则 | 含义 |
|------|------|
| **以对象为中心** | 世界是对象与关系，不是漏洞类型清单 |
| **实验优先** | 可差分实验是一等产物；假设要可证伪 |
| **最小 Runtime** | Runtime 提供能力与投影，**不**做决策、不判洞、不点菜 |
| **模型自主推理** | 下一步仍在模型；知识描述世界，不是操作手册 |

对照普通 Agent：

```text
任务 → 工作流 → 工具 → 结果
```

象雄更接近：

```text
观察 → 世界模型 → 假设 → 实验 → 更新后的理解
```

再压成四句：

- Runtime 提供 **能力**，不提供 **决策**
- 知识描述 **世界**，不描述 **流程**
- 实验是 **一等产物**
- **推理责任** 仍在大模型

**更像成功：** 少把可能性提前删掉、未知边能留住、实验能对准冲突。  
**不是 KPI：** 知识文件读了几次、payload 数量、「路径扫了多少」。

---

## 它解决什么 / 不解决什么

| 要解决 | 不解决 |
|--------|--------|
| 上下文里「当前世界」是什么（对象、未知、反证、实验） | 替你规划整条渗透路径 |
| 工具与状态的机械硬度（HTTP / 实验 / memory 闸） | 自动选漏洞类型 / 技能路由 |
| 减少错误完成信号（有报告 ≠ 测完） | 保证一定挖到洞 |
| 建模时的反压缩阻尼（相似 ≠ 同一等） | 把经验库塞进每轮上下文 |

---

## 四层架构

整个工程只承认四层。不允许靠「再加 Mode / Planner / 知识路由器」变成第五层。

```text
契约 Contract     ← CLAUDE.md + cognition/*.md
知识 Knowledge    ← knowledge/*.md
运行时 Runtime    ← runtime/ + scripts/ + context/（含会话开始投影）
产物 Artifacts    ← results/
```

### 契约（Contract）

- **`CLAUDE.md`**：给渗透 Agent 的短合同——工具怎么敲、账怎么记、完成边界、报告槽位。要短、可执行。
- **`cognition/*.md`**：推理镜头（对象 / 证据 / 实验 / 表面错位），**不是**领域操作手册。  
  会话开始时 **按固定顺序原样拼接**，Runtime **不**根据目标选型或解释内容。

人 / 维护者读的边界说明在 **`ARCHITECTURE.md`**、**`MAINTAINER.md`**，不要和 Agent 热路径混读。

### 知识（Knowledge）

先验有生命周期：

```text
实验假设 → 冻结标本 →（跨目标重复 miss + 可观察行为改善）→ 可进入运行时的先验
```

**未证明能改变「世界模型更新行为」的文本，没有资格当运行时知识。**

| 文件 | 状态 | 含义 |
|------|------|------|
| `false_closures.md` | 失败先验库 | 冷知识；「你默认 / 实际 / 还不知道」。**不**被 hook 加载全文 |
| `bounty_criteria.md` | 报告过滤 | 写 `report.md` 时按需阅读；冷，不进会话开始 |
| `*_objects.md` 等 | **冻结 / 非运行时** | 领域切分标本；不路由、不按技术栈点菜、不进会话开始 |

热路径里与「压缩纪律」相关的，是会话开始固定 **6 行** 反压缩短句，**不是**把 `knowledge/` 整目录热起来。

### 运行时（Runtime）

| 目录 | 职责 |
|------|------|
| `runtime/` | HTTP、执行、实验、合并 memory |
| `scripts/` | 会话开始 / 观测 / 停止检查 / memory 守卫等 hook |
| `context/` | 可选上下文组装（工具后触发；只机械加载，不排序、不建议） |

原则：

- 只做 IO、状态、有界投影；
- **不**判断漏洞、**不**选下一步、**不**按关键词加载知识；
- 可以改变「模型当前看见什么」，不能替模型思考。

### 产物（Artifacts）

`results/<target>/` 保存本场可复查材料（**仅本地，默认不进 Git**）：

| 路径 | 内容 |
|------|------|
| `responses/` | HTTP 全文（界面截断 ≠ 全文，以磁盘为准） |
| `experiments/exp-NNN/` | 实验假设、观察、结论 |
| `scripts/` | 本场脚本与 PoC |
| `report.md` | 给人 / 平台看的漏洞报告 |
| `lessons.md` | 短复盘 + 主因 |
| `memory.md` | 本目标归档世界（换目标时由 merge 机械归档） |

**当前热世界** 在 `.claude/memory.md`（唯一 active）。  
`merge.py` 在 DELTA 出现新 `target:` 时：**归档 → 清空 → 初始化**，不判断公司 / 根域是否该继承。

---

## 目录结构

```text
xiangxiong/
├── CLAUDE.md                 # Agent 合同（热）
├── README.md                 # 本文件（人）
├── LICENSE                   # MIT
├── ARCHITECTURE.md           # 开发者边界（人）
├── MAINTAINER.md             # 维护纪律（人）
├── .claude/                  # settings.example.json；local/memory 不入库
├── cognition/                # 推理镜头（会话开始固定加载）
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
├── scripts/                  # 会话开始等 hook
│   ├── session_start.py
│   ├── observe.py
│   ├── stop_check.py
│   └── memory_guard.py
├── context/                  # 可选上下文组装（机械投影）
├── resources/                # 中性词表等（非认知层）
└── results/                  # 按目标的产物（本地，不入库）
```

---

## 上下文如何加载（Agent 看见什么）

### 1. Claude Code 读 `CLAUDE.md`

操作合同：HTTP / 执行 / 实验 / memory 前缀、探索与收口、有漏洞时如何写 `report.md`。

### 2. 会话开始（`scripts/session_start.py`）

每个会话开始时注入（顺序以脚本为准）：

| 段 | 作用 |
|----|------|
| `[COGNITION]` | 固定四个 cognition 文件；单文件 / 总长有上限 |
| `[ANTI-COMPRESSION]` | **固定 6 行**（见下）；**不**读 `false_closures.md` |
| `[OBSERVE]` | 连续无产出、同动作次数、停滞等级 |
| `[WORLD MODEL]` | 对象 / 约束 / 反证 → 实验 → 关系殿后 |
| `[RECENT EXPERIMENTS]` | 当前目标最近实验摘要 |
| `[RESUME]` | **是否具备恢复上下文的投影资格**（有目标且世界桶非空）——**不是**「继续 / 停止」判决 |
| `[ARTIFACTS]` | 磁盘上是否有 `report.md` / `lessons.md` |

### 3. 其它 hooks

| 时机 | 脚本 | 作用 |
|------|------|------|
| 写文件前 | `memory_guard.py` | 禁止直接改热 memory |
| Bash 之后 | `observe.py` | 停滞计数（传感器，不决策） |
| 轮次结束 | `stop_check.py` | 投影停滞等级；不强制继续 |
| Bash 之后 | `context/assembler.py` | 可选：机械重投影世界（不排序、不建议） |

### 4. `knowledge/` 默认不加载

只有主动阅读才会看到正文。定位是「冷标本 / 案例库」，不是每轮菜单。

---

## 两个重要设计点

### A. 产物完成 ≠ 探索完成

历史上：有了 `report.md` 就关掉恢复投影，等于用 **产物** 冒充 **世界已测完**。

现规则：

- 是否投影恢复上下文，**只看**「有目标 + memory 里仍有世界状态」；
- 报告 / 复盘 **只**出现在产物元数据里，**不**参与「是否恢复」判断。

```text
完成证据 ≠ 探索完成
对象边闭合 ≠ 整个目标世界闭合
写完报告 = 导出产物，不是目标已测完
```

### B. 压缩纪律是「曝光」，不是「触发器」

`[ANTI-COMPRESSION]` 六行（脚本里的固定短句，原则不是步骤）：

```text
Similar identifier ≠ same object.
Related behavior ≠ proven shared boundary.
Successful path ≠ adjacent surface exhausted.
Missing observation ≠ absence.
Supported explanations should constrain the next experiment.
Preserved unknowns do not retain equal priority outside the current explanation boundary.
```

中文对照：

1. 相似标识 ≠ 同一对象  
2. 相关行为 ≠ 已证明的共同边界  
3. 某条路径成功 ≠ 邻接面已穷尽  
4. 缺观察 ≠ 不存在  
5. 已获支持的解释，应约束下一跳实验  
6. 解释边界外的未知可以保留，但不应与当前主线等权抢预算  

| 是 | 不是 |
|----|------|
| 建对象图时的默认摩擦 + 支持后的下一跳约束 | 漏洞技巧库 / 规划器 |
| 低干扰、不必「被调用很多次」 | 每轮强制检查清单 |
| 防错误压扁，也防支持后无关未知等权 | 发现漏洞后加载失败先验全文 |

验证应看：**高支持解释之后，下一跳是否偏向确认 / 细化 / 证伪该解释**。  
**不要**用知识文件读取次数当成功指标。

---

## Memory 与记账

Memory 回答 **「世界是什么状态」**，不是操作日志或待办。

| 前缀 | 进入世界投影的桶 |
|------|------------------|
| `surface:` / `vuln:` / `cred:` | 对象 |
| `tech:` / `port:` / `artifact:` | 约束 |
| `hypothesis:`（可选 `[observed\|confirmed\|contradicted\|blocked]`） | 关系 / 未知 |
| `counter:` / `dead:` | 反证 / 死路 |
| `target:` | 身份 |
| `future:` | 只进 memory，**不投影** |

- 禁止直接改热 memory；走 `echo "DELTA:…" | python runtime/merge.py`。
- 每条少于 3 行，一次不超过 15 行；有观察 / 冲突 / 结论才写，不是每步填表。
- 换目标：DELTA 写新 `target:` → merge 机械归档旧世界。

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

展示截断 ≠ 磁盘全文：以 `results/<target>/responses/` 为准。

---

## 报告（`report.md`）

给人 / SRC 平台看，**一洞一节**。

- 标题、域名、类型、等级  
- 简述（概况 + 危害；URL / Payload 不进简述）  
- **完整绝对 URL** + 可选功能导航  
- 分步复现 + **每个 HTTP 步附完整原始请求包**（`METHOD /path HTTP/1.1`+`Host`+Header+空行+Body，可粘 Burp Repeater）/ 响应 + `req_xxx` / 实验编号  
- 可落地修复  

写报告前问：影响面 + 可利用性够赏金平台收吗？不确定时阅读 `knowledge/bounty_criteria.md`。  
纯配置 / 指纹 / 无利用链信息泄露 → 可不写报告（memory 已记录）。  
多洞可同文件；禁止只有摘要表或无证据「可能」。

---

## 使用

安装、hooks 配置与验证见顶部 **[快速开始](#快速开始)**。

```bash
cd xiangxiong
claude
```

hooks 由 Claude Code 读取 `.claude/settings.local.json`，模板里是相对路径的 `scripts/` / `context/` 入口（`settings.example.json`）。

**范围：** 仅授权目标。作者不对未授权扫描或滥用负责。  
**不要提交：** `results/`、`.claude/memory.md`、本机 `settings.local.json`、cookie / VPN / `.env`（见 `.gitignore`）。

---

## 更新日志

### v0.2.0（2026-08）

**Object-Centered projection 一致性修复**

- **投影镜像 world schema**：`HYPOTHESES` 段按 memory 顺序全量展示 hypothesis，state 作为行内属性（`observed/confirmed/contradicted/blocked/unknown`），不再按 state 值过滤或分组。
- **移除 schema 外分类**：删除 `OPEN UNKNOWNS` / `STALLED` 自创桶名与过滤逻辑，消除 projection 对世界状态的解释层。
- **Contract 边界句收敛**：引导语只保留"来自世界模型，不是任务要求，不是下一步指令"。
- **回归锁**：新增 state visibility 测试——hypothesis 从 `observed` 转移至 `confirmed/contradicted` 后仍必须可见，防未来再次丢失 state（65 tests passed）。

**Runtime 能力增强（v0.1 → v0.2 累积）**

- **Observation Artifact Contract**：`execute.py` / `http_request.py` 返回 `stdout_*/stderr_*` 与 `body_artifact/body_size/body_truncated` 物理元数据（截断 ≠ 全文）。
- **Turn Event Telemetry**：`stop_check.py` 统计 `turn_tool_use_count` / `turns_without_tool_use`，仅被动传感，不接 `decision:block`。
- **单投影源（Phase 2.0）**：`assembler.py` 收敛为 runtime 适配器，SessionStart 与 PostTool 共享 `format_world_section` / `format_unknown_sections`。
- **测试体系**：`tests/phase0` ~ `tests/phase2_3` 分阶段回归锁，验证解析无损、投影一致、边界契约。
- **上手体验**：README 新增「快速开始」章节；新增 `requirements.txt` 明确依赖（Runtime 纯标准库，测试需 pytest）。

---

## 相关文档（读者分流）

| 读者 | 读什么 |
|------|--------|
| 渗透 Agent | `CLAUDE.md`、会话开始投影、cognition |
| 人 / 维护 | 本 README、`ARCHITECTURE.md`、`MAINTAINER.md` |
| 冷先验正文 | `knowledge/*`（不自动进会话） |
| 单场事实 | `results/<target>/`（本地） |

改热路径（`CLAUDE.md` / cognition / 会话开始投影）时：  
**站在模型角度——删了下一跳会变吗？不变则删。**  
金句、演进史、给人看的叙事进 `ARCHITECTURE` / 本文，不灌进 Agent 合同。

---

## 一句话

**象雄 = 在 Claude Code 上给黑盒渗透一个「世界状态 + 最小摩擦」的 Runtime；  
知识与报告是旁路资产，决策仍在模型；  
优化的是过早收缩世界的偏差，不是堆更多漏洞菜单。**

---

## 开发者声明

这是个人对 LLM Agent 认知架构的探索，重点在"如何避免模型过早闭合世界"。

设计取舍（行为优先、对象延迟、反因果表达）基于有限实验，可能存在未发现的问题。欢迎讨论与质疑。
