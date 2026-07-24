"""
Phase 6 — Governance & Reporting.

Computes executive KPIs live from pipeline output (not hardcoded):
MTTR, MTBF, alert reduction %, automation rate, estimated automation savings,
service availability.
"""

from dataclasses import dataclass

from .correlation import Incident


@dataclass
class KPIReport:
    total_raw_alerts: int
    total_incidents: int
    alert_reduction_pct: float
    mttr_minutes: float | None
    mtbf_hours: float | None
    auto_remediated: int
    automation_rate_pct: float
    est_automation_savings_usd: float
    service_availability_pct: float

    def to_dict(self) -> dict:
        return {
            "Raw Alerts Ingested": self.total_raw_alerts,
            "Actionable Incidents": self.total_incidents,
            "Alert Reduction": f"{self.alert_reduction_pct}%",
            "MTTR": f"{self.mttr_minutes:.0f} min" if self.mttr_minutes else "n/a",
            "MTBF": f"{self.mtbf_hours:.1f} h" if self.mtbf_hours else "n/a",
            "Auto-Remediated": self.auto_remediated,
            "Automation Rate": f"{self.automation_rate_pct}%",
            "Est. Automation Savings": f"${self.est_automation_savings_usd:,.0f}/yr",
            "Service Availability": f"{self.service_availability_pct}%",
        }


class KPIEngine:
    def __init__(self, manual_mttr_min: float = 45.0,
                 engineer_cost_per_hr: float = 60.0,
                 incidents_per_year_estimate: int = 2400,
                 observation_window_h: float = 2.0):
        self.manual_mttr_min = manual_mttr_min
        self.engineer_cost_per_hr = engineer_cost_per_hr
        self.incidents_per_year = incidents_per_year_estimate
        self.window_h = observation_window_h

    def compute(self, raw_alert_count: int, incidents: list[Incident],
                ticket_lifecycles: list[dict] | None = None) -> KPIReport:
        """
        ticket_lifecycles: optional list of {opened_at, resolved_at} dicts pulled
        back from a real ITSM system (see EnterpriseServiceNowConnector
        .fetch_lifecycle). When provided, MTTR is computed from actual ticket
        timestamps — production truth — instead of simulated incident timing.
        """
        n_inc = len(incidents)
        resolved = [i for i in incidents if i.status == "resolved" and i.resolved_ts]
        auto = len(resolved)

        mttr = None
        real_cycles = [c for c in (ticket_lifecycles or [])
                       if c.get("opened_at") and c.get("resolved_at")]
        if real_cycles:
            mttr = sum(c["resolved_at"] - c["opened_at"]
                       for c in real_cycles) / len(real_cycles) / 60
        elif resolved:
            mttr = sum((i.resolved_ts - i.created_ts) for i in resolved) / len(resolved) / 60

        mtbf = None
        if n_inc > 1:
            times = sorted(i.created_ts for i in incidents)
            gaps = [(b - a) for a, b in zip(times, times[1:])]
            mtbf = (sum(gaps) / len(gaps)) / 3600 if gaps else None

        reduction = round(100 * (1 - n_inc / raw_alert_count), 1) if raw_alert_count else 0.0
        automation_rate = round(100 * auto / n_inc, 1) if n_inc else 0.0

        # savings: minutes of manual toil avoided per auto-remediated incident, annualized
        saved_min_per_inc = max(self.manual_mttr_min - (mttr or self.manual_mttr_min * 0.2), 0)
        annual_auto_incidents = self.incidents_per_year * (automation_rate / 100)
        savings = annual_auto_incidents * saved_min_per_inc / 60 * self.engineer_cost_per_hr

        # availability over observation window: downtime = unresolved critical incident time
        downtime_s = 0.0
        for i in incidents:
            if i.severity == "critical":
                end = i.resolved_ts or (i.created_ts + self.window_h * 3600 * 0.25)
                downtime_s += max(end - i.created_ts, 0)
        window_s = self.window_h * 3600
        availability = round(max(0.0, 100 * (1 - min(downtime_s, window_s) / window_s)), 2)

        return KPIReport(
            total_raw_alerts=raw_alert_count,
            total_incidents=n_inc,
            alert_reduction_pct=reduction,
            mttr_minutes=mttr,
            mtbf_hours=mtbf,
            auto_remediated=auto,
            automation_rate_pct=automation_rate,
            est_automation_savings_usd=round(savings, 0),
            service_availability_pct=availability,
        )
