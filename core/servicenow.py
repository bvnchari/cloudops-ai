"""
Enterprise ServiceNow Connector (Phase 5, production-grade).

Improvements over the basic connector:
  * Auth: Basic OR OAuth2 (password grant) — auto-selected from env
  * Resilience: retries with exponential backoff, connect/read timeouts,
    session reuse, clear error surfacing
  * ITOM-correct integration path: pushes to the Event Management API
    (em_event) when SN_USE_EVENT_API=1, letting ServiceNow's own event rules
    correlate and open incidents — the pattern enterprises actually deploy.
    Falls back to direct Table API incident creation otherwise (PDI-friendly:
    PDIs don't ship the ITOM Event Management plugin).
  * CMDB upsert: queries by name before insert -> idempotent, no duplicates
  * Real KPI feed: fetches opened_at / resolved_at back from ServiceNow so
    MTTR is computed from actual ticket lifecycle, not simulation.

Configuration (env vars, or a .env file loaded by your shell):
  SN_INSTANCE       e.g. dev212345          (required)
  SN_USER           integration username    (required for basic auth)
  SN_PASSWORD       password                (required for basic auth)
  SN_CLIENT_ID      OAuth client id         (optional -> switches to OAuth)
  SN_CLIENT_SECRET  OAuth client secret     (optional)
  SN_USE_EVENT_API  "1" to use em_event     (optional, default off)
  SN_TIMEOUT_S      request timeout         (optional, default 15)
  SN_MAX_RETRIES    retry attempts          (optional, default 3)
"""

import os
import time
from datetime import datetime, timezone

from .itsm import ITSMBackend, Ticket, _sn_class
from .topology import TopologyMap

SN_DT_FMT = "%Y-%m-%d %H:%M:%S"   # ServiceNow returns UTC in this format


class ServiceNowError(RuntimeError):
    pass


