# Experiment Model

Hypothesis → Experiment → Observation → Conclusion 表达概念关系，不是固定工作流。Hypothesis 应可证伪并指向对象间关系；Experiment 用可观察差异检验该关系，尽量区分变量并记录 artifact。Observation 保留实际结果及其上下文。Conclusion 只陈述本实验对假设的支持、反驳或未区分范围，不外推未观察对象。

当观察存在多个内部解释时，只保留能解释现象的最小候选关系模型。每个候选必须导出不同的可观察预测；下一实验只改变能区分候选的变量。伪代码若有助于产生预测，可作为临时模型，但不是实现事实，不写入长期状态。

Baseline 与 Mutation 是可选的实验关系：它们用于区分“条件变化导致结果变化”和“结果本来如此”，而不是规定请求形式。实验还应暴露其依赖的 premise；当共享 premise 失效，相关实验结论必须按原证据边界重估。

负结果只关闭被实际区分的关系范围。版本、标签、状态码或有限变体不能替代部署条件、可达性和实际影响；未区分结果保留为 unknown，而不是伪装成完成。

这与现有 Experiment Contract 一致：实验 metadata 关联假设、动作、观察、artifact、结论与状态；这些字段建立可追溯性，不替代 Agent 的语义判断，也不改变数据模型。
