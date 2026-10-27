# ARCHITECTURE.md — 开发者宪法

## 优化什么

**不优化 Agent 知识量，优化 LLM 在不确定环境中的状态保持能力。**  
失败常是过早收缩世界（错合 / 错丢 / 错完成），不是缺漏洞菜。

## Prompt 演化（守门）

任何进入热路径（CLAUDE / cognition / SessionStart / 反压缩 7 行）的文本，必须回答：

> 它是否会改变 LLM 的下一步推理选择？

| 答案 | 处置 |
|---|---|
| 不会 / 只是正确废话 / 给人看的总结 | 删，或只进 ARCHITECTURE |
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
| Failure priors | 已验证 | `CLAUDE.md` 末尾固定 7 行反压缩（2026-09-17 由 `project.py` 迁出） |
| Compression guards | 已验证 | `cognition/object_model` 同一性 + CLAUDE 完成/记账句 |

**已退役，勿重建：** `knowledge/false_closures.md` 正文（价值已兑现进 7 行投影，正文跨全部会话 **0** 读取）与 `knowledge/*_objects.md`（领域切分标本，跨全部会话 **1** 次读取）。两者零代码引用、无强制 Read 点，删除不改任何投影。

`knowledge/` 现留两份，**都靠"强制 Read 点 + 冷存储"活着**，判据不是"有用"，而是"只在某一时刻有用"：

| 文件 | 读取点 | 为什么不在热路径 |
|---|---|---|
| `bounty_criteria.md` | 写 `report.md` 前（CLAUDE.md 强制；跨会话 13 次读取） | 进/不进 report 的过滤判据 |
| `report_spec.md` | 同上，同一次阅读 | 报告**格式**契约。2026-09-18 静态指令审计：它原先 643 字符挤在 `CLAUDE.md` 里每轮都在场，却只在收口那一刻有用（见规格 §8.0l） |

反例判据（两份都满足）：**有强制 Read 点**（不是"希望 agent 想起来去看"）且**正文不可压缩进热路径**。

### Runtime

Runtime 是机械层，只负责状态、IO、工具与有界上下文投影。SessionStart 只按常量顺序拼接**动态世界状态**并自限于注入预算；静态认知文本（cognition 四文件 + 反压缩 7 行）由宿主随 `CLAUDE.md` 注入，不占 hook 预算。它不选择、不解释、不路由 cognition，也不加载或选择 knowledge。

**投影只在 SessionStart 注入一次 → 账本会过期。** 所以 `runtime/digest.py` 持有投影的
「去哪 + 做什么」两段（`[GOAL]` / `[NEXT]`）的**唯一实现**，`project.py`（会话开始）
与 `merge.py` / `experiment.py`（记账后回吐）共用它 —— 判据机械（`[GOAL]+[NEXT]`
前后逐字节比较），只加 `surface:` 不触发，改动派发队列才触发。两处各写一份渲染会让
`[NEXT]` 出现两个版本；本仓库已在此类"复制粘贴的实现"上翻过两次车
（`protocol_scan`/`diff_scan` 的 status 归一化、`EVIDENCE_BLOCK_RE` 的三份副本）。

**memory 是读-改-写，有两条写入路径。** `merge.py`（DELTA 应用）与
`experiment.py`（`close` 写回）共用 `merge.memory_transaction()`（`filelock.py`，`.memory.lock`）。
两条路径并发是常态（agent 把"记一笔账 + 关一个实验"放进同一消息的两次并行调用），
无锁即 lost update —— 而记账是这套系统里唯一让探索可累积的通道。

**SessionStart 是认知接口（投影），不是完成判决器：**  
`[RESUME]` 已于 2026-09-18 投影审计**删除**（它的 target 与 `[GOAL]` 段头重复、计数与 `[TERRAIN]` 重复）。它原本承载的不变量**仍然成立**：`report.md` / `lessons.md` 只进 `[ARTIFACTS]` 元数据（present|absent），**不得**充当完成判据或关闭任何段。Runtime 可陈述计数与文件存在；不可输出 `exploration_complete` 类判断。停不停是 Agent 的事。

**Completion Evidence ≠ Exploration Completion（分析语言，非 schema）：**  
`vuln:` / 单对象边闭合 ≠ 整个 target world 闭合。Object closure ≠ World closure。产物写完是 Artifact 事件，不是 World State transition。**探索完成是条件性判断**（依赖当前世界状态），不是永久事实。

