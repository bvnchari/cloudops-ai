"""
Real metrics sources — the piece that actually populates Telemetry and
anomaly detection in LIVE mode.

ServiceNow is a system of record (CMDB, incidents), not a metrics store —
that's why LIVE mode's Telemetry tab starts empty. This module plugs in a
REAL time-series backend so it doesn't have to stay empty: Prometheus,
Datadog, Dynatrace, or Grafana (as a proxy in front of any of those).

Each connector implements the same interface:
  test_connection() -> dict          (real reachability/auth check)
  query_range(promql_or_query, start_ts, end_ts, step_s) -> list[(ts, value)]

`METRIC_QUERY_TEMPLATES` maps CloudOps-AI's internal metric names (the same
ones core.telemetry's synthetic data and core.sli's SLI definitions use) to
a query template with a `{ci}` placeholder for the CI/resource name. These
are sensible defaults for a standard Prometheus node-exporter + generic app
instrumentation setup — override any of them in the Config tab for your
actual label conventions.
"""

import time
from dataclasses import dataclass, field

from .telemetry import MetricPoint

# Default query templates per canonical metric name. {ci} is substituted
# with the CI's name at query time. These assume common exporter
# conventions (node_exporter, generic app metrics) — edit per-metric in the
# Config tab to match your actual label schema.
DEFAULT_PROMQL_TEMPLATES = {
    "node_cpu_utilization": '100 - (avg by (instance) (rate(node_cpu_seconds_total{{mode="idle",instance=~".*{ci}.*"}}[5m])) * 100)',
    "node_memory_utilization": '100 * (1 - node_memory_MemAvailable_bytes{{instance=~".*{ci}.*"}} / node_memory_MemTotal_bytes{{instance=~".*{ci}.*"}})',
    "node_disk_utilization": '100 - (node_filesystem_avail_bytes{{instance=~".*{ci}.*",mountpoint="/"}} / node_filesystem_size_bytes{{instance=~".*{ci}.*",mountpoint="/"}} * 100)',
    "api_latency_p99": 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{service=~".*{ci}.*"}}[5m])) by (le))',
    "api_error_rate": '100 * sum(rate(http_requests_total{{service=~".*{ci}.*",status=~"5.."}}[5m])) / sum(rate(http_requests_total{{service=~".*{ci}.*"}}[5m]))',
    "alb_5xx_rate": '100 * sum(rate(alb_request_count_total{{lb=~".*{ci}.*",status=~"5.."}}[5m])) / sum(rate(alb_request_count_total{{lb=~".*{ci}.*"}}[5m]))',
    "db_connections": 'sum(pg_stat_database_numbackends{{instance=~".*{ci}.*"}})',
    "db_replication_lag": 'pg_replication_lag_seconds{{instance=~".*{ci}.*"}}',
    "pod_restart_count": 'sum(increase(kube_pod_container_status_restarts_total{{pod=~".*{ci}.*"}}[15m]))',
    "network_packet_loss": '100 * sum(rate(node_network_receive_errs_total{{instance=~".*{ci}.*"}}[5m])) / sum(rate(node_network_receive_packets_total{{instance=~".*{ci}.*"}}[5m]))',
    "login_success_rate": '100 * sum(rate(auth_login_success_total{{instance=~".*{ci}.*"}}[5m])) / sum(rate(auth_login_attempts_total{{instance=~".*{ci}.*"}}[5m]))',
}


class MetricsSource:
    def test_connection(self) -> dict:
        raise NotImplementedError

    def query_range(self, query: str, start_ts: float, end_ts: float,
                    step_s: int = 60) -> list[tuple[float, float]]:
        raise NotImplementedError


