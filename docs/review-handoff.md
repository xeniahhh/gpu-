# 2026年11月1日复核与运行说明

本目录是课程项目的优化副本。源目录未改动。已有历史文档中的“完成S0–S12”描述不能理解为真实分布式、LLM或真实GPU集群已经验证。

## 本次实际修改

- 先统一选路再分片，按全局竞争重算flow/job/link速率，并由flow累加链路承载，修正200 Gbps链路的flow合计400 Gbps问题。
- 检查互斥分片与一致模型配置，保持1/2/3 worker及单worker失效结果一致。
- 网络故障与worker失效同时出现时明确409拒绝，未知worker拒绝，未放置作业标为degraded。
- 增加规则型只读Assistant和viewer/editor共享Bearer权限。前端token仅保留在内存，不写URL或localStorage。
- 增加前端失败结果处理与互斥故障选择。普通Vite构建仍是标准路径，本次另提供可复现离线构建脚本和新版dist。
- Compose发布端口绑定127.0.0.1，容器环境要求显式editor token。NATS仍是未认证本地开发配置。

## 已验证与未验证

2026年11月1日，Python 3.12.14、macOS arm64：

- 87项单元测试通过，其中新增17项纯安全/助手测试。
- 4项S9进程内分片集成测试通过，不包含NATS网络。
- 前端app/node两项TypeScript检查通过，离线静态产物已构建。
- Compose YAML和token/端口规则静态检查通过。
- 基准包含1次预热及5次原始样本，见benchmark_review.json和benchmark_review.md。

FastAPI/httpx无法在本次受限网络环境安装，因此HTTP集成测试未运行。浏览器端到端、Docker/NATS/PostgreSQL实机联调未完成。现有dist的生成不等于UI端到端验证通过。正式汇报前须在可运行环境完成这些检查。

## 本地启动（macOS/Linux）

需要Python 3.12或3.13。首次设置：

```bash
python3.12 -m venv .venv
cp .env.example .env
.venv/bin/python -m pip install -r requirements-review.txt
.venv/bin/python scripts/start_gateway.py
```

打开 http://127.0.0.1:8000/ 。已有新版dist可由Gateway直接提供，无需先启动Vite。联网且需要重建前端时，在apps/web执行 `pnpm install && pnpm build`。缺少Vite原生依赖但JS依赖齐全时，可用 `node build-offline.mjs`，并单独运行TypeScript检查。

若使用uv，可用 `uv venv --python 3.12 .venv` 与 `uv pip install --python .venv/bin/python -r requirements-review.txt`。不要复制本次机器的虚拟环境到别的机器。

项目完整测试：

```bash
.venv/bin/python scripts/run_tests.py
.venv/bin/python scripts/benchmark_review.py --repeats 5 --output docs/benchmark_review-rerun.json
```

纯领域测试与S9只需标准库，SOURCE_ROOTS需加入PYTHONPATH，或通过scripts/run_tests.py配置。该完整脚本同时包含HTTP测试，缺依赖时会失败，不应把缺失测试解释为通过。

在.env中设置两个独立、至少24位ASCII token后，使用前端访问设置输入令牌。viewer只读，editor能写。创建与运行演示需editor。不要将实际token提交至仓库。无token时只允许本机回环直连，并拒绝代理头、外部Host或Origin。

Compose的启动方法与限制见infra/README.md。这里不宣称容器启动已验证。

## 汇报取值和边界

- 1024 GPU是模型规模：16 leaf × 8 node/leaf × 8 GPU/node，另有8个spine。
- 对比夹具在200 Gbps/流量单位时，大作业对齐率50%与100%，吞吐代理均100 Gbps。不能说0%→100%，不能说训练吞吐提高。
- 单节点无跨节点通信的proxy为null，不能当成0 Gbps。未放置时也可能null，需结合placement状态。
- 当前单进程同步执行、SSE完成后回放、NATS未接主路径、run内存存储。
- 规则Assistant不是LLM，服务角色权限不是个人身份或多租户ACL。
- 基准时延只覆盖CPU领域函数。recovery_ms不是故障发现或真实恢复时延。

## 演示前验收

1. 在实际汇报电脑安装依赖，执行完整测试并保存日志。
2. 加载三节点案例，分别运行网络故障和worker故障，检查不误报成功。
3. 用viewer验证写接口403、editor验证允许、错误token验证401。
4. 请求Assistant并确认字段证据对应当前run，失败运行不导致前端崩溃。
5. 改优先级、查看diff、提交revision，再展示旧revision未改变。
6. 准备基准JSON与CLI输出作备选。完整API未就绪时不要现场声称权限与助手已联调。

## 课程案例参考

已通过用户登录的浏览器审阅 https://github.com/lzy1113-create/AI-INFRA 的README（2026-11-01）。它面向场地与设施规划，本项目关注GPU网络。可借鉴其apps/packages边界、契约优先、不可变迁移、RLS+project_id权限隔离、not checked验证状态和双租户不同样例的检查方法。本次未复制案例代码，未对该仓库功能做运行验证。

## 最终课程交付

课件标注11月4日汇报、11月18日提交。成员与真实分工待填写，自拟Option4须确认已获教师批准。PPT与演讲手册不替代课程要求的8–12页正式架构报告。终稿还需Git仓库、构建说明、自动测试及运行证据。不要将本次AI协助修改直接说成未经辅助的个人贡献，应按实际过程说明并掌握技术细节。
