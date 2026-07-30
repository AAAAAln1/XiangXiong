# 赏金SRC报告过滤标准

**定位：** report写作约束，不是漏洞知识库。冷知识，写report时Read。不投SessionStart，不Router。

**原则：** memory保留完整世界（所有surface/vuln/hypothesis/counter）；report仅输出赏金价值信号。

**判断标准：** Impact + Exploitability，不是漏洞类型。

---

## 进入report（优先）

### 认证与授权
- 未授权读写**关键数据**（用户信息/订单/支付/后台管理）
- 越权（水平/垂直）修改他人数据或访问管理功能
- 账户接管（密码重置缺验证、Session固定、JWT伪造）
- 暴力破解**无有效限流**（无验证码且无账户锁定）

### 代码执行与注入
- SQL注入（有回显或时间盲注可证）
- RCE（命令注入/模板注入/反序列化）
- SSRF（可访问内网关键服务或云metadata）
- XXE（可读文件或SSRF）
- 文件上传getshell

### 业务逻辑
- 支付金额篡改（客户端price/amount可控且服务端未校验）
- 优惠券/积分无限刷
- 密码重置绕过（跳步/竞态/验证码复用）
- 关键流程跳步（支付前置可绕过）

### XSS（限场景）
- 存储型XSS（后台/评论/UGC可打管理员）
- DOM XSS（可构造利用链）
- 反射型XSS需可直接危害（配合CSRF/钓鱼）

---

## 不进入report（默认，但memory仍记录）

**强调：** 这些发现仍记 `surface:`/`vuln:` memory，不删除世界。仅控制是否写入最终report.md。

### 配置类
- **CORS任意Origin反射**（除非配合未授权写敏感操作可跨域利用）
- 安全响应头缺失（X-Frame-Options/CSP/HSTS/X-Content-Type-Options）
- OPTIONS预检请求可达
- Cookie缺Secure/HttpOnly（无直接利用链）

### 信息泄露（无直接利用）
- **前端硬编码**密钥/API key/内网IP（无法用于权限提升或数据访问）
- X-Powered-By/Server版本指纹
- .git/.env返回404（未真实泄露）
- README/robots.txt/swagger-ui仅文档（无敏感数据/无未授权能力）
- actuator端点无敏感（仅health/info/metrics）
- **测试环境域名**test/uat/dev（纯测试数据且隔离，无法穿透生产）

### 拒绝服务（低危害）
- 慢速DoS（需大量资源）
- 正则ReDoS（需极端输入）
- 邮件/短信轰炸（有session级限流，无法持续滥用）

### 枚举（无后续利用）
- 用户名/邮箱枚举（仅提示存在，无爆破/重置链）
- 弱口令提示（未实际爆破成功）
- 验证码4位纯数字（有尝试次数限制/账户锁定）

### 前端安全（服务端已防御）
- 前端JS逻辑可绕过（服务端正常鉴权，API返401/403）
- localStorage明文存储（无XSS无法读取）
- 前端路由守卫可绕过（业务API仍鉴权）

---

## 判断流程

### 步骤1：发现阶段（不过滤）
所有观察正常记录：
- `surface:` 状态码/端点/响应特征
- `hypothesis:` 未解释关系
- `vuln:` 可复现且有影响的发现

**禁止在这阶段审查赏金价值。**

### 步骤2：写report阶段（过滤）
问：**这个发现能让攻击者直接获得什么非授权权限/数据/控制？**

✅ 进report：
- 能读别人订单/手机号/支付记录
- 能改别人密码/地址/余额
- 能0元下单/无限领券/绕过支付
- 能getshell/读服务器文件/执行命令
- 能接管账户（密码重置无验证/Session固定/JWT伪造）
- 能提升权限（普通用户→管理员）

❌ 不进report（memory已有）：
- "可能有风险"/"配置不当"
- 信息泄露但无法利用
- 有防御机制的枚举/爆破
- 纯前端绕过（服务端已防御）

