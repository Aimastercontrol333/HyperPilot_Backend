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
        # Truncate account-value history to the training window so the score can't peek at
        # post-boundary equity (that would leak the future into the daily-Sharpe calc).
        av_tr = [(ts, v) for ts, v in (av_by_wallet.get(addr) or []) if ts <= boundary_ms]
        m = compute_metrics(addr, train, av_history=av_tr, fills=train_fills)
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


# ─────────────────────────────────────────────────────────────────────────────
# v2 — survivability-aware analysis (the thesis is bounded loss, not mean return)
#
# Three upgrades over the legacy decile view above:
#   1. Bans are tested on the metric they target — forward BLOW-UPS / drawdown —
#      not average return (a gambler's average looks fine until the liquidation).
#   2. The score is ranked ONLY among non-banned ("copyable") wallets, in
#      QUINTILES (more wallets/bucket = less noise), on forward drawdown + blow-up.
#   3. raw vs shrunk score are both ranked, to reveal if shrinkage over-compresses.
# ─────────────────────────────────────────────────────────────────────────────

BLOWUP_DD_PCT = 25.0       # forward equity drawdown that counts as a survival failure
BLOWUP_TRADE_PCT = -35.0   # a single forward trade this bad = a no-stop-loss blow-up
MIN_TEST_TRADES = 5        # need a few forward trades or the forward metrics are noise
MIN_SCORED_TRUST = 40      # non-banned wallets needed before the ranking is trustworthy


@dataclass
class _WFRow:
    score: float
    raw: float
    banned: bool
    eligible: bool
    fwd: dict


def _additive_max_dd(net_rets: list[float]) -> float:
    """Max drawdown of the cumulative equity built from per-trade net % returns
    (same additive convention metrics.py uses for max_drawdown_pct)."""
    cur = peak = mdd = 0.0
    for r in net_rets:
        cur += r
        peak = max(peak, cur)
        mdd = max(mdd, peak - cur)
    return mdd


def _forward_outcomes(test: list[RoundTrip]) -> dict:
    nets = [t.ret_pct - fm.round_trip_cost_bps(t.coin, 10_000) / 100.0 for t in test]
    mean = stats.fmean(nets)
    sd = stats.pstdev(nets) if len(nets) > 1 else 0.0
    mdd = _additive_max_dd(nets)
    worst = min(nets)
    blew = (mdd >= BLOWUP_DD_PCT) or (worst <= BLOWUP_TRADE_PCT)
    return {"n": len(nets), "mean_ret": mean, "max_dd": mdd, "worst": worst,
            "blowup": blew, "sharpe": (mean / sd) if sd > 1e-9 else 0.0}


def _group(rows: list[_WFRow]) -> dict:
    if not rows:
        return {"n": 0}
    return {
        "n": len(rows),
        "blowup_rate": round(stats.fmean([1.0 if r.fwd["blowup"] else 0.0 for r in rows]), 3),
        "avg_fwd_max_dd_pct": round(stats.fmean([r.fwd["max_dd"] for r in rows]), 2),
        "avg_fwd_ret_pct": round(stats.fmean([r.fwd["mean_ret"] for r in rows]), 3),
        "avg_fwd_worst_pct": round(stats.fmean([r.fwd["worst"] for r in rows]), 2),
    }


def _agg(b: list[_WFRow], q: int) -> dict:
    return {
        "quintile": q, "n": len(b),
        "avg_score": round(stats.fmean([r.score for r in b]), 1),
        "avg_raw_score": round(stats.fmean([r.raw for r in b]), 1),
        "fwd_mean_ret_pct": round(stats.fmean([r.fwd["mean_ret"] for r in b]), 3),
        "fwd_max_dd_pct": round(stats.fmean([r.fwd["max_dd"] for r in b]), 2),
        "blowup_rate": round(stats.fmean([1.0 if r.fwd["blowup"] else 0.0 for r in b]), 3),
        "fwd_sharpe": round(stats.fmean([r.fwd["sharpe"] for r in b]), 2),
        "win_rate": round(stats.fmean([1.0 if r.fwd["mean_ret"] > 0 else 0.0 for r in b]), 3),
    }


def _quintiles(rows: list[_WFRow], key, k: int = 5) -> list[dict]:
    rows = sorted(rows, key=key)
    n = len(rows)
    out = []
    for i in range(k):
        lo, hi = i * n // k, ((i + 1) * n // k if i < k - 1 else n)
        b = rows[lo:hi]
        if b:
            out.append(_agg(b, i + 1))
    return out


