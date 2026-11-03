# D-B1｜解析校验、放置与共享竞争（B，2026-11-03）

全部为 SYNTHETIC 参数的 CPU 仿真，不是 GPU 测量。

- 原始结果：`evidence/team_1103_baseline/<情景>/`（input / result / events / checks / link_timeseries / metrics / bottleneck.svg）
- 本目录：`completion_table.csv`（完成时间与各阶段耗时）、`path_examples.csv`（三类路径示例）
- implementation_hash：`d844734439ed524fe81d843d0065a17510ebf161c5efc036f31cf4bb061cd4e3`（E0 的 result 不带该字段）
- 环境与基线复现：`evidence/team_1103_B_env/README.md`

## 1. E0 解析对照

输入：4 个 rank，每节点 1 卡、同一机架，节点带宽 100 Gbps，延迟 0，payload 64 MiB。

| 项 | 手算 | 仿真（checks.json） |
|---|---|---|
| 每块大小 | 64 MiB / 4 = 16 MiB = 16,777,216 B | `chunk_bytes` 一致 |
| 轮数 | ReduceScatter 3 + AllGather 3 = 6 | 6 |
| 每 rank 发送 | 6 × 16 MiB = 96 MiB | — |
| 全组字节 | 4 × 96 MiB = 402,653,184 B | `exact_group_bytes` 402,653,184 |
| 每轮时间 | 16,777,216 × 8 / 100e9 = 1.34217728 ms | — |
| 总时间 | 6 × 1.34217728 = 8.05306368 ms | `analytic_time` 8.05306368 |

每条流只在 `transfer_completed` 时按 `size_bytes` 计一次，发送和接收不重复计。

## 2. 完成时间对照

GPT-2 Small：124,439,808 参数 × 4 B（FP32）= 梯度 497,759,232 B；4 rank 时每块 124,439,808 B。每作业 30 步，每 10 步存一次检查点。

放置列按输入里的 `gpu_ids` 描述（见第 8 节）。

| 情景 | 放置 | 作业 | 完成 ms | 每步同步 ms | 每轮 ms | sync_fraction |
|---|---|---|---|---|---|---|
| D1_compact | compact，A 在 n0/n1，B 在 n2/n3 | A、B | 5343.73 | 59.79 | 9.965 | 0.36 |
| D2_A_alone | spread，A 用四个节点的 gpu0 | A | 7137.47 | 119.58 | 19.930 | 0.53 |
| D2_B_alone | spread，B 用四个节点的 gpu1 | B | 7137.47 | 119.58 | 19.930 | 0.53 |
| D2_competition | spread，A、B 同时运行 | A、B | 10721.33 | 239.04 | 39.841 | 0.69 |

- 同一作业、同一放置：并发 / 独占 = 10721.33 / 7137.47 = **1.502**（`contention_comparison.csv`）
- 同样两个作业、不同放置：spread 并发 / compact 并发 = 10721.33 / 5343.73 = **2.006**

## 3. 这些数字怎么来的

`core.py` 的规则：
- 路径（`Network.path`）：同节点只占 `node:<n>:local`；跨节点占 `node:<src>:tx` + `node:<dst>:rx`；跨机架再加 `rack:<src>:tx` + `rack:<dst>:rx`。
- 启动延迟（`latency_ms`）：同节点 2 µs；跨节点 2 段 × 5 µs = 10 µs；跨机架 4 段 × 5 µs = 20 µs。
- 速率（`Engine._allocate`）：每条流取路径上每段资源"容量 ÷ 正在用这段资源的流数"的最小值。
- Ring（`ring_allreduce`）：每轮 4 条流全部完成才开始下一轮，所以每轮时间 = 4 条流里最慢的那条。

逐个情景：

| 情景 | 最慢的流 | 计算 | 每轮 |
|---|---|---|---|
| D1_compact | 同机架跨节点，网卡 100 Gbps | 10 µs + 124,439,808 × 8 / 100e9 = 9.965 ms | 9.965 ms |
| D2_*_alone | 跨机架，机架上联 50 Gbps | 20 µs + 124,439,808 × 8 / 50e9 = 19.930 ms | 19.930 ms |
| D2_competition | 跨机架，`rack:r0:tx` 上 A、B 各一条流，各得 25 Gbps | 20 µs + 124,439,808 × 8 / 25e9 = 39.841 ms | 39.841 ms |

每步同步 = 6 轮。完成时间 = 30 × (计算 100 + 更新 5 + 每步同步) + 2 次检查点 × 200：
- compact：30 × (105 + 59.791) + 400 = 5343.73
- alone：30 × (105 + 119.582) + 400 = 7137.47
- competition：30 × (105 + 239.044) + 400 = 10721.33

并发时同步时间几乎翻倍（3587 → 7171 ms），但计算、更新、检查点共 3550 ms 不受网络影响，所以总时间只慢 1.50 倍。

