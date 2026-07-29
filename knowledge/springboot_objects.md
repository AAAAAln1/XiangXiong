# Spring Boot Objects

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
Application、Management endpoint、Config、Mapping、Heapdump、Gateway、Service boundary。

## Relations
Application 暴露 Mapping；Management endpoint 描述或操作 Application 状态；Config 影响 Application 与 Management endpoint；Heapdump 是特定运行状态的 artifact；Gateway 将外部请求映射到 Service boundary，边界后可能存在多个 Application。

## Constraints
部署可改变暴露、前缀、端口与边界；Gateway 可生成、改写或隐藏响应；Management endpoint 的概念存在不代表外部可达；状态码不证明后端对象存在或不存在。

## Unknowns
外部入口与 Application 的映射，Gateway 与 Service boundary 的责任划分，Config 的来源和生效范围，Mapping 的部署差异，artifact 与具体进程的 provenance。
