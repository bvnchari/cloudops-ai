"""
CloudOps-AI — Executive Dashboard (Phase 6 UI).

Run locally:  streamlit run app.py
Deploy:       HuggingFace Spaces (Streamlit SDK) — same pattern as CloudBridge.
"""

import pandas as pd
import streamlit as st

from core.kpi import KPIEngine
from pipeline import run_pipeline

st.set_page_config(page_title="CloudOps-AI | Enterprise AIOps", layout="wide",
                   page_icon="🛰️")

st.title("🛰️ CloudOps-AI — Enterprise AIOps Platform")
st.caption("Ingestion → Anomaly Detection → Correlation → Auto-Remediation → ITSM → Governance")


@st.cache_data(show_spinner="Running AIOps pipeline...")
def load():
    return run_pipeline(verbose=False)


# session-scoped state (never persisted to disk, never written to the repo)
st.session_state.setdefault("sn_config", None)      # SNConfig once validated
st.session_state.setdefault("sn_status", None)      # last connection test result
st.session_state.setdefault("publish_result", None)
st.session_state.setdefault("data_source", "demo")     # demo | live
st.session_state.setdefault("anthropic_key", "")
st.session_state.setdefault("live_result", None)

if st.session_state.data_source == "live" and st.session_state.live_result:
    result = st.session_state.live_result
    st.info(f"**LIVE mode** — topology and alerts sourced from ServiceNow "
            f"(`{result.get('alert_source', 'n/a')}`). Telemetry and anomaly "
            f"detection are unavailable in this mode: ServiceNow is a system of "
            f"record, not a metrics store.")
else:
    result = load()
stats = result["stats"]
incidents = result["incidents"]

# If incidents were published to a real ServiceNow instance, recompute KPIs
# from actual ticket lifecycle timestamps instead of simulated timing.
pub = st.session_state.publish_result
if pub and pub.lifecycles:
    kpi = KPIEngine().compute(stats["raw_alerts"], incidents,
                              ticket_lifecycles=pub.lifecycles)
    kpi_source = f"live ServiceNow ({len(pub.lifecycles)} tickets)"
else:
    kpi = result["kpi"]
    kpi_source = "simulated remediation timing"

# ---------------- KPI tiles ----------------
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Raw Alerts", kpi.total_raw_alerts)
c2.metric("Incidents", kpi.total_incidents,
          delta=f"-{kpi.alert_reduction_pct}% noise", delta_color="inverse")
c3.metric("MTTR", f"{kpi.mttr_minutes:.0f} min" if kpi.mttr_minutes else "n/a")
c4.metric("Automation Rate", f"{kpi.automation_rate_pct}%")
c5.metric("Est. Savings", f"${kpi.est_automation_savings_usd/1000:.0f}K/yr")
c6.metric("Availability", f"{kpi.service_availability_pct}%")
st.caption(f"MTTR source: {kpi_source}")

st.divider()

(tab_oncall, tab_exec, tab_funnel, tab_inc, tab_metrics, tab_rem,
 tab_itsm, tab_ai, tab_cfg) = st.tabs(
    ["🎯 On-Call", "📊 Executive", "📉 Alert Funnel", "🚨 Incidents",
     "📈 Telemetry", "🔧 Remediation", "🎫 ITSM", "🤖 AI Analyst", "⚙️ Config"])

# ---------------- Funnel ----------------
with tab_funnel:
    st.subheader("Noise Reduction Funnel")
    funnel = pd.DataFrame({
        "Stage": ["Raw alerts", "Deduplicated events", "Correlated incidents"],
        "Count": [stats["raw_alerts"], stats["deduped_events"], stats["incidents"]],
    })
    st.bar_chart(funnel.set_index("Stage"), horizontal=True)
    st.success(f"**{stats['noise_reduction_pct']}% alert noise eliminated** — "
               f"{stats['raw_alerts']} raw alerts collapsed into "
               f"{stats['incidents']} actionable incidents with root-cause attribution.")

