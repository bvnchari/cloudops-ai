"""
SLI — Service Level Indicators (the measurement layer).

An SLI is a ratio of good events to valid events. Two standard flavours:

  * Event/request-based:  good datapoints / all datapoints
      e.g. "% of minutes where p99 latency stayed under 500ms"
      Computed from metric time-series (Phase 1 telemetry).

  * Time-based availability:  uptime / total time
      e.g. "% of the window with no critical incident open"
      Computed from correlated incidents — works in live ServiceNow mode too,
      where no metric series exist.

SLIs are deliberately separate from SLOs: the indicator is a fact about the
system, the objective is a decision about what's good enough. Same SLI can
back several SLOs (internal target) and an SLA (external commitment).
"""

from dataclasses import dataclass, field

from .correlation import Incident
from .telemetry import MetricPoint

COMPARISONS = {
    "lt": lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
    "gt": lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
}


@dataclass
class SLIDefinition:
    name: str
    kind: str                 # availability | latency | error_rate | custom
    ci_id: str = ""
    metric: str = ""
    threshold: float = 0.0
    comparison: str = "lt"    # good event = value <comparison> threshold
    unit: str = ""
    description: str = ""


@dataclass
class SLIResult:
    definition: SLIDefinition
    good_events: int
    valid_events: int
    window_h: float
    source: str               # "metric_series" | "incident_timeline"
    bad_samples: list = field(default_factory=list)

    @property
    def ratio_pct(self) -> float:
        if self.valid_events <= 0:
            return 100.0
        return round(100 * self.good_events / self.valid_events, 3)

    @property
    def bad_events(self) -> int:
        return max(self.valid_events - self.good_events, 0)

    @property
    def headline(self) -> str:
        d = self.definition
        if self.source == "incident_timeline":
            return f"{self.ratio_pct}% of the window free of critical incidents"
        return (f"{self.ratio_pct}% of samples had {d.metric} "
                f"{d.comparison} {d.threshold}{d.unit}")


class SLICalculator:
    """Computes SLIs from either metric series or the incident timeline."""

    # ---------- event/request-based ----------
    def from_series(self, definition: SLIDefinition,
                    series: list[MetricPoint]) -> SLIResult:
        if not series:
            return SLIResult(definition, 0, 0, 0.0, "metric_series")
        cmp_fn = COMPARISONS[definition.comparison]
        good, bad_samples = 0, []
        for p in series:
            if cmp_fn(p.value, definition.threshold):
                good += 1
            elif len(bad_samples) < 5:
                bad_samples.append((p.ts, round(p.value, 2)))
        window_h = (series[-1].ts - series[0].ts) / 3600 if len(series) > 1 else 0.0
        return SLIResult(definition=definition, good_events=good,
                         valid_events=len(series), window_h=round(window_h, 2),
                         source="metric_series", bad_samples=bad_samples)

    # ---------- time-based availability ----------
    def from_incidents(self, definition: SLIDefinition, incidents: list[Incident],
                       window_h: float = 2.0,
                       business_service: str | None = None,
                       severities: tuple = ("critical",),
                       bucket_seconds: int = 60) -> SLIResult:
        """
        Discretises the window into buckets and marks a bucket bad if any
        qualifying incident was open during it. Overlapping incidents therefore
        do not double-count downtime — a common error in naive calculations.
        """
        total_buckets = max(int(window_h * 3600 / bucket_seconds), 1)
        relevant = [
            i for i in incidents
            if i.severity in severities
            and (business_service is None or i.business_service == business_service)
        ]
        if not relevant:
            return SLIResult(definition, total_buckets, total_buckets,
                             window_h, "incident_timeline")

        window_end = max((i.resolved_ts or i.created_ts) for i in relevant)
        window_start = window_end - window_h * 3600

        bad = set()
        samples = []
        for inc in relevant:
            start = max(inc.created_ts, window_start)
            end = min(inc.resolved_ts or window_end, window_end)
            if end <= start:
                continue
            s_idx = int((start - window_start) / bucket_seconds)
            e_idx = int((end - window_start) / bucket_seconds)
            for b in range(max(s_idx, 0), min(e_idx + 1, total_buckets)):
                bad.add(b)
            if len(samples) < 5:
                samples.append((inc.incident_id, round((end - start) / 60, 1)))

        good = total_buckets - len(bad)
        return SLIResult(definition=definition, good_events=good,
                         valid_events=total_buckets, window_h=window_h,
                         source="incident_timeline", bad_samples=samples)


# Demo SLI definitions covering the classic four golden signals
DEFAULT_SLIS = [
    SLIDefinition(name="API latency", kind="latency", ci_id="svc-api",
                  metric="api_latency_p99", threshold=500.0, comparison="lt",
                  unit="ms", description="p99 request latency under 500ms"),
    SLIDefinition(name="API error rate", kind="error_rate", ci_id="svc-api",
                  metric="api_error_rate", threshold=1.0, comparison="lt",
                  unit="%", description="request error rate under 1%"),
    SLIDefinition(name="Edge availability", kind="availability", ci_id="alb-01",
                  metric="alb_5xx_rate", threshold=1.0, comparison="lt",
                  unit="%", description="load balancer 5xx rate under 1%"),
    SLIDefinition(name="Platform uptime", kind="availability",
                  description="window free of critical incidents"),
]
