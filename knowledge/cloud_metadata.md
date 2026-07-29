# Cloud Metadata Objects

```
Status: frozen / non-runtime

Purpose:
Historical domain partition experiment (world-splitting prior), not Agent instructions.

Do not:
- treat as runtime knowledge
- auto-load
- route by stack
- inject into SessionStart

Future shape if promoted:
must become a behavior-changing prior, not remain an ontology encyclopedia.

Possible forms:
- boundary failure prior
- world partition prior

Promotion requires all three:
1. repeated miss across targets (same partition collapse)
2. clear boundary-failure or partition form (not object catalog alone)
3. observable behavior improvement (Experiment / less false closure)
```

## Objects
Metadata Service、Instance Identity、Temporary Credential、Permission Boundary、Network Namespace、UserData。

## Relations
Metadata Service 在特定 Network Namespace 中向工作负载呈现 Instance Identity、Temporary Credential 或 UserData；Temporary Credential 受 Permission Boundary 约束；Instance Identity 将计算实例与权限、配置或审计主体关联。

## Constraints
Provider 协议可能约束可达网络、请求上下文、会话属性、字段语义与凭据生命周期；这些约束应按实际 provider 文档和观察建模，不使用请求模板。地址或响应内容不能唯一归因于 Metadata Service、provider 或实例身份。

## Unknowns
响应的 producer 与 provenance，调用者所在 Network Namespace，Instance Identity 的绑定对象，Temporary Credential 的有效边界，Permission Boundary 的实际决策范围，UserData 的来源与消费者。
