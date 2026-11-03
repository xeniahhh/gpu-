import {useMemo, useState} from "react";
import {
  api,
  setAccessToken,
  type AssistantExplanation,
  type BootstrapResponse,
  type Diff,
  type Revision,
  type Run,
  type Topology
} from "./api";

const JOB_COLORS = ["#1769e0", "#e66b2c", "#00897b", "#805ad5", "#c53030"];

function formatGbps(value: number | null | undefined) {
  if (value === null || value === undefined) return "无跨节点流量";
  return `${value.toFixed(1)} Gbps`;
}

function formatPercent(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}

function statusLabel(status: string) {
  return {
    draft: "草稿",
    validated: "已校验",
    validation_failed: "校验失败",
    committed: "已提交",
    completed: "已完成",
    degraded: "部分完成",
    failed: "失败"
  }[status] ?? status;
}

function TopologySvg({
  topology,
  run,
  faultTarget
}: {
  topology: Topology;
  run: Run | null;
  faultTarget: string;
}) {
  const width = 1040;
  const height = 430;
  const maxSpines = Math.min(topology.spines.length, 8);
  const maxLeaves = Math.min(topology.leaves.length, 8);
  const visibleLeafIds = new Set(topology.leaves.slice(0, maxLeaves).map((leaf) => leaf.id));
  const visibleNodeIds = new Set(
    topology.nodes.filter((node) => visibleLeafIds.has(node.leaf_id)).map((node) => node.id)
  );
  const positions = new Map<string, {x: number; y: number}>();

  topology.spines.slice(0, maxSpines).forEach((spine, index) => {
    positions.set(spine.id, {
      x: ((index + 0.5) / maxSpines) * width,
      y: 55
    });
  });
  topology.leaves.filter((leaf) => visibleLeafIds.has(leaf.id)).forEach((leaf, index) => {
    positions.set(leaf.id, {
      x: ((index + 0.5) / maxLeaves) * width,
      y: 205
    });
  });
  topology.nodes.filter((node) => visibleNodeIds.has(node.id)).forEach((node, index) => {
    positions.set(node.id, {
      x: ((index + 0.5) / Math.max(visibleNodeIds.size, 1)) * width,
      y: 355
    });
  });

  const assignmentColor = new Map<string, string>();
  run?.result?.placement.assignments.forEach((assignment, index) => {
    assignment.node_ids.forEach((nodeId) =>
      assignmentColor.set(nodeId, JOB_COLORS[index % JOB_COLORS.length])
    );
  });

  return (
    <div className="topology-shell">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="GPU fabric 拓扑">
        {topology.links.map((link) => {
          const from = positions.get(link.endpoint_a);
          const to = positions.get(link.endpoint_b);
          if (!from || !to) return null;
          const failed = link.id === faultTarget;
          const activeFlow = run?.result?.evaluation.flows.some((flow) =>
            flow.path_link_ids.includes(link.id)
          );
          return (
            <line
              key={link.id}
              x1={from.x}
              y1={from.y}
              x2={to.x}
              y2={to.y}
              className={failed ? "link-fault" : activeFlow ? "link-flow" : "link"}
            />
          );
        })}
        {[...positions.entries()].map(([id, position]) => {
          const isSpine = id.startsWith("spine");
          const isLeaf = id.startsWith("leaf");
          const isNode = id.startsWith("node");
          const fill = isNode ? assignmentColor.get(id) : undefined;
          const failed = id === faultTarget;
          return (
            <g key={id} transform={`translate(${position.x},${position.y})`}>
              <rect
                x={isNode ? -38 : -45}
                y={isNode ? -21 : -17}
                width={isNode ? 76 : 90}
                height={isNode ? 42 : 34}
                rx={isNode ? 10 : 8}
                className={`svg-node ${failed ? "svg-fault" : ""}`}
                style={fill ? {fill, stroke: fill} : undefined}
              />
              <text className={fill ? "svg-label-light" : "svg-label"} textAnchor="middle" dy="4">
                {id}
              </text>
            </g>
          );
        })}
      </svg>
      {(topology.spines.length > maxSpines || topology.leaves.length > maxLeaves) && (
        <p className="muted topology-note">
          大拓扑按前 {maxLeaves} 个 leaf 聚合显示；统计和计算仍覆盖全部实体。
        </p>
      )}
    </div>
  );
}