## 4. 路径示例与字节量

详见 `path_examples.csv`，以下为节选：

| 类型 | 例子 | 占用资源 | 单独时速率 | 竞争时速率 |
|---|---|---|---|---|
| 同节点 | n0:gpu0 → n0:gpu1（D1） | node:n0:local | 200 Gbps | — |
| 同机架跨节点 | n0:gpu0 → n1:gpu0（D2） | node:n0:tx, node:n1:rx | 100 Gbps | 50 Gbps（n0:tx 被 A、B 平分） |
| 跨机架 | n1:gpu0 → n2:gpu0（D2） | node:n1:tx, rack:r0:tx, rack:r1:rx, node:n2:rx | 50 Gbps | 25 Gbps（rack:r0:tx 被 A、B 平分） |

D1_compact 与 D2_competition 的逻辑通信量相同，都是 `completed_transfer_bytes` = 179,193,323,520 B（2 作业 × 30 步 × 4 rank × 6 轮 × 124,439,808 B），但物理分布不同：
- compact：一半走 `node:*:local`，一半走同机架网卡，机架链路 0 字节。
- spread：一半走同机架网卡，一半跨机架，`rack:r0:tx`、`rack:r1:rx`、`rack:r1:tx`、`rack:r0:rx` 各承载 44,798,330,880 B。

## 5. 瓶颈曲线

`bottleneck.svg` 画的是利用率最高的机架链路（作图脚本优先选 `rack:` 资源）：
- D2_A_alone / D2_competition：`rack:r0:rx` 在同步阶段都达到容量 50 Gbps。区别在 `link_timeseries.csv` 的 `active_flows` 列：独占时 1 条流占满 50 Gbps，并发时 2 条流各 25 Gbps，同步段时长翻倍。
- D1_compact：曲线是 `rack:r0:rx`，全程为 0。这正说明紧凑放置没有跨机架流量；它的实际瓶颈是网卡（`node:*:tx/rx`，100 Gbps），看 `link_timeseries.csv` 即可。

## 6. 约束检查

四个 D 情景的 `checks.json` 都通过：`gpu_world_memory_checkpoint`、`link_capacity`、`synchronization_before_update`、`completed_jobs_have_target_progress`。我另外逐行检查了 `link_timeseries.csv`，所有采样的 `allocated_gbps` 都不超过 `capacity_gbps`，`max_capacity_ratio` = 1.0。

PASS 只表示上面列出的断言成立。本节四个情景里所有作业都是 `completed`，这一点是单独核对的。

## 7. 三条结论

1. **不同 GPU 也会互相拖慢。** D2 里 A 用 gpu0、B 用 gpu1，GPU 没有重叠，但两者的 Ring 都经过 `rack:r0:tx` 和 `rack:r1:tx`（各 50 Gbps）。并发时每条跨机架流从 50 降到 25 Gbps，每轮从 19.93 ms 变成 39.84 ms，作业完成时间从 7137 ms 变成 10721 ms（×1.50）。
2. **放置决定物理负载。** 同样两个作业、同样 179.19 GB 逻辑通信量，compact 让每个作业留在一个机架内：机架链路 0 字节，两个作业不共用任何资源，瓶颈变成 100 Gbps 网卡。结果 5343.7 ms，比 spread 并发快 2.0 倍。
3. **网络瓶颈直接拉长训练步。** 模型里 DDP 每步必须等 AllReduce 完成才更新，计算和通信不重叠，所以 sync_fraction 从 0.36（compact）升到 0.53（spread 独占）、0.69（spread 并发），慢的部分全部来自同步。

## 8. 边界

- 速率是保守平均分配，不是 max-min 最优。例如 D2_competition 中 `node:n1:tx`（100 Gbps）只用了 50 Gbps：两条流都被 `rack:r0:tx` 限在 25 Gbps，剩余带宽不会再分配。本情景没有别的流需要这部分带宽，所以结果不受影响；其他情景里这条规则可能让时间偏长。
- 网络是两级有效容量模型（节点 tx/rx/local + 机架 tx/rx），没有交换机图、ECMP 或故障绕路。
- D1/D2 的输入里每个作业都写明了 `gpu_ids`。`ddp.py` 的 `candidates()` 在首次分配时直接用这份列表，`placement_policy` 只在没有指定 GPU 或故障后重新分配时才起作用。所以第 2 节比较的是两种具体放置，不是放置算法之间的比较。另外，compact 与 first_fit 在代码里走同一分支（按机架、节点排序），不能当两种算法。
- 单桶 Ring、每轮屏障、计算与通信不重叠。真实 DDP 会分桶并与反向计算重叠，实际受网络影响的程度可能更小。
- compute 100 ms、optimizer 5 ms、检查点 200 ms 等均为 SYNTHETIC 设定；梯度字节由 GPT-2 结构推导（`gradient_source: DERIVED`）。
