"""Deterministic read-only explanation of stored run results; no LLM or tools."""
from __future__ import annotations

from typing import Any


MODEL_LIMITATIONS = [
    "这是静态、无向、等权流量的带宽代理模型，不包含报文队列、TCP/RDMA或NCCL执行时序。",
    "rail对齐率描述放置结构，当前网络模型未显式建模NIC/rail，不能据此推断真实训练吞吐提升。",
    "多worker目前在单进程中顺序执行，worker故障由输入模拟，不代表真实网络故障检测。",
    "本Assistant使用确定性规则解释已有结果，未接入LLM，也不执行修改、重规划或提交操作。",
]


def explain_run(run: dict[str, Any], question: str) -> dict[str, Any]:
    """Question selects a view, and is never executed or treated as an instruction."""
    evidence: list[dict[str, Any]] = [{"field": "status", "value": run["status"]}]
    lines = [f"运行 {run['id']} 的状态为 {run['status']}。"]
    result = run.get("result") or {}
    if run.get("error"):
        evidence.append({"field": "error", "value": run["error"]})
        lines.append(f"运行失败原因：{run['error']}。")
    if result:
        placement = result.get("placement") or {}
        evaluation = result.get("evaluation") or {}
        unplaced = placement.get("unplaced_job_ids", [])
        unroutable = evaluation.get("unroutable_flow_count", 0)
        evidence.extend([
            {"field": "result.placement.unplaced_job_ids", "value": unplaced},
            {"field": "result.evaluation.unroutable_flow_count", "value": unroutable},
        ])
        lines.append(f"未放置作业 {len(unplaced)} 个，无法路由的流 {unroutable} 条。")
        if unplaced:
            lines.append("未放置作业：" + "、".join(str(job) for job in unplaced) + "。请核对GPU容量和放置约束。")
        links = evaluation.get("links", [])
        active = [item for item in links if item.get("traffic_units", 0) > 0]
        if active:
            top = max(active, key=lambda item: (item.get("utilization", 0), item.get("link_id", "")))
            index = links.index(top)
            evidence.append({"field": f"result.evaluation.links[{index}]", "value": top})
            lines.append(
                f"需求/容量比最高的链路是 {top['link_id']}：需求 {top['requested_gbps']:g} Gbps，"
                f"容量 {top['capacity_gbps']:g} Gbps，比值 {top['utilization']:.2f}。"
                "此比值表示请求负载，不是实测链路利用率。"
            )
            if top.get("oversubscribed"):
                lines.append("该链路存在超订，可比较更分散的放置或更高链路容量，改动后需要重新评估。")
        else:
            lines.append("当前没有跨节点活动流，链路带宽结果不能反映节点内GPU通信性能。")
        if "fault_replan" in result:
            replan = result["fault_replan"]
            migrations = replan.get("migrations", [])
            evidence.append({"field": "result.fault_replan.migrations", "value": migrations})
            lines.append(f"故障重规划给出 {len(migrations)} 条迁移记录。迁移是规划结果，未在真实GPU上执行。")
        if "distributed_evaluation" in result:
            distributed = result["distributed_evaluation"]
            evidence.append({"field": "result.distributed_evaluation.reassigned", "value": distributed.get("reassigned", {})})
            if distributed.get("reassigned"):
                lines.append("模拟worker故障后的分片重分配记录已附在证据中。")
    else:
        lines.append("此运行没有可用评估结果，不推测带宽或故障原因。")
    focus = "model_limits" if any(word in question.lower() for word in ("模型", "真实", "局限", "rail", "llm", "assistant")) else "run_diagnostics"
    if focus == "model_limits":
        lines.extend(MODEL_LIMITATIONS)
    return {
        "mode": "rules_based",
        "read_only": True,
        "run_id": run["id"],
        "focus": focus,
        "answer": "\n".join(lines),
        "evidence": evidence,
        "limitations": MODEL_LIMITATIONS,
    }
