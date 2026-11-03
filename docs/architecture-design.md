# FabricSandbox 架构基线

## 目标

在模拟的 1024 GPU fabric 上完成作业放置、带宽影响预测、故障重规划和不可变
修订版管理。系统不控制真实集群，不采集实时遥测。

## 服务边界

```text
React/SVG Web
    <-> Gateway API
    <-> NATS JetStream
    <-> Planner / N 个 Evaluator Worker / Aggregator
    <-> PostgreSQL
```

- Gateway：项目、修订版、运行接口和 SSE。
- Planner：候选放置、分区任务和租约。
- Evaluator Worker：路径、负载和局部指标计算。
- Aggregator：结果融合、影响分析和迁移建议。
- PostgreSQL：业务主数据和不可变快照。
- NATS JetStream：可重投命令、事件和 worker 心跳。

## MVP 范围

- 8 spine x 16 leaf x 8 node x 8 GPU，共 1024 GPU。
- `ring_all_reduce` 与 `all_to_all` 两种通信模式。
- rail 感知贪心放置与 first-fit 对照。
- 链路中断和 worker 宕机。
- 草稿、校验、提交和版本对比。
- 六分钟端到端演示。

## 实施顺序

```text
S0  工程骨架、虚拟环境、固定测试入口
S1  领域模型、契约和微型拓扑
S2  拓扑生成与语义校验
S3  first-fit 放置
S4  rail-aware 放置
S5  路径、流量和影响指标
S6  修订版生命周期
S7  Gateway API 与本地闭环
S8  PostgreSQL 持久化
S9  NATS 多 worker
S10 故障注入与重规划
S11 React/SVG 界面
S12 混沌测试、验收和演示
```

每一步都必须保持原有测试通过，并通过接口或适配器接入，不能重写已完成模块。

