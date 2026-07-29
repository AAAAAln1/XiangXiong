# Authentication Objects

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
Principal、Credential、Issuer、Token、Validator、Audience、Scope、Resource、Policy。

## Relations
Issuer 根据 Credential 或 Principal 状态签发 Token；Validator 校验 Token 与 Issuer、Audience 等约束；Policy 将 Principal、Scope、Resource 与授权结果关联。Issuance、validation、authorization 是不同关系，任一成立不推出其余成立。

## Constraints
Token 的格式与声明不证明真实性；Credential 的存在不证明可被 Issuer 接受；Validator 的接受不证明 Principal 对 Resource 获得授权；Scope 的文本不等于 Policy 的实际语义。

## Unknowns
Issuer 与 Validator 的信任边，Token 面向的 Audience，Scope 如何映射 Resource，Policy 的决策输入，Principal 与 Credential 的绑定范围。