# ---------------- Incidents ----------------
with tab_inc:
    st.subheader("Correlated Incidents (with probable root cause)")
    rows = [{
        "Incident": i.incident_id, "Severity": i.severity.upper(),
        "Title": i.title, "Root Cause": i.probable_root_cause,
        "Service": i.business_service, "CIs Impacted": len(i.impacted_cis),
        "Alerts Folded": i.raw_alert_count, "Status": i.status,
        "Remediation": i.remediation or "-",
    } for i in incidents]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    for i in incidents:
        with st.expander(f"{i.incident_id} — correlated events ({len(i.events)})"):
            st.table(pd.DataFrame([{
                "Event": e.event_id, "CI": e.ci_id, "Metric": e.metric,
                "Severity": e.severity, "Occurrences": e.count,
            } for e in i.events]))

# ---------------- Telemetry ----------------
with tab_metrics:
    st.subheader("Metric Explorer")
    if not result.get("series"):
        st.warning("No metric time-series in LIVE mode. ServiceNow stores events "
                   "and CIs, not raw metrics — connect Prometheus/Datadog for this "
                   "tab, or switch back to demo mode in ⚙️ Config.")
        st.stop()
    keys = sorted(result["series"].keys())
    sel = st.selectbox("Series", [f"{ci} · {m}" for ci, m in keys])
    ci, metric = sel.split(" · ")
    pts = result["series"][(ci, metric)]
    df = pd.DataFrame({"time": pd.to_datetime([p.ts for p in pts], unit="s"),
                       "value": [p.value for p in pts]}).set_index("time")
    st.line_chart(df)
    sigs = [s for s in result["signals"] if s.ci_id == ci and s.metric == metric]
    if sigs:
        st.warning(f"{len(sigs)} anomaly signal(s) on this series — "
                   f"kinds: {sorted({s.kind for s in sigs})}")

# ---------------- Remediation ----------------
with tab_rem:
    st.subheader("Self-Healing Execution Log")
    for r in result["remediations"]:
        icon = "✅" if r.success else "❌"
        with st.expander(f"{icon} {r.incident_id} → {r.runbook_name}"):
            for line in r.log:
                st.code(line, language="bash")

# ---------------- ITSM ----------------
with tab_itsm:
    pub = st.session_state.publish_result
    if pub and pub.tickets:
        st.subheader("ServiceNow Tickets — LIVE")
        st.success(f"Published to **{pub.backend}** · {pub.created} tickets "
                   f"({pub.auto_closed} auto-closed) · {pub.cmdb_cis_synced} CMDB CIs "
                   f"synced · {pub.duration_s}s")
        tickets = pub.tickets
    else:
        st.subheader("ServiceNow Tickets (simulated)")
        st.info("Running against the in-memory mock. Configure a real instance in "
                "the **⚙️ Config** tab to publish these incidents for real.")
        tickets = result["tickets"]

    st.dataframe(pd.DataFrame([{
        "Number": t.number, "State": t.state, "Impact": t.impact,
        "Urgency": t.urgency, "Service": t.business_service,
        "CMDB CI": t.cmdb_ci, "Short Description": t.short_description,
    } for t in tickets]), use_container_width=True, hide_index=True)

    if pub and pub.errors:
        with st.expander(f"⚠️ {len(pub.errors)} error(s) during publish"):
            for stage, detail in pub.errors:
                st.error(f"**{stage}** — {detail}")

# ---------------- AI Analyst ----------------
with tab_ai:
    st.subheader("AI Incident Analyst — Executive Briefs & RCA Narratives")
    briefs = result.get("briefs", [])
    if briefs:
        st.caption(f"Narrative backend: **{briefs[0].backend}** "
                   f"({'Claude API' if briefs[0].backend == 'llm' else 'deterministic — set ANTHROPIC_API_KEY for LLM narratives'})")
    for b in briefs:
        with st.container(border=True):
            st.markdown(f"**{b.incident_id} — Executive Summary**")
            st.info(b.exec_summary)
            st.markdown("**Technical RCA narrative**")
            st.write(b.rca_narrative)
            st.markdown("**Recommended follow-ups**")
            for rec in b.recommendations:
                st.markdown(f"- {rec}")

