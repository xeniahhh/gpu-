# C 组任务交付状态

更新日期：2026-11-03

团队映射：A = 熊宇琛，B = 王忻晔，C = 马贤纯。

## 已完成

- D17 / T17：真实 HTTP 提交、查询、错误输入和 TP 场景接口。
- D18 / T18：不可变输入、SQLite 结果、事件回放和重启状态。
- D19 / T19：匿名、viewer、editor 权限和输入边界。
- D20 / T20：规则型只读解释，确认返回前后运行记录不变。
- D21 / T21：DDP 与 TP 页面、状态时间线、容量曲线和解释区。
- D22：系统架构、数据关系、DDP 状态、TP 请求状态和技术边界已写入报告与 PPT。
- D23 / T23：已形成 12 页报告和 17 页模板版 PPT；最终 PPT 仍按团队要求留到最后修改。
- TP 第二案例：9/9 个代表场景 PASS，6/6 个 CPU 扩展实验完成。
- A/B/C 集成：A、B 摘要复制到项目内，新增受权限保护的
  `GET /api/v2/evidence`，页面顶部展示三人状态、关键指标、来源口径和实现 hash。

## 实际验收

- 当前完整测试：180/180 通过。其中 178 项为 A/B 基线，2 项为 C 的
  A/B/C 集成证据接口测试。
- 集成后的 HTTP 验收：16/16 通过。
- 浏览器：Edge 实际连接 14 个场景，显示 A/B/C 证据区、9 个指标、
  2 个作业行、状态时间线、容量曲线和 4 项约束检查。
- 服务重启：3/3 通过，completed 记录保留，未完成任务变为 interrupted。
- 实现 hash：`d844734439ed524fe81d843d0065a17510ebf161c5efc036f31cf4bb061cd4e3`。
- 本地 Git 交付标签：`c-delivery-20261103`；标签指向包含全部 C 交付物的固定提交。

## 主要证据

- `evidence/integration_1103/http_acceptance.json`
- `evidence/integration_1103/browser_integrated.json`
- `evidence/integration_1103/browser_integrated.png`
- `evidence/integration_1103/provenance.json`
- `evidence/integration_1103/SHA256SUMS.txt`

## 尚未完成

- 将本地标签 `c-delivery-20261103` 对应的提交发布到可访问的 GitHub/GitLab 仓库。
- 在答辩电脑上重新执行最终启动和接口检查。
- 录制五分钟演示视频并完成三人排练。
- 根据最终演示稿再修改 PPT 并冻结。

因此，C 的网页和证据整合已完成，但 C 任务整体仍未关闭。