**cmd / a-05 标本 — Exist vs Relevance（人侧，非模块）：**  
- **cmd（该升的未升）：** 同一攻击面在不同世界状态下对「重要关系」的影响不同；高危读链已够写报告后，权限/可达/控制假设变了仍继承旧注意力 → 写/配置边不重开。不是缺攻击知识，不是 getshell 清单。  
- **a-05（Exist≠Relevance）：** 会管理 unknown 的**存在**，不会管理「下一步该被什么牵引」；LFI 等已饱和后，仅因未观察而存在的路径仍占预算 → 搜索爆炸。廉价「可能有用」伪连接不算。不是「深度税必须 40 分钟」，不是应加 Fast Mode。  
- **统一：** 阶段一防把世界压扁（Identity/Evidence/Completion/stale prior）；阶段二防把所有可能性等权展开。未知可保留≠自动优先投入；是否优先取决于是否可能改变当前目标下的重要对象关系、约束或假设。Contract 完成语义只留该认知句；**禁止** value/priority/score/relevance 字段、EV 引擎、Mode/Skill、unknown ranking。Collapse 名仅人侧，**不进**热路径。本阶段 CLAUDE 该句后**冻结**扩写，下步行为回归。

**`[ANTI-COMPRESSION]` = exposure，不是 trigger：**  
`CLAUDE.md` 末尾固定 7 行短镜头（2026-09-17 由 SessionStart 投影迁出，改随 system prompt 注入）：**5** 行防错误压扁（相似标识≠同一对象、相关行为≠已证边界、单路径成功≠邻接面穷尽、缺观察≠不存在、被阻塞面≠已排除面）+ **2** 行支持后行动约束（已支持解释约束下一实验；解释边界外的保留未知不享等权）。**不**加载任何知识文件正文、**不**加条、**不** Router、**不**按 vuln 事件调用；**不**引入 False Openness / planner / score。投影顺序为 attention 顺序（TERRAIN 形状 → FRONTIER 该做什么 → EXCLUDED 别做什么 → WORLD MODEL 定局），非 schema。hit = 高支持解释后下一跳偏向确认/细化/证伪该解释，而非无关扩未知。母问题：什么有资格改变当前世界模型与下一步（artifact 假完成 / 知识未进入 / 相似当同一 / 支持后拒绝正确收窄）。

**World boundary（memory 生命周期）：**  
Active memory（`.claude/memory.md`）表示唯一 current world；`results/<target>/memory.md` 为 archived world。`merge.py` 在 `target:` 身份变化时机械执行 archive → clear → init，不判断根域/公司/是否继承。标签 `target:` 本身不等于隔离——隔离由 Runtime 闸保证。

**Memory 不是什么：** Memory 是世界状态压缩，不是操作日志或待办清单。

错误：surface: 测试了 /admin / hypothesis: 测试 SQL 注入
正确：hypothesis [contradicted]: 存在 admin 后台 / hypothesis: search 参数可能存在注入

判断：Memory 回答"世界是什么状态"，不是"我做了/要做什么"

**跨 session recovery（宿主依赖）：**  
SessionStart 只读 `.claude/memory.md`（active world）。新 session 时若 memory 为空，SessionStart 输出空 WORLD。**恢复机制依赖宿主**：宿主需在 session 切换时保持 memory.md 不清空，或用户手动从 `results/<target>/memory.md` 恢复到 `.claude/memory.md`。SessionStart **不**推断历史、**不**自动恢复 archive、**不**判断 target 继承关系。

`resources/` 可存放 Runtime 使用的机械资源（如 `resources/wordlists/` 下的中性 surface path/token prior）。它不是第五认知层：只服务采样覆盖，不包含漏洞利用步骤，不替代 Knowledge 或 Agent 推理。

**Projection = 认知接口（v0.3 宪法，贯穿 Runtime 与 Contract）：**

**核心不变量：**
> **Projection 可以改变 LLM 看世界的方式，但不能改变 LLM 认为世界是什么。**

Projection 不是数据展示层，不是"优化后的 memory"，也不是"第二个 World Model"——它是 LLM 当前认知循环的**输入接口**。它的价值不是压缩信息，而是降低 LLM **从已有事实重新构建当前世界状态的成本**。它处于观察与解释的边界：

```
Runtime：观察 → 整理 → 展示
LLM：   解释 → 假设 → 实验 → 确认
```

**七条宪法：**

1. **Memory 是唯一世界状态。** projection 永远不新增、不修改、不删除任何 memory 语义。

2. **Projection 是认知透镜，不是世界状态。** 删除任何 projection，memory 不变、world 不变，只是阅读成本增加；若删除后 LLM 无法恢复世界，说明该 projection 已越界。

3. **Projection 可重排与压缩结构，但不得引入语义解释。** 合法形态：`memory → WorldView → format_world() / format_hypothesis() / format_object()`。非法形态：`hypothesis interpretation → object inference → frontier generation`——后者把 LLM 本应完成的解释过程固化进 Runtime。

