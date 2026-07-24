"""
CloudOps-AI — end-to-end pipeline (Phases 1-6).

Run: python3 pipeline.py

Simulates a realistic incident storm on an EKS-based payments platform:
  1. Node-01 CPU saturation (step)      -> cascades to API latency + 5xx on ALB
  2. Node-02 disk filling up (ramp)     -> predictive alert before breach
  3. DB replication lag spike           -> worker service degradation
Then runs: ingestion -> threshold alerting -> anomaly detection ->
dedup/correlation/suppression -> auto-remediation -> ITSM tickets -> KPI report.
"""

from core.topology import build_demo_topology
from core.telemetry import SyntheticSource, AlertGenerator
from core.anomaly import AnomalyDetector
from core.correlation import CorrelationEngine
from core.remediation import RemediationEngine
from core.itsm import ITSMBridge
from core.kpi import KPIEngine


def run_pipeline(verbose: bool = True):
    log = print if verbose else (lambda *a, **k: None)

    # ---------- Phase 1: topology + telemetry ----------
    topo = build_demo_topology()
    src = SyntheticSource(seed=7)
    # incident scenario injection
    src.inject_incident("node-01", "node_cpu_utilization", start_idx=70, magnitude=58, shape="step")
    src.inject_incident("svc-api", "api_latency_p99", start_idx=72, magnitude=900, shape="step")
    src.inject_incident("svc-api", "api_error_rate", start_idx=73, magnitude=6.5, shape="step")
    src.inject_incident("alb-01", "alb_5xx_rate", start_idx=74, magnitude=4.0, shape="step")
    src.inject_incident("node-02", "node_disk_utilization", start_idx=40, magnitude=38, shape="ramp")
    src.inject_incident("rds-01", "db_replication_lag", start_idx=80, magnitude=25, shape="spike")

    collection = {
        ("node-01", "node_cpu_utilization"), ("node-01", "node_memory_utilization"),
        ("node-02", "node_cpu_utilization"), ("node-02", "node_disk_utilization"),
        ("svc-api", "api_latency_p99"), ("svc-api", "api_error_rate"),
        ("alb-01", "alb_5xx_rate"), ("rds-01", "db_replication_lag"),
        ("rds-01", "db_connections"), ("pod-api-1", "pod_restart_count"),
    }
    all_series = {k: src.series(metric=k[1], ci_id=k[0], n_points=120) for k in collection}
    log(f"[Phase 1] Telemetry ingested: {len(all_series)} series x 120 points "
        f"= {sum(len(s) for s in all_series.values())} datapoints across "
        f"{len(topo.cis)} CIs")

    # ---------- Phase 1b: legacy threshold alerting (the noise source) ----------
    gen = AlertGenerator()
    raw_alerts = []
    for series in all_series.values():
        raw_alerts += gen.evaluate(series)
    log(f"[Phase 1] Static-threshold alerts fired: {len(raw_alerts)} (noisy baseline)")

    # ---------- Phase 3: anomaly detection (runs alongside thresholds) ----------
    det = AnomalyDetector()
    signals = []
    for (ci, metric), series in all_series.items():
        signals += det.point_anomalies(series)
        signals += det.level_shifts(series)
        if metric == "node_disk_utilization":
            pred = det.predict_breach(series, limit=95.0)
            if pred:
                signals.append(pred)
    ml_alerts = det.to_alerts(signals)
    log(f"[Phase 3] Anomaly signals: {len(signals)} "
        f"({sum(1 for s in signals if s.kind=='predicted_breach')} predictive)")

    # ---------- Phase 2: correlation / dedup / suppression ----------
    engine = CorrelationEngine(topo)
    incidents, stats = engine.process(raw_alerts + ml_alerts)
    log(f"[Phase 2] {stats['raw_alerts']} raw alerts -> {stats['deduped_events']} events "
        f"-> {stats['incidents']} incidents  (noise reduction: {stats['noise_reduction_pct']}%)")
    for inc in incidents:
        log(f"    {inc.incident_id} [{inc.severity}] {inc.title}")
        log(f"        RCA: {inc.probable_root_cause} | service: {inc.business_service} "
            f"| raw alerts folded: {inc.raw_alert_count}")

    # ---------- Phase 4: auto-remediation ----------
    rem = RemediationEngine()
    for inc in incidents:
        rem.remediate(inc)
    resolved = [i for i in incidents if i.status == "resolved"]
    log(f"[Phase 4] Auto-remediated: {len(resolved)}/{len(incidents)} "
        f"| pending change approval: {len(rem.pending_approval)}")
    for r in rem.results:
        log(f"    {r.incident_id} -> {r.runbook_name} ({'success' if r.success else 'FAILED'})")

    # ---------- Phase 5: ITSM ----------
    bridge = ITSMBridge()   # uses ServiceNow if SN_INSTANCE set, else mock
    n_cis = bridge.sync_cmdb(topo)
    tickets = []
    for inc in incidents:
        t = bridge.open_ticket(inc)
        t = bridge.close_if_remediated(inc, t)
        tickets.append(t)
    backend = type(bridge.backend).__name__
    log(f"[Phase 5] ITSM backend: {backend} | CMDB CIs synced: {n_cis} "
        f"| tickets: {len(tickets)} "
        f"({sum(1 for t in tickets if t.state=='Resolved')} auto-closed)")
    for t in tickets:
        log(f"    {t.number} [{t.state}] {t.short_description}")

    # ---------- Phase 6: KPIs ----------
    # If a real ServiceNow backend is active, feed actual ticket lifecycle
    # timestamps into the KPI engine (production-truth MTTR).
    lifecycles = None
    if hasattr(bridge.backend, "fetch_lifecycle"):
        lifecycles = []
        for t in tickets:
            try:
                lc = bridge.backend.fetch_lifecycle(t)
                if lc:
                    lifecycles.append(lc)
            except Exception as e:            # keep KPI reporting resilient
                log(f"    [warn] lifecycle fetch failed for {t.number}: {e}")
    kpi = KPIEngine().compute(stats["raw_alerts"], incidents,
                              ticket_lifecycles=lifecycles)
    log("[Phase 6] Executive KPI report:")
    for k, v in kpi.to_dict().items():
        log(f"    {k:28s} {v}")

    # ---------- Phase 7: AI incident analyst ----------
    from core.ai_agent import AIIncidentAnalyst
    analyst = AIIncidentAnalyst(topo)
    briefs = analyst.analyze_all(incidents)
    backend = briefs[0].backend if briefs else "n/a"
    log(f"[Phase 7] AI analyst briefs generated: {len(briefs)} (backend: {backend})")
    for b in briefs:
        log(f"    {b.incident_id}: {b.exec_summary[:110]}...")

    return {
        "series": all_series, "raw_alerts": raw_alerts, "signals": signals,
        "incidents": incidents, "stats": stats, "tickets": tickets,
        "remediations": rem.results, "kpi": kpi, "topology": topo,
        "briefs": briefs,
    }