### 步骤3：边界情况
- **test环境有生产数据** 或 **可穿透生产** → 进report（标注测试环境）
- **test环境纯测试数据且隔离** → 不进report（memory已记录，供安服场景）
- **CORS + 未授权写关键数据** → 进report（可跨域利用）
- **CORS + 已鉴权读** → 不进report（需受害者已登录，实际危害低）

---

## 边界案例对照

| 发现 | memory记录 | 是否进report | 理由 |
|---|---|---|---|
| CORS + 未授权写关键数据 | vuln: | ✅ 是 | 可跨域触发危害 |
| CORS + 已鉴权读接口 | surface: | ❌ 否 | 需受害者已登录，实际价值低 |
| 前端hardcode + 可伪造管理token | vuln: | ✅ 是 | 直接利用链 |
| 前端hardcode密钥但无利用点 | surface: | ❌ 否 | 仅信息泄露 |
| actuator/env暴露DB密码 | vuln: | ✅ 是 | 敏感凭据可直接利用 |
| actuator仅health/metrics | surface: | ❌ 否 | 无敏感数据 |
| Swagger + 未授权管理接口 | vuln: | ✅ 是 | 未授权接口本身是洞，文档只是发现途径 |
| Swagger仅文档无未授权接口 | surface: | ❌ 否 | 仅信息泄露 |
| 邮件轰炸无任何限流 | vuln: | ✅ 是 | 可持续滥用 |
| 邮件轰炸有session限流 | surface: | ❌ 否 | 危害有限 |
| 邮箱枚举 + 密码重置可爆破 | vuln: | ✅ 是 | 完整攻击链 |
| 邮箱枚举但无后续利用 | surface: | ❌ 否 | 仅信息泄露 |

**关键：** 左侧"memory记录"列永远有值，不因赏金价值而丢弃观察。

---

## 实施要点

1. **观察阶段**：所有发现正常记 memory（`surface:`/`vuln:`/`hypothesis:`），不因赏金价值而删除
2. **写report前**：问自己"Impact+Exploitability够吗？"，不确定时Read本文
3. **水洞价值**：不进report ≠ 无价值。memory保留供：
   - 后续组合利用（CORS + 未来发现的未授权写）
   - 安服场景提交
   - 理解目标完整攻击面
4. **report定位**：赏金平台可收的高信噪比漏洞列表
5. **禁止**：在探索/实验阶段预判赏金价值而跳过验证

---

## 典型场景示例

### 场景1：示例目标 A
**发现：**
- 未授权写日志（/log-server/access无鉴权，可污染运营数据）
- CORS允许Origin:null+Credentials
- 前端app.js硬编码DES密钥/高德key/内网IP

**处理：**
- memory: 三个都记 `vuln:` 或 `surface:`
- report: 仅写"未授权写日志"（可持续滥用，影响数据完整性）
- 不写report: CORS（需配合未授权写才危害，已在未授权写中体现）、前端硬编码（无直接利用链）

### 场景2：示例目标 B
**发现：**
- API登录无验证码可爆破
- 邮箱枚举接口
- 邮件发送无CSRF可轰炸（但有session限流）
- 系统信息泄露(/ajax/lang)

**处理：**
- memory: 四个都记
- report: 写前三个（爆破+枚举组成攻击链，邮件轰炸虽有限流但可新session绕过）
- 不写report: 系统信息泄露（仅辅助侦查，无直接危害）

### 场景3：test环境
**发现：**
- test.example.com Swagger暴露
- actuator/env有测试DB密码
- 前端指向生产API baseURL

**处理：**
- memory: 三个都记
- report判断：
  - 若test DB含生产数据 → 写report
  - 若前端可跨域请求生产API造成CORS攻击 → 写report
  - 若纯测试数据且隔离 → 不写report（但memory保留供安服）
