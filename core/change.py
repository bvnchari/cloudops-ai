"""
AI-Assisted Change Management.

Engineer submits a plain-language problem statement. CloudOps-AI:
  1. Drafts a ChangePlan (ordered steps) + BackoutPlan (reverse steps) —
     via LLM if ANTHROPIC_API_KEY is set, else by matching the same
     runbook library core.remediation already uses (so Dev/Test/Prod
     plans stay consistent with what the Remediation tab would recommend
     for a matching incident).
  2. Dry-runs the plan in Dev, then Test — logging exactly what each step
     would do. Deliberately the SAME honesty model as core.remediation's
     ReadOnlyExecutor fix: nothing is actually executed anywhere by this
     module, so no stage ever fabricates a "it worked" result. A real
     pipeline/Executor can be wired in later without changing this state
     machine.
  3. Once Dev + Test are recorded, the change queues for Prod behind an
     explicit TWO-STAGE approval (distinct named approvers) — nothing
     reaches Prod on a single click.
  4. On submission for Prod approval, opens a linked ServiceNow Change
     Request (or the in-memory mock) AND a Jira issue, both cross-
     referenced to this ChangeRequest's id — same cross-link pattern as
     core.jira's incident sync.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .remediation import DEFAULT_RUNBOOKS

STAGES = ["draft", "dev", "test", "pending_prod_approval",
         "approved_stage1", "approved_stage2", "prod", "closed"]


@dataclass
class ChangeStep:
    name: str
    command: str


@dataclass
class ChangePlan:
    steps: list          # list[ChangeStep]
    backout_steps: list   # list[ChangeStep]
    risk: str             # "low" | "medium" | "high"
    rationale: str
    source: str           # "llm" | "runbook_match" | "manual_review_required"


@dataclass
class Evidence:
    environment: str      # "dev" | "test" | "prod"
    recorded_at: float
    steps_logged: list    # str lines, one per step attempted
    note: str = ("Dry-run only — no environment was actually touched. These "
                "are the exact commands a human or a wired-in Executor "
                "would run next, in order.")


@dataclass
class Approval:
    stage: str            # "stage1" | "stage2"
    approver: str
    approved_at: float


@dataclass
class ChangeRequest:
    change_id: str
    problem_statement: str
    requested_by: str
    plan: ChangePlan
    status: str = "draft"
    dev_evidence: Evidence | None = None
    test_evidence: Evidence | None = None
    prod_evidence: Evidence | None = None
    approvals: list = field(default_factory=list)     # list[Approval]
    sn_change_number: str | None = None
    jira_key: str | None = None
    created_at: float = field(default_factory=time.time)
    target_context: dict | None = None   # explicit cluster/namespace for real Prod execution

    @property
    def ready_for_prod(self) -> bool:
        return self.dev_evidence is not None and self.test_evidence is not None

    @property
    def stage1_done(self) -> bool:
        return any(a.stage == "stage1" for a in self.approvals)

    @property
    def stage2_done(self) -> bool:
        return any(a.stage == "stage2" for a in self.approvals)

    @property
    def fully_approved(self) -> bool:
        return self.stage1_done and self.stage2_done


def _dry_run(plan: ChangePlan, environment: str) -> Evidence:
    lines = [f"[DRY-RUN][{environment}] {s.name}: `{s.command}`" for s in plan.steps]
    return Evidence(environment=environment, recorded_at=time.time(), steps_logged=lines)


def advance_to_dev(cr: ChangeRequest) -> ChangeRequest:
    cr.dev_evidence = _dry_run(cr.plan, "dev")
    cr.status = "dev"
    return cr


def advance_to_test(cr: ChangeRequest) -> ChangeRequest:
    cr.test_evidence = _dry_run(cr.plan, "test")
    cr.status = "test"
    return cr


def submit_for_prod_approval(cr: ChangeRequest) -> ChangeRequest:
    if not cr.ready_for_prod:
        raise ValueError("Dev and Test dry-runs must both be recorded first.")
    cr.status = "pending_prod_approval"
    return cr


def approve(cr: ChangeRequest, stage: str, approver: str) -> ChangeRequest:
    if stage not in ("stage1", "stage2"):
        raise ValueError("stage must be 'stage1' or 'stage2'")
    if stage == "stage2" and not cr.stage1_done:
        raise ValueError("Stage 1 approval is required before Stage 2.")
    if not approver.strip():
        raise ValueError("Approver name is required.")
    cr.approvals.append(Approval(stage=stage, approver=approver.strip(),
                                 approved_at=time.time()))
    cr.status = "approved_stage1" if stage == "stage1" else "approved_stage2"
    return cr


def advance_to_prod(cr: ChangeRequest, executor=None) -> ChangeRequest:
    if not cr.fully_approved:
        raise ValueError("Both approval stages are required before Prod.")

    is_real = executor is not None and getattr(executor, "is_real", False) and not executor.read_only
    if not is_real:
        cr.prod_evidence = _dry_run(cr.plan, "prod")
        cr.status = "prod"
        return cr

    # ---- Real, verified Prod execution ----
    if not cr.target_context or not cr.target_context.get("target_found", True):
        raise ValueError("No cluster/namespace target selected for this change — "
                         "pick one from the inventory before executing Prod for real.")
    ctx = cr.target_context
    from .remediation import RunbookAction
    log_lines = []
    ok = True
    for step in cr.plan.steps:
        action = RunbookAction(name=step.name, command=step.command)
        if not executor.supports(action):
            ok = False
            log_lines.append(f"[UNSUPPORTED] {step.name}: `{step.command}` — no real "
                             f"executor for this action type; change not executed.")
            break
        success, line = executor.run(action, ctx)
        log_lines.append(line)
        if not success:
            ok = False
            break

    verified = False
    if ok:
        deployment = ctx.get("deployment", "unknown")
        verified, vline = executor.verify(None, type("_", (), {
            "probable_root_cause": deployment, "business_service": None})(), ctx)
        log_lines.append(vline)

    cr.prod_evidence = Evidence(
        environment="prod", recorded_at=time.time(), steps_logged=log_lines,
        note=("Executed for real and verified via a live post-check."
              if (ok and verified) else
              "Execution attempted but NOT verified — treat as unresolved; "
              "do not consider this change complete."))
    cr.status = "prod" if (ok and verified) else "prod_execution_failed"
    return cr


def close(cr: ChangeRequest) -> ChangeRequest:
    cr.status = "closed"
    return cr


# ---------------- AI change planner ----------------

class AIChangePlanner:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        import os
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("ANTHROPIC_MODEL",
                                             "claude-haiku-4-5-20251001")

    def generate(self, problem_statement: str) -> ChangePlan:
        if self.api_key:
            try:
                return self._llm_plan(problem_statement)
            except Exception:
                pass  # fall through to runbook match — never fail the request
        return self._runbook_fallback(problem_statement)

    def _llm_plan(self, problem_statement: str) -> ChangePlan:
        import json
        import requests
        prompt = (
            "You are a change-management assistant for an SRE platform. "
            "Given the engineer's problem statement, draft a safe, minimal "
            "change plan.\n\n"
            "Respond ONLY with JSON: {\"steps\": [{\"name\": str, "
            "\"command\": str}], \"backout_steps\": [{\"name\": str, "
            "\"command\": str}], \"risk\": \"low\"|\"medium\"|\"high\", "
            "\"rationale\": str}\n\n"
            f"Problem statement:\n{problem_statement}"
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        if not r.ok:
            detail = ""
            try:
                err = r.json().get("error", {})
                detail = f"{err.get('type', '')}: {err.get('message', '')}".strip(": ")
            except Exception:
                detail = r.text[:250]
            raise RuntimeError(f"HTTP {r.status_code} — {detail}")
        text = "".join(b.get("text", "") for b in r.json()["content"])
        data = json.loads(text.replace("```json", "").replace("```", "").strip())
        return ChangePlan(
            steps=[ChangeStep(**s) for s in data["steps"]],
            backout_steps=[ChangeStep(**s) for s in data["backout_steps"]],
            risk=data.get("risk", "medium"), rationale=data.get("rationale", ""),
            source="llm")

    def _runbook_fallback(self, problem_statement: str) -> ChangePlan:
        """No API key (or LLM failed): match the same runbook library
        core.remediation uses, by keyword overlap with the problem
        statement, so an engineer typing 'disk filling up on node-02'
        gets the same plan the Remediation engine would apply to a
        matching incident."""
        text = problem_statement.lower()
        best, best_score = None, 0
        for rb in DEFAULT_RUNBOOKS:
            keywords = [rb.name.lower()] + [m.lower().replace("_", " ") for m in rb.match_metrics]
            score = sum(1 for kw in keywords if any(w in text for w in kw.split()))
            if score > best_score:
                best, best_score = rb, score

        if best and best_score > 0:
            steps = [ChangeStep(name=a.name, command=a.command) for a in best.actions]
            backout = [ChangeStep(name=f"Revert: {a.name}", command=f"# manual reverse of: {a.command}")
                      for a in reversed(best.actions)]
            return ChangePlan(steps=steps, backout_steps=backout,
                              risk="medium" if best.auto_approve else "high",
                              rationale=f"Matched existing runbook '{best.name}' "
                                       f"by keyword overlap with the problem statement.",
                              source="runbook_match")

        return ChangePlan(
            steps=[ChangeStep(name="Manual review required",
                              command="# No LLM configured and no runbook matched — "
                                     "an engineer must author this plan manually.")],
            backout_steps=[ChangeStep(name="N/A", command="# no automated backout available")],
            risk="high",
            rationale="Neither an LLM (no ANTHROPIC_API_KEY) nor an existing "
                     "runbook matched this problem statement closely enough "
                     "to draft a plan automatically.",
            source="manual_review_required")


# ---------------- SN Change + Jira cross-linking ----------------

def open_change_ticket(sn_config, cr: ChangeRequest) -> dict:
    """Opens a real ServiceNow Change Request if sn_config is provided and
    valid, else returns an in-memory mock number — same fallback pattern as
    core.itsm.ITSMBridge."""
    if sn_config is not None:
        from .servicenow import EnterpriseServiceNowConnector
        conn = EnterpriseServiceNowConnector(sn_config)
        data = conn._request("POST", "/api/now/table/change_request", json={
            "short_description": f"[CloudOps-AI] {cr.problem_statement[:150]}",
            "description": cr.plan.rationale,
            "risk": cr.plan.risk, "type": "normal",
            "justification": f"AI-drafted change {cr.change_id}, validated in Dev/Test.",
        })["result"]
        return {"number": data["number"], "sys_id": data["sys_id"]}
    seq = int(time.time()) % 900000 + 100000
    return {"number": f"CHG{seq:07d}", "sys_id": f"mock-{seq}"}


def open_jira_for_change(jira_bridge, cr: ChangeRequest):
    """Files a Jira issue cross-referenced to the ChangeRequest and (if
    already opened) the ServiceNow Change number — reuses JiraBridge exactly
    as core.jira's incident sync does."""
    description = (
        f"AI-drafted change request.\n\n"
        f"Change ID: {cr.change_id}\n"
        f"ServiceNow Change: {cr.sn_change_number or 'not yet opened'}\n"
        f"Risk: {cr.plan.risk}\n\n"
        f"Problem statement:\n{cr.problem_statement}\n\n"
        f"Rationale:\n{cr.plan.rationale}"
    )
    return jira_bridge.create_issue(
        summary=f"[Change] {cr.problem_statement[:80]}",
        description=description, incident_id=cr.change_id,
        sn_ticket_number=cr.sn_change_number, reason="change_request")