class PrometheusSource(MetricsSource):
    def __init__(self, base_url: str, timeout_s: float = 15.0,
                bearer_token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.bearer_token = bearer_token

    def _headers(self):
        return {"Authorization": f"Bearer {self.bearer_token}"} if self.bearer_token else {}

    def test_connection(self) -> dict:
        import requests
        r = requests.get(f"{self.base_url}/-/healthy", headers=self._headers(),
                         timeout=self.timeout_s)
        if not r.ok:
            r = requests.get(f"{self.base_url}/api/v1/status/buildinfo",
                             headers=self._headers(), timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"Prometheus unreachable: {r.status_code} {r.text[:200]}")
        return {"reachable": True, "provider": "prometheus", "url": self.base_url}

    def query_range(self, query, start_ts, end_ts, step_s=60):
        import requests
        r = requests.get(f"{self.base_url}/api/v1/query_range",
                         params={"query": query, "start": start_ts, "end": end_ts,
                                "step": step_s},
                         headers=self._headers(), timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"Prometheus query failed: {r.status_code} {r.text[:300]}")
        data = r.json()
        result = data.get("data", {}).get("result", [])
        if not result:
            return []
        return [(float(ts), float(val)) for ts, val in result[0].get("values", [])]


class GrafanaSource(MetricsSource):
    """Proxies PromQL through a Grafana-managed Prometheus datasource —
    useful when Prometheus itself isn't directly reachable but Grafana is."""

    def __init__(self, base_url: str, api_key: str, datasource_uid: str,
                timeout_s: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.datasource_uid = datasource_uid
        self.timeout_s = timeout_s

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def test_connection(self) -> dict:
        import requests
        r = requests.get(f"{self.base_url}/api/health", headers=self._headers(),
                         timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"Grafana unreachable: {r.status_code} {r.text[:200]}")
        return {"reachable": True, "provider": "grafana", "url": self.base_url}

    def query_range(self, query, start_ts, end_ts, step_s=60):
        import requests
        r = requests.get(
            f"{self.base_url}/api/datasources/proxy/uid/{self.datasource_uid}"
            f"/api/v1/query_range",
            params={"query": query, "start": start_ts, "end": end_ts, "step": step_s},
            headers=self._headers(), timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"Grafana proxy query failed: {r.status_code} {r.text[:300]}")
        result = r.json().get("data", {}).get("result", [])
        if not result:
            return []
        return [(float(ts), float(val)) for ts, val in result[0].get("values", [])]


class DatadogSource(MetricsSource):
    def __init__(self, api_key: str, app_key: str, site: str = "datadoghq.com",
                timeout_s: float = 15.0):
        self.api_key = api_key
        self.app_key = app_key
        self.site = site
        self.timeout_s = timeout_s

    def _headers(self):
        return {"DD-API-KEY": self.api_key, "DD-APPLICATION-KEY": self.app_key}

    def test_connection(self) -> dict:
        import requests
        r = requests.get(f"https://api.{self.site}/api/v1/validate",
                         headers=self._headers(), timeout=self.timeout_s)
        if not r.ok or not r.json().get("valid"):
            raise RuntimeError(f"Datadog auth failed: {r.status_code} {r.text[:200]}")
        return {"reachable": True, "provider": "datadog", "site": self.site}

    def query_range(self, query, start_ts, end_ts, step_s=60):
        import requests
        r = requests.get(f"https://api.{self.site}/api/v1/query",
                         params={"query": query, "from": int(start_ts), "to": int(end_ts)},
                         headers=self._headers(), timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"Datadog query failed: {r.status_code} {r.text[:300]}")
        series = r.json().get("series", [])
        if not series:
            return []
        return [(float(ts), float(val)) for ts, val in series[0].get("pointlist", [])
               if val is not None]


class DynatraceSource(MetricsSource):
    def __init__(self, base_url: str, api_token: str, timeout_s: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout_s = timeout_s

    def _headers(self):
        return {"Authorization": f"Api-Token {self.api_token}"}

    def test_connection(self) -> dict:
        import requests
        r = requests.get(f"{self.base_url}/api/v2/activeGates",
                         headers=self._headers(), timeout=self.timeout_s,
                         params={"pageSize": 1})
        if not r.ok:
            raise RuntimeError(f"Dynatrace unreachable: {r.status_code} {r.text[:200]}")
        return {"reachable": True, "provider": "dynatrace", "url": self.base_url}

    def query_range(self, query, start_ts, end_ts, step_s=60):
        import requests
        r = requests.get(f"{self.base_url}/api/v2/metrics/query",
                         params={"metricSelector": query,
                                "from": int(start_ts * 1000), "to": int(end_ts * 1000)},
                         headers=self._headers(), timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"Dynatrace query failed: {r.status_code} {r.text[:300]}")
        result = r.json().get("result", [])
        if not result or not result[0].get("data"):
            return []
        dp = result[0]["data"][0]
        timestamps = dp.get("timestamps", [])
        values = dp.get("values", [])
        return [(ts / 1000, v) for ts, v in zip(timestamps, values) if v is not None]


def fetch_series_for_cis(source: MetricsSource, ci_names: list[str],
                         metrics: list[str] | None = None,
                         templates: dict | None = None,
                         window_h: float = 2.0, step_s: int = 60) -> dict:
    """Builds and runs one query per (ci, metric) pair, returning the same
    dict[(ci_id, metric)] -> list[MetricPoint] shape core.telemetry produces
    for demo mode, so downstream code (Telemetry tab, anomaly detection,
    SLI calc) doesn't need to know the difference."""
    templates = templates or DEFAULT_PROMQL_TEMPLATES
    metrics = metrics or list(templates.keys())
    end_ts = time.time()
    start_ts = end_ts - window_h * 3600
    series = {}
    for ci in ci_names:
        for metric in metrics:
            template = templates.get(metric)
            if not template:
                continue
            query = template.format(ci=ci)
            try:
                points = source.query_range(query, start_ts, end_ts, step_s)
            except Exception:
                continue  # one bad query/CI combo shouldn't kill the whole pull
            if points:
                series[(ci, metric)] = [MetricPoint(ts=ts, metric=metric, ci_id=ci, value=val)
                                        for ts, val in points]
    return series
