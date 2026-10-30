"""Judging a sample of trades (back-test or forward), with fx-board's pre-registered rule ported unchanged in spirit:

  - standard error clustered by trading day (trades of one day are not independent: same regime, same news);
  - "confirming" is decided ONCE, on the first `eval_trades` trades by entry time: lower bound mean - z_bar x se > 0 and
    mean >= min_avg_r, z_bar = Sidak bar for `m` hypotheses at a 1 % family-wise error rate. Looking every day at a fixed bar
    gets crossed by luck sooner or later (fx-board docs/REVISION-2026-10-12-forward-rule.md measured ~10 % instead of 1 %);
  - "rejected" may be decided at any look from `min_trades`: the 95 % upper bound < min_avg_r;
  - otherwise "accumulating" (fewer than eval_trades) or "unclear" (final).

eval_trades is 200 here, not fx-board's 100: scalping produces trades fast enough, and a scalper's edge per trade is small.
Fix m and eval_trades BEFORE the first forward trade; changing them after seeing results makes the test meaningless.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import NormalDist, median

FORWARD = {"fwer": 0.01, "min_trades": 30, "eval_trades": 200, "min_avg_r": 0.05, "reject_z": 1.645}


@dataclass(frozen=True)
class Stats:
    n: int
    mean: float
    se: float
    z: float
    clusters: int


def family_z(m: int, fwer: float = FORWARD["fwer"]) -> float:
    alpha = 1 - (1 - fwer) ** (1 / max(1, int(m)))
    return NormalDist().inv_cdf(1 - alpha)


def _day(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, timezone.utc).date().isoformat()


def clustered_mean(trades: list[tuple[int, float]]) -> Stats:
    """trades: [(entry time ms, R)]."""
    n = len(trades)
    if n < 2:
        return Stats(n, trades[0][1] if n else 0.0, math.inf, 0.0, n)
    mean = sum(r for _, r in trades) / n
    by_day: dict[str, float] = {}
    for t, r in trades:
        by_day[_day(t)] = by_day.get(_day(t), 0.0) + (r - mean)
    g = len(by_day)
    se = math.sqrt(g / (g - 1) * sum(v * v for v in by_day.values())) / n if g > 1 else math.inf
    return Stats(n, mean, se, mean / se if 0 < se < math.inf else 0.0, g)


def judge(trades: list[tuple[int, float]], m: int = 1, rule: dict = FORWARD) -> dict:
    trades = sorted(trades)
    z_bar = family_z(m, rule["fwer"])
    a = clustered_mean(trades)
    out = {"n": a.n, "avg_r": a.mean, "se": a.se, "z": a.z, "days": a.clusters, "z_bar": z_bar,
           "lb": a.mean - z_bar * a.se, "ub95": a.mean + rule["reject_z"] * a.se, "eval_avg_r": None, "eval_lb": None}
    status = None
    if a.n >= rule["eval_trades"]:
        h = clustered_mean(trades[: rule["eval_trades"]])  # the single pre-fixed look
        out["eval_avg_r"], out["eval_lb"] = h.mean, h.mean - z_bar * h.se
        if out["eval_lb"] > 0 and h.mean >= rule["min_avg_r"]:
            status = "confirming"
    if status is None:
        if a.n >= rule["min_trades"] and out["ub95"] < rule["min_avg_r"]:
            status = "rejected"
        else:
            status = "unclear" if a.n >= rule["eval_trades"] else "accumulating"
    out["status"] = status
    return out


def execution_report(rows: list[dict]) -> dict:
    """Live vs. assumed execution, from journal trades: if live slippage or latency is worse than the back-test assumed, the
    back-test's numbers do not apply to this account."""
    slip = [r["slippage"] for r in rows if r.get("slippage") is not None]
    lat = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)
    return {
        "trades": len(rows),
        "slippage_median": median(slip) if slip else None,
        "slippage_worst": max(slip) if slip else None,
        "latency_median_ms": median(lat) if lat else None,
        "latency_p95_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None,
    }
