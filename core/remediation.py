"""
Phase 4 — Automation & Auto-Remediation (self-healing).

Rule-driven runbook engine, same design philosophy as FinnieAI's health_engine:
all fixes flow through a central registry — no inline patching.

Runbooks are declarative: match conditions -> ordered actions -> verification.
Executors are pluggable (LocalExecutor for demo; swap in AnsibleExecutor /
SSMExecutor / K8sExecutor for real environments — interface is identical).
"""

import time
from dataclasses import dataclass, field

from .correlation import Incident


@dataclass
class RunbookAction:
    name: str
    command: str            # what would run (kubectl / ansible / aws cli / script)
    timeout_s: int = 60


@dataclass
class Runbook:
    runbook_id: str
    name: str
    match_metrics: list          # trigger metrics
    match_severities: list       # e.g. ["critical"]
    actions: list = field(default_factory=list)
    verify: str = ""             # post-check description
    auto_approve: bool = True    # False -> requires human approval (change mgmt)


@dataclass
class RemediationResult:
    incident_id: str
    runbook_id: str
    runbook_name: str
    started_ts: float
    finished_ts: float
    success: bool
    log: list = field(default_factory=list)
    mode: str = "simulated"      # "simulated" | "read_only" | "live"


class Executor:
    """Pluggable execution backend."""
    read_only: bool = False

    def run(self, action: RunbookAction) -> tuple[bool, str]:
        raise NotImplementedError


class LocalExecutor(Executor):
    """DEMO-ONLY executor: simulates execution with realistic logs and always
    reports success. Appropriate ONLY for synthetic/demo incidents where
    nothing real is at stake. Replace with AnsibleExecutor / SSMExecutor /
    K8sExecutor (same interface) to actually execute against real
    infrastructure — that integration isn't included here since it requires
    real credentials and a blast-radius review specific to your environment.
    """
    read_only = False

    def run(self, action: RunbookAction) -> tuple[bool, str]:
        return True, f"[SIMULATED] {action.name}: `{action.command}` completed"


class ReadOnlyExecutor(Executor):
    """Safe default for REAL incidents (e.g. sourced from live ServiceNow):
    logs exactly what a runbook *would* run, executes nothing, and never
    fabricates a resolution. RemediationEngine treats this executor
    specially — matched incidents get a recommendation, not a fake
    "resolved" status, and are never auto-closed in ITSM on the strength
    of a dry-run.
    """
    read_only = True

    def run(self, action: RunbookAction) -> tuple[bool, str]:
        return False, f"[DRY-RUN — not executed] {action.name}: `{action.command}`"


DEFAULT_RUNBOOKS = [
    Runbook(
        runbook_id="RB-001", name="Pod restart / reschedule",
        match_metrics=["pod_restart_count", "node_memory_utilization"],
        match_severities=["critical", "warning"],
        actions=[
            RunbookAction("Cordon node", "kubectl cordon {node}"),
            RunbookAction("Restart workload", "kubectl rollout restart deploy/{deployment}"),
            RunbookAction("Uncordon node", "kubectl uncordon {node}"),
        ],
        verify="Pod Ready=True and restart counter stable for 10m",
    ),
    Runbook(
        runbook_id="RB-002", name="Disk cleanup automation",
        match_metrics=["node_disk_utilization"],
        match_severities=["critical", "warning"],
        actions=[
            RunbookAction("Prune images", "crictl rmi --prune"),
            RunbookAction("Rotate logs", "logrotate -f /etc/logrotate.d/containers"),
            RunbookAction("Clear tmp", "find /tmp -mtime +2 -delete"),
        ],
        verify="disk_utilization < 75%",
    ),
    Runbook(
        runbook_id="RB-003", name="Service restart automation",
        match_metrics=["api_error_rate", "api_latency_p99", "alb_5xx_rate"],
        match_severities=["critical"],
        actions=[
            RunbookAction("Capture diagnostics", "kubectl logs --tail=500 deploy/{deployment} > /diag/pre.log"),
            RunbookAction("Rolling restart", "kubectl rollout restart deploy/{deployment}"),
            RunbookAction("Wait for rollout", "kubectl rollout status deploy/{deployment} --timeout=180s"),
        ],
        verify="error_rate < 1% and p99 < 300ms for 15m",
    ),
    Runbook(
        runbook_id="RB-004", name="Cloud auto-scaling",
        match_metrics=["node_cpu_utilization", "db_connections"],
        match_severities=["critical"],
        actions=[
            RunbookAction("Scale node group", "aws eks update-nodegroup-config --scaling-config desiredSize=+2"),
        ],
        verify="cpu_utilization < 70% cluster-wide",
        auto_approve=False,   # capacity changes go through change management
    ),
    Runbook(
        runbook_id="RB-005", name="DB replication recovery",
        match_metrics=["db_replication_lag"],
        match_severities=["critical", "warning"],
        actions=[
            RunbookAction("Kill long queries", "CALL kill_queries_over(300)"),
            RunbookAction("Restart replication", "CALL mysql.rds_restart_replication()"),
        ],
        verify="replication_lag < 2s",
    ),
]


class RemediationEngine:
    def __init__(self, runbooks: list[Runbook] | None = None,
                 executor: Executor | None = None):
        self.runbooks = runbooks or DEFAULT_RUNBOOKS
        self.executor = executor or LocalExecutor()
        self.results: list[RemediationResult] = []
        self.pending_approval: list[tuple[Incident, Runbook]] = []

    def match(self, incident: Incident) -> Runbook | None:
        inc_metrics = {e.metric for e in incident.events}
        for rb in self.runbooks:
            if inc_metrics & set(rb.match_metrics) and incident.severity in rb.match_severities:
                return rb
        return None

    def remediate(self, incident: Incident) -> RemediationResult | None:
        rb = self.match(incident)
        if not rb:
            return None
        if not rb.auto_approve:
            self.pending_approval.append((incident, rb))
            incident.status = "pending_approval"
            incident.remediation = f"{rb.name} (awaiting change approval)"
            return None

        read_only = self.executor.read_only
        start = time.time()
        incident.status = "remediating" if not read_only else "remediation_recommended"
        log, ok = [], True
        for action in rb.actions:
            success, line = self.executor.run(action)
            log.append(line)
            if read_only:
                continue          # dry-run: log every step, never execute/abort
            if not success:
                ok = False
                break
        finish = time.time()

        if read_only:
            # Never fabricate a resolution for a real incident. A human (or a
            # real Executor plugged into `self.executor`) still has to act.
            ok = False
            incident.status = "remediation_recommended"
            incident.remediation = f"{rb.name} (recommended — dry-run only, not executed)"
        elif ok:
            incident.status = "resolved"
            incident.resolved_ts = incident.created_ts + 300 + 120 * len(rb.actions)  # simulated MTTR
            incident.remediation = rb.name

        result = RemediationResult(
            incident_id=incident.incident_id, runbook_id=rb.runbook_id,
            runbook_name=rb.name, started_ts=start, finished_ts=finish,
            success=ok, log=log,
            mode="read_only" if read_only else "simulated",
        )
        self.results.append(result)
        return result
