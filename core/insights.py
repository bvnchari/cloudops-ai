"""
Operational Insights — the day-to-day engineer view.

Three analyses that turn pipeline output into work you can act on:
  1. Triage queue      — what to look at first, and why (explainable scoring)
  2. Noise hotspots    — which CI/metric pairs generate the most alert volume,
                         i.e. the highest-value threshold-tuning targets
  3. Automation gaps   — incidents with no matching runbook: the automation
                         backlog, ranked by recurring toil cost
"""

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .correlation import Incident
from .remediation import RemediationEngine
from .telemetry import RawAlert
from .topology import TopologyMap

SEV_WEIGHT = {"critical": 10.0, "warning": 4.0, "info": 1.0}


@dataclass
class TriageItem:
    incident: Incident
    score: float
    rank: int
    age_minutes: float
    blast_radius: int
    reasons: list = field(default_factory=list)   # explainable scoring

    @property
    def needs_human(self) -> bool:
        return self.incident.status not in ("resolved",)


class TriageQueue:
    """
    Priority = severity x blast radius x business impact x age, with
    auto-resolved incidents demoted. Every score carries its reasoning so an
    on-call engineer can see *why* something is at the top, not just that it is.
    """

    def __init__(self, topology: TopologyMap, now: float | None = None):
        self.topology = topology
        self.now = now or time.time()

    def build(self, incidents: list[Incident]) -> list[TriageItem]:
        items = []
        for inc in incidents:
            reasons = []
            score = SEV_WEIGHT.get(inc.severity, 1.0)
            reasons.append(f"severity {inc.severity} (x{score:g})")

            blast = len(self.topology.downstream_of(inc.probable_root_cause)) \
                if inc.probable_root_cause else 0
            if blast:
                factor = 1 + blast * 0.25
                score *= factor
                reasons.append(f"{blast} downstream CI(s) at risk (x{factor:.2f})")

            if inc.business_service:
                score *= 1.5
                reasons.append("mapped to a business service (x1.5)")

            age_min = max((self.now - inc.created_ts) / 60, 0.0)
            if age_min > 30 and inc.status != "resolved":
                factor = 1 + min(age_min / 120, 2.0)
                score *= factor
                reasons.append(f"unresolved for {age_min:.0f}m (x{factor:.2f})")

            if inc.status == "resolved":
                score *= 0.2
                reasons.append("auto-remediated (x0.2 — informational)")
            elif inc.status == "pending_approval":
                score *= 1.4
                reasons.append("blocked on change approval (x1.4)")

            items.append(TriageItem(incident=inc, score=round(score, 1), rank=0,
                                    age_minutes=round(age_min, 1),
                                    blast_radius=blast, reasons=reasons))

        items.sort(key=lambda i: i.score, reverse=True)
        for idx, item in enumerate(items, 1):
            item.rank = idx
        return items


@dataclass
class NoiseHotspot:
    ci_id: str
    metric: str
    alert_count: int
    pct_of_total: float
    severity_mix: dict
    recommendation: str


def noise_hotspots(alerts: list[RawAlert], top_n: int = 10) -> list[NoiseHotspot]:
    """Rank the CI/metric pairs producing the most alert volume."""
    total = len(alerts) or 1
    counts = Counter((a.ci_id, a.metric) for a in alerts)
    sev = defaultdict(Counter)
    for a in alerts:
        sev[(a.ci_id, a.metric)][a.severity] += 1

    out = []
    for (ci, metric), n in counts.most_common(top_n):
        pct = round(100 * n / total, 1)
        mix = dict(sev[(ci, metric)])
        if mix.get("warning", 0) > 3 * mix.get("critical", 1):
            rec = "Mostly warnings — raise the warning threshold or add hysteresis."
        elif pct > 25:
            rec = "Dominates alert volume — add rate limiting or a longer 'for' duration."
        elif n > 20:
            rec = "High repeat volume — candidate for deduplication window tuning."
        else:
            rec = "Within normal range."
        out.append(NoiseHotspot(ci_id=ci, metric=metric, alert_count=n,
                                pct_of_total=pct, severity_mix=mix,
                                recommendation=rec))
    return out


@dataclass
class AutomationGap:
    incident_id: str
    metrics: list
    severity: str
    root_cause: str | None
    suggested_runbook: str
    est_annual_toil_hours: float


def automation_gaps(incidents: list[Incident],
                    engine: RemediationEngine | None = None,
                    manual_mttr_min: float = 45.0,
                    assumed_annual_recurrence: int = 24) -> list[AutomationGap]:
    """
    Incidents with no matching runbook — the automation backlog. Each is costed
    in annual toil hours so the backlog can be prioritized against other work.
    """
    engine = engine or RemediationEngine()
    gaps = []
    for inc in incidents:
        if engine.match(inc):
            continue
        metrics = sorted({e.metric for e in inc.events})
        toil = assumed_annual_recurrence * manual_mttr_min / 60
        gaps.append(AutomationGap(
            incident_id=inc.incident_id, metrics=metrics, severity=inc.severity,
            root_cause=inc.probable_root_cause,
            suggested_runbook=f"Auto-remediation for {', '.join(metrics[:2])} "
                              f"on {inc.probable_root_cause or 'affected CI'}",
            est_annual_toil_hours=round(toil, 1),
        ))
    gaps.sort(key=lambda g: g.est_annual_toil_hours, reverse=True)
    return gaps


@dataclass
class ServiceHealth:
    business_service: str
    incidents: int
    critical: int
    auto_resolved: int
    open_items: int
    health_score: float          # 0-100
    grade: str


def service_scorecard(incidents: list[Incident]) -> list[ServiceHealth]:
    """Per-business-service health rollup for management reporting."""
    by_service = defaultdict(list)
    for inc in incidents:
        by_service[inc.business_service or "Unmapped"].append(inc)

    out = []
    for svc, incs in by_service.items():
        crit = sum(1 for i in incs if i.severity == "critical")
        auto = sum(1 for i in incs if i.status == "resolved")
        open_items = len(incs) - auto
        # start at 100, penalize criticals and unresolved work, credit automation
        score = 100.0 - (crit * 12) - (open_items * 8) + (auto * 3)
        score = max(0.0, min(100.0, score))
        grade = ("A" if score >= 90 else "B" if score >= 75 else
                 "C" if score >= 60 else "D" if score >= 40 else "F")
        out.append(ServiceHealth(business_service=svc, incidents=len(incs),
                                 critical=crit, auto_resolved=auto,
                                 open_items=open_items,
                                 health_score=round(score, 1), grade=grade))
    out.sort(key=lambda s: s.health_score)
    return out
