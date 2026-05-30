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
    day_start_equity: float = 0.0
    day_start_ms: int = 0
    _equity: float = 0.0

    def __post_init__(self):
        self._equity = self.start_equity
        self.day_start_equity = self.start_equity
        self.day_start_ms = int(time.time() * 1000)

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
        costs = fm.estimate_costs(coin, notional, None)
        entry = fm.apply_entry(mark_px, side, costs)
        self.open_positions[(trader, coin)] = PaperPosition(
            trader=trader, coin=coin, side=side, entry_px=entry, notional=notional,
            weight=weight, open_ms=ts, entry_cost_bps=costs.entry_cost_bps + costs.fee_bps)

    def _close(self, key, mark_px, ts, reason: str, forced_ret_pct: float | None = None):
        pos = self.open_positions.pop(key, None)
        if pos is None:
            return
        costs = fm.estimate_costs(pos.coin, pos.notional, None)
        if forced_ret_pct is not None:
            net_pct = forced_ret_pct
        else:
            exit_px = fm.apply_exit(mark_px, pos.side, costs)
            s = 1.0 if pos.side == "long" else -1.0
            gross_pct = s * (exit_px - pos.entry_px) / pos.entry_px * 100.0
            hold_h = max((ts - pos.open_ms) / 3.6e6, 0.0)
            funding = fm.funding_cost_usd(pos.notional, pos.side, hold_h, None)
            funding_pct = funding / pos.notional * 100.0 if pos.notional else 0.0
            # entry costs already implied in entry_px; subtract exit-side + fee here
            net_pct = gross_pct - (costs.exit_cost_bps + costs.fee_bps) / 100.0 - funding_pct
        pnl = pos.notional * net_pct / 100.0
        self.realized_pnl += pnl
        self._equity += pnl
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
            funding_pct = (fm.funding_cost_usd(pos.notional, pos.side, hold_h, None)
                           / pos.notional * 100.0) if pos.notional else 0.0
            net_pct = gross_pct - funding_pct
            # OUR discipline overlay: independent stop, even if the trader holds
            if net_pct <= -self.own_stop_pct:
                self._close(key, mark, now_ms, reason="our_stop", forced_ret_pct=-self.own_stop_pct)
                continue
            unreal += pos.notional * net_pct / 100.0
        self._equity = self.start_equity + self.realized_pnl + unreal
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
            "updated_at": now,
        }


class LiveCopyEngine:
    """Wires PaperPortfolio to the Hyperliquid WebSocket (userFills + allMids)."""

    def __init__(self, basket: list[tuple[str, float]], ws_url: str = C.HL_WS,
                 start_equity: float = C.PORTFOLIO["start_equity_usd"]):
        self.basket = basket
        self.ws_url = ws_url
        self.weights = dict(basket)
        self.pf = PaperPortfolio(start_equity=start_equity, weights=self.weights)
        self.mids: dict[str, float] = {}
        self._lock = threading.Lock()

    def _on_open(self, ws):
        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        for addr, _ in self.basket:
            ws.send(json.dumps({"method": "subscribe",
                                "subscription": {"type": "userFills", "user": addr}}))
        print(f"[livecopy] mirroring {len(self.basket)} basket wallets")

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
        while True:
            time.sleep(every_s)
            with self._lock:
                self.pf.mark_to_market(self.mids, int(time.time() * 1000))
                snap = self.pf.snapshot(self.mids)
            try:
                with open(snapshot_path, "w") as fp:
                    json.dump(snap, fp)
            except Exception:  # noqa: BLE001
                pass

    def run(self, snapshot_path: str = "live_paper.json"):
        if websocket is None:
            raise RuntimeError("websocket-client not installed. Run: pip install websocket-client")
        threading.Thread(target=self._mtm_loop, args=(snapshot_path,), daemon=True).start()
        ws = websocket.WebSocketApp(self.ws_url, on_open=self._on_open, on_message=self._on_message)
        ws.run_forever(reconnect=5, ping_interval=30, ping_timeout=10)
