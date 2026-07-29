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

## 蒸馏 FC 正文

`你默认/实际/还不知道`。四闸：关系；非 surface 可证；去 Target 仍成立；非清单。映射旧条优先。v0.1 冻结期优先记 miss，不预填 Dependency/Quantity/漏洞菜。

## 渗透 CLAUDE 只留

工具怎么敲、账怎么记、截断≠全文、卡住换边、完成勿谎称穷尽、status 合法值。  
不留：架构故事、开发禁令表、演进理由。
