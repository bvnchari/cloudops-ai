"""
Publisher — drives incident -> ServiceNow publication as an explicit action.

Separated from the pipeline so the UI can:
  * run the AIOps pipeline against mock ITSM (safe default), and
  * publish to a real instance only when the user clicks Publish.

Per-item error isolation: one failing ticket never aborts the batch, and the
result object reports exactly what succeeded, what failed, and why.
"""

import time
from dataclasses import dataclass, field

from .correlation import Incident
from .itsm import ITSMBridge
from .topology import TopologyMap


@dataclass
class PublishResult:
    backend: str
    started_ts: float
    finished_ts: float
    cmdb_cis_synced: int = 0
    tickets: list = field(default_factory=list)      # Ticket objects
    lifecycles: list = field(default_factory=list)   # real opened_at/resolved_at
    errors: list = field(default_factory=list)       # (stage, detail)

    @property
    def duration_s(self) -> float:
        return round(self.finished_ts - self.started_ts, 1)

    @property
    def created(self) -> int:
        return len(self.tickets)

    @property
    def auto_closed(self) -> int:
        return sum(1 for t in self.tickets if t.state in ("Resolved", "Cleared"))

    @property
    def ok(self) -> bool:
        return bool(self.tickets) and not self.errors


def publish_incidents(bridge: ITSMBridge,
                      incidents: list[Incident],
                      topology: TopologyMap | None = None,
                      sync_cmdb: bool = True,
                      close_resolved: bool = True,
                      fetch_lifecycle: bool = True,
                      progress_cb=None) -> PublishResult:
    """
    Publish correlated incidents to the configured ITSM backend.

    progress_cb(done, total, label) is called as work proceeds so the UI can
    render a progress bar.
    """
    result = PublishResult(backend=bridge.backend_name,
                           started_ts=time.time(), finished_ts=time.time())
    total = len(incidents) + (1 if sync_cmdb and topology else 0)
    done = 0

    # --- CMDB sync (idempotent upsert) ---
    if sync_cmdb and topology is not None:
        try:
            result.cmdb_cis_synced = bridge.sync_cmdb(topology)
        except Exception as e:
            result.errors.append(("cmdb_sync", str(e)[:300]))
        done += 1
        if progress_cb:
            progress_cb(done, total, "CMDB sync")

    # --- incidents -> tickets ---
    for inc in incidents:
        try:
            ticket = bridge.open_ticket(inc)
            if close_resolved:
                try:
                    ticket = bridge.close_if_remediated(inc, ticket)
                except Exception as e:
                    result.errors.append((f"close:{inc.incident_id}", str(e)[:300]))
            result.tickets.append(ticket)

            if fetch_lifecycle and hasattr(bridge.backend, "fetch_lifecycle"):
                try:
                    lc = bridge.backend.fetch_lifecycle(ticket)
                    if lc:
                        result.lifecycles.append(lc)
                except Exception as e:
                    result.errors.append((f"lifecycle:{inc.incident_id}", str(e)[:300]))
        except Exception as e:
            result.errors.append((f"create:{inc.incident_id}", str(e)[:300]))
        done += 1
        if progress_cb:
            progress_cb(done, total, inc.incident_id)

    result.finished_ts = time.time()
    return result
