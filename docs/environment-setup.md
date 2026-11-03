# 环境配置

## 项目虚拟环境

项目根目录：解压后的 `FabricSandbox_v2`。每台电脑使用自己的路径，不复制
其他成员的绝对路径。

虚拟环境：

```text
<项目根目录>/.venv
```

当前环境由标准库 `venv` 创建，不使用 uv：

```text
Python 3.13.12
pip 25.3
```

### 直接运行

不需要激活环境：

```powershell
.\.venv\Scripts\python.exe scripts\check_env.py
.\.venv\Scripts\python.exe scripts\run_tests.py
```

### 激活后运行

```powershell
.\.venv\Scripts\Activate.ps1
python scripts/check_env.py
python scripts/run_tests.py
```

退出环境：

```powershell
deactivate
```

如果 PowerShell 阻止激活脚本，直接使用 `.venv\Scripts\python.exe` 即可，不建议
为了一个项目永久修改系统执行策略。

## 换项目目录

源码可以移动，`.venv` 不可直接移动。进入新的项目根目录后重新创建：

```powershell
python -m venv .venv
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pip install -r requirements-demo.txt
.\.venv\Scripts\python.exe scripts\check_env.py
.\.venv\Scripts\python.exe scripts\run_tests.py
```

Web/API 与测试依赖从 `requirements-demo.txt` 安装，始终使用项目环境：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-demo.txt
```

不要使用全局 `pip install`，否则依赖不会进入项目 `.venv`。

## 支持范围

| 项目 | S0 要求 |
|---|---|
| Python | 3.12 或 3.13 |
| pip | 由 venv 提供 |
| 系统 | 64 位 Windows、macOS 或 Linux |
| 编码 | UTF-8 |
| Docker、PostgreSQL、NATS、Node.js | 当前不要求 |

后续启用阶段：

- S8：Docker Compose 与 PostgreSQL 16。
- S9：NATS Server 2.10+ 并启用 JetStream。
- S11：Node.js 20/22/24 与 pnpm 10+。

完整环境检查：

```powershell
.\.venv\Scripts\python.exe scripts\check_env.py --full
```

## 配置文件

- `.env`：当前本地配置，已被 Git 忽略。
- `.env.example`：可以提交的环境变量模板。
- `.python-version`：期望 Python 3.13。
- `.vscode/settings.json`：默认使用项目 `.venv` 和 unittest。

## 环境变量

| 变量 | 默认值 | 启用阶段 |
|---|---|---|
| `FABRIC_SANDBOX_ENV` | `local` | 全阶段 |
| `LOG_LEVEL` | `INFO` | 全阶段 |
| `API_HOST` | `127.0.0.1` | S7 |
| `API_PORT` | `8000` | S7 |
| `WEB_PORT` | `5173` | S11 |
| `POSTGRES_HOST` | `127.0.0.1` | S8 |
| `POSTGRES_PORT` | `5432` | S8 |
| `POSTGRES_DB` | `fabric_sandbox` | S8 |
| `POSTGRES_USER` | `fabric` | S8 |
| `POSTGRES_PASSWORD` | `fabric_dev_only` | S8 |
| `DATABASE_URL` | 本地 PostgreSQL URL | S8 |
| `NATS_HOST` | `127.0.0.1` | S9 |
| `NATS_PORT` | `4222` | S9 |
| `NATS_MONITOR_PORT` | `8222` | S9 |
| `NATS_URL` | `nats://127.0.0.1:4222` | S9 |
| `NATS_STREAM` | `FABRIC_RUNS` | S9 |

示例密码仅用于本地开发。进入部署阶段前必须更换。

