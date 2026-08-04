# Object Model

## 对象 vs 非对象

对象是业务实体或能力边界，不是技术资源或漏洞类型。

正例：authentication boundary / OSS AK 权限边界
反例：/admin 路径（这是 surface）/ IDOR（这是关系类型）

---

对象用 attributes、relations、constraints、unknowns 描述。漏洞名是判断不是对象。关系要证。

**Unknown = 账上说不清的关系**（可差分证实/证伪），不是兴趣列表、不是洞名待办。

**Observation vs Fact：** 单次观察不是事实结论。观察 → hypothesis → 验证 → 事实。例：server: nginx/1.18 是 observation，不等于存在 nginx 漏洞。

**Experiment：** 用于消除会影响决策的不确定性，不是固定流程。单次探测不需要 create。

**冲突即记账：** 观察和当前模型打架或说不清 → 立刻留下可证伪 `hypothesis:`；禁止因「没马上打成」丢掉。可换主线，不可因新故事遗忘未闭合 hypothesis。

**对象同一性：** 路径 / query act / 函数名 / 近义业务名默认不是同一对象；合并要关系证据。回答「是否测过 X」须同一资源标识（path/method 等），近义名不顶替。

**Object Identity 判断：** 多个 surface 是否属于同一对象，取决于是否共享同一失败机制。

若多个 surface 在同一 Trust Boundary 内、服务同一业务能力、对同类攻击以相同方式失败 → 同一对象的多个入口，不必产生多条 hypothesis。

例：`/user/profile` 和 `/api/user/info` 若受同一认证机制保护 → 同一对象；`/admin/user/info` 需要管理员权限 → 不同对象，不同 Trust Boundary。

对象有邻接（上下游、入口、表示）。发现 A 不自动证明邻居存在。多入口是否同一业务对象、校验是否一致 = unknown。

目录/路由表/规范类 artifact：Known 与 Unknown 同时增加；有 vuln 也不等于未知已空。

多条结论可共享同一未证 premise；premise 倒了，依赖它的边要重估。证据不因被引用多次而变独立。

**Hypothesis 表达：**

一条 hypothesis = 一个核心关系。

目标：让模型快速理解未知关系，而不是拆成机械任务。

避免：
  "OSS AK 是否泄露数据并且可以访问其他 bucket/RAM/写入"

优先：
  "OSS AK 权限边界"
  
验证后记录：
  "List/Get/Put 成功，Delete 失败"

**但避免过度拆分：**

错误：
  - OSS AK 是否 List
  - OSS AK 是否 Get
  - OSS AK 是否 Put
  - OSS AK 是否 Delete

这些属于同一对象能力边界。

拆分依据：
  - 不同对象 → 可以拆
  - 不同关系 → 可以拆
  - 同一对象不同能力面 → 可保持一个 hypothesis
