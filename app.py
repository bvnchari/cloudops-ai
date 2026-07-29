"""
CloudOps-AI — Executive Dashboard (Phase 6 UI).

Run locally:  streamlit run app.py
Deploy:       HuggingFace Spaces (Streamlit SDK) — same pattern as CloudBridge.
"""

from datetime import datetime, timedelta, timezone

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
st.session_state.setdefault("jira_config", None)     # JiraConfig once validated
st.session_state.setdefault("jira_status", None)     # last connection test result
st.session_state.setdefault("jira_issues", [])        # JiraIssue objects filed this session
st.session_state.setdefault("change_requests", {})     # change_id -> ChangeRequest
st.session_state.setdefault("k8s_executor_config", None)  # dict of KubernetesExecutor kwargs, or None (read-only)
st.session_state.setdefault("cluster_inventory_records", None)  # list[dict] uploaded cluster/namespace rows
st.session_state.setdefault("metrics_source_config", None)  # dict describing active Prometheus/Datadog/Dynatrace/Grafana source
st.session_state.setdefault("data_source", "demo")     # demo | live
st.session_state.setdefault("anthropic_key", "")
st.session_state.setdefault("live_result", None)
st.session_state.setdefault("report_subscriptions", {})   # name -> ReportSubscription

def _auto_load_sn_config():
    """If SN_* secrets/env vars are set and no config is loaded yet, connect
    automatically — avoids re-typing credentials after every refresh/redeploy.
    Silently no-ops if unset or the test connection fails; the Config tab
    form remains available as a manual fallback either way."""
    if st.session_state.sn_config is not None:
        return
    try:
        from core.servicenow import SNConfig, EnterpriseServiceNowConnector
        secrets_sn = dict(st.secrets.get("servicenow", {})) if hasattr(st, "secrets") else {}
        cfg = SNConfig(
            instance=secrets_sn.get("instance", ""), user=secrets_sn.get("user", ""),
            password=secrets_sn.get("password", ""),
            client_id=secrets_sn.get("client_id", ""),
            client_secret=secrets_sn.get("client_secret", ""),
        ) if secrets_sn.get("instance") else SNConfig.from_env()
        if cfg and not cfg.validate():
            EnterpriseServiceNowConnector(cfg).test_connection()
            st.session_state.sn_config = cfg
    except Exception:
        pass  # fall back to manual entry in the Config tab — never block startup

def _auto_load_jira_config():
    if st.session_state.jira_config is not None:
        return
    try:
        from core.jira import JiraConfig, EnterpriseJiraConnector
        secrets_jira = dict(st.secrets.get("jira", {})) if hasattr(st, "secrets") else {}
        cfg = JiraConfig(
            base_url=secrets_jira.get("base_url", ""), email=secrets_jira.get("email", ""),
            api_token=secrets_jira.get("api_token", ""),
            project_key=secrets_jira.get("project_key", ""),
        ) if secrets_jira.get("base_url") else JiraConfig.from_env()
        if cfg and not cfg.validate():
            EnterpriseJiraConnector(cfg).test_connection()
            st.session_state.jira_config = cfg
    except Exception:
        pass