# ---------------- Config ----------------
with tab_cfg:
    st.subheader("ServiceNow Connection")

    cfg = st.session_state.sn_config
    if cfg:
        st.success(f"Configured: **{cfg.instance}** · "
                   f"auth: {'OAuth2' if cfg.uses_oauth else 'Basic'} · "
                   f"{'Event API (em_event)' if cfg.use_event_api else 'Table API (incident)'}")
    else:
        st.warning("Not configured — the platform is running against the in-memory mock.")

    with st.form("sn_config_form"):
        col_a, col_b = st.columns(2)
        with col_a:
            instance = st.text_input(
                "Instance name", value=(cfg.instance if cfg else ""),
                placeholder="dev123456",
                help="Just the name from your instance URL — not the full https:// address.")
            user = st.text_input("Username", value=(cfg.user if cfg else ""),
                                 placeholder="cloudops.integration")
            password = st.text_input("Password", type="password",
                                     value=(cfg.password if cfg else ""))
        with col_b:
            use_event_api = st.checkbox(
                "Use ITOM Event Management API (em_event)",
                value=(cfg.use_event_api if cfg else False),
                help="Publishes events with dedup keys and lets ServiceNow's own "
                     "event rules correlate. Requires the Event Management plugin "
                     "— NOT available on free Personal Developer Instances.")
            timeout_s = st.number_input("Timeout (seconds)", 5.0, 120.0,
                                        value=(cfg.timeout_s if cfg else 15.0), step=5.0)
            max_retries = st.number_input("Max retries", 1, 6,
                                          value=(cfg.max_retries if cfg else 3))

        with st.expander("OAuth2 (optional — leave blank for basic auth)"):
            client_id = st.text_input("Client ID", value=(cfg.client_id if cfg else ""))
            client_secret = st.text_input("Client Secret", type="password",
                                          value=(cfg.client_secret if cfg else ""))

        submitted = st.form_submit_button("💾 Save & Test Connection", type="primary")

    if submitted:
        from core.servicenow import SNConfig, EnterpriseServiceNowConnector, ServiceNowError
        new_cfg = SNConfig(instance=instance.strip(), user=user.strip(),
                           password=password, client_id=client_id.strip(),
                           client_secret=client_secret, use_event_api=use_event_api,
                           timeout_s=float(timeout_s), max_retries=int(max_retries))
        problems = new_cfg.validate()
        if problems:
            for p in problems:
                st.error(p)
        else:
            with st.spinner("Testing connection..."):
                try:
                    info = EnterpriseServiceNowConnector(new_cfg).test_connection()
                    st.session_state.sn_config = new_cfg
                    st.session_state.sn_status = info
                    st.success(f"Connected to {info['instance']} "
                               f"(auth: {info['auth']}). Configuration saved for "
                               f"this session.")
                    st.rerun()
                except ServiceNowError as e:
                    st.session_state.sn_status = None
                    st.error(f"Connection failed: {e}")
                    st.caption("Common causes: instance hibernating (wake it at "
                               "developer.servicenow.com), wrong instance name, "
                               "bad credentials, or the user lacks the itil role.")
                except Exception as e:
                    st.error(f"Unexpected error: {e}")

    st.divider()
    st.subheader("Publish Incidents to ServiceNow")

    if not st.session_state.sn_config:
        st.info("Save a working connection above to enable publishing.")
    else:
        c1, c2, c3 = st.columns(3)
        do_cmdb = c1.checkbox("Sync CMDB", value=True,
                              help="Idempotent upsert of topology CIs — safe to re-run.")
        do_close = c2.checkbox("Auto-close remediated", value=True)
        do_lifecycle = c3.checkbox("Read back lifecycle for real MTTR", value=True)

        st.caption(f"Ready to publish **{len(incidents)} incident(s)** and "
                   f"**{len(result['topology'].cis)} CI(s)** to "
                   f"`{st.session_state.sn_config.instance}`.")

        if st.button("🚀 Publish to ServiceNow", type="primary"):
            from core.itsm import ITSMBridge
            from core.publisher import publish_incidents
            bar = st.progress(0.0, text="Starting...")

            def on_progress(done, total, label):
                bar.progress(done / max(total, 1), text=f"{label} ({done}/{total})")

            try:
                bridge = ITSMBridge(sn_config=st.session_state.sn_config)
                pub_result = publish_incidents(
                    bridge, incidents, topology=result["topology"],
                    sync_cmdb=do_cmdb, close_resolved=do_close,
                    fetch_lifecycle=do_lifecycle, progress_cb=on_progress)
                st.session_state.publish_result = pub_result
                bar.empty()
                if pub_result.ok:
                    st.success(f"Published {pub_result.created} tickets "
                               f"({pub_result.auto_closed} auto-closed) in "
                               f"{pub_result.duration_s}s. See the 🎫 ITSM tab.")
                else:
                    st.warning(f"Completed with issues: {pub_result.created} created, "
                               f"{len(pub_result.errors)} error(s). See the 🎫 ITSM tab.")
                st.rerun()
            except Exception as e:
                bar.empty()
                st.error(f"Publish failed: {e}")

    if st.session_state.publish_result:
        if st.button("Clear publish results"):
            st.session_state.publish_result = None
            st.rerun()

    st.divider()
    st.subheader("Data Source")
    st.caption("**Demo**: synthetic telemetry drives all engines (all 7 tabs). "
               "**Live**: topology and alerts are pulled FROM ServiceNow — "
               "correlation, RCA, remediation, KPIs and AI briefs run on real "
               "data, but metric charts and anomaly detection are unavailable "
               "(ServiceNow holds no time-series).")

    mode = st.radio("Source", ["demo", "live"],
                    index=0 if st.session_state.data_source == "demo" else 1,
                    format_func=lambda m: ("Synthetic demo (all tabs)" if m == "demo"
                                           else "ServiceNow live (real CMDB + alerts)"),
                    horizontal=True)

    if mode == "live":
        if not st.session_state.sn_config:
            st.warning("Connect to ServiceNow above before switching to live mode.")
        else:
            cols = st.columns(2)
            if cols[0].button("🔄 Pull data from ServiceNow", type="primary"):
                from core.servicenow import EnterpriseServiceNowConnector
                from pipeline import run_pipeline_live
                with st.spinner("Pulling CMDB and alerts, running engines..."):
                    try:
                        conn = EnterpriseServiceNowConnector(st.session_state.sn_config)
                        live = run_pipeline_live(
                            conn, verbose=False,
                            api_key=st.session_state.anthropic_key or None)
                        st.session_state.live_result = live
                        st.session_state.data_source = "live"
                        if not live["incidents"]:
                            st.warning(f"Pulled {len(live['topology'].cis)} CIs but "
                                       f"found no open alerts/incidents to correlate. "
                                       f"Publish some first, or create incidents in "
                                       f"the instance.")
                        else:
                            st.success(f"Live: {len(live['topology'].cis)} CIs, "
                                       f"{live['stats']['raw_alerts']} alerts -> "
                                       f"{live['stats']['incidents']} incidents.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Pull failed: {e}")
            if cols[1].button("Inspect instance inventory"):
                from core.servicenow import EnterpriseServiceNowConnector
                from core.sn_source import ServiceNowDataSource
                try:
                    inv = ServiceNowDataSource(
                        EnterpriseServiceNowConnector(st.session_state.sn_config)
                    ).inventory()
                    st.json(inv)
                except Exception as e:
                    st.error(f"Inventory failed: {e}")
    elif st.session_state.data_source != "demo":
        st.session_state.data_source = "demo"
        st.session_state.live_result = None
        st.rerun()

    st.divider()
    st.subheader("AI Incident Analyst (Phase 7)")
    briefs_now = result.get("briefs", [])
    backend_now = briefs_now[0].backend if briefs_now else "n/a"
    st.caption(f"Current narrative backend: **{backend_now}**. Without a key, "
               "briefs are generated deterministically from incident structure "
               "(no hallucination risk). With a key, Claude writes the executive "
               "summary and RCA narrative, still grounded in the same structured "
               "data.")
    key_in = st.text_input("Anthropic API key (optional)", type="password",
                           value=st.session_state.anthropic_key,
                           placeholder="sk-ant-...",
                           help="Session-scoped only — never stored or committed.")
    model_in = st.selectbox(
        "Model", ["claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"],
        index=0, help="Haiku is fastest and cheapest for short briefs.")
    kc1, kc2 = st.columns(2)
    if kc1.button("Enable LLM narratives"):
        if not key_in.strip():
            st.error("Enter a key first.")
        else:
            from core.ai_agent import AIIncidentAnalyst
            with st.spinner("Testing key and regenerating briefs..."):
                analyst = AIIncidentAnalyst(result["topology"],
                                            api_key=key_in.strip(),
                                            model=model_in)
                new_briefs = analyst.analyze_all(result["incidents"])
                if new_briefs and new_briefs[0].backend == "llm":
                    st.session_state.anthropic_key = key_in.strip()
                    result["briefs"] = new_briefs
                    st.success("LLM narratives enabled — see the 🤖 AI Analyst tab.")
                else:
                    st.error(f"Key rejected or API unreachable: "
                             f"{analyst.last_error or 'unknown error'}. "
                             f"Falling back to deterministic briefs.")
    if kc2.button("Disable / clear key"):
        st.session_state.anthropic_key = ""
        st.rerun()

    st.divider()
    with st.expander("🔒 How credentials are handled"):
        st.markdown(
            "- Credentials live **only in this browser session** — never written "
            "to disk, never committed, never shared with other visitors.\n"
            "- They are cleared when the session ends or the app restarts.\n"
            "- Use a **dedicated integration user** with the `itil` role rather "
            "than `admin`.\n"
            "- On a **public** deployment, each visitor supplies their own "
            "instance — this app ships with no credentials baked in.\n"
            "- Publishing writes **real tickets** to the instance you configure. "
            "Point it at a developer/test instance, not production.")

