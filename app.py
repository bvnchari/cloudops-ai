"""
CloudOps-AI — Executive Dashboard (Phase 6 UI).

Run locally:  streamlit run app.py
Deploy:       HuggingFace Spaces (Streamlit SDK) — same pattern as CloudBridge.
"""

import pandas as pd
import streamlit as st

from pipeline import run_pipeline

st.set_page_config(page_title="CloudOps-AI | Enterprise AIOps", layout="wide",
                   page_icon="🛰️")

st.title("🛰️ CloudOps-AI — Enterprise AIOps Platform")
st.caption("Ingestion → Anomaly Detection → Correlation → Auto-Remediation → ITSM → Governance")


@st.cache_data(show_spinner="Running AIOps pipeline...")
def load():
    return run_pipeline(verbose=False)


result = load()
kpi = result["kpi"]
stats = result["stats"]
incidents = result["incidents"]

# ---------------- KPI tiles ----------------
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Raw Alerts", kpi.total_raw_alerts)
c2.metric("Incidents", kpi.total_incidents,
          delta=f"-{kpi.alert_reduction_pct}% noise", delta_color="inverse")
c3.metric("MTTR", f"{kpi.mttr_minutes:.0f} min" if kpi.mttr_minutes else "n/a")
c4.metric("Automation Rate", f"{kpi.automation_rate_pct}%")
c5.metric("Est. Savings", f"${kpi.est_automation_savings_usd/1000:.0f}K/yr")
c6.metric("Availability", f"{kpi.service_availability_pct}%")

st.divider()

tab_funnel, tab_inc, tab_metrics, tab_rem, tab_itsm, tab_ai = st.tabs(
    ["📉 Alert Funnel", "🚨 Incidents", "📈 Telemetry", "🔧 Remediation",
     "🎫 ITSM", "🤖 AI Analyst"])

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
    st.subheader("ServiceNow Tickets (auto-created / auto-closed)")
    st.dataframe(pd.DataFrame([{
        "Number": t.number, "State": t.state, "Impact": t.impact,
        "Urgency": t.urgency, "Service": t.business_service,
        "CMDB CI": t.cmdb_ci, "Short Description": t.short_description,
    } for t in result["tickets"]]), use_container_width=True, hide_index=True)
    st.caption("Backend auto-selects: real ServiceNow (set SN_INSTANCE / SN_USER / "
               "SN_PASSWORD env vars — a free PDI works) or in-memory mock for demo.")

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
