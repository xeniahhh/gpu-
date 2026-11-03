# S2 拓扑生成与语义校验

## S2 在做什么

S2 把 S1 定义的数据模型变成两个可以独立使用的能力：

1. 根据规模参数生成确定性的 spine/leaf fabric。
2. 对拓扑和作业执行语义检查，返回带 code、严重级别、实体和说明的 findings。

S2 不分配作业，也不计算路径和带宽指标。

## 默认生成拓扑

```text
8 spine x 16 leaf x 8 node x 8 GPU
= 128 node
= 1024 GPU
```

链路：

- 128 条 spine-leaf 链路。
- 128 条 leaf-node 链路。
- 共 256 条链路。

生成规则固定，相同配置得到相同 ID、GPU rail 和链路顺序。

## 校验发现

校验器当前检查：

- 实体 ID 重复。
- leaf、node 引用不存在的 rack 或 leaf。
- node 的 rack 与 leaf 所属 rack 不一致。
- node 没有 GPU。
- GPU slot 或 rail 重复、为负数。
- 功耗预算不是有限正数。
- 链路端点为不存在或类型不匹配的实体。
- 链路自环或容量非法。
- fabric 不连通。
- 作业 GPU 数量非法或超过拓扑容量。
- 作业 ID 重复。

结构上无法解析的 JSON 仍由 S1 模型抛错；可以成功解析但语义不合理的数据由 S2
返回 finding。这样 API 层未来可以统一展示多个问题，而不是遇到第一个问题就停止。

## 如何查看结果

生成并验证默认 1024 GPU 拓扑：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s2.py
```

验证 S1 微型夹具和两个作业：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s2.py --preset micro
```

预期 MVP 摘要：

```text
spines : 8
racks  : 16
leaves : 16
nodes  : 128
GPUs   : 1024
links  : 256
Validation: PASS
Errors    : 0
Warnings  : 0
Findings  : none
```

## 独立验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

S2 测试覆盖生成规模、确定性 rail、参数拒绝、引用错误、ID 重复、层级不一致、
链路类型错误、rail 重复、断路和作业容量。
