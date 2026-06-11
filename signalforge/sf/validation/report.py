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
from .walkforward import row_for_wallet, _assemble

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
    rows = []          # only tiny per-wallet result rows are kept in memory
    pulled = 0
    for addr in addresses[:max_wallets]:
        try:
            fills, av = _pull(client, addr, lookback_days)
        except Exception:  # noqa: BLE001
            continue
        if len(fills) < C.MIN_TRADES:
            continue
        pulled += 1
        r = row_for_wallet(fills, av, boundary)
        if r is not None:
            rows.append(r)
        del fills, av           # let each wallet's history be freed before the next
    a = _assemble(rows)
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


def build_report_multi(addresses: list[str], lookback_days: int = 240,
                       holdout_days: int = 60, n_windows: int = 3,
                       max_wallets: int = 300) -> dict:
    """Multi-window walk-forward: the same test run over n_windows STAGGERED 60-day
    holdouts (window 1 = the most recent 60d, window 2 = the 60d before that, ...).
    One window can be flattered or damned by a single market regime; agreement
    across independent windows is what makes the verdict publishable.

    Each wallet's fills are pulled ONCE and every window is computed from the same
    data, so this costs the same API budget as a single-window run. Top-level fields
    mirror the single-window report (taken from the most recent window) so the
    existing /walkforward.json consumers keep working; the per-window detail and the
    cross-window consensus are added alongside.
    """
    client = HyperliquidClient()
    now = int(time.time() * 1000)
    # window i: train on everything before (now - (i+1)*h), test on the h-day slice after it
    bounds = [(now - (i + 1) * holdout_days * DAY, now - i * holdout_days * DAY)
              for i in range(n_windows)]
    rows_by_win: list[list] = [[] for _ in range(n_windows)]
    pulled = 0
    for addr in addresses[:max_wallets]:
        try:
            fills, av = _pull(client, addr, lookback_days)
        except Exception:  # noqa: BLE001
            continue
        if len(fills) < C.MIN_TRADES:
            continue
        pulled += 1
        for i, (b, e) in enumerate(bounds):
            r = row_for_wallet(fills, av, b, test_end_ms=e)
            if r is not None:
                rows_by_win[i].append(r)
        del fills, av
    windows = []
    for i, rows in enumerate(rows_by_win):
        a = _assemble(rows)
        windows.append({
            "window": i + 1,
            "holdout_start_days_ago": (i + 1) * holdout_days,
            "holdout_end_days_ago": i * holdout_days,
            "wallets_analyzed": a["scored_wallets"] + a["banned_wallets"],
            "scored_wallets": a["scored_wallets"],
            "banned_wallets": a["banned_wallets"],
            "ban_effectiveness": a["ban_effectiveness"],
            "score_quintiles": a["score_quintiles"],
            "survivability": a["survivability"],
            "survivability_raw": a["survivability_raw"],
            "predictive_dimension": a["predictive_dimension"],
            "verdict": a["verdict"],
            "_shrink_hint": a["_shrink_hint"],
        })
    # consensus across windows
    verdicts = [w["verdict"] for w in windows]
    usable = [w for w in windows if w["verdict"] != "insufficient_data"]
    n_pred = sum(1 for w in usable if w["verdict"] == "predictive")
    if not usable:
        consensus = "insufficient_data"
    elif n_pred == len(usable):
        consensus = "predictive"
    elif n_pred >= (len(usable) + 1) // 2:
        consensus = "predictive_majority"
    else:
        consensus = "weak_or_none"
    dims = [w["predictive_dimension"] for w in usable]
    earns_every_window = bool(usable) and all(d == "both" for d in dims)

    latest = windows[0] if windows else {}
    report = {
        "generated_at": int(time.time()),
        "mode": "multi_window",
        "n_windows": n_windows,
        "holdout_days": holdout_days,
        "lookback_days": lookback_days,
        "wallets_pulled": pulled,
        # top-level mirrors the MOST RECENT window for backwards compatibility
        "wallets_analyzed": latest.get("wallets_analyzed", 0),
        "scored_wallets": latest.get("scored_wallets", 0),
        "banned_wallets": latest.get("banned_wallets", 0),
        "ban_effectiveness": latest.get("ban_effectiveness", {}),
        "score_quintiles": latest.get("score_quintiles", []),
        "survivability": latest.get("survivability", {}),
        "survivability_raw": latest.get("survivability_raw", {}),
        "predictive_dimension": latest.get("predictive_dimension", "none"),
        "windows": windows,
        "consensus": {"verdict": consensus, "window_verdicts": verdicts,
                      "predictive_windows": n_pred, "usable_windows": len(usable),
                      "earns_in_every_window": earns_every_window},
        "summary": {"verdict": consensus, **latest.get("survivability", {})},
        "verdict": consensus,
    }
    report["plain_english"] = _plain_english_multi(report, holdout_days)
    return report


def _plain_english_multi(rep: dict, holdout_days: int) -> str:
    cons = rep["consensus"]
    wins = rep["windows"]
    usable = [w for w in wins if w["verdict"] != "insufficient_data"]
    parts: list[str] = []
    if cons["verdict"] == "insufficient_data":
        return ("Not enough copyable wallets in any holdout window yet to judge the score. "
                "Keep the backend running and check back as more wallets are audited.")
    parts.append(f"Tested across {len(usable)} independent {holdout_days}-day holdout windows: "
                 f"{cons['predictive_windows']} of {len(usable)} came back PREDICTIVE on survivability "
                 f"(higher score -> shallower forward drawdowns, fewer blow-ups).")
    if cons["verdict"] == "predictive":
        dd = [w["survivability"].get("dd_reduction_top_vs_bottom_pp", 0) for w in usable]
        bl = [w["survivability"].get("blowup_reduction_top_vs_bottom_pp", 0) for w in usable]
        parts.append(f"Top-vs-bottom drawdown reduction ranged {min(dd):+.1f} to {max(dd):+.1f}pt and "
                     f"blow-up reduction {min(bl):+.0f} to {max(bl):+.0f}pp across windows — a consistent, "
                     f"regime-independent risk signal. This is the publishable claim.")
    elif cons["verdict"] == "predictive_majority":
        parts.append("The signal held in most but not all windows — treat it as real but "
                     "regime-sensitive; keep validating before leaning on it publicly.")
    else:
        parts.append("The signal did not hold across windows — what looked predictive in one period "
                     "may have been that period's regime. Tune the weights before relying on it.")
    if cons.get("earns_in_every_window"):
        parts.append("The top tier ALSO out-earned the bottom tier in every window.")
    else:
        parts.append("Forward RETURNS were not consistently positive or consistently separated — "
                     "treat the score as a risk filter, not a winner-picker, and don't market it as one.")
    hints = {w.get("_shrink_hint", "") for w in wins if w.get("_shrink_hint")}
    if hints:
        parts.append(next(iter(hints)))
    return " ".join(parts)


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
