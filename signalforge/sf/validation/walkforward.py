"""
Validation — the test that tells you whether any of this works.

Two jobs:

1. walk_forward(): strictly out-of-sample. Score each wallet on a training
   window, then measure its REAL forward round-trip performance on the next
   window (priced through the fill model). Bucket by score decile. If the top
   decile doesn't beat the bottom decile forward, the Safety Score has no
   predictive power yet and must be fixed BEFORE Phase 1 goes public. This is
   the make-or-break experiment; run it on history first.

2. measure_delay_drift(): replace the assumed `delay_drift_bps` with a measured
   one. For real entries, compare price at the trader's fill time vs price at
   (fill time + your latency) in the trade direction. The distribution of that
   move IS your empirical edge-decay.
"""
from __future__ import annotations

import statistics as stats
from dataclasses import dataclass

from .. import config as C
from ..features.metrics import RoundTrip, compute_metrics, build_round_trips
from ..scoring.safety_score import score_wallet
from ..sim import fill_model as fm


@dataclass
class DecileResult:
    decile: int
    n_wallets: int
    avg_score: float
    avg_forward_net_ret_pct: float
    win_rate: float


def _split_trips(trips: list[RoundTrip], boundary_ms: int):
    train = [t for t in trips if t.close_ms <= boundary_ms]
    test = [t for t in trips if t.open_ms > boundary_ms]
    return train, test


def walk_forward(fills_by_wallet: dict[str, list[dict]], boundary_ms: int,
                 av_by_wallet: dict[str, list] | None = None) -> list[DecileResult]:
    """
    boundary_ms splits each wallet's history into train (score) / test (forward).
    Scores the train window with the SAME signals production uses (real leverage
    from account-value history, market-maker detection from fills), then measures
    the wallet's real forward net return. Returns decile buckets of
    (avg score) vs (avg forward net return).
    """
    av_by_wallet = av_by_wallet or {}
    scored: list[tuple[float, float, bool]] = []  # (score, fwd_net_ret, win)
    for addr, fills in fills_by_wallet.items():
        trips = build_round_trips(fills)
        train, test = _split_trips(trips, boundary_ms)
        if len(train) < C.MIN_TRADES or not test:
            continue
        train_fills = [f for f in fills if int(f["time"]) <= boundary_ms]
        m = compute_metrics(addr, train, av_history=av_by_wallet.get(addr), fills=train_fills)
        sr = score_wallet(m)
        if sr is None:
            continue
        # forward performance, net of frictions, equal-notional per trade
        fwd = []
        for t in test:
            cost_bps = fm.round_trip_cost_bps(t.coin, 10_000)
            fwd.append(t.ret_pct - cost_bps / 100.0)
        if not fwd:
            continue
        scored.append((sr.score, stats.fmean(fwd), stats.fmean(fwd) > 0))

    if not scored:
        return []
    scored.sort(key=lambda x: x[0])
    n = len(scored)
    out: list[DecileResult] = []
    for d in range(10):
        lo = d * n // 10
        hi = (d + 1) * n // 10 if d < 9 else n
        bucket = scored[lo:hi]
        if not bucket:
            continue
        out.append(DecileResult(
            decile=d + 1, n_wallets=len(bucket),
            avg_score=round(stats.fmean([b[0] for b in bucket]), 1),
            avg_forward_net_ret_pct=round(stats.fmean([b[1] for b in bucket]), 3),
            win_rate=round(stats.fmean([1.0 if b[2] else 0.0 for b in bucket]), 3),
        ))
    return out


def summarize_walk_forward(deciles: list[DecileResult]) -> dict:
    if not deciles:
        return {"verdict": "insufficient_data"}
    top = deciles[-1].avg_forward_net_ret_pct
    bot = deciles[0].avg_forward_net_ret_pct
    spread = round(top - bot, 3)
    # rank correlation-ish monotonicity check
    rets = [d.avg_forward_net_ret_pct for d in deciles]
    mono = sum(1 for i in range(1, len(rets)) if rets[i] >= rets[i - 1]) / (len(rets) - 1)
    verdict = "predictive" if (spread > 0 and mono >= 0.6) else "weak_or_none"
    return {"top_decile_fwd_ret": top, "bottom_decile_fwd_ret": bot,
            "spread_pct": spread, "monotonicity": round(mono, 2), "verdict": verdict}


def measure_delay_drift(client, fills: list[dict], latency_s: float | None = None) -> dict:
    """
    For each entry fill, compare price at fill time vs price latency_s later (in
    trade direction). Uses 1m candles around each fill. Returns the adverse-drift
    distribution in bps -> use the median to replace config.FILL['delay_drift_bps'].
    """
    latency_s = latency_s or (C.FILL["detection_latency_s"] + C.FILL["decision_latency_s"])
    drifts: list[float] = []
    opens = [f for f in fills if str(f.get("dir", "")).startswith("Open")][:200]
    for f in opens:
        coin = f["coin"]; t = int(f["time"]); px = float(f["px"])
        side = 1.0 if "Long" in f["dir"] else -1.0
        candles = client.candles(coin, "1m", t - 60_000, t + 180_000)
        if not candles:
            continue
        after = [c for c in candles if int(c["t"]) >= t + latency_s * 1000]
        if not after:
            continue
        px_after = float(after[0]["c"])
        drift_bps = side * (px_after - px) / px * 1e4  # +ve = moved against follower
        drifts.append(drift_bps)
    if not drifts:
        return {"measured": False}
    drifts.sort()
    return {"measured": True, "n": len(drifts),
            "median_bps": round(stats.median(drifts), 2),
            "p75_bps": round(drifts[int(len(drifts) * 0.75)], 2),
            "recommendation": "set config.FILL['delay_drift_bps'] to the median"}
