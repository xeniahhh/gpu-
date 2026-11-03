> 此为原版代码审查与算法修复记录。后续新增的认证、规则助手、端口加固和最新测试状态以 [复核与运行说明](review-handoff.md) 为准。

# FabricSandbox 代码审查与修复记录

审查基线：`/Users/yuchenxiong/大学学习/大四上/数据通信网络/gpu`。原目录只读。下文未特别标注的路径相对此目录，行号为原版。修复位于 `output/fabric_sandbox_20261101/gpu_optimized`。

## 总体判断

这不是仅有界面的空壳。项目已有纯领域模型、拓扑生成与语义校验、两种确定性放置策略、网络路径和静态带宽代理、故障重规划、不可变修订版、持久化适配器、API、React/SVG 工作台及测试。但文档把目标架构和已接通架构混在一起：当前主路径是单进程同步的模块化程序，不能称为已经运行起来的网络分布式服务系统。1024 GPU 是模型规模，不代表驱动了1024块真实GPU。

## 实际模块与算法

| 模块 | 代码证据 | 能力和边界 |
|---|---|---|
| First-fit | `packages/fabric-domain/src/fabric_domain/placement.py:127` | 作业按 `(-priority,id)` 排序，GPU按节点ID和slot排序。剩余资源足够时整作业分配，不抢占、不排队。 |
| Rail-aware | 同文件`:226`、`:349`、`:405` | 枚举按节点ID连续扫描的候选节点组，分别 compact/balanced 分配，贪心选最高分。不是全局最优，也没有探索任意节点子集。 |
| 启发式目标 | 同文件`:426` | `0.55*rail_alignment + 0.20/node_count + 0.15/leaf_count + 0.10*(1-fragmentation)`。权重为人工设定，缺少消融/标定证据。rail_alignment为“至少在两个节点出现的rail种数/使用过的rail种数”，不是所有节点两两对齐概率。 |
| 拓扑与契约 | `models.py`、`validation.py:350` | schema_version=1；明确spine/leaf/node/GPU/link/job类型；检查引用、ID、合法容量、连通性和作业尺寸。单个作业不超过GPU总量，不检查所有作业请求之和，因此过载是合法待调度情景。 |
| 修订版 | `revision_lifecycle.py:241,286,305,324` | 草稿→校验通过/失败→提交，SHA-256对规范JSON求hash，排除revision_id。提交检测parent与current一致。哈希用于内容识别，不是签名或防篡改认证。 |
| 故障重规划 | `faults.py:178` | 删除故障节点/链接，识别不可达节点和作业，保留未受影响作业的GPU占用，只对受影响作业重新放置。迁移是建议，无实际GPU进程迁移或checkpoint。 |
| API | `services/gateway/src/gateway/api.py` | 项目/修订版/校验/提交/diff/运行/事件接口；404不存在，422契约错误，409状态冲突。运行返回201以及最终状态，不是202后台队列。 |
| 存储 | `storage.py:23,195` | 默认SQLite；PostgreSQL适配器存在。run结果和事件仍在GatewayRuntime.runs内存字典中，重启丢失。 |

## 性能模型的准确定义

`evaluation.py:216`把作业内GPU通信折叠为节点间无向流量单位。ring按GPU排列中的相邻rank和闭环计数，忽略节点内边；all-to-all用两节点GPU数量乘积计数。无向化将方向合并，不能当作完整双向链路/真实NCCL通信模型。

`evaluation.py:173`使用BFS获取最短跳数路径，最多64条候选。`:298`按“加入当前流后的最大链路负载比、负载比之和、字典序”贪心择路。`:334`的每单位流速率为 `min(需求, 路径上capacity/全局traffic_units)`。它是保守的静态等权代理，不是渐进填水的max-min fair allocator，不会把远端瓶颈空出的容量重新分配给其他流。

每作业 `throughput_proxy_gbps` 是该作业所有节点间流的最小每单位速率。无跨节点流量时为null。原UI把所有null均解释为“无跨节点流量”，但未放置作业也可能是null，需要结合placement状态解释。

`utilization` 当前字段实际为“请求速率/容量”，因此可超过1；它不是链路实测利用率。`allocated_gbps` 修复后才等于经过该链路的端到端流分配之和。

Leaf超订比为下行容量和/上行容量和。二分带宽proxy按leaf ID排序分半，取两侧上行总容量的较小值，并非任意拓扑的最小割/最大流。

**没有模拟**：报文队列、时延、丢包、ECN/PFC、TCP/RDMA拥塞控制、消息大小和collective阶段、PCIe/NVLink开销、计算通信重叠、真实训练step时间、真实故障发现和checkpoint恢复。`rail_id` 没有进入网络图，只有放置评分使用它，因此不能用当前带宽模型证明rail对齐带来的物理网络收益。

## 原版最重要缺陷及已完成修复

