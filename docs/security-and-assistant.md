# 认证、权限与只读 Assistant

## 实现范围

新增 `security.py` 实现服务级 Bearer token 权限。配置 `FABRIC_VIEWER_TOKEN` 和/或 `FABRIC_EDITOR_TOKEN` 后启动鉴权模式。token 至少 24 个 ASCII 字符，两个角色不能共用 token。建议用 `secrets.token_urlsafe(32)` 生成，不提交到仓库。所有 `/api/` 数据接口统一检查权限，`GET /api/health` 公开。只读请求允许 viewer/editor，项目创建、revision 创建/校验/提交、运行创建和 demo bootstrap 仅 editor 可调用。

未配置 token 时只接受 loopback 传输对端访问数据 API，并在 health/info 返回 `auth_mode: local_demo`。不能把此模式放在反向代理后或作为多人共享部署。非 local 环境未配置 token 会启动失败。Host 请求头不能决定访问权限。

这是原型级共享 token 与角色授权，不含用户注册、密码、OAuth/OIDC、项目归属、多租户隔离、token 撤销列表或安全审计。共享部署必须使用 TLS，并在下一阶段引入正式身份与项目级 ACL。CORS 仅限制浏览器跨源访问，不能代替鉴权。

## API

`POST /api/assistant/explain`，viewer/editor 均可调用：

```json
{"run_id":"<已创建的run id>","question":"请解释本次运行的瓶颈和局限"}
```

返回 `mode: rules_based`、`read_only: true`、`answer`、`evidence` 和 `limitations`。evidence 的 `field` 定位已有 run JSON 字段，`value` 取自该字段。回答可说明未放置作业、无法路由的流、请求负载最高链路、故障迁移及分片重分配。问题文本最多 1000 字符，只选择解释视角，不执行命令，也不会改变项目或修订版。

本功能是**规则型只读 Assistant**，未接入 LLM，不能称作大模型 Assistant 已完成。课程如要求大模型调用/自然语言规划，此项仍需补齐。建议后续由 LLM 调用受限只读工具，并保留字段证据，写操作通过独立权限与显式确认流程实现。

返回的 `utilization` 在现有模型中定义为请求需求/链路容量，可大于 1；Assistant 明确指出它不是实测链路利用率。结果仅为静态带宽代理，rail 对齐也不是实测吞吐提升。

## curl 示例

```bash
curl -H "Authorization: Bearer $FABRIC_VIEWER_TOKEN" http://127.0.0.1:8000/api/projects
curl -X POST -H "Authorization: Bearer $FABRIC_VIEWER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"替换为实际run_id","question":"解释瓶颈"}' \
  http://127.0.0.1:8000/api/assistant/explain
```

缺失/错误 token 返回 401，viewer 执行写操作返回 403。测试 `tests/integration/test_auth_assistant.py` 覆盖主要写入口、读取鉴权、无 token 远端访问拒绝、只读解释与问题中的恶意写指令不会产生副作用。

## 本地演示的请求边界

本地模式除验证传输对端为 loopback 外，还拒绝 Forwarded/X-Forwarded-For/X-Real-IP 代理头、非本地主机 Host，以及来自非本地 8000/5173 端口的浏览器 Origin。这样不会只凭 Host 或转发头把远端请求当作本地用户。测试客户端的 `testclient` 对端和 `testserver` 主机仅用于无网络的 Starlette 测试传输。部署到代理后必须启用 token 模式。

Compose 固定使用 `FABRIC_SANDBOX_ENV=compose`，要求提供非空 editor token，并把发布端口绑定到 127.0.0.1。详情见 `infra/README.md`。NATS 仍是无认证的本地开发配置，未宣称容器或消息总线联调成功。
