# 仿真与 GPU 实测执行说明

## 已交付内容与证据边界

- `scripts/run_v2_experiments.py`：在普通 CPU 电脑上运行 DDP/TP 仿真及解析校验，导出输入、结果、事件、指标、约束检查与精确 SVG 曲线。
- `scripts/profile_gpu_workload.py`：用户在 GPU 服务器上手动运行的可选工具。单卡模式测 GPT-2 Small 的计算和显存，集合通信模式测指定张量大小的 NCCL AllReduce。
- 当前机器没有 PyTorch/CUDA，GPU 实测状态为 `NOT_RUN`。脚本的帮助、语法与缺少依赖时的退出路径已经验证；不能说 GPU 测量路径已经在 3090 上通过。
- TP 仿真中的 Qwen2.5-7B 计算时间仍是 `SYNTHETIC`。该脚本没有运行 Qwen 推理，不能用 GPT-2 profile 替代 Qwen 的 prefill/decode profile。

## 1. 先复现全部仿真（组员可以独立完成）

在项目根目录、使用项目 Python 3.12/3.13 环境运行：

```bash
.venv/bin/python scripts/run_v2_experiments.py --list
.venv/bin/python scripts/run_v2_experiments.py --output evidence/v2/my_simulation_run
```

Windows 对应使用 `.venv\Scripts\python.exe`。输出目录应为空；如确实需要覆盖同一组结果，显式添加 `--overwrite`。不要把不同版本的结果混到同一个目录。

主验收共 16 项：E0 解析 Ring；DDP 紧凑/跨机架/各作业孤立、成组等待、显存不可调度、有/无备用卡故障、检查点写入中故障；TP=1/2/4、两个副本争用网络、KV 不足拒绝、整个副本故障。

每个实验目录包含：

| 文件 | 作用 |
|---|---|
| `input.json` | 完整冻结输入，可以回放 |
| `result.json` | 实际仿真输出，含输入 hash、模型假设、运行统计 |
| `profile.json` | 参数与来源标签；不含虚构的 GPU 测量 |
| `events.jsonl` | 每次状态/传输变化的模拟时间和原因 |
| `metrics.csv` | 每个训练作业或推理请求的指标 |
| `link_timeseries.csv` | 方向资源容量和实际分配随模拟时间的变化 |
| `checks.json` | 引擎不变量以及该情景的业务验收断言 |
| `bottleneck.svg` | 从原始样本绘制的阶梯曲线；纯本地无通信场景可能没有此图 |

顶层 `summary.json` 汇总执行与验收；`contention_comparison.csv` 对照同一 GPU 放置下 DDP 作业独占/并发的完成时间。`PASS` 表示模型断言通过，不意味着所有业务任务都成功。例如 KV 超限被正确拒绝，本身就是通过的测试。

原始实验采用合成参数。仿真训练耗时和脚本自身的 `engine_wall_ms` 分别记录。确定性重放不能被当成统计独立样本来计算置信区间。

## 2. 准备 GPU 测量环境（只在服务器执行）

先把项目复制到服务器工作目录，进入项目根目录。优先使用现有、能正常执行 PyTorch CUDA 的 Python 环境。若需独立环境，可使用：

```bash
uv venv --python 3.12 .venv-profile
uv pip install --python .venv-profile/bin/python torch transformers
.venv-profile/bin/python scripts/profile_gpu_workload.py --help
uv pip freeze --python .venv-profile/bin/python > profile_requirements_resolved.txt
```

上述安装只用于可选的实测环境，不修改仿真项目的依赖，不安装或更新 NVIDIA 驱动。解析到的版本必须与实验结果一起保存；CUDA 可用性取决于服务器已有驱动和所装 PyTorch 构建。若服务器已有合适环境，把下列 `.venv-profile/bin/python` 替换为那个解释器路径即可。

GPT-2 用 `GPT2Config` 随机初始化，直接生成 token ID，不调用 `from_pretrained`，无需下载权重、tokenizer 或语料。它检验计算/通信负载，不测语言模型训练效果。

## 3. 单卡计算与显存 profile（案例一）

```bash
CUDA_VISIBLE_DEVICES=0 .venv-profile/bin/python scripts/profile_gpu_workload.py profile \
  --batch-size 1 --sequence-length 512 --warmup 5 --repeat 20 \
  --output evidence/gpu/gpt2_fp32_b1_s512.json
```

脚本在预热后记录每次 CUDA 前后向耗时、优化器耗时、总 GPU 时间和同步后的墙钟时间；参数/梯度都是 FP32，优化器固定为 AdamW（foreach=False）。参数量必须等于 124,439,808，梯度字节必须等于 497,759,232，否则明确失败。

