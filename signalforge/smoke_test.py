"""
Offline smoke test — proves the engine end-to-end without the live API.

Generates synthetic fills in Hyperliquid's exact shape for three archetypes:
  - 'disciplined'  : small bounded losses, lets winners run, low leverage
  - 'gambler'      : huge swings, a liquidation, high leverage
  - 'martingale'   : doubles size after losses
Then runs the real metrics -> score -> basket -> paper sim -> walk-forward path.
"""
from __future__ import annotations

import random
import time

from sf.features.metrics import build_round_trips, compute_metrics
from sf.scoring.safety_score import score_wallet
from sf.sim.paper_trader import run_paper_sim, capacity_curve
from sf.validation.walkforward import walk_forward, summarize_walk_forward

random.seed(7)
NOW = int(time.time() * 1000)
DAY = 86_400_000


def mk_fills(profile: str, n_trades: int, start_days_ago: int = 110) -> list[dict]:
    fills = []
    coins = ["BTC", "ETH", "SOL"]
    base = {"BTC": 60000.0, "ETH": 3400.0, "SOL": 150.0}
    t = NOW - start_days_ago * DAY
    size_mult = 1.0
    for i in range(n_trades):
        coin = random.choice(coins)
        px = base[coin] * (1 + random.uniform(-0.02, 0.02))
        side = random.choice(["Long", "Short"])
        qty = round(2.0 * size_mult / base[coin] * base["BTC"], 4)

        if profile == "disciplined":
            r = random.choices([random.uniform(1, 4), random.uniform(-0.8, -0.2)],
                               weights=[0.5, 0.5])[0]
            size_mult = 1.0
        elif profile == "gambler":
            r = random.choices([random.uniform(2, 8), random.uniform(-20, -6)],
                               weights=[0.55, 0.45])[0]
            size_mult = random.uniform(1, 3)
        else:  # martingale
            prev = fills[-1] if fills else None
            r = random.choices([random.uniform(0.5, 2), random.uniform(-3, -1)],
                               weights=[0.5, 0.5])[0]
            size_mult = size_mult * 2 if (prev and "loss_flag" in str(prev)) else 1.0

        notional = px * qty
        pnl = notional * r / 100.0
        fee = notional * 0.00035
        open_t = t
        close_t = t + random.randint(1, 20) * 3600_000
        fills.append({"coin": coin, "px": str(px), "sz": str(qty), "side": "B",
                      "time": open_t, "dir": f"Open {side}", "closedPnl": "0",
                      "fee": str(round(fee, 2)), "tid": i * 2})
        fills.append({"coin": coin, "px": str(px * (1 + (r/100 if side == 'Long' else -r/100))),
                      "sz": str(qty), "side": "A", "time": close_t,
                      "dir": f"Close {side}", "closedPnl": str(round(pnl, 2)),
                      "fee": str(round(fee, 2)), "tid": i * 2 + 1})
        # a liquidation for the gambler
        if profile == "gambler" and i == n_trades // 2:
            fills[-1]["dir"] = f"Close {side}"  # keep shape; liq counted via dir string
            fills.append({"coin": coin, "px": str(px), "sz": "0", "side": "A",
                          "time": close_t + 1, "dir": "Liquidated Long",
                          "closedPnl": "-9999", "fee": "0", "tid": i * 2 + 99999})
        t = close_t + random.randint(2, 30) * 3600_000
    return fills


