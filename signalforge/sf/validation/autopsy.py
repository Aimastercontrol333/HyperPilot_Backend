"""
Blow-up autopsy — finds what the ban gates are MISSING.

Two consecutive walk-forwards showed the auto-bans are not separating forward
blow-ups from survivors (banned wallets blew up no more — latest window, LESS —
than the wallets we kept). The gates catch the cartoon gamblers (liquidations,
40x leverage, martingale); the wallets that actually blow up forward are passing
them. This tool answers: what did those wallets look like BEFORE they blew up?

For every non-banned, scoreable wallet it computes the production training-window
fingerprint (the same metrics the score sees), then labels the wallet by what it
ACTUALLY did in the holdout window (blowup = forward DD >= 25% or a single trade
<= -35%). It then prints/writes:

  1. group means: blown-up vs survived, per feature, with the gap — the features
     with the biggest gaps are where the next ban/penalty rule lives
  2. the per-wallet table (sorted worst forward DD first) so individual cases can
     be eyeballed against Hyperliquid's own app

This is a founder-facing research tool; it changes nothing in production.

Run:  python -m sf.validation.autopsy --db /var/data/signalforge.db --out autopsy.json
"""
from __future__ import annotations

import argparse
import json
import statistics as stats
import time

from .. import config as C
from ..ingest.hyperliquid import HyperliquidClient
from ..ingest import store
from ..features.metrics import build_round_trips, compute_metrics
from ..scoring.safety_score import score_wallet
from .walkforward import _split_trips, _forward_outcomes
from .report import _pull

DAY = 86_400_000

# training-window features to fingerprint (read with getattr so a missing field
# can never crash the autopsy)
FEATURES = [
    "n_trades", "history_days", "win_rate", "expectancy_pct", "max_single_loss_pct",
    "max_drawdown_pct", "sharpe", "sortino", "avg_leverage_proxy", "max_leverage_proxy",
    "pnl_consistency", "frequency_cv", "martingale_score", "single_trade_dominance",
    "equity_curve_quality",
]


def _fingerprint(m) -> dict:
    out = {}
    for f in FEATURES:
        v = getattr(m, f, None)
        out[f] = round(float(v), 4) if isinstance(v, (int, float)) else None
    return out


def _group_means(rows: list[dict]) -> dict:
    out = {}
    for f in FEATURES:
        vals = [r["fingerprint"][f] for r in rows if r["fingerprint"].get(f) is not None]
        out[f] = round(stats.fmean(vals), 4) if vals else None
    return out


def run_autopsy(addresses: list[str], lookback_days: int = 240, holdout_days: int = 60,
                max_wallets: int = 400) -> dict:
    client = HyperliquidClient()
    now = int(time.time() * 1000)
    boundary = now - holdout_days * DAY
    rows: list[dict] = []
    pulled = 0
    for addr in addresses[:max_wallets]:
        try:
            fills, av = _pull(client, addr, lookback_days)
        except Exception:  # noqa: BLE001
            continue
        if len(fills) < C.MIN_TRADES:
            continue
        pulled += 1
        trips = build_round_trips(fills)
        train, test = _split_trips(trips, boundary)
        if len(train) < C.MIN_TRADES or len(test) < 5:
            del fills, av
            continue
        train_fills = [f for f in fills if int(f["time"]) <= boundary]
        av_tr = [(ts, v) for ts, v in (av or []) if ts <= boundary]
        m = compute_metrics(addr, train, av_history=av_tr, fills=train_fills)
        sr = score_wallet(m)
        del fills, av
        if sr is None or sr.banned:
            continue  # autopsy targets wallets the gates KEPT
        fwd = _forward_outcomes(test)
        rows.append({
            "address": addr[:6] + "…" + addr[-4:],
            "address_full": addr,
            "score": round(sr.score, 1),
            "fingerprint": _fingerprint(m),
            "fwd_max_dd_pct": round(fwd["max_dd"], 2),
            "fwd_mean_ret_pct": round(fwd["mean_ret"], 3),
            "fwd_worst_trade_pct": round(fwd["worst"], 2),
            "blowup": fwd["blowup"],
        })

    blown = [r for r in rows if r["blowup"]]
    safe = [r for r in rows if not r["blowup"]]
    mb, ms = _group_means(blown), _group_means(safe)
    gaps = {}
    for f in FEATURES:
        if mb.get(f) is not None and ms.get(f) is not None:
            gaps[f] = round(mb[f] - ms[f], 4)
    # rank features by |relative gap| as a rough "where to look first" ordering
    ranked = sorted(
        ((f, g, abs(g) / (abs(ms[f]) + 1e-9)) for f, g in gaps.items()),
        key=lambda x: -x[2])
    leads = [{"feature": f, "blowup_minus_survivor": g, "relative_gap": round(rel, 3)}
             for f, g, rel in ranked]

    return {
        "generated_at": int(time.time()),
        "holdout_days": holdout_days,
        "wallets_pulled": pulled,
        "wallets_kept_by_gates": len(rows),
        "blowups": len(blown),
        "survivors": len(safe),
        "blowup_rate": round(len(blown) / len(rows), 3) if rows else None,
        "feature_means_blowup": mb,
        "feature_means_survivor": ms,
        "feature_gaps_ranked": leads,
        "wallets": sorted(rows, key=lambda r: -r["fwd_max_dd_pct"]),
        "how_to_read": (
            "feature_gaps_ranked lists training-window features by how differently the "
            "future blow-ups looked vs the survivors, BEFORE the holdout. Large gaps = "
            "candidate ban/penalty rules the current gates are missing. Verify the top "
            "candidates by eyeballing the worst wallets in `wallets` against Hyperliquid's "
            "own app before changing any gate."),
    }


def main():
    ap = argparse.ArgumentParser(description="Blow-up autopsy: what the ban gates are missing")
    ap.add_argument("--db", default=None, help="SQLite DB to read audited wallets from")
    ap.add_argument("--seeds", default="", help="comma-separated addresses (if no DB)")
    ap.add_argument("--lookback", type=int, default=240)
    ap.add_argument("--holdout", type=int, default=60)
    ap.add_argument("--max", type=int, default=400)
    ap.add_argument("--out", default="autopsy.json")
    args = ap.parse_args()

    addresses: list[str] = []
    if args.db:
        conn = store.connect(args.db)
        addresses = [r["address"] for r in conn.execute(
            "SELECT address FROM wallet_scores WHERE banned=0 ORDER BY score DESC").fetchall()]
    addresses += [s.strip() for s in args.seeds.split(",") if s.strip()]

    print(f"[autopsy] analyzing up to {min(len(addresses), args.max)} kept wallets "
          f"(holdout {args.holdout}d)...")
    rep = run_autopsy(addresses, args.lookback, args.holdout, args.max)
    with open(args.out, "w") as f:
        json.dump(rep, f, indent=2)
    print("\n" + "=" * 70)
    print(f"BLOW-UP AUTOPSY — {rep['blowups']} blow-ups / {rep['survivors']} survivors "
          f"among {rep['wallets_kept_by_gates']} gate-kept wallets")
    print("=" * 70)
    print("Top feature gaps (blowup mean minus survivor mean, pre-holdout):")
    for lead in rep["feature_gaps_ranked"][:8]:
        print(f"  {lead['feature']:<26} {lead['blowup_minus_survivor']:+.4f} "
              f"(rel {lead['relative_gap']:.2f})")
    print(f"\nFull detail written to {args.out}")


if __name__ == "__main__":
    main()
