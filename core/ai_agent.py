"""
Phase 7 — AI Incident Analyst (agentic summarization).

Staged agent workflow (LangGraph-style nodes, dependency-free):
    gather_context -> analyze_rca -> draft_narrative -> recommend_actions

Two backends behind one interface:
  * LLMBackend      — calls the Anthropic API when ANTHROPIC_API_KEY is set;
                      produces a natural-language RCA narrative + exec summary.
  * TemplateBackend — deterministic, offline narrative built from incident
                      structure. Default on HF Spaces / demos — no key needed.

The agent consumes the SAME incident objects the pipeline produces, so the
narrative is grounded in real correlation output (RCA, blast radius, folded
alert counts, remediation), not free-form hallucination.
"""

import json
import os
from dataclasses import dataclass

from .correlation import Incident
from .topology import TopologyMap


@dataclass
class IncidentBrief:
    incident_id: str
    exec_summary: str        # 2-3 sentences for leadership
    rca_narrative: str       # technical story: what happened, why, evidence
    recommendations: list    # follow-up actions beyond the auto-remediation
    backend: str             # "llm" | "template"


class AIIncidentAnalyst:
    def __init__(self, topology: TopologyMap, use_llm: bool | None = None,
                 api_key: str | None = None, model: str | None = None):
        self.topology = topology
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("ANTHROPIC_MODEL",
                                             "claude-haiku-4-5-20251001")
        self.use_llm = use_llm if use_llm is not None else bool(self.api_key)
        self.last_error: str | None = None

    # ---------------- node 1: gather ----------------
    def _gather_context(self, inc: Incident) -> dict:
        root_ci = self.topology.get(inc.probable_root_cause) if inc.probable_root_cause else None
        blast = (self.topology.downstream_of(inc.probable_root_cause)
                 if inc.probable_root_cause else [])
        return {
            "incident_id": inc.incident_id,
            "severity": inc.severity,
            "title": inc.title,
            "business_service": inc.business_service,
            "root_cause_ci": inc.probable_root_cause,
            "root_cause_layer": root_ci.layer if root_ci else None,
            "blast_radius": blast,
            "impacted_cis": inc.impacted_cis,
            "raw_alerts_folded": inc.raw_alert_count,
            "events": [{"metric": e.metric, "ci": e.ci_id,
                        "severity": e.severity, "count": e.count}
                       for e in inc.events],
            "status": inc.status,
            "remediation": inc.remediation,
            "anomaly_driven": any("anomaly" in e.message.lower() or
                                  e.message.startswith("[")
                                  for e in inc.events),
        }

    # ---------------- node 2: analyze ----------------
    def _analyze_rca(self, ctx: dict) -> dict:
        metrics = sorted({e["metric"] for e in ctx["events"]})
        layer = ctx["root_cause_layer"] or "unknown"
        cascade = (f"originating at the {layer} layer and cascading to "
                   f"{len(ctx['impacted_cis'])} configuration items"
                   if len(ctx["impacted_cis"]) > 1
                   else "contained to a single configuration item")
        ctx["analysis"] = {
            "symptom_metrics": metrics,
            "cascade_description": cascade,
            "detection_path": ("ML anomaly detection + threshold breach"
                               if ctx["anomaly_driven"] else "threshold breach"),
        }
        return ctx

    # ---------------- node 3+4: narrate & recommend ----------------
    def analyze(self, inc: Incident) -> IncidentBrief:
        ctx = self._analyze_rca(self._gather_context(inc))
        if self.use_llm:
            try:
                return self._llm_brief(ctx)
            except Exception as e:        # graceful degradation to template
                self.last_error = str(e)[:200]
        return self._template_brief(ctx)

    def analyze_all(self, incidents: list[Incident]) -> list[IncidentBrief]:
        return [self.analyze(i) for i in incidents]

    # ---------------- LLM backend ----------------
    def _llm_brief(self, ctx: dict) -> IncidentBrief:
        import requests
        prompt = (
            "You are an SRE incident analyst. Using ONLY the structured incident "
            "data below (do not invent metrics or systems), write:\n"
            "1. exec_summary: 2-3 plain-language sentences for leadership.\n"
            "2. rca_narrative: one technical paragraph — what happened, root "
            "cause, blast radius, how it was detected and resolved.\n"
            "3. recommendations: 3 specific follow-up actions.\n"
            "Respond ONLY with JSON: {\"exec_summary\": str, "
            "\"rca_narrative\": str, \"recommendations\": [str, str, str]}\n\n"
            f"Incident data:\n{json.dumps(ctx, indent=2)}"
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model,
                  "max_tokens": 700,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        if not r.ok:
            # surface the API's own explanation — a bare 400 tells us nothing
            detail = ""
            try:
                err = r.json().get("error", {})
                detail = f"{err.get('type', '')}: {err.get('message', '')}".strip(": ")
            except Exception:
                detail = r.text[:250]
            raise RuntimeError(f"HTTP {r.status_code} — {detail}")
        text = "".join(b.get("text", "") for b in r.json()["content"])
        data = json.loads(text.replace("```json", "").replace("```", "").strip())
        return IncidentBrief(incident_id=ctx["incident_id"],
                             exec_summary=data["exec_summary"],
                             rca_narrative=data["rca_narrative"],
                             recommendations=list(data["recommendations"])[:3],
                             backend="llm")

    # ---------------- deterministic backend ----------------
    def _template_brief(self, ctx: dict) -> IncidentBrief:
        a = ctx["analysis"]
        svc = ctx["business_service"] or "the platform"
        resolved = ctx["status"] == "resolved"
        exec_summary = (
            f"{svc} experienced a {ctx['severity']} incident "
            f"({ctx['incident_id']}). Monitoring correlated "
            f"{ctx['raw_alerts_folded']} raw alerts into this single actionable "
            f"incident, {a['cascade_description']}. "
            + (f"It was automatically resolved via '{ctx['remediation']}' with no "
               f"manual intervention."
               if resolved else "Resolution is in progress.")
        )
        rca_narrative = (
            f"Root cause was isolated to {ctx['root_cause_ci']} "
            f"({a.get('detection_path', 'threshold breach')} on "
            f"{', '.join(a['symptom_metrics'])}). The correlation engine grouped "
            f"symptoms across {len(ctx['impacted_cis'])} CIs "
            f"({', '.join(ctx['impacted_cis'])}) using the service-dependency "
            f"topology, suppressing downstream symptom alerts and attributing "
            f"probable cause to the shared upstream dependency. "
            + (f"The '{ctx['remediation']}' runbook executed automatically and "
               f"the incident was verified resolved."
               if resolved else "Remediation is pending.")
        )
        recs = [
            f"Review capacity headroom and alert thresholds for "
            f"{ctx['root_cause_ci']} to catch precursors earlier.",
            f"Add a synthetic check on {svc} covering "
            f"{a['symptom_metrics'][0] if a['symptom_metrics'] else 'the affected path'} "
            f"to reduce time-to-detect.",
            "Run a blameless post-incident review to validate the runbook's "
            "verification criteria and expand auto-remediation coverage.",
        ]
        return IncidentBrief(incident_id=ctx["incident_id"],
                             exec_summary=exec_summary,
                             rca_narrative=rca_narrative,
                             recommendations=recs,
                             backend="template")