class EnterpriseServiceNowConnector(ITSMBackend):
    def __init__(self):
        import requests
        self.requests = requests
        self.instance = os.environ["SN_INSTANCE"]
        self.base = f"https://{self.instance}.service-now.com"
        self.timeout = float(os.environ.get("SN_TIMEOUT_S", "15"))
        self.max_retries = int(os.environ.get("SN_MAX_RETRIES", "3"))
        self.use_event_api = os.environ.get("SN_USE_EVENT_API", "0") == "1"
        self.session = requests.Session()
        self._token = None
        self._token_expiry = 0.0
        self._oauth = bool(os.environ.get("SN_CLIENT_ID"))
        if not self._oauth:
            self.session.auth = (os.environ["SN_USER"], os.environ["SN_PASSWORD"])
        self.session.headers.update({"Accept": "application/json",
                                     "Content-Type": "application/json"})

    # ---------------- auth ----------------
    def _ensure_token(self):
        if not self._oauth or time.time() < self._token_expiry - 60:
            return
        r = self.requests.post(f"{self.base}/oauth_token.do", data={
            "grant_type": "password",
            "client_id": os.environ["SN_CLIENT_ID"],
            "client_secret": os.environ["SN_CLIENT_SECRET"],
            "username": os.environ["SN_USER"],
            "password": os.environ["SN_PASSWORD"],
        }, timeout=self.timeout)
        if not r.ok:
            raise ServiceNowError(f"OAuth token request failed: {r.status_code} {r.text[:200]}")
        tok = r.json()
        self._token = tok["access_token"]
        self._token_expiry = time.time() + int(tok.get("expires_in", 1800))
        self.session.headers["Authorization"] = f"Bearer {self._token}"

    # ---------------- resilient request ----------------
    def _request(self, method: str, path: str, **kwargs):
        self._ensure_token()
        url = f"{self.base}{path}"
        last_err = None
        for attempt in range(self.max_retries):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if r.status_code == 401 and self._oauth:
                    self._token_expiry = 0        # force refresh, retry
                    self._ensure_token()
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    raise ServiceNowError(f"{r.status_code}: {r.text[:200]}")
                if not r.ok:
                    raise ServiceNowError(
                        f"{method} {path} -> {r.status_code}: {r.text[:300]}")
                return r.json()
            except (self.requests.exceptions.ConnectionError,
                    self.requests.exceptions.Timeout,
                    ServiceNowError) as e:
                last_err = e
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)      # 1s, 2s, 4s backoff
        raise ServiceNowError(f"ServiceNow request failed after "
                              f"{self.max_retries} attempts: {last_err}")

    # ---------------- incidents ----------------
    def create_incident(self, t: Ticket) -> Ticket:
        if self.use_event_api:
            return self._send_event(t)
        data = self._request("POST", "/api/now/table/incident", json={
            "short_description": t.short_description,
            "description": t.description,
            "impact": t.impact, "urgency": t.urgency,
            "cmdb_ci": t.cmdb_ci or "",
            "category": "AIOps", "contact_type": "monitoring",
            "comments": f"Auto-created by CloudOps-AI for {t.incident_id}",
        })["result"]
        t.number, t.sys_id = data["number"], data["sys_id"]
        t.state = "New"
        return t

    def _send_event(self, t: Ticket) -> Ticket:
        """ITOM Event Management path: push a raw event; SN event rules take over."""
        data = self._request("POST", "/api/now/table/em_event", json={
            "source": "CloudOps-AI",
            "node": t.cmdb_ci or "",
            "type": "AIOpsIncident",
            "severity": {"1": "1", "2": "3", "3": "4"}.get(t.impact, "3"),
            "description": t.short_description,
            "additional_info": t.description[:4000],
            "message_key": t.incident_id,          # dedup key on SN side
        })["result"]
        t.sys_id = data["sys_id"]
        t.number = f"EM:{data['sys_id'][:8]}"
        t.state = "Event Sent"
        return t

    def resolve_incident(self, t: Ticket, notes: str) -> Ticket:
        if self.use_event_api:
            # send clearing event; SN correlation closes the alert/incident
            self._request("POST", "/api/now/table/em_event", json={
                "source": "CloudOps-AI", "message_key": t.incident_id,
                "severity": "0", "description": f"CLEAR: {t.short_description}",
            })
            t.state, t.close_notes = "Cleared", notes
            t.resolved_at = time.time()
            return t
        self._request("PATCH", f"/api/now/table/incident/{t.sys_id}", json={
            "state": "6", "close_code": "Solved (Permanently)",
            "close_notes": notes, "resolution_code": "Solved (Permanently)",
        })
        t.state, t.close_notes = "Resolved", notes
        t.resolved_at = time.time()
        return t

    def fetch_lifecycle(self, t: Ticket) -> dict | None:
        """Pull real opened_at/resolved_at back from ServiceNow for KPI truth."""
        if not t.sys_id or self.use_event_api:
            return None
        data = self._request(
            "GET", f"/api/now/table/incident/{t.sys_id}"
                   "?sysparm_fields=opened_at,resolved_at,state,number")["result"]
        out = {}
        for field in ("opened_at", "resolved_at"):
            raw = data.get(field) or ""
            if raw:
                dt = datetime.strptime(raw, SN_DT_FMT).replace(tzinfo=timezone.utc)
                out[field] = dt.timestamp()
        out["state"] = data.get("state")
        return out or None

    # ---------------- CMDB (idempotent upsert) ----------------
    def sync_cmdb(self, topology: TopologyMap) -> int:
        count = 0
        for ci in topology.cis.values():
            table = _sn_class(ci.ci_type)
            existing = self._request(
                "GET", f"/api/now/table/{table}"
                       f"?sysparm_query=name={ci.name}&sysparm_fields=sys_id&sysparm_limit=1"
            )["result"]
            payload = {"name": ci.name, "short_description":
                       f"CloudOps-AI | layer={ci.layer} | service={ci.business_service}"}
            if existing:
                self._request("PATCH",
                              f"/api/now/table/{table}/{existing[0]['sys_id']}",
                              json=payload)
            else:
                self._request("POST", f"/api/now/table/{table}", json=payload)
            count += 1
        return count

    # ---------------- diagnostics ----------------
    def test_connection(self) -> dict:
        info = {"instance": self.instance,
                "auth": "oauth2" if self._oauth else "basic",
                "event_api": self.use_event_api}
        data = self._request(
            "GET", "/api/now/table/sys_user?sysparm_limit=1&sysparm_fields=user_name")
        info["reachable"] = True
        info["sample_user_visible"] = bool(data.get("result"))
        return info
