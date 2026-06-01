"""
Walk-forward report — the founder's "is the edge real?" check.

Answers one question in plain English: does a HIGH Safety Score actually predict
BETTER forward performance, or is the score just guessing?

How: for each wallet, score it on its history UP TO a holdout date (using the same
signals production uses), then measure what it ACTUALLY did over the held-out
window after that date. Bucket wallets by score decile and compare the top group's
forward return against the bottom group's. A real edge shows up as the top decile
beating the bottom, monotonically.

This needs a few hundred wallets with enough history to be trustworthy, so it
reports "warming up" until enough have been audited.

Run manually:   python -m sf.validation.report --db /var/data/signalforge.db --out walkforward.json
Or let the worker run it daily and serve /walkforward.json.
"""
from __future__ import annotations

import argparse
import json
import time

from .. import config as C
from ..ingest.hyperliquid import HyperliquidClient
from ..ingest import store
from .walkforward import walk_forward, summarize_walk_forward

DAY = 86_400_000
MIN_WALLETS_PRELIM = 40      # below this: not enough to say anything
MIN_WALLETS_TRUST = 120      # at/above this: a trustworthy read


def _pull(client: HyperliquidClient, addr: str, lookback_days: int):
    start = int((time.time() - lookback_days * 86400) * 1000)
    fills = client.user_fills_by_time(addr, start)
    av = None
    try:
        for window, blk in client.portfolio(addr):
            if window == "allTime":
                av = sorted((int(ts), float(v)) for ts, v in blk.get("accountValueHistory", []))
                break
    except Exception:  # noqa: BLE001
        av = None
    return fills, av


def build_report(addresses: list[str], lookback_days: int = 180,
                 holdout_days: int = 60, max_wallets: int = 300) -> dict:
    client = HyperliquidClient()
    boundary = int(time.time() * 1000) - holdout_days * DAY
    fills_by_wallet: dict[str, list] = {}
    av_by_wallet: dict[str, list] = {}
    pulled = 0
    for addr in addresses[:max_wallets]:
        try:
            fills, av = _pull(client, addr, lookback_days)
        except Exception:  # noqa: BLE001
            continue
        if len(fills) >= C.MIN_TRADES:
            fills_by_wallet[addr] = fills
            if av:
                av_by_wallet[addr] = av
            pulled += 1

    deciles = walk_forward(fills_by_wallet, boundary, av_by_wallet)
    n_analyzed = sum(d.n_wallets for d in deciles)
    summary = summarize_walk_forward(deciles)

    report = {
        "generated_at": int(time.time()),
        "holdout_days": holdout_days,
        "lookback_days": lookback_days,
        "wallets_pulled": pulled,
        "wallets_analyzed": n_analyzed,
        "deciles": [d.__dict__ for d in deciles],
        "summary": summary,
    }
    report["verdict"], report["plain_english"] = _verdict(n_analyzed, summary, holdout_days)
    return report


def _verdict(n: int, summary: dict, holdout_days: int) -> tuple[str, str]:
    if n < MIN_WALLETS_PRELIM:
        return ("insufficient_data",
                f"Not enough audited wallets yet to judge the score — only {n} qualify "
                f"(need ~{MIN_WALLETS_TRUST}, with at least {MIN_WALLETS_PRELIM} for a first read). "
                f"Keep the backend running and check back in a few days as the leaderboard "
                f"and harvester feed more wallets in.")
    top = summary.get("top_decile_fwd_ret", 0.0)
    bot = summary.get("bottom_decile_fwd_ret", 0.0)
    spread = summary.get("spread_pct", 0.0)
    trust = "" if n >= MIN_WALLETS_TRUST else (
        f" (Preliminary — based on {n} wallets; the read firms up past ~{MIN_WALLETS_TRUST}.)")
    if summary.get("verdict") == "predictive":
        return ("predictive",
                f"The Safety Score works.{trust} Over the {holdout_days} days AFTER scoring, the "
                f"highest-rated wallets averaged {top:+.2f}% per trade while the lowest-rated "
                f"averaged {bot:+.2f}% — a {spread:+.2f}-point edge in the right direction. "
                f"High scores predicted better real performance.")
    return ("weak_or_none",
            f"The Safety Score is not yet predictive.{trust} The highest-rated wallets averaged "
            f"{top:+.2f}% per trade over the next {holdout_days} days versus {bot:+.2f}% for the "
            f"lowest-rated — not a reliable gap. The scoring weights likely need tuning before "
            f"leaning on the score. This is exactly the signal to catch now, on paper.")


def main():
    ap = argparse.ArgumentParser(description="Walk-forward predictive-power report")
    ap.add_argument("--db", default=None, help="SQLite DB to read audited wallets from")
    ap.add_argument("--seeds", default="", help="comma-separated addresses (if no DB)")
    ap.add_argument("--lookback", type=int, default=180)
    ap.add_argument("--holdout", type=int, default=60)
    ap.add_argument("--max", type=int, default=300)
    ap.add_argument("--out", default="walkforward.json")
    args = ap.parse_args()

    addresses: list[str] = []
    if args.db:
        conn = store.connect(args.db)
        # prefer wallets we've already scored; fall back to most-active discovered
        addresses = [r["address"] for r in
                     conn.execute("SELECT address FROM wallet_scores ORDER BY score DESC").fetchall()]
        if len(addresses) < args.max:
            for a in store.top_addresses(conn, limit=args.max, min_hits=2):
                if a not in addresses:
                    addresses.append(a)
    addresses += [s.strip() for s in args.seeds.split(",") if s.strip()]

    print(f"[walkforward] analyzing up to {min(len(addresses), args.max)} wallets "
          f"(holdout {args.holdout}d)...")
    report = build_report(addresses, args.lookback, args.holdout, args.max)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    print("\n" + "=" * 70)
    print("WALK-FORWARD REPORT")
    print("=" * 70)
    print(f"Wallets analyzed: {report['wallets_analyzed']}")
    print(f"Verdict: {report['verdict'].upper()}")
    print("\n" + report["plain_english"])
    print("=" * 70)
    print(f"\nFull detail written to {args.out}")


if __name__ == "__main__":
    main()
