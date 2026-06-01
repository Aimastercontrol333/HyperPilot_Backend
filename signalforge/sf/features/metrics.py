"""
Feature engineering: raw fills -> round-trip trades -> behavioral metrics.

Hyperliquid fills carry `dir` ("Open Long", "Close Short", ...) and `closedPnl`,
which lets us stitch a position's opens and closes into round-trips without
guessing. From those round-trips we derive the inputs the Safety Score needs:
drawdown, per-trade loss distribution, leverage proxy, return series, cadence,
martingale signal, etc.

Leverage caveat (be honest): fills don't include leverage directly. We compute
a notional/equity proxy and, where possible, cross-check against the current
clearinghouse leverage. It's an approximation and flagged as such.
"""
from __future__ import annotations

import math
import statistics as stats
from dataclasses import dataclass, field
from typing import Any

from .. import config as C


@dataclass
class RoundTrip:
    coin: str
    direction: str          # "long" | "short"
    open_ms: int
    close_ms: int
    qty: float              # absolute base size
    avg_entry: float
    avg_exit: float
    closed_pnl: float       # realized PnL (USD) net of HL fees in `fee`
    fees: float
    notional: float         # avg_entry * qty
    ret_pct: float          # closed_pnl / notional * 100

    @property
    def hold_s(self) -> float:
        return max((self.close_ms - self.open_ms) / 1000.0, 0.0)


def _is_open(dir_str: str) -> bool:
    return dir_str.startswith("Open")


def _dir_to_side(dir_str: str) -> str:
    return "long" if "Long" in dir_str else "short"


def build_round_trips(fills: list[dict]) -> list[RoundTrip]:
    """
    Walk fills per coin, maintaining running position. When a position returns to
    (near) flat, emit a RoundTrip. Uses `closedPnl` from close fills for realized
    PnL so we don't have to model fees twice.
    """
    fills = sorted(fills, key=lambda f: int(f["time"]))
    trips: list[RoundTrip] = []
    # per-coin open lot accumulator
    book: dict[str, dict[str, Any]] = {}

    for f in fills:
        coin = f["coin"]
        dirs = f.get("dir", "")
        if not dirs or "Long" not in dirs and "Short" not in dirs:
            continue
        px = float(f["px"]); sz = float(f["sz"])
        t = int(f["time"]); fee = float(f.get("fee", 0) or 0)
        cpnl = float(f.get("closedPnl", 0) or 0)
        side = _dir_to_side(dirs)
        st = book.get(coin)

        if _is_open(dirs):
            if st is None or st["qty"] == 0:
                book[coin] = {"side": side, "qty": sz, "entry_notional": px * sz,
                              "open_ms": t, "fees": fee}
            else:
                # adding to existing position (same side assumed for opens)
                st["qty"] += sz
                st["entry_notional"] += px * sz
                st["fees"] += fee
        else:  # Close
            if st is None or st["qty"] == 0:
                continue  # close without a tracked open (history boundary) -> skip
            close_qty = min(sz, st["qty"])
            avg_entry = st["entry_notional"] / st["qty"] if st["qty"] else px
            st["qty"] -= close_qty
            st["fees"] += fee
            st["_close_notional"] = st.get("_close_notional", 0.0) + px * close_qty
            st["_close_qty"] = st.get("_close_qty", 0.0) + close_qty
            st["_pnl"] = st.get("_pnl", 0.0) + cpnl
            if st["qty"] <= 1e-9:  # flat -> emit round trip
                cq = st["_close_qty"]
                avg_exit = st["_close_notional"] / cq if cq else px
                notional = avg_entry * cq
                trips.append(RoundTrip(
                    coin=coin, direction=st["side"], open_ms=st["open_ms"], close_ms=t,
                    qty=cq, avg_entry=avg_entry, avg_exit=avg_exit,
                    closed_pnl=st["_pnl"], fees=st["fees"], notional=notional,
                    ret_pct=(st["_pnl"] / notional * 100.0) if notional else 0.0,
                ))
                book[coin] = {"side": side, "qty": 0}
    return trips


