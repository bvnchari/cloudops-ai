"""
SLO & Error Budget Engine — management-level reliability governance.

Implements the Google SRE model:
  * SLO target (e.g. 99.9% availability over 30 days)
  * Error budget = allowed unreliability = (1 - target) * window
  * Burn rate = how fast the budget is being consumed relative to the window.
    Burn rate 1.0 = exactly on pace to exhaust the budget at window end.
  * Multi-window burn-rate alerting (fast-burn vs slow-burn) so a short severe
    outage and a long low-grade degradation both surface, without paging on
    every blip.
"""

from dataclasses import dataclass, field

from .correlation import Incident

# Google SRE multi-window burn-rate thresholds
FAST_BURN = 14.4    # exhausts a 30d budget in ~2 days -> page
SLOW_BURN = 6.0     # exhausts in ~5 days -> ticket
CREEP_BURN = 1.0    # on pace to exactly exhaust -> watch


@dataclass
class SLO:
    name: str
    business_service: str
    target_pct: float = 99.9          # availability objective
    window_days: int = 30
    severity_counts: tuple = ("critical",)   # which incidents consume budget

    @property
    def budget_minutes(self) -> float:
        return (1 - self.target_pct / 100) * self.window_days * 24 * 60


@dataclass
class SLOStatus:
    slo: SLO
    consumed_minutes: float
    observed_window_h: float
    contributing_incidents: list = field(default_factory=list)

    @property
    def budget_minutes(self) -> float:
        return self.slo.budget_minutes

    @property
    def remaining_minutes(self) -> float:
        return max(self.budget_minutes - self.consumed_minutes, 0.0)

    @property
    def consumed_pct(self) -> float:
        if self.budget_minutes <= 0:
            return 100.0
        return round(100 * self.consumed_minutes / self.budget_minutes, 1)

    @property
    def burn_rate(self) -> float:
        """Budget-consumption pace relative to the SLO window."""
        window_min = self.slo.window_days * 24 * 60
        observed_min = max(self.observed_window_h * 60, 1e-6)
        if self.budget_minutes <= 0:
            return 0.0
        budget_frac = self.consumed_minutes / self.budget_minutes
        time_frac = observed_min / window_min
        return round(budget_frac / time_frac, 2) if time_frac > 0 else 0.0

    @property
    def achieved_pct(self) -> float:
        """Availability actually achieved over the observed window."""
        observed_min = max(self.observed_window_h * 60, 1e-6)
        return round(max(0.0, 100 * (1 - min(self.consumed_minutes, observed_min)
                                     / observed_min)), 3)

    @property
    def status(self) -> str:
        if self.remaining_minutes <= 0:
            return "EXHAUSTED"
        if self.burn_rate >= FAST_BURN:
            return "FAST BURN"
        if self.burn_rate >= SLOW_BURN:
            return "SLOW BURN"
        if self.burn_rate >= CREEP_BURN:
            return "AT RISK"
        return "HEALTHY"

    @property
    def action(self) -> str:
        return {
            "EXHAUSTED": "Freeze feature releases; reliability work takes priority.",
            "FAST BURN": "Page on-call. Budget exhausts in days at this rate.",
            "SLOW BURN": "Raise a ticket; investigate within the current sprint.",
            "AT RISK": "Monitor. On pace to consume the full budget this window.",
            "HEALTHY": "No action. Budget available for feature velocity.",
        }[self.status]


class SLOEngine:
    def __init__(self, slos: list[SLO] | None = None,
                 observation_window_h: float = 2.0):
        self.slos = slos or DEFAULT_SLOS
        self.window_h = observation_window_h

    def evaluate_from_sli(self, slo: "SLO", sli_result) -> "SLOStatus":
        """
        Bind an SLO to a measured SLI — the correct chain: the indicator is
        measured, the objective judges it. Bad-event time becomes budget
        consumption.
        """
        window_min = max(sli_result.window_h * 60, 1e-6)
        consumed = window_min * (1 - sli_result.ratio_pct / 100)
        return SLOStatus(slo=slo, consumed_minutes=round(consumed, 2),
                         observed_window_h=sli_result.window_h,
                         contributing_incidents=[
                             (str(a), b) for a, b in sli_result.bad_samples])

    def evaluate(self, incidents: list[Incident]) -> list[SLOStatus]:
        out = []
        for slo in self.slos:
            consumed = 0.0
            contributing = []
            for inc in incidents:
                if inc.business_service != slo.business_service:
                    continue
                if inc.severity not in slo.severity_counts:
                    continue
                # unresolved incidents are still burning: charge the full window
                end = inc.resolved_ts or (inc.created_ts + self.window_h * 3600)
                minutes = max((end - inc.created_ts) / 60, 0.0)
                consumed += minutes
                contributing.append((inc.incident_id, round(minutes, 1)))
            out.append(SLOStatus(slo=slo, consumed_minutes=round(consumed, 1),
                                 observed_window_h=self.window_h,
                                 contributing_incidents=contributing))
        return out


DEFAULT_SLOS = [
    SLO(name="Payments availability", business_service="Payments Platform",
        target_pct=99.9, window_days=30),
    SLO(name="Payments latency (critical+warning)",
        business_service="Payments Platform",
        target_pct=99.5, window_days=30, severity_counts=("critical", "warning")),
]
