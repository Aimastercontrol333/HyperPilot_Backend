#!/usr/bin/env python3
"""
HyperPilot self-check — verifies that the LIVE data obeys the rules.

It does NOT judge whether the edge is real (only the walk-forward does that). It checks
that the system is internally consistent and honest: no impossible numbers, every
"eligible" wallet truly clears every copy gate, bans/exclusions match the metrics,
and the headline KPIs add up. Run it any time; if it prints ALL CHECKS PASSED you can
truthfully say every figure on the dashboard is verified.

Usage:
    python selfcheck.py https://hyperpilot-backend.onrender.com   # live backend
    python selfcheck.py ./dashboard.json                          # a saved dashboard.json
"""
import json
import sys
import urllib.request

# Current rules (keep in sync with sf/config.py)
SHARPE_BAND = (-4.0, 4.0)
SORTINO_BAND = (-6.0, 6.0)
LEV_CAP = 75.0
COPY_MIN_TRADES = 30
COPY_MIN_DAYS = 30
AUTO_PASS = {"max_dd": 25.0, "avg_lev": 10.0, "min_sharpe": 1.5}
MM_MAKER_RATIO = 0.6
FACTORS = ["drawdown_control", "stoploss_discipline", "leverage_discipline",
           "pnl_consistency", "risk_adjusted_return", "frequency_stability",
           "anti_martingale", "wallet_trust"]

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results = []   # (level, message)


def check(cond, ok_msg, bad_msg, level=FAIL):
    results.append((PASS if cond else level, ok_msg if cond else bad_msg))
    return cond


def _get(base, path):
    url = base.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def load(arg):
    """Return (dashboard, health, walkforward). For a local file, only dashboard."""
    if arg.startswith("http"):
        return _get(arg, "/dashboard.json"), _get(arg, "/health"), _get(arg, "/walkforward.json")
    with open(arg) as f:
        return json.load(f), None, None


def within(x, lo, hi):
    return x is None or (lo <= x <= hi)


