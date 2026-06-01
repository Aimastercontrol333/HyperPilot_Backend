"""
Discovery widener — WebSocket trade-stream address harvester.

The Hyperliquid Info API is address-keyed (no "list all traders" call), so we
discover the universe ourselves: subscribe to the public `trades` feed for a set
of coins and harvest the wallet addresses out of every trade's `users` field
(confirmed in the HL docs: WsTrade carries `users: [addrA, addrB]`). Every print
gives us two real, active wallets to audit.

Design notes:
  - Synchronous `websocket-client` with run_forever(reconnect=...) so a non-tech
    operator can run it as one long-lived process that heals itself.
  - HL allows up to 1000 subscriptions/IP; we batch a handful of liquid coins.
  - Addresses are buffered and flushed to the SQLite store periodically to keep
    write pressure low.
  - Reconnects are expected and handled (HL disconnects without warning).

Run as the always-on worker:  python -m sf.ingest.harvester
"""
from __future__ import annotations

import json
import threading
import time

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None

from .. import config as C
from . import store

# Coins to listen on. More coins = wider discovery, but mind the 1000-sub cap.
DEFAULT_COINS = ["BTC", "ETH", "SOL", "HYPE", "ARB", "AVAX", "LINK", "DOGE",
                 "XRP", "SUI", "OP", "BNB"]
FLUSH_EVERY_S = 10
FLUSH_EVERY_N = 200


class AddressHarvester:
    def __init__(self, coins: list[str] | None = None, db_path: str = store.DEFAULT_DB,
                 ws_url: str = C.HL_WS):
        self.coins = coins or DEFAULT_COINS
        self.db_path = db_path
        self.ws_url = ws_url
        self._buffer: dict[str, set[str]] = {}   # coin -> set(addresses)
        self._buf_count = 0
        self._last_flush = time.time()
        self._lock = threading.Lock()
        self._total_new = 0
        self._conn = store.connect(db_path)

    # -- ws lifecycle --------------------------------------------------------
    def _on_open(self, ws):
        for coin in self.coins:
            ws.send(json.dumps({"method": "subscribe",
                                "subscription": {"type": "trades", "coin": coin}}))
        print(f"[harvester] subscribed to trades for {len(self.coins)} coins")

    def _on_message(self, ws, message: str):
        try:
            msg = json.loads(message)
        except Exception:  # noqa: BLE001
            return
        if msg.get("channel") != "trades":
            return
        trades = msg.get("data") or []
        for t in trades:
            coin = t.get("coin", "?")
            users = t.get("users") or []          # [buyer, seller]
            with self._lock:
                bucket = self._buffer.setdefault(coin, set())
                for u in users:
                    u = (u or "").lower()
                    if u.startswith("0x") and len(u) == 42:
                        bucket.add(u)
                        self._buf_count += 1
        self._maybe_flush()

    def _on_error(self, ws, err):
        print(f"[harvester] ws error: {err}")

    def _on_close(self, ws, code, reason):
        print(f"[harvester] ws closed ({code} {reason}); will reconnect")
        self._flush()

    # -- buffering / persistence --------------------------------------------
    def _maybe_flush(self):
        if (self._buf_count >= FLUSH_EVERY_N
                or time.time() - self._last_flush >= FLUSH_EVERY_S):
            self._flush()

    def _flush(self):
        with self._lock:
            if not self._buffer:
                self._last_flush = time.time()
                return
            snapshot = {c: list(s) for c, s in self._buffer.items() if s}
            self._buffer.clear()
            self._buf_count = 0
        new = 0
        for coin, addrs in snapshot.items():
            new += store.record_addresses(self._conn, addrs, coin)
        self._total_new += new
        st = store.stats(self._conn)
        print(f"[harvester] +{new} new addresses (total discovered={st['discovered']})")
        self._last_flush = time.time()

    # -- run -----------------------------------------------------------------
    def run(self):
        if websocket is None:
            raise RuntimeError("websocket-client not installed. Run: pip install websocket-client")
        ws = websocket.WebSocketApp(
            self.ws_url, on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close)
        # run_forever auto-reconnects; ping keeps the connection alive
        ws.run_forever(reconnect=5, ping_interval=30, ping_timeout=10)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Harvest active wallet addresses from HL trade stream")
    ap.add_argument("--coins", default=",".join(DEFAULT_COINS))
    ap.add_argument("--db", default=store.DEFAULT_DB)
    args = ap.parse_args()
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    print(f"[harvester] starting on {coins}")
    AddressHarvester(coins=coins, db_path=args.db).run()


if __name__ == "__main__":
    main()
