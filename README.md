# CloudOps-AI — Enterprise AIOps Platform

End-to-end AIOps pipeline built from scratch: observability ingestion, ML-driven anomaly detection, topology-based event correlation, self-healing auto-remediation, ServiceNow ITSM integration, and live governance KPIs.

```
Telemetry (Phase 1) ──► Threshold Alerts ─┐
        │                                 ├──► Correlation Engine (Phase 2) ──► Incidents + RCA
        └──► Anomaly Detection (Phase 3) ─┘             │
                                                        ▼
                     Governance KPIs (Phase 6) ◄── Auto-Remediation (Phase 4)
                                                        │
                                                        ▼
                                          ServiceNow ITSM (Phase 5)
                                          tickets · CMDB sync · auto-close
```

## Quick start

```bash
pip install -r requirements.txt
python3 pipeline.py            # run the full pipeline in the terminal
python3 -m pytest tests/ -v    # 8 phase-validation tests
streamlit run app.py           # executive dashboard
```

## What each phase does

**Phase 1 — Observability & Ingestion** (`core/telemetry.py`, `core/topology.py`)
Multi-layer telemetry (infra / platform / app / data / network) over a CMDB-lite service-dependency graph modeling an EKS-based payments platform. The `MetricSource` interface is Prometheus-swappable: replace `SyntheticSource` with a `PrometheusSource` querying the HTTP API and nothing downstream changes. Static-threshold alerting is deliberately noisy — it is the baseline the AIOps layer improves on.

**Phase 2 — Event Correlation & Noise Reduction** (`core/correlation.py`)
Three stages, BigPanda/Moogsoft-style: deduplication (same CI/metric/severity within a window collapses), time + topology correlation (events whose CIs share an upstream dependency merge into one incident, the shared dependency becomes probable root cause), and suppression (downstream symptom incidents fold into their parent). Demo result: **~440 raw alerts → 3 incidents (99.3% noise reduction)** with correct RCA.

**Phase 3 — ML-Driven Anomaly Detection** (`core/anomaly.py`)
Statistical ensemble with no heavy ML dependencies: robust z-score (median/MAD) for point anomalies, EWMA control band for sustained level shifts, and linear-trend extrapolation for predictive alerts ("disk will hit 95% in ~N hours"). The predictor has two false-positive gates — slope significance (t-stat) and a new-high check that rejects diurnal/seasonal swings. Anomalies emit as first-class alerts into the same correlation pipeline.

**Phase 4 — Automation & Auto-Remediation** (`core/remediation.py`)
Declarative runbook registry (restart, disk cleanup, scaling, DB replication recovery) with pluggable executors — `LocalExecutor` for demo, same interface for Ansible/SSM/K8s. Capacity-changing runbooks are gated behind change approval (`auto_approve=False`). All fixes flow through the engine — no inline patching.

**Phase 5 — ServiceNow ITOM Integration** (`core/itsm.py`)
One interface, two backends: `ServiceNowConnector` makes real REST calls (a free Personal Developer Instance works — set `SN_INSTANCE`, `SN_USER`, `SN_PASSWORD`) and `MockITSM` mirrors it in memory. Covers incident auto-creation with impact/urgency mapping, CMDB sync from the topology map, service mapping on tickets, and auto-close on successful remediation.

**Phase 6 — Governance & Reporting** (`core/kpi.py`, `app.py`)
MTTR, MTBF, alert reduction %, automation rate, estimated automation savings, and service availability — computed live from pipeline output, not hardcoded. Streamlit dashboard with KPI tiles, alert funnel, incident/RCA table, metric explorer with anomaly overlays, remediation logs, and the ITSM ticket queue.

## Roadmap (Phase 7+)
- Real Prometheus/OTel ingestion (`PrometheusSource`)
- LLM-powered incident summarization & RCA narrative (LangGraph agent)
- Topology auto-discovery from K8s API
- Grafana-embedded dashboards, alert webhook receiver (Alertmanager-compatible)
