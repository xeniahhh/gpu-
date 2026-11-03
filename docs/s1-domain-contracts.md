# S1 领域模型与 JSON 契约

## 交付范围

S1 只交付：

- 不可变领域数据模型。
- revision、topology、job 和 event envelope 的 v1 JSON Schema。
- 一个可以手工核对的微型 fabric 夹具。
- 模型与 JSON 之间的无损解析。
- 独立的命令行查看入口。

S1 不进行引用完整性、容量、rail 对齐或可达性语义校验；这些属于 S2。

## 微型拓扑

夹具：

```text
tests/fixtures/topologies/micro-2x2.json
```

规模：

```text
2 spine
2 leaf
2 rack
2 node
8 GPU
6 link
```

两个节点各有 4 个 GPU，rail ID 分别为 0-3。两个作业各请求 4 GPU，分别使用
`ring_all_reduce` 和 `all_to_all`。

## 如何查看结果

摘要模式：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s1.py
```

规范化 JSON：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s1.py --json
```

预期摘要包含：

```text
spines : 2
racks  : 2
leaves : 2
nodes  : 2
GPUs   : 8
links  : 6
JSON round-trip: PASS
```

## 契约文件

```text
packages/event-contracts/schemas/topology-v1.schema.json
packages/event-contracts/schemas/job-v1.schema.json
packages/event-contracts/schemas/revision-v1.schema.json
packages/event-contracts/schemas/event-envelope-v1.schema.json
```

当前支持 `schema_version = 1`。不兼容修改需要增加新版本或显式迁移，不能静默改变
旧字段语义。

## 独立验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

S1 测试覆盖夹具结构、rail 字段、通信模式、JSON 无损往返、版本拒绝、事件信封和
Schema 文件有效性。