def main():
    profiles = {
        "0x" + "a" * 40: "disciplined",
        "0x" + "b" * 40: "disciplined",
        "0x" + "c" * 40: "gambler",
        "0x" + "d" * 40: "martingale",
        "0x" + "e" * 40: "disciplined",
    }
    fills_by_wallet, scores = {}, []
    for addr, prof in profiles.items():
        fills = mk_fills(prof, n_trades=120)
        fills_by_wallet[addr] = fills
        trips = build_round_trips(fills)
        liqs = sum(1 for f in fills if "Liquidat" in f["dir"])
        m = compute_metrics(addr, trips, account_value=200_000, liquidations=liqs)
        sr = score_wallet(m)
        if sr:
            sr.metrics.extra["_trips"] = trips
            scores.append(sr)
            print(f"{prof:12s} {addr[:8]}.. score={sr.score:5.1f} "
                  f"eligible={sr.eligible} banned={sr.banned} {sr.ban_reasons} "
                  f"| dd={m.max_drawdown_pct:.1f}% maxLoss={m.max_single_loss_pct:.1f}% "
                  f"sharpe={m.sharpe:.2f} arch={sr.archetype}")

    # basket = eligible, score-weighted
    elig = [s for s in scores if s.eligible]
    tot = sum(s.score for s in elig) or 1
    basket = [(s.address, s.score / tot) for s in elig]
    trips_by_wallet = {s.address: s.metrics.extra["_trips"] for s in scores}

    print("\nBasket:", [(a[:8] + '..', round(w, 3)) for a, w in basket])
    sim = run_paper_sim(basket, trips_by_wallet)
    print(f"Paper sim: return={sim.total_return_pct}% maxDD={sim.max_drawdown_pct:.1f}% "
          f"sharpe={sim.sharpe} win={sim.win_rate} trades={sim.n_trades} "
          f"avgCost={sim.avg_cost_bps}bps")
    print("Capacity:", capacity_curve(basket, trips_by_wallet))

    boundary = NOW - 30 * DAY
    deciles = walk_forward(fills_by_wallet, boundary)
    print("\nWalk-forward summary:", summarize_walk_forward(deciles))

    live_engine_checks()
    print("\n✓ engine ran end-to-end")


