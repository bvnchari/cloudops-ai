"""
Phase 3 — ML-Driven Anomaly Detection & Predictive Monitoring.

Statistical ensemble (no heavy ML deps — deployable anywhere, incl. HF Spaces):
  * Robust z-score (median/MAD)  -> point anomalies
  * EWMA control band            -> drift / level-shift detection
  * Linear trend extrapolation   -> predictive alerts ("disk full in ~N hours")

Design note: each detector returns AnomalySignal objects that feed the same
correlation pipeline as threshold alerts — anomalies become first-class events.
"""

import statistics
from dataclasses import dataclass

from .telemetry import MetricPoint, RawAlert


@dataclass
class AnomalySignal:
    ci_id: str
    metric: str
    ts: float
    value: float
    kind: str          # point_anomaly | level_shift | predicted_breach
    score: float       # detector-specific confidence/score
    detail: str


class AnomalyDetector:
    def __init__(self, z_threshold: float = 4.0, ewma_alpha: float = 0.3,
                 ewma_band: float = 3.5):
        self.z_threshold = z_threshold
        self.ewma_alpha = ewma_alpha
        self.ewma_band = ewma_band
        self._alert_seq = 0

    # ---- robust z-score (median / MAD): resistant to the anomaly polluting the baseline
    def point_anomalies(self, series: list[MetricPoint], train_frac: float = 0.6) -> list[AnomalySignal]:
        if len(series) < 20:
            return []
        n_train = max(int(len(series) * train_frac), 10)
        train = [p.value for p in series[:n_train]]
        med = statistics.median(train)
        mad = statistics.median(abs(v - med) for v in train) or 1e-6
        out = []
        for p in series[n_train:]:
            z = 0.6745 * (p.value - med) / mad
            if abs(z) >= self.z_threshold:
                out.append(AnomalySignal(
                    ci_id=p.ci_id, metric=p.metric, ts=p.ts, value=p.value,
                    kind="point_anomaly", score=round(abs(z), 1),
                    detail=f"robust z={z:+.1f} vs baseline median {med:.1f}",
                ))
        return out

    # ---- EWMA control chart: catches sustained level shifts static thresholds miss
    def level_shifts(self, series: list[MetricPoint]) -> list[AnomalySignal]:
        if len(series) < 20:
            return []
        vals = [p.value for p in series]
        ewma = vals[0]
        resid_std = statistics.pstdev(vals[:15]) or 1e-6
        out = []
        breach_run = 0
        for p in series[1:]:
            ewma = self.ewma_alpha * p.value + (1 - self.ewma_alpha) * ewma
            dev = abs(p.value - ewma)
            if dev > self.ewma_band * resid_std:
                breach_run += 1
            else:
                breach_run = 0
            if breach_run == 3:  # 3 consecutive out-of-band points = confirmed shift
                out.append(AnomalySignal(
                    ci_id=p.ci_id, metric=p.metric, ts=p.ts, value=p.value,
                    kind="level_shift", score=round(dev / resid_std, 1),
                    detail=f"sustained deviation from EWMA ({ewma:.1f})",
                ))
        return out

    # ---- predictive: linear fit on recent window, extrapolate to capacity limit
    def predict_breach(self, series: list[MetricPoint], limit: float,
                       window: int = 30, horizon_points: int = 720) -> AnomalySignal | None:
        if len(series) < window:
            return None
        recent = series[-window:]
        xs = list(range(window))
        ys = [p.value for p in recent]
        x_mean, y_mean = sum(xs) / window, sum(ys) / window
        denom = sum((x - x_mean) ** 2 for x in xs) or 1e-6
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom
        if slope <= 1e-4:
            return None
        # significance gate: reject slow seasonal drift masquerading as a real trend.
        # t-stat of the slope must be strong AND total rise over the window must
        # dominate residual noise.
        intercept = y_mean - slope * x_mean
        residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        resid_std = (sum(r * r for r in residuals) / max(window - 2, 1)) ** 0.5 or 1e-6
        t_stat = slope / (resid_std / denom ** 0.5)
        total_rise = slope * window
        if t_stat < 8.0 or total_rise < 3.0 * resid_std:
            return None
        # new-high gate: a genuine capacity fill pushes beyond historical range;
        # diurnal/seasonal swings stay inside it. Compare recent level to the
        # max observed *before* the trend window.
        history = [p.value for p in series[:-window]]
        if history and max(ys[-3:]) <= max(history) * 1.02:
            return None
        current = ys[-1]
        points_to_breach = (limit - current) / slope
        if 0 < points_to_breach <= horizon_points:
            interval_s = (recent[-1].ts - recent[0].ts) / max(window - 1, 1)
            eta_h = points_to_breach * interval_s / 3600
            p = recent[-1]
            return AnomalySignal(
                ci_id=p.ci_id, metric=p.metric, ts=p.ts, value=current,
                kind="predicted_breach", score=round(slope, 3),
                detail=f"trending +{slope:.2f}/interval — projected to hit "
                       f"{limit:.0f} in ~{eta_h:.1f}h",
            )
        return None

    # ---- bridge: anomalies -> alerts so they flow through the same correlation engine
    def to_alerts(self, signals: list[AnomalySignal]) -> list[RawAlert]:
        alerts = []
        for s in signals:
            self._alert_seq += 1
            severity = "critical" if (s.kind == "predicted_breach" or s.score >= 8) else "warning"
            alerts.append(RawAlert(
                alert_id=f"AML-{self._alert_seq:06d}",
                ts=s.ts, ci_id=s.ci_id, metric=s.metric,
                severity=severity, value=round(s.value, 2),
                message=f"[{s.kind}] {s.metric} on {s.ci_id}: {s.detail}",
                labels={"source": "anomaly_detection", "kind": s.kind},
            ))
        return alerts
