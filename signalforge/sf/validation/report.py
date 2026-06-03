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
from .walkforward import analyze

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

    a = analyze(fills_by_wallet, boundary, av_by_wallet)
    n_analyzed = a["scored_wallets"] + a["banned_wallets"]

    report = {
        "generated_at": int(time.time()),
        "holdout_days": holdout_days,
        "lookback_days": lookback_days,
        "wallets_pulled": pulled,
        "wallets_analyzed": n_analyzed,
        "scored_wallets": a["scored_wallets"],
        "banned_wallets": a["banned_wallets"],
        "ban_effectiveness": a["ban_effectiveness"],
        "score_quintiles": a["score_quintiles"],
        "raw_quintiles": a["raw_quintiles"],
        "survivability": a["survivability"],
        "survivability_raw": a["survivability_raw"],
        "predictive_dimension": a["predictive_dimension"],
        "summary": {"verdict": a["verdict"], **a["survivability"]},
        "verdict": a["verdict"],
    }
    report["plain_english"] = _plain_english(report, a["_shrink_hint"], holdout_days)
    return report


def _plain_english(rep: dict, shrink_hint: str, holdout_days: int) -> str:
    n = rep["scored_wallets"]
    be = rep["ban_effectiveness"]
    sv = rep["survivability"]
    v = rep["verdict"]
    dim = rep["predictive_dimension"]
    parts: list[str] = []

    if v == "insufficient_data":
        return (f"Not enough copyable (non-banned) wallets yet to judge the score — only {n} qualify "
                f"(need ~{MIN_WALLETS_TRUST}). Keep the backend running and check back as more wallets "
                f"are audited.")

    # 1) did the bans avoid blow-ups?
    bv = be.get("verdict")
    if bv == "bans_avoid_blowups":
        parts.append(f"The auto-bans are working: over the next {holdout_days} days, banned wallets blew up "
                     f"{be.get('blowup_rate_reduction_pp', 0):+.0f}pp more often and ran "
                     f"{be.get('dd_reduction_pp', 0):+.1f}-pt deeper drawdowns than the wallets we kept.")
    elif bv == "inconclusive":
        parts.append("The auto-bans aren't yet clearly separating blow-ups from survivors on this sample "
                     "(banned and kept wallets had similar forward drawdowns) — watch as more data arrives.")

    # 2) does a higher score predict SAFER forward behavior?
    if v == "predictive":
        parts.append(f"Among the {n} copyable wallets, a higher Safety Score predicted SAFER forward behavior: "
                     f"the top fifth ran {sv.get('dd_reduction_top_vs_bottom_pp', 0):+.1f}-pt shallower drawdowns "
                     f"and blew up {sv.get('blowup_reduction_top_vs_bottom_pp', 0):+.0f}pp less than the bottom "
                     f"fifth. That matches the thesis — the score protects capital.")
        if dim == "both":
            parts.append(f"It also earned more (+{sv.get('return_spread_top_vs_bottom_pp', 0):.2f}pp/trade, "
                         f"top vs bottom).")
        else:
            parts.append("It does NOT yet predict higher per-trade returns — so treat the score as a risk filter, "
                         "not a winner-picker, and don't market it as one.")
    else:
        parts.append(f"Among the {n} copyable wallets, a higher Safety Score did NOT yet predict safer forward "
                     f"behavior (top-vs-bottom drawdown gap {sv.get('dd_reduction_top_vs_bottom_pp', 0):+.1f}pt, "
                     f"blow-up gap {sv.get('blowup_reduction_top_vs_bottom_pp', 0):+.0f}pp). The weights need "
                     f"tuning before the score can be leaned on. This is exactly the signal to catch now, on paper.")
        if dim == "return_only":
            parts.append("Interestingly the score DID separate forward returns even though it didn't separate "
                         "drawdowns — a hint the weights are picking up something, just not survivability yet.")

    if shrink_hint:
        parts.append(shrink_hint)
    if n < MIN_WALLETS_TRUST:
        parts.append(f"(Preliminary — based on {n} copyable wallets; the read firms up past ~{MIN_WALLETS_TRUST}.)")
    return " ".join(parts)


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