if __name__ == "__main__":
    run_pipeline()


def run_pipeline_live(connector, verbose: bool = True, api_key: str | None = None):
    """
    LIVE mode — sources topology and alerts FROM ServiceNow instead of
    synthetic telemetry, then runs the same correlation / remediation /
    AI-analysis engines on that real data.

    Honest scope: ServiceNow is a system of record, not a metrics store, so
    the telemetry ingestion (Phase 1) and anomaly detection (Phase 3) stages
    have no input in this mode and are skipped rather than faked. Everything
    downstream — correlation, RCA, remediation matching, KPIs, AI briefs —
    runs on real data.
    """
    from core.sn_source import ServiceNowDataSource
    from core.ai_agent import AIIncidentAnalyst

    log = print if verbose else (lambda *a, **k: None)
    src = ServiceNowDataSource(connector)

    topo = src.fetch_topology()
    log(f"[LIVE] CMDB topology: {len(topo.cis)} CIs "
        f"({sum(1 for c in topo.cis.values() if c.depends_on)} with dependencies)")

    alerts, table = src.fetch_alerts()
    log(f"[LIVE] Alerts pulled from `{table}`: {len(alerts)}")
    if not alerts:
        log("[LIVE] No open alerts/incidents found in the instance.")

    engine = CorrelationEngine(topo)
    incidents, stats = engine.process(alerts) if alerts else ([], {
        "raw_alerts": 0, "deduped_events": 0, "incidents": 0,
        "noise_reduction_pct": 0.0})
    log(f"[LIVE] {stats['raw_alerts']} alerts -> {stats['incidents']} incidents "
        f"({stats['noise_reduction_pct']}% noise reduction)")

    rem = RemediationEngine()
    for inc in incidents:
        rem.remediate(inc)
    log(f"[LIVE] Runbook matches: {len(rem.results)} "
        f"| pending approval: {len(rem.pending_approval)}")

    kpi = KPIEngine().compute(max(stats["raw_alerts"], 1), incidents)

    analyst = AIIncidentAnalyst(topo, api_key=api_key)
    briefs = analyst.analyze_all(incidents)

    return {
        "mode": "live", "series": {}, "raw_alerts": alerts, "signals": [],
        "incidents": incidents, "stats": stats, "tickets": [],
        "remediations": rem.results, "kpi": kpi, "topology": topo,
        "briefs": briefs, "alert_source": table,
    }
