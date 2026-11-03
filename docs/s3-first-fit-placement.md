# S3 first-fit 放置

## S3 在做什么

S3 在 S2 校验通过的拓扑上执行最简单的作业放置，作为后续 rail-aware 算法的
对照基线。

规则固定且可复现：

1. 作业按优先级降序排列；优先级相同时按作业 ID 升序。
2. GPU 候选按 node ID 升序、GPU slot 升序排列。
3. 从候选序列开头连续分配。
4. 一个作业会尽量填满当前节点，再使用下一个节点。
5. 剩余 GPU 不足时，该作业整单不分配，不产生部分结果。

S3 不考虑 rail、rack 局部性、链路容量、碎片、带宽或故障。

## 微型夹具结果

`micro-2x2.json` 中有两个作业，每个请求 4 GPU：

```text
job-ring-01 -> node-0 的 GPU 0-3
job-a2a-02  -> node-1 的 GPU 0-3
```

分配结果：

```text
Allocated GPUs : 8/8
Utilization    : 100.0%
Unplaced jobs  : none
Complete       : YES
```

## 如何查看结果

摘要：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s3.py
```

完整 JSON：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s3.py --json
```

## 输出字段

- `job_order`：实际处理作业的顺序。
- `assignments`：每个作业的节点、GPU、slot 和 rail。
- `assigned_gpus`：已分配 GPU 数。
- `utilization`：已分配 GPU / 拓扑总 GPU 数。
- `unplaced_job_ids`：未能分配的作业。
- `findings`：失败原因，例如 `INSUFFICIENT_GPUS`。

## 独立验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

S3 测试覆盖确定性顺序、优先级排序、微型拓扑完整分配、容量不足、GPU 不重叠、
重复执行一致性和结果查看命令。
