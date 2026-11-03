# S9 分布式评估

## S9 在做什么

S9 把作业评估拆分为多个 worker 分区：

```text
Planner -> 多个 Evaluator worker -> Aggregator
```

- Planner 按作业 ID 确定性分片。
- Evaluator 使用完整拓扑的只读镜像执行本分片放置与路径评估。
- Aggregator 合并所有分片的流量、链路负载、作业吞吐和瓶颈。
- worker 丢失时，其作业分片转交给健康 worker。
- NATS worker 入口使用队列订阅，多个进程竞争同一评估主题。

本地无需 NATS 服务，in-process coordinator 使用同一分片、评估和合并协议。

## 故障行为

如果 `worker-1` 在评估前丢失：

1. 记录 `worker_failed` 事件。
2. 将 `worker-1` 的作业转交 `worker-2`。
3. 继续评估全部作业。
4. 返回 merge 后结果和恢复耗时。

全部 worker 不可用时返回明确错误，不伪造结果。

## 本地验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

S9 测试覆盖确定性分片、多 worker 合并、单 worker 故障重分配和全部 worker
不可用。

## NATS 模式

启动 NATS 后可运行多个 evaluator：

```powershell
.\.venv\Scripts\python.exe -m evaluator.nats_worker
```

队列主题：

```text
fabric.eval.evaluate
```

当前本机没有 Docker/NATS Server，因此队列 worker 代码已完成导入和语法验证，
实际 NATS 连接将在 Compose 环境中启用。
