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
    print("\n✓ engine ran end-to-end")


if __name__ == "__main__":
    main()