# ---------------- On-Call (day-to-day engineer view) ----------------
with tab_oncall:
    from core.insights import (TriageQueue, noise_hotspots, automation_gaps)
    from core.reports import postmortem_markdown

    st.subheader("Triage Queue")
    st.caption("Priority = severity × blast radius × business impact × age. "
               "Auto-remediated incidents are demoted to informational.")

    queue = TriageQueue(result["topology"]).build(incidents)
    open_items = [q for q in queue if q.needs_human]

    q1, q2, q3 = st.columns(3)
    q1.metric("Needs attention", len(open_items))
    q2.metric("Auto-handled", len(queue) - len(open_items))
    q3.metric("Highest priority", f"{queue[0].score:g}" if queue else "—")

    show_all = st.checkbox("Show auto-remediated incidents too", value=True)
    briefs_by_id = {b.incident_id: b for b in result.get("briefs", [])}
    rem_by_id = {r.incident_id: r for r in result.get("remediations", [])}

    for item in queue:
        if not show_all and not item.needs_human:
            continue
        inc = item.incident
        icon = {"critical": "🔴", "warning": "🟠"}.get(inc.severity, "🔵")
        state = "✅ auto-resolved" if inc.status == "resolved" else f"⚠️ {inc.status}"
        with st.expander(f"**#{item.rank}** {icon} `{inc.incident_id}` · "
                         f"score {item.score:g} · {state} · {inc.title}"):
            c1, c2, c3 = st.columns(3)
            c1.metric("Root cause CI", inc.probable_root_cause or "—")
            c2.metric("Blast radius", f"{item.blast_radius} CI(s)")
            c3.metric("Age", f"{item.age_minutes:.0f} min")

            st.markdown("**Why this priority:**")
            for reason in item.reasons:
                st.markdown(f"- {reason}")

            st.markdown(f"**Alerts folded in:** {inc.raw_alert_count} · "
                        f"**Remediation:** {inc.remediation or 'none matched'}")

            pm = postmortem_markdown(inc, brief=briefs_by_id.get(inc.incident_id),
                                     remediation=rem_by_id.get(inc.incident_id),
                                     topology=result["topology"])
            st.download_button("📄 Download postmortem", pm,
                               file_name=f"postmortem_{inc.incident_id}.md",
                               mime="text/markdown", key=f"pm_{inc.incident_id}")

    st.divider()
    st.subheader("Alert Tuning — Noise Hotspots")
    st.caption("The CI/metric pairs generating the most volume. Tuning these "
               "gives the biggest reduction in on-call noise.")
    hotspots = noise_hotspots(result["raw_alerts"])
    if hotspots:
        st.dataframe(pd.DataFrame([{
            "CI": h.ci_id, "Metric": h.metric, "Alerts": h.alert_count,
            "% of volume": h.pct_of_total,
            "Critical": h.severity_mix.get("critical", 0),
            "Warning": h.severity_mix.get("warning", 0),
            "Recommendation": h.recommendation,
        } for h in hotspots]), use_container_width=True, hide_index=True)
    else:
        st.info("No alerts in the current window.")

    st.divider()
    st.subheader("Automation Backlog — Runbook Coverage Gaps")
    gaps = automation_gaps(incidents)
    if gaps:
        total_toil = sum(g.est_annual_toil_hours for g in gaps)
        st.warning(f"{len(gaps)} incident pattern(s) had no matching runbook — "
                   f"an estimated **{total_toil:.0f} hours/year** of manual toil.")
        st.dataframe(pd.DataFrame([{
            "Incident": g.incident_id, "Metrics": ", ".join(g.metrics),
            "Severity": g.severity, "Root cause": g.root_cause,
            "Suggested runbook": g.suggested_runbook,
            "Est. annual toil (h)": g.est_annual_toil_hours,
        } for g in gaps]), use_container_width=True, hide_index=True)
    else:
        st.success("Full runbook coverage — every incident matched an "
                   "auto-remediation path.")