def _survivability_spread(qs: list[dict]) -> dict:
    if len(qs) < 2:
        return {}
    top, bot = qs[-1], qs[0]
    dds = [q["fwd_max_dd_pct"] for q in qs]
    mono = sum(1 for i in range(1, len(dds)) if dds[i] <= dds[i - 1]) / (len(dds) - 1)
    return {
        "dd_reduction_top_vs_bottom_pp": round(bot["fwd_max_dd_pct"] - top["fwd_max_dd_pct"], 2),
        "blowup_reduction_top_vs_bottom_pp": round((bot["blowup_rate"] - top["blowup_rate"]) * 100, 1),
        "return_spread_top_vs_bottom_pp": round(top["fwd_mean_ret_pct"] - bot["fwd_mean_ret_pct"], 3),
        "drawdown_monotonicity": round(mono, 2),
    }


def _assemble(rows: list[_WFRow]) -> dict:
    banned = [r for r in rows if r.banned]
    keep = [r for r in rows if not r.banned]

    # 1) ban effectiveness — tested on forward blow-ups / drawdown, not return
    ban_eff = {"banned": _group(banned), "scored": _group(keep)}
    if banned and keep:
        ban_eff["blowup_rate_reduction_pp"] = round(
            (ban_eff["banned"]["blowup_rate"] - ban_eff["scored"]["blowup_rate"]) * 100, 1)
        ban_eff["dd_reduction_pp"] = round(
            ban_eff["banned"]["avg_fwd_max_dd_pct"] - ban_eff["scored"]["avg_fwd_max_dd_pct"], 2)
        ban_eff["verdict"] = ("bans_avoid_blowups"
                              if (ban_eff["blowup_rate_reduction_pp"] >= 10 or ban_eff["dd_reduction_pp"] >= 5)
                              else "inconclusive")
    else:
        ban_eff["verdict"] = "insufficient"

    # 2) score ranking among copyable (non-banned) wallets, by shrunk and raw score
    score_q = _quintiles(keep, key=lambda r: r.score)
    raw_q = _quintiles(keep, key=lambda r: r.raw)
    surv = _survivability_spread(score_q)
    surv_raw = _survivability_spread(raw_q)

    # raw-vs-shrunk diagnostic
    shrink_hint = ""
    if surv and surv_raw:
        if surv_raw.get("dd_reduction_top_vs_bottom_pp", 0) - surv.get("dd_reduction_top_vs_bottom_pp", 0) >= 3:
            shrink_hint = ("The raw (un-shrunk) score separates survivors better than the shrunk score — "
                           "shrinkage (K) is likely over-compressing the scores; try lowering it.")

    # 3) survivability verdict (the thesis = bounded loss, not mean return)
    dd = surv.get("dd_reduction_top_vs_bottom_pp", 0.0)
    blow = surv.get("blowup_reduction_top_vs_bottom_pp", 0.0)
    ret = surv.get("return_spread_top_vs_bottom_pp", 0.0)
    mono = surv.get("drawdown_monotonicity", 0.0)
    protects = (dd >= 3 and mono >= 0.6) or (blow >= 10)
    earns = ret > 0.2
    dimension = ("both" if protects and earns else
                 "survivability" if protects else
                 "return_only" if earns else "none")

    if len(keep) < MIN_SCORED_TRUST:
        verdict = "insufficient_data"
    elif protects:
        verdict = "predictive"
    else:
        verdict = "weak_or_none"

    return {
        "scored_wallets": len(keep),
        "banned_wallets": len(banned),
        "ban_effectiveness": ban_eff,
        "score_quintiles": score_q,
        "raw_quintiles": raw_q,
        "survivability": surv,
        "survivability_raw": surv_raw,
        "predictive_dimension": dimension,
        "verdict": verdict,
        "_shrink_hint": shrink_hint,
    }


def analyze(fills_by_wallet: dict[str, list[dict]], boundary_ms: int,
            av_by_wallet: dict[str, list] | None = None) -> dict:
    """Survivability-aware walk-forward. Returns the rich analysis dict that
    build_report wraps with a plain-English verdict."""
    av_by_wallet = av_by_wallet or {}
    rows: list[_WFRow] = []
    for addr, fills in fills_by_wallet.items():
        trips = build_round_trips(fills)
        train, test = _split_trips(trips, boundary_ms)
        if len(train) < C.MIN_TRADES or len(test) < MIN_TEST_TRADES:
            continue
        train_fills = [f for f in fills if int(f["time"]) <= boundary_ms]
        av_tr = [(ts, v) for ts, v in (av_by_wallet.get(addr) or []) if ts <= boundary_ms]
        m = compute_metrics(addr, train, av_history=av_tr, fills=train_fills)
        sr = score_wallet(m)
        if sr is None:
            continue
        rows.append(_WFRow(sr.score, sr.raw_score, sr.banned, sr.eligible, _forward_outcomes(test)))

    return _assemble(rows)

