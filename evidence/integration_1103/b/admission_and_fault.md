# D-B2｜资源准入与故障影响（B，2026-11-03）

全部为 SYNTHETIC 参数的 CPU 仿真，不是 GPU 测量。

- 原始结果：`evidence/team_1103_baseline/<情景>/`（events.jsonl 是完整故障轨迹）
- 本目录：
  - `job_outcomes.csv`：每个作业的终态、等待、丢失步数、各 epoch 的 GPU
  - `state_segments.csv`：状态段，computing/syncing/updating 合并为 training
  - `key_events.csv`：故障、回滚、等待原因、分配、忽略旧事件、检查点提交、终态
- implementation_hash：`d844734439ed524fe81d843d0065a17510ebf161c5efc036f31cf4bb061cd4e3`

共同设定：4 节点 × 2 卡，n0/n1 在机架 r0，n2/n3 在 r1，上联 50 Gbps。每步计算 100 ms、更新 5 ms，检查点保存 200 ms，恢复 400 ms（group_init 100 + restore 300），均为 SYNTHETIC。

## 1. 结果一览

| 情景 | 设置 | 结果 |
|---|---|---|
| D3_admission | A 4 卡、B 2 卡在 0 ms 到达；C 4 卡在 1 ms 到达 | C 等待 4743.74 ms，B 结束后整组拿到 4 卡；三个作业都完成 |
| D3_memory_reject | 每卡 3 GiB，每 rank 峰值显存 4 GiB | A、B 在 0 ms 直接 `unschedulable`，不进入排队 |
| D4_fault_spare | A 在 n0/n1，B 在 n2，n3 空闲；3000 ms n0 宕机 | A 回滚到第 10 步、丢 6 步，立刻换到 n1+n3，恢复 400 ms；B 不受影响 |
| D4_fault_no_spare | A 在 n0/n1，B 在 n2/n3，8 卡全占；3000 ms n0 宕机 | A 丢 6 步后等待 2343.73 ms，B 完成后才拿到 n1+n2；B 不受影响 |
| D4_checkpoint_fault | 同 spare，但故障发生在 A 保存第 10 步检查点的中途（1747.91 ms） | 这次保存不算数，A 回滚到第 0 步、丢 10 步 |

## 2. 逐个情景解释

### D3_admission：暂时缺卡就等待，不部分占卡

- 0 ms：A 拿到 n0、n1 共 4 卡，B 拿到 n2 的 2 卡，只剩 n3 两张空闲。
- 1 ms：C 到达，需要 4 卡。`waiting_reason` 写的是"需要 4 张可达且显存合格的 GPU，当前空闲 2 张；不部分占卡"。C 不会先占住这 2 张。
- 4744.74 ms：B 完成，n2 释放，空闲变成 4 张。C 在同一时刻被分配 n2+n3，等待 4743.74 ms。
- C 跑 10 步、不存检查点（`checkpoint_interval: 0`），4744.74 + 10 × 164.79 = 6392.65 ms 完成。

B 为什么是 4744.74 ms：B 两个 rank 都在 n2 上，Ring 的两条流 gpu0→gpu1 和 gpu1→gpu0 共用 `node:n2:local`（200 Gbps，不分方向），各得 100 Gbps。每块 248,879,616 B，每轮 19.91 ms，2 轮共 39.82 ms/步。30 × (105 + 39.82) + 2 × 200 = 4744.74 ms。

### D3_memory_reject：永久不适配直接拒绝

每张 GPU 只有 3 GiB，每个 rank 都需要 4 GiB。集群总显存 8 × 3 = 24 GiB，比 4 rank × 4 GiB = 16 GiB 多；如果错误地把显存按卡数平摊，就会以为放得下。代码按每 rank 判断（检查项 `scenario_memory_is_per_rank`），所以两个作业在 0 ms 就是 `unschedulable`，原因是"单卡显存规格或集群总卡数无法满足固定通信组"，不会永远排队。

### D4_fault_spare：有备用卡，回滚后立刻换卡

- 3000 ms 时 A 已完成 16 步，正在第 17 步的 updating 阶段；最近一次持久化检查点是第 10 步。
- n0 宕机后，A 整组中断，n1 上没坏的 2 张卡也一起释放。随后回滚到第 10 步，丢失第 11–16 步共 6 步；epoch 从 0 变成 1，`world_size` 仍是 4。
- 同一时刻 n1 + n3 有 4 张空闲卡，A 被重新分配，恢复 400 ms，3400 ms 继续训练。
- 3001.45 ms 原本是 epoch 0 第 17 步更新结束的时刻，这个事件被记为 `ignored_old_event`，没有推进步数。
- 新放置 n1（r0）+ n3（r1）跨机架，每步从 164.79 ms 变成 224.58 ms（同步从 59.79 变成 119.58，与 D2_A_alone 相同）。
- 完成时间：3400 + 20 × 224.58 + 1 次检查点 200 = 8091.64 ms。
- B 只用 n2，与 A 的新旧资源都不重叠，完成时间 4744.74 ms，与 D3_admission 中的 B 完全相同。

### D4_fault_no_spare：没有备用卡，整组等待，不缩小规模

