"""
Always-on worker — the deployable unit once discovery is live.

Runs three things in one process:
  1. the WebSocket address harvester (background thread, self-healing)
  2. a periodic scoring pass (every SCORE_EVERY_MIN) that reads harvested wallets
     from the shared SQLite DB, audits them, and writes dashboard.json
  3. a tiny HTTP server that serves /dashboard.json (with CORS) and /health, so
     the website can fetch live data from one URL

Deploy this as a single web service (Render/Railway/a small VPS). One process,
one DB file, one URL. No Kafka, no cluster — that's Phase 2+.

Run:  PORT=8080 SCORE_EVERY_MIN=20 python -m sf.worker
"""
from __future__ import annotations

import hmac
import json
import os
import statistics as _stats
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Render (and most containers) buffer Python stdout, which hides our diagnostic
# prints. Force line-buffering so [discover]/[pipeline] logs show up live.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

from . import pipeline
from .ingest import store
from .ingest.harvester import AddressHarvester
from .sim.live_copy import LiveCopyEngine
from .validation.report import build_report
from . import config as C

DB_PATH = os.environ.get("SF_DB", store.DEFAULT_DB)
OUT_PATH = os.environ.get("SF_OUT", "dashboard.json")
LIVE_PATH = os.environ.get("SF_LIVE", "live_paper.json")
STATE_PATH = os.environ.get("SF_STATE", "live_state.json")
WF_PATH = os.environ.get("SF_WF", "walkforward.json")
WF_EVERY_HOURS = int(os.environ.get("WF_EVERY_HOURS", "24"))
WF_MAX_WALLETS = int(os.environ.get("WF_MAX_WALLETS", "700"))  # how many top-scored wallets the daily walk-forward evaluates (was hard-coded 300; ~0.23 survive as non-banned scored, so 700 → ~150, comfortably past the 120 the verdict firms up at)
PORT = int(os.environ.get("PORT", "8080"))
SCORE_EVERY_MIN = int(os.environ.get("SCORE_EVERY_MIN", "20"))
MAX_WALLETS = int(os.environ.get("SF_MAX_WALLETS", "150"))
SEEDS = [s for s in os.environ.get("SF_SEEDS", "").split(",") if s.strip()]

_last_run = {"at": 0, "ok": False, "kpis": {}}


