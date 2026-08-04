# ARCHITECTURE.md — 开发者宪法

## 优化什么

**不优化 Agent 知识量，优化 LLM 在不确定环境中的状态保持能力。**  
失败常是过早收缩世界（错合 / 错丢 / 错完成），不是缺漏洞菜。

## Prompt 演化（守门）

任何进入热路径（CLAUDE / cognition / SessionStart / FC 索引）的文本，必须回答：

> 它是否会改变 LLM 的下一步推理选择？

| 答案 | 处置 |
|---|---|
| 不会 / 只是正确废话 / 给人看的总结 | 删，或只进 ARCHITECTURE / MAINTAINER |
| 只是领域经验、未证 hit/miss | 冷知识或 frozen，不进热路径 |
| 改变观察/记忆/记账/推理状态（下一跳） | 可留 |

**站在 LLM 角度：能改下一跳的留下，漂亮话砍掉。**  
此句是**改仓库的人的评价函数**，不是渗透 Agent 行为规则；禁止写入 `CLAUDE.md` / `object_model`。

## 四层模型

整个工程只有四层，不允许出现第五层。

```
Contract   ← CLAUDE.md + cognition/*.md
Knowledge  ← knowledge/*.md
Runtime    ← runtime/ + scripts/
Artifacts  ← results/
```

### Contract

`CLAUDE.md` 是 core contract：工具、记账、完成边界——短句、可执行。`cognition/*.md` 是 reasoning lenses（如何理解世界），不是第五层，不含 SOP/领域操作菜。热路径只留改下一跳的约束。

### Knowledge

Knowledge 不是百科目录，而是先验生命周期：

```
实验假设 → frozen artifact → (重复 miss + 行为改善) → promoted prior → runtime knowledge
```

**只有被证明能改变世界模型更新行为的知识，才配进入 runtime。** 未证明的先验不得伪装成 Agent 指令。

维护者分类（脑中，不增目录）：

| 类 | 状态 | 仓库落点 |
|---|---|---|
| Failure priors | 已验证 | `knowledge/false_closures.md`（FC 是**首个**已验证 knowledge pattern，不是唯一可能） |
| Compression guards | 已验证 | `cognition/object_model` 同一性 + CLAUDE 完成/记账句 |
| World partition priors | 实验标本 | `knowledge/*_objects.md` — **frozen / non-runtime** |

`*_objects`：历史领域切分实验。不自动注入、不 Router、不进 SessionStart、不按技术栈点菜。晋升须可观察行为改善；形态须是 behavior-changing prior（boundary failure 与/或 world partition），不得停留在 ontology 百科。

object_model 与 domain partition 不竞争：前者是元认知（勿错误合并）；后者是「该领域应有哪些对象边界」的假设——后者尚未证明热路径消费方式。

### Runtime

Runtime 是机械层，只负责状态、IO、工具与有界上下文投影。SessionStart 按常量顺序原样拼接明确的 cognition 文件，报告缺失或读取错误后继续；它不选择、不解释、不路由 cognition，也不加载或选择 knowledge。

**SessionStart 是认知接口（投影），不是完成判决器：**  
`[RESUME]` = 是否给模型恢复上下文（target + world 桶非空），**不是** Continue Decision。`report.md` / `lessons.md` 只进 `[ARTIFACTS]` 元数据（present|absent），**不得**关闭 `[RESUME]`。Runtime 可陈述计数与文件存在；不可输出 `exploration_complete` 类判断。停不停是 Agent 的事。

**Completion Evidence ≠ Exploration Completion（分析语言，非 schema）：**  
`vuln:` / 单对象边闭合 ≠ 整个 target world 闭合。Object closure ≠ World closure。产物写完是 Artifact 事件，不是 World State transition。**探索完成是条件性判断**（依赖当前世界状态），不是永久事实。

**cmd / a-05 标本 — Exist vs Relevance（人侧，非模块）：**  
- **cmd（该升的未升）：** 同一攻击面在不同世界状态下对「重要关系」的影响不同；高危读链已够写报告后，权限/可达/控制假设变了仍继承旧注意力 → 写/配置边不重开。不是缺攻击知识，不是 getshell 清单。  
- **a-05（Exist≠Relevance）：** 会管理 unknown 的**存在**，不会管理「下一步该被什么牵引」；LFI 等已饱和后，仅因未观察而存在的路径仍占预算 → 搜索爆炸。廉价「可能有用」伪连接不算。不是「深度税必须 40 分钟」，不是应加 Fast Mode。  
- **统一：** 阶段一防把世界压扁（Identity/Evidence/Completion/stale prior）；阶段二防把所有可能性等权展开。未知可保留≠自动优先投入；是否优先取决于是否可能改变当前目标下的重要对象关系、约束或假设。Contract 完成语义只留该认知句；**禁止** value/priority/score/relevance 字段、EV 引擎、Mode/Skill、unknown ranking。Collapse 名仅人侧，**不进**热路径。本阶段 CLAUDE 该句后**冻结**扩写，下步行为回归。

