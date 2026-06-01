"""
Safety Score engine.

Turns WalletMetrics into a transparent 0-100 score:
  1. Each factor -> a 0..1 sub-score via a calibrated, explainable function.
  2. Weighted sum (config.WEIGHTS) -> raw 0..100.
  3. Empirical-Bayes shrinkage by sample size (small n -> pulled to prior mean).
  4. Hard AUTO-BAN gates override everything (any liquidation, >40x, martingale,
     no stop-loss discipline, wash/bot pattern) -> banned, score floored.
  5. Auto-pass eligibility flag for basket inclusion.

Everything is explainable: we return the per-factor contributions so the
dashboard / a user can see *why* a wallet scored what it did. That transparency
is the product.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import config as C
from ..features.metrics import WalletMetrics


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ---- factor sub-scores (each returns 0..1, higher = safer) -----------------
def _f_drawdown(m: WalletMetrics) -> float:
    # 0% dd -> 1.0 ; 25% dd -> ~0.5 ; 50%+ -> 0
    return _clamp01(1.0 - m.max_drawdown_pct / 50.0)


def _f_stoploss(m: WalletMetrics) -> float:
    # bounded worst single loss is the signal of stop-loss discipline
    # <=5% -> ~1 ; 10% -> ~0.5 ; >=20% -> 0
    return _clamp01(1.0 - (m.max_single_loss_pct - 5.0) / 15.0)


def _f_leverage(m: WalletMetrics) -> float:
    # only meaningful when leverage is real (not proxy/unknown); else neutral
    if m.extra.get("leverage_is_proxy") or m.avg_leverage_proxy is None:
        return 0.5
    return _clamp01(1.0 - (m.avg_leverage_proxy - 2.0) / 13.0)  # 2x->1, 15x->0


def _f_consistency(m: WalletMetrics) -> float:
    return _clamp01(m.pnl_consistency)


def _f_rar(m: WalletMetrics) -> float:
    # Sharpe 0 -> 0 ; 1.5 -> 0.6 ; 3+ -> 1
    return _clamp01(m.sharpe / 3.0)


def _f_frequency(m: WalletMetrics) -> float:
    # cadence regularity: cv 0 -> 1 ; cv 2+ -> 0
    return _clamp01(1.0 - m.frequency_cv / 2.0)


def _f_anti_martingale(m: WalletMetrics) -> float:
    return _clamp01(1.0 - m.martingale_score)


def _f_wallet_trust(m: WalletMetrics) -> float:
    # ramps with sample size and history length
    by_n = _clamp01(m.n_trades / C.PREFERRED_TRADES)
    by_days = _clamp01(m.history_days / C.MIN_HISTORY_DAYS)
    return _clamp01(0.6 * by_n + 0.4 * by_days)


FACTORS = {
    "drawdown_control": _f_drawdown,
    "stoploss_discipline": _f_stoploss,
    "leverage_discipline": _f_leverage,
    "pnl_consistency": _f_consistency,
    "risk_adjusted_return": _f_rar,
    "frequency_stability": _f_frequency,
    "anti_martingale": _f_anti_martingale,
    "wallet_trust": _f_wallet_trust,
}


@dataclass
class ScoreResult:
    address: str
    score: float                       # final 0-100 after shrinkage + gates
    raw_score: float                   # pre-shrinkage weighted score
    eligible: bool                     # passes auto-pass AND not banned
    banned: bool
    ban_reasons: list[str]
    archetype: str
    factors: dict[str, float] = field(default_factory=dict)   # sub-scores 0..1
    contributions: dict[str, float] = field(default_factory=dict)  # weighted pts
    notes: list = field(default_factory=list)   # non-banning exclusion reasons
    metrics: WalletMetrics | None = None


def _auto_ban(m: WalletMetrics) -> list[str]:
    reasons = []
    if m.liquidations > C.AUTO_BAN["max_liquidations"]:
        reasons.append("repeat_liquidations")
    if (not m.extra.get("leverage_is_proxy")) and m.max_leverage_proxy is not None \
            and m.max_leverage_proxy >= C.AUTO_BAN["max_leverage_ever"]:
        reasons.append("excessive_leverage")
    if m.martingale_score >= 0.5:
        reasons.append("martingale_pattern")
    if m.max_single_loss_pct >= 35.0:  # no evidence of any stop discipline
        reasons.append("no_stoploss_discipline")
    # crude wash/bot heuristic: implausibly high frequency with ~0 net edge
    if m.extra.get("freq_per_day", 0) > 50 and abs(m.expectancy_pct) < 0.02:
        reasons.append("wash_or_bot_pattern")
    return reasons


def _auto_pass(m: WalletMetrics) -> bool:
    ap = C.AUTO_PASS
    return (
        m.max_single_loss_pct < ap["max_single_trade_loss_pct"]
        and m.max_drawdown_pct < ap["max_drawdown_pct"]
        and (m.extra.get("leverage_is_proxy") or m.avg_leverage_proxy is None
             or m.avg_leverage_proxy < ap["avg_leverage"])
        and m.sharpe > ap["min_sharpe"]
        and m.history_days >= ap["min_history_days"]
    )


def _shrink(raw: float, n: int) -> float:
    w = n / (n + C.SHRINKAGE_K)
    return w * raw + (1 - w) * C.PRIOR_MEAN


def score_wallet(m: WalletMetrics) -> ScoreResult | None:
    if m is None or m.n_trades < C.MIN_TRADES:
        return None

    sub = {name: fn(m) for name, fn in FACTORS.items()}
    contrib = {name: sub[name] * C.WEIGHTS[name] * 100.0 for name in sub}
    raw = sum(contrib.values())
    shrunk = _shrink(raw, m.n_trades)

    ban_reasons = _auto_ban(m)
    banned = len(ban_reasons) > 0
    final = 0.0 if banned else round(shrunk, 1)

    # Market-makers are not "bad" — their spread-capture edge just isn't copyable
    # by a delayed taker-follower. Exclude from the basket, but don't ban or zero.
    notes = []
    if m.extra.get("is_market_maker"):
        notes.append("uncopyable_market_maker")
    eligible = (not banned) and (not notes) and _auto_pass(m)

    return ScoreResult(
        address=m.address, score=final, raw_score=round(raw, 1),
        eligible=eligible, banned=banned, ban_reasons=ban_reasons,
        archetype=m.archetype, factors={k: round(v, 3) for k, v in sub.items()},
        contributions={k: round(v, 2) for k, v in contrib.items()},
        notes=notes, metrics=m,
    )
