"""
Hyperliquid ingestion client (Phase 1).

Uses only the free public Info API:
  POST https://api.hyperliquid.xyz/info   body={"type": <method>, ...}

Implements the pieces the audit + simulator actually need:
  - discover_universe()      candidate wallets to audit (leaderboard + seeds)
  - user_fills_by_time()     a wallet's executed trades (px, sz, dir, closedPnl, fee)
  - clearinghouse_state()    current positions, leverage, account value
  - funding_history()        per-coin funding rate series (for the sim)
  - l2_book() / candles()    market data for slippage + price-at-time

The address-keyed nature of the API is why discovery is its own step: there is
no "list all traders" call, so we seed from the public leaderboard and (later)
widen via the WS trades stream / a node.
"""
from __future__ import annotations

import time
import threading
from collections import deque
from typing import Any

import requests

from .. import config as C


class RateLimiter:
    """Simple sliding-window limiter so we never trip HL's IP weight cap."""

    def __init__(self, per_min: int):
        self.per_min = per_min
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.time()
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= self.per_min:
                sleep_for = 60 - (now - self.calls[0]) + 0.05
                time.sleep(max(sleep_for, 0))
            self.calls.append(time.time())


class HyperliquidClient:
    def __init__(self, api_url: str = C.HL_API):
        self.api_url = api_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.limiter = RateLimiter(C.RATE_LIMIT_PER_MIN)

    # -- low level -----------------------------------------------------------
    def _post(self, body: dict[str, Any]) -> Any:
        last_err: Exception | None = None
        for attempt in range(C.MAX_RETRIES):
            self.limiter.acquire()
            try:
                r = self.session.post(self.api_url, json=body, timeout=C.REQUEST_TIMEOUT_S)
                if r.status_code == 429:
                    time.sleep(C.RETRY_BACKOFF_S * (attempt + 1) * 2)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:  # noqa: BLE001 - retry on any transient failure
                last_err = e
                time.sleep(C.RETRY_BACKOFF_S * (attempt + 1))
        raise RuntimeError(f"Hyperliquid request failed after retries: {body.get('type')}: {last_err}")

    # -- metadata ------------------------------------------------------------
    def meta_and_asset_ctxs(self) -> Any:
        """Universe of perps + per-asset context (mark px, funding, oi)."""
        return self._post({"type": "metaAndAssetCtxs"})

    def all_mids(self) -> dict[str, str]:
        return self._post({"type": "allMids"})

    # -- discovery -----------------------------------------------------------
    def discover_universe(self, extra_seeds: list[str] | None = None,
                          leaderboard_url: str = C.HL_LEADERBOARD) -> list[str]:
        """
        Build the candidate wallet set to audit.

        Strategy (Phase 1, free):
          1. Pull the public leaderboard snapshot for the active-trader universe.
          2. Union with any manually-seeded addresses you trust/watch.
        Phase 3 widens this via the WS `trades` stream and/or a HyperCore node so
        coverage approaches "every active wallet", not just leaderboard names.
        """
        addrs: set[str] = set()
        try:
            r = self.session.get(leaderboard_url, timeout=C.REQUEST_TIMEOUT_S)
            r.raise_for_status()
            data = r.json()
            rows = data.get("leaderboardRows", data if isinstance(data, list) else [])
            for row in rows:
                a = (row.get("ethAddress") or row.get("user") or "").lower()
                if a.startswith("0x") and len(a) == 42:
                    addrs.add(a)
        except Exception as e:  # noqa: BLE001
            # Leaderboard URL/shape changes occasionally; discovery must degrade
            # gracefully to seeds rather than crash the whole pipeline.
            print(f"[discover] leaderboard fetch failed ({e}); falling back to seeds")
        for a in (extra_seeds or []):
            a = a.lower()
            if a.startswith("0x") and len(a) == 42:
                addrs.add(a)
        return sorted(addrs)

    # -- per-wallet ----------------------------------------------------------
    def user_fills_by_time(self, address: str, start_ms: int,
                           end_ms: int | None = None) -> list[dict]:
        """
        A wallet's fills in [start_ms, end_ms]. Each fill:
          coin, px, sz, side('B'|'A'), time, startPosition, dir('Open Long'/
          'Close Short'/...), closedPnl, fee, hash, oid, crossed, tid
        HL returns at most 2000 fills/call, so we page backwards by time.
        """
        out: list[dict] = []
        cursor = end_ms or int(time.time() * 1000)
        while cursor > start_ms:
            body = {"type": "userFillsByTime", "user": address,
                    "startTime": start_ms, "endTime": cursor}
            batch = self._post(body) or []
            if not batch:
                break
            out.extend(batch)
            oldest = min(int(f["time"]) for f in batch)
            if oldest <= start_ms or len(batch) < 2000:
                break
            cursor = oldest - 1
        # de-dup by trade id, ascending by time
        seen, uniq = set(), []
        for f in sorted(out, key=lambda x: int(x["time"])):
            tid = f.get("tid") or (f.get("hash"), f.get("oid"), f.get("time"))
            if tid in seen:
                continue
            seen.add(tid)
            if start_ms <= int(f["time"]) <= (end_ms or cursor + 1):
                uniq.append(f)
        return uniq

    def clearinghouse_state(self, address: str) -> dict:
        """Current account: assetPositions[].position(.leverage, szi, entryPx,
        liquidationPx), marginSummary.accountValue, withdrawable, etc."""
        return self._post({"type": "clearinghouseState", "user": address})

    def user_funding(self, address: str, start_ms: int) -> list[dict]:
        return self._post({"type": "userFunding", "user": address, "startTime": start_ms}) or []

    # -- market data (for the simulator) -------------------------------------
    def funding_history(self, coin: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        body = {"type": "fundingHistory", "coin": coin, "startTime": start_ms}
        if end_ms:
            body["endTime"] = end_ms
        return self._post(body) or []

    def l2_book(self, coin: str) -> dict:
        return self._post({"type": "l2Book", "coin": coin})

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
        req = {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}
        return self._post({"type": "candleSnapshot", "req": req}) or []
