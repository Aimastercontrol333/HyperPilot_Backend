"""
Live copy paper-trading engine.

This is the real-time half of the product. While `paper_trader.py` replays
history to prove the basket, this engine mirrors the basket *as it trades, live*:

  - subscribe to `userFills` for every basket wallet + `allMids` for prices
  - when a basket wallet OPENS a position  -> open a mirrored PAPER position now,
    priced at the live market through the fill model (never the trader's price)
  - mark every open paper position to market every tick
  - when the wallet CLOSES (or OUR own stop fires) -> close the paper position,
    realise PnL net of fees + funding, append to the closed-trade log
  - expose a live snapshot (open positions, closed trades, equity, today's PnL)
    that the worker serves and the website renders

`PaperPortfolio` is pure logic (no network) so it can be unit-tested with
synthetic fills/prices. `LiveCopyEngine` wires it to the Hyperliquid WebSocket.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict

from .. import config as C
from . import fill_model as fm

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None


def _dir_delta(dir_str: str, sz: float) -> float:
    """Signed change to the trader's net position for a fill `dir`."""
    if "Liquidat" in dir_str:
        return 0.0  # handled as forced flat by caller
    if dir_str == "Open Long":
        return +sz
    if dir_str == "Close Long":
        return -sz
    if dir_str == "Open Short":
        return -sz
    if dir_str == "Close Short":
        return +sz
    return 0.0


@dataclass
class PaperPosition:
    trader: str
    coin: str
    side: str            # long | short
    entry_px: float
    notional: float
    weight: float
    open_ms: int
    entry_cost_bps: float