- 故障前 8 卡全占。n0 宕机后健康卡剩 6 张：B 占 4 张，空闲只有 n1 的 2 张。A 和 B 不可能同时以 4 卡继续。
- A 回滚到第 10 步后进入 waiting，原因同样是"当前空闲 2 张；不部分占卡"。它不会缩成 2 卡继续跑。
- B 继续，5343.73 ms 完成（与 D1_compact 中的 B 相同）。A 在同一时刻拿到 n1+n2，等待 2343.73 ms，再恢复 400 ms，5743.73 ms 继续。
- `recovery_ms` = 2343.73 + 400 = 2743.73。
- 完成时间：5743.73 + 20 × 224.58 + 200 = 10435.38 ms。

### D4_checkpoint_fault：保存中途故障，不能用这份检查点

- A 在 1647.91 ms 完成第 10 步后开始保存检查点，本应在 1847.91 ms 提交。
- 1747.91 ms n0 宕机，保存只进行了一半。此时最近一次已提交的检查点是第 0 步，所以回滚到 0，丢 10 步，`physical_completed_steps` = 40。
- 1847.91 ms 那次提交属于 epoch 0，被记为 `ignored_old_event`，没有生效。
- 立刻换到 n1+n3，2147.91 ms 继续：30 × 224.58 + 2 × 200 = 7137.47，2147.91 + 7137.47 = 9285.38 ms 完成。

## 3. 故障让 A 多花了多少时间

无故障时 A 在 5343.73 ms 完成（D1_compact）。差值可以拆成四项：

| 情景 | 白做的工作 | 等待 | 恢复 | 新放置变慢 | 合计 | 实际差值 |
|---|---|---|---|---|---|---|
| D4_fault_spare | 3000 − 1847.91 = 1152.09 | 0 | 400 | 20 × 59.79 = 1195.82 | 2747.91 | 8091.64 − 5343.73 = 2747.91 |
| D4_fault_no_spare | 1152.09 | 2343.73 | 400 | 1195.82 | 5091.64 | 10435.38 − 5343.73 = 5091.64 |
| D4_checkpoint_fault | 1747.91（从 0 ms 起全部白做） | 0 | 400 | 30 × 59.79 = 1793.73 | 3941.64 | 9285.38 − 5343.73 = 3941.64 |

"白做的工作"指最近一次已提交检查点之后到故障时刻的时间。"新放置变慢"是恢复后剩余步数 × 每步多出的同步时间（119.58 − 59.79）。

## 4. 独立核对（按 events.jsonl 重放）

对 D3_admission、D4_fault_spare、D4_fault_no_spare、D4_checkpoint_fault 逐事件重建 GPU 所有权：
- 没有任何 GPU 同时属于两个作业。
- 故障时刻之后，没有分配宕机节点 n0 上的 GPU。
- 结束时没有残留占用。
- 同时占用的 GPU 最多 8 张（D3、D4_fault_no_spare）和 6 张（D4_fault_spare、D4_checkpoint_fault）。
- 每个 `step_completed` 的 epoch 都等于该作业当时的 epoch，同一 epoch 内步号严格递增。
- A 的 `step_completed` 事件数：fault_spare / fault_no_spare 为 epoch 0 16 次 + epoch 1 20 次 = 36，checkpoint_fault 为 10 + 30 = 40，都等于 `physical_completed_steps`。逻辑完成步数都是 30。

各情景 `checks.json` 全部通过，包括 `scenario_wait_for_whole_group`、`scenario_memory_is_per_rank`、`scenario_rollback_then_complete`、`scenario_unaffected_job_continues`、`scenario_no_spare_means_wait`。

## 5. 结论

1. **成组准入。** 作业要么一次拿到全部 R 张卡，要么一张都不占。暂时不够就等待（D3_admission 的 C、D4_fault_no_spare 的 A），永久放不下就直接拒绝（D3_memory_reject）。
2. **故障只影响相关作业，且按整组处理。** n0 宕机时 A 连同 n1 上完好的卡一起中断、回滚、重新准入；B 的完成时间与无故障时一字不差。
3. **恢复不是无损迁移。** 代价包括检查点之后白做的工作、可能的等待、400 ms 恢复，以及新放置可能更慢。保存中途发生故障时，这次保存作废，只能退回更早的检查点。

## 6. 边界与发现

- 这 5 个情景只有节点故障（`node_down`）。链路故障由单元测试 `test_link_failure_during_compute_also_invalidates_future_ring` 覆盖：`rack:r0:tx` 在 50 ms 断开、500 ms 恢复，两个跨机架作业都重启。该测试在本机通过（`team_1103_B_env/run_tests_2_after_psycopg_env.log`）。
- 排队规则（`ddp.py` 的 `dispatch()`）：等待作业按 (priority 降序, arrival_ms, id) 排成严格队列。队首放不下就停止本轮分配，后面的小作业不会插队（没有回填），也不抢占正在运行的作业。本组情景里每次只有一个作业在等待，没有覆盖多作业排队的顺序。
- `unschedulable` 作业的 `completed_at_ms` 和 `completion_ms` 是 0.0，而不是空值：`dispatch()` 拒绝时把 `finished_at` 设成了当前时刻。`summary` 里的 `mean_completion_ms` 只统计 `completed`，不受影响；但网页作业表的"完成时刻"列在 D3_memory_reject 中会显示 0，演示时要配合"状态"列解释。这部分属于 `ddp.py` 和网页，需要转告 A、C，B 没有改动。
