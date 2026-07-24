"""
SLA — Service Level Agreements (the contractual layer).

An SLA is an external commitment with consequences. Key differences from an SLO:

  * Looser than the SLO on purpose. The gap is the safety margin — you should
    breach your internal objective long before you breach the contract.
  * Has a defined measurement period (usually calendar-monthly).
  * Allows exclusions (planned maintenance, customer-caused, force majeure)
    which are subtracted before the achieved figure is computed.
  * Carries service credits: tiered refunds triggered by the achieved level.

This module computes achieved availability, breach state, applicable credit
tier, and the resulting financial exposure — so reliability work can be argued
in currency, not just percentages.
"""

from dataclasses import dataclass, field

from .sli import SLIResult


@dataclass
class CreditTier:
    """If achieved availability falls BELOW threshold_pct, credit_pct applies."""
    threshold_pct: float
    credit_pct: float
    label: str = ""


@dataclass
class SLA:
    name: str
    customer: str
    business_service: str
    commitment_pct: float = 99.5          # contractual floor
    measurement_period_days: int = 30
    monthly_contract_value: float = 50000.0
    credit_tiers: list = field(default_factory=list)
    excluded_minutes: float = 0.0         # approved maintenance etc.

    def __post_init__(self):
        if not self.credit_tiers:
            self.credit_tiers = [
                CreditTier(99.5, 10.0, "Below commitment"),
                CreditTier(99.0, 25.0, "Significant breach"),
                CreditTier(95.0, 50.0, "Severe breach"),
            ]

    @property
    def period_minutes(self) -> float:
        return self.measurement_period_days * 24 * 60

    @property
    def allowed_downtime_minutes(self) -> float:
        return (1 - self.commitment_pct / 100) * self.period_minutes


@dataclass
class SLAStatus:
    sla: SLA
    achieved_pct: float
    downtime_minutes: float
    observed_window_h: float
    linked_slo_target: float | None = None

    @property
    def breached(self) -> bool:
        return self.achieved_pct < self.sla.commitment_pct

    @property
    def headroom_minutes(self) -> float:
        """Downtime still permissible before the contract is breached."""
        return round(max(self.sla.allowed_downtime_minutes -
                         self.downtime_minutes, 0.0), 1)

    @property
    def credit_tier(self) -> CreditTier | None:
        applicable = [t for t in self.sla.credit_tiers
                      if self.achieved_pct < t.threshold_pct]
        return max(applicable, key=lambda t: t.credit_pct) if applicable else None

    @property
    def credit_pct(self) -> float:
        tier = self.credit_tier
        return tier.credit_pct if tier else 0.0

    @property
    def financial_exposure(self) -> float:
        return round(self.sla.monthly_contract_value * self.credit_pct / 100, 2)

    @property
    def status(self) -> str:
        if self.breached:
            return "BREACHED"
        # within 20% of the allowed downtime budget
        used = (self.downtime_minutes / self.sla.allowed_downtime_minutes
                if self.sla.allowed_downtime_minutes else 0)
        if used >= 0.8:
            return "AT RISK"
        if used >= 0.5:
            return "WATCH"
        return "MEETING"

    @property
    def slo_buffer_pct(self) -> float | None:
        """Safety margin between the internal objective and the contract."""
        if self.linked_slo_target is None:
            return None
        return round(self.linked_slo_target - self.sla.commitment_pct, 3)

    @property
    def action(self) -> str:
        return {
            "BREACHED": (f"Service credit of {self.credit_pct:.0f}% "
                         f"(${self.financial_exposure:,.0f}) is owed. Notify "
                         f"account management and open a corrective action plan."),
            "AT RISK": ("Downtime budget nearly consumed. Escalate to engineering "
                        "leadership; defer risky changes this period."),
            "WATCH": "Over half the contractual downtime budget used. Monitor closely.",
            "MEETING": "Commitment being met with healthy margin.",
        }[self.status]


class SLAEngine:
    def __init__(self, slas: list[SLA] | None = None):
        self.slas = slas or DEFAULT_SLAS

    def evaluate(self, sli_result: SLIResult,
                 linked_slo_target: float | None = None) -> list[SLAStatus]:
        """
        Uses an availability SLI as the measured figure. Excluded minutes
        (approved maintenance) are credited back before assessment.
        """
        out = []
        for sla in self.slas:
            window_min = max(sli_result.window_h * 60, 1e-6)
            raw_downtime = window_min * (1 - sli_result.ratio_pct / 100)
            downtime = max(raw_downtime - sla.excluded_minutes, 0.0)
            achieved = round(max(0.0, 100 * (1 - downtime / window_min)), 3)
            out.append(SLAStatus(sla=sla, achieved_pct=achieved,
                                 downtime_minutes=round(downtime, 1),
                                 observed_window_h=sli_result.window_h,
                                 linked_slo_target=linked_slo_target))
        return out


DEFAULT_SLAS = [
    SLA(name="Payments Platform — Enterprise tier", customer="Enterprise customers",
        business_service="Payments Platform", commitment_pct=99.5,
        monthly_contract_value=50000.0),
]