**`[ANTI-COMPRESSION]` = exposure，不是 trigger：**  
SessionStart 投影固定 6 行短镜头：**4** 行防错误压扁（相似标识≠同一对象、相关行为≠已证边界、单路径成功≠邻接面穷尽、缺观察≠不存在）+ **2** 行支持后行动约束（已支持解释约束下一实验；解释边界外的保留未知不享等权）。**不**加载 `false_closures.md` 正文、**不**加条、**不** Router、**不**按 vuln 事件调用；**不**引入 False Openness / planner / score。FC 知识库仍冷。WORLD MODEL 投影顺序为 attention 顺序（objects/constraints/counters → recent experiments → unknowns 殿后），非 schema。hit = 高支持解释后下一跳偏向确认/细化/证伪该解释，而非无关扩未知。母问题：什么有资格改变当前世界模型与下一步（artifact 假完成 / 知识未进入 / 相似当同一 / 支持后拒绝正确收窄）。

**World boundary（memory 生命周期）：**  
Active memory（`.claude/memory.md`）表示唯一 current world；`results/<target>/memory.md` 为 archived world。`merge.py` 在 `target:` 身份变化时机械执行 archive → clear → init，不判断根域/公司/是否继承。标签 `target:` 本身不等于隔离——隔离由 Runtime 闸保证。

**Memory 不是什么：** Memory 是世界状态压缩，不是操作日志或待办清单。

错误：surface: 测试了 /admin / hypothesis: 测试 SQL 注入
正确：hypothesis [contradicted]: 存在 admin 后台 / hypothesis: search 参数可能存在注入

判断：Memory 回答"世界是什么状态"，不是"我做了/要做什么"

**跨 session recovery（宿主依赖）：**  
SessionStart 只读 `.claude/memory.md`（active world）。新 session 时若 memory 为空，SessionStart 输出空 WORLD。**恢复机制依赖宿主**：宿主需在 session 切换时保持 memory.md 不清空，或用户手动从 `results/<target>/memory.md` 恢复到 `.claude/memory.md`。SessionStart **不**推断历史、**不**自动恢复 archive、**不**判断 target 继承关系。

`resources/` 可存放 Runtime 使用的机械资源（如 `resources/wordlists/` 下的中性 surface path/token prior）。它不是第五认知层：只服务采样覆盖，不包含漏洞利用步骤，不替代 Knowledge 或 Agent 推理。

### Artifacts

`results/` 保存目标相关响应、实验 metadata、证据、脚本、报告与复盘。Runtime 可读写 Artifact 路径和 metadata，但不解释目标内容。现有 Experiment 数据模型保持不变。目标侧可选用 `results/<target>/wordlists/` 存放本场展开/裁剪结果。

Artifacts 与 World State **并列**，禁止 Artifact 反向充当 completion prior（历史 bug：`report` 存在则不投影 RESUME）。

`report.md` 是给人/平台读的产物（非 Runtime 知识）。写报告时按 **一洞一节**，字段对齐常见 SRC/漏洞盒子表单：

| 平台字段 | report 落点 |
|---|---|
| 漏洞标题 | 节标题：`域名/系统-功能点-漏洞类型` |
| 漏洞域名 | 节内「域名」 |
| 漏洞类型 | 节内「类型」（如 Web/逻辑） |
| 漏洞等级 | 严重/高危/中危/低危 |
| 漏洞简述 | 概况+危害；URL/Payload 不进简述 |
| 漏洞 url/功能点 | **完整绝对 URL**（`https://host[:port]/path[?query]`），禁止只写 path；可另附功能导航 |
| 复现步骤 | 分步；每个 HTTP 步含完整原始请求包（`METHOD /path HTTP/1.1`+`Host`+Header+空行+Body，可粘 Burp Repeater）+响应要点+证据指针 |
| 修复建议 | 可落地措施，优先对开发友好 |

禁止：只有摘要表、只有「可能」、只有结论无请求包。多洞可同文件，须逐洞拆开。Agent 侧必填约束在 `CLAUDE.md` 收口句，不在此重复 SOP。

## 分层边界

- 好推理的契约或通用理解镜头属于 Contract。
- 领域对象知识属于 Knowledge。
- 状态、IO、Hook 与工具属于 Runtime；`resources/` 为 Runtime 的数据附件。
- 目标相关事实与执行产物属于 Artifacts。
- 无法归入四层的能力不得新增为另一层。

Runtime 可以固定拼接文本，但不能理解业务、判断漏洞、组织推理或自动选择领域知识。Knowledge 与 cognition 都不得成为 Router、Planner、Scheduler、Mode 或 SOP。
