# 内网工具合同（冷卡：首次使用内网工具前读）

> 定位：工具**默认参数与输出合同**，不是攻击教程、不是 SOP。
> 与 `bounty_criteria.md` 同级——需要时才读，不进每轮热路径。
> 授权边界、凭据记账、dead 三态的机制由工具闸 REJECT 回执当场教学，本卡只补参数经验。

## 0. 开工顺序（授权内网 engagement）

1. 声明授权（用户明确授权后）：
   `python runtime/scope.py init <engagement> --allow 10.0.x.0/24,... --deny <ip...>`
   - target 用 engagement 名（如 `intranet-acme-2026q4`），主机/端口/凭据写进账本**行内**；
     跳板换主机不换 target（memory 保留），换授权网段才换 engagement。
   - 私网目标无 scope.json → http/execute 在动作前 REJECT；越界回执给可抄修法。
2. 换立足点：`scope.py position <engagement> --at "<ip> via SSH/隧道" --art art-NNN`
   （立足点是易腐执行态，进 scope.json，不进账本。）
3. 地形聚合：`python runtime/intranet_scan.py <engagement>`（见 §3）。

## 1. 凭据：值不进账本/投影

```
python runtime/credentials.py put <t> --from-file <值文件> [--kind pw|ntlm|hash|key|token] --note "<身份>"
cred: c-003 svc_backup @10.0.x.0/24 来源=<主机/途径> (exp-NNN)
```
- 账本 cred 行含明文/哈希会被 REJECT；投影只显示「N 组 + list 指针」。
- 口令喷洒/复用前先建 hypothesis：**锁定策略未知时默认高风险**——一次锁死账号是内网最贵事故；
  先小样本（1-2 个高价值账号）验证，再决定是否扩大。

## 2. nmap（经 execute.py 跑）

- 无 root（Windows/普通 shell 常态）默认 `-sT -sV -sC`；不要加 `-sS`（需 root，静默失败）。
- **必须带 `-oX results/<t>/artifacts/<名>.xml`**：intranet_scan 靠它聚合地形；
  长网段扫描用 `execute.py --background --timeout 600`。
- 单主机快扫可省 -oX（stdout 已全量落盘登记 art），但网段结果想进 [HOST MAP] 必须 -oX。
- 结果三态入账（均需证据 art/exp）：
  - `dead [filtered]:` 从当前位置到不了（ACL/丢包）→ 换协议/走已有隧道/换立足点重估；
  - `dead [closed]:` 主机在但端口关 → 不重试；
  - `dead [unreachable]:` 无路由 → 族级 counter 收口。
- 「扫了 80 端口」≠「扫完网段」：未探存活/未探端口在 [TERRAIN] 容量计数里是硬缺口。

## 3. intranet_scan.py

- 吃 `artifacts/index.json` 里 kind=port_scan/host_alive/service_enum 的产物（nmap XML 优先，grepable 降级）；
- 输出 `[HOST MAP]`（host→ports→service）+ **可抄行** `surface: host ... (art-NNN)`；
  自己审阅后 merge，Runtime 不自动入账（不替你确认主机身份）；
- host_map 登记为 kind=host_map 的 art，投影计数以它为准；手改 host_map 会因 sha256 不符被拒显。

## 4. 其他

- impacket/nxc/responder 等走 `execute.py`（命令串与引用脚本受 scope 第二层字面量检查；
  该层拦不住运行时动态目标，授权纪律仍是最终边界）。
- `bash -c`/`powershell -Command`/`python script.py` 不触发交互告警；只有裸 `bash`/`powershell` 才告警。
- 非 HTTP 结论同样要证据：`dead:`/`counter:`/`vuln:` 无 req/exp/art 引用会被 REJECT。
