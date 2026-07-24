"""
ServiceNow Data Source — the INBOUND half of the integration.

Makes the platform bidirectional:
    OUTBOUND (publisher.py):  our incidents  -> ServiceNow tickets
    INBOUND  (this module):   ServiceNow CMDB + alerts -> our engines

What can honestly be sourced from ServiceNow:
  * Topology / CMDB       -> cmdb_ci_* tables + cmdb_rel_ci relationships
  * Events / alerts       -> em_alert (ITOM) or the incident table as fallback
What CANNOT:
  * Raw metric time-series. ServiceNow is a system of record, not a TSDB.
    Metric charts require Prometheus/Datadog/etc. In live mode the telemetry
    and anomaly-detection stages are therefore skipped, not faked.
"""

from datetime import datetime, timezone

from .telemetry import RawAlert
from .topology import ConfigurationItem, TopologyMap

SN_DT_FMT = "%Y-%m-%d %H:%M:%S"

# ServiceNow class -> our internal CI type
CLASS_TO_TYPE = {
    "cmdb_ci_server": "node",
    "cmdb_ci_kubernetes_pod": "pod",
    "cmdb_ci_service": "service",
    "cmdb_ci_database": "database",
    "cmdb_ci_lb": "loadbalancer",
    "cmdb_ci_netgear": "network",
    "cmdb_ci_win_server": "node",
    "cmdb_ci_linux_server": "node",
    "cmdb_ci_app_server": "service",
}
TYPE_TO_LAYER = {
    "node": "infrastructure", "pod": "platform", "service": "application",
    "database": "data", "loadbalancer": "network", "network": "network",
}
# ServiceNow severity (1 critical .. 5 ok) / priority (1 highest) -> ours
SN_SEVERITY = {"1": "critical", "2": "critical", "3": "warning",
               "4": "info", "5": "info", "0": "info"}


def _ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, SN_DT_FMT).replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


class ServiceNowDataSource:
    """Reads real CMDB and alert data from a ServiceNow instance."""

    def __init__(self, connector):
        """connector: an EnterpriseServiceNowConnector (already configured)."""
        self.sn = connector

    # ---------------- CMDB -> topology ----------------
    def fetch_topology(self, limit_per_class: int = 100,
                       classes: list[str] | None = None) -> TopologyMap:
        topo = TopologyMap()
        sys_id_to_ci: dict[str, str] = {}

        for table in (classes or list(CLASS_TO_TYPE)):
            try:
                rows = self.sn._request(
                    "GET", f"/api/now/table/{table}"
                           f"?sysparm_limit={limit_per_class}"
                           f"&sysparm_fields=sys_id,name,short_description"
                           f"&sysparm_query=operational_status=1"
                )["result"]
            except Exception:
                continue                      # class not present on this instance
            ci_type = CLASS_TO_TYPE[table]
            for r in rows:
                if not r.get("name"):
                    continue
                ci_id = r["name"]
                sys_id_to_ci[r["sys_id"]] = ci_id
                topo.add(ConfigurationItem(
                    ci_id=ci_id, name=r["name"], ci_type=ci_type,
                    layer=TYPE_TO_LAYER.get(ci_type, "infrastructure"),
                    business_service=(r.get("short_description") or "").strip()
                                     or "Unmapped Service",
                ))

        # relationships -> depends_on edges
        try:
            rels = self.sn._request(
                "GET", "/api/now/table/cmdb_rel_ci?sysparm_limit=500"
                       "&sysparm_fields=parent,child")["result"]
            for rel in rels:
                parent = (rel.get("parent") or {}).get("value") if isinstance(
                    rel.get("parent"), dict) else rel.get("parent")
                child = (rel.get("child") or {}).get("value") if isinstance(
                    rel.get("child"), dict) else rel.get("child")
                p_ci, c_ci = sys_id_to_ci.get(parent), sys_id_to_ci.get(child)
                # in SN, child depends on parent
                if p_ci and c_ci and c_ci in topo.cis and p_ci != c_ci:
                    if p_ci not in topo.cis[c_ci].depends_on:
                        topo.cis[c_ci].depends_on.append(p_ci)
        except Exception:
            pass                              # relationships optional

        return topo

    # ---------------- alerts ----------------
    def fetch_alerts(self, limit: int = 300,
                     prefer_event_api: bool = True) -> tuple[list[RawAlert], str]:
        """Returns (alerts, source_table). Tries em_alert, falls back to incident."""
        if prefer_event_api:
            alerts = self._from_em_alert(limit)
            if alerts:
                return alerts, "em_alert"
        return self._from_incidents(limit), "incident"

    def _from_em_alert(self, limit: int) -> list[RawAlert]:
        try:
            rows = self.sn._request(
                "GET", f"/api/now/table/em_alert?sysparm_limit={limit}"
                       f"&sysparm_query=state!=Closed^ORDERBYDESCsys_created_on"
                       f"&sysparm_fields=number,node,type,severity,description,"
                       f"metric_name,sys_created_on,source")["result"]
        except Exception:
            return []
        out = []
        for i, r in enumerate(rows):
            node = r.get("node") or "unknown-ci"
            out.append(RawAlert(
                alert_id=r.get("number") or f"EM-{i:05d}",
                ts=_ts(r.get("sys_created_on")) or 0.0,
                ci_id=node,
                metric=r.get("metric_name") or r.get("type") or "event",
                severity=SN_SEVERITY.get(str(r.get("severity")), "warning"),
                message=(r.get("description") or "ServiceNow alert")[:300],
                value=0.0,
                labels={"source": f"servicenow:{r.get('source') or 'em_alert'}"},
            ))
        return out

    def _from_incidents(self, limit: int) -> list[RawAlert]:
        rows = self.sn._request(
            "GET", f"/api/now/table/incident?sysparm_limit={limit}"
                   f"&sysparm_query=active=true^ORDERBYDESCopened_at"
                   f"&sysparm_fields=number,short_description,priority,"
                   f"cmdb_ci,category,opened_at&sysparm_display_value=true")["result"]
        out = []
        for i, r in enumerate(rows):
            ci = r.get("cmdb_ci")
            ci_id = (ci.get("display_value") if isinstance(ci, dict) else ci) or "unassigned-ci"
            out.append(RawAlert(
                alert_id=r.get("number") or f"INC-{i:05d}",
                ts=_ts(r.get("opened_at")) or 0.0,
                ci_id=ci_id,
                metric=(r.get("category") or "incident"),
                severity=SN_SEVERITY.get(str(r.get("priority", "3"))[:1], "warning"),
                message=(r.get("short_description") or "ServiceNow incident")[:300],
                value=0.0,
                labels={"source": "servicenow:incident"},
            ))
        return out

    # ---------------- summary for the UI ----------------
    def inventory(self) -> dict:
        topo = self.fetch_topology()
        alerts, table = self.fetch_alerts()
        return {"cis": len(topo.cis), "alerts": len(alerts),
                "alert_source": table,
                "with_dependencies": sum(1 for c in topo.cis.values() if c.depends_on)}
