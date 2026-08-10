# 象雄（Xiangxiong）

黑盒渗透测试用的 **认知 Runtime**，跑在 [Claude Code](https://claude.ai/code) 上。

不是扫描器，也不是「漏洞菜 + 自动点菜」的专家系统。  
**象雄是研究型框架。** 合法授权与测试范围由使用者自行负责。

> 模型往往有搜索与推理能力；失败常来自 **过早压缩世界**——  
> 相似名并成同一对象、未证关系当成已证、局部成功当成目标完成。  
> Runtime 的职责是 **持久化 LLM 的认知资产**（结论 + 状态 + 证据编号），  
> **不**替模型决定下一步、不判洞、不跑 SOP、不记录事件日志。

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
# 跑回归测试（25 个用例，覆盖解析/投影/记账/证据绑定契约）
python -m pytest tests/ -q
```

### 4. 第一次使用

```bash
# 在项目根目录启动 Claude Code
claude
# 输入一个授权目标，例如：
#   测试 https://example.com/ 这个站点
```

Agent 启动时 hooks 注入结论账本投影（`[WORLD MODEL]` / `[HYPOTHESES]` / `[RESUME]` 等），探索中通过 `merge.py` 合并记账，下次会话开始投影最新账本。产物落在 `results/<target>/`。

> **注意：** 测试前确认目标已获授权；`results/`、`.claude/memory.md`、`settings.local.json` 均不入库。

---

## 设计哲学

多数 Agent 框架在编码 **工作流**。  
象雄探索的是另一条路：在不确定环境里 **不让 LLM 思考过的结论丢失**。

| 原则 | 含义 |
|------|------|
| **认知持久化** | Runtime 保证 LLM 的结论 / 状态 / 证据编号不丢失；对象关系判断保留在模型上下文 |
| **实验优先** | 可差分实验是一等产物；`close` 自动把结论写回账本 |
| **最小 Runtime** | 只做执行、落盘、记账、单次投影——不判洞、不选下一步、不记事件日志 |
| **模型自主推理** | 下一步仍在模型；Runtime 不引导、不打断、不干预停滞 |

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
- 账本存 **结论**，不存 **日志**

**更像成功：** 少把可能性提前删掉、未知边能留住、实验能对准冲突、结论能跨会话恢复。  
**不是 KPI：** 知识文件读了几次、payload 数量、「路径扫了多少」。

---

## 它解决什么 / 不解决什么

| 要解决 | 不解决 |
|--------|--------|
| 上下文里「当前世界」是什么（对象、未知、反证、实验） | 替你规划整条渗透路径 |
| 工具与状态的机械硬度（HTTP / 实验 / memory 闸） | 自动选漏洞类型 / 技能路由 |
| 减少错误完成信号（有报告 ≠ 测完） | 保证一定挖到洞 |
| 长会话 / 中断后认知可恢复（结论账本 + 证据引用可解析） | 把经验库塞进每轮上下文 |

---

## 四层架构

整个工程只承认四层。不允许靠「再加 Mode / Planner / 知识路由器」变成第五层。

```text
契约 Contract     ← CLAUDE.md + cognition/*.md
知识 Knowledge    ← knowledge/*.md
运行时 Runtime    ← runtime/ + scripts/（执行 / 记账 / 会话开始投影）
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
| `scripts/` | 会话开始投影 / memory 守卫等 hook |

原则：

- 只做 IO、状态、单次投影与记账；
- **不**判断漏洞、**不**选下一步、**不**按关键词加载知识、**不**记录事件日志；
- 保证「LLM 判定过的结论被保存、引用可恢复、目标一致」，不干预 LLM 的探索过程。

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
├── scripts/                  # hooks + 验证工具
│   ├── session_start.py      # SessionStart 单次投影
│   ├── memory_guard.py       # 防裸写热 memory
│   └── validate_persistence.py  # 离线认知持久化验证
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
| `[WORLD MODEL]` | 结论账本（vuln / hypothesis / counter / dead + 证据编号） |
| `[OPEN EXPERIMENTS]` | 当前 target 未关闭实验状态（id / observations / artifacts / hypothesis）；纯展示，不提醒关闭 |
| `[RECENT EXPERIMENTS]` | 当前目标最近实验摘要 |
| `[RESUME]` | **是否具备恢复上下文的投影资格**（有目标且账本非空）——**不是**「继续 / 停止」判决 |
| `[ARTIFACTS]` | 磁盘上是否有 `report.md` / `lessons.md` |

### 3. 其它 hooks

| 时机 | 脚本 | 作用 |
|------|------|------|
| 写文件前 | `memory_guard.py` | 禁止直接改热 memory |

v0.3 起**没有** PostToolUse / Stop hook：工具调用后不重投影、不记录停滞、不干预轮次。世界更新完全由「合并记账 → 下次 SessionStart 投影」驱动。

### 4. `knowledge/` 默认不加载

只有主动阅读才会看到正文。定位是「冷标本 / 案例库」，不是每轮菜单。

---

## 两个重要设计点

### A. 产物完成 ≠ 探索完成

历史上：有了 `report.md` 就关掉恢复投影，等于用 **产物** 冒充 **世界已测完**。

现规则：

- 是否投影恢复上下文，**只看**「有目标 + memory 账本非空」；
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

Memory 回答 **「当前目标已确认的认知资产」**——结论账本，不是操作日志、不是环境截图。

| 前缀 | 含义 |
|------|------|
| `surface:` / `vuln:` / `cred:` | 对象事实 / 已确认漏洞 / 凭据 |
| `tech:` / `port:` / `artifact:` | 技术约束 / 端口 / 证据索引 |
| `hypothesis:`（可选 `[observed\|confirmed\|contradicted\|blocked]`） | 关系 / 未知（带状态） |
| `counter:` / `dead:` | 反证 / 死路 |
| `target:` | 身份 |
| `future:` | 只进 memory，**不投影** |

- 禁止直接改热 memory；走 `echo "DELTA:…" | python runtime/merge.py`。
- 每条少于 3 行，一次不超过 15 行；有观察 / 冲突 / 结论才写，不是每步填表。
- 换目标：DELTA 写新 `target:` → merge 机械归档旧世界。

**结论 vs 事实**：`surface:` 只记对象存在性 / 技术事实。越权 / 绕过 / 泄露等探索**结论**用 `hypothesis [状态]` / `vuln:` / `counter:` 表达，并带 `req_`/`exp-` 证据编号——结论不带证据或引用不可解析时，merge 会打 WARN / WARN HIGH。

**账本是更新不是追加**：同一 target 内，同文本的 `hypothesis` 行会被**替换**（状态升级，如 `observed` → `confirmed` 带证据），不会重复堆积；不同文本则是新条目。

错误示例：`surface: 测试了 /admin`  
正确示例：`hypothesis [confirmed]: 未授权可读他人问卷 (req_747)`

---

## 常用 Runtime 工具

细节以 `CLAUDE.md` 为准。

| 能力 | 入口 |
|------|------|
| HTTP | `python runtime/http_request.py <target> <curl 风格参数…>`（禁止裸 curl 当主路径） |
| 非 HTTP / 脚本 | `python runtime/execute.py <target> --timeout 60 --purpose "…" --cmd "…"`；复杂逻辑先写 `results/<target>/scripts/` |
| 实验 | `create … --activate --hypothesis "…"`；`close … --status success\|failed\|blocked\|unknown --memory-update '{"text":"探索后的结论","status":"confirmed","evidence":["req_747"]}'`（close 自动写回账本） |
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

hooks 由 Claude Code 读取 `.claude/settings.local.json`，模板里是相对路径的 `scripts/` 入口（`settings.example.json`）。

**范围：** 仅授权目标。作者不对未授权扫描或滥用负责。  
**不要提交：** `results/`、`.claude/memory.md`、本机 `settings.local.json`、cookie / VPN / `.env`（见 `.gitignore`）。

---

## 更新日志

### v0.3.1（2026-08）

**证据完整性加固 + Runtime 边界冻结**

#### 三刀清理（冻结前收尾）
- **删除 pattern_counts**：`http_request.py` 移除 v0.2 Observer 残留的响应签名统计（`.pattern_counts.json` / `[OBSERVATION REPETITION]`）。Runtime 不再产生任何事件日志，只剩 `request → save response artifact → metadata → experiment binding`。
- **证据引用完整性（账本 FK 约束）**：`merge.py` 对结论行做三级校验——L1 无证据引用 WARN；L2 引用（`req_NNN`/`exp-NNN`）文件不可解析 WARN HIGH；L3 可解析静默停止。只查存在性，不检查响应内容、不判断是否证明漏洞。
- **hypothesis ledger update semantics**：同 target 内同 normalized text（去状态 / 去证据块 / 压缩空白）的 `hypothesis` 行被**替换**（状态升级），不同文本为新条目——memory 是账本不是日志。

#### Runtime 边界冻结
- **删除 observer 残留**：`merge.py` 移除 `OBSERVE_STATE_PATH` 常量、`reset_observe_on_target_change()` 函数（31 行）、`.observe_state.json` 读写逻辑、`consecutive_none` / `same_action_count` / `prefix_counts` 计数器。
- **[OPEN EXPERIMENTS] 投影**：SessionStart 新增未关闭实验状态展示（id / observations 数量 / artifact / hypothesis），纯状态投影，不提醒关闭、不判断生命周期、不阻塞行为。
- **离线验证工具**：新增 `scripts/validate_persistence.py`，衡量 evidence coverage、hypothesis lifecycle completion、experiment→memory 转化率。
- **架构边界确认**：Runtime 职责收敛为 artifact persistence + cognitive ledger + context projection；不判断停滞、不控制行为、不引入 planner/workflow。

测试收敛：25 tests passed（删除 pattern 回归锁）。

### v0.3.0（2026-08）

**从"行为控制系统"收敛为"认知持久化 Runtime"**

- **删除高频投影**：移除 PostToolUse `assembler.py`（每次工具调用重复注入 3KB Contract）与 Stop `stop_check.py`；hooks 只剩 PreToolUse memory_guard + SessionStart。世界更新由「合并记账 → 下次 SessionStart 投影」驱动。
- **删除停滞传感（v0.3.0）**：`observe.py` / `[OBSERVE]` 段整体移除（对脚本化 HTTP 失明且无接线决策）。v0.3.1 后续清除 `.observe_state.json` Runtime 残留。
- **实验闭环自动写回**：`experiment.py close --memory-update '{"text":...,"evidence":[...]}'` 机械写回 `hypothesis [状态]` + 证据行（target 校验 / 查重 / 写入），消灭"憋大改"手工 DELTA。
- **证据绑定**：merge 对 `confirmed/contradicted/vuln/counter` 无证据引用打 WARN；CLAUDE.md 明确"结论 vs 事实"记账规则。
- **结论账本**：memory 从"对象世界模型"收敛为"已确认认知资产账本"（vuln / hypothesis[状态] / counter / dead + 证据编号）。
- 实测：无投影下 agent 自主完成微前端逆向（某 SRC 目标 A）、签名还原 + AES/DES 解密（某 SRC 目标 B），结论均带证据落账。

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

**象雄 = 在 Claude Code 上给黑盒渗透一个「认知持久化 + 最小摩擦」的 Runtime；  
不控制 Agent 怎么探索，只保证它想清楚的结论不丢失——  
优化的是过早收缩世界的偏差，不是堆更多漏洞菜单。**

---

## 开发者声明

这是个人对 LLM Agent 认知架构的探索，重点在"如何避免模型过早闭合世界"。

设计取舍（行为优先、对象延迟、反因果表达）基于有限实验，可能存在未发现的问题。欢迎讨论与质疑。
