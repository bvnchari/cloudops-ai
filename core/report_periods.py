"""
Phase 8 — Report Periods & Bundling.

Wraps the existing KPI / SLI / SLO / SLA / scorecard objects (core/kpi.py,
core/sli.py, core/slo.py, core/sla.py, core/insights.py) into a single
ReportBundle scoped to a reporting period: daily, weekly, monthly, quarterly,
or a custom date range.

Honest scope note: the pipeline (core/telemetry.py) currently produces one
observation window of data per run (SyntheticSource, or PrometheusSource once
wired in per core/telemetry.py's connector). There is no historical
time-series store yet. Until one exists, every period type here evaluates
the SAME measured window against an SLO/SLA sized to that period length —
this is the correct seam to plug a real time-series backend into later
(swap the MetricSource, keep everything below unchanged). Nothing in this
module fabricates historical numbers; period labels and window sizing are
real, the underlying measurement window is whatever the active MetricSource
returns.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}


@dataclass
class ReportPeriod:
    label: str
    period_type: str          # daily | weekly | monthly | quarterly | custom
    days: int
    start: datetime
    end: datetime

    @property
    def range_str(self) -> str:
        return f"{self.start:%Y-%m-%d} to {self.end:%Y-%m-%d}"


def build_period(period_type: str, custom_days: int | None = None,
                  end: datetime | None = None) -> ReportPeriod:
    """Builds a ReportPeriod. For period_type='custom', custom_days is required."""
    end = end or datetime.now(timezone.utc)
    if period_type == "custom":
        if not custom_days or custom_days < 1:
            raise ValueError("custom_days must be a positive integer for period_type='custom'")
        days = custom_days
    elif period_type in PERIOD_DAYS:
        days = PERIOD_DAYS[period_type]
    else:
        raise ValueError(f"Unknown period_type '{period_type}'. "
                         f"Use one of {list(PERIOD_DAYS)} or 'custom'.")
    start = end - timedelta(days=days)
    label = f"{period_type.capitalize()} Report"
    return ReportPeriod(label=label, period_type=period_type, days=days,
                       start=start, end=end)


@dataclass
class ReportBundle:
    """Everything an exec/PDF/Excel report needs, scoped to one period."""
    period: ReportPeriod
    kpi: object
    stats: dict
    slo_statuses: list = field(default_factory=list)
    sla_statuses: list = field(default_factory=list)
    sli_results: list = field(default_factory=list)
    scorecard: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    hotspots: list = field(default_factory=list)


def build_bundle(period: ReportPeriod, kpi, stats, slo_statuses=None,
                 sla_statuses=None, sli_results=None, scorecard=None,
                 gaps=None, hotspots=None) -> ReportBundle:
    # SLO/SLA objects carry a fixed window_days sized for their own target
    # (e.g. 30d, 90d) independent of the report period — that's correct,
    # since the *contract* window and the *report* period are different
    # concepts. We don't rescale them here; we just report both side by side.
    return ReportBundle(
        period=period, kpi=kpi, stats=stats,
        slo_statuses=slo_statuses or [], sla_statuses=sla_statuses or [],
        sli_results=sli_results or [], scorecard=scorecard or [],
        gaps=gaps or [], hotspots=hotspots or [],
    )