def _atomic_write_json(path, obj, indent=None):
    """Write to a temp file then atomically rename, so a concurrent HTTP read never
    sees a half-written file (was causing intermittent 'warming_up' flashes)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=indent)
    os.replace(tmp, path)


def _basket_from_store() -> list[tuple[str, float]]:
    """Top eligible wallets -> capped, confidence-weighted, normalized weights."""
    with store.session(DB_PATH) as conn:
        rows = store.get_eligible_scores(conn, limit=C.PORTFOLIO["basket_size"])
    if not rows:
        return []
    # confidence-weight by sample size (full stake at ~60 trades) so thinner eligible
    # wallets get a smaller allocation until they've proven themselves.
    raw = {r["address"]: r["score"] * min((r["n_trades"] or 0) / 60.0, 1.0) for r in rows}
    tot = sum(raw.values()) or 1.0
    cap = C.PORTFOLIO["max_weight_per_trader"]
    return [(a, min(s / tot, cap)) for a, s in raw.items()]


def _harvester_thread():
    while True:
        try:
            AddressHarvester(db_path=DB_PATH).run()
        except Exception as e:  # noqa: BLE001
            print(f"[worker] harvester crashed, restarting in 10s: {e}")
            time.sleep(10)


def _livecopy_thread():
    """Wait until a basket exists, then mirror it live. If the engine ever stops,
    loop and refresh the basket. (Basket refresh = engine restart; fine for Phase 1.)"""
    while True:
        basket = _basket_from_store()
        if not basket:
            time.sleep(60)
            continue
        print(f"[worker] starting live copy on {len(basket)} basket wallets")
        try:
            LiveCopyEngine(basket, start_equity=C.PORTFOLIO["start_equity_usd"],
                           state_path=STATE_PATH).run(snapshot_path=LIVE_PATH, state_path=STATE_PATH,
                                                      basket_provider=_basket_from_store)
        except Exception as e:  # noqa: BLE001
            print(f"[worker] livecopy stopped ({e}); refreshing basket in 30s")
        time.sleep(30)


def _walkforward_thread():
    time.sleep(300)  # let the scorer populate the DB first
    while True:
        try:
            with store.session(DB_PATH) as conn:
                addrs = [r["address"] for r in conn.execute(
                    "SELECT address FROM wallet_scores ORDER BY score DESC").fetchall()]
                for a in store.top_addresses(conn, limit=300, min_hits=2):
                    if a not in addrs:
                        addrs.append(a)
            if addrs:
                print(f"[worker] walk-forward starting on {len(addrs)} wallets...")
                rep = build_report(addrs, max_wallets=WF_MAX_WALLETS)
                _atomic_write_json(WF_PATH, rep, indent=2)
                print(f"[worker] walk-forward: {rep['verdict']} "
                      f"({rep['wallets_analyzed']} wallets)")
        except Exception as e:  # noqa: BLE001
            print(f"[worker] walk-forward failed: {e}")
        time.sleep(WF_EVERY_HOURS * 3600)


def _scorer_thread():
    time.sleep(30)  # let the harvester collect some addresses first
    while True:
        try:
            print("[worker] scoring pass starting...")
            data = pipeline.run(SEEDS, max_wallets=MAX_WALLETS, db_path=DB_PATH)
            _atomic_write_json(OUT_PATH, data, indent=2)
            _last_run.update(at=int(time.time()), ok=True, kpis=data.get("kpis", {}))
            print(f"[worker] wrote {OUT_PATH}: {data.get('kpis')}")
        except Exception as e:  # noqa: BLE001
            _last_run.update(at=int(time.time()), ok=False)
            print(f"[worker] scoring pass failed: {e}")
        time.sleep(SCORE_EVERY_MIN * 60)


def _max_dd_from_curve(curve) -> float:
    """Max drawdown (%) from a [(ms, equity), ...] curve. 0.0 if too short."""
    peak, mdd = None, 0.0
    for pt in curve or []:
        try:
            v = float(pt[1])
        except (TypeError, ValueError, IndexError):
            continue
        if v <= 0:
            continue
        peak = v if peak is None else max(peak, v)
        if peak:
            mdd = max(mdd, (peak - v) / peak * 100.0)
    return round(mdd, 2)


def _apply_basket_overlay(data: dict) -> None:
    """The Intelligence page's basket tiles (P&L, Max DD, equity curve, Sortino,
    avg leverage, ...) used to read the HISTORICAL-REPLAY sim, which is starved of
    trip data for basket members scored in earlier passes -> it returned 0% / a flat
    curve. That is why the page looked dead while the live engine had real PnL.

    Fix: source the basket PERFORMANCE tiles from the LIVE forward record (the thing
    we actually publish, no backtest), and the basket COMPOSITION tiles from the
    eligible audited wallets. Everything here is real, no fabricated numbers.
    """
    kpis = data.setdefault("kpis", {})
    live = data.get("live") or {}
    curve = live.get("equity_curve") or []

    # --- performance, from the live forward paper-trading portfolio ---
    if isinstance(live, dict) and curve:
        if live.get("total_return_pct") is not None:
            kpis["basket_return_pct"] = live["total_return_pct"]      # "Basket P&L" (live, since start)
        kpis["basket_max_dd_pct"] = _max_dd_from_curve(curve)         # "Basket Max DD" (live)
        if live.get("win_rate") is not None:
            kpis["basket_win_rate"] = live["win_rate"]
        data["equity_curve"] = curve                                 # "Basket Equity Curve" (live)

    # --- composition, aggregated across the eligible (basket) wallets ---
    rows = data.get("traders_table") or []
    elig = [r for r in rows if r.get("eligible")]
    levs = [r["avg_lev"] for r in elig
            if r.get("leverage_known") and r.get("avg_lev") is not None]
    sharpes = [r["sharpe"] for r in elig if r.get("sharpe") is not None]
    sortinos = [r["sortino"] for r in elig if r.get("sortino") is not None]
    kpis["basket_count"] = len(elig)
    kpis["basket_avg_leverage"] = round(_stats.fmean(levs), 1) if levs else None
    kpis["basket_sharpe"] = round(_stats.median(sharpes), 2) if sharpes else kpis.get("basket_sharpe")
    kpis["basket_sortino"] = round(_stats.median(sortinos), 2) if sortinos else None
    # every eligible wallet is liquidation-free by rule (any liquidation = auto-ban)
    kpis["basket_liquidations"] = 0 if elig else None
    mls = [r["max_single_loss_pct"] for r in elig if r.get("max_single_loss_pct") is not None]
    kpis["basket_avg_max_loss_pct"] = round(_stats.fmean(mls), 1) if mls else None


def _merged_dashboard() -> bytes:
    """Scoring dashboard + live paper-trading snapshot, in one payload."""
    try:
        with open(OUT_PATH) as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        data = {"status": "warming_up"}
    try:
        with open(LIVE_PATH) as f:
            data["live"] = json.load(f)
    except Exception:  # noqa: BLE001
        data["live"] = {"status": "warming_up"}
    try:
        _apply_basket_overlay(data)
    except Exception as e:  # noqa: BLE001 - never let the overlay break the feed
        print(f"[worker] basket overlay skipped: {e}")
    return json.dumps(data).encode()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")  # site can fetch cross-origin
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/dashboard.json"):
            self._send(200, _merged_dashboard())
        elif self.path.startswith("/live.json"):
            try:
                with open(LIVE_PATH, "rb") as f:
                    self._send(200, f.read())
            except FileNotFoundError:
                self._send(503, b'{"status":"warming_up"}')
        elif self.path.startswith("/walkforward.json"):
            try:
                with open(WF_PATH, "rb") as f:
                    self._send(200, f.read())
            except FileNotFoundError:
                self._send(503, b'{"verdict":"warming_up","plain_english":"Walk-forward has not run yet. It runs after the backend warms up and then daily."}')
        elif self.path.startswith("/admin/wallets"):
            # Private: full (unmasked) addresses for the founder's own ground-truth checks.
            # Disabled unless SF_ADMIN_KEY is set. Prefer header X-Admin-Key (not logged); ?key=
            # still works for browser use but DOES appear in access logs. Constant-time compare.
            admin_key = os.environ.get("SF_ADMIN_KEY", "")
            from urllib.parse import urlparse, parse_qs
            given = self.headers.get("X-Admin-Key")
            if not given:
                given = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
            if not admin_key or not hmac.compare_digest(str(given), str(admin_key)):
                self._send(403, b'{"error":"forbidden - set SF_ADMIN_KEY; send header X-Admin-Key (preferred) or ?key="}')
                return
            with store.session(DB_PATH) as conn:
                rows = store.get_eligible_scores(conn, limit=C.PORTFOLIO["basket_size"])
            out = [{"address": r["address"], "score": round(r["score"], 1),
                    "archetype": r.get("archetype"), "n_trades": r.get("n_trades"),
                    "max_dd": r.get("max_dd"), "expectancy_pct": r.get("expectancy")}
                   for r in rows]
            self._send(200, json.dumps({"eligible_full_addresses": out}).encode())
        elif self.path.startswith("/health"):
            with store.session(DB_PATH) as conn:
                st = store.stats(conn)
            self._send(200, json.dumps({"status": "ok", "last_run": _last_run, "db": st}).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, *_):  # quiet default logging
        pass


def main():
    try:
        with store.session(DB_PATH) as conn:
            n = store.realign_eligibility(conn, C.AUTO_PASS["min_trades"], C.AUTO_PASS["min_history_days"])
        print(f"[startup] realigned eligibility to current copy gate: demoted {n} stale wallet(s)")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] eligibility realign skipped: {e}")
    threading.Thread(target=_harvester_thread, daemon=True).start()
    threading.Thread(target=_scorer_thread, daemon=True).start()
    threading.Thread(target=_livecopy_thread, daemon=True).start()
    threading.Thread(target=_walkforward_thread, daemon=True).start()
    print(f"[worker] serving on :{PORT}  (/dashboard.json, /live.json, /walkforward.json, /health)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