@dataclass
class PaperPortfolio:
    start_equity: float
    weights: dict[str, float]                      # address -> weight (sum<=1)
    own_stop_pct: float = C.PORTFOLIO["own_stop_loss_pct"]
    realized_pnl: float = 0.0
    open_positions: dict[tuple, PaperPosition] = field(default_factory=dict)
    trader_net: dict[tuple, float] = field(default_factory=dict)
    closed: list[dict] = field(default_factory=list)
    per_trader: dict = field(default_factory=dict)   # truncated addr -> {pnl,trades,wins}
    day_start_equity: float = 0.0
    day_start_ms: int = 0
    equity_history: list = field(default_factory=list)   # [(ms, equity), ...]
    spread_bps: dict = field(default_factory=dict)        # coin -> measured half-spread (bps), set by engine
    funding_hr: dict = field(default_factory=dict)        # coin -> recent avg funding (bps/hour), set by engine
    _equity: float = 0.0
    _last_eq_ms: int = 0

    def __post_init__(self):
        self._equity = self.start_equity
        self.day_start_equity = self.start_equity
        self.day_start_ms = int(time.time() * 1000)
        self.equity_history = [(self.day_start_ms, round(self.start_equity, 2))]
        self._last_eq_ms = self.day_start_ms

    # -- event in: a basket wallet's fill ------------------------------------
    def on_fill(self, trader: str, coin: str, dir_str: str, sz: float,
                mark_px: float, ts: int) -> None:
        if trader not in self.weights or mark_px <= 0:
            return
        key = (trader, coin)
        prev = self.trader_net.get(key, 0.0)
        if "Liquidat" in dir_str:
            new = 0.0
        else:
            new = prev + _dir_delta(dir_str, sz)
        self.trader_net[key] = new

        was_in = abs(prev) > 1e-12
        now_in = abs(new) > 1e-12

        # transition flat -> in : OPEN mirror
        if not was_in and now_in:
            self._open(trader, coin, "long" if new > 0 else "short", mark_px, ts)
        # transition in -> flat : CLOSE mirror
        elif was_in and not now_in:
            self._close(key, mark_px, ts, reason="trader_closed")
        # sign flip : close then open
        elif was_in and now_in and (prev > 0) != (new > 0):
            self._close(key, mark_px, ts, reason="trader_flipped")
            self._open(trader, coin, "long" if new > 0 else "short", mark_px, ts)

    def _open(self, trader, coin, side, mark_px, ts):
        weight = self.weights.get(trader, 0.0)
        notional = self._equity * min(weight, C.PORTFOLIO["max_weight_per_trader"])
        if notional <= 0:
            return
        costs = fm.estimate_costs(coin, notional, None, self.spread_bps.get(coin))
        entry = fm.apply_entry(mark_px, side, costs)
        self.open_positions[(trader, coin)] = PaperPosition(
            trader=trader, coin=coin, side=side, entry_px=entry, notional=notional,
            weight=weight, open_ms=ts, entry_cost_bps=costs.entry_cost_bps + costs.fee_bps)

    def _close(self, key, mark_px, ts, reason: str, forced_ret_pct: float | None = None):
        pos = self.open_positions.pop(key, None)
        if pos is None:
            return
        costs = fm.estimate_costs(pos.coin, pos.notional, None, self.spread_bps.get(pos.coin))
        if forced_ret_pct is not None:
            net_pct = forced_ret_pct
        else:
            exit_px = fm.apply_exit(mark_px, pos.side, costs)
            s = 1.0 if pos.side == "long" else -1.0
            gross_pct = s * (exit_px - pos.entry_px) / pos.entry_px * 100.0
            hold_h = max((ts - pos.open_ms) / 3.6e6, 0.0)
            fseries = [self.funding_hr[pos.coin]] if pos.coin in self.funding_hr else None
            funding = fm.funding_cost_usd(pos.notional, pos.side, hold_h, fseries)
            funding_pct = funding / pos.notional * 100.0 if pos.notional else 0.0
            # entry costs already implied in entry_px; subtract exit-side + fee here
            net_pct = gross_pct - (costs.exit_cost_bps + costs.fee_bps) / 100.0 - funding_pct
        pnl = pos.notional * net_pct / 100.0
        self.realized_pnl += pnl
        self._equity += pnl
        tkey = pos.trader[:6] + "…" + pos.trader[-4:]
        pt = self.per_trader.setdefault(tkey, {"pnl": 0.0, "trades": 0, "wins": 0})
        pt["pnl"] += pnl; pt["trades"] += 1; pt["wins"] += 1 if net_pct > 0 else 0
        self.closed.append({
            "coin": pos.coin, "side": pos.side, "trader": pos.trader[:6] + "…" + pos.trader[-4:],
            "open_ms": pos.open_ms, "close_ms": ts, "notional": round(pos.notional, 2),
            "net_ret_pct": round(net_pct, 3), "pnl_usd": round(pnl, 2), "reason": reason,
        })
        self.closed = self.closed[-500:]

    # -- periodic: mark to market + enforce our own stop ---------------------
    def mark_to_market(self, mids: dict[str, float], now_ms: int) -> None:
        unreal = 0.0
        for key, pos in list(self.open_positions.items()):
            mark = mids.get(pos.coin)
            if not mark:
                continue
            s = 1.0 if pos.side == "long" else -1.0
            gross_pct = s * (mark - pos.entry_px) / pos.entry_px * 100.0
            hold_h = max((now_ms - pos.open_ms) / 3.6e6, 0.0)
            fseries = [self.funding_hr[pos.coin]] if pos.coin in self.funding_hr else None
            funding_pct = (fm.funding_cost_usd(pos.notional, pos.side, hold_h, fseries)
                           / pos.notional * 100.0) if pos.notional else 0.0
            net_pct = gross_pct - funding_pct
            # OUR discipline overlay: independent stop, even if the trader holds
            if net_pct <= -self.own_stop_pct:
                self._close(key, mark, now_ms, reason="our_stop", forced_ret_pct=-self.own_stop_pct)
                continue
            unreal += pos.notional * net_pct / 100.0
        self._equity = self.start_equity + self.realized_pnl + unreal
        # record an equity point at most every 2 minutes (cap series length)
        if now_ms - self._last_eq_ms >= 120_000:
            self.equity_history.append((now_ms, round(self._equity, 2)))
            self.equity_history = self.equity_history[-240:]
            self._last_eq_ms = now_ms
        # daily reset
        if now_ms - self.day_start_ms > 86_400_000:
            self.day_start_equity = self._equity
            self.day_start_ms = now_ms

    # -- live snapshot for the website ---------------------------------------
    def snapshot(self, mids: dict[str, float] | None = None) -> dict:
        mids = mids or {}
        now = int(time.time() * 1000)
        open_list = []
        for pos in self.open_positions.values():
            mark = mids.get(pos.coin, pos.entry_px)
            s = 1.0 if pos.side == "long" else -1.0
            unreal_pct = s * (mark - pos.entry_px) / pos.entry_px * 100.0
            open_list.append({
                "coin": pos.coin, "side": pos.side,
                "trader": pos.trader[:6] + "…" + pos.trader[-4:],
                "entry": round(pos.entry_px, 4), "mark": round(mark, 4),
                "unreal_pct": round(unreal_pct, 2),
                "age_h": round((now - pos.open_ms) / 3.6e6, 1),
                "notional": round(pos.notional, 2),
            })
        wins = [c for c in self.closed if c["net_ret_pct"] > 0]
        return {
            "status": "STREAMING",
            "equity": round(self._equity, 2),
            "start_equity": self.start_equity,
            "total_return_pct": round((self._equity / self.start_equity - 1) * 100, 2),
            "today_pnl_usd": round(self._equity - self.day_start_equity, 2),
            "today_pnl_pct": round((self._equity / self.day_start_equity - 1) * 100, 2)
                              if self.day_start_equity else 0.0,
            "open_positions": sorted(open_list, key=lambda x: -x["age_h"]),
            "open_count": len(open_list),
            "closed_count": len(self.closed),
            "win_rate": round(len(wins) / len(self.closed), 3) if self.closed else 0.0,
            "recent_closed": list(reversed(self.closed[-30:])),
            "equity_curve": self.equity_history,
            "per_trader": {k: {"pnl": round(v["pnl"], 2), "trades": v["trades"],
                               "win_rate": round(v["wins"] / v["trades"], 2) if v["trades"] else 0}
                           for k, v in self.per_trader.items()},
            "updated_at": now,
        }

    # -- persistence across restarts (Render disk) ---------------------------
    def to_state(self) -> dict:
        return {"start_equity": self.start_equity, "realized_pnl": self.realized_pnl,
                "equity_history": self.equity_history, "day_start_equity": self.day_start_equity,
                "day_start_ms": self.day_start_ms, "closed": self.closed[-200:],
                "per_trader": self.per_trader}

    @classmethod
    def from_state(cls, state: dict, weights: dict, own_stop_pct: float) -> "PaperPortfolio":
        pf = cls(start_equity=state.get("start_equity", C.PORTFOLIO["start_equity_usd"]),
                 weights=weights, own_stop_pct=own_stop_pct)
        pf.realized_pnl = state.get("realized_pnl", 0.0)
        hist = [tuple(p) for p in state.get("equity_history", [])]
        if hist:
            pf.equity_history = hist
            pf._last_eq_ms = hist[-1][0]
        pf.day_start_equity = state.get("day_start_equity", pf.day_start_equity)
        pf.day_start_ms = state.get("day_start_ms", pf.day_start_ms)
        pf.closed = state.get("closed", [])
        pf.per_trader = state.get("per_trader", {})
        pf._equity = pf.start_equity + pf.realized_pnl
        return pf


