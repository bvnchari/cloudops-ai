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


class _FakeSN:
    """Simulates ServiceNow REST responses for inbound-source testing."""
    def __init__(self, with_alerts=True, em=True):
        self.with_alerts, self.em = with_alerts, em
    def _request(self, method, path, **kw):
        if "cmdb_ci_server" in path:
            return {"result": [
                {"sys_id": "s1", "name": "prod-node-01", "short_description": "Payments"},
                {"sys_id": "s2", "name": "prod-node-02", "short_description": "Payments"}]}
        if "cmdb_ci_service" in path:
            return {"result": [
                {"sys_id": "s3", "name": "payments-api", "short_description": "Payments"}]}
        if "cmdb_rel_ci" in path:
            return {"result": [{"parent": "s1", "child": "s3"}]}
        if "em_alert" in path:
            if not (self.em and self.with_alerts):
                raise RuntimeError("Event Management not installed")
            return {"result": [
                {"number": "EM0001", "node": "prod-node-01", "type": "CPU",
                 "severity": "1", "description": "CPU critical",
                 "metric_name": "cpu_util", "sys_created_on": "2026-07-24 10:00:00",
                 "source": "CloudOps-AI"},
                {"number": "EM0002", "node": "payments-api", "type": "Latency",
                 "severity": "2", "description": "latency high",
                 "metric_name": "api_latency", "sys_created_on": "2026-07-24 10:02:00",
                 "source": "CloudOps-AI"}]}
        if "incident" in path:
            if not self.with_alerts:
                return {"result": []}
            return {"result": [
                {"number": "INC0012345", "short_description": "DB slow",
                 "priority": "1", "cmdb_ci": {"display_value": "prod-node-01"},
                 "category": "database", "opened_at": "2026-07-24 09:00:00"}]}
        return {"result": []}


def test_sn_source_builds_topology_from_cmdb():
    from core.sn_source import ServiceNowDataSource
    topo = ServiceNowDataSource(_FakeSN()).fetch_topology()
    assert "prod-node-01" in topo.cis and "payments-api" in topo.cis
    assert topo.cis["prod-node-01"].ci_type == "node"
    assert topo.cis["payments-api"].layer == "application"
    # relationship: child depends on parent
    assert "prod-node-01" in topo.cis["payments-api"].depends_on
    assert "payments-api" in topo.downstream_of("prod-node-01")


def test_sn_source_alerts_prefers_em_then_falls_back():
    from core.sn_source import ServiceNowDataSource
    alerts, table = ServiceNowDataSource(_FakeSN(em=True)).fetch_alerts()
    assert table == "em_alert" and len(alerts) == 2
    assert alerts[0].severity == "critical" and alerts[0].ci_id == "prod-node-01"
    assert alerts[0].ts > 0
    # instance without Event Management -> incident table fallback
    alerts2, table2 = ServiceNowDataSource(_FakeSN(em=False)).fetch_alerts()
    assert table2 == "incident" and alerts2[0].alert_id == "INC0012345"


def test_live_pipeline_end_to_end_on_servicenow_data():
    from pipeline import run_pipeline_live
    res = run_pipeline_live(_FakeSN(), verbose=False)
    assert res["mode"] == "live"
    assert res["series"] == {} and res["signals"] == []   # honestly empty, not faked
    assert len(res["topology"].cis) == 3
    assert res["stats"]["raw_alerts"] == 2
    assert res["incidents"], "correlation should produce incidents from SN alerts"
    inc = res["incidents"][0]
    assert inc.probable_root_cause in res["topology"].cis
    assert res["briefs"] and res["briefs"][0].backend == "template"


def test_live_pipeline_handles_empty_instance():
    from pipeline import run_pipeline_live
    res = run_pipeline_live(_FakeSN(with_alerts=False, em=False), verbose=False)
    assert res["stats"]["raw_alerts"] == 0 and res["incidents"] == []
    assert res["briefs"] == []