1. **worker数改变路由和吞吐结果**。`coordinator.py:70`顺序调用各分片，`worker.py:63`在各自分片的零负载图上独立选路。全局共享链路负载使这些决策不独立。修复：协调器先生成确定性全局路径，worker接受routing_plan，只评估固定路径；1/2/3个worker和单worker失效结果一致。当前生成全局路径会执行一次完整评估，存在额外计算开销，应在后续拆出纯路由阶段。该修复保证正确性，不代表获得并行加速。

2. **flow、job、link三层速率冲突**。`merge.py:24`保留局部分片flow速率，`:110`却对job速率按全局负载重算。原版rail-aware-comparison.json、200 Gbps需求、2 worker实测：共享spine-0链路容量200，但两条flow各报200，合计400；job proxy却均为66.666667。修复：聚合器从合并后的flow推导全局负载，再重算每条flow速率、job最低速率、link实际承载速率；同时拒绝重复作业分区及拓扑/容量/配置不一致。

3. **链路allocated不满足端到端流量守恒**。`evaluation.py:366`和`merge.py:63`原来使用min(本链路需求,本链路容量)，忽略其他链路瓶颈。1 worker、同一200 Gbps夹具实测：leaf-node报告400，真实flow之和333.333333。修复：按最终flow的allocated逐链路累加。

4. **组合故障被静默忽略**。UI `App.tsx:218`同时发送fabric faults与failed_workers，runtime.py:174的faults分支却完全绕过worker协调器。修复：返回409，明确要求分别运行两种故障。API现在也拒绝未知worker ID和非法策略/worker数量。

5. **未放置作业也可能显示completed**。`runtime.py:217`只检查unroutable flow数，而未放置作业没有生成flow。修复：只有placement.complete且没有unroutable flow时才completed，否则degraded。

新增测试：`tests/unit/test_review_flow_consistency.py`、`tests/integration/test_review_run_semantics.py`。

已运行验证：Python3.12，18项domain/S5/S9/新增一致性测试通过。Gateway新回归等待上层提供FastAPI依赖后统一跑。原始复现也由同一Python3.12执行，以上数字是实测。

## 尚未实现或尚未验证的重点

- `runtime.py:150`在全局RLock内执行全部计算，读请求和其他run会被阻塞。`api.py:190`的SSE只遍历已完成run.events并结束，既无实时推送，也无event id、Last-Event-ID重放。`queued`枚举存在，但没有真正排队。
- `coordinator.py:70`是进程内顺序调用，无并行、网络传输、lease或心跳发现。`recovery_ms`计的是从evaluate开始到所有合并完成的总耗时，不能称为真实故障恢复时延。
- `nats_worker.py:26`确有NATS Core queue subscription入口，但Gateway/Planner没有request或publish调用，设置NATS_URL不会自动接通。没有使用JetStream持久consumer、ack、重投或幂等去重。Compose启动NATS并不能补齐缺失的发送路径。
- `events.py:39`定义了EventEnvelope，但运行事件仍是随意dict，没有用该contract封装。不能把schema文件存在说成完整端到端契约执行。
- 原版未找到Assistant、LLM SDK、工具调用路由、认证中间件或对象级授权。CORS允许列表不构成鉴权。能访问API者可以读所有项目、创建修订版并提交。
- Compose将Postgres/NATS/监控端口映射到默认全接口；数据库默认口令可预测，NATS未配置认证。只能作为隔离开发部署。API未限制拓扑规模/请求体、无速率限制，且run字典不回收，可能耗尽CPU和内存。
- `revision_lifecycle.py:330`先读parent再更新，`storage.py:150/315`UPDATE没有compare-and-swap条件。单进程RLock能串行化，多个Gateway进程却可能同时通过旧parent检查，造成并发覆盖。应在数据库事务中用 `UPDATE ... WHERE current_revision_id = expected_parent` 并检查rowcount，或锁定project行。
- 前端 `Run.result` 类型声明非null，运行失败后后端返回null，`App.tsx:170`等仍直接取 `.evaluation`，存在错误场景崩溃风险。
- 前端只展示前8个leaf；文案称“聚合显示”，实际是截断隐藏其他leaf而非聚合，演示1024GPU需讲清可视范围。

## 最有价值的短期优化顺序

1. 已先修正确性和错误契约，保留可复现前后对照作为核心模块分析和个人理解的证据。
2. 加一个受控只读Assistant入口，检索当前revision/run并解释瓶颈；把可执行工具限定为读取与校验。真实LLM接入需要显式provider/模型配置，规则fallback必须标注，不应伪称LLM。
3. 加服务端身份验证和角色权限，至少区分只读viewer与operator，并给401/403测试。多租户还需项目所有权，不要用“有登录”代替对象授权。
4. 下一阶段再接通真实NATS请求、超时重试、幂等与worker心跳，并测HTTP延迟、单机算法耗时、worker数扩展曲线。当前报告应写模拟分片，展示确定性恢复协议，不展示虚构线性加速。

汇报最应避免的表述：真实1024GPU运行、NCCL吞吐提高、已实现JetStream可靠消息、毫秒级真实容错、SSE实时进度、生产级权限隔离、最优放置算法。