@dataclass
class WalletMetrics:
    address: str
    n_trades: int
    history_days: float
    win_rate: float
    avg_ret_pct: float
    expectancy_pct: float            # mean per-trade return (the real edge metric)
    max_single_loss_pct: float       # worst single round-trip (abs)
    max_drawdown_pct: float          # equity-curve drawdown
    sharpe: float
    sortino: float
    avg_leverage_proxy: float
    max_leverage_proxy: float
    pnl_consistency: float           # 0..1, 1 = very steady
    frequency_cv: float              # coefficient of variation of inter-trade gaps
    martingale_score: float          # 0..1, 1 = strong martingale (bad)
    liquidations: int
    cum_return_pct: float
    archetype: str
    extra: dict = field(default_factory=dict)


def _equity_curve(trips: list[RoundTrip]) -> list[float]:
    eq, cur = [], 0.0
    for t in trips:
        cur += t.ret_pct          # in "R-ish" units of notional %
        eq.append(cur)
    return eq


def _max_drawdown(eq: list[float]) -> float:
    peak, mdd = -1e18, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def _classify(direction_mix: float, avg_hold_h: float, freq_per_day: float,
              avg_lev: float) -> str:
    if avg_hold_h < 1 and freq_per_day > 8:
        return "Scalper"
    if avg_hold_h < 8 and freq_per_day > 2:
        return "Momentum"
    if avg_hold_h > 72:
        return "Position"
    if avg_lev <= 3 and avg_hold_h > 24:
        return "Conservative"
    if freq_per_day < 1:
        return "Swing"
    return "Swing"


def _av_at(history: list, ts: int) -> float | None:
    """Account value at (or just before) ts, from a sorted [(ts,value),...] series."""
    if not history:
        return None
    val = None
    for pts, v in history:
        if pts <= ts:
            val = v
        else:
            break
    return val if val is not None else history[0][1]


def _real_leverage(trips: list[RoundTrip], av_history: list):
    """Genuine leverage = trade notional / account equity AT THE TIME of the trade.
    Returns (avg, max, n_valid). Trades where equity is unknown/tiny are skipped so
    a near-zero balance can't manufacture absurd leverage."""
    levs = []
    for t in trips:
        eq = _av_at(av_history, t.open_ms)
        if eq and eq > 100:                      # need a real balance to divide by
            levs.append(min(t.notional / eq, 75.0))   # cap data-noise outliers
    if len(levs) < 5:
        return None, None, 0
    return stats.fmean(levs), max(levs), len(levs)


def _maker_and_fanout(fills: list[dict]):
    """maker_ratio = share of passive (non-crossed) fills — high => market-maker,
    whose spread-capture edge a delayed taker-follower CANNOT replicate.
    fanout = most distinct coins opened within any 5-minute window — high => a
    systematic basket/MM bot rather than a discretionary trader."""
    if not fills:
        return None, 0
    crossed_vals = [f.get("crossed") for f in fills if "crossed" in f]
    maker_ratio = (sum(1 for c in crossed_vals if c is False) / len(crossed_vals)
                   if crossed_vals else None)
    opens = sorted([(int(f["time"]), f["coin"]) for f in fills
                    if str(f.get("dir", "")).startswith("Open")])
    fanout = 0
    for i, (t0, _) in enumerate(opens):
        coins = {c for t, c in opens[i:] if t - t0 <= 300_000}
        fanout = max(fanout, len(coins))
        if fanout >= 25:
            break
    return maker_ratio, fanout


