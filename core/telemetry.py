"""
Phase 1 — Observability & Ingestion Layer.

Generates realistic multi-layer telemetry (infra / platform / app / data / network)
as time-series metrics + raw alerts. The MetricSource interface is designed so a
PrometheusSource (querying a real Prometheus HTTP API) can be dropped in later
with zero changes to the correlation / anomaly / remediation layers.
"""

import math
import random
import time
from dataclasses import dataclass, field


@dataclass
class MetricPoint:
    ts: float
    metric: str          # e.g. node_cpu_utilization
    ci_id: str
    value: float
    unit: str = ""


@dataclass
class RawAlert:
    alert_id: str
    ts: float
    ci_id: str
    metric: str
    severity: str        # critical | warning | info
    message: str
    value: float
    labels: dict = field(default_factory=dict)


class MetricSource:
    """Interface. Swap SyntheticSource for a PrometheusSource in production."""
    def series(self, metric: str, ci_id: str, n_points: int, interval_s: int) -> list[MetricPoint]:
        raise NotImplementedError


class SyntheticSource(MetricSource):
    """
    Realistic synthetic telemetry with:
      - diurnal seasonality (business-hours load curve)
      - gaussian noise
      - injectable incident scenarios (cpu saturation, memory leak, latency spike,
        disk fill, network errors) so the ML layer has something real to catch.
    """

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)
        self.incidents: list[dict] = []   # {ci_id, metric, start_idx, magnitude, shape}

    def inject_incident(self, ci_id: str, metric: str, start_idx: int,
                        magnitude: float, shape: str = "step"):
        self.incidents.append(dict(ci_id=ci_id, metric=metric,
                                   start_idx=start_idx, magnitude=magnitude, shape=shape))

    def _baseline(self, metric: str) -> tuple[float, float, str]:
        table = {
            "node_cpu_utilization":   (35.0, 4.0, "%"),
            "node_memory_utilization": (55.0, 3.0, "%"),
            "node_disk_utilization":  (48.0, 1.0, "%"),
            "pod_restart_count":      (0.0, 0.2, "count"),
            "api_latency_p99":        (180.0, 25.0, "ms"),
            "api_error_rate":         (0.4, 0.15, "%"),
            "db_connections":         (120.0, 15.0, "conn"),
            "db_replication_lag":     (0.8, 0.3, "s"),
            "alb_5xx_rate":           (0.2, 0.1, "%"),
            "network_packet_loss":    (0.05, 0.03, "%"),
        }
        return table.get(metric, (50.0, 5.0, ""))

    def series(self, metric: str, ci_id: str, n_points: int = 120,
               interval_s: int = 60) -> list[MetricPoint]:
        base, noise, unit = self._baseline(metric)
        now = time.time()
        points = []
        for i in range(n_points):
            ts = now - (n_points - i) * interval_s
            # diurnal seasonality: +/-15% swing over a simulated day
            season = 1.0 + 0.15 * math.sin(2 * math.pi * i / max(n_points, 1))
            val = base * season + self.rng.gauss(0, noise)
            # apply injected incidents
            for inc in self.incidents:
                if inc["ci_id"] == ci_id and inc["metric"] == metric and i >= inc["start_idx"]:
                    if inc["shape"] == "step":
                        val += inc["magnitude"]
                    elif inc["shape"] == "ramp":   # e.g. memory leak / disk fill
                        val += inc["magnitude"] * (i - inc["start_idx"]) / max(n_points - inc["start_idx"], 1)
                    elif inc["shape"] == "spike" and i - inc["start_idx"] < 5:
                        val += inc["magnitude"]
            points.append(MetricPoint(ts=ts, metric=metric, ci_id=ci_id,
                                      value=max(val, 0.0), unit=unit))
        return points


class AlertGenerator:
    """
    Static-threshold alerting (the 'legacy noisy monitoring' the AIOps layer improves on).
    Deliberately chatty — fires per-datapoint breaches — so the correlation engine's
    noise-reduction value is measurable (alert reduction % KPI).
    """

    THRESHOLDS = {
        "node_cpu_utilization":    ("warning", 75.0, "critical", 90.0),
        "node_memory_utilization": ("warning", 80.0, "critical", 92.0),
        "node_disk_utilization":   ("warning", 80.0, "critical", 90.0),
        "api_latency_p99":         ("warning", 400.0, "critical", 800.0),
        "api_error_rate":          ("warning", 2.0, "critical", 5.0),
        "db_replication_lag":      ("warning", 5.0, "critical", 15.0),
        "alb_5xx_rate":            ("warning", 1.0, "critical", 3.0),
        "network_packet_loss":     ("warning", 0.5, "critical", 2.0),
        "pod_restart_count":       ("warning", 1.0, "critical", 3.0),
    }

    def __init__(self):
        self._counter = 0

    def evaluate(self, points: list[MetricPoint]) -> list[RawAlert]:
        alerts = []
        for p in points:
            rule = self.THRESHOLDS.get(p.metric)
            if not rule:
                continue
            w_label, w_thr, c_label, c_thr = rule
            severity = None
            if p.value >= c_thr:
                severity = c_label
            elif p.value >= w_thr:
                severity = w_label
            if severity:
                self._counter += 1
                alerts.append(RawAlert(
                    alert_id=f"ALT-{self._counter:06d}",
                    ts=p.ts, ci_id=p.ci_id, metric=p.metric,
                    severity=severity, value=round(p.value, 2),
                    message=f"{p.metric} = {p.value:.1f}{p.unit} on {p.ci_id} "
                            f"(threshold {c_thr if severity == 'critical' else w_thr}{p.unit})",
                    labels={"source": "static_threshold"},
                ))
        return alerts
