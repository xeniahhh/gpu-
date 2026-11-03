# FabricSandbox v2：GPU 通信网络规划与故障分析

课程 MAiNS 自选项目的重构实现。主场景为 GPT-2 Small 多作业同步数据并行训练；第二场景为 Qwen2.5-7B 张量并行推理。输入模型 profile、GPU/网络资源、工作负载、放置与故障轨迹，输出完成时间、状态变化、链路竞争和约束检查。

**这是事件驱动的流级仿真器，不会运行神经网络或控制真实 GPU。** 示例计算时间、显存峰值、链路参数中的 SYNTHETIC 值均为设定值。真实 GPU 测量脚本单独提供，尚未在服务器执行。原有静态规划页面保留为 `/legacy`，本版本入口为 `/workload`。

## 快速开始

需要 Python 3.12 或 3.13。仿真 CLI 只用标准库，不必安装 CUDA、不必下载模型。

```bash
python3 scripts/run_v2_experiments.py --output evidence/my_run
```

安装 Web/API 与测试依赖（macOS/Linux）：

```bash
python3 -m venv .venv
cp .env.example .env
.venv/bin/python -m pip install -r requirements-demo.txt
.venv/bin/python scripts/run_tests.py
.venv/bin/python scripts/serve_workloads.py --state-dir var/demo
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pip install -r requirements-demo.txt
.\.venv\Scripts\python.exe scripts\run_tests.py
.\.venv\Scripts\python.exe scripts\serve_workloads.py --state-dir var/demo
```

浏览器访问 http://127.0.0.1:8000/workload 。默认仅绑定本机；加 `--auth` 可生成演示用 viewer/editor token，按终端提示粘贴到页面。不要将 token 写入报告或分享的结果文件。指定固定 `--state-dir` 才能在下次启动时继续查看原记录；省略时使用新的临时目录。

已有 uv 用户也可用 `uv venv --python 3.12 .venv` 和 `uv pip install --python .venv/bin/python -r requirements-demo.txt`。

## 五分钟演示

1. 选择 DDP 跨机架共享链路，提交，查看路径容量曲线和作业结果。
2. 改选集中放置，运行并比较；输入参数来源始终显示为仿真设定。
3. 运行资源不足情景，展示四卡作业等待而不是只占两卡。
4. 运行无备用资源的节点故障，展示整组中止、回滚、等待和恢复。
5. 切换 TP 的 KV 准入或副本故障，说明激活/KV 与训练梯度语义不同。

页面的事件是完成后的**仿真回放**，不是实时 GPU 监控。提交/查询是异步的，但本版用单个后台线程串行执行不同仿真，不宣称多进程计算加速。

## 模块与责任边界

| 模块 | 文件 | 职责 |
|---|---|---|
| 公共资源与事件引擎 | `packages/fabric-sim/src/fabric_sim/core.py` | GPU/节点/NIC/机架方向资源、有限同机带宽、活动流容量、Ring 两阶段 |
| 训练场景 | `packages/fabric-sim/src/fabric_sim/ddp.py` | 成组准入、完整模型每卡显存、步依赖、检查点、故障重启和 epoch |
| 推理场景 | `packages/fabric-sim/src/fabric_sim/tp.py` | 合法 TP、逐层激活通信、KV 预留、排队、token 与副本故障 |
| 输入合同 | `packages/fabric-sim/src/fabric_sim/validation.py` | 有限数值/上限/字段检查、输入 hash |
| 应用服务 | `services/gateway/src/gateway/simulation_api.py` | 有界异步提交、SQLite 快照/结果、查询、只读规则解释 |
| 权限 | `services/gateway/src/gateway/security.py` | viewer/editor 和本地访问边界 |
| 展示 | `apps/web/workload.html` | 场景输入、指标、状态证据与资源曲线 |
| 复现实验 | `scripts/run_v2_experiments.py` | 解析校验、独占/并发对照、准入、故障与证据导出 |

旧 `fabric-domain`、planner、aggregator 保留供兼容页面和既有测试使用。v2 不依赖 NATS；旧 worker 骨架不属于新主执行链路。新的网络采用两级有效容量资源模型，未实现任意拓扑多路径路由优化或故障后自动绕路。

## 核心接口

- `GET /api/v2/scenarios`：读取示例输入。
- `POST /api/v2/simulations`：冻结输入并返回 202、id、input_hash。
- `GET /api/v2/simulations/{id}`：查询执行状态、原输入及结果。
- `GET /api/v2/simulations/{id}/events`：分页读取结果事件。
- `POST /api/v2/simulations/{id}/explain`：只读、规则驱动解释，不是 LLM Agent。

`completed` 表示仿真程序正常结束，不表示所有训练作业或推理请求成功。故障恢复、不可调度、拒绝等应查看领域状态。协调器重启时未完成记录变成 interrupted；不自动续跑。当前共享角色不是个人账户或多租户隔离。

## 分工与交付文档

- [案例一：24 项详细任务](docs/refactor/case1_tasks.md)
- [案例二：24 项详细任务](docs/refactor/case2_tasks.md)
- [课程评分与报告要求](docs/refactor/rubric_mapping.md)
- [共同实现合同](docs/refactor/IMPLEMENTATION_CONTRACT.md)
- [真实 GPU 测量操作](docs/refactor/GPU_EXECUTION.md)
- [重构说明与验收状态](docs/refactor/DELIVERY.md)

每份任务表区分单人/联调、负责人、依赖、预计工时、交付物和验收标准。建议两天主做 DDP，TP 作为共享内核支持的第二场景；不要把两份清单的工时直接叠加。人员姓名、仓库 URL 和实际分工贡献需由团队填写。

## 模型边界

- Ring 为单桶、轮间屏障、无计算通信重叠；不是完整 NCCL 协议仿真。
- 流速率按路径各资源等分容量后的最小值分配，保证可行但可能不充分利用带宽，不宣称最优 max-min。
- 同机用有限共享总线近似；跨节点消耗两端 NIC，跨机架额外消耗方向上联。未模拟包、RDMA/PFC/ECN、PCIe详细拓扑。
- DDP 固定 world size、GPU 独占、无弹性缩卡。检查点 I/O 为设定延时，未竞争存储网络。
- TP 每副本一次处理一条请求，预留最大长度 KV；未实现连续批处理、PagedAttention、输出内容或无损 KV 迁移。
- 实际模型结构参考官方配置；推理权重大小使用模型卡舍入参数量近似。性能结论只适用于明确给定的 profile。

正式提交要求是托管 Git 代码库及 8–12 页 MD/PDF 报告，PPT 不替代报告。当前交付是本地项目，尚未发布到团队 Git 仓库。
