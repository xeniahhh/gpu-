# S8 持久化

## 两种 repository

`RevisionRepository` 是业务层使用的稳定接口，目前有两个实现：

- `SqliteRevisionRepository`：本地零依赖演示，默认写入
  `var/fabric_sandbox.db`。
- `PostgresRevisionRepository`：最终部署实现，使用 JSONB 保存修订正文。

业务层、API 和修订版状态机不依赖具体数据库。

## 后端选择

`.env` 中使用：

```text
REVISION_STORE=sqlite
SQLITE_PATH=var/fabric_sandbox.db
```

可选值：

- `memory`：仅测试，进程退出后清空。
- `sqlite`：本地持久化，默认。
- `postgres`：读取 `DATABASE_URL`。

本地启动默认使用 SQLite，因此即使没有 Docker 也能保留项目、修订正文、状态和
当前修订版指针。

PostgreSQL 建表文件：

```text
db/migrations/001_init.sql
```

提交修订版时，revision 状态和 project 当前指针在同一数据库事务中更新。

## 验证

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

测试会创建临时 SQLite 数据库，完成校验和提交，重新打开 repository 后确认数据
仍然存在。

当前环境没有 Docker，因此 PostgreSQL 适配器只完成代码和迁移验证；实际数据库连接
将在 Compose 环节启用。