显存报告区分 `peak_allocated_bytes` 与 `peak_reserved_bytes`。`admission_budget_bytes` 在 reserved 峰值上加一个明确配置的余量（默认 1 GiB），这个余量是策略，不是测量值。单卡测量未创建 DDP 梯度桶，也没有覆盖所有框架外/NCCL 显存，因此最终准入仍需保守检查。

测完后，组员应核对 JSON 的 `status=COMPLETED`，再把中位数映射到新建的仿真 profile：

| GPU 测量字段 | DDP 仿真字段 |
|---|---|
| `timing.forward_backward_ms.median_ms` | `compute_ms` |
| `timing.optimizer_ms.median_ms` | `optimizer_ms` |
| `memory.admission_budget_bytes / 2^30` | `peak_memory_gib` |

不要覆盖旧 SYNTHETIC 版本。新增 `source=MEASURED` 时，必须同时提供 `compute_ms`、`optimizer_ms`、`peak_memory_gib` 和 `measurement_id`；measurement_id 可引用归档原始文件的 SHA256，并保留实验配置。检查点保存、恢复时间若仍是设定值，应分别说明，不能因计算时间实测就把整个系统都标为已校准。v2 的 `field_sources` 会区分这些来源。

## 4. 两卡、四卡集合通信测量（两个案例共用的基础校验）

先测同 NUMA 的卡，再测跨 NUMA；这些卡号来自用户给出的拓扑。执行前再次核对当前服务器的 `nvidia-smi topo -m`。

```bash
CUDA_VISIBLE_DEVICES=0,1 .venv-profile/bin/python -m torch.distributed.run --standalone --nproc-per-node=2 \
  scripts/profile_gpu_workload.py collective --bytes 497759232 --dtype float32 \
  --warmup 5 --repeat 20 --output evidence/gpu/allreduce_0_1.json

CUDA_VISIBLE_DEVICES=0,4 .venv-profile/bin/python -m torch.distributed.run --standalone --nproc-per-node=2 \
  scripts/profile_gpu_workload.py collective --bytes 497759232 --dtype float32 \
  --warmup 5 --repeat 20 --output evidence/gpu/allreduce_0_4.json

CUDA_VISIBLE_DEVICES=0,1,2,3 .venv-profile/bin/python -m torch.distributed.run --standalone --nproc-per-node=4 \
  scripts/profile_gpu_workload.py collective --bytes 497759232 --dtype float32 \
  --warmup 5 --repeat 20 --output evidence/gpu/allreduce_0_1_2_3.json
```

每次计时前重置张量为 1，并在通信组内同步；计时使用 CUDA Event，结束后同步。每轮以最慢 rank 的耗时统计，不用跨进程绝对时间戳相减。最终整个张量应等于 rank 数，否则测量失败。

结果记录每个 rank 的耗时样本、设备信息、可见卡映射，以及算法带宽和归一化 bus bandwidth。后者使用 NCCL Tests 的 `2(R−1)/R` 因子；它不是某条物理链路实际占用的直接测量。程序不会强制改变 NCCL_ALGO 或传输配置，不能默认实际选择的是 Ring。

案例二若要测试小消息，可在保持相同 GPU 组合的前提下改变 `--bytes`，例如 `--bytes 7168 --dtype bfloat16` 对应一个 hidden=3584 的 BF16 激活向量。此举只验证一种集合通信负载，不等于测得 Qwen 整体 token 延迟。

## 5. 如何使用结果与判定失败

- `NOT_RUN`：依赖或 CUDA 不可用，没有取得性能结果；不能记为 0 ms。
- `FAILED`：执行发生异常或数据校验不通过；应先排查，不能选取部分数据当成功。
- `COMPLETED`：当前固定配置的测量结束，仍需核对环境和假设。

单机 3090 的两个 NUMA 组不是两个机架。P2P read 显示 CNS 不能直接推导所有 NCCL 通信不可用；实际走什么通路需要进一步日志或专门测量。当前仿真中的机内共享资源是简化，不能用跨机架模拟结果解释全部 NUMA 差异。

仿真没有计算/通信重叠，真实 DDP 可能重叠，因此不要硬把“单卡前后向 + 单独 AllReduce”说成真实 DDP 的精确步时长。这里的测量主要用于检查字节量、数量级和资源约束。

## 6. 组员交付检查

组员提交原始 JSON、依赖版本、实际 GPU 组合、完整命令、复现备注以及一页对照表；不要只发截图。没有运行的内容保留 NOT_RUN。展示图上同时注明“仿真/实测”、精度、消息字节、卡数、样本次数和单位。

参考：

- PyTorch CUDA Event：https://docs.pytorch.org/docs/stable/generated/torch.cuda.Event.html
- PyTorch DDP：https://docs.pytorch.org/docs/stable/notes/ddp.html
- Transformers GPT-2 配置：https://huggingface.co/docs/transformers/model_doc/gpt2
- NCCL Tests 带宽定义：https://github.com/NVIDIA/nccl-tests/blob/master/doc/PERFORMANCE.md