def live_engine_checks():
    """Unit checks for the live-copy integrity layer (no network):
    staleness guard, unpriced-open block, vol-aware stop, suspension review,
    de-listed-close mirroring, restart persistence of open positions."""
    from sf.sim.live_copy import PaperPortfolio
    import sf.config as C

    print("\n--- live engine integrity checks ---")
    T1, T2 = "0x" + "1" * 40, "0x" + "2" * 40
    now = NOW

    pf = PaperPortfolio(start_equity=100_000, weights={T1: 0.10, T2: 0.10})

    # 1) unpriced open is BLOCKED (no fresh mark for the coin)
    pf.on_fill(T1, "xyz:GOLD", "Open Long", 1.0, 4447.0, now)
    assert not pf.open_positions, "unpriced coin must not be copied"
    print("✓ unpriced open blocked (no fresh mark)")

    # 2) priced open works; vol-aware stop computed
    pf.daily_vol["DOGE"] = 5.0                      # 5%/day mover
    pf.mark_ms["BTC"] = now; pf.mark_ms["DOGE"] = now
    pf.on_fill(T1, "BTC", "Open Long", 0.1, 60_000.0, now)
    pf.on_fill(T2, "DOGE", "Open Long", 1000.0, 0.2, now)
    assert (T1, "BTC") in pf.open_positions and (T2, "DOGE") in pf.open_positions
    doge_stop = pf.open_positions[(T2, "DOGE")].stop_pct
    exp = min(max(C.PORTFOLIO["own_stop_loss_pct"], C.PORTFOLIO["stop_vol_mult"] * 5.0),
              C.PORTFOLIO["stop_cap_pct"])
    assert abs(doge_stop - exp) < 0.01, f"vol stop {doge_stop} != {exp}"
    print(f"✓ vol-aware stop: DOGE stop={doge_stop}% (BTC stop={pf.open_positions[(T1,'BTC')].stop_pct}%)")

    # 3) staleness guard: stale mark -> no PnL, no stop fire, flagged in snapshot
    stale_ms = {"BTC": now, "DOGE": now - (C.STALE_MARK_MAX_S + 60) * 1000}
    crash = {"BTC": 60_000.0, "DOGE": 0.05}        # DOGE 'crashed' but mark is stale
    pf.mark_to_market(crash, now + 1000, stale_ms)
    assert (T2, "DOGE") in pf.open_positions, "stale price must not fire the stop"
    snap = pf.snapshot(crash, stale_ms)
    assert snap["data_quality"] == "degraded" and snap["unpriced_count"] == 1
    drow = [r for r in snap["open_positions"] if r["coin"] == "DOGE"][0]
    assert drow["stale"] and drow["unreal_usd"] is None, "stale position must show no fake PnL"
    print(f"✓ staleness guard: degraded quality, unpriced ${snap['unpriced_notional_usd']:.0f} excluded, stop frozen")

    # 4) de-listed trader's close still mirrors
    pf.weights = {T2: 0.10}                         # T1 de-listed
    px = pf.open_positions[(T1, "BTC")].entry_px
    pf.on_fill(T1, "BTC", "Close Long", 0.1, px * 1.01, now + 2000)
    assert (T1, "BTC") not in pf.open_positions, "de-listed trader close must mirror"
    assert pf.closed[-1]["reason"] == "trader_closed"
    print("✓ de-listed trader's close mirrored")

    # 5) suspension auto-review reinstates after SUSPENSION_REVIEW_DAYS
    pf.suspended.add(T1); pf.suspended_at[T1] = now
    later = now + int((C.SUSPENSION_REVIEW_DAYS * 86_400_000) + 1000)
    pf.mark_to_market({"BTC": 60_000.0}, later, {"BTC": later})
    assert T1 not in pf.suspended, "suspension must auto-review"
    print(f"✓ suspension auto-review after {C.SUSPENSION_REVIEW_DAYS:.0f}d")

    # 6) restart persistence: open positions + trader_net survive to_state/from_state
    state = pf.to_state()
    pf2 = PaperPortfolio.from_state(state, pf.weights, C.PORTFOLIO["own_stop_loss_pct"])
    assert set(pf2.open_positions) == set(pf.open_positions), "open positions must survive restart"
    assert pf2.trader_net == pf.trader_net, "trader nets must survive restart"
    print(f"✓ restart persistence: {len(pf2.open_positions)} open position(s) restored")

    # 7) phantom-close guard: a Close fill while OUR book is flat must NOT open
    #    an opposite-side mirror (post-deploy state-loss failure mode)
    pf3 = PaperPortfolio(start_equity=100_000, weights={T1: 0.10})
    pf3.mark_ms["ETH"] = now
    pf3.on_fill(T1, "ETH", "Close Long", 1.0, 1600.0, now)
    assert not pf3.open_positions, "phantom close must not open a short"
    assert abs(pf3.trader_net.get((T1, "ETH"), 0.0)) < 1e-12
    pf3.on_fill(T1, "ETH", "Open Long", 1.0, 1600.0, now + 1000)
    assert (T1, "ETH") in pf3.open_positions, "real open must still work after ignored close"
    print("✓ phantom-close guard: untracked Close ignored, real Open still mirrors")

    # 8) on-demand builder-coin pricing: a fill in an unpriced xyz: coin (no prior
    #    poll, so mark_ms has no entry) must still open by fetching the price now.
    #    The fill carries the trader's px as the mark (83.468); the fetcher supplies
    #    the freshness stamp that the staleness guard requires.
    pf4 = PaperPortfolio(start_equity=100_000, weights={T1: 0.10})
    pf4._price_fetcher = lambda coin: (83.468, now) if coin == "xyz:CL" else None
    assert "xyz:CL" not in pf4.mark_ms, "precondition: coin not yet priced by any poll"
    pf4.on_fill(T1, "xyz:CL", "Open Long", 10.0, 83.468, now)
    assert (T1, "xyz:CL") in pf4.open_positions, "builder coin must open via on-demand price"
    assert pf4.open_positions[(T1, "xyz:CL")].entry_px > 0
    print("✓ on-demand builder-coin pricing: xyz:CL opened (deadlock fixed)")

    # 9) still blocked if the fetcher has no price (honest, no fake)
    pf5 = PaperPortfolio(start_equity=100_000, weights={T1: 0.10})
    pf5._price_fetcher = lambda coin: None
    pf5.on_fill(T1, "xyz:NOPE", "Open Long", 10.0, 1.23, now)
    assert not pf5.open_positions, "unfetchable builder coin must still be skipped"
    print("✓ unfetchable builder coin still skipped (no fake price)")


if __name__ == "__main__":
    main()