function MetricCard({
  label,
  value,
  hint
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
      {hint && <small>{hint}</small>}
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<BootstrapResponse | null>(null);
  const [currentRevision, setCurrentRevision] = useState<Revision | null>(null);
  const [draftRevision, setDraftRevision] = useState<Revision | null>(null);
  const [diff, setDiff] = useState<Diff | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [comparison, setComparison] = useState<Run[]>([]);
  const [faultType, setFaultType] = useState("node_down");
  const [faultTarget, setFaultTarget] = useState("node-1");
  const [flowUnit, setFlowUnit] = useState(10);
  const [failWorker, setFailWorker] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [token, setToken] = useState("");
  const [assistant, setAssistant] = useState<AssistantExplanation | null>(null);
  const [question, setQuestion] = useState("请解释本次运行的瓶颈、故障影响和模型局限。");

  const revision = draftRevision ?? currentRevision ?? session?.revision ?? null;
  const content = currentRevision?.content ?? session?.revision.content ?? null;

  const topology = content?.topology ?? null;
  const evaluation = run?.result?.evaluation;
  const fault = run?.result?.fault_replan;
  const distributed = run?.result?.distributed_evaluation;

  const targetOptions = useMemo(() => {
    if (!topology) return [];
    if (faultType === "node_down") return topology.nodes.map((item) => item.id);
    if (faultType === "switch_down") {
      return [...topology.spines, ...topology.leaves].map((item) => item.id);
    }
    return topology.links.map((item) => item.id);
  }, [faultType, topology]);

  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setMessage("");
    try {
      await action();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  function loadDemo() {
    return perform(async () => {
      const response = await api.bootstrap();
      setSession(response);
      setCurrentRevision(response.revision);
      setDraftRevision(null);
      setDiff(null);
      setComparison([]);
      setAssistant(null);
      setFailWorker(false);
      setFaultType("node_down");
      setFaultTarget("node-1");
      const initialRun = await api.run({
        revision_id: response.revision.id,
        strategy: "rail_aware",
        flow_unit_gbps: flowUnit
      });
      setRun(initialRun);
      setMessage(initialRun.status === "failed" || !initialRun.result
        ? "演示拓扑已加载，本次规划失败，请查看运行错误。"
        : "演示拓扑已加载，并完成首次 rail-aware 规划。");
    });
  }

  function launchScenario() {
    if (!revision) return;
    return perform(async () => {
      setAssistant(null);
      const scenarioRun = await api.run({
        revision_id: revision.id,
        strategy: "rail_aware",
        flow_unit_gbps: flowUnit,
        faults: !failWorker && faultTarget ? [{type: faultType, target_id: faultTarget}] : [],
        failed_workers: failWorker ? ["worker-1"] : []
      });
      setRun(scenarioRun);
      setMessage(
        scenarioRun.status === "completed"
          ? "规划与影响分析完成。"
          : `运行状态：${statusLabel(scenarioRun.status)}`
      );
    });
  }

  function compareStrategies() {
    if (!revision) return;
    return perform(async () => {
      setAssistant(null);
      const first = await api.run({
        revision_id: revision.id,
        strategy: "first_fit",
        flow_unit_gbps: flowUnit
      });
      const rail = await api.run({
        revision_id: revision.id,
        strategy: "rail_aware",
        flow_unit_gbps: flowUnit
      });
      setComparison([first, rail]);
      setRun(rail);
      setMessage([first, rail].some((item) => item.status === "failed" || !item.result)
        ? "部分策略运行失败，请查看各策略的错误信息。"
        : "first-fit 与 rail-aware 对比完成。");
    });
  }

  function createAndValidateRevision() {
    if (!session || !currentRevision) return;
    return perform(async () => {
      let base = currentRevision;
      if (base.status !== "committed") {
        base = await api.commit(base.id);
        setCurrentRevision(base);
      }
      const nextContent = structuredClone(base.content);
      nextContent.jobs[0].priority += 10;
      const draft = await api.createRevision(session.project.id, nextContent);
      const validation = await api.validate(draft.id);
      const nextDiff = await api.compare(base.id, draft.id);
      setDraftRevision(validation.revision);
      setDiff(nextDiff);
      setMessage("新修订版已校验，差异已生成。");
    });
  }

  function commitDraft() {
    if (!draftRevision) return;
    return perform(async () => {
      const committed = await api.commit(draftRevision.id);
      setCurrentRevision(committed);
      setDraftRevision(null);
      setMessage("新修订版已提交，当前版本指针已更新。");
    });
  }

  function resetFault() {
    setFaultType("node_down");
    setFaultTarget("node-1");
    setFailWorker(false);
  }

  function explainRun() {
    if (!run || !question.trim()) return;
    return perform(async () => {
      setAssistant(await api.explain(run.id, question.trim()));
    });
  }

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <div className="eyebrow">DATA COMMUNICATION NETWORK</div>
          <h1>FabricSandbox</h1>
          <p>GPU 集群作业放置与故障重规划沙箱</p>
        </div>
        <div className="top-actions">
          <span className="status-pill">
            <i className={busy ? "dot busy" : "dot"} />
            {busy ? "计算中" : token.trim() ? "已设置访问令牌" : "未设置访问令牌"}
          </span>
          <button className="primary" onClick={loadDemo} disabled={busy}>
            加载演示
          </button>
        </div>
      </header>

      <details className="access-settings">
        <summary>访问设置</summary>
        <label htmlFor="access-token">Bearer 访问令牌</label>
        <div className="access-input">
          <input
            id="access-token"
            type="password"
            value={token}
            autoComplete="off"
            spellCheck={false}
            placeholder="本地演示可留空"
            onChange={(event) => {
              setToken(event.target.value);
              setAccessToken(event.target.value);
              setAssistant(null);
            }}
          />
          <button className="ghost" onClick={() => {setToken(""); setAccessToken(""); setAssistant(null);}}>
            清除令牌
          </button>
        </div>
        <p>令牌只保存在当前页面内存中，刷新即清除。服务端未配置令牌时仅允许本机演示。配置后，viewer 可读和解释结果，editor 可创建、运行和提交。</p>
      </details>

      {message && <div className={message.includes("失败") ? "notice error" : "notice"}>{message}</div>}
      {run && (run.status === "failed" || !run.result) && (
        <div className="notice error" role="alert">
          运行失败：{run.error || "服务端未返回评估结果。"}
        </div>
      )}

      {!session ? (
        <main className="empty-state">
          <div className="empty-orbit">FS</div>
          <h2>从一张可计算的 fabric 拓扑开始</h2>
          <p>加载三节点演示环境，完成放置、带宽影响分析和节点故障重规划。</p>
          <button className="primary large" onClick={loadDemo} disabled={busy}>
            {busy ? "正在初始化…" : "加载 3 节点演示"}
          </button>
        </main>
      ) : (
        <main className="workspace">
          <section className="summary-row">
            <MetricCard
              label="GPU 资源"
              value={run?.result ? `${run.result.placement.assigned_gpus}/${topology?.nodes.reduce((sum, node) => sum + node.gpus.length, 0) ?? 0}` : "无评估结果"}
              hint="已分配 / 总资源"
            />
            <MetricCard
              label="作业"
              value={`${content?.jobs.length ?? 0}`}
              hint={run?.result ? `${run.result.placement.job_order.length} 个已进入调度` : "本次运行无评估结果"}
            />
            <MetricCard
              label="运行状态"
              value={statusLabel(run?.status ?? "planning")}
              hint={run?.strategy ?? "rail_aware"}
            />
            <MetricCard
              label="二分带宽代理"
              value={`${evaluation?.balanced_leaf_bisection_proxy_gbps.toFixed(0) ?? "—"} Gbps`}
              hint="balanced leaf split"
            />
          </section>

          <section className="panel topology-panel">
            <div className="panel-heading">
              <div>
                <span className="section-index">01</span>
                <h2>Fabric 拓扑与放置</h2>
              </div>
              <div className="legend">
                {content?.jobs.map((job, index) => (
                  <span key={job.id}>
                    <i style={{background: JOB_COLORS[index % JOB_COLORS.length]}} />
                    {job.id}
                  </span>
                ))}
              </div>
            </div>
            {topology && <TopologySvg topology={topology} run={run} faultTarget={faultTarget} />}
          </section>

          <div className="two-column">
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <span className="section-index">02</span>
                  <h2>放置方案</h2>
                </div>
                <button className="secondary" onClick={compareStrategies} disabled={busy}>
                  对比策略
                </button>
              </div>
              <div className="job-list">
                {run?.result?.placement.assignments.map((assignment, index) => {
                  const metric = evaluation?.jobs.find(
                    (item) => item.job_id === assignment.job_id
                  );
                  return (
                    <article className="job-card" key={assignment.job_id}>
                      <div
                        className="job-marker"
                        style={{background: JOB_COLORS[index % JOB_COLORS.length]}}
                      >
                        {index + 1}
                      </div>
                      <div className="job-main">
                        <div className="job-title">
                          <strong>{assignment.job_id}</strong>
                          <span>{assignment.node_ids.join(" · ")}</span>
                        </div>
                        <div className="gpu-strip">
                          {assignment.gpus.map((gpu) => (
                            <span
                              key={gpu.gpu_id}
                              title={`${gpu.gpu_id} / rail ${gpu.rail_id}`}
                              style={{background: JOB_COLORS[index % JOB_COLORS.length]}}
                            >
                              R{gpu.rail_id}
                            </span>
                          ))}
                        </div>
                        <div className="job-metrics">
                          <span>吞吐 {formatGbps(metric?.throughput_proxy_gbps)}</span>
                          <span>
                            rail 对齐{" "}
                            {assignment.quality
                              ? formatPercent(assignment.quality.rail_alignment)
                              : "基线"}
                          </span>
                        </div>
                      </div>
                    </article>
                  );
                })}
                {run && !run.result && <p className="muted">本次运行未产生放置方案。</p>}
              </div>
            </section>

            <section className="panel">
              <div className="panel-heading">
                <div>
                  <span className="section-index">03</span>
                  <h2>故障与重规划</h2>
                </div>
                <button className="ghost" onClick={resetFault}>
                  重置
                </button>
              </div>
              <div className="form-grid">
                <label>
                  故障类型
                  <select
                    value={faultType}
                    disabled={failWorker}
                    onChange={(event) => {
                      setFaultType(event.target.value);
                      const firstTarget =
                        event.target.value === "node_down"
                          ? topology?.nodes[1]?.id
                          : event.target.value === "switch_down"
                            ? topology?.leaves[0]?.id
                            : topology?.links[0]?.id;
                      setFaultTarget(firstTarget ?? "");
                    }}
                  >
                    <option value="node_down">节点宕机</option>
                    <option value="switch_down">交换机故障</option>
                    <option value="link_down">链路中断</option>
                  </select>
                </label>
                <label>
                  故障目标
                  <select value={faultTarget} disabled={failWorker} onChange={(event) => setFaultTarget(event.target.value)}>
                    <option value="">无 fabric 故障</option>
                    {targetOptions.map((target) => (
                      <option key={target} value={target}>
                        {target}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  每流量单位请求
                  <input
                    type="number"
                    min="1"
                    value={flowUnit}
                    onChange={(event) => setFlowUnit(Number(event.target.value))}
                  />
                </label>
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={failWorker}
                    onChange={(event) => {
                      setFailWorker(event.target.checked);
                      if (event.target.checked) setFaultTarget("");
                    }}
                  />
                  模拟 worker-1 丢失
                </label>
              </div>
              <p className="muted">fabric 故障与 worker 丢失分开验证。勾选 worker 丢失后，本次只运行进程内重分配演示。</p>
              <button className="danger-action" onClick={launchScenario} disabled={busy}>
                运行故障场景
              </button>
              <div className="fault-result">
                <div>
                  <span>受影响作业</span>
                  <strong>{fault?.affected_job_ids.join(", ") || "无"}</strong>
                </div>
                <div>
                  <span>不可达节点</span>
                  <strong>{fault?.unavailable_node_ids.join(", ") || "无"}</strong>
                </div>
                <div>
                  <span>worker 重分配</span>
                  <strong>
                    {distributed
                      ? `${Object.keys(distributed.reassigned).length} 个 / ${distributed.recovery_ms.toFixed(1)} ms`
                      : "当前场景未使用"}
                  </strong>
                </div>
              </div>
              {fault?.migrations.length ? (
                <div className="migration-list">
                  {fault.migrations.map((migration) => (
                    <div key={migration.job_id}>
                      <strong>{migration.job_id}</strong>
                      <span>
                        {migration.from_node_ids.join(",")} → {migration.to_node_ids.join(",")}
                      </span>
                      <em>{migration.moved_gpu_count} GPU moved</em>
                    </div>
                  ))}
                </div>
              ) : null}
            </section>
          </div>

          <section className="panel metrics-panel">
            <div className="panel-heading">
              <div>
                <span className="section-index">04</span>
                <h2>链路负载与流量</h2>
              </div>
              <span className={evaluation?.unroutable_flow_count ? "badge danger" : "badge good"}>
                {evaluation?.unroutable_flow_count ?? 0} unreachable
              </span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>链路</th>
                    <th>请求</th>
                    <th>容量</th>
                    <th>利用率</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {[...(evaluation?.links ?? [])]
                    .sort((a, b) => b.utilization - a.utilization)
                    .slice(0, 12)
                    .map((link) => (
                      <tr key={link.link_id}>
                        <td className="mono">{link.link_id}</td>
                        <td>{link.requested_gbps.toFixed(1)} Gbps</td>
                        <td>{link.capacity_gbps.toFixed(0)} Gbps</td>
                        <td>
                          <div className="util-track">
                            <i style={{width: `${Math.min(link.utilization, 1) * 100}%`}} />
                          </div>
                          {formatPercent(link.utilization)}
                        </td>
                        <td>
                          <span className={link.oversubscribed ? "badge danger" : "badge"}>
                            {link.oversubscribed ? "超载" : "正常"}
                          </span>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
            <div className="flow-list">
              {evaluation?.flows.map((flow) => (
                <div key={`${flow.job_id}-${flow.source_node_id}-${flow.target_node_id}`}>
                  <strong>{flow.job_id}</strong>
                  <span>
                    {flow.source_node_id} → {flow.target_node_id}
                  </span>
                  <em>{formatGbps(flow.per_unit_gbps)} / unit</em>
                </div>
              ))}
            </div>
          </section>

          <section className="panel assistant-panel">
            <div className="panel-heading">
              <div>
                <h2>运行解释 Assistant</h2>
              </div>
              <span className="badge">规则型，只读</span>
            </div>
            <p className="assistant-intro">根据本次运行结果解释瓶颈和故障影响，未接入大语言模型，不能修改拓扑或执行操作。</p>
            <label htmlFor="assistant-question">关于当前运行的问题</label>
            <div className="assistant-input">
              <input
                id="assistant-question"
                type="text"
                value={question}
                maxLength={1000}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {if (event.key === "Enter" && !busy) void explainRun();}}
              />
              <button className="secondary" onClick={explainRun} disabled={busy || !run || !question.trim()}>
                解释当前运行
              </button>
            </div>
            {assistant && (
              <div className="assistant-result" aria-live="polite">
                <p>{assistant.answer}</p>
                <details>
                  <summary>结果依据</summary>
                  <dl>
                    {assistant.evidence.map((item, index) => (
                      <div key={`${item.field}-${index}`}>
                        <dt className="mono">{item.field}</dt>
                        <dd>{typeof item.value === "string" ? item.value : JSON.stringify(item.value)}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
                <ul className="assistant-limitations">
                  {assistant.limitations.map((item, index) => <li key={index}>{item}</li>)}
                </ul>
              </div>
            )}
          </section>

          <section className="panel revision-panel">
            <div className="panel-heading">
              <div>
                <span className="section-index">05</span>
                <h2>修订版生命周期</h2>
              </div>
              <span className="mono small">{revision?.id}</span>
            </div>
            <div className="revision-flow">
              <div className={revision?.status === "draft" ? "active" : "done"}>草稿</div>
              <span />
              <div className={revision?.status === "validated" ? "active" : revision?.status === "committed" ? "done" : ""}>
                校验
              </div>
              <span />
              <div className={revision?.status === "committed" ? "active" : ""}>提交</div>
            </div>
            <div className="revision-actions">
              <button className="secondary" onClick={createAndValidateRevision} disabled={busy}>
                优先级 +10 并创建修订版
              </button>
              <button className="primary" onClick={commitDraft} disabled={busy || !draftRevision}>
                提交新修订版
              </button>
              <button
                className="primary"
                onClick={() => currentRevision && perform(async () => {
                  const committed = await api.commit(currentRevision.id);
                  setCurrentRevision(committed);
                  setMessage("当前修订版已提交。");
                })}
                disabled={busy || currentRevision?.status === "committed"}
              >
                提交当前版本
              </button>
            </div>
            {diff && (
              <div className="diff-box">
                <strong>版本差异</strong>
                <span>
                  +{diff.added_count} / -{diff.removed_count} / ~{diff.changed_count}
                </span>
                {diff.entries.map((entry) => (
                  <code key={entry.path}>{entry.path}</code>
                ))}
              </div>
            )}
          </section>

          {comparison.length === 2 && (
            <section className="panel comparison-panel">
              <div className="panel-heading">
                <div>
                  <span className="section-index">06</span>
                  <h2>策略对比</h2>
                </div>
              </div>
              <div className="comparison-grid">
                {comparison.map((item) => {
                  if (item.status === "failed" || !item.result) {
                    return (
                      <article key={item.id}>
                        <span>{item.strategy}</span>
                        <strong>运行失败</strong>
                        <p role="alert">{item.error || "服务端未返回评估结果。"}</p>
                      </article>
                    );
                  }
                  const avgAlignment =
                    item.result.placement.assignments.reduce(
                      (sum, assignment) => sum + (assignment.quality?.rail_alignment ?? 0),
                      0
                    ) / Math.max(item.result.placement.assignments.length, 1);
                  return (
                    <article key={item.id}>
                      <span>{item.strategy}</span>
                      <strong>{formatPercent(avgAlignment)}</strong>
                      <p>rail 对齐</p>
                      <small>
                        {item.result.placement.assigned_gpus}/{item.result.placement.total_gpus} GPU
                      </small>
                    </article>
                  );
                })}
              </div>
            </section>
          )}
        </main>
      )}

      <footer>
        <span>FabricSandbox · local sandbox</span>
        <span>拓扑与带宽为模型化结果，不连接真实集群</span>
      </footer>
    </div>
  );
}
