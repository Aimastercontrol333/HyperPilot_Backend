"""
Fill model — the honest cost of being a follower.

Core truth: you never get the trader's price. You get the market AFTER your
detect+decide delay, you cross the spread, you pay size-based impact, and the
price has usually drifted against you while you were late. Then funding accrues
while the position is open, and you pay taker fees both ways.

    effective_entry = ref_px * (1 + side * (half_spread + slippage + delay_drift))
    effective_exit  = ref_px * (1 - side * (half_spread + slippage))   # symmetric

`delay_drift_bps` is a placeholder until you MEASURE it via replay
(validation.walkforward.measure_delay_drift). Measuring beats assuming.
"""
from __future__ import annotations

import math  # noqa: F401
from dataclasses import dataclass

from .. import config as C


@dataclass
class FillCosts:
    half_spread_bps: float
    slippage_bps: float
    delay_drift_bps: float
    fee_bps: float

    @property
    def entry_cost_bps(self) -> float:
        return self.half_spread_bps + self.slippage_bps + self.delay_drift_bps

    @property
    def exit_cost_bps(self) -> float:
        return self.half_spread_bps + self.slippage_bps


def estimate_costs(coin: str, notional_usd: float, top_depth_usd: float | None = None,
                   measured_half_spread_bps: float | None = None) -> FillCosts:
    tier = C.coin_tier(coin)
    # Real book spread when the live engine supplies it; tier fallback otherwise.
    if measured_half_spread_bps is not None and measured_half_spread_bps > 0:
        half_spread = measured_half_spread_bps
    else:
        half_spread = C.FILL["half_spread_bps"][tier]
    # square-root impact, anchored to a $100k clip: at 100k -> impact_bps_per_100k,
    # at 1M -> x3.16, at 10k -> x0.32. Realistic and size-sensitive.
    base_impact = C.FILL["impact_bps_per_100k"][tier]
    slippage = base_impact * (max(notional_usd, 1) / 100_000.0) ** 0.5
    slippage = min(slippage, 300.0)  # clamp pathological thin-book cases
    # adverse drift derived from how late we are (total latency) — falls to ~1-4 bps at 300ms
    total_latency = C.FILL["detection_latency_s"] + C.FILL["decision_latency_s"]
    drift = C.FILL["drift_bps_per_s"][tier] * total_latency
    return FillCosts(
        half_spread_bps=half_spread,
        slippage_bps=slippage,
        delay_drift_bps=drift,
        fee_bps=C.FILL["taker_fee_bps"],
    )


def apply_entry(ref_px: float, side: str, costs: FillCosts) -> float:
    s = 1.0 if side == "long" else -1.0
    return ref_px * (1.0 + s * costs.entry_cost_bps / 1e4)


def apply_exit(ref_px: float, side: str, costs: FillCosts) -> float:
    s = 1.0 if side == "long" else -1.0
    return ref_px * (1.0 - s * costs.exit_cost_bps / 1e4)


def funding_cost_usd(notional_usd: float, side: str, hold_hours: float,
                     funding_series_bps_hourly: list[float] | None) -> float:
    """
    Funding accrues over the hold. Hyperliquid settles funding HOURLY, so the series is
    per-hour rates in bps and we accrue one interval per hour held. Longs pay positive
    funding, shorts receive it (and vice-versa). Returns USD cost (positive = cost to us).
    """
    if funding_series_bps_hourly:
        avg_bps_hr = sum(funding_series_bps_hourly) / len(funding_series_bps_hourly)
    else:
        avg_bps_hr = C.FUNDING_FALLBACK_BPS_PER_HOUR
    intervals = hold_hours          # hourly settlement
    s = 1.0 if side == "long" else -1.0
    return notional_usd * (avg_bps_hr / 1e4) * intervals * s


def round_trip_cost_bps(coin: str, notional_usd: float,
                        top_depth_usd: float | None = None) -> float:
    """Convenience: total friction (entry+exit spread/slippage/drift + 2x fee)."""
    c = estimate_costs(coin, notional_usd, top_depth_usd)
    return c.entry_cost_bps + c.exit_cost_bps + 2 * c.fee_bps
