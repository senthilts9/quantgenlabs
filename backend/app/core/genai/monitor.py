"""GenAI operational risk over the platform's OWN model usage.

Not a generic LLM dashboard: it scores the invocations this application
actually made, using the same quantitative tooling the market-risk side uses.
An LLM in a trading workflow is an operational-risk exposure -- latency and
cost have tails, behaviour drifts, and calls fail -- so it gets measured with
VaR, z-scores and split-window drift rather than a request counter.

Four components, deliberately kept separate so a spike is attributable:

  OPERATIONAL  historical-simulation VaR(95) on latency and cost. A tail is
               what breaks an SLA, not the mean.
  ANOMALY      z-score of the most recent call against the rolling window.
  DRIFT        split-window comparison (older half vs recent half) of latency
               and reply length -- a proxy for behavioural change when there
               is no ground-truth label to score against.
  RELIABILITY  observed error rate.

HONEST LIMITATION, stated here rather than buried: none of this measures
answer QUALITY. There is no hallucination or correctness score, because
scoring that properly needs either ground truth or a judge model, and
pretending a latency metric says anything about correctness would be worse
than admitting the gap.
"""
from __future__ import annotations

import statistics


def _pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def _z(x: float, vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    sd = statistics.stdev(vals)
    return 0.0 if sd == 0 else (x - statistics.fmean(vals)) / sd


def analyse(rows: list[dict], window: int = 50) -> dict:
    """Score a list of invocation records (newest first)."""
    if not rows:
        return {"empty": True,
                "message": "no model invocations recorded yet — ask the Risk "
                           "Copilot something, then refresh"}

    recent = rows[:window]
    ok = [r for r in recent if r.get("ok")]
    lat = [float(r["latency_ms"]) for r in ok]
    cost = [float(r["cost_usd"]) for r in ok]
    reply = [float(r.get("reply_chars") or 0) for r in ok]
    tin = sum(int(r.get("input_tokens") or 0) for r in rows)
    tout = sum(int(r.get("output_tokens") or 0) for r in rows)

    errors = [r for r in recent if not r.get("ok")]
    error_rate = len(errors) / len(recent) if recent else 0.0

    # Operational: VaR(95) on the tail, not the average.
    lat_var = _pct(lat, 0.95)
    cost_var = _pct(cost, 0.95)

    # Anomaly: newest observation vs the rest of the window.
    latest = ok[0] if ok else None
    z_lat = _z(float(latest["latency_ms"]), lat[1:]) if latest and len(lat) > 2 else 0.0
    z_len = _z(float(latest.get("reply_chars") or 0), reply[1:]) if latest and len(reply) > 2 else 0.0

    # Drift: split-window. Needs enough history on BOTH halves to mean
    # anything, so it reports None rather than a number built on 2 samples.
    drift = None
    if len(lat) >= 8:
        half = len(lat) // 2
        new_l, old_l = lat[:half], lat[half:]
        new_r, old_r = reply[:half], reply[half:]
        def rel(a, b):
            mb = statistics.fmean(b)
            return abs(statistics.fmean(a) - mb) / mb if mb else 0.0
        drift = {
            "latency_shift_pct": round(rel(new_l, old_l) * 100, 2),
            "reply_length_shift_pct": round(rel(new_r, old_r) * 100, 2),
            "n_recent": len(new_l), "n_prior": len(old_l),
        }

    def band(x: float) -> str:
        return ("CRITICAL" if x >= 75 else "HIGH" if x >= 50
                else "MEDIUM" if x >= 25 else "LOW")

    # Component scores on 0-100. Thresholds are judgement calls, named as
    # constants in the code rather than hidden in a formula.
    op = min(100.0, (lat_var or 0) / 5000 * 100)          # 5s latency = 100
    anom = min(100.0, (abs(z_lat) + abs(z_len)) / 6 * 100)  # 3 sigma each = 100
    dr = min(100.0, ((drift or {}).get("latency_shift_pct", 0.0)) / 50 * 100)
    rel_score = min(100.0, error_rate * 100 * 2)           # 50% errors = 100
    unified = 0.35 * op + 0.25 * anom + 0.20 * dr + 0.20 * rel_score

    return {
        "n_total": len(rows),
        "n_window": len(recent),
        "components": {
            "operational_risk": round(op, 2),
            "anomaly_risk": round(anom, 2),
            "drift_risk": round(dr, 2),
            "reliability_risk": round(rel_score, 2),
            "unified_score": round(unified, 2),
            "band": band(unified),
        },
        "latency": {
            "mean_ms": round(statistics.fmean(lat), 1) if lat else None,
            "median_ms": round(statistics.median(lat), 1) if lat else None,
            "var95_ms": round(lat_var, 1) if lat_var else None,
            "max_ms": round(max(lat), 1) if lat else None,
        },
        "cost": {
            "total_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows), 6),
            "mean_usd": round(statistics.fmean(cost), 6) if cost else None,
            "var95_usd": round(cost_var, 6) if cost_var else None,
        },
        "tokens": {"input": tin, "output": tout, "total": tin + tout},
        "reliability": {
            "error_rate_pct": round(error_rate * 100, 2),
            "errors": len(errors),
            "recent_errors": [
                {"ts": e["ts"], "error": e.get("error")} for e in errors[:5]],
        },
        "anomaly": {"latency_z": round(z_lat, 3), "reply_length_z": round(z_len, 3)},
        "drift": drift,
        "recent": [
            {k: r[k] for k in ("ts", "model", "endpoint", "latency_ms",
                               "input_tokens", "output_tokens", "cost_usd", "ok")}
            for r in rows[:25]
        ],
        "limitation": ("no answer-quality or hallucination score: that needs "
                       "ground truth or a judge model, and latency tells you "
                       "nothing about correctness"),
    }
