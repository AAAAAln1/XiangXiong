# Maintainer — 给改仓库的 Agent

不是渗透 `CLAUDE.md`。不是架构炫文。  
职责：在现有哲学下，系统更稳、更简单、更一致。真 Target/lessons 驱动。禁止为优雅重设计。

## 写提示词的铁律（先于一切）

**站在 LLM 角度：能改下一跳的留下，漂亮话砍掉。**

这是**修改 Agent 的人的评价函数**，不是渗透 Agent 规则。  
**禁止**写入 `CLAUDE.md` / `cognition/*` / SessionStart 投影。详：ARCHITECTURE「Prompt 演化」。

- 改热路径（CLAUDE / cognition / SessionStart / FC 索引）时：每句问「删了，下一动作变吗？」不变→删。
- 金句、哲学、演进史、正确废话 → 本文或 ARCHITECTURE。
- 有用 = 工具怎么敲、账怎么记、何时必须 `hypothesis:`、何时换边、status 合法值。有害 = 读完点头、行为照旧。
- 解释 ≠ 控制。只让人觉得正确的句子 = 漂亮话。

## 读者分流（写任何字之前）

| 读者 | 文件 | 写什么 |
|---|---|---|
| 渗透 Agent | CLAUDE、cognition、SessionStart 投影、FC 索引 | 改**下一跳行为**的短句/事实 |
| 渗透 Agent 冷 | FC 正文 | 关系语言；无步骤菜 |
| 人/维护 | ARCHITECTURE、README、本文、`*_objects`（frozen） | 边界、拒绝项、怎么改仓库；frozen 不是 Agent 知识 |

**禁：把给人看的叙事、禁令散文、演进史塞进 CLAUDE 或 SessionStart。**

## LLM 侧：什么有用 / 什么有害

**有用（促思/改行为）**

- 缺口有名字：`hypothesis:` / unknowns 非空或显式 `(none)`
- 冲突：两种解释预测不同
- 不完整：截断、标签≠body、磁盘上有全文
- 便宜下一跳：少仪式、工具不炸、落盘路径稳
- FC **索引在场**（固定整表，不点菜）→ 怀疑入口
- 外部量：consecutive_none / same_action（换 target 须清零）

**有害（阻判断/假闭合）**

- 开场/测试 **SOP 清单**（先测A后测B）
- 条件→加载某类 knowledge / skill（Router）
- 长正确废话、身份散文、重复金句
- 条数膨胀的 FC/百科（扫不动→回默认剧本）
- 转向税：必 create/close 才敢试；Windows 引号反复失败
- 完成叙事：路径扫完=测完；report 存在=测完；软失败当 vuln
- 「可选读」当主方案（统计上等于没有）
- 惩罚型禁令代替关系语言（「禁止弱口令」vs「选人≠认证」）
- 未知注水（checklist 式 hypothesis）；相似命名默认合并对象

改完再问一次：删这句，下一跳变吗？不变→删。

## 评价不变量（非新模块）

Open World 不是 Mode/目录/知识文件。评价时问：是否在证据不足时降低了世界模型自由度（错合对象 / 错合关系 / 错合完成）？目标是 **minimize unjustified compression**，不是 maximize unknown。  
工程落点仅：`cognition/object_model` 同一性、`knowledge/false_closures`、CLAUDE 完成/记账句。禁：`object_closure.md` / `open_world.md` / compression 菜 / Active Pool。

## Knowledge 生命周期（非新目录）

```
实验假设 → frozen artifact → (重复 miss + 行为改善) → promoted prior → runtime knowledge
```

**未证明能改变世界模型更新行为的先验，无资格进 runtime knowledge；也不得因此删光未验证假设。**

| 类 | 状态 | 落点 |
|---|---|---|
| Failure priors | 已验证 | FC — **first validated knowledge pattern**，不是唯一可能 |
| Compression guards | 已验证 | object identity + Completion Contract |
| World partition priors | frozen | `*_objects` — 标本，非 Agent 知识 |

`*_objects`：不删正文、不投 SessionStart、不 Router、不按栈点菜。晋升须三条：跨 Target 重复 miss；boundary failure 或 partition 形态（非纯 catalog）；可观察行为改善。晋升后形态须是 behavior-changing prior（failure 与/或 partition），不得停留 ontology。

object_model ≠ domain partition：前者勿错误合并；后者「领域该怎么切」——后者未证热路径。

## 改之前

1. 真问题？哪个 Target/日志？重复？不改怎样？  
2. 哪一层？Contract/Knowledge/Runtime/Artifacts  
3. 删>移>重写>增  
4. 是否新层/SOP/Mode/skill 路由/点菜？是→拒  

