# Surface generation prior（路径覆盖，不是攻击知识）

## 用途

补 **surface 漏采样**（recon generation），让容易被命名偏见跳过的 path/token 有机会进入观察集合。

**不是：**

- 漏洞利用步骤 / PoC / payload
- 领域 Knowledge（勿与 `knowledge/*.md` 混淆）
- 默认全站爆破引擎

## 文件

| 文件 | 说明 |
|------|------|
| `surface_tokens.txt` | 一行一个 token；与已观察后缀/前缀机械组合 |
| `surface_paths_mini.txt` | 短路径/文件名；可直接拼到 origin 后探测 |

## 推荐用法（两阶段，先想后补）

路径/入口发现不要一上来整表狂扫：

1. **阶段 A — 内生 recon**  
   首页、登录页、JS、robots、响应内链接与自拟少量路径（基于已观察 tech）。  
   有预算上限（请求数或「主入口成本已升高」）。

2. **阶段 B — prior 补充（必做补漏）**  
   加载本目录小表，与阶段 A **去重** 后探测；**限流分批**（建议单轮展开后探测上限约 500～1500，可按目标再收）。  
   禁止默认挂载外部整库（OneList / 数十万行目录字典）。大库仅显式加深时 opt-in。

3. **阶段 C — 认知**  
   对异常/高价值 surface 建可证伪 hyp 与小 experiment。  
   命中结果只记 `surface:`；名称暗示的功能不是 object 事实。

## 维护

- 只加 **中性、跨站可复用** 的易漏面；禁止回流具体 exploit 参数串。
- 保持小：token 数百级、paths 一两千级以内。
- 大字典原料可留仓库外（如 `F:\@@sec_tools\一些字典`），仅裁剪结果进本目录或 `results/<target>/wordlists/`。
