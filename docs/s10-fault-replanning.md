# S10 故障与重规划

## S10 在做什么

S10 支持三类基础设施故障：

- `link_down`：一条链路不可用。
- `switch_down`：spine 或 leaf 上所有关联链路不可用。
- `node_down`：GPU 节点及其关联链路不可用。

故障注入后：

1. 找出不可达节点和受影响作业。
2. 保留未受影响作业的原分配。
3. 只在剩余可达 GPU 上重规划受影响作业。
4. 重新计算路径、吞吐和瓶颈。
5. 输出迁移建议和迁移 GPU 数量。

评估 worker 自身丢失不属于拓扑故障，由 S9 的作业分片重投处理。

## 演示夹具

```text
tests/fixtures/topologies/fault-replan.json
```

拓扑包含 3 个 node，每个 4 GPU。初始把两个作业放在 node-0 和 node-1；对
node-1 注入 `node_down` 后，受影响作业迁移到 node-0 和 node-2。

单条 spine 链路故障通常可通过另一条 spine 路径绕行，因此不产生迁移。

## 结果字段

- `removed_link_ids`：故障删除的链路。
- `unavailable_node_ids`：故障或不可达节点。
- `affected_job_ids`：需要重规划的作业。
- `unaffected_job_ids`：保留原分配的作业。
- `migrations`：迁移前后节点和迁移 GPU 数。
- `evaluation`：故障后路径和带宽指标。

## 验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

S10 测试覆盖单链路故障、node 故障迁移、leaf switch 故障、未知故障目标和确定性。
