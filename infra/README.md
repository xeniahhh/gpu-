# 本地教学部署

Compose 包含 app、PostgreSQL、NATS，可选 `nats-workers` profile。当前配置仅面向本地教学：所有发布到宿主机的 API、数据库、NATS 和 NATS monitor 端口均绑定 `127.0.0.1`。容器间通过 Compose 网络通信。

启动前生成并设置独立随机密钥，示例在项目根目录运行：

```bash
export FABRIC_EDITOR_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export FABRIC_VIEWER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose -f infra/compose.yaml up --build
```

Compose 未设置非空 `FABRIC_EDITOR_TOKEN` 时会在配置插值阶段明确报错。服务启动时还校验密钥长度至少 24 个 ASCII 字符、两个角色密钥不同。`FABRIC_VIEWER_TOKEN` 可省略，此时只有 editor token 有效。前端输入 token 后访问数据 API。健康检查公开，返回认证模式。

项目根目录 `.env` 不会因 `-f infra/compose.yaml` 自动作为期望的配置来源。建议使用 shell 环境变量，或明确指定 `docker compose --env-file .env -f infra/compose.yaml ...`。不要把真实密钥写入版本控制。

PostgreSQL 默认口令和无认证 NATS 都是本地开发设置。NATS monitor 无认证，不能公开暴露。loopback 端口绑定不能隔离同机进程或 Compose 网络内的其他容器。共享部署仍需 NATS 认证/TLS、数据库独立凭据、网络隔离和正式用户/项目权限。

Gateway 目前未把真实运行链路接入 NATS，因此可选 worker 容器不等于分布式端到端功能已经完成。本次仅修改并静态检查 Compose 配置，未进行容器启动或通信实测。
