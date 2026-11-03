# S7 Gateway API

## S7 在做什么

S7 把领域模型、校验、放置、带宽评估和修订版生命周期组合成一个 FastAPI
服务，并暴露 REST API、运行事件和 OpenAPI 文档。

当前状态存储在进程内存中，重启后清空。S8 将替换为 PostgreSQL 持久化。

## 启动

```powershell
.\.venv\Scripts\python.exe scripts\start_gateway.py
```

浏览器访问：

```text
http://127.0.0.1:8000/docs
```

## 主要接口

```text
GET    /api/health
POST   /api/projects
GET    /api/projects
POST   /api/projects/{project_id}/revisions
GET    /api/projects/{project_id}/revisions
GET    /api/revisions/{revision_id}
POST   /api/revisions/{revision_id}/validate
POST   /api/revisions/{revision_id}/commit
GET    /api/revisions/{left_id}/diff/{right_id}
POST   /api/runs
GET    /api/runs/{run_id}
GET    /api/runs/{run_id}/events
```

只有 `validated` 或 `committed` 修订版可以运行。运行结果同时包含放置方案和 S5
路径/带宽评估。

## 自动验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

集成测试覆盖项目和修订版、校验与提交、草稿运行拒绝、放置评估、运行查询、diff
和 SSE 事件。