# ---------------- Executive (management view) ----------------
with tab_exec:
    from core.slo import SLOEngine, DEFAULT_SLOS, SLO
    from core.insights import service_scorecard, automation_gaps, noise_hotspots
    from core.reports import exec_report_markdown

    st.subheader("Error Budget Status")
    st.caption("Burn rate 1.0 = exactly on pace to exhaust the budget by window "
               "end. Thresholds follow the Google SRE multi-window model "
               "(14.4× page, 6× ticket).")

    with st.expander("Adjust SLO targets"):
        target = st.slider("Availability target (%)", 99.0, 99.99, 99.9, 0.01)
        window_days = st.selectbox("Window (days)", [7, 30, 90], index=1)
        custom_slos = [SLO(name=s.name, business_service=s.business_service,
                           target_pct=target, window_days=window_days,
                           severity_counts=s.severity_counts)
                       for s in DEFAULT_SLOS]

    statuses = SLOEngine(custom_slos).evaluate(incidents)
    for s in statuses:
        color = {"HEALTHY": "🟢", "AT RISK": "🟡", "SLOW BURN": "🟠",
                 "FAST BURN": "🔴", "EXHAUSTED": "⛔"}[s.status]
        with st.container(border=True):
            st.markdown(f"### {color} {s.slo.name} — {s.status}")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Target", f"{s.slo.target_pct}%")
            m2.metric("Achieved", f"{s.achieved_pct}%")
            m3.metric("Budget used", f"{s.consumed_pct}%")
            m4.metric("Burn rate", f"{s.burn_rate}×")
            st.progress(min(s.consumed_pct / 100, 1.0),
                        text=f"{s.remaining_minutes:.0f} of "
                             f"{s.budget_minutes:.0f} budget minutes remaining")
            st.info(f"**Recommended action:** {s.action}")
            if s.contributing_incidents:
                st.caption("Contributing incidents: " + ", ".join(
                    f"{i} ({m}m)" for i, m in s.contributing_incidents))

    st.divider()
    st.subheader("Service Health Scorecard")
    scorecard = service_scorecard(incidents)
    st.dataframe(pd.DataFrame([{
        "Service": s.business_service, "Grade": s.grade, "Score": s.health_score,
        "Incidents": s.incidents, "Critical": s.critical,
        "Auto-resolved": s.auto_resolved, "Open": s.open_items,
    } for s in scorecard]), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("ROI Model")
    st.caption("Adjust the assumptions — savings recalculate live. Defaults are "
               "conservative; state your own assumptions when presenting.")
    r1, r2, r3 = st.columns(3)
    manual_mttr = r1.number_input("Manual MTTR (min)", 10, 240, 45, 5)
    hourly_cost = r2.number_input("Engineer cost ($/hr)", 20, 300, 60, 5)
    annual_incidents = r3.number_input("Incidents/year", 100, 20000, 2400, 100)

    from core.kpi import KPIEngine as _KE
    roi = _KE(manual_mttr_min=manual_mttr, engineer_cost_per_hr=hourly_cost,
              incidents_per_year_estimate=annual_incidents).compute(
                  stats["raw_alerts"], incidents)
    v1, v2, v3 = st.columns(3)
    v1.metric("Automation rate", f"{roi.automation_rate_pct}%")
    v2.metric("Est. annual savings", f"${roi.est_automation_savings_usd:,.0f}")
    v3.metric("Toil hours avoided/yr",
              f"{roi.est_automation_savings_usd / max(hourly_cost, 1):,.0f}")

    st.divider()
    st.subheader("Export")
    report_md = exec_report_markdown(
        kpi, stats, statuses, scorecard, automation_gaps(incidents),
        noise_hotspots(result["raw_alerts"]),
        period_label=("live ServiceNow data" if result.get("mode") == "live"
                      else "demo analysis window"))
    st.download_button("📊 Download reliability briefing (Markdown)", report_md,
                       file_name="reliability_briefing.md", mime="text/markdown")
    with st.expander("Preview"):
        st.markdown(report_md)