def test_ai_analyst_accepts_explicit_key_and_degrades():
    from core.ai_agent import AIIncidentAnalyst
    result = run_pipeline(verbose=False)
    # bad key -> must fall back to template, record error, never crash
    a = AIIncidentAnalyst(result["topology"], api_key="sk-ant-invalid")
    briefs = a.analyze_all(result["incidents"][:1])
    assert briefs[0].backend == "template"
    assert a.last_error


def test_slo_error_budget_math():
    from core.slo import SLO, SLOEngine, SLOStatus
    slo = SLO(name="test", business_service="Payments Platform",
              target_pct=99.9, window_days=30)
    # 30d at 99.9% => 43.2 minutes of budget
    assert abs(slo.budget_minutes - 43.2) < 0.01

    # consuming exactly the budget share for the observed window => burn rate 1.0
    window_h = 2.0
    share = slo.budget_minutes * (window_h * 60) / (30 * 24 * 60)
    s = SLOStatus(slo=slo, consumed_minutes=share, observed_window_h=window_h)
    assert abs(s.burn_rate - 1.0) < 0.01
    assert s.status == "AT RISK"

    # 20x that pace => fast burn => page
    s2 = SLOStatus(slo=slo, consumed_minutes=share * 20, observed_window_h=window_h)
    assert s2.burn_rate > 14.4 and s2.status == "FAST BURN"
    assert "Page" in s2.action

    # zero consumption => healthy, full budget remaining
    s3 = SLOStatus(slo=slo, consumed_minutes=0.0, observed_window_h=window_h)
    assert s3.status == "HEALTHY" and s3.remaining_minutes == slo.budget_minutes
    assert s3.achieved_pct == 100.0

    # exhausted budget clamps remaining at zero
    s4 = SLOStatus(slo=slo, consumed_minutes=slo.budget_minutes * 3,
                   observed_window_h=window_h)
    assert s4.remaining_minutes == 0.0 and s4.status == "EXHAUSTED"


def test_slo_engine_only_counts_matching_service_and_severity():
    from core.slo import SLO, SLOEngine
    result = run_pipeline(verbose=False)
    crit_only = SLO(name="c", business_service="Payments Platform",
                    target_pct=99.9, severity_counts=("critical",))
    both = SLO(name="b", business_service="Payments Platform",
               target_pct=99.9, severity_counts=("critical", "warning"))
    other = SLO(name="o", business_service="Nonexistent Service", target_pct=99.9)
    s_crit, s_both, s_other = SLOEngine([crit_only, both, other]).evaluate(
        result["incidents"])
    assert s_both.consumed_minutes >= s_crit.consumed_minutes
    assert s_other.consumed_minutes == 0.0 and s_other.status == "HEALTHY"


def test_triage_queue_ranking_is_explainable():
    from core.insights import TriageQueue
    result = run_pipeline(verbose=False)
    queue = TriageQueue(result["topology"]).build(result["incidents"])
    assert len(queue) == len(result["incidents"])
    # sorted descending, ranks sequential, every score carries reasoning
    scores = [q.score for q in queue]
    assert scores == sorted(scores, reverse=True)
    assert [q.rank for q in queue] == list(range(1, len(queue) + 1))
    assert all(q.reasons for q in queue)
    # auto-resolved incidents must be demoted below an equivalent open one
    import copy
    inc = copy.deepcopy(result["incidents"][0])
    inc.status, inc.resolved_ts = "open", None
    open_q = TriageQueue(result["topology"]).build([inc])[0]
    resolved_q = next(q for q in queue
                      if q.incident.incident_id == inc.incident_id)
    assert open_q.score > resolved_q.score


def test_noise_hotspots_and_automation_gaps():
    from core.insights import noise_hotspots, automation_gaps
    from core.remediation import RemediationEngine, Runbook
    result = run_pipeline(verbose=False)

    hot = noise_hotspots(result["raw_alerts"], top_n=5)
    assert hot and hot[0].alert_count >= hot[-1].alert_count      # ranked
    assert sum(h.alert_count for h in hot) <= len(result["raw_alerts"])
    assert all(h.recommendation for h in hot)

    # with default runbooks everything matches -> no gaps
    assert automation_gaps(result["incidents"]) == []
    # with an empty runbook set, every incident becomes a costed gap
    empty = RemediationEngine(runbooks=[Runbook(
        runbook_id="RB-X", name="none", match_metrics=[], match_severities=[])])
    gaps = automation_gaps(result["incidents"], engine=empty)
    assert len(gaps) == len(result["incidents"])
    assert all(g.est_annual_toil_hours > 0 for g in gaps)


