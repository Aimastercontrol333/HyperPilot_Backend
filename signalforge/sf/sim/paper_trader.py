"""
Paper-trading engine.

Given the basket wallets' real round-trips (from features.metrics) and the fill
model, it replays/forwards them onto a sim portfolio and produces an honest,
timestamped track record:

  - every mirrored trade priced through the delay/slippage/fee/funding model
  - OUR OWN risk overlay enforced independently of the trader (own stop-loss,
    leverage cap, per-trader / per-asset / portfolio caps)
  - capacity tested at multiple notional sizes

This is what the Phase-1 dashboard publishes. It is NOT a backtest: in live
mode you feed it new closed round-trips as they arrive; in replay mode you feed
it history to validate. Same engine, same costs, either way.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import config as C
from ..features.metrics import RoundTrip
from . import fill_model as fm


@dataclass
class SimTrade:
    coin: str
    side: str
    open_ms: int
    close_ms: int
    trader: str
    weight: float
    notional: float
    gross_ret_pct: float       # the trader's raw round-trip return
    net_ret_pct: float         # after our frictions + own-stop overlay
    pnl_usd: float
    costs_bps: float
    funding_usd: float
    stopped_by_us: bool


@dataclass
class SimResult:
    start_equity: float
    end_equity: float
    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)  # (ms, equity)

    # headline stats
    total_return_pct: float = 0.0
    win_rate: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    n_trades: int = 0
    avg_cost_bps: float = 0.0


def _net_round_trip(trip: RoundTrip, notional_usd: float,
                    funding_series_bps_8h: list[float] | None,
                    own_stop_pct: float) -> tuple[float, float, float, bool]:
    """
    Returns (net_ret_pct, costs_bps, funding_usd, stopped_by_us).

    We start from the trader's gross round-trip return, subtract round-trip
    friction (spread+slippage+drift+2*fee), subtract funding, and then apply our
    own stop: if the realized net return is worse than -own_stop_pct we assume we
    exited at the stop (bounded loss), which is exactly the discipline overlay
    that is part of the product's value.
    """
    costs_bps = fm.round_trip_cost_bps(trip.coin, notional_usd)
    funding_usd = fm.funding_cost_usd(notional_usd, trip.direction,
                                      trip.hold_s / 3600.0, funding_series_bps_8h)
    funding_pct = funding_usd / notional_usd * 100.0 if notional_usd else 0.0
    net = trip.ret_pct - costs_bps / 100.0 - funding_pct
    stopped = False
    if net < -own_stop_pct:
        net = -own_stop_pct
        stopped = True
    return net, costs_bps, funding_usd, stopped


def run_paper_sim(
    basket: list[tuple[str, float]],            # [(address, weight), ...] weights sum<=1
    trips_by_wallet: dict[str, list[RoundTrip]],
    funding_by_coin: dict[str, list[float]] | None = None,
    start_equity: float = C.PORTFOLIO["start_equity_usd"],
    own_stop_pct: float = C.PORTFOLIO["own_stop_loss_pct"],
) -> SimResult:
    funding_by_coin = funding_by_coin or {}
    # gather all trips with their wallet weight, ordered by close time (realized)
    pending: list[tuple[RoundTrip, str, float]] = []
    wmap = dict(basket)
    for addr, w in basket:
        for t in trips_by_wallet.get(addr, []):
            pending.append((t, addr, w))
    pending.sort(key=lambda x: x[0].close_ms)

    equity = start_equity
    res = SimResult(start_equity=start_equity, end_equity=start_equity)
    peak = start_equity
    rets: list[float] = []
    cost_acc: list[float] = []

    for trip, addr, weight in pending:
        # notional sized to current equity * weight, capped at portfolio leverage
        notional = min(equity * weight,
                       equity * C.PORTFOLIO["max_portfolio_leverage"] * weight)
        net_pct, costs_bps, funding_usd, stopped = _net_round_trip(
            trip, notional, funding_by_coin.get(trip.coin), own_stop_pct)
        pnl = notional * net_pct / 100.0
        equity += pnl
        rets.append(net_pct)
        cost_acc.append(costs_bps)
        peak = max(peak, equity)
        res.max_drawdown_pct = max(res.max_drawdown_pct, (peak - equity) / peak * 100.0)
        res.equity_curve.append((trip.close_ms, round(equity, 2)))
        res.trades.append(SimTrade(
            coin=trip.coin, side=trip.direction, open_ms=trip.open_ms,
            close_ms=trip.close_ms, trader=addr, weight=weight, notional=round(notional, 2),
            gross_ret_pct=round(trip.ret_pct, 3), net_ret_pct=round(net_pct, 3),
            pnl_usd=round(pnl, 2), costs_bps=round(costs_bps, 1),
            funding_usd=round(funding_usd, 2), stopped_by_us=stopped,
        ))

    res.end_equity = round(equity, 2)
    res.n_trades = len(res.trades)
    res.total_return_pct = round((equity / start_equity - 1) * 100.0, 2)
    if rets:
        wins = sum(1 for r in rets if r > 0)
        res.win_rate = round(wins / len(rets), 3)
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        sd = var ** 0.5
        res.sharpe = round((mean / sd) * (len(rets) ** 0.5), 2) if sd > 0 else 0.0
        res.avg_cost_bps = round(sum(cost_acc) / len(cost_acc), 1)
    return res


def capacity_curve(basket, trips_by_wallet, funding_by_coin=None) -> dict[int, dict]:
    """Run the same sim at several portfolio sizes; slippage scales with size."""
    out = {}
    for size in C.PORTFOLIO["capacity_test_sizes"]:
        r = run_paper_sim(basket, trips_by_wallet, funding_by_coin, start_equity=size)
        out[size] = {"total_return_pct": r.total_return_pct,
                     "max_drawdown_pct": round(r.max_drawdown_pct, 2),
                     "sharpe": r.sharpe, "avg_cost_bps": r.avg_cost_bps}
    return out