def _auto_load_anthropic_key():
    if st.session_state.anthropic_key:
        return
    try:
        import os
        key = (dict(st.secrets.get("anthropic", {})).get("api_key", "")
              if hasattr(st, "secrets") else "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if key:
            st.session_state.anthropic_key = key
    except Exception:
        pass

_auto_load_sn_config()
_auto_load_jira_config()
_auto_load_anthropic_key()


def _auto_load_metrics_source_config():
    if st.session_state.get("metrics_source_config"):
        return
    try:
        if not hasattr(st, "secrets"):
            return
        cfg = dict(st.secrets.get("metrics_source", {}))
        provider = cfg.get("provider", "")
        if not provider:
            return
        from core import metrics_sources as ms
        if provider == "prometheus":
            src = ms.PrometheusSource(base_url=cfg["base_url"], bearer_token=cfg.get("bearer_token", ""))
        elif provider == "datadog":
            src = ms.DatadogSource(api_key=cfg["api_key"], app_key=cfg["app_key"],
                                   site=cfg.get("site", "datadoghq.com"))
        elif provider == "dynatrace":
            src = ms.DynatraceSource(base_url=cfg["base_url"], api_token=cfg["api_token"])
        elif provider == "grafana":
            src = ms.GrafanaSource(base_url=cfg["base_url"], api_key=cfg["api_key"],
                                   datasource_uid=cfg["datasource_uid"])
        else:
            return
        src.test_connection()
        st.session_state.metrics_source_config = dict(cfg)
    except Exception:
        pass  # fall back to manual Config tab entry — never block startup

_auto_load_metrics_source_config()


def _build_metrics_source():
    cfg = st.session_state.get("metrics_source_config")
    if not cfg:
        return None
    from core import metrics_sources as ms
    provider = cfg["provider"]
    if provider == "prometheus":
        return ms.PrometheusSource(base_url=cfg["base_url"], bearer_token=cfg.get("bearer_token", ""))
    if provider == "datadog":
        return ms.DatadogSource(api_key=cfg["api_key"], app_key=cfg["app_key"], site=cfg.get("site", "datadoghq.com"))
    if provider == "dynatrace":
        return ms.DynatraceSource(base_url=cfg["base_url"], api_token=cfg["api_token"])
    if provider == "grafana":
        return ms.GrafanaSource(base_url=cfg["base_url"], api_key=cfg["api_key"],
                                datasource_uid=cfg["datasource_uid"])
    return None

if st.session_state.data_source == "live" and st.session_state.live_result:
    result = st.session_state.live_result
    if result.get("series"):
        st.info(f"**LIVE mode** — topology and alerts sourced from ServiceNow "
                f"(`{result.get('alert_source', 'n/a')}`). Telemetry and anomaly "
                f"detection are running on real metrics from a configured "
                f"metrics source ({len(result['series'])} series).")
    else:
        st.info(f"**LIVE mode** — topology and alerts sourced from ServiceNow "
                f"(`{result.get('alert_source', 'n/a')}`). Telemetry and anomaly "
                f"detection are unavailable until a metrics source (Prometheus/"
                f"Datadog/Dynatrace/Grafana) is configured in ⚙️ Config — "
                f"ServiceNow itself is a system of record, not a metrics store.")
else:
    result = load()
    if st.session_state.sn_config:
        st.warning("**DEMO mode** — showing synthetic data, not your real "
                  "ServiceNow instance. If you meant to see live data, go to "
                  "⚙️ Config → Connections → ServiceNow → Data Source and "
                  "click **🔄 Pull data from ServiceNow** (this resets to demo "
                  "on every refresh).")
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

(tab_oncall, tab_exec, tab_slx, tab_reports, tab_funnel, tab_inc, tab_metrics,
 tab_rem, tab_change, tab_itsm, tab_ai, tab_cfg) = st.tabs(
    ["🎯 On-Call", "📊 Executive", "🎚️ SLI/SLO/SLA", "📤 Reports & Delivery",
     "📉 Alert Funnel", "🚨 Alerts", "📈 Telemetry", "🔧 Remediation",
     "⚡ Change Mgmt", "🎫 Incidents", "🤖 AI Analyst", "⚙️ Config"])

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
    import altair as alt

    st.subheader("Metric Explorer")
    st.caption("Live time-series from the active data source — every number "
               "below (stats, chart, anomalies, exports) is computed from the "
               "same `result[\"series\"]`/`result[\"signals\"]` the rest of the "
               "app uses; nothing here is separately mocked.")
    if not result.get("series"):
        st.warning("No metric time-series in LIVE mode. ServiceNow stores events "
                   "and CIs, not raw metrics — connect a real metrics backend in "
                   "**⚙️ Config → Infrastructure & Execution → 📈 Metrics Source** "
                   "(Prometheus, Datadog, Dynatrace, or Grafana) and re-pull, or "
                   "switch back to demo mode in ⚙️ Config.")
    else:
        keys = sorted(result["series"].keys())
        key_labels = {f"{ci} · {m}": (ci, m) for ci, m in keys}
        all_signals = result.get("signals", [])

        def _series_df(ci: str, metric: str) -> pd.DataFrame:
            pts = result["series"][(ci, metric)]
            sigs = {s.ts for s in all_signals if s.ci_id == ci and s.metric == metric}
            return pd.DataFrame({
                "ci_id": ci, "metric": metric,
                "time": pd.to_datetime([p.ts for p in pts], unit="s"),
                "value": [p.value for p in pts],
                "is_anomaly": [p.ts in sigs for p in pts],
            })

        # ---- Top KPI strip ----
        total_points = sum(len(result["series"][k]) for k in keys)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Series tracked", len(keys))
        k2.metric("Data points", f"{total_points:,}")
        k3.metric("Anomaly signals", len(all_signals))
        k4.metric("Source", "LIVE" if result.get("mode") == "live" else "Synthetic/Demo")

        st.divider()

        # ---- Series selection (multi-compare) ----
        sel_labels = st.multiselect(
            "Series to compare", list(key_labels.keys()),
            default=[list(key_labels.keys())[0]],
            help="Select one or more CI · metric pairs. Each renders its own "
                 "chart with anomaly points overlaid in red.")

        if not sel_labels:
            st.info("Select at least one series above.")
        else:
            frames = [_series_df(*key_labels[lbl]) for lbl in sel_labels]
            combined = pd.concat(frames, ignore_index=True)

            # ---- Real time-range filter, driven by the actual data ----
            tmin, tmax = combined["time"].min(), combined["time"].max()
            if tmin < tmax:
                t_start, t_end = st.slider(
                    "Time range", min_value=tmin.to_pydatetime(),
                    max_value=tmax.to_pydatetime(),
                    value=(tmin.to_pydatetime(), tmax.to_pydatetime()),
                    format="MM/DD HH:mm")
                combined = combined[(combined["time"] >= t_start) & (combined["time"] <= t_end)]

            # ---- Per-series stats table ----
            stats_rows = []
            for lbl in sel_labels:
                ci, metric = key_labels[lbl]
                sub = combined[(combined["ci_id"] == ci) & (combined["metric"] == metric)]
                if sub.empty:
                    continue
                stats_rows.append({
                    "Series": lbl,
                    "Latest": round(sub["value"].iloc[-1], 3),
                    "Min": round(sub["value"].min(), 3),
                    "Max": round(sub["value"].max(), 3),
                    "Avg": round(sub["value"].mean(), 3),
                    "P95": round(sub["value"].quantile(0.95), 3),
                    "Anomalies": int(sub["is_anomaly"].sum()),
                    "Status": "🔴 Anomalous" if sub["is_anomaly"].any() else "🟢 Normal",
                })
            st.dataframe(pd.DataFrame(stats_rows), use_container_width=True, hide_index=True)

            # ---- Chart(s) with anomaly overlay ----
            for lbl in sel_labels:
                ci, metric = key_labels[lbl]
                sub = combined[(combined["ci_id"] == ci) & (combined["metric"] == metric)]
                if sub.empty:
                    continue
                base = alt.Chart(sub).encode(x=alt.X("time:T", title="Time"))
                line = base.mark_line(color="#1B6FA8").encode(
                    y=alt.Y("value:Q", title=metric))
                points = base.transform_filter(alt.datum.is_anomaly).mark_circle(
                    size=70, color="#D64545").encode(y="value:Q",
                    tooltip=["time:T", "value:Q"])
                st.caption(f"**{lbl}**")
                st.altair_chart((line + points).properties(height=220),
                                use_container_width=True)

            st.divider()

            # ---- Bulk export: real data, not re-derived ----
            st.markdown("### 📦 Bulk data export")
            e1, e2 = st.columns(2)
            with e1:
                sel_csv = combined.sort_values(["ci_id", "metric", "time"]).to_csv(index=False)
                st.download_button(
                    "⬇️ Download selected series (CSV)", sel_csv,
                    file_name="cloudops_selected_series.csv", mime="text/csv",
                    key="metrics_dl_selected")
            with e2:
                full_frames = [_series_df(ci, m) for ci, m in keys]
                full_csv = pd.concat(full_frames, ignore_index=True) \
                             .sort_values(["ci_id", "metric", "time"]).to_csv(index=False)
                st.download_button(
                    "⬇️ Download ALL telemetry (CSV)", full_csv,
                    file_name="cloudops_all_telemetry.csv", mime="text/csv",
                    key="metrics_dl_all",
                    help=f"Every point across all {len(keys)} series currently loaded "
                         f"— {total_points:,} rows.")

# ---------------- Remediation ----------------
with tab_rem:
    from core.remediation import DEFAULT_RUNBOOKS

    st.subheader("Self-Healing Execution Log")

    is_live = result.get("mode") == "live"
    has_real_executor = bool(st.session_state.get("k8s_executor_config"))
    if is_live and has_real_executor:
        st.success("**LIVE mode — real execution active.** A real `kubectl` is "
                   "wired into a real cluster/namespace (⚙️ Config → Real "
                   "Kubernetes Executor). Matched kubectl-based runbooks "
                   "actually run, and are only marked resolved if a real "
                   "`kubectl rollout status` check confirms the fix worked. "
                   "Non-kubectl runbooks are honestly refused, not faked.")
    elif is_live:
        st.warning("**LIVE mode — read-only.** These incidents came from a "
                   "real ServiceNow instance, so nothing is actually "
                   "executed. Below are dry-run **recommendations**: exactly "
                   "which runbook would fire and which commands it would "
                   "run, logged but not sent anywhere. Connect a real "
                   "executor in **⚙️ Config → Real Kubernetes Executor** to "
                   "actually execute and verify these.")
    else:
        st.info("**Demo mode — simulated.** These are synthetic incidents "
                "against synthetic infrastructure, so the executor reports "
                "simulated success for realistic logs. Nothing runs anywhere "
                "real. Switch to LIVE ServiceNow data in ⚙️ Config to see the "
                "honest read-only (or real-executor) behavior instead.")

    rems = result["remediations"]
    n_resolved = sum(1 for r in rems if r.mode == "simulated" and r.success)
    n_recommended = sum(1 for r in rems if r.mode == "read_only")
    n_failed = sum(1 for r in rems if r.mode == "simulated" and not r.success)
    n_live_verified = sum(1 for r in rems if r.mode == "live" and r.success)
    n_live_failed = sum(1 for r in rems if r.mode == "live" and not r.success)
    m1, m2, m3 = st.columns(3)
    if is_live and has_real_executor:
        m1.metric("Executed & verified", n_live_verified)
        m2.metric("Attempted, not verified", n_live_failed)
        m3.metric("Runbooks matched", len(rems))
    elif is_live:
        m1.metric("Recommendations", n_recommended)
        m2.metric("Runbooks matched", len(rems))
        m3.metric("Executed", 0, help="Always 0 without a real executor configured.")
    else:
        m1.metric("Auto-resolved", n_resolved)
        m2.metric("Failed", n_failed)
        m3.metric("Total runbook runs", len(rems))

    for r in rems:
        if r.mode == "read_only":
            icon = "🟡"
            label = "Recommended (dry-run)"
        elif r.mode == "live" and r.success:
            icon = "✅"
            label = "Executed & verified (real)"
        elif r.mode == "live":
            icon = "❌"
            label = "Executed but not verified — NOT resolved"
        elif r.success:
            icon = "✅"
            label = "Simulated success"
        else:
            icon = "❌"
            label = "Failed"
        with st.expander(f"{icon} {r.incident_id} → {r.runbook_name} · {label}"):
            for line in r.log:
                st.code(line, language="bash")

    st.divider()
    st.subheader("What incidents does the AI act on?")
    st.caption("A runbook only matches when BOTH the incident's correlated "
               "metrics AND its severity fit — everything else is left for a "
               "human. `auto_approve=False` runbooks (capacity/scaling "
               "changes) never auto-fire even in demo mode; they always wait "
               "for change-management approval, shown as 🕐 pending in the "
               "On-Call tab.")
    st.dataframe(pd.DataFrame([{
        "Runbook": rb.name, "Triggers on metrics": ", ".join(rb.match_metrics),
        "Severities": ", ".join(rb.match_severities),
        "Approval": "Auto" if rb.auto_approve else "🕐 Requires human approval",
        "Actions": " → ".join(a.name for a in rb.actions),
    } for rb in DEFAULT_RUNBOOKS]), use_container_width=True, hide_index=True)

# ---------------- Change Management ----------------
with tab_change:
    from core.change import (AIChangePlanner, advance_to_dev, advance_to_test,
                             submit_for_prod_approval, approve, advance_to_prod,
                             close as close_change, open_change_ticket,
                             open_jira_for_change)

    st.subheader("⚡ AI-Assisted Change Management")
    st.caption("Engineer submits a problem statement → AI drafts a change "
               "plan + backout plan → Dev/Test dry-run → **two-stage** Prod "
               "approval → linked ServiceNow Change + Jira issue. Every "
               "stage is a dry-run — nothing here executes real commands, "
               "same honesty model as the 🔧 Remediation tab. Wire a real "
               "Executor into `core.change` when you're ready to actually "
               "run these against an environment.")

    with st.form("change_new_form"):
        problem = st.text_area(
            "Problem statement", placeholder="e.g. Disk utilization on "
            "node-02 is climbing steadily and will breach 95% within a day.")
        requester = st.text_input("Requested by", placeholder="your name")
        submitted_new = st.form_submit_button("🤖 Draft change plan", type="primary")

    if submitted_new:
        if not problem.strip():
            st.error("Problem statement is required.")
        else:
            planner = AIChangePlanner(api_key=st.session_state.anthropic_key or None)
            plan = planner.generate(problem)
            seq = len(st.session_state.change_requests) + 1
            change_id = f"CHG-AI-{seq:04d}"
            from core.change import ChangeRequest
            cr = ChangeRequest(change_id=change_id, problem_statement=problem,
                              requested_by=requester or "unknown", plan=plan)
            st.session_state.change_requests[change_id] = cr
            st.success(f"Drafted **{change_id}** via **{plan.source}** "
                      f"(risk: {plan.risk}).")
            st.rerun()

    st.divider()
    crs = st.session_state.change_requests
    if not crs:
        st.info("No change requests yet — draft one above.")
    else:
        pick = st.selectbox("Change request", list(crs.keys())[::-1],
                            format_func=lambda k: f"{k} · {crs[k].status} · "
                                                  f"{crs[k].problem_statement[:50]}")
        cr = crs[pick]

        c1, c2, c3 = st.columns(3)
        c1.metric("Status", cr.status)
        c2.metric("Risk", cr.plan.risk)
        c3.metric("Plan source", cr.plan.source)
        if cr.plan.source == "manual_review_required":
            st.warning("No LLM configured and no runbook matched — this plan "
                      "needs a human author before proceeding.")

        st.markdown("**Change plan**")
        for s in cr.plan.steps:
            st.code(f"{s.name}: {s.command}", language="bash")
        st.markdown("**Backout plan**")
        for s in cr.plan.backout_steps:
            st.code(f"{s.name}: {s.command}", language="bash")
        st.caption(cr.plan.rationale)

        st.divider()
        st.markdown("### Dev / Test dry-run")
        d1, d2 = st.columns(2)
        with d1:
            if st.button("▶️ Dry-run in Dev", key=f"dev_{cr.change_id}",
                        disabled=cr.dev_evidence is not None):
                advance_to_dev(cr)
                st.rerun()
            if cr.dev_evidence:
                st.success("Dev dry-run recorded.")
                with st.expander("Dev evidence"):
                    for line in cr.dev_evidence.steps_logged:
                        st.code(line, language="bash")
                    st.caption(cr.dev_evidence.note)
        with d2:
            if st.button("▶️ Dry-run in Test", key=f"test_{cr.change_id}",
                        disabled=cr.dev_evidence is None or cr.test_evidence is not None):
                advance_to_test(cr)
                st.rerun()
            if cr.test_evidence:
                st.success("Test dry-run recorded.")
                with st.expander("Test evidence"):
                    for line in cr.test_evidence.steps_logged:
                        st.code(line, language="bash")
                    st.caption(cr.test_evidence.note)

        st.divider()
        st.markdown("### Prod — two-stage approval")
        if not cr.ready_for_prod:
            st.info("Dev and Test dry-runs must both be recorded before "
                    "requesting Prod approval.")
        else:
            if cr.status == "dev" or cr.status == "test":
                if st.button("📨 Submit for Prod approval", key=f"submit_{cr.change_id}"):
                    submit_for_prod_approval(cr)
                    sn_cfg = st.session_state.sn_config
                    ticket = open_change_ticket(sn_cfg, cr)
                    cr.sn_change_number = ticket["number"]
                    try:
                        from core.jira import JiraBridge
                        jbridge = JiraBridge(jira_config=st.session_state.jira_config)
                        issue = open_jira_for_change(jbridge, cr)
                        cr.jira_key = issue.key
                        st.session_state.jira_issues = st.session_state.jira_issues + [issue]
                    except Exception as e:
                        st.warning(f"SN Change opened ({ticket['number']}) but Jira "
                                  f"filing failed: {e}")
                    st.rerun()
            if cr.sn_change_number:
                st.caption(f"🎫 ServiceNow Change: **{cr.sn_change_number}**" +
                          (f" · 🔧 Jira: **{cr.jira_key}**" if cr.jira_key else ""))

            if cr.status in ("pending_prod_approval", "approved_stage1"):
                a1, a2 = st.columns(2)
                with a1:
                    if not cr.stage1_done:
                        approver1 = st.text_input("Stage 1 approver (peer/lead)",
                                                  key=f"appr1_{cr.change_id}")
                        if st.button("✅ Approve — Stage 1", key=f"apr1btn_{cr.change_id}"):
                            if approver1.strip():
                                approve(cr, "stage1", approver1)
                                st.rerun()
                            else:
                                st.error("Approver name required.")
                    else:
                        a = next(a for a in cr.approvals if a.stage == "stage1")
                        st.success(f"Stage 1 approved by **{a.approver}**")
                with a2:
                    if cr.stage1_done and not cr.stage2_done:
                        approver2 = st.text_input("Stage 2 approver (change manager/CAB)",
                                                  key=f"appr2_{cr.change_id}")
                        if st.button("✅ Approve — Stage 2", key=f"apr2btn_{cr.change_id}"):
                            if approver2.strip():
                                approve(cr, "stage2", approver2)
                                st.rerun()
                            else:
                                st.error("Approver name required.")
                    elif cr.stage2_done:
                        a = next(a for a in cr.approvals if a.stage == "stage2")
                        st.success(f"Stage 2 approved by **{a.approver}**")

            if cr.fully_approved and cr.status not in ("prod", "closed"):
                k8s_cfg = st.session_state.get("k8s_executor_config")
                inv_records = st.session_state.get("cluster_inventory_records")
                if k8s_cfg and inv_records:
                    st.caption("A real executor is configured — pick the target "
                              "cluster/namespace for this change from the inventory "
                              "to execute (and verify) it for real, or leave "
                              "unselected to keep this a dry-run.")
                    from core.cluster_inventory import ClusterInventory
                    inv = ClusterInventory.from_records(inv_records)
                    target_labels = ["(dry-run only — don't execute for real)"] + \
                                    [f"{t.match} · {t.cluster_name}/{t.namespace}" for t in inv.targets]
                    pick = st.selectbox("Execution target", target_labels,
                                        key=f"target_{cr.change_id}")
                    if pick != target_labels[0]:
                        t = inv.targets[target_labels.index(pick) - 1]
                        cr.target_context = {"deployment": t.match.rstrip("*"), "node": t.match.rstrip("*"),
                                             "namespace": t.namespace, "kube_context": t.kube_context,
                                             "target_found": True, "target": t}
                    else:
                        cr.target_context = None

                btn_label = ("🚀 Execute in Prod (real)" if (k8s_cfg and cr.target_context)
                            else "🚀 Dry-run in Prod")
                if st.button(btn_label, key=f"prod_{cr.change_id}", type="primary"):
                    live_exec = None
                    if k8s_cfg and cr.target_context:
                        from core.k8s_executor import KubernetesExecutor
                        live_exec = KubernetesExecutor(**k8s_cfg)
                    try:
                        advance_to_prod(cr, executor=live_exec)
                    except ValueError as e:
                        st.error(str(e))
                    st.rerun()
            if cr.prod_evidence:
                if cr.status == "prod" and cr.prod_evidence.note.startswith("Executed for real"):
                    st.success("Executed for real and verified — the change is genuinely complete.")
                elif cr.status == "prod_execution_failed":
                    st.error("Executed but NOT verified — this change is NOT complete. "
                             "Investigate before closing.")
                else:
                    st.success("Prod dry-run recorded — both approvals on file, "
                              "ready for a human (or a wired-in Executor) to "
                              "actually execute.")
                with st.expander("Prod evidence"):
                    for line in cr.prod_evidence.steps_logged:
                        st.code(line, language="bash")
                    st.caption(cr.prod_evidence.note)
                if cr.status not in ("closed", "prod_execution_failed") and \
                        st.button("🔒 Close change", key=f"close_{cr.change_id}"):
                    close_change(cr)
                    st.rerun()

# ---------------- ITSM ----------------
with tab_itsm:
    from core.reports import itsm_report_markdown

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

    SLA_HOURS = {"1": 1.0, "2": 4.0, "3": 8.0}
    _now = datetime.now(timezone.utc).timestamp()

    def _elapsed_min(t) -> float:
        return ((t.resolved_at or _now) - t.opened_at) / 60

    def _is_breach(t) -> bool:
        return _elapsed_min(t) / 60 > SLA_HOURS.get(t.impact, 8.0)

    # ---- KPI strip ----
    open_n = sum(1 for t in tickets if t.state != "Resolved")
    resolved = [t for t in tickets if t.state == "Resolved" and t.resolved_at]
    mttr = (sum((t.resolved_at - t.opened_at) / 60 for t in resolved)
           / len(resolved)) if resolved else None
    breaches = [t for t in tickets if _is_breach(t)]

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Total tickets", len(tickets))
    k2.metric("Open", open_n)
    k3.metric("Resolved", len(tickets) - open_n)
    k4.metric("MTTR", f"{mttr:.0f} min" if mttr is not None else "—")
    k5.metric("SLA breaches", len(breaches),
             delta=None if not breaches else f"-{len(breaches)}",
             delta_color="inverse")
    k6.metric("Filed in Jira", len(st.session_state.jira_issues))

    st.divider()

    # ---- Filters ----
    f1, f2, f3, f4 = st.columns(4)
    states = sorted({t.state for t in tickets})
    impacts = sorted({t.impact for t in tickets})
    services = sorted({t.business_service or "Unmapped" for t in tickets})
    f_state = f1.multiselect("State", states, default=states)
    f_impact = f2.multiselect("Impact", impacts, default=impacts,
                              format_func=lambda i: f"P{i}")
    f_service = f3.multiselect("Business service", services, default=services)
    f_search = f4.text_input("Search (number/description)", "")

    filtered = [
        t for t in tickets
        if t.state in f_state and t.impact in f_impact
        and (t.business_service or "Unmapped") in f_service
        and (f_search.lower() in t.number.lower()
             or f_search.lower() in t.short_description.lower())
    ]
    st.caption(f"Showing {len(filtered)} of {len(tickets)} tickets.")

    rows = []
    for t in filtered:
        rows.append({
            "Number": t.number, "State": t.state, "Impact": f"P{t.impact}",
            "Urgency": t.urgency, "Service": t.business_service or "Unmapped",
            "CMDB CI": t.cmdb_ci, "Short Description": t.short_description,
            "Elapsed/Resolution (min)": round(_elapsed_min(t)),
            "SLA": "🔴 Breach" if _is_breach(t) else "🟢 On track",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ---- Cross-linked ticket detail (ties back to the correlated Incident
    # and, if synced, the Jira engineering issue) ----
    incidents_by_id = {i.incident_id: i for i in incidents}
    jira_by_incident = {i.incident_id: i for i in st.session_state.jira_issues}
    from core.insights import automation_gaps as _auto_gaps_itsm
    _gap_ids = {g.incident_id for g in _auto_gaps_itsm(incidents)}
    with st.expander(f"🔍 Ticket detail & cross-links ({len(filtered)} tickets)"):
        for t in filtered:
            inc = incidents_by_id.get(t.incident_id)
            jira = jira_by_incident.get(t.incident_id)
            st.markdown(f"**{t.number}** · {t.short_description}")
            c1, c2, c3 = st.columns(3)
            c1.caption(f"State: **{t.state}** · Impact **P{t.impact}** / "
                      f"Urgency **{t.urgency}**")
            c2.caption(f"Service: **{t.business_service or 'Unmapped'}** · "
                      f"CI: **{t.cmdb_ci or '—'}**")
            c3.caption(f"SLA: {'🔴 Breach' if _is_breach(t) else '🟢 On track'} "
                      f"({_elapsed_min(t):.0f} min)")
            if inc:
                st.caption(f"↳ Linked incident **{inc.incident_id}** · root cause "
                          f"**{inc.probable_root_cause or 'not determined'}** · "
                          f"{inc.raw_alert_count} alerts correlated")
            if jira:
                st.caption(f"🔧 Jira: **[{jira.key}]({jira.url})** "
                          f"({jira.reason.replace('_', ' ')})")
            elif t.incident_id in _gap_ids or _is_breach(t):
                st.caption("🟡 Not yet filed in Jira — qualifies as engineering "
                          "backlog (automation gap or SLA breach). Sync from "
                          "**⚙️ Config → Jira Integration**.")
            if t.close_notes:
                st.caption(f"Close notes: {t.close_notes}")
            st.divider()

    if pub and pub.errors:
        with st.expander(f"⚠️ {len(pub.errors)} error(s) during publish"):
            for stage, detail in pub.errors:
                st.error(f"**{stage}** — {detail}")

    st.divider()

    # ---- Bulk export + report generation (interlinked with Reports & Delivery) ----
    st.markdown("### 📦 Export & reporting")
    e1, e2 = st.columns(2)
    with e1:
        csv_data = pd.DataFrame(rows).to_csv(index=False)
        st.download_button("⬇️ Download filtered tickets (CSV)", csv_data,
                           file_name="cloudops_itsm_tickets.csv", mime="text/csv",
                           key="itsm_dl_csv")
    with e2:
        if st.button("📤 Generate Incident Ticket Summary report", key="itsm_gen_report"):
            md = itsm_report_markdown(filtered or tickets, period_label="current window",
                                      sla_hours=SLA_HOURS,
                                      jira_issues=st.session_state.jira_issues)
            st.session_state["rep_markdown"] = md
            st.session_state["rep_markdown_name"] = "CloudOps-AI_ITSM_Ticket_Summary.md"
            st.session_state["rep_type_choice"] = "Incident Ticket Summary"
            st.success("Report built — download it right here, or open "
                       "**📤 Reports & Delivery** where it's already loaded.")
            st.download_button("⬇️ Download Markdown", md,
                               file_name="CloudOps-AI_ITSM_Ticket_Summary.md",
                               mime="text/markdown", key="itsm_dl_md")

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
    st.header("⚙️ Configuration")
    st.caption("Connections (ServiceNow, Jira, LLM) authenticate CloudOps-AI "
               "to external SaaS systems. Infrastructure & Execution "
               "(Kubernetes, AWS, GCP, Azure) is what actually lets "
               "Remediation and Change Mgmt execute and verify real fixes — "
               "see each provider's tab for its own Save & Test.")

    st.markdown("""
    <style>
    /* Top-level: Connections (blue) vs Infrastructure & Execution (orange) */
    [class*="st-key-cfg_top_level"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(1),
    [class*="st-key-cfg_top_level"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(1) p {
        color: #1B6FA8 !important; font-weight: 600 !important;
    }
    [class*="st-key-cfg_top_level"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(2),
    [class*="st-key-cfg_top_level"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(2) p {
        color: #C0621F !important; font-weight: 600 !important;
    }
    [class*="st-key-cfg_top_level"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(1) { border-bottom-color: #1B6FA8 !important; background-color: rgba(27,111,168,0.08) !important; }
    [class*="st-key-cfg_top_level"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(2) { border-bottom-color: #C0621F !important; background-color: rgba(192,98,31,0.08) !important; }

    /* Connections sub-tabs: ServiceNow / Jira / LLM — each a distinct shade */
    [class*="st-key-cfg_conn_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(1),
    [class*="st-key-cfg_conn_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(1) p { color: #0F6E56 !important; }
    [class*="st-key-cfg_conn_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(2),
    [class*="st-key-cfg_conn_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(2) p { color: #534AB7 !important; }
    [class*="st-key-cfg_conn_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(3),
    [class*="st-key-cfg_conn_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(3) p { color: #A8781E !important; }
    [class*="st-key-cfg_conn_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(1) { border-bottom-color: #0F6E56 !important; background-color: rgba(15,110,86,0.08) !important; }
    [class*="st-key-cfg_conn_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(2) { border-bottom-color: #534AB7 !important; background-color: rgba(83,74,183,0.08) !important; }
    [class*="st-key-cfg_conn_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(3) { border-bottom-color: #A8781E !important; background-color: rgba(168,120,30,0.08) !important; }

    /* Infrastructure sub-tabs: Kubernetes / AWS / GCP / Azure / Inventory — each distinct */
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(1),
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(1) p { color: #2C5F8A !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(2),
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(2) p { color: #D2691E !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(3),
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(3) p { color: #1A73E8 !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(4),
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(4) p { color: #6264A7 !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(5),
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(5) p { color: #8A6D3B !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(6),
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(6) p { color: #B8471E !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(7),
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"]:nth-of-type(7) p { color: #117A65 !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(1) { border-bottom-color: #2C5F8A !important; background-color: rgba(44,95,138,0.08) !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(2) { border-bottom-color: #D2691E !important; background-color: rgba(210,105,30,0.08) !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(3) { border-bottom-color: #1A73E8 !important; background-color: rgba(26,115,232,0.08) !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(4) { border-bottom-color: #6264A7 !important; background-color: rgba(98,100,167,0.08) !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(5) { border-bottom-color: #8A6D3B !important; background-color: rgba(138,109,59,0.08) !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(6) { border-bottom-color: #B8471E !important; background-color: rgba(184,71,30,0.08) !important; }
    [class*="st-key-cfg_infra_subtabs"] [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:nth-of-type(7) { border-bottom-color: #117A65 !important; background-color: rgba(17,122,101,0.08) !important; }
    </style>
    """, unsafe_allow_html=True)

    with st.container(key="cfg_top_level"):
        conn_group, infra_group = st.tabs(["🔌 Connections", "🏗️ Infrastructure & Execution"])

    with conn_group:
        with st.container(key="cfg_conn_subtabs"):
            sn_tab, jira_tab, llm_tab = st.tabs(["🎫 ServiceNow", "🔧 Jira", "🤖 LLM (Anthropic)"])

        with sn_tab:
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
                    password = st.text_input(
                        "Password", type="password",
                        placeholder="•••••••• (already set — leave blank to keep)" if (cfg and cfg.password) else "",
                        help="Never pre-filled for security — leave blank to keep the "
                             "currently saved password unchanged.")
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
                    client_secret = st.text_input(
                        "Client Secret", type="password",
                        placeholder="•••••••• (already set — leave blank to keep)" if (cfg and cfg.client_secret) else "",
                        help="Never pre-filled for security — leave blank to keep unchanged.")

                submitted = st.form_submit_button("💾 Save & Test Connection", type="primary")

            if submitted:
                from core.servicenow import SNConfig, EnterpriseServiceNowConnector, ServiceNowError
                resolved_password = password or (cfg.password if cfg else "")
                resolved_client_secret = client_secret or (cfg.client_secret if cfg else "")
                new_cfg = SNConfig(instance=instance.strip(), user=user.strip(),
                                   password=resolved_password, client_id=client_id.strip(),
                                   client_secret=resolved_client_secret, use_event_api=use_event_api,
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
                is_live_data = result.get("mode") == "live"
                confirm_demo_publish = True  # only overridden below when data is demo
                if not is_live_data:
                    st.error(
                        f"⚠️ **You are currently viewing DEMO/synthetic data**, not "
                        f"live ServiceNow data — this happens automatically after "
                        f"every refresh (the mode resets to demo unless you re-pull "
                        f"live data). Publishing now would create **{len(incidents)} "
                        f"fake tickets** in your real instance "
                        f"`{st.session_state.sn_config.instance}`. Go to the Data "
                        f"Source section below and click **🔄 Pull data from "
                        f"ServiceNow** first if you meant to publish real incidents.")
                    confirm_demo_publish = st.checkbox(
                        "I understand this is demo/synthetic data and want to "
                        "publish it to the real instance anyway.")
                else:
                    st.success(f"🟢 Viewing LIVE data from "
                              f"`{st.session_state.sn_config.instance}` — safe to publish.")

                c1, c2, c3 = st.columns(3)
                do_cmdb = c1.checkbox("Sync CMDB", value=True,
                                      help="Idempotent upsert of topology CIs — safe to re-run.")
                do_close = c2.checkbox("Auto-close remediated", value=True)
                do_lifecycle = c3.checkbox("Read back lifecycle for real MTTR", value=True)

                st.caption(f"Ready to publish **{len(incidents)} incident(s)** and "
                           f"**{len(result['topology'].cis)} CI(s)** to "
                           f"`{st.session_state.sn_config.instance}`.")

                if st.button("🚀 Publish to ServiceNow", type="primary",
                            disabled=not confirm_demo_publish):
                    from core.itsm import ITSMBridge
                    from core.publisher import publish_incidents
                    from core.servicenow import EnterpriseServiceNowConnector
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
                                       f"{pub_result.duration_s}s. See the 🎫 Incidents tab.")
                        else:
                            st.warning(f"Completed with issues: {pub_result.created} created, "
                                       f"{len(pub_result.errors)} error(s). See the 🎫 Incidents tab.")
                        # Auto-refresh: if we're in live mode, re-pull CMDB/alerts now so
                        # the just-published tickets and any lifecycle updates show up
                        # immediately, instead of requiring a second manual pull.
                        if st.session_state.data_source == "live":
                            with st.spinner("Refreshing live data from ServiceNow..."):
                                try:
                                    from pipeline import run_pipeline_live
                                    k8s_cfg = st.session_state.get("k8s_executor_config")
                                    live_exec = None
                                    if k8s_cfg:
                                        from core.k8s_executor import KubernetesExecutor
                                        from core.cluster_inventory import ClusterInventory
                                        inv_records = st.session_state.get("cluster_inventory_records")
                                        inventory = ClusterInventory.from_records(inv_records) if inv_records else None
                                        live_exec = KubernetesExecutor(**k8s_cfg, inventory=inventory)
                                    refreshed = run_pipeline_live(
                                        EnterpriseServiceNowConnector(st.session_state.sn_config),
                                        verbose=False,
                                        api_key=st.session_state.anthropic_key or None,
                                        executor=live_exec,
                                        metrics_source=_build_metrics_source())
                                    st.session_state.live_result = refreshed
                                except Exception:
                                    pass  # publish already succeeded; a stale pull just means click again
                        st.rerun()
                    except Exception as e:
                        bar.empty()
                        st.error(f"Publish failed: {e}")

            if st.session_state.publish_result:
                if st.button("Clear publish results"):
                    st.session_state.publish_result = None
                    st.rerun()

            st.divider()

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
                    def _build_live_executor():
                        k8s_cfg = st.session_state.get("k8s_executor_config")
                        if not k8s_cfg:
                            return None
                        from core.k8s_executor import KubernetesExecutor
                        from core.cluster_inventory import ClusterInventory
                        inv_records = st.session_state.get("cluster_inventory_records")
                        inventory = ClusterInventory.from_records(inv_records) if inv_records else None
                        return KubernetesExecutor(**k8s_cfg, inventory=inventory)

                    cols = st.columns(2)
                    if cols[0].button("🔄 Pull data from ServiceNow", type="primary"):
                        from core.servicenow import EnterpriseServiceNowConnector
                        from pipeline import run_pipeline_live
                        with st.spinner("Pulling CMDB and alerts, running engines..."):
                            try:
                                conn = EnterpriseServiceNowConnector(st.session_state.sn_config)
                                live = run_pipeline_live(
                                    conn, verbose=False,
                                    api_key=st.session_state.anthropic_key or None,
                                    executor=_build_live_executor(),
                                    metrics_source=_build_metrics_source())
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



        with jira_tab:
            st.subheader("Jira Integration — Engineering Backlog Sync")
            st.caption("When Jira is integrated alongside ServiceNow, CloudOps-AI "
                       "auto-files an engineering Jira issue — cross-referenced to "
                       "the ServiceNow ticket number — for any incident that's an "
                       "**automation-backlog gap** (no runbook matched) or has "
                       "**breached its SLA** while still open. Everything else stays "
                       "in ServiceNow only, so Jira doesn't fill up with noise.")

            jcfg = st.session_state.jira_config
            if jcfg:
                st.success(f"Configured: **{jcfg.base_url}** · project "
                           f"**{jcfg.project_key}** · issue type **{jcfg.issue_type}**")
            else:
                st.warning("Not configured — Jira sync runs against an in-memory mock.")

            with st.form("jira_config_form"):
                j1, j2 = st.columns(2)
                with j1:
                    jira_url = st.text_input(
                        "Jira base URL", value=(jcfg.base_url if jcfg else ""),
                        placeholder="https://yourcompany.atlassian.net")
                    jira_email = st.text_input("Atlassian account email",
                                               value=(jcfg.email if jcfg else ""))
                    jira_token = st.text_input(
                        "API token", type="password",
                        placeholder="•••••••• (already set — leave blank to keep)" if (jcfg and jcfg.api_token) else "",
                        help="Generate at id.atlassian.com -> API tokens. Never "
                             "pre-filled — leave blank to keep the saved token.")
                with j2:
                    jira_project = st.text_input("Project key", value=(jcfg.project_key if jcfg else ""),
                                                 placeholder="AIOPS")
                    jira_issue_type = st.text_input("Issue type",
                                                    value=(jcfg.issue_type if jcfg else "Bug"))
                    jira_timeout = st.number_input("Timeout (seconds)", 5.0, 120.0,
                                                   value=(jcfg.timeout_s if jcfg else 15.0), step=5.0)
                jira_submitted = st.form_submit_button("💾 Save & Test Jira Connection", type="primary")

            if jira_submitted:
                from core.jira import JiraConfig, EnterpriseJiraConnector, JiraError
                resolved_token = jira_token or (jcfg.api_token if jcfg else "")
                new_jcfg = JiraConfig(base_url=jira_url.strip().rstrip("/"), email=jira_email.strip(),
                                      api_token=resolved_token, project_key=jira_project.strip(),
                                      issue_type=jira_issue_type.strip() or "Bug",
                                      timeout_s=float(jira_timeout))
                problems = new_jcfg.validate()
                if problems:
                    for p in problems:
                        st.error(p)
                else:
                    with st.spinner("Testing Jira connection..."):
                        try:
                            info = EnterpriseJiraConnector(new_jcfg).test_connection()
                            st.session_state.jira_config = new_jcfg
                            st.session_state.jira_status = info
                            st.success(f"Connected as {info['account']}. Configuration "
                                       f"saved for this session.")
                            st.rerun()
                        except JiraError as e:
                            st.session_state.jira_status = None
                            st.error(f"Connection failed: {e}")
                        except Exception as e:
                            st.error(f"Unexpected error: {e}")

            st.markdown("**Sync engineering backlog to Jira**")
            _pub_for_jira = st.session_state.publish_result
            _tickets_for_jira = (_pub_for_jira.tickets if (_pub_for_jira and _pub_for_jira.tickets)
                                else result["tickets"])
            from core.insights import automation_gaps as _auto_gaps_check
            _gaps_for_jira = _auto_gaps_check(incidents)
            _already_filed = {i.incident_id for i in st.session_state.jira_issues}
            from core.jira import sync_candidates
            _candidates = sync_candidates(_tickets_for_jira, _gaps_for_jira, _already_filed)
            st.caption(f"{len(_candidates)} ticket(s) currently qualify "
                      f"(automation gap or SLA breach) and haven't been filed yet.")

            if st.button("🔧 Sync to Jira", disabled=not _candidates, key="jira_sync_btn"):
                from core.jira import JiraBridge, sync_to_jira
                bar = st.progress(0.0, text="Starting...")

                def on_jira_progress(done, total, label):
                    bar.progress(done / max(total, 1), text=f"{label} ({done}/{total})")

                try:
                    bridge = JiraBridge(jira_config=st.session_state.jira_config)
                    filed = sync_to_jira(bridge, _tickets_for_jira, _gaps_for_jira,
                                         _already_filed, progress_cb=on_jira_progress)
                    st.session_state.jira_issues = st.session_state.jira_issues + filed
                    bar.empty()
                    st.success(f"Filed {len(filed)} of {len(_candidates)} Jira issue(s). "
                              f"See the 🎫 Incidents tab for cross-linked tickets.")
                    st.rerun()
                except Exception as e:
                    bar.empty()
                    st.error(f"Jira sync failed: {e}")

            if st.session_state.jira_issues:
                st.dataframe(pd.DataFrame([{
                    "Jira Key": i.key, "Incident": i.incident_id,
                    "SN Ticket": i.sn_ticket_number, "Reason": i.reason,
                    "Status": i.status, "URL": i.url,
                } for i in st.session_state.jira_issues]),
                    use_container_width=True, hide_index=True)
                if st.button("Clear Jira sync results"):
                    st.session_state.jira_issues = []
                    st.rerun()



        with llm_tab:
            st.subheader("AI Incident Analyst (Phase 7)")
            briefs_now = result.get("briefs", [])
            backend_now = briefs_now[0].backend if briefs_now else "n/a"
            st.caption(f"Current narrative backend: **{backend_now}**. Without a key, "
                       "briefs are generated deterministically from incident structure "
                       "(no hallucination risk). With a key, Claude writes the executive "
                       "summary and RCA narrative, still grounded in the same structured "
                       "data.")
            key_in = st.text_input(
                "Anthropic API key (optional)", type="password",
                placeholder=("•••••••• (already set — leave blank to keep)"
                            if st.session_state.anthropic_key else "sk-ant-..."),
                help="Session-scoped only — never stored, committed, or pre-filled "
                     "back into this field for security.")
            model_in = st.selectbox(
                "Model", ["claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"],
                index=0, help="Haiku is fastest and cheapest for short briefs.")
            kc1, kc2 = st.columns(2)
            if kc1.button("Enable LLM narratives"):
                resolved_key = key_in.strip() or st.session_state.anthropic_key
                if not resolved_key:
                    st.error("Enter a key first.")
                else:
                    from core.ai_agent import AIIncidentAnalyst
                    with st.spinner("Testing key and regenerating briefs..."):
                        analyst = AIIncidentAnalyst(result["topology"],
                                                    api_key=resolved_key,
                                                    model=model_in)
                        new_briefs = analyst.analyze_all(result["incidents"])
                        if new_briefs and new_briefs[0].backend == "llm":
                            st.session_state.anthropic_key = resolved_key
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


    with infra_group:
        with st.container(key="cfg_infra_subtabs"):
            k8s_tab, aws_tab, gcp_tab, azure_tab, onprem_tab, metrics_tab, inv_tab = st.tabs(
                ["☸️ Kubernetes", "🟠 AWS", "🔵 GCP", "🔷 Azure", "🏠 On-Premises",
                 "📈 Metrics Source", "📋 Cluster Inventory"])

        with k8s_tab:
            st.subheader("Real Kubernetes Executor (optional — EXECUTES real commands)")
            st.caption("By default, LIVE mode is **read-only**: Remediation and Change "
                       "Mgmt only log dry-run recommendations, nothing is executed "
                       "anywhere. Configuring this connects a real `kubectl` to a "
                       "real cluster/namespace — matched runbooks (pod restart, "
                       "service restart) will actually run, and only mark an "
                       "incident resolved if a real `kubectl rollout status` check "
                       "confirms it. Runbooks that aren't kubectl-based (disk "
                       "cleanup, node-group scaling, SQL) are honestly refused, not "
                       "faked. **Only point this at a cluster/namespace you control "
                       "and are comfortable letting this app touch — never "
                       "production.**")

            k8s_cfg = st.session_state.get("k8s_executor_config")
            if k8s_cfg:
                st.success(f"Real executor active: namespace **{k8s_cfg['namespace']}** "
                           f"{'(context: ' + k8s_cfg['context'] + ')' if k8s_cfg.get('context') else ''}")
            else:
                st.info("Not configured — LIVE mode remains read-only (dry-run recommendations only).")

            with st.form("k8s_executor_form"):
                kc1, kc2 = st.columns(2)
                with kc1:
                    kubeconfig_path = st.text_input(
                        "Kubeconfig path (blank = default `~/.kube/config` on this host)",
                        value=(k8s_cfg.get("kubeconfig_path") or "") if k8s_cfg else "")
                    k8s_context = st.text_input(
                        "Context (blank = current context)",
                        value=(k8s_cfg.get("context") or "") if k8s_cfg else "")
                with kc2:
                    k8s_namespace = st.text_input(
                        "Namespace", value=(k8s_cfg.get("namespace", "default") if k8s_cfg else "default"))
                    k8s_timeout = st.number_input(
                        "Timeout (seconds)", 10, 600,
                        value=(k8s_cfg.get("timeout_s", 60) if k8s_cfg else 60), step=10)
                confirm_real = st.checkbox(
                    "I understand this will execute real kubectl commands against "
                    "this cluster/namespace, and I control this environment.")
                k8s_submitted = st.form_submit_button("💾 Save & Test Kubernetes Connection",
                                                      type="primary")

            if k8s_submitted:
                if not confirm_real:
                    st.error("Check the confirmation box first — this executes real commands.")
                else:
                    from core.k8s_executor import KubernetesExecutor
                    new_cfg = dict(kubeconfig_path=kubeconfig_path.strip() or None,
                                   context=k8s_context.strip() or None,
                                   namespace=k8s_namespace.strip() or "default",
                                   timeout_s=int(k8s_timeout))
                    with st.spinner("Testing kubectl connection..."):
                        try:
                            info = KubernetesExecutor(**new_cfg).test_connection()
                            st.session_state.k8s_executor_config = new_cfg
                            st.success(f"Connected — namespace **{info['namespace']}**, "
                                      f"context **{info['context']}**. Real execution "
                                      f"is now active for future LIVE data pulls.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Connection failed: {e}")

            if k8s_cfg and st.button("Disable real execution (back to read-only)"):
                st.session_state.k8s_executor_config = None
                st.rerun()


        with aws_tab:
            st.subheader("AWS Account")
            st.caption("Register a dedicated automation IAM identity for this AWS "
                       "account — used to log into EKS clusters listed in the "
                       "Cluster Inventory tab. Same Save & Test pattern as "
                       "ServiceNow/Jira.")

            aws_accts = {n: a for n, a in st.session_state.get("cloud_accounts", {}).items()
                        if a.provider == "aws"}
            if aws_accts:
                st.dataframe(pd.DataFrame([
                    {"Nickname": n, "Region": a.aws_region,
                     "Auth": "Ambient host identity" if a.aws_use_ambient_identity else "Static keys"}
                    for n, a in aws_accts.items()
                ]), use_container_width=True, hide_index=True)

            with st.form("aws_account_form"):
                aa1, aa2 = st.columns(2)
                with aa1:
                    aws_nickname = st.text_input("Nickname", placeholder="prod-aws")
                    aws_region = st.text_input("Default region", placeholder="us-east-1")
                with aa2:
                    aws_ambient = st.checkbox("Use this host's own IAM identity "
                                              "(instance/task role) instead of static keys")
                    aws_key = st.text_input("AWS Access Key ID", disabled=aws_ambient)
                aws_secret = st.text_input(
                    "AWS Secret Access Key", type="password", disabled=aws_ambient,
                    placeholder="•••••••• (leave blank to keep unchanged)",
                    help="Never pre-filled for security.")
                aws_submitted = st.form_submit_button("💾 Save & Test AWS Account", type="primary")

            if aws_submitted:
                if not aws_nickname.strip():
                    st.error("Nickname is required.")
                else:
                    from core.cloud_accounts import CloudAccount
                    existing = aws_accts.get(aws_nickname.strip())
                    resolved_secret = aws_secret or (existing.aws_secret_access_key if existing else "")
                    new_acct = CloudAccount(
                        nickname=aws_nickname.strip(), provider="aws",
                        aws_access_key_id=aws_key, aws_secret_access_key=resolved_secret,
                        aws_region=aws_region.strip(), aws_use_ambient_identity=aws_ambient)
                    with st.spinner("Testing AWS identity..."):
                        try:
                            info = new_acct.test_connection()
                            st.session_state.setdefault("cloud_accounts", {})
                            st.session_state.cloud_accounts[new_acct.nickname] = new_acct
                            st.success(f"Connected — {info['detail'][:150]}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Connection failed: {e}")

            if aws_accts and st.button("Remove an AWS account", key="rm_aws"):
                pick = st.selectbox("Which one?", list(aws_accts.keys()), key="rm_aws_pick")
                if st.button("Confirm remove", key="rm_aws_confirm"):
                    del st.session_state.cloud_accounts[pick]
                    st.rerun()

        with gcp_tab:
            st.subheader("GCP Project")
            st.caption("Register a dedicated automation service account for this "
                       "GCP project — used to log into GKE clusters listed in the "
                       "Cluster Inventory tab.")

            gcp_accts = {n: a for n, a in st.session_state.get("cloud_accounts", {}).items()
                        if a.provider == "gcp"}
            if gcp_accts:
                st.dataframe(pd.DataFrame([
                    {"Nickname": n, "Project": a.gcp_project,
                     "Auth": "Service account key" if a.gcp_service_account_key_json else "Ambient gcloud identity"}
                    for n, a in gcp_accts.items()
                ]), use_container_width=True, hide_index=True)

            with st.form("gcp_account_form"):
                gg1, gg2 = st.columns(2)
                with gg1:
                    gcp_nickname = st.text_input("Nickname", placeholder="checkout-gcp")
                with gg2:
                    gcp_project = st.text_input("GCP Project ID")
                gcp_key_file = st.file_uploader(
                    "Service account key JSON (leave empty to use ambient "
                    "`gcloud` identity on this host)", type=["json"],
                    help="Uploaded as a file, never shown as visible/copyable text.")
                gcp_submitted = st.form_submit_button("💾 Save & Test GCP Account", type="primary")

            if gcp_submitted:
                if not gcp_nickname.strip():
                    st.error("Nickname is required.")
                else:
                    from core.cloud_accounts import CloudAccount
                    existing = gcp_accts.get(gcp_nickname.strip())
                    key_json = (gcp_key_file.getvalue().decode("utf-8") if gcp_key_file
                               else (existing.gcp_service_account_key_json if existing else ""))
                    new_acct = CloudAccount(
                        nickname=gcp_nickname.strip(), provider="gcp",
                        gcp_service_account_key_json=key_json, gcp_project=gcp_project.strip())
                    with st.spinner("Testing GCP identity..."):
                        try:
                            info = new_acct.test_connection()
                            st.session_state.setdefault("cloud_accounts", {})
                            st.session_state.cloud_accounts[new_acct.nickname] = new_acct
                            st.success(f"Connected — {info['detail'][:150]}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Connection failed: {e}")

            if gcp_accts and st.button("Remove a GCP account", key="rm_gcp"):
                pick = st.selectbox("Which one?", list(gcp_accts.keys()), key="rm_gcp_pick")
                if st.button("Confirm remove", key="rm_gcp_confirm"):
                    del st.session_state.cloud_accounts[pick]
                    st.rerun()

        with azure_tab:
            st.subheader("Azure Subscription")
            st.caption("Register a dedicated service principal or managed identity "
                       "for this Azure subscription — used to log into AKS "
                       "clusters listed in the Cluster Inventory tab.")

            az_accts = {n: a for n, a in st.session_state.get("cloud_accounts", {}).items()
                       if a.provider == "azure"}
            if az_accts:
                st.dataframe(pd.DataFrame([
                    {"Nickname": n, "Subscription": a.azure_subscription_id,
                     "Auth": "Service principal" if a.azure_client_id else "Ambient managed identity"}
                    for n, a in az_accts.items()
                ]), use_container_width=True, hide_index=True)

            with st.form("azure_account_form"):
                az1, az2 = st.columns(2)
                with az1:
                    az_nickname = st.text_input("Nickname", placeholder="prod-azure")
                    az_tenant = st.text_input("Tenant ID")
                with az2:
                    az_sub = st.text_input("Subscription ID")
                    az_client = st.text_input("Service Principal Client ID (blank = "
                                              "use ambient managed identity)")
                az_secret = st.text_input(
                    "Service Principal Client Secret", type="password",
                    placeholder="•••••••• (leave blank to keep unchanged)",
                    help="Never pre-filled for security.")
                az_submitted = st.form_submit_button("💾 Save & Test Azure Account", type="primary")

            if az_submitted:
                if not az_nickname.strip():
                    st.error("Nickname is required.")
                else:
                    from core.cloud_accounts import CloudAccount
                    existing = az_accts.get(az_nickname.strip())
                    resolved_secret = az_secret or (existing.azure_client_secret if existing else "")
                    new_acct = CloudAccount(
                        nickname=az_nickname.strip(), provider="azure",
                        azure_tenant_id=az_tenant.strip(), azure_client_id=az_client.strip(),
                        azure_client_secret=resolved_secret, azure_subscription_id=az_sub.strip())
                    with st.spinner("Testing Azure identity..."):
                        try:
                            info = new_acct.test_connection()
                            st.session_state.setdefault("cloud_accounts", {})
                            st.session_state.cloud_accounts[new_acct.nickname] = new_acct
                            st.success(f"Connected — {info['detail'][:150]}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Connection failed: {e}")

            if az_accts and st.button("Remove an Azure account", key="rm_az"):
                pick = st.selectbox("Which one?", list(az_accts.keys()), key="rm_az_pick")
                if st.button("Confirm remove", key="rm_az_confirm"):
                    del st.session_state.cloud_accounts[pick]
                    st.rerun()

        with onprem_tab:
            st.subheader("On-Premises / Self-Managed Clusters")
            st.caption("No cloud IAM login step applies here — an on-prem or "
                       "self-managed cluster (bare-metal, VMware, kubeadm, "
                       "k3s, etc.) is reached the same way as any kubectl "
                       "context: whatever cluster-issued client certificate, "
                       "static token, or OIDC config is already in your "
                       "kubeconfig. Set `provider` to `self-managed` and "
                       "leave `account_nickname` blank on its Cluster "
                       "Inventory row — 'Login all targets' will skip it "
                       "and only 'Validate inventory contexts' applies.")

            onprem_context = st.text_input(
                "Context to test", placeholder="on-prem-cluster-context",
                key="onprem_test_context",
                help="Any context name already present in your local kubeconfig.")
            if st.button("🔍 Test this context", key="onprem_test_btn"):
                import subprocess
                try:
                    args = ["kubectl", "cluster-info"]
                    if onprem_context.strip():
                        args += ["--context", onprem_context.strip()]
                    proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
                    if proc.returncode == 0:
                        st.success(f"Reachable — {(proc.stdout or '').strip()[:200]}")
                    else:
                        st.error((proc.stderr or proc.stdout or "unknown error")[:400])
                except FileNotFoundError:
                    st.error("kubectl not found on this host.")
                except Exception as e:
                    st.error(f"Test failed: {e}")

        with metrics_tab:
            st.subheader("Metrics Source (closes the LIVE-mode Telemetry gap)")
            st.caption("ServiceNow has no metric time-series — this is what "
                       "actually populates the 📈 Telemetry tab and real "
                       "anomaly detection in LIVE mode. Pick one backend, "
                       "Save & Test, and every future 'Pull data from "
                       "ServiceNow' will fetch real series for each CMDB CI "
                       "and run the same anomaly detector demo mode uses.")

            prom_sub, dd_sub, dt_sub, graf_sub = st.tabs(
                ["Prometheus", "Datadog", "Dynatrace", "Grafana"])

            active = st.session_state.get("metrics_source_config")
            if active:
                st.success(f"Active source: **{active['provider']}**")
                if st.button("Disable metrics source (back to no telemetry in LIVE mode)"):
                    st.session_state.metrics_source_config = None
                    st.rerun()

            with prom_sub:
                with st.form("prom_metrics_form"):
                    prom_url = st.text_input("Prometheus base URL",
                                             placeholder="http://prometheus.internal:9090")
                    prom_token = st.text_input("Bearer token (optional)", type="password")
                    prom_submit = st.form_submit_button("💾 Save & Test Prometheus", type="primary")
                if prom_submit:
                    from core.metrics_sources import PrometheusSource
                    src = PrometheusSource(base_url=prom_url.strip(), bearer_token=prom_token)
                    try:
                        info = src.test_connection()
                        st.session_state.metrics_source_config = {
                            "provider": "prometheus", "base_url": prom_url.strip(),
                            "bearer_token": prom_token}
                        st.success(f"Connected — {info}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Connection failed: {e}")

            with dd_sub:
                with st.form("dd_metrics_form"):
                    dd_api = st.text_input("Datadog API key", type="password")
                    dd_app = st.text_input("Datadog Application key", type="password")
                    dd_site = st.text_input("Site", value="datadoghq.com",
                                            help="datadoghq.com (US1), datadoghq.eu (EU), etc.")
                    dd_submit = st.form_submit_button("💾 Save & Test Datadog", type="primary")
                if dd_submit:
                    from core.metrics_sources import DatadogSource
                    src = DatadogSource(api_key=dd_api, app_key=dd_app, site=dd_site.strip())
                    try:
                        info = src.test_connection()
                        st.session_state.metrics_source_config = {
                            "provider": "datadog", "api_key": dd_api, "app_key": dd_app,
                            "site": dd_site.strip()}
                        st.success(f"Connected — {info}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Connection failed: {e}")

            with dt_sub:
                with st.form("dt_metrics_form"):
                    dt_url = st.text_input("Dynatrace environment URL",
                                           placeholder="https://abc12345.live.dynatrace.com")
                    dt_token = st.text_input("API token", type="password")
                    dt_submit = st.form_submit_button("💾 Save & Test Dynatrace", type="primary")
                if dt_submit:
                    from core.metrics_sources import DynatraceSource
                    src = DynatraceSource(base_url=dt_url.strip(), api_token=dt_token)
                    try:
                        info = src.test_connection()
                        st.session_state.metrics_source_config = {
                            "provider": "dynatrace", "base_url": dt_url.strip(),
                            "api_token": dt_token}
                        st.success(f"Connected — {info}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Connection failed: {e}")

            with graf_sub:
                st.caption("Proxies PromQL through a Grafana-managed Prometheus "
                          "datasource — useful when Grafana is reachable but "
                          "Prometheus itself isn't.")
                with st.form("graf_metrics_form"):
                    graf_url = st.text_input("Grafana base URL",
                                             placeholder="https://grafana.internal")
                    graf_key = st.text_input("API key / service account token", type="password")
                    graf_ds_uid = st.text_input("Prometheus datasource UID",
                                                help="Found in Grafana under Connections -> Data sources")
                    graf_submit = st.form_submit_button("💾 Save & Test Grafana", type="primary")
                if graf_submit:
                    from core.metrics_sources import GrafanaSource
                    src = GrafanaSource(base_url=graf_url.strip(), api_key=graf_key,
                                        datasource_uid=graf_ds_uid.strip())
                    try:
                        info = src.test_connection()
                        st.session_state.metrics_source_config = {
                            "provider": "grafana", "base_url": graf_url.strip(),
                            "api_key": graf_key, "datasource_uid": graf_ds_uid.strip()}
                        st.success(f"Connected — {info}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Connection failed: {e}")

            with st.expander("Query templates (advanced — edit if your label schema differs)"):
                from core.metrics_sources import DEFAULT_PROMQL_TEMPLATES
                st.caption("One query template per metric, with `{ci}` substituted "
                          "for each CMDB CI's name at fetch time. Defaults assume "
                          "node_exporter + generic app instrumentation conventions.")
                st.json(st.session_state.get("metric_query_templates", DEFAULT_PROMQL_TEMPLATES))

        with inv_tab:
            st.markdown("#### Cluster/Namespace Inventory (multi-cluster resolution)")
            st.caption("Maps each incident's root-cause CI or business service to the "
                       "**specific** cluster/namespace/kubeconfig context it belongs "
                       "to — required for AWS/GCP/Azure environments with more than "
                       "one cluster. Each row's `kube_context` must already exist in "
                       "your kubeconfig (set up ahead of time via `aws eks "
                       "update-kubeconfig`, `gcloud container clusters "
                       "get-credentials`, or `az aks get-credentials`, using a "
                       "dedicated least-privilege automation identity — CloudOps-AI "
                       "does not perform cloud IAM login itself, it only selects "
                       "and uses the resulting context). Incidents that don't match "
                       "any row are refused, never guessed.")

            from core.cluster_inventory import ClusterInventory, CSV_TEMPLATE
            st.download_button("⬇️ Download CSV template", CSV_TEMPLATE,
                               file_name="cluster_inventory_template.csv", mime="text/csv")

            inv_records = st.session_state.get("cluster_inventory_records")
            inv_file = st.file_uploader("Upload cluster inventory (CSV)", type=["csv"],
                                        key="cluster_inv_upload")
            if inv_file is not None:
                file_fingerprint = f"{inv_file.name}:{inv_file.size}"
                if st.session_state.get("_last_processed_inv_file") != file_fingerprint:
                    try:
                        inv = ClusterInventory.from_csv(inv_file.getvalue().decode("utf-8"))
                        st.session_state.cluster_inventory_records = inv.to_rows()
                        st.session_state._last_processed_inv_file = file_fingerprint
                        st.success(f"Loaded {len(inv.targets)} cluster target(s).")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Couldn't parse that CSV: {e}")

            if inv_records:
                st.dataframe(pd.DataFrame(inv_records), use_container_width=True, hide_index=True)
                vc1, vc2, vc3 = st.columns(3)
                with vc1:
                    if st.button("🔍 Validate inventory contexts"):
                        import subprocess
                        try:
                            proc = subprocess.run(["kubectl", "config", "get-contexts", "-o", "name"],
                                                  capture_output=True, text=True, timeout=15)
                            known = set((proc.stdout or "").splitlines())
                            missing = [r["match"] for r in inv_records if r["kube_context"] not in known]
                            if missing:
                                st.warning(f"{len(missing)} row(s) reference a kube_context not "
                                          f"found locally: {', '.join(missing)}. Run the matching "
                                          f"`aws eks update-kubeconfig` / `gcloud ... get-credentials` "
                                          f"/ `az aks get-credentials` first, or use 'Login all "
                                          f"targets' below if a Cloud Account is registered.")
                            else:
                                st.success(f"All {len(inv_records)} kube_context(s) found locally.")
                        except FileNotFoundError:
                            st.error("kubectl not found on this host — install it to validate contexts.")
                        except Exception as e:
                            st.error(f"Validation failed: {e}")
                with vc2:
                    if st.button("🔑 Login all targets (Method 2)",
                                help="Uses each row's account_nickname to look up a "
                                     "registered Cloud Account and actually perform "
                                     "cloud login for that cluster."):
                        from core.cluster_inventory import ClusterInventory
                        from core.cloud_accounts import CloudAccountRegistry
                        inv = ClusterInventory.from_records(inv_records)
                        registry = CloudAccountRegistry(list(st.session_state.get("cloud_accounts", {}).values()))
                        with st.spinner("Logging into each cluster target..."):
                            results = registry.login_all(inv.targets)
                        n_ok = sum(1 for _, ok, _ in results if ok)
                        st.success(f"{n_ok}/{len(results)} logged in successfully.") if n_ok == len(results) \
                            else st.warning(f"{n_ok}/{len(results)} logged in successfully.")
                        for match, ok, msg in results:
                            (st.success if ok else st.error)(f"{match}: {msg}")
                with vc3:
                    if st.button("Clear inventory"):
                        st.session_state.cluster_inventory_records = None
                        st.rerun()
            else:
                st.info("No inventory loaded — real execution falls back to the single "
                        "kubeconfig context/namespace configured above, and refuses to "
                        "act on any incident once an inventory exists but doesn't "
                        "match it.")


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
    _pub_oncall = st.session_state.publish_result
    tickets_by_id = {t.incident_id: t for t in
                     (_pub_oncall.tickets if (_pub_oncall and _pub_oncall.tickets)
                      else result["tickets"])}
    jira_by_id = {j.incident_id: j for j in st.session_state.jira_issues}
    gaps = automation_gaps(incidents)
    gap_ids = {g.incident_id for g in gaps}

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

            _tk = tickets_by_id.get(inc.incident_id)
            _jr = jira_by_id.get(inc.incident_id)
            if _tk:
                from core.jira import sla_breached as _sla_breached_check
                _breach = _sla_breached_check(_tk)
                badge = (f"🎫 SN **{_tk.number}** ({_tk.state}) · "
                        f"SLA {'🔴 breach' if _breach else '🟢 on track'}")
                if _jr:
                    badge += f" · 🔧 Jira **[{_jr.key}]({_jr.url})**"
                elif _breach or inc.incident_id in gap_ids:
                    badge += " · 🟡 qualifies for Jira, not yet synced"
                st.caption(badge)

            pm = postmortem_markdown(inc, brief=briefs_by_id.get(inc.incident_id),
                                     remediation=rem_by_id.get(inc.incident_id),
                                     topology=result["topology"],
                                     ticket=_tk, jira_issue=_jr)
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
    if gaps:
        total_toil = sum(g.est_annual_toil_hours for g in gaps)
        st.warning(f"{len(gaps)} incident pattern(s) had no matching runbook — "
                   f"an estimated **{total_toil:.0f} hours/year** of manual toil.")
        st.dataframe(pd.DataFrame([{
            "Incident": g.incident_id, "Metrics": ", ".join(g.metrics),
            "Severity": g.severity, "Root cause": g.root_cause,
            "Suggested runbook": g.suggested_runbook,
            "Est. annual toil (h)": g.est_annual_toil_hours,
            "Jira": (jira_by_id[g.incident_id].key if g.incident_id in jira_by_id
                    else "🟡 not filed"),
        } for g in gaps]), use_container_width=True, hide_index=True)
        _unsynced_gaps = [g for g in gaps if g.incident_id not in jira_by_id]
        if _unsynced_gaps:
            st.caption(f"{len(_unsynced_gaps)} of {len(gaps)} gap(s) not yet in Jira — "
                      f"sync from **⚙️ Config → Jira Integration**.")
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
        from core.slo import slos_for_incidents
        is_live = result.get("mode") == "live"
        has_business_service = any(i.business_service for i in incidents)
        base_slos = slos_for_incidents(incidents) if is_live else DEFAULT_SLOS
        if is_live and not has_business_service:
            st.caption("No business_service set on any live incident yet — "
                      "falling back to demo SLO names until real data has one.")
        custom_slos = [SLO(name=s.name, business_service=s.business_service,
                           target_pct=target, window_days=window_days,
                           severity_counts=s.severity_counts)
                       for s in base_slos]

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
    st.subheader("ITSM & Jira Sync Status")
    st.caption("Same live ticket/Jira data as the 🎫 Incidents tab and 🔧 Jira sync in "
               "⚙️ Config — surfaced here for management visibility, not "
               "recomputed separately.")
    from core.jira import sla_breached as _exec_sla_breached
    _pub_exec = st.session_state.publish_result
    _tickets_exec = (_pub_exec.tickets if (_pub_exec and _pub_exec.tickets)
                     else result["tickets"])
    _jira_exec = st.session_state.jira_issues
    _breaches_exec = [t for t in _tickets_exec if _exec_sla_breached(t)]
    _gaps_exec = automation_gaps(incidents)
    _filed_ids_exec = {j.incident_id for j in _jira_exec}
    _pending_exec = [g for g in _gaps_exec if g.incident_id not in _filed_ids_exec] + \
                    [t for t in _breaches_exec if t.incident_id not in _filed_ids_exec]
    _pending_unique = {getattr(x, "incident_id") for x in _pending_exec}

    x1, x2, x3, x4 = st.columns(4)
    x1.metric("Open tickets", sum(1 for t in _tickets_exec if t.state != "Resolved"))
    x2.metric("SLA breaches", len(_breaches_exec))
    x3.metric("Filed in Jira", len(_jira_exec))
    x4.metric("Pending Jira sync", len(_pending_unique),
             delta=None if not _pending_unique else f"-{len(_pending_unique)}",
             delta_color="inverse")
    if _pending_unique:
        st.info(f"{len(_pending_unique)} incident(s) qualify for engineering "
               f"backlog (automation gap or SLA breach) but haven't been "
               f"synced to Jira yet. Sync from **⚙️ Config → Jira Integration**.")

    st.divider()
    st.subheader("Export")
    report_md = exec_report_markdown(
        kpi, stats, statuses, scorecard, automation_gaps(incidents),
        noise_hotspots(result["raw_alerts"]),
        period_label=("live ServiceNow data" if result.get("mode") == "live"
                      else "demo analysis window"),
        jira_issues=_jira_exec, sla_breach_count=len(_breaches_exec))
    st.download_button("📊 Download reliability briefing (Markdown)", report_md,
                       file_name="reliability_briefing.md", mime="text/markdown")
    with st.expander("Preview"):
        st.markdown(report_md)

# ---------------- SLI / SLO / SLA chain ----------------
with tab_slx:
    from core.sli import SLICalculator, SLIDefinition, DEFAULT_SLIS
    from core.slo import SLO, SLOEngine
    from core.sla import SLA, SLAEngine, CreditTier

    st.subheader("The Reliability Chain")
    st.caption("**SLI** is what you measure · **SLO** is your internal target · "
               "**SLA** is the external commitment with money attached. The gap "
               "between SLO and SLA is your safety margin: you should breach the "
               "objective long before the contract.")

    calc = SLICalculator()

    # ---- 1. SLI ----
    st.markdown("### 1️⃣ SLI — Service Level Indicators (measured)")
    sli_results = []
    series_map = result.get("series", {})

    for d in DEFAULT_SLIS:
        if d.metric and (d.ci_id, d.metric) in series_map:
            sli_results.append(calc.from_series(d, series_map[(d.ci_id, d.metric)]))
        elif not d.metric:
            sli_results.append(calc.from_incidents(
                d, incidents, window_h=2.0,
                business_service=None))  # platform-wide, not one hardcoded service

    if not sli_results:
        st.warning("No SLIs computable — metric series unavailable in this mode.")
    else:
        cols = st.columns(len(sli_results))
        for col, r in zip(cols, sli_results):
            col.metric(r.definition.name, f"{r.ratio_pct}%",
                       delta=f"{r.bad_events} bad / {r.valid_events}",
                       delta_color="inverse")
        st.dataframe(pd.DataFrame([{
            "SLI": r.definition.name, "Kind": r.definition.kind,
            "Definition": r.headline, "Good": r.good_events,
            "Valid": r.valid_events, "Ratio": f"{r.ratio_pct}%",
            "Source": r.source,
        } for r in sli_results]), use_container_width=True, hide_index=True)

    st.divider()

    # ---- 2. SLO ----
    st.markdown("### 2️⃣ SLO — Service Level Objective (internal target)")
    availability_sli = next((r for r in sli_results
                             if r.source == "incident_timeline"), None) \
        or (sli_results[0] if sli_results else None)

    c1, c2 = st.columns(2)
    slo_target = c1.slider("SLO target (%)", 99.0, 99.99, 99.9, 0.01,
                           key="slx_slo_target")
    slo_window = c2.selectbox("SLO window (days)", [7, 30, 90], index=1,
                              key="slx_slo_window")

    if availability_sli:
        slo = SLO(name="Payments availability",
                  business_service="Payments Platform",
                  target_pct=slo_target, window_days=slo_window)
        slo_status = SLOEngine().evaluate_from_sli(slo, availability_sli)
        color = {"HEALTHY": "🟢", "AT RISK": "🟡", "SLOW BURN": "🟠",
                 "FAST BURN": "🔴", "EXHAUSTED": "⛔"}[slo_status.status]
        with st.container(border=True):
            st.markdown(f"#### {color} {slo.name} — {slo_status.status}")
            m = st.columns(4)
            m[0].metric("Target", f"{slo.target_pct}%")
            m[1].metric("Measured (SLI)", f"{availability_sli.ratio_pct}%")
            m[2].metric("Budget used", f"{slo_status.consumed_pct}%")
            m[3].metric("Burn rate", f"{slo_status.burn_rate}×")
            st.progress(min(slo_status.consumed_pct / 100, 1.0),
                        text=f"{slo_status.remaining_minutes:.0f} of "
                             f"{slo_status.budget_minutes:.0f} error-budget "
                             f"minutes remaining")
            st.info(f"**Action:** {slo_status.action}")
            st.caption(f"Backed by SLI: *{availability_sli.definition.name}* "
                       f"({availability_sli.source})")

    st.divider()

    # ---- 3. SLA ----
    st.markdown("### 3️⃣ SLA — Service Level Agreement (contractual)")
    s1, s2, s3 = st.columns(3)
    sla_commit = s1.slider("SLA commitment (%)", 95.0, 99.99, 99.5, 0.05,
                           key="slx_sla_commit")
    contract_value = s2.number_input("Monthly contract value ($)",
                                     1000, 5_000_000, 50_000, 1000,
                                     key="slx_contract")
    excluded = s3.number_input("Excluded minutes (planned maintenance)",
                               0, 10_000, 0, 10, key="slx_excluded")

    if slo_target <= sla_commit:
        st.error(f"⚠️ Your SLO target ({slo_target}%) is not tighter than the SLA "
                 f"commitment ({sla_commit}%). The internal objective must be "
                 f"stricter than the contract, or you get no early warning "
                 f"before breaching it.")

    if availability_sli:
        sla = SLA(name="Payments Platform — Enterprise tier",
                  customer="Enterprise customers",
                  business_service="Payments Platform",
                  commitment_pct=sla_commit,
                  monthly_contract_value=float(contract_value),
                  excluded_minutes=float(excluded))
        sla_status = SLAEngine([sla]).evaluate(availability_sli,
                                               linked_slo_target=slo_target)[0]
        icon = {"MEETING": "🟢", "WATCH": "🟡", "AT RISK": "🟠",
                "BREACHED": "🔴"}[sla_status.status]
        with st.container(border=True):
            st.markdown(f"#### {icon} {sla.name} — {sla_status.status}")
            m = st.columns(4)
            m[0].metric("Commitment", f"{sla.commitment_pct}%")
            m[1].metric("Achieved", f"{sla_status.achieved_pct}%")
            m[2].metric("Downtime headroom",
                        f"{sla_status.headroom_minutes:.0f} min")
            m[3].metric("Credit exposure",
                        f"${sla_status.financial_exposure:,.0f}",
                        delta=f"{sla_status.credit_pct:.0f}% credit"
                        if sla_status.credit_pct else "no credit owed",
                        delta_color="inverse")
            if sla_status.breached:
                st.error(f"**Action:** {sla_status.action}")
            else:
                st.success(f"**Action:** {sla_status.action}")

            buffer = sla_status.slo_buffer_pct
            if buffer is not None:
                st.caption(f"Safety margin: SLO is **{buffer:.2f} percentage "
                           f"points** tighter than the SLA — the buffer that "
                           f"lets you react before the contract is at risk.")

        st.markdown("**Service credit tiers**")
        st.dataframe(pd.DataFrame([{
            "If achieved falls below": f"{t.threshold_pct}%",
            "Service credit": f"{t.credit_pct}%",
            "Credit value": f"${sla.monthly_contract_value * t.credit_pct / 100:,.0f}",
            "Tier": t.label,
            "Currently applicable": "✅" if (sla_status.credit_tier and
                                            sla_status.credit_tier.threshold_pct
                                            == t.threshold_pct) else "",
        } for t in sla.credit_tiers]), use_container_width=True, hide_index=True)

    with st.expander("How these three relate"):
        st.markdown(
            "- **SLI** — a ratio of good events to valid events, computed from "
            "telemetry (`p99 latency < 500ms`) or the incident timeline "
            "(`minutes free of critical incidents`). It is a fact, not a goal.\n"
            "- **SLO** — a target on that SLI (`99.9% over 30 days`). Breaching "
            "it consumes error budget and triggers *engineering* decisions: "
            "freeze releases, prioritise reliability work.\n"
            "- **SLA** — a looser, contractual version of the same measurement, "
            "with service credits attached. Breaching it triggers *commercial* "
            "consequences.\n\n"
            "The ordering matters: **SLI ≥ SLO target > SLA commitment**. If the "
            "SLO isn't tighter than the SLA, the first warning you get is a "
            "customer invoice credit.")

# ---------------- Reports & Delivery ----------------
with tab_reports:
    from core.export import export_excel, export_pdf
    from core.insights import automation_gaps, noise_hotspots, service_scorecard
    from core.report_periods import build_bundle, build_period
    from core.scheduler import ReportSubscription, SMTPConfig, send_report_email
    from core.sla import SLA, SLAEngine
    from core.sli import DEFAULT_SLIS, SLICalculator
    from core.slo import SLO, SLOEngine

    st.subheader("📤 Reports & Delivery")
    st.caption("Generate a management-ready PDF or Excel report for any period, "
               "or configure a standing email subscription.")

    def _build_current_bundle(period_type: str, custom_start=None, custom_end=None):
        """Builds a ReportBundle from whatever data is currently loaded (`result`)."""
        period = build_period(period_type, custom_start=custom_start, custom_end=custom_end)
        calc = SLICalculator()
        sli_results = []
        for d in DEFAULT_SLIS:
            if d.metric and (d.ci_id, d.metric) in result.get("series", {}):
                sli_results.append(calc.from_series(d, result["series"][(d.ci_id, d.metric)]))
            elif not d.metric:
                sli_results.append(calc.from_incidents(
                    d, incidents, window_h=2.0, business_service=None))

        availability_sli = next((r for r in sli_results if r.source == "incident_timeline"),
                                sli_results[0] if sli_results else None)
        slo_statuses, sla_statuses = [], []
        if availability_sli:
            slo = SLO(name="Payments availability", business_service="Payments Platform",
                      target_pct=99.9, window_days=period.days or 1)
            slo_statuses = [SLOEngine().evaluate_from_sli(slo, availability_sli)]
            sla = SLA(name="Payments Platform — Enterprise tier", customer="Enterprise customers",
                      business_service="Payments Platform", commitment_pct=99.5)
            sla_statuses = SLAEngine([sla]).evaluate(availability_sli, linked_slo_target=99.9)

        scorecard = service_scorecard(incidents)
        gaps = automation_gaps(incidents)
        hotspots = noise_hotspots(result.get("raw_alerts", []))
        return build_bundle(period=period, kpi=kpi, stats=stats,
                            slo_statuses=slo_statuses, sla_statuses=sla_statuses,
                            sli_results=sli_results, scorecard=scorecard,
                            gaps=gaps, hotspots=hotspots)

    st.markdown("### 1️⃣ Generate a report")

    REPORT_TYPES = {
        "Full Reliability Report": "bundle",
        "Executive Briefing": "markdown",
        "Incident Postmortem": "markdown",
        "SLA Compliance": "bundle",
        "Alert Funnel / Noise": "bundle",
        "Incident Ticket Summary": "markdown",
    }
    report_choice = st.selectbox(
        "Report type", list(REPORT_TYPES.keys()), key="rep_type_choice",
        help="Pick which report to build, then export it below. Tickets and "
             "incidents feed this from the same data as the ITSM and On-Call "
             "tabs — building it there loads it here automatically too.")
    report_kind = REPORT_TYPES[report_choice]
    _STD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}

    def _filtered_bundle(period_type, custom_start=None, custom_end=None, only=None):
        """Builds the full bundle, then optionally blanks out every section
        except `only` (one of 'sla', 'funnel') so export_pdf/export_excel
        render a single-topic report using the same renderers."""
        b = _build_current_bundle(period_type, custom_start, custom_end)
        if only == "sla":
            b.slo_statuses, b.scorecard, b.gaps, b.hotspots = [], [], [], []
        elif only == "funnel":
            b.slo_statuses, b.sla_statuses, b.scorecard, b.gaps = [], [], [], []
        return b

    def _period_picker(key_prefix: str):
        """Renders period-type + (standard window OR From/To dates) and
        returns (period_type, custom_start, custom_end)."""
        c1, c2 = st.columns(2)
        period_type = c1.selectbox(
            "Report period", ["daily", "weekly", "monthly", "quarterly", "custom"],
            index=1, key=f"{key_prefix}_period_choice")
        custom_start = custom_end = None
        if period_type == "custom":
            today = datetime.now(timezone.utc).date()
            with c2:
                d1, d2 = st.columns(2)
                from_date = d1.date_input("From", value=today - timedelta(days=14),
                                          max_value=today, key=f"{key_prefix}_from")
                to_date = d2.date_input("To", value=today, max_value=today,
                                        key=f"{key_prefix}_to")
            if from_date >= to_date:
                st.error("**From** date must be before **To** date.")
                return period_type, None, None
            custom_start = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
            custom_end = datetime.combine(to_date, datetime.min.time(), tzinfo=timezone.utc)
        else:
            c2.caption(f"Standard window: **{_STD_DAYS[period_type]} days**")
        return period_type, custom_start, custom_end

    if report_choice == "Incident Postmortem":
        if not incidents:
            st.info("No incidents in the current dataset to report on.")
        else:
            inc_pick = st.selectbox(
                "Incident", incidents, key="rep_inc_pick",
                format_func=lambda i: f"{i.incident_id} · {i.title}")
            if st.button("🔄 Build postmortem", key="rep_build_pm"):
                from core.reports import postmortem_markdown
                briefs_by_id = {b.incident_id: b for b in result.get("briefs", [])}
                rem_by_id = {r.incident_id: r for r in result.get("remediations", [])}
                _pub_pm = st.session_state.publish_result
                _tickets_pm = (_pub_pm.tickets if (_pub_pm and _pub_pm.tickets)
                              else result["tickets"])
                tickets_by_id_pm = {t.incident_id: t for t in _tickets_pm}
                jira_by_id_pm = {j.incident_id: j for j in st.session_state.jira_issues}
                st.session_state["rep_markdown"] = postmortem_markdown(
                    inc_pick, brief=briefs_by_id.get(inc_pick.incident_id),
                    remediation=rem_by_id.get(inc_pick.incident_id),
                    topology=result["topology"],
                    ticket=tickets_by_id_pm.get(inc_pick.incident_id),
                    jira_issue=jira_by_id_pm.get(inc_pick.incident_id))
                st.session_state["rep_markdown_name"] = f"postmortem_{inc_pick.incident_id}.md"

    elif report_choice == "Executive Briefing":
        period_choice, c_start, c_end = _period_picker("rep_exec")
        if st.button("🔄 Build executive briefing", key="rep_build_exec"):
            from core.reports import exec_report_markdown
            from core.jira import sla_breached as _rep_sla_breached
            b = _build_current_bundle(period_choice, c_start, c_end)
            _pub_rep = st.session_state.publish_result
            _tickets_rep = (_pub_rep.tickets if (_pub_rep and _pub_rep.tickets)
                           else result["tickets"])
            _breach_count_rep = sum(1 for t in _tickets_rep if _rep_sla_breached(t))
            st.session_state["rep_markdown"] = exec_report_markdown(
                b.kpi, b.stats, b.slo_statuses, b.scorecard, b.gaps,
                hotspots=b.hotspots, period_label=b.period.label,
                jira_issues=st.session_state.jira_issues,
                sla_breach_count=_breach_count_rep)
            st.session_state["rep_markdown_name"] = "CloudOps-AI_Executive_Briefing.md"

    elif report_choice == "Incident Ticket Summary":
        _pub = st.session_state.publish_result
        _tickets = _pub.tickets if (_pub and _pub.tickets) else result["tickets"]
        st.caption(f"{len(_tickets)} ticket(s) currently loaded "
                  f"({'LIVE ServiceNow' if _pub and _pub.tickets else 'simulated'}).")
        if st.button("🔄 Build ITSM ticket summary", key="rep_build_itsm"):
            from core.reports import itsm_report_markdown
            st.session_state["rep_markdown"] = itsm_report_markdown(
                _tickets, jira_issues=st.session_state.jira_issues)
            st.session_state["rep_markdown_name"] = "CloudOps-AI_ITSM_Ticket_Summary.md"

    else:  # bundle-backed: Full Reliability Report, SLA Compliance, Alert Funnel / Noise
        period_choice, c_start, c_end = _period_picker("rep")
        _only = {"SLA Compliance": "sla", "Alert Funnel / Noise": "funnel"}.get(report_choice)
        if st.button("🔄 Build report bundle", key="rep_build_btn"):
            st.session_state["rep_bundle"] = _filtered_bundle(
                period_choice, c_start, c_end, only=_only)
            st.session_state["rep_markdown"] = None

    # ---- Downloads ----
    if report_kind == "markdown":
        md = st.session_state.get("rep_markdown")
        if md:
            st.success("Report ready.")
            st.download_button("⬇️ Download Markdown", md,
                               file_name=st.session_state.get("rep_markdown_name", "report.md"),
                               mime="text/markdown", key="rep_dl_md")
            with st.expander("Preview"):
                st.markdown(md)
        else:
            st.info("Click **Build** above to generate this report.")
    else:
        bundle = st.session_state.get("rep_bundle")
        if bundle:
            st.success(f"Bundle ready — {bundle.period.label} ({bundle.period.range_str})")
            dl1, dl2 = st.columns(2)
            with dl1:
                if st.button("📄 Generate PDF", key="rep_gen_pdf"):
                    pdf_path = export_pdf(bundle, "/tmp/cloudops_report.pdf")
                    with open(pdf_path, "rb") as f:
                        st.download_button("⬇️ Download PDF", f, file_name="CloudOps-AI_Report.pdf",
                                           mime="application/pdf", key="rep_dl_pdf")
            with dl2:
                if st.button("📊 Generate Excel", key="rep_gen_xlsx"):
                    xlsx_path = export_excel(bundle, "/tmp/cloudops_report.xlsx")
                    with open(xlsx_path, "rb") as f:
                        st.download_button("⬇️ Download Excel", f,
                                           file_name="CloudOps-AI_Report.xlsx",
                                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                           key="rep_dl_xlsx")
        else:
            st.info("Click **Build report bundle** to compute the data for the "
                    "selected period, then export.")

    st.divider()

    st.markdown("### 2️⃣ Configure automated email delivery")
    st.caption("Requires SMTP credentials as environment variables/secrets: "
               "`SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` (optionally `SMTP_PORT`, "
               "`SMTP_FROM`). A standing subscription re-sends on the cadence "
               "below for as long as this app process keeps running — for "
               "guaranteed enterprise delivery, point a cron worker or "
               "EventBridge Scheduler at `send_report_email()` directly "
               "(see `core/scheduler.py` docstring).")

    with st.form("rep_sub_form"):
        f1, f2, f3 = st.columns(3)
        sub_name = f1.text_input("Subscription name", placeholder="e.g. cfo-weekly")
        sub_recipients = f2.text_input("Recipients (comma-separated)",
                                       placeholder="cfo@company.com, vp-eng@company.com")
        sub_period = f3.selectbox("Cadence", ["daily", "weekly", "monthly", "quarterly"],
                                  index=1, key="rep_sub_period")
        sub_formats = st.multiselect("Formats", ["pdf", "excel"], default=["pdf"],
                                     key="rep_sub_formats")
        submitted = st.form_submit_button("➕ Add subscription")
        if submitted:
            if not sub_name or not sub_recipients:
                st.error("Subscription name and at least one recipient are required.")
            else:
                recipients = [r.strip() for r in sub_recipients.split(",") if r.strip()]
                st.session_state["report_subscriptions"][sub_name] = ReportSubscription(
                    name=sub_name, recipients=recipients, period_type=sub_period,
                    formats=tuple(sub_formats) or ("pdf",))
                st.success(f"Subscription **{sub_name}** added ({sub_period}, "
                          f"{'/'.join(sub_formats) or 'pdf'}).")

    subs = st.session_state["report_subscriptions"]
    if subs:
        st.markdown("**Active subscriptions**")
        st.dataframe(pd.DataFrame([{
            "Name": s.name, "Recipients": ", ".join(s.recipients),
            "Cadence": s.period_type, "Formats": "/".join(s.formats),
            "Active": s.active,
        } for s in subs.values()]), use_container_width=True, hide_index=True)

        pick = st.selectbox("Send a test email now for:", list(subs.keys()),
                            key="rep_test_send_pick")
        if st.button("✉️ Send test email now", key="rep_test_send_btn"):
            try:
                test_bundle = _build_current_bundle(subs[pick].period_type)
                res = send_report_email(subs[pick], test_bundle)
                st.success(f"Sent to {', '.join(res['recipients'])} "
                          f"({'/'.join(res['formats_sent'])}).")
            except RuntimeError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Send failed: {e}")

        remove = st.selectbox("Remove subscription:", ["—"] + list(subs.keys()),
                              key="rep_remove_pick")
        if remove != "—" and st.button("🗑️ Remove", key="rep_remove_btn"):
            del st.session_state["report_subscriptions"][remove]
            st.rerun()
    else:
        st.caption("No subscriptions configured yet.")
