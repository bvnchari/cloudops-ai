"""
Enterprise Jira Cloud Integration — the ENGINEERING BACKLOG half of the
ITSM story.

Real-world pattern this models: ServiceNow owns the ITSM ticket lifecycle
(impact/urgency, SLA clock, customer-facing state) while Jira owns the
engineering backlog (who fixes it, sprint planning, code-linked work).
Many enterprises wire the two together (Jira Service Management <->
ServiceNow spoke, Exalate, Unito, or a custom webhook). Rather than depend
on that connector existing, CloudOps-AI files the Jira issue directly and
stamps the ServiceNow ticket number onto it — so the two systems stay
cross-referenced even before/without a native SN<->Jira sync.

AIOps logic (core of this module, see `sync_candidates` / `sync_to_jira`):
  A ticket becomes an engineering-backlog candidate — worth a Jira issue —
  when EITHER is true:
    * Automation gap: no remediation runbook matched this incident
      (core.insights.automation_gaps already computes this cost in
      toil-hours; we reuse that list rather than re-deriving it).
    * SLA breach: the ITSM ticket has exceeded its impact-based resolution
      target (see core.reports.itsm_report_markdown's SLA_HOURS convention)
      and is still open — i.e. a human hasn't caught up with it yet.
  Tickets that are neither don't get engineering noise filed against them.

Configuration (env vars, or the UI Config tab, session-scoped only):
  JIRA_BASE_URL     e.g. https://yourcompany.atlassian.net   (required)
  JIRA_EMAIL        Atlassian account email                  (required)
  JIRA_API_TOKEN    API token from id.atlassian.com           (required)
  JIRA_PROJECT_KEY  e.g. AIOPS                                (required)
  JIRA_ISSUE_TYPE   default "Bug"
"""

import os
import time
from dataclasses import dataclass, field


class JiraError(RuntimeError):
    pass


@dataclass
class JiraConfig:
    base_url: str
    email: str = ""
    api_token: str = field(default="", repr=False)
    project_key: str = ""
    issue_type: str = "Bug"
    timeout_s: float = 15.0
    max_retries: int = 3

    def validate(self) -> list[str]:
        problems = []
        if not self.base_url:
            problems.append("Jira base URL is required (e.g. https://yourco.atlassian.net).")
        if self.base_url and not self.base_url.startswith("https://"):
            problems.append("Jira base URL must start with https://")
        if not self.email:
            problems.append("Atlassian account email is required.")
        if not self.api_token:
            problems.append("Jira API token is required (id.atlassian.com -> API tokens).")
        if not self.project_key:
            problems.append("Project key is required (e.g. AIOPS).")
        return problems

    @classmethod
    def from_env(cls) -> "JiraConfig | None":
        if not os.environ.get("JIRA_BASE_URL"):
            return None
        return cls(
            base_url=os.environ["JIRA_BASE_URL"].rstrip("/"),
            email=os.environ.get("JIRA_EMAIL", ""),
            api_token=os.environ.get("JIRA_API_TOKEN", ""),
            project_key=os.environ.get("JIRA_PROJECT_KEY", ""),
            issue_type=os.environ.get("JIRA_ISSUE_TYPE", "Bug"),
            timeout_s=float(os.environ.get("JIRA_TIMEOUT_S", "15")),
            max_retries=int(os.environ.get("JIRA_MAX_RETRIES", "3")),
        )


@dataclass
class JiraIssue:
    key: str                 # e.g. "AIOPS-42"
    incident_id: str
    sn_ticket_number: str | None
    summary: str
    status: str = "To Do"
    url: str = ""
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    reason: str = ""          # why this was filed: "automation_gap" | "sla_breach"


class JiraBackend:
    def create_issue(self, summary: str, description: str,
                     incident_id: str, sn_ticket_number: str | None,
                     reason: str) -> JiraIssue:
        raise NotImplementedError

    def test_connection(self) -> dict:
        raise NotImplementedError


class MockJira(JiraBackend):
    """In-memory Jira for demo/CI — same interface, no network calls."""

    def __init__(self, project_key: str = "AIOPS"):
        self.project_key = project_key
        self.issues: list[JiraIssue] = []
        self._seq = 1000

    def create_issue(self, summary, description, incident_id,
                     sn_ticket_number, reason) -> JiraIssue:
        self._seq += 1
        key = f"{self.project_key}-{self._seq}"
        issue = JiraIssue(key=key, incident_id=incident_id,
                          sn_ticket_number=sn_ticket_number, summary=summary,
                          url=f"https://demo.atlassian.net/browse/{key}",
                          reason=reason)
        self.issues.append(issue)
        return issue

    def test_connection(self) -> dict:
        return {"account": "mock@demo.local", "reachable": True, "mode": "mock"}


