"""
Phase 5 — ServiceNow ITOM / ITSM Integration.

Two backends behind one interface:
  * ServiceNowConnector — real REST calls to a ServiceNow instance (PDI works).
    Set SN_INSTANCE / SN_USER / SN_PASSWORD env vars.
  * MockITSM — in-memory store with identical behavior for demo / CI.

Covers the JD checklist: incident auto-creation, CMDB sync, event rules
(severity->priority mapping), service mapping (business_service on the ticket),
and auto-close on successful remediation.
"""

import os
import time
from dataclasses import dataclass, field

from .correlation import Incident
from .topology import TopologyMap

PRIORITY_MAP = {"critical": ("1", "1"), "warning": ("2", "3"), "info": ("3", "4")}  # impact, urgency


@dataclass
class Ticket:
    number: str
    sys_id: str
    incident_id: str
    short_description: str
    description: str
    impact: str
    urgency: str
    business_service: str | None
    cmdb_ci: str | None
    state: str = "New"           # New | In Progress | Resolved
    opened_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    close_notes: str = ""


class ITSMBackend:
    def create_incident(self, t: Ticket) -> Ticket: raise NotImplementedError
    def resolve_incident(self, t: Ticket, notes: str) -> Ticket: raise NotImplementedError
    def sync_cmdb(self, topology: TopologyMap) -> int: raise NotImplementedError


class MockITSM(ITSMBackend):
    def __init__(self):
        self.tickets: list[Ticket] = []
        self.cmdb: dict[str, dict] = {}
        self._seq = 10000

    def create_incident(self, t: Ticket) -> Ticket:
        self._seq += 1
        t.number = f"INC{self._seq:07d}"
        t.sys_id = f"mock-{self._seq}"
        self.tickets.append(t)
        return t

    def resolve_incident(self, t: Ticket, notes: str) -> Ticket:
        t.state = "Resolved"
        t.resolved_at = time.time()
        t.close_notes = notes
        return t

    def sync_cmdb(self, topology: TopologyMap) -> int:
        for ci_id, ci in topology.cis.items():
            self.cmdb[ci_id] = {
                "name": ci.name, "sys_class_name": _sn_class(ci.ci_type),
                "u_layer": ci.layer, "u_business_service": ci.business_service,
                "u_depends_on": ci.depends_on,
            }
        return len(self.cmdb)


class ServiceNowConnector(ITSMBackend):
    """Real ServiceNow REST backend. Requires `requests` + env vars:
    SN_INSTANCE (e.g. dev12345), SN_USER, SN_PASSWORD."""

    def __init__(self):
        import requests  # lazy import
        self.requests = requests
        self.instance = os.environ["SN_INSTANCE"]
        self.auth = (os.environ["SN_USER"], os.environ["SN_PASSWORD"])
        self.base = f"https://{self.instance}.service-now.com/api/now/table"

    def create_incident(self, t: Ticket) -> Ticket:
        r = self.requests.post(f"{self.base}/incident", auth=self.auth, json={
            "short_description": t.short_description,
            "description": t.description,
            "impact": t.impact, "urgency": t.urgency,
            "business_service": t.business_service or "",
            "cmdb_ci": t.cmdb_ci or "",
            "category": "AIOps", "contact_type": "monitoring",
        }, timeout=15)
        r.raise_for_status()
        rec = r.json()["result"]
        t.number, t.sys_id = rec["number"], rec["sys_id"]
        return t

    def resolve_incident(self, t: Ticket, notes: str) -> Ticket:
        r = self.requests.patch(f"{self.base}/incident/{t.sys_id}", auth=self.auth, json={
            "state": "6", "close_code": "Solved (Permanently)",
            "close_notes": notes,
        }, timeout=15)
        r.raise_for_status()
        t.state, t.close_notes = "Resolved", notes
        t.resolved_at = time.time()
        return t

    def sync_cmdb(self, topology: TopologyMap) -> int:
        count = 0
        for ci in topology.cis.values():
            r = self.requests.post(f"{self.base}/{_sn_class(ci.ci_type)}",
                                   auth=self.auth, json={"name": ci.name}, timeout=15)
            if r.ok:
                count += 1
        return count


def _sn_class(ci_type: str) -> str:
    return {
        "node": "cmdb_ci_server", "pod": "cmdb_ci_kubernetes_pod",
        "service": "cmdb_ci_service", "database": "cmdb_ci_database",
        "loadbalancer": "cmdb_ci_lb", "network": "cmdb_ci_netgear",
    }.get(ci_type, "cmdb_ci")


class ITSMBridge:
    """Orchestrates incident <-> ticket lifecycle."""

    def __init__(self, backend: ITSMBackend | None = None, sn_config=None):
        """
        Backend selection order:
          1. explicit backend argument
          2. sn_config (e.g. supplied from the UI config tab)
          3. SN_* environment variables
          4. MockITSM (demo default)
        """
        if backend:
            self.backend = backend
        elif sn_config is not None:
            from .servicenow import EnterpriseServiceNowConnector
            self.backend = EnterpriseServiceNowConnector(sn_config)
        elif os.environ.get("SN_INSTANCE"):
            from .servicenow import EnterpriseServiceNowConnector
            self.backend = EnterpriseServiceNowConnector()
        else:
            self.backend = MockITSM()

    @property
    def backend_name(self) -> str:
        return type(self.backend).__name__

    @property
    def is_live(self) -> bool:
        return self.backend_name != "MockITSM"

    def open_ticket(self, inc: Incident) -> Ticket:
        impact, urgency = PRIORITY_MAP.get(inc.severity, ("3", "4"))
        desc_lines = [
            f"AIOps correlated incident {inc.incident_id}",
            f"Probable root cause: {inc.probable_root_cause}",
            f"Impacted CIs: {', '.join(inc.impacted_cis)}",
            f"Raw alerts correlated: {inc.raw_alert_count}",
            "", "Correlated events:",
        ] + [f"  - [{e.severity}] {e.message} (x{e.count})" for e in inc.events]
        ticket = Ticket(
            number="", sys_id="", incident_id=inc.incident_id,
            short_description=f"[AIOps] {inc.title}",
            description="\n".join(desc_lines),
            impact=impact, urgency=urgency,
            business_service=inc.business_service,
            cmdb_ci=inc.probable_root_cause,
        )
        return self.backend.create_incident(ticket)

    def close_if_remediated(self, inc: Incident, ticket: Ticket) -> Ticket:
        if inc.status == "resolved":
            return self.backend.resolve_incident(
                ticket, f"Auto-remediated via runbook: {inc.remediation}")
        return ticket

    def sync_cmdb(self, topology: TopologyMap) -> int:
        return self.backend.sync_cmdb(topology)
