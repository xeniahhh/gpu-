# 实施进度

| 步骤 | 内容 | 状态 |
|---|---|---|
| S0 | 干净工程、根目录 venv、环境配置、测试入口 | Complete |
| S1 | 领域模型、契约和微型拓扑 | Complete |
| S2 | 拓扑生成与语义校验 | Complete |
| S3 | first-fit 放置 | Complete |
| S4 | rail-aware 放置 | Complete |
| S5 | 路径与影响指标 | Complete |
| S6 | 修订版生命周期 | Complete |
| S7 | Gateway API 与本地闭环 | Complete |
| S8 | SQLite/PostgreSQL 持久化 | Complete |
| S9 | 多 worker 评估与故障重分片 | Complete |
| S10 | 故障注入与重规划 | Complete |
| S11 | React/SVG 界面 | Complete |
| S12 | 综合验收与演示 | Complete |

## S0 验收

```powershell
.\.venv\Scripts\python.exe scripts\check_env.py
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S1 验收

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s1.py
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S2 验收

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s2.py
.\.venv\Scripts\python.exe scripts\inspect_s2.py --preset micro
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S3 验收

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s3.py
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S4 验收

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s4.py
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S5 验收

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s5.py
.\.venv\Scripts\python.exe scripts\inspect_s5.py --flow-unit-gbps 200
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S6 验收

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s6.py
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S7 验收

```powershell
.\.venv\Scripts\python.exe scripts\start_gateway.py
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S8 验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S9 验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S10 验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

## S11-S12 验收

```powershell
.\scripts\run_checks.ps1
.\.venv\Scripts\python.exe scripts\start_gateway.py
```