顺序：Runtime→Tool→可见性→Knowledge→CLAUDE→ARCHITECTURE。

## Runtime

可：IO、Hook、投影、状态、工具硬度。  
不可：判洞、定下一步、按关键词选 knowledge。

**Active memory = 唯一 active world。**  
`.claude/memory.md` 是 working tree，不是历史仓库；跨 target 只在 `results/<target>/memory.md`。  
`merge.py`：DELTA 新 `target:` 与 active 不一致 → 机械 archive → clear → init；不判断根域/公司/是否继承 hypothesis。禁止用 CLAUDE checklist 代替此闸。

FC：T1 MUST 投 Index；T3 可再钉同一 Index；T2 不做；正文冷。  
FC **v0.1 frozen**：默认不加条、不 ontology 进热路径；v0.2 要 hit/miss。验证看：是否改 Experiment、是否减错误闭合、Unknown 是否可实验。

## Section Renderer / Composer Separation

Projection 责任分两层，禁止混合：

- **Renderer**（`format_world_section` / `format_unknown_sections` 等）：只负责「一个 section 怎么展示」；不读业务来源之外的拼装、不知道文档位置。
- **Composer**（`compile()` / `assemble()` / `_render_world_from_view`）：只负责「各 section 放哪、事件如何组合」；必须保持历史 attention contract（Phase0 金样）不可漂移。

**禁止创建这种 helper：读源数据 + 渲染多个 section + 拼接成 LLM-facing 片段** —— 名字像 projection、实际是隐藏 composer，复用即悄悄改掉段序（Phase 2.0 事故即此）。命名向 `project_xxx()` / `get_world_context()` 靠拢的「大而全」接口一律拒。

其它规则：

- 同一世界数据可以有多个展示（如 SessionStart RECENT 与 PostTool RECENT 窗口不同）。**same data source ≠ same projection contract**；不要因为「都是 recent」顺手统一——统一是又一次 Phase 2.0。
- 新增 section renderer 可以；**新增 `format_full_xxx_context()` 类聚合函数**不可以（会重建黑盒与隐式顺序）。
- 共享「section 语义」合理；共享「文档组合顺序」危险。

## Observation Cache 边界（`.pattern_counts.json`）

`results/<target>/.pattern_counts.json` 是 **工具观察缓存**，不是世界状态：

- 只回答「同一响应签名出现过几次」（`(request_sig, response_sig, occurrences)`），**不回答** stable / blocked / saturated / 下一步。
- 可删、可重建、有界；**不进** merge / world projection / session_start 注意力 / observe 模式。
- 禁止演化：merge 读它、projection 展示它、session_start 按它改 attention、observe 按它自动切 mode——否则观察缓存会变成**隐藏世界模型**。
- 实验开关：`XIANGXIONG_OBS_REPEAT=0` 关闭（A/B 对照用）。

## Observation Artifact Contract（物理元数据）

每个被展示的载荷流，tool 首包一阶暴露 `{path, size, truncated}`（预览≠全文）：

- execute：`stdout_*` / `stderr_*`；http：`body_*`（与 `saved`/`length` 并存）。
- 属 Observation 物理层，**不是** experiment conclusion/status。
- **不进** world / merge / SessionStart / observe 决策；禁止 should_read / 自动 tail / 重要性。
- Runtime Minimal = 无认知逻辑 + 记录可观察；≠ 少暴露入口。

## Turn Event Telemetry（`.turn_state.json`）

**Add missing Turn event telemetry** — 不是 idle detection；不解决 Host generation timeout。

- Stop 独占：`turn_tool_use_count` / `turns_without_tool_use`；仅 count==0 时输出两数字。
- 与 target-loop（`.observe_state` / `consecutive_none` / `stagnation_level`）分离。
- **禁止**：idle/watch/stuck/level；因无 tool 的 `decision:block`；SessionStart/WORLD/DELTA；改 observe.py。
- 换 target：`reset_observe_on_target_change` 机械清零 turn state。
- stop_check 两类职责冻结：contract enforcement vs passive observation；后者不得影响前者。

## 蒸馏 FC 正文

`你默认/实际/还不知道`。四闸：关系；非 surface 可证；去 Target 仍成立；非清单。映射旧条优先。v0.1 冻结期优先记 miss，不预填 Dependency/Quantity/漏洞菜。

## 渗透 CLAUDE 只留

工具怎么敲、账怎么记、截断≠全文、卡住换边、完成勿谎称穷尽、status 合法值。  
不留：架构故事、开发禁令表、演进理由。
