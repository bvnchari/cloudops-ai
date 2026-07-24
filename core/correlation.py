"""
Phase 2 — Event Correlation & Noise Reduction (the AIOps 'brain').

Pipeline: raw alerts -> dedup -> time+topology correlation -> suppression
          -> incidents with probable root cause.

Mimics BigPanda/Moogsoft-style logic:
  * Deduplication: same (ci, metric, severity) within window collapses to one event.
  * Correlation: events close in time whose CIs share an upstream dependency
    merge into a single incident; the shared dependency is flagged as probable RCA.
  * Suppression: downstream symptom events of an already-open incident are suppressed.
"""

import time
from dataclasses import dataclass, field

from .telemetry import RawAlert
from .topology import TopologyMap

SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


@dataclass
class Event:
    """Deduplicated alert."""
    event_id: str
    ci_id: str
    metric: str
    severity: str
    first_ts: float
    last_ts: float
    count: int
    message: str


@dataclass
class Incident:
    incident_id: str
    created_ts: float
    severity: str
    title: str
    events: list = field(default_factory=list)
    impacted_cis: list = field(default_factory=list)
    probable_root_cause: str | None = None
    business_service: str | None = None
    status: str = "open"          # open | remediating | resolved
    resolved_ts: float | None = None
    remediation: str | None = None
    suppressed_alerts: int = 0

    @property
    def raw_alert_count(self) -> int:
        return sum(e.count for e in self.events) + self.suppressed_alerts


class CorrelationEngine:
    def __init__(self, topology: TopologyMap,
                 dedup_window_s: int = 900,
                 correlation_window_s: int = 600):
        self.topology = topology
        self.dedup_window_s = dedup_window_s
        self.correlation_window_s = correlation_window_s
        self._event_seq = 0
        self._incident_seq = 0

    # ---------- Stage 1: deduplication ----------
    def deduplicate(self, alerts: list[RawAlert]) -> list[Event]:
        events: list[Event] = []
        last: dict[tuple, Event] = {}
        for a in sorted(alerts, key=lambda x: x.ts):
            key = (a.ci_id, a.metric, a.severity)
            ev = last.get(key)
            if ev and a.ts - ev.last_ts <= self.dedup_window_s:
                ev.count += 1
                ev.last_ts = a.ts
            else:
                self._event_seq += 1
                ev = Event(
                    event_id=f"EVT-{self._event_seq:05d}",
                    ci_id=a.ci_id, metric=a.metric, severity=a.severity,
                    first_ts=a.ts, last_ts=a.ts, count=1, message=a.message,
                )
                events.append(ev)
                last[key] = ev
        return events

    # ---------- Stage 2: topology + time correlation ----------
    def correlate(self, events: list[Event]) -> list[Incident]:
        incidents: list[Incident] = []
        remaining = sorted(events, key=lambda e: e.first_ts)
        while remaining:
            seed = remaining.pop(0)
            cluster = [seed]
            # gather events within the correlation window that share topology with the cluster
            still = []
            for ev in remaining:
                in_window = abs(ev.first_ts - seed.first_ts) <= self.correlation_window_s
                related = self._topologically_related(seed.ci_id, ev.ci_id)
                if in_window and related:
                    cluster.append(ev)
                else:
                    still.append(ev)
            remaining = still
            incidents.append(self._to_incident(cluster))
        return incidents

    def _topologically_related(self, ci_a: str, ci_b: str) -> bool:
        if ci_a == ci_b:
            return True
        root = self.topology.shared_root([ci_a, ci_b])
        if root:
            return True
        return ci_b in self.topology.downstream_of(ci_a) or \
               ci_a in self.topology.downstream_of(ci_b)

    def _to_incident(self, cluster: list[Event]) -> Incident:
        self._incident_seq += 1
        cis = sorted({e.ci_id for e in cluster})
        severity = max(cluster, key=lambda e: SEVERITY_RANK[e.severity]).severity
        root = self.topology.shared_root(cis) if len(cis) > 1 else cis[0]
        root_ci = self.topology.get(root) if root else None
        biz = root_ci.business_service if root_ci else None
        title_metric = max(cluster, key=lambda e: e.count).metric
        return Incident(
            incident_id=f"INC-{self._incident_seq:05d}",
            created_ts=min(e.first_ts for e in cluster),
            severity=severity,
            title=f"{title_metric} degradation on {root or cis[0]} "
                  f"({len(cis)} CI{'s' if len(cis) > 1 else ''} impacted)",
            events=cluster,
            impacted_cis=cis,
            probable_root_cause=root,
            business_service=biz,
        )

    # ---------- Stage 3: suppression ----------
    def suppress(self, incidents: list[Incident]) -> list[Incident]:
        """
        If incident B's CIs are entirely downstream of incident A's root cause and it
        opened later, B is a symptom — fold it into A and count its alerts as suppressed.
        """
        incidents = sorted(incidents, key=lambda i: i.created_ts)
        kept: list[Incident] = []
        for inc in incidents:
            parent = None
            for k in kept:
                if not k.probable_root_cause:
                    continue
                blast = set(self.topology.downstream_of(k.probable_root_cause)) | {k.probable_root_cause}
                if set(inc.impacted_cis) <= blast and inc.created_ts >= k.created_ts:
                    parent = k
                    break
            if parent:
                parent.suppressed_alerts += sum(e.count for e in inc.events)
                parent.impacted_cis = sorted(set(parent.impacted_cis) | set(inc.impacted_cis))
            else:
                kept.append(inc)
        return kept

    # ---------- Full pipeline ----------
    def process(self, alerts: list[RawAlert]) -> tuple[list[Incident], dict]:
        events = self.deduplicate(alerts)
        incidents = self.suppress(self.correlate(events))
        raw = len(alerts)
        final = len(incidents)
        stats = {
            "raw_alerts": raw,
            "deduped_events": len(events),
            "incidents": final,
            "noise_reduction_pct": round(100 * (1 - final / raw), 1) if raw else 0.0,
        }
        return incidents, stats
