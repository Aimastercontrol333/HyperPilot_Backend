"""
Phase-1 pipeline orchestrator.

  discover universe -> ingest fills/positions/funding -> compute metrics ->
  Safety Score -> select basket -> paper-sim (with capacity) -> dashboard.json

Run:  python -m sf.pipeline --seeds 0xabc...,0xdef... --out dashboard.json
The emitted JSON matches what the existing site renders (KPIs, traders table,
equity curve, paper trades), so the front-end just fetches it instead of using
the simulated placeholders.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict

from . import config as C
from .ingest.hyperliquid import HyperliquidClient
from .features.metrics import build_round_trips, compute_metrics
from .scoring.safety_score import score_wallet, ScoreResult
from .sim.paper_trader import run_paper_sim, capacity_curve


def _liquidation_count(fills: list[dict]) -> int:
    return sum(1 for f in fills if "Liquidat" in str(f.get("dir", "")))


def audit_wallet(client: HyperliquidClient, addr: str, lookback_days: int) -> ScoreResult | None:
    start_ms = int((time.time() - lookback_days * 86400) * 1000)
    fills = client.user_fills_by_time(addr, start_ms)
    if len(fills) < C.MIN_TRADES:
        return None
    trips = build_round_trips(fills)
    state = {}
    try:
        state = client.clearinghouse_state(addr)
    except Exception:  # noqa: BLE001
        pass
    acct_val = None
    try:
        acct_val = float(state.get("marginSummary", {}).get("accountValue")) or None
    except Exception:  # noqa: BLE001
        acct_val = None
    m = compute_metrics(addr, trips, acct_val, liquidations=_liquidation_count(fills))
    sr = score_wallet(m)
    if sr is not None:
        sr.metrics.extra["_fills"] = len(fills)
        sr.metrics.extra["_trips"] = trips
    return sr


def build_basket(scores: list[ScoreResult]) -> list[tuple[str, float]]:
    eligible = sorted([s for s in scores if s.eligible], key=lambda s: s.score, reverse=True)
    chosen = eligible[: C.PORTFOLIO["basket_size"]]
    if not chosen:
        return []
    mode = C.PORTFOLIO["weighting"]
    if mode == "equal":
        raw = {s.address: 1.0 for s in chosen}
    elif mode == "inverse_vol":
        raw = {s.address: 1.0 / (s.metrics.max_drawdown_pct + 1.0) for s in chosen}
    else:  # safety_score
        raw = {s.address: s.score for s in chosen}
    tot = sum(raw.values())
    cap = C.PORTFOLIO["max_weight_per_trader"]
    return [(a, min(w / tot, cap)) for a, w in raw.items()]


def run(seeds: list[str], lookback_days: int = C.LOOKBACK_DAYS,
        max_wallets: int | None = None, db_path: str | None = None) -> dict:
    client = HyperliquidClient()

    # Discovery: union of (a) harvested addresses from the DB [most active first],
    # (b) the public leaderboard, (c) manual seeds.
    conn = None
    universe: list[str] = []
    if db_path:
        from .ingest import store
        conn = store.connect(db_path)
        universe.extend(store.top_addresses(conn, limit=max_wallets or 500, min_hits=2))
    lb = client.discover_universe(extra_seeds=seeds)
    universe.extend(a for a in lb if a not in set(universe))
    if max_wallets:
        universe = universe[:max_wallets]
    print(f"[pipeline] auditing {len(universe)} wallets "
          f"({'DB+' if db_path else ''}leaderboard+seeds)...")

    scores: list[ScoreResult] = []
    for i, addr in enumerate(universe, 1):
        try:
            sr = audit_wallet(client, addr, lookback_days)
            if sr:
                scores.append(sr)
                if conn is not None:
                    from .ingest import store
                    store.upsert_score(conn, sr)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(universe)}] {addr[:10]}.. failed: {e}")
        if i % 25 == 0:
            print(f"  audited {i}/{len(universe)}")

    basket = build_basket(scores)
    trips_by_wallet = {}
    for s in scores:
        if s.metrics:
            trips_by_wallet[s.address] = s.metrics.extra.get("_trips", [])

    return assemble_dashboard(scores, basket, trips_by_wallet)


def assemble_dashboard(scores, basket, trips_by_wallet) -> dict:
    sim = run_paper_sim(basket, trips_by_wallet)
    cap = capacity_curve(basket, trips_by_wallet)
    eligible = [s for s in scores if s.eligible]
    table = []
    for s in sorted(scores, key=lambda x: x.score, reverse=True)[:50]:
        m = s.metrics
        table.append({
            "wallet": s.address[:6] + "…" + s.address[-4:],
            "venue": "Hyperliquid", "archetype": s.archetype,
            "safety": s.score, "eligible": s.eligible, "banned": s.banned,
            "ban_reasons": s.ban_reasons,
            "avg_lev": round(m.avg_leverage_proxy, 1),
            "max_dd": round(m.max_drawdown_pct, 1),
            "win_pct": round(m.win_rate * 100, 1),
            "expectancy_pct": round(m.expectancy_pct, 2),
            "n_trades": m.n_trades, "history_days": round(m.history_days, 0),
        })
    return {
        "generated_at": int(time.time()),
        "disclaimer": "Research preview · simulated · not financial advice.",
        "kpis": {
            "wallets_audited": len(scores),
            "passed_filter": len(eligible),
            "pass_rate_pct": round(len(eligible) / max(len(scores), 1) * 100, 1),
            "basket_size": len(basket),
            "basket_return_pct": sim.total_return_pct,
            "basket_max_dd_pct": round(sim.max_drawdown_pct, 1),
            "basket_sharpe": sim.sharpe,
            "basket_win_rate": sim.win_rate,
            "avg_cost_bps": sim.avg_cost_bps,
        },
        "capacity": cap,
        "equity_curve": sim.equity_curve,
        "recent_trades": [asdict(t) for t in sim.trades[-30:]],
        "traders_table": table,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="", help="comma-separated 0x addresses to seed/watch")
    ap.add_argument("--lookback", type=int, default=C.LOOKBACK_DAYS)
    ap.add_argument("--max", type=int, default=None, help="cap wallets audited (testing)")
    ap.add_argument("--db", default=None, help="SQLite path to read harvested wallets / persist scores")
    ap.add_argument("--out", default="dashboard.json")
    args = ap.parse_args()
    seeds = [s.strip() for s in args.seeds.split(",") if s.strip()]
    data = run(seeds, args.lookback, args.max, args.db)
    with open(args.out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[pipeline] wrote {args.out}: {data['kpis']}")


if __name__ == "__main__":
    main()