4. **每个 projection token 必须直接回指 memory，或由 memory 机械派生。** 两类派生：
   - **Structural Derivation（允许）**：`count: 5` / `state: confirmed` / `source: hypothesis row #12`——任何实现机械得到同一结果（`len(hypotheses)`）。
   - **Semantic Derivation（禁止）**：`DC01 is high value target` / `this host is likely domain controller`——这是 interpretation，不是 projection。
   - **规则：Projection may compress structure. Projection must never compress meaning.**

5. **投影函数相互独立：** 所有 projection 从 WorldView 派生，**从不从其他 projection 派生**。composer 只做顺序拼接，不做语义级联。禁止"投影链投影"。

6. **Derived projection 不得被视为权威。** 任何结论必须引用 memory/hypothesis，不得单独引用 projection。LLM 停止回看 surface/hypothesis/experiment 而直接基于 Object View 推理，即为第二事实源。

7. **Object/relation/frontier projection 可暴露不确定性，但绝不可消解不确定性。**

**分组即语义操作（Object projection 的特殊风险）：**
- **允许**展示候选聚合：`SURFACE CLUSTER CANDIDATE — members: [10.0.0.5, DC01.local] — boundary: hypothesis [observed]: A ? B`
- **禁止**展示确认对象：`Object: DC01 — smb, ldap, kerberos`——"Object" 这个名字已暗示归并完成，视觉结构会让未确认关系看起来已确认，**Object projection 自身制造 closure**。
- 边界：投影可展示"候选聚合"（未确认分组 + boundary hypothesis 状态），不可展示"确认对象"（归并结论）。

**分层（按"是否属于世界状态本身"划分，非重要性）：**
- **Core Projection（默认注入）**：TERRAIN + FRONTIER + EXCLUDED + WORLD MODEL + UNEXPLAINED——描述"世界有多大、哪些地方未确定、哪些方向已排除"，是 memory 的直接认知接口；删除后 LLM 无法工作。
- **Derived Projection（按需 Read）**：OBJECT / RELATION——描述"如何组织世界"，不是世界本身；删除后只是扫描成本增加。**不默认注入、不参与 action、不产生新 memory、不作为其他 projection 输入。**

**AVAILABLE PROJECTIONS（目录，必须极度弱化）：**
```
[AVAILABLE PROJECTIONS]
object_projection: exists
frontier_projection: absent
Read only. No recommendation provided.
```
- 命名用 **projections** 而非 views——强调这是 memory 的排列方式，不是能力。
- 目录不携带统计（`4 objects` 已暗示"对象数量是重要维度"），不排序，不推荐。统计若需要，留在 projection 内部。
- 目录的存在性与注入行为分离：Runtime 列出"有哪些排列可选"，选择权全程在 LLM。

**Projection Closure 最终判据：**
projection 的结构不得让未确认关系看起来已确认。验证方法：对每个投影段问——"这个分组/计数/省略是否暗示了 memory 中不存在的确定性？"若是，即 closure。

### Artifacts

`results/` 保存目标相关响应、实验 metadata、证据、脚本、报告与复盘。Runtime 可读写 Artifact 路径和 metadata，但不解释目标内容。现有 Experiment 数据模型保持不变。目标侧可选用 `results/<target>/wordlists/` 存放本场展开/裁剪结果。

Artifacts 与 World State **并列**，禁止 Artifact 反向充当 completion prior（历史 bug：`report` 存在则不投影 `[RESUME]`；该段已于 2026-09-18 删除，但这条禁令对 `[ARTIFACTS]` 与 `[NEXT]` 同样适用）。

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

禁止：只有摘要表、只有「可能」、只有结论无请求包。多洞可同文件，须逐洞拆开。

以上为**人读的摘要**；Agent 侧的格式契约在 `knowledge/report_spec.md`（收口时随 `bounty_criteria.md` 一起 Read，由 `CLAUDE.md` 与投影 `[ARTIFACTS]` 双向指向），**不在此重复 SOP**。

`[ARTIFACTS]` 段头当前形如：`[ARTIFACTS] — 收口产物；写前先 Read knowledge/report_spec.md（格式）与 knowledge/bounty_criteria.md（过滤）`。

## 分层边界

- 好推理的契约或通用理解镜头属于 Contract。
- 领域对象知识属于 Knowledge。
- 状态、IO、Hook 与工具属于 Runtime；`resources/` 为 Runtime 的数据附件。
- 目标相关事实与执行产物属于 Artifacts。
- 无法归入四层的能力不得新增为另一层。

Runtime 可以固定拼接文本，但不能理解业务、判断漏洞、组织推理或自动选择领域知识。Knowledge 与 cognition 都不得成为 Router、Planner、Scheduler、Mode 或 SOP。