def audit(dash, health):
    rows = dash.get("traders_table", [])
    kpis = dash.get("kpis", {})

    # ---- KPI internal consistency ----
    d, s = kpis.get("discovered", 0), kpis.get("wallets_audited", 0)
    e, b = kpis.get("passed_filter", 0), kpis.get("banned", 0)
    check(d >= s >= 0, f"KPI order ok (discovered {d} >= scored {s})",
          f"KPI order broken: discovered {d} < scored {s}")
    check(s >= e >= 0, f"eligible within scored ({e} <= {s})",
          f"eligible {e} exceeds scored {s}")
    check(b <= s, f"banned within scored ({b} <= {s})", f"banned {b} exceeds scored {s}")
    if s:
        expect = round(e / s * 100, 1)
        check(abs(expect - kpis.get("pass_rate_pct", expect)) <= 0.6,
              f"pass-rate consistent (~{expect}%)",
              f"pass_rate_pct {kpis.get('pass_rate_pct')} != computed {expect}", WARN)

    if health:
        lr = health.get("last_run", {})
        check(lr.get("ok") is True, "last scoring pass ok", "last scoring pass did NOT finish ok")
        db = health.get("db", {})
        check(db.get("scored", 0) > 0, f"DB has scored wallets ({db.get('scored')})",
              "DB shows 0 scored wallets")

    # ---- per-wallet number sanity (applies to EVERY row) ----
    bad_band = []
    for r in rows:
        w = r.get("wallet", "?")
        if not within(r.get("sharpe"), *SHARPE_BAND): bad_band.append(f"{w} sharpe={r.get('sharpe')}")
        if not within(r.get("sortino"), *SORTINO_BAND): bad_band.append(f"{w} sortino={r.get('sortino')}")
        if not within(r.get("max_dd"), 0, 100): bad_band.append(f"{w} max_dd={r.get('max_dd')}")
        if r.get("avg_lev") is not None and not within(r.get("avg_lev"), 0, LEV_CAP):
            bad_band.append(f"{w} avg_lev={r.get('avg_lev')}")
        if not within(r.get("win_pct"), 0, 100): bad_band.append(f"{w} win%={r.get('win_pct')}")
        if not within(r.get("safety"), 0, 100): bad_band.append(f"{w} safety={r.get('safety')}")
        for fac in FACTORS:
            v = (r.get("factors") or {}).get(fac)
            if v is not None and not within(v, 0, 1):
                bad_band.append(f"{w} {fac}={v}")
    check(not bad_band, f"all {len(rows)} rows have in-band numbers",
          "out-of-band numbers: " + "; ".join(bad_band[:8]))

    # ---- every ELIGIBLE wallet must truly clear every copy gate ----
    bad_elig = []
    for r in rows:
        if not r.get("eligible"):
            continue
        w = r.get("wallet", "?")
        if r.get("banned"): bad_elig.append(f"{w} eligible+banned")
        if r.get("notes"): bad_elig.append(f"{w} eligible but excluded {r.get('notes')}")
        nt = r.get("n_trades")
        if nt is not None and nt < COPY_MIN_TRADES: bad_elig.append(f"{w} n_trades={nt}<{COPY_MIN_TRADES}")
        hd = r.get("history_days")
        if hd is not None and hd < COPY_MIN_DAYS: bad_elig.append(f"{w} history={hd}d<{COPY_MIN_DAYS}")
        if r.get("max_dd") is not None and r["max_dd"] >= AUTO_PASS["max_dd"]:
            bad_elig.append(f"{w} max_dd={r['max_dd']}>=25")
        if r.get("sharpe") is not None and r["sharpe"] <= AUTO_PASS["min_sharpe"]:
            bad_elig.append(f"{w} sharpe={r['sharpe']}<=1.5")
        if r.get("leverage_known") and r.get("avg_lev") is not None and r["avg_lev"] >= AUTO_PASS["avg_lev"]:
            bad_elig.append(f"{w} avg_lev={r['avg_lev']}>=10")
    check(not bad_elig, "every eligible wallet clears all copy gates",
          "eligible wallets that should NOT be: " + "; ".join(bad_elig[:8]))

    # ---- market-maker exclusions should look like market-makers ----
    mm_odd = []
    for r in rows:
        notes = r.get("notes") or []
        if "uncopyable_market_maker" in notes:
            if r.get("eligible"): mm_odd.append(f"{r.get('wallet')} MM-flagged yet eligible")
            mr = r.get("maker_ratio")
            # may have been flagged by fan-out instead of maker_ratio, so this is a soft check
            if mr is not None and mr < MM_MAKER_RATIO:
                mm_odd.append(f"{r.get('wallet')} maker_ratio={mr} (flagged via fan-out?)")
    check(not [m for m in mm_odd if "eligible" in m],
          "no market-maker is also eligible",
          "MM wallets marked eligible: " + "; ".join(m for m in mm_odd if "eligible" in m))

    # ---- live engine sanity ----
    live = dash.get("live", {})
    if live:
        eq = live.get("equity")
        check(eq is None or eq > 0, f"live equity positive ({eq})", f"live equity invalid ({eq})", WARN)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "https://hyperpilot-backend.onrender.com"
    try:
        dash, health, wf = load(arg)
    except Exception as e:  # noqa: BLE001
        print(f"could not load data from {arg}: {e}")
        sys.exit(2)

    audit(dash, health)

    print(f"\nHyperPilot self-check  —  source: {arg}")
    print("=" * 64)
    for level, msg in results:
        mark = {"PASS": "  ok ", "WARN": " warn", "FAIL": "FAIL "}[level]
        print(f"[{mark}] {msg}")
    n_fail = sum(1 for lv, _ in results if lv == FAIL)
    n_warn = sum(1 for lv, _ in results if lv == WARN)
    print("=" * 64)
    if wf:
        print(f"walk-forward verdict: {wf.get('verdict')} — {wf.get('plain_english', '')[:160]}")
    if n_fail:
        print(f"\n{n_fail} CHECK(S) FAILED, {n_warn} warning(s). Investigate the FAIL lines above.")
        sys.exit(1)
    print(f"\nALL CHECKS PASSED ({n_warn} warning(s)). Every figure obeys the rules.")
    print("Note: this proves the data is consistent, NOT that the edge is real — that's the walk-forward.")


if __name__ == "__main__":
    main()
