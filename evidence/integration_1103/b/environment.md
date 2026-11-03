# B 环境与基线复现记录（2026-11-03）

- 机器：Windows 10 Pro 19045，Python 3.12.10（`python_version.txt`），无 GPU 参与
- 代码：`FabricSandbox_v2_重构代码与实验交付.zip`，implementation_hash `d844734439ed524fe81d843d0065a17510ebf161c5efc036f31cf4bb061cd4e3`
- 依赖：`pip_freeze.txt`

## 测试

| 轮次 | 操作 | 结果 | 日志 |
|---|---|---|---|
| 1 | 按说明只装 `requirements-demo.txt` | 运行 156 项，2 FAIL、5 ERROR | `run_tests_1_requirements_demo_only.log` |
| 2 | 补装 `psycopg[binary]>=3.2,<4.0`，`copy .env.example .env` | 178 项全部 OK | `run_tests_2_after_psycopg_env.log` |

第 1 轮原因：
- 5 个 ERROR 都是 `ModuleNotFoundError: No module named 'psycopg'`。`gateway/runtime.py` 导入 `gateway/storage.py`，后者顶层 `import psycopg`，而 `requirements-demo.txt` 没有列出它（`requirements-review.txt` 有）。同一条导入链也在 `scripts/serve_workloads.py` → `gateway.main` 上，只装 demo 依赖时网页服务同样无法启动。
- 2 个 FAIL 是 `tests/unit/test_fresh_workspace.py` 要求项目根目录有 `.env`；代码包只带 `.env.example`。

建议启动命令补两步：

```powershell
.\.venv\Scripts\python.exe -m pip install "psycopg[binary]>=3.2,<4.0"
copy .env.example .env
```

## 19 情景复现

`scripts/run_v2_experiments.py --output evidence/team_1103_baseline`：19/19 PASS。

与 `evidence/v2/revalidated` 逐项比较：
- `metrics.csv`、`link_timeseries.csv`、`contention_comparison.csv` 逐字节相同。
- `input.json`、`checks.json`、`events.jsonl`、`profile.json`、`bottleneck.svg` 去掉换行符差异（本机 CRLF，原件 LF）后相同。
- `result.json` 按 JSON 内容比较，唯一不同字段是 `engine_wall_ms`（程序墙钟）。
- `summary.json` 不同字段只有 `python`、`recorded_at_utc`、各项 `engine_wall_ms` 和 `export_wall_ms`。

结论：本机复现了原基线的全部仿真结果。`E0_analytic/result.json` 没有 `implementation_hash` 字段，其余 18 项均为上面的 hash。
