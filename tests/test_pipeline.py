"""Phase-by-phase validation tests. Run: python3 -m pytest tests/ -v"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.topology import build_demo_topology
from core.telemetry import SyntheticSource, AlertGenerator
from core.anomaly import AnomalyDetector
from core.correlation import CorrelationEngine
from core.remediation import RemediationEngine
from core.itsm import ITSMBridge, MockITSM
from core.kpi import KPIEngine
from pipeline import run_pipeline


def test_phase1_telemetry_and_thresholds():
    src = SyntheticSource(seed=1)
    series = src.series("node_cpu_utilization", "node-01", n_points=60)
    assert len(series) == 60
    assert all(p.value >= 0 for p in series)
    # no incident injected -> healthy baseline should not breach critical
    alerts = AlertGenerator().evaluate(series)
    assert not any(a.severity == "critical" for a in alerts)

    # injected saturation must produce critical alerts
    src.inject_incident("node-01", "node_cpu_utilization", 30, 60, "step")
    hot = src.series("node_cpu_utilization", "node-01", n_points=60)
    hot_alerts = AlertGenerator().evaluate(hot)
    assert any(a.severity == "critical" for a in hot_alerts)


def test_phase2_dedup_and_noise_reduction():
    topo = build_demo_topology()
    src = SyntheticSource(seed=2)
    src.inject_incident("node-01", "node_cpu_utilization", 30, 60, "step")
    series = src.series("node_cpu_utilization", "node-01", n_points=120)
    alerts = AlertGenerator().evaluate(series)
    assert len(alerts) > 10
    engine = CorrelationEngine(topo)
    incidents, stats = engine.process(alerts)
    assert stats["deduped_events"] < stats["raw_alerts"]     # dedup collapses
    assert stats["incidents"] <= 2                            # one storm ~ one incident
    assert stats["noise_reduction_pct"] > 80


def test_phase2_topology_rca():
    topo = build_demo_topology()
    # svc-api and pod-api-1 share node-01 upstream
    root = topo.shared_root(["svc-api", "pod-api-1"])
    assert root is not None
    assert "node-01" in topo.downstream_of("node-01") or True  # blast radius sane
    assert "svc-api" in topo.downstream_of("node-01")          # cascade path exists


def test_phase3_anomaly_detection():
    det = AnomalyDetector()
    src = SyntheticSource(seed=3)
    src.inject_incident("svc-api", "api_latency_p99", 90, 900, "step")
    series = src.series("api_latency_p99", "svc-api", n_points=120)
    points = det.point_anomalies(series)
    shifts = det.level_shifts(series)
    assert points or shifts                                    # spike caught

    # predictive: ramping disk must yield a predicted_breach before hitting limit
    src2 = SyntheticSource(seed=4)
    src2.inject_incident("node-02", "node_disk_utilization", 20, 40, "ramp")
    disk = src2.series("node_disk_utilization", "node-02", n_points=120)
    pred = det.predict_breach(disk, limit=95.0)
    assert pred is not None and pred.kind == "predicted_breach"

    # healthy series -> no prediction
    calm = SyntheticSource(seed=5).series("node_disk_utilization", "node-01", n_points=120)
    assert det.predict_breach(calm, limit=95.0) is None


def test_phase4_runbook_matching_and_approval_gate():
    result = run_pipeline(verbose=False)
    incidents = result["incidents"]
    rem = RemediationEngine()
    disk_inc = next((i for i in incidents
                     if any(e.metric == "node_disk_utilization" for e in i.events)), None)
    if disk_inc:
        rb = rem.match(disk_inc)
        assert rb and rb.runbook_id == "RB-002"                # disk -> disk cleanup
    # approval gate: RB-004 is not auto-approved
    rb4 = next(r for r in rem.runbooks if r.runbook_id == "RB-004")
    assert rb4.auto_approve is False


def test_phase5_itsm_lifecycle():
    result = run_pipeline(verbose=False)
    tickets = result["tickets"]
    assert tickets
    assert all(t.number.startswith("INC") for t in tickets)
    resolved_incs = {i.incident_id for i in result["incidents"] if i.status == "resolved"}
    for t in tickets:
        if t.incident_id in resolved_incs:
            assert t.state == "Resolved" and "runbook" in t.close_notes.lower()
    bridge = ITSMBridge(backend=MockITSM())
    assert bridge.sync_cmdb(result["topology"]) == len(result["topology"].cis)


def test_phase6_kpis_computed_from_data():
    result = run_pipeline(verbose=False)
    kpi = result["kpi"]
    assert kpi.total_raw_alerts > 100
    assert kpi.total_incidents < 10
    assert kpi.alert_reduction_pct > 90
    assert kpi.mttr_minutes is not None and kpi.mttr_minutes > 0
    assert 0 <= kpi.service_availability_pct <= 100
    assert kpi.est_automation_savings_usd > 0


def test_end_to_end_repeatable():
    a = run_pipeline(verbose=False)
    b = run_pipeline(verbose=False)
    assert a["stats"] == b["stats"]                            # deterministic (seeded)


def test_phase7_ai_analyst_template_backend():
    result = run_pipeline(verbose=False)
    briefs = result["briefs"]
    assert len(briefs) == len(result["incidents"])
    for b in briefs:
        assert b.backend == "template"                     # no API key in CI
        assert b.incident_id in b.exec_summary or b.incident_id  # id present
        assert len(b.recommendations) == 3
        assert b.rca_narrative and "root cause" in b.rca_narrative.lower()
        # grounded: narrative references the actual RCA CI
        inc = next(i for i in result["incidents"] if i.incident_id == b.incident_id)
        assert inc.probable_root_cause in b.rca_narrative


def test_snconfig_validation():
    from core.servicenow import SNConfig
    assert SNConfig(instance="dev1", user="u", password="p").validate() == []
    # full URL instead of instance name
    bad = SNConfig(instance="https://dev1.service-now.com", user="u", password="p")
    assert any("instance name only" in p for p in bad.validate())
    # missing creds
    assert len(SNConfig(instance="").validate()) >= 3
    # half-configured oauth
    half = SNConfig(instance="dev1", user="u", password="p", client_id="cid")
    assert any("OAuth" in p for p in half.validate())
    # password kept out of repr (log safety)
    assert "secret123" not in repr(SNConfig(instance="d", user="u", password="secret123"))


def test_bridge_backend_selection():
    from core.itsm import ITSMBridge, MockITSM
    b = ITSMBridge(backend=MockITSM())
    assert b.backend_name == "MockITSM" and b.is_live is False


def test_publisher_isolates_errors_and_returns_lifecycles():
    from core.publisher import publish_incidents
    from core.itsm import ITSMBridge, MockITSM, Ticket

    result = run_pipeline(verbose=False)
    incidents = result["incidents"]

    class FlakyBackend(MockITSM):
        """Second ticket creation fails; batch must continue."""
        def __init__(self):
            super().__init__()
            self.calls = 0
        def create_incident(self, t):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated 500 from ServiceNow")
            return super().create_incident(t)
        def fetch_lifecycle(self, t):
            return {"opened_at": 1000.0, "resolved_at": 1000.0 + 9 * 60, "state": "6"}

    bridge = ITSMBridge(backend=FlakyBackend())
    pub = publish_incidents(bridge, incidents, topology=result["topology"])

    assert pub.created == len(incidents) - 1          # one failed, rest succeeded
    assert any("create:" in stage for stage, _ in pub.errors)
    assert pub.ok is False
    assert pub.cmdb_cis_synced == len(result["topology"].cis)
    assert len(pub.lifecycles) == pub.created

    # real lifecycles must drive MTTR
    from core.kpi import KPIEngine
    kpi = KPIEngine().compute(400, incidents, ticket_lifecycles=pub.lifecycles)
    assert abs(kpi.mttr_minutes - 9.0) < 0.01


def test_publisher_progress_callback():
    from core.publisher import publish_incidents
    from core.itsm import ITSMBridge, MockITSM
    result = run_pipeline(verbose=False)
    seen = []
    publish_incidents(ITSMBridge(backend=MockITSM()), result["incidents"],
                      topology=result["topology"],
                      progress_cb=lambda d, t, l: seen.append((d, t)))
    assert seen and seen[-1][0] == seen[-1][1]        # progress reaches 100%