def compute_metrics(address: str, trips: list[RoundTrip],
                    account_value: float | None = None,
                    liquidations: int = 0,
                    av_history: list | None = None,
                    fills: list[dict] | None = None) -> WalletMetrics | None:
    if not trips:
        return None
    rets = [t.ret_pct for t in trips]
    n = len(trips)
    span_days = max((trips[-1].close_ms - trips[0].open_ms) / 86_400_000.0, 1e-6)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    win_rate = len(wins) / n
    avg_ret = stats.fmean(rets)
    mean = avg_ret
    sd = stats.pstdev(rets) if n > 1 else 0.0
    downside = [min(r, 0) for r in rets]
    dsd = (sum(d * d for d in downside) / n) ** 0.5 if n else 0.0
    # annualize-ish per trade (kept simple + comparable across wallets)
    sharpe = (mean / sd * math.sqrt(n / span_days * 365)) if sd > 0 else 0.0
    sortino = (mean / dsd * math.sqrt(n / span_days * 365)) if dsd > 0 else 0.0

    eq = _equity_curve(trips)
    mdd = _max_drawdown(eq)
    max_single_loss = abs(min(rets)) if losses else 0.0

    # REAL leverage: trade notional vs account equity at the time of each trade.
    # If we can't measure it reliably, leave it unknown (None) rather than fake it.
    avg_lev, max_lev, lev_known = None, None, False
    if av_history:
        a, mx, cnt = _real_leverage(trips, av_history)
        if cnt >= 5:
            avg_lev, max_lev, lev_known = a, mx, True

    # pnl consistency: 1 - normalized volatility of returns, clamped
    pnl_consistency = max(0.0, 1.0 - (sd / (abs(mean) + abs(sd) + 1e-9)))

    # cadence regularity
    gaps = [(trips[i].open_ms - trips[i - 1].close_ms) / 1000.0 for i in range(1, n)]
    gaps = [g for g in gaps if g >= 0]
    if len(gaps) > 1 and stats.fmean(gaps) > 0:
        freq_cv = stats.pstdev(gaps) / stats.fmean(gaps)
    else:
        freq_cv = 1.0

    # martingale: do sizes increase right after losses?
    mart_hits = 0; mart_chances = 0
    for i in range(1, n):
        if trips[i - 1].ret_pct < 0:
            mart_chances += 1
            if trips[i].notional > trips[i - 1].notional * 1.5:
                mart_hits += 1
    martingale = (mart_hits / mart_chances) if mart_chances else 0.0

    avg_hold_h = stats.fmean([t.hold_s for t in trips]) / 3600.0
    freq_per_day = n / span_days
    long_share = sum(1 for t in trips if t.direction == "long") / n
    archetype = _classify(long_share, avg_hold_h, freq_per_day, avg_lev if lev_known else 5.0)

    # market-maker / uncopyable detection
    maker_ratio, fanout = _maker_and_fanout(fills)
    is_mm = ((maker_ratio is not None and maker_ratio >= 0.6) or fanout >= 10
             or (freq_per_day > 50 and abs(avg_ret) < 0.02))
    # anti-scalp: a follower can't mirror sub-minute holds before price moves away
    too_fast = (avg_hold_h * 60.0) < C.MIN_HOLD_MINUTES
    if is_mm:
        archetype = "Market Maker"
    elif too_fast:
        archetype = "Latency Scalper"

    return WalletMetrics(
        address=address, n_trades=n, history_days=span_days, win_rate=win_rate,
        avg_ret_pct=avg_ret, expectancy_pct=avg_ret, max_single_loss_pct=max_single_loss,
        max_drawdown_pct=mdd, sharpe=sharpe, sortino=sortino,
        avg_leverage_proxy=avg_lev, max_leverage_proxy=max_lev,
        pnl_consistency=pnl_consistency, frequency_cv=freq_cv,
        martingale_score=martingale, liquidations=liquidations,
        cum_return_pct=eq[-1] if eq else 0.0, archetype=archetype,
        extra={"avg_hold_h": avg_hold_h, "freq_per_day": freq_per_day,
               "leverage_is_proxy": not lev_known, "leverage_known": lev_known,
               "maker_ratio": maker_ratio, "fanout": fanout, "is_market_maker": is_mm,
               "too_fast": too_fast, "avg_hold_min": round(avg_hold_h * 60.0, 1)},
    )
