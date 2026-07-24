"""
Phase 8 — Scheduled report delivery.

  * ReportSubscription — one recipient's standing config (period, format, cadence)
  * send_report_email() — builds the attachment(s) via core/export.py and
    sends over SMTP (real send; no mock mode — fails loudly if unconfigured)
  * ReportScheduler — thin wrapper over APScheduler's BackgroundScheduler that
    fires send_report_email() on each subscription's cadence

Honest operational note: APScheduler's BackgroundScheduler only runs jobs
for as long as the hosting Python process is alive. Inside a Streamlit app
process (per-session, can be recycled), that means the schedule survives
only while the app process stays up — fine for a demo or a single
long-running deployment, but for enterprise-grade guaranteed delivery this
should be moved to a standalone worker (e.g. a small cron container or an
AWS EventBridge Scheduler -> Lambda, the same pattern already used for the
SLO/SLA engines) that calls send_report_email() directly. The scheduler
class below is structured so that swap is a call-site change, not a rewrite.

Env vars (mirrors SN_* / PROM_* naming used elsewhere in this codebase):
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
"""

import os
import smtplib
import tempfile
from dataclasses import dataclass, field
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .export import export_excel, export_pdf
from .report_periods import ReportBundle


@dataclass
class ReportSubscription:
    name: str
    recipients: list[str]
    period_type: str = "weekly"      # daily | weekly | monthly | quarterly | custom
    custom_days: int | None = None
    formats: tuple = ("pdf",)        # any of "pdf", "excel"
    active: bool = True


@dataclass
class SMTPConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str

    @classmethod
    def from_env(cls) -> "SMTPConfig":
        host = os.environ.get("SMTP_HOST", "")
        user = os.environ.get("SMTP_USER", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        sender = os.environ.get("SMTP_FROM", user)
        port = int(os.environ.get("SMTP_PORT", "587"))
        if not (host and user and password):
            raise RuntimeError(
                "SMTP not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD "
                "(and optionally SMTP_PORT, SMTP_FROM) as environment "
                "variables / HF Secrets before scheduling report delivery.")
        return cls(host=host, port=port, user=user, password=password, sender=sender)


def send_report_email(subscription: ReportSubscription, bundle: ReportBundle,
                      smtp_config: SMTPConfig | None = None) -> dict:
    """
    Builds requested attachment(s) from `bundle` and emails them to
    subscription.recipients. Returns a small result dict for logging/UI
    feedback rather than raising on a partial-success path, so a single bad
    subscription doesn't take down a batch send in ReportScheduler.
    """
    cfg = smtp_config or SMTPConfig.from_env()

    msg = MIMEMultipart()
    msg["Subject"] = f"CloudOps-AI {bundle.period.label} — {bundle.period.range_str}"
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(subscription.recipients)
    msg.attach(MIMEText(
        f"Attached: the {bundle.period.label.lower()} reliability report "
        f"for {bundle.period.range_str}.\n\n"
        f"Generated automatically by CloudOps-AI.", "plain"))

    attached = []
    with tempfile.TemporaryDirectory() as tmp:
        if "pdf" in subscription.formats:
            path = export_pdf(bundle, f"{tmp}/report.pdf")
            with open(path, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="pdf")
            part.add_header("Content-Disposition", "attachment",
                            filename=f"CloudOps-AI_{subscription.period_type}_report.pdf")
            msg.attach(part)
            attached.append("pdf")
        if "excel" in subscription.formats:
            path = export_excel(bundle, f"{tmp}/report.xlsx")
            with open(path, "rb") as f:
                part = MIMEApplication(
                    f.read(),
                    _subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            part.add_header("Content-Disposition", "attachment",
                            filename=f"CloudOps-AI_{subscription.period_type}_report.xlsx")
            msg.attach(part)
            attached.append("excel")

        with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as server:
            server.starttls()
            server.login(cfg.user, cfg.password)
            server.sendmail(cfg.sender, subscription.recipients, msg.as_string())

    return {"subscription": subscription.name, "recipients": subscription.recipients,
            "formats_sent": attached, "status": "sent"}


# CRON_MAP: how each period type maps to an APScheduler cron trigger.
# Reports are generated the morning after the period closes.
CRON_MAP = {
    "daily":     dict(hour=6, minute=0),
    "weekly":    dict(day_of_week="mon", hour=6, minute=30),
    "monthly":   dict(day=1, hour=7, minute=0),
    "quarterly": dict(month="1,4,7,10", day=1, hour=7, minute=30),
}


class ReportScheduler:
    """
    Wraps APScheduler's BackgroundScheduler. `bundle_factory` is a callable
    `(period_type, custom_days) -> ReportBundle` supplied by the caller (the
    Streamlit app or a standalone worker), so this module has no dependency
    on how the bundle gets built — it only owns cadence + delivery.
    """

    def __init__(self, bundle_factory):
        from apscheduler.schedulers.background import BackgroundScheduler
        self.bundle_factory = bundle_factory
        self.scheduler = BackgroundScheduler()
        self.subscriptions: dict[str, ReportSubscription] = {}
        self._started = False

    def add_subscription(self, sub: ReportSubscription):
        self.subscriptions[sub.name] = sub
        if sub.period_type == "custom":
            trigger_kwargs = dict(hour=6, minute=0)   # custom needs manual cadence choice
        else:
            trigger_kwargs = CRON_MAP[sub.period_type]
        self.scheduler.add_job(
            self._fire, "cron", id=sub.name, replace_existing=True,
            kwargs={"sub_name": sub.name}, **trigger_kwargs)

    def remove_subscription(self, name: str):
        self.subscriptions.pop(name, None)
        if self.scheduler.get_job(name):
            self.scheduler.remove_job(name)

    def _fire(self, sub_name: str):
        sub = self.subscriptions.get(sub_name)
        if not sub or not sub.active:
            return
        bundle = self.bundle_factory(sub.period_type, sub.custom_days)
        return send_report_email(sub, bundle)

    def start(self):
        if not self._started:
            self.scheduler.start()
            self._started = True

    def shutdown(self):
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False