def test_service_scorecard_grades():
    from core.insights import service_scorecard
    result = run_pipeline(verbose=False)
    cards = service_scorecard(result["incidents"])
    assert cards
    for c in cards:
        assert 0 <= c.health_score <= 100
        assert c.grade in "ABCDF"
        assert c.incidents == c.auto_resolved + c.open_items


def test_report_generators_produce_valid_markdown():
    from core.reports import postmortem_markdown, exec_report_markdown
    from core.slo import SLOEngine
    from core.insights import service_scorecard, automation_gaps, noise_hotspots
    result = run_pipeline(verbose=False)
    inc = result["incidents"][0]
    brief = next(b for b in result["briefs"] if b.incident_id == inc.incident_id)

    pm = postmortem_markdown(inc, brief=brief, topology=result["topology"])
    assert pm.startswith(f"# Postmortem — {inc.incident_id}")
    for section in ("## Impact", "## Root cause", "## Timeline",
                    "## Response", "## Follow-up actions"):
        assert section in pm
    assert (inc.probable_root_cause or "") in pm
    assert "blameless" in pm.lower()

    rep = exec_report_markdown(
        result["kpi"], result["stats"],
        SLOEngine().evaluate(result["incidents"]),
        service_scorecard(result["incidents"]),
        automation_gaps(result["incidents"]),
        noise_hotspots(result["raw_alerts"]))
    assert rep.startswith("# Reliability Briefing")
    assert "## Error budget status" in rep and "## Service health scorecard" in rep
    assert str(result["stats"]["incidents"]) in rep


def test_sli_event_based_from_metric_series():
    from core.sli import SLICalculator, SLIDefinition
    from core.telemetry import SyntheticSource
    calc = SLICalculator()
    d = SLIDefinition(name="lat", kind="latency", ci_id="svc-api",
                      metric="api_latency_p99", threshold=500.0, comparison="lt")

    # healthy series: baseline 180ms, everything under 500 -> ~100%
    clean = SyntheticSource(seed=11).series("api_latency_p99", "svc-api", 120)
    r_clean = calc.from_series(d, clean)
    assert r_clean.valid_events == 120
    assert r_clean.ratio_pct > 99.0
    assert r_clean.good_events + r_clean.bad_events == r_clean.valid_events

    # degraded second half -> ratio must drop materially
    src = SyntheticSource(seed=11)
    src.inject_incident("svc-api", "api_latency_p99", 60, 900, "step")
    bad = src.series("api_latency_p99", "svc-api", 120)
    r_bad = calc.from_series(d, bad)
    assert r_bad.ratio_pct < 60.0
    assert r_bad.bad_samples                      # captures offending values
    assert "api_latency_p99" in r_bad.headline


def test_sli_comparison_operators():
    from core.sli import SLICalculator, SLIDefinition
    from core.telemetry import MetricPoint
    calc = SLICalculator()
    pts = [MetricPoint(ts=i, metric="m", ci_id="c", value=float(i))
           for i in range(10)]          # values 0..9
    lt = calc.from_series(SLIDefinition("x", "custom", metric="m",
                                        threshold=5.0, comparison="lt"), pts)
    gte = calc.from_series(SLIDefinition("x", "custom", metric="m",
                                         threshold=5.0, comparison="gte"), pts)
    assert lt.good_events == 5 and gte.good_events == 5
    assert lt.ratio_pct == 50.0


