# 案例 2：Qwen2.5-7B TP 基线冻结记录

冻结日期：2026-11-03

本记录冻结当前 TP 第二案例的实验范围、解释边界和证据。它不是新的核心功能开发基线。主线仍为 GPT-2 Small 多作业 DDP 训练，TP 作为第二案例。

## 冻结结论

- 场景：Qwen2.5-7B-Instruct 结构的 TP=1/2/4 流级仿真。
- 工作负载：固定输入/输出长度的请求；每个副本一次处理一个请求。
- 通信：Megatron 式层内激活 AllReduce 抽象。
- 显存：权重分片、激活与静态最大长度 KV 预留。
- 故障：整个副本故障使在途请求失败；不实现无损 KV 迁移。
- 参数：计算 profile、网络和恢复参数均为合成设定。
- GPU 实测：`NOT_RUN`。没有被测 Qwen 硬件性能。

## 冻结实验

| 实验 | 作用 | 状态 |
|---|---|---|
| `E0_analytic` | Ring 字节量与解析时间参考 | PASS |
| `T1_tp1` | 单卡副本基线 | PASS |
| `T1_tp2` | 两卡 TP 基线 | PASS |
| `T1_tp4` | 四卡 TP 基线 | PASS |
| `T2_pair_control` | 两副本共享网络 | PASS |
| `T2_A_alone` | 请求 A 独占对照 | PASS |
| `T2_B_alone` | 请求 B 独占对照 | PASS |
| `T2_kv_reject` | 最大长度 KV 预算拒绝 | PASS |
| `T3_fault` | 整副本故障与健康副本继续 | PASS |

以上 9/9 场景的各场景 `checks.json` 均通过。`PASS` 只表示列出的模型不变量成立，不表示所有请求一定成功。

## 冻结数据

- 9 个场景，每个场景 8 个证据文件。
- HTTP 验收：15/15 通过。
- TP 多副本浏览器验收：12 个指标、4 个请求、6 个约束检查。
- CPU 单因素补充实验：6/6 完成。
- 仿真实现 hash：`d844734439ed524fe81d843d0065a17510ebf161c5efc036f31cf4bb061cd4e3`

补充实验的合成结果：

| 轴 | 值 | 平均 TTFT | 最后完成时刻 | 处理事件数 |
|---|---:|---:|---:|---:|
| 上联带宽 | 25 Gbps | 60.72334336 ms | 92.21404672 ms | 4484 |
| 上联带宽 | 50 Gbps | 44.28167168 ms | 75.38702336 ms | 4484 |
| 上联带宽 | 100 Gbps | 36.06083584 ms | 66.97351168 ms | 4484 |
| 请求量 | 2 | 44.28167168 ms | 75.38702336 ms | 4484 |
| 请求量 | 4 | 81.97518336 ms | 150.77404672 ms | 8968 |
| 请求量 | 8 | 157.36220672 ms | 301.54809344 ms | 17936 |

带宽实验只改变上联带宽；请求量实验只改变请求数量。单次墙钟不构成统计显著性证据。

## 代码冻结 hash

以下 SHA-256 用于识别冻结时实际使用的关键文件：

| 文件 | SHA-256 |
|---|---|
| `packages/fabric-sim/src/fabric_sim/tp.py` | `C05234F0B5D910F93E1E3BD1FC5B4E1F21440F84D7ECD2E7D27DB288828B2085` |
| `packages/fabric-sim/src/fabric_sim/core.py` | `12715A165F272169D9A871935FF48FB3E69841C4A41E11B6EEC4A80D590DE86E` |
| `packages/fabric-sim/src/fabric_sim/validation.py` | `84F5219263858AA1ED157428D79EE21EEF50DD0075B60D2312349D3D670710F5` |
| `scripts/run_v2_experiments.py` | `50496A2866586B8B00AF5A4218AD2AF26CB7C6DCAF9B100048DEC0CF816F47C1` |
| `scripts/verify_task_c.py` | `492AB54829FEAC28B020320FAEEED8CE0AFD45C8CF58F867FE3417D67721A034` |
| `scripts/browser_verify_task_c.ps1` | `4EBC005CE3A3901E281D02628CDFFBB8D4D431895B7C28DB08C416B7EFA2AFE7` |
| `apps/web/workload.html` | `5E8057B9575AA90CC704A4E23446E9C3AF628A1D46A7BEC1F062C8C3A4018C89` |
| `services/gateway/src/gateway/simulation_api.py` | `634FA57788B9A9561EEEF0E20CBBA0669D269AF6BDB9EF121BBC0958CF232AD6` |

## 后续允许与不允许

允许进行：

- 把既有结果绘制成报告图表。
- 整理报告、PPT、演示脚本和贡献说明。
- 修复不影响实验含义的排版或文档错误。

必须重新解冻并重跑的情况：

- 修改 `tp.py`、`core.py`、输入校验或 TP 场景定义。
- 修改 Qwen 结构、TP 分配、KV 公式或请求状态语义。
- 更换 profile 来源、带宽或请求集。
- 修复影响结果的事件、资源或故障逻辑。

当前不得宣称：

- 已运行真实 Qwen2.5-7B 推理。
- 已获得真实 GPU TTFT、token 吞吐或 NCCL 带宽。
- 已实现连续批处理、PagedAttention 或 KV 迁移。
- 已在两台真实机架之间验证 TP 跨机架性能。
- TP2 在所有真实硬件上都优于 TP1 或 TP4。

## 证据位置

- 主仿真：`evidence/team_1103_tp/`
- 单因素补充：`evidence/team_1103_tp_extra/`
- 结果说明：`evidence/team_1103_tp/README.md`
- HTTP 验收：`evidence/team_1103_tp/http_acceptance.json`
- 浏览器验收：`evidence/team_1103_tp/browser_tp_acceptance.json`
- 页面截图：`evidence/team_1103_tp/browser_tp_acceptance.png`
