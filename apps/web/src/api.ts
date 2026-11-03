export type Project = {
  id: string;
  name: string;
  current_revision_id: string | null;
};

export type Gpu = {
  id: string;
  slot: number;
  rail_id: number;
};

export type Node = {
  id: string;
  rack_id: string;
  leaf_id: string;
  gpus: Gpu[];
  power_budget_watts?: number;
};

export type Topology = {
  id: string;
  spines: {id: string}[];
  racks: {id: string}[];
  leaves: {id: string; rack_id: string}[];
  nodes: Node[];
  links: {
    id: string;
    kind: "spine_leaf" | "leaf_node";
    endpoint_a: string;
    endpoint_b: string;
    capacity_gbps: number;
  }[];
};

export type Job = {
  id: string;
  requested_gpus: number;
  communication_mode: "ring_all_reduce" | "all_to_all";
  priority: number;
};

export type RevisionContent = {
  schema_version: number;
  revision_id: string;
  topology: Topology;
  jobs: Job[];
};

export type Revision = {
  id: string;
  project_id: string;
  parent_revision_id: string | null;
  status: "draft" | "validated" | "validation_failed" | "committed";
  content_hash: string;
  content: RevisionContent;
};

export type BootstrapResponse = {
  project: Project;
  revision: Revision;
  validation: {valid: boolean; findings: unknown[]};
};

export type Run = {
  id: string;
  revision_id: string;
  strategy: "rail_aware" | "first_fit";
  status: "queued" | "planning" | "evaluating" | "completed" | "degraded" | "failed";
  created_at: string;
  completed_at: string | null;
  error: string | null;
  result: {
    revision_id: string;
    revision_content_hash: string;
    placement: {
      strategy: string;
      topology_id: string;
      job_order: string[];
      total_gpus: number;
      assigned_gpus: number;
      utilization: number;
      complete: boolean;
      assignments: {
        job_id: string;
        node_ids: string[];
        gpus: {node_id: string; gpu_id: string; slot: number; rail_id: number}[];
        quality?: {
          rail_alignment: number;
          score: number;
          node_count: number;
          leaf_count: number;
          fragmentation_penalty: number;
        };
      }[];
      unplaced_job_ids: string[];
      findings: {code: string; message: string; job_id?: string}[];
    };
    evaluation: {
      topology_id: string;
      placement_strategy: string;
      flow_unit_gbps: number;
      unroutable_flow_count: number;
      oversubscribed_link_ids: string[];
      balanced_leaf_bisection_proxy_gbps: number;
      flows: {
        job_id: string;
        source_node_id: string;
        target_node_id: string;
        traffic_units: number;
        path_link_ids: string[];
        allocated_gbps: number;
        per_unit_gbps: number;
        unroutable: boolean;
      }[];
      links: {
        link_id: string;
        capacity_gbps: number;
        traffic_units: number;
        requested_gbps: number;
        allocated_gbps: number;
        utilization: number;
        oversubscribed: boolean;
      }[];
      jobs: {
        job_id: string;
        flow_count: number;
        traffic_units: number;
        throughput_proxy_gbps: number | null;
        bottleneck_link_ids: string[];
      }[];
      leaf_oversubscription: {
        leaf_id: string;
        downlink_capacity_gbps: number;
        uplink_capacity_gbps: number;
        ratio: number | null;
      }[];
    };
    distributed_evaluation?: {
      assignments: Record<string, string[]>;
      failed_workers: string[];
      reassigned: Record<string, string>;
      recovery_ms: number;
      events: {type: string; worker_id?: string; job_ids?: string[]}[];
    };
    fault_replan?: {
      faults: {type: string; target_id: string}[];
      removed_link_ids: string[];
      removed_node_ids: string[];
      disconnected_node_ids: string[];
      unavailable_node_ids: string[];
      affected_job_ids: string[];
      unaffected_job_ids: string[];
      migrations: {
        job_id: string;
        from_node_ids: string[];
        to_node_ids: string[];
        moved_gpu_count: number;
      }[];
      complete: boolean;
    };
  } | null;
};

export type Diff = {
  left_revision_id: string;
  right_revision_id: string;
  added_count: number;
  removed_count: number;
  changed_count: number;
  entries: {
    path: string;
    change: "added" | "removed" | "changed";
    before: unknown;
    after: unknown;
  }[];
};

export type AssistantExplanation = {
  mode: "rules_based";
  read_only: true;
  answer: string;
  evidence: {field: string; value: unknown}[];
  limitations: string[];
};

// Deliberately kept in memory: credentials never enter URLs or browser storage.
let accessToken = "";

export function setAccessToken(value: string) {
  accessToken = value.trim();
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(path, {
    ...init,
    headers
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({detail: response.statusText}));
    throw new Error(body.detail ?? `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  explain: (runId: string, question: string) =>
    request<AssistantExplanation>("/api/assistant/explain", {
      method: "POST",
      body: JSON.stringify({run_id: runId, question})
    }),
  bootstrap: () => request<BootstrapResponse>("/api/demo/bootstrap", {method: "POST"}),
  validate: (revisionId: string) =>
    request<{revision: Revision; validation: {valid: boolean}}>(
      `/api/revisions/${revisionId}/validate`,
      {method: "POST"}
    ),
  commit: (revisionId: string) =>
    request<Revision>(`/api/revisions/${revisionId}/commit`, {method: "POST"}),
  createRevision: (projectId: string, content: RevisionContent) =>
    request<Revision>(`/api/projects/${projectId}/revisions`, {
      method: "POST",
      body: JSON.stringify({content})
    }),
  compare: (left: string, right: string) =>
    request<Diff>(`/api/revisions/${left}/diff/${right}`),
  run: (payload: {
    revision_id: string;
    strategy: "rail_aware" | "first_fit";
    flow_unit_gbps: number;
    faults?: {type: string; target_id: string}[];
    failed_workers?: string[];
  }) =>
    request<Run>("/api/runs", {
      method: "POST",
      body: JSON.stringify(payload)
    })
};
