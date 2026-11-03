# S4 rail-aware 放置

## S4 在做什么

S4 新增 rail-aware 贪心策略，同时保留 S3 first-fit 作为对照基线。它回答：

> 在容量相同的前提下，是否可以调整多节点作业的 GPU 分布，使不同节点尽量使用相同
> rail，并减少未来无法使用的碎片？

算法仍然是确定性的，不进行链路带宽计算或全局最优搜索。

## 评分规则

候选放置按以下四项评分：

```text
score =
    0.55 * rail_alignment
  + 0.20 * node_locality
  + 0.15 * leaf_locality
  + 0.10 * (1 - fragmentation_penalty)
```

- `rail_alignment`：多节点作业共同使用的 rail 占全部已用 rail 的比例。
- `node_locality`：使用节点越少越好。
- `leaf_locality`：使用 leaf 越少越好。
- `fragmentation_penalty`：作业分配后仍部分空闲的已使用节点比例。

评分相同时按 GPU ID 字典序选择，保证结果可复现。

## 对比夹具

```text
tests/fixtures/topologies/rail-aware-comparison.json
```

两个节点各有 4 个 GPU：

- `job-multi-01` 请求 6 GPU。
- `job-small-02` 请求 2 GPU。

first-fit 先填满 node-0，再向 node-1 放 2 个 GPU。rail-aware 把 6 个 GPU
均衡分成 3+3，并在两个节点上共同使用 rail 0、1、2。

两种策略都能使用全部 8 个 GPU，但 rail-aware 提高了多节点作业的共享 rail 比例。

## 如何查看结果

并排查看两种策略：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s4.py
```

查看完整 JSON：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s4.py --json
```

## 独立验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

S4 测试覆盖均衡 rail 分配、相对 first-fit 的 rail 改善、GPU 不重叠、单节点
局部性、容量不足、确定性，以及对比命令输出。
