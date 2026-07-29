# Frontend Objects

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
Bundle、Route、API、Backend、IdP、Config，以及被观察到的候选 secret。

## Relations
Bundle 描述或引用 Route、API 与 Config；Route 连接用户状态与视图；API 连接前端语义与 Backend；IdP 与前端、Backend 之间存在身份声明和验证关系。候选 secret 只有在 value、use、acceptance 三者均有证据时才可视为有效凭据。

## Constraints
构建产物可能过期、分环境或经变换；前端可见配置不等于服务端接受；同名 Route 与 API 不保证映射到同一 Backend。

## Unknowns
Bundle 对应哪个部署环境，Route 与 API 的真实映射，Config 的来源与消费者，身份声明由谁签发和验证，候选 secret 的 value、use、acceptance 是否成立。