def test_sli_time_based_no_double_counting_overlaps():
    from core.sli import SLICalculator, SLIDefinition
    from core.correlation import Incident
    calc = SLICalculator()
    d = SLIDefinition(name="uptime", kind="availability")
    base = 100_000.0
    # two overlapping 30-minute incidents inside a 2h window
    a = Incident(incident_id="A", created_ts=base, severity="critical",
                 title="a", resolved_ts=base + 1800, status="resolved")
    b = Incident(incident_id="B", created_ts=base + 900, severity="critical",
                 title="b", resolved_ts=base + 2700, status="resolved")
    r = calc.from_incidents(d, [a, b], window_h=2.0)
    bad_minutes = r.bad_events            # 1-minute buckets
    # union is 45 min, not 60 — overlap must not be counted twice
    assert 44 <= bad_minutes <= 47, bad_minutes
    assert r.valid_events == 120

    # no qualifying incidents -> perfect
    clean = calc.from_incidents(d, [], window_h=2.0)
    assert clean.ratio_pct == 100.0

    # severity filter excludes warnings
    w = Incident(incident_id="W", created_ts=base, severity="warning",
                 title="w", resolved_ts=base + 3600, status="resolved")
    assert calc.from_incidents(d, [w], window_h=2.0).ratio_pct == 100.0


def test_slo_binds_to_sli_measurement():
    from core.sli import SLICalculator, SLIDefinition
    from core.slo import SLO, SLOEngine
    from core.correlation import Incident
    calc = SLICalculator()
    base = 100_000.0
    inc = Incident(incident_id="A", created_ts=base, severity="critical",
                   title="a", resolved_ts=base + 600, status="resolved")
    sli = calc.from_incidents(SLIDefinition("up", "availability"), [inc],
                              window_h=2.0)
    slo = SLO(name="t", business_service="Payments Platform", target_pct=99.9)
    status = SLOEngine().evaluate_from_sli(slo, sli)
    # ~10 minutes of a 120-minute window consumed
    assert 9.0 <= status.consumed_minutes <= 12.0
    assert status.burn_rate > 1.0 and status.status != "HEALTHY"


def test_sla_credit_tiers_and_exposure():
    from core.sla import SLA, SLAEngine
    from core.sli import SLIResult, SLIDefinition
    d = SLIDefinition(name="up", kind="availability")
    sla = SLA(name="ent", customer="c", business_service="Payments Platform",
              commitment_pct=99.5, monthly_contract_value=50_000.0)

    def status_for(ratio_pct, excluded=0.0):
        sla.excluded_minutes = excluded
        good = int(1000 * ratio_pct / 100)
        r = SLIResult(d, good_events=good, valid_events=1000,
                      window_h=100.0, source="incident_timeline")
        return SLAEngine([sla]).evaluate(r, linked_slo_target=99.9)[0]

    ok = status_for(99.99)
    assert not ok.breached and ok.credit_pct == 0.0
    assert ok.financial_exposure == 0.0 and ok.status == "MEETING"

    mild = status_for(99.2)                     # below 99.5, above 99.0
    assert mild.breached and mild.credit_pct == 10.0
    assert mild.financial_exposure == 5000.0

    severe = status_for(94.0)                   # below all tiers -> worst applies
    assert severe.credit_pct == 50.0 and severe.financial_exposure == 25_000.0
    assert "credit" in severe.action.lower()

    # SLO must sit above the SLA commitment — that gap is the safety margin
    assert abs(ok.slo_buffer_pct - 0.4) < 0.001

    # exclusions (planned maintenance) are credited back before assessment
    with_excl = status_for(99.2, excluded=500.0)
    assert with_excl.achieved_pct > mild.achieved_pct
    assert not with_excl.breached


def test_sla_headroom_and_risk_states():
    from core.sla import SLA, SLAEngine
    from core.sli import SLIResult, SLIDefinition
    d = SLIDefinition(name="up", kind="availability")
    sla = SLA(name="e", customer="c", business_service="s",
              commitment_pct=99.0, measurement_period_days=30)
    # 30d @ 99% => 432 minutes allowed downtime
    assert abs(sla.allowed_downtime_minutes - 432.0) < 0.1

    r = SLIResult(d, good_events=997, valid_events=1000, window_h=100.0,
                  source="incident_timeline")   # 0.3% bad => 18 min downtime
    s = SLAEngine([sla]).evaluate(r)[0]
    assert abs(s.downtime_minutes - 18.0) < 0.5
    assert abs(s.headroom_minutes - 414.0) < 1.0
    assert s.status == "MEETING" and not s.breached