class EnterpriseJiraConnector(JiraBackend):
    """Real Jira Cloud REST API v3 backend."""

    def __init__(self, config: JiraConfig | None = None):
        import requests
        cfg = config or JiraConfig.from_env()
        if cfg is None:
            raise JiraError("No Jira configuration supplied "
                            "(pass JiraConfig or set JIRA_BASE_URL).")
        problems = cfg.validate()
        if problems:
            raise JiraError("Invalid configuration: " + " ".join(problems))
        self.config = cfg
        self.requests = requests
        self.base = cfg.base_url.rstrip("/")
        self.timeout = cfg.timeout_s
        self.max_retries = cfg.max_retries
        self.session = requests.Session()
        self.session.auth = (cfg.email, cfg.api_token)
        self.session.headers.update({"Accept": "application/json",
                                     "Content-Type": "application/json"})

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base}{path}"
        last_err = None
        for attempt in range(self.max_retries):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if r.status_code == 429 or r.status_code >= 500:
                    raise JiraError(f"{r.status_code}: {r.text[:200]}")
                if not r.ok:
                    raise JiraError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
                return r.json() if r.text else {}
            except (self.requests.exceptions.ConnectionError,
                    self.requests.exceptions.Timeout, JiraError) as e:
                last_err = e
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        raise JiraError(f"Jira request failed after {self.max_retries} attempts: {last_err}")

    def create_issue(self, summary, description, incident_id,
                     sn_ticket_number, reason) -> JiraIssue:
        desc_doc = {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": description}]}],
        }
        payload = {"fields": {
            "project": {"key": self.config.project_key},
            "summary": summary,
            "description": desc_doc,
            "issuetype": {"name": self.config.issue_type},
            "labels": ["cloudops-ai", "aiops", reason],
        }}
        data = self._request("POST", "/rest/api/3/issue", json=payload)
        key = data["key"]
        return JiraIssue(key=key, incident_id=incident_id,
                         sn_ticket_number=sn_ticket_number, summary=summary,
                         url=f"{self.base}/browse/{key}", reason=reason)

    def fetch_status(self, issue_key: str) -> dict | None:
        data = self._request(
            "GET", f"/rest/api/3/issue/{issue_key}"
                   "?fields=status,resolutiondate")
        f = data.get("fields", {})
        return {"status": (f.get("status") or {}).get("name"),
               "resolutiondate": f.get("resolutiondate")}

    def test_connection(self) -> dict:
        data = self._request("GET", "/rest/api/3/myself")
        return {"account": data.get("emailAddress") or data.get("displayName"),
               "reachable": True, "mode": "live"}


class JiraBridge:
    """Backend selection, mirrors ITSMBridge's pattern for consistency."""

    def __init__(self, jira_config: JiraConfig | None = None):
        if jira_config is not None:
            self.backend = EnterpriseJiraConnector(jira_config)
        elif os.environ.get("JIRA_BASE_URL"):
            self.backend = EnterpriseJiraConnector()
        else:
            self.backend = MockJira()

    @property
    def is_live(self) -> bool:
        return type(self.backend).__name__ != "MockJira"

    def create_issue(self, *a, **kw) -> JiraIssue:
        return self.backend.create_issue(*a, **kw)

    def test_connection(self) -> dict:
        return self.backend.test_connection()


# ---------------- AIOps sync logic ----------------

def sla_breached(ticket, sla_hours: dict | None = None, now: float | None = None) -> bool:
    sla_hours = sla_hours or {"1": 1.0, "2": 4.0, "3": 8.0}
    now = now or time.time()
    elapsed_h = ((ticket.resolved_at or now) - ticket.opened_at) / 3600
    return ticket.state != "Resolved" and elapsed_h > sla_hours.get(ticket.impact, 8.0)


def sync_candidates(tickets, gaps, already_filed: set[str] | None = None):
    """Decides which tickets are worth an engineering Jira issue right now.

    Returns a list of (ticket, reason) tuples. `gaps` is the output of
    core.insights.automation_gaps(incidents) — reused, not re-derived, so
    this logic always agrees with what the On-Call/Executive tabs show as
    the automation backlog. `already_filed` is a set of incident_ids that
    already have a Jira issue, so re-running never double-files.
    """
    already_filed = already_filed or set()
    gap_ids = {g.incident_id for g in gaps}
    out = []
    for t in tickets:
        if t.incident_id in already_filed:
            continue
        if t.incident_id in gap_ids:
            out.append((t, "automation_gap"))
        elif sla_breached(t):
            out.append((t, "sla_breach"))
    return out


def sync_to_jira(bridge: JiraBridge, tickets, gaps,
                 already_filed: set[str] | None = None,
                 progress_cb=None) -> list[JiraIssue]:
    """Files Jira issues for every sync candidate. Per-item error isolation:
    one failure doesn't stop the batch — caller can inspect the returned
    list length against len(candidates) to see if anything was skipped."""
    candidates = sync_candidates(tickets, gaps, already_filed)
    filed: list[JiraIssue] = []
    for i, (t, reason) in enumerate(candidates, 1):
        reason_label = ("no automation runbook matched" if reason == "automation_gap"
                        else "SLA target exceeded while still open")
        description = (
            f"Auto-filed by CloudOps-AI.\n\n"
            f"ServiceNow ticket: {t.number}\n"
            f"Incident: {t.incident_id}\n"
            f"Business service: {t.business_service or 'Unmapped'}\n"
            f"Impact: P{t.impact} / Urgency: {t.urgency}\n"
            f"Reason filed: {reason_label}\n\n"
            f"{t.description}"
        )
        try:
            issue = bridge.create_issue(
                summary=f"[AIOps] {t.short_description}",
                description=description, incident_id=t.incident_id,
                sn_ticket_number=t.number, reason=reason)
            filed.append(issue)
        except Exception:
            pass  # per-item isolation — caller sees fewer results than candidates
        if progress_cb:
            progress_cb(i, len(candidates), t.number)
    return filed
