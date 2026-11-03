# S6 修订版生命周期与对比

## S6 在做什么

S6 在内存中实现项目修订版管理：

```text
DRAFT -> VALIDATED -> COMMITTED
      -> VALIDATION_FAILED
```

- 修订正文使用不可变对象保存。
- 草稿创建后正文不能原地修改，编辑会产生新修订版。
- 只有校验通过的修订版可以提交。
- 提交前检查父版本是否仍是项目当前版本，防止旧草稿覆盖新状态。
- 任意两个修订版可以按实体 ID 比较新增、删除和修改。

S6 使用内存仓库，进程退出后数据消失。PostgreSQL 持久化属于 S8。

## 关键规则

1. 新建项目时没有当前修订版。
2. 新草稿的父版本自动设置为项目当前修订版。
3. 校验失败后状态为 `validation_failed`，不能提交。
4. 提交成功后，项目当前修订版指向该修订版。
5. 旧修订版正文和内容哈希永不改变。
6. 同一内容只改变运行修订 ID 时，内容哈希保持相同。

## 内容哈希

`content_hash` 是规范化 JSON 的 SHA-256：

- 字段按名称排序。
- 不包含仅供运行时使用的 `revision_id`。
- 正文的拓扑、作业和 schema version 全部参与计算。

比较修订版时会忽略运行时 `revision_id`，按实体 ID 展示实际内容变化。

## 如何查看结果

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s6.py
```

演示流程：

1. 创建并提交正文为原始的 revision 1。
2. 修改 `job-ring-01` 优先级为 20，创建 revision 2。
3. 校验 revision 2 并显示相对 revision 1 的 diff。
4. 提交 revision 2。
5. 创建一个 GPU 请求超过拓扑容量的 revision 3。
6. 显示 revision 3 校验失败且提交被拒绝。

预期 diff 包含：

```text
jobs.job-ring-01: priority: 10 -> 20
summary: +0 -0 ~1
```

## 独立验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

S6 测试覆盖完整状态机、未校验提交拒绝、校验失败、父版本、内容哈希不变性、
修订版 diff、旧版本不可变性和结果查看命令。