class LiveCopyEngine:
    """Wires PaperPortfolio to the Hyperliquid WebSocket (userFills + allMids)."""

    def __init__(self, basket: list[tuple[str, float]], ws_url: str = C.HL_WS,
                 start_equity: float = C.PORTFOLIO["start_equity_usd"],
                 state_path: str | None = None):
        self.basket = basket
        self.ws_url = ws_url
        self.weights = dict(basket)
        self.state_path = state_path
        prior = None
        if state_path and os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    prior = json.load(f)
            except Exception:  # noqa: BLE001
                prior = None
        if prior:
            self.pf = PaperPortfolio.from_state(prior, self.weights, C.PORTFOLIO["own_stop_loss_pct"])
            print(f"[livecopy] resumed from saved state (equity={self.pf._equity:.0f}, "
                  f"{len(self.pf.equity_history)} curve points)")
        else:
            self.pf = PaperPortfolio(start_equity=start_equity, weights=self.weights)
        self.mids: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ws = None
        self._basket_provider = None
        try:
            from ..ingest.hyperliquid import HyperliquidClient
            self.client = HyperliquidClient()
        except Exception:  # noqa: BLE001
            self.client = None

    def _marketdata_loop(self, every_s: int = 300):
        """Populate the portfolio's real-cost caches from Hyperliquid: the measured
        top-of-book half-spread (from l2Book) and each coin's recent hourly funding
        (from fundingHistory). Refreshes coins we currently hold plus a watchlist of
        common coins, so most entries and every close price through real data instead
        of the tier estimate."""
        if self.client is None:
            return
        watch = set(getattr(C, "MAJOR_COINS", set())) | set(getattr(C, "MID_COINS", set()))
        while True:
            try:
                coins = set(self.pf.spread_bps)  # keep refreshing what we've seen
                coins |= {pos.coin for pos in self.pf.open_positions.values()}
                coins |= watch
                now = int(time.time() * 1000)
                for coin in list(coins):
                    # measured half-spread from the live book
                    try:
                        book = self.client.l2_book(coin) or {}
                        levels = book.get("levels") or []
                        if len(levels) >= 2 and levels[0] and levels[1]:
                            bid = float(levels[0][0]["px"]); ask = float(levels[1][0]["px"])
                            mid = (bid + ask) / 2.0
                            if mid > 0 and ask >= bid:
                                self.pf.spread_bps[coin] = (ask - bid) / 2.0 / mid * 1e4
                    except Exception:  # noqa: BLE001
                        pass
                    # recent hourly funding (bps/hour) averaged over the last ~24h
                    try:
                        hist = self.client.funding_history(coin, now - 24 * 3_600_000) or []
                        rates = [float(h["fundingRate"]) * 1e4 for h in hist if "fundingRate" in h]
                        if rates:
                            self.pf.funding_hr[coin] = sum(rates) / len(rates)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            time.sleep(every_s)

    def _on_open(self, ws):
        self._ws = ws
        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        for addr, _ in self.basket:
            ws.send(json.dumps({"method": "subscribe",
                                "subscription": {"type": "userFills", "user": addr}}))
        print(f"[livecopy] mirroring {len(self.basket)} basket wallets")

    def _refresh_loop(self, every_s: int):
        """Periodically re-read the eligible basket and subscribe to newly-eligible
        wallets in place. Without this the engine froze on whatever basket existed at
        startup (near-empty during the warmup sweep) and ignored every wallet that
        qualified afterward."""
        while True:
            time.sleep(every_s)
            provider = getattr(self, "_basket_provider", None)
            if not provider:
                continue
            try:
                new_basket = provider()
            except Exception:  # noqa: BLE001
                continue
            if not new_basket:
                continue
            new_weights = dict(new_basket)
            with self._lock:
                added = [a for a, _ in new_basket if a not in self.weights]
                self.weights = new_weights
                self.pf.weights = new_weights
                self.basket = new_basket
            ws = getattr(self, "_ws", None)
            if ws is not None and added:
                for a in added:
                    try:
                        ws.send(json.dumps({"method": "subscribe",
                                            "subscription": {"type": "userFills", "user": a}}))
                    except Exception:  # noqa: BLE001
                        pass
                print(f"[livecopy] basket refreshed: +{len(added)} new wallets now mirrored "
                      f"(total {len(new_basket)})")

    def _on_message(self, ws, message: str):
        try:
            msg = json.loads(message)
        except Exception:  # noqa: BLE001
            return
        ch = msg.get("channel")
        if ch == "allMids":
            mids = (msg.get("data") or {}).get("mids", {})
            with self._lock:
                for coin, px in mids.items():
                    try:
                        self.mids[coin] = float(px)
                    except (TypeError, ValueError):
                        pass
        elif ch == "userFills":
            data = msg.get("data") or {}
            if data.get("isSnapshot"):
                return  # ignore historical snapshot; we only mirror new fills
            user = (data.get("user") or "").lower()
            for f in data.get("fills", []):
                coin = f.get("coin"); dir_str = f.get("dir", "")
                try:
                    sz = float(f.get("sz", 0))
                except (TypeError, ValueError):
                    continue
                with self._lock:
                    mark = self.mids.get(coin) or float(f.get("px", 0) or 0)
                    self.pf.on_fill(user, coin, dir_str, sz, mark, int(f.get("time", time.time() * 1000)))

    def _mtm_loop(self, snapshot_path: str, every_s: int = 5):
        n = 0
        while True:
            time.sleep(every_s)
            with self._lock:
                self.pf.mark_to_market(self.mids, int(time.time() * 1000))
                snap = self.pf.snapshot(self.mids)
                state = self.pf.to_state()
            try:
                tmp = snapshot_path + ".tmp"
                with open(tmp, "w") as fp:
                    json.dump(snap, fp)
                os.replace(tmp, snapshot_path)
            except Exception:  # noqa: BLE001
                pass
            # save resume state to disk every ~1 min (every 12th 5s cycle)
            n += 1
            if self.state_path and n % 12 == 0:
                try:
                    tmp = self.state_path + ".tmp"
                    with open(tmp, "w") as fp:
                        json.dump(state, fp)
                    os.replace(tmp, self.state_path)
                except Exception:  # noqa: BLE001
                    pass

    def run(self, snapshot_path: str = "live_paper.json", state_path: str | None = None,
            basket_provider=None, refresh_every_s: int = 300):
        if state_path:
            self.state_path = state_path
        self._basket_provider = basket_provider
        if websocket is None:
            raise RuntimeError("websocket-client not installed. Run: pip install websocket-client")
        threading.Thread(target=self._mtm_loop, args=(snapshot_path,), daemon=True).start()
        threading.Thread(target=self._marketdata_loop, daemon=True).start()
        if basket_provider is not None:
            threading.Thread(target=self._refresh_loop, args=(refresh_every_s,), daemon=True).start()
        ws = websocket.WebSocketApp(self.ws_url, on_open=self._on_open, on_message=self._on_message)
        ws.run_forever(reconnect=5, ping_interval=30, ping_timeout=10)
