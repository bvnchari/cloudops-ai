"""
core/prom_metrics.py — Prometheus instrumentation for CloudOps-AI

Exposes a /metrics endpoint (via a background HTTP server on port 8000)
with app-specific counters/gauges, separate from Streamlit's own port 8501.

Usage in app.py (add near the top, after other imports):

    from core.prom_metrics import (
        start_metrics_server,
        record_page_view,
        record_ai_agent_call,
        record_servicenow_call,
        record_alert_processed,
        set_active_incidents,
    )
    start_metrics_server()   # call once, e.g. guarded by st.session_state

Then instrument key code paths, e.g.:

    record_page_view(tab_name="AI Insights")
    record_ai_agent_call(duration_seconds=1.23, status="success")
    record_servicenow_call(endpoint="incident", status="success", duration_seconds=0.45)
    record_alert_processed(severity="critical", noise_reduced=True)
    set_active_incidents(count=7)
"""

import threading
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    start_http_server,
)

# ---- Metric definitions -----------------------------------------------

PAGE_VIEWS = Counter(
    "cloudops_ai_page_views_total",
    "Total page/tab views in the CloudOps-AI Streamlit app",
    ["tab_name"],
)

AI_AGENT_CALLS = Counter(
    "cloudops_ai_agent_calls_total",
    "Total calls to the AI incident analyst agent",
    ["status"],  # success | error
)

AI_AGENT_DURATION = Histogram(
    "cloudops_ai_agent_call_duration_seconds",
    "Duration of AI agent calls",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

SERVICENOW_CALLS = Counter(
    "cloudops_ai_servicenow_calls_total",
    "Total ServiceNow API calls",
    ["endpoint", "status"],
)

SERVICENOW_DURATION = Histogram(
    "cloudops_ai_servicenow_call_duration_seconds",
    "Duration of ServiceNow API calls",
    ["endpoint"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10),
)

ALERTS_PROCESSED = Counter(
    "cloudops_ai_alerts_processed_total",
    "Total alerts processed by the noise-reduction pipeline",
    ["severity", "noise_reduced"],  # noise_reduced: "true" | "false"
)

ACTIVE_INCIDENTS = Gauge(
    "cloudops_ai_active_incidents",
    "Current number of active/open incidents",
)

APP_INFO = Gauge(
    "cloudops_ai_app_info",
    "Static app info (always 1); labels carry version metadata",
    ["version"],
)

_server_started = False
_lock = threading.Lock()


def start_metrics_server(port: int = 8000):
    """Start the Prometheus metrics HTTP server once per process."""
    global _server_started
    with _lock:
        if not _server_started:
            start_http_server(port)
            APP_INFO.labels(version="v1").set(1)
            _server_started = True


def record_page_view(tab_name: str):
    PAGE_VIEWS.labels(tab_name=tab_name).inc()


def record_ai_agent_call(duration_seconds: float, status: str = "success"):
    AI_AGENT_CALLS.labels(status=status).inc()
    AI_AGENT_DURATION.observe(duration_seconds)


def record_servicenow_call(endpoint: str, status: str, duration_seconds: float):
    SERVICENOW_CALLS.labels(endpoint=endpoint, status=status).inc()
    SERVICENOW_DURATION.labels(endpoint=endpoint).observe(duration_seconds)


def record_alert_processed(severity: str, noise_reduced: bool):
    ALERTS_PROCESSED.labels(
        severity=severity, noise_reduced=str(noise_reduced).lower()
    ).inc()


def set_active_incidents(count: int):
    ACTIVE_INCIDENTS.set(count)
