# Build 2026-06-12-builderdex-pricing — Deploy Notes

## The bug this fixes (why you had zero live trades)
Your basket wallets trade builder-dex coins (xyz:AAPL, xyz:CL, vntl:SPACEX, ...)
heavily. Those coins are NOT in Hyperliquid's main allMids WS feed. The previous
build only polled a builder dex AFTER it already held a position in one of its
coins — but the staleness guard (correctly) refused to OPEN an unpriced position.
Deadlock: never priced -> never opened -> never priced. Result: every builder-coin
fill logged "SKIP open ... no fresh live mark (last never)" and the account sat flat.

## The fix
1. **Proactive builder-dex polling.** The engine now polls every known builder dex
   (`BUILDER_DEXES = {"xyz","vntl"}` in config) every cycle, so prices EXIST before
   the first open. Verified against the live API: `{"type":"allMids","dex":"xyz"}`
   returns keys already prefixed (`xyz:CL`), parsed verbatim.
2. **On-demand fallback.** If a fill arrives before the first poll (startup race),
   the engine fetches that one coin's price on the spot (cached ~30s) instead of
   skipping the open. If the price genuinely can't be fetched, it still skips —
   never opens blind.
3. **Tighter cadence.** Marketdata refresh 300s -> 120s so builder marks stay fresh.
4. Logs now print `priced N xyz: coins from builder dex` each cycle, and
   `builder-dex 'X' mid poll failed: ...` if a dex query errors — so you can SEE it working.

Unchanged: all gates, the staleness guard itself, disclaimers. Coins that truly
can't be priced are still never copied — honesty intact.

## Deploy (step by step)
1. Download the zip.
2. GitHub -> HyperPilot_Backend repo -> delete the old `signalforge` folder ->
   upload this new one -> **Commit changes**.
3. Render auto-redeploys (~2-3 min).
4. **DO NOT touch SF_RESET_LIVE.** Leave it on its current value. (Your logs showed
   it got flipped jun5->jun11->jun5 yesterday, which reset the account twice. Pick
   ONE value and never change it unless you deliberately want a fresh account.)
   Touch nothing else in env vars.
5. Open /health -> confirm "build": "2026-06-12-builderdex-pricing". If it shows the
   old stamp the deploy didn't take.

## What you should see within ~15 minutes (this is the proof it worked)
- Render logs: `[livecopy] priced NN xyz: coins from builder dex` (and vntl:) every
  ~2 min. This is the line that was missing.
- NO MORE `SKIP open xyz:... no fresh live mark` lines for normal builder coins.
- /live.json: as basket wallets open positions, `open_positions` fills in with real
  marks and `data_quality: "ok"`. The flat-line at 100,000 starts to move.
- These are still slow position traders (a few trades/week total across 5 wallets),
  so expect 1-3 opens in the first day, not a flood. But you will see SOMETHING.

## Honest note for later (not a code issue)
These five "disciplined position traders" trade tokenized stocks/commodities/pre-IPO
markets (AAPL, AMD, oil, gold, SpaceX, Anthropic) as much as crypto perps. When you
pitch this, describe the product accurately: "we copy disciplined traders across
Hyperliquid perps AND builder-dex tokenized markets," not "crypto perp traders."
Matters for diligence.
