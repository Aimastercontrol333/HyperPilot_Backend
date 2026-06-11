# Build 2026-06-11-staleness-volstop-wf3 — Deploy Notes

## What changed (and why)

### Tier 1 — Data integrity (the no-fake-data fixes)
1. **Feed-staleness guard.** A position whose coin has no fresh mark within 15 min
   (`STALE_MARK_MAX_S=900`): its PnL is excluded from equity, its stop/TP are NOT
   evaluated (we never trade on untrusted prices), and it is flagged `stale` in the
   feed. `live.json` gains `data_quality` ("ok"/"degraded"), `unpriced_count`,
   `unpriced_notional_usd`. The site shows "unpriced · no live mark" instead of a
   frozen fake price.
2. **Unpriced coins are never copied.** `_open` refuses any coin without a fresh
   live mark (`ALLOW_UNPRICED_OPENS=False`). This ends the xyz:GOLD-frozen-at-entry-
   for-6-days failure mode.
3. **Builder-dex pricing.** The marketdata loop now polls `allMids` per builder dex
   (e.g. `{"type":"allMids","dex":"xyz"}`) every ~5 min, so xyz: coins get real
   marks where the API provides them. If the dex can't be queried they simply stay
   unpriced and the guard handles them honestly. WATCH the first hours: if xyz:
   coins start showing real moving marks, the polling works; if they show
   "unpriced", the dex query isn't supported in this shape — they'll be excluded
   from copying either way, which is safe.
4. **Restart persistence of open positions.** `to_state`/`from_state` now carry
   open positions + trader nets. Previously a Render restart silently vaporized all
   open exposure (no closure records) — an integrity hole in the public record.

### Tier 3 — Copy mechanics
5. **Volatility-aware stop.** Per-position stop = clamp(1.5 × coin's avg daily
   move, 5%..10%), from 14d daily candles. Falls back to flat 5% when no vol data.
   Tunables: `PORTFOLIO["stop_vol_mult"]` (0 disables), `"stop_cap_pct"`.
6. **ON_DELIST policy.** When a wallet drops out of the basket while we hold its
   positions: `"tighten_stop"` (default — halves remaining stop room), `"close"`
   (exits at next FRESH mark only), or `"ride"`. Also fixed: a de-listed trader's
   own closes now mirror correctly (the old guard silently dropped their fills).
7. **Suspension auto-review.** A circuit-breaker-suspended wallet is reinstated
   after `SUSPENSION_REVIEW_DAYS=7` with its breaker counter reset (it can re-fire).

### Tier 2 — Validation
8. **Multi-window walk-forward.** The daily report now runs `WF_WINDOWS=3`
   staggered 60-day holdouts from ONE data pull (same API budget) and reports a
   cross-window `consensus` verdict: `predictive` (all windows), `predictive_majority`,
   or `weak_or_none`. Top-level fields mirror the most recent window so the site
   keeps working. Set env `WF_WINDOWS=1` to restore the old single-window report.
   Note: 3 windows of 60d need 180d of forward data + training history; default
   lookback raised to 240d for this path. Early windows may say insufficient_data
   until enough wallets have long histories — that's honest, not broken.
9. **Blow-up autopsy tool** (research, founder-run, changes nothing in prod):
   `python -m sf.validation.autopsy --db /var/data/signalforge.db --out autopsy.json`
   Fingerprints the gate-KEPT wallets that blew up forward vs those that survived,
   and ranks which training-window features separate them — that ranking is where
   the next ban/penalty rule lives. Run it after a few days; verify top candidates
   against Hyperliquid's app before changing any gate.

## Deploy (click-by-click)
1. GitHub repo `Aimastercontrol333/HyperPilot_Backend`: delete the old
   `signalforge` folder, upload this new one, commit. Render auto-redeploys.
2. (Optional) Render env: add `WF_WINDOWS=3` explicitly (3 is already the default).
   Do NOT touch the Disk. Editing env vars never wipes it.
3. Verify `/health` shows `"build": "2026-06-11-staleness-volstop-wf3"`.
   If it shows the old stamp, the deploy didn't take — check Render logs.
4. Upload the new `index.html` to the Vercel repo (HyperPilot_HYPI), commit,
   hard-refresh the site (Cmd+Shift+R).

## What to verify after deploy
- `/health` → new build stamp, `last_run.ok: true`.
- `/live.json` → has `data_quality` field. If "degraded": `unpriced_notional_usd`
  shows exactly how much of the book has no live price — that exposure is now
  EXCLUDED from the equity headline (expect equity to shift by the amount the old
  frozen marks were contributing, i.e. likely no change since they contributed 0,
  but the labeling is now honest).
- Within ~10 min of start: Render logs show either xyz: coins getting marks from
  the dex poll, or "SKIP open ... unpriced coins are not copied" lines. Both are
  correct behavior.
- New closed trades may show varying stop sizes (5–10%) in `net_ret_pct` for
  `our_stop` exits — that's the vol-aware stop, not a bug.
- Next walk-forward run (within 24h or after restart): `/walkforward.json` has
  `mode: "multi_window"`, a `windows` array, and a `consensus` block. The
  `plain_english` field summarizes the cross-window read.
- Restart-resilience: after any Render restart, open positions should REAPPEAR
  in `/live.json` (previously they vanished).

## Unchanged on purpose
All scoring gates, ban thresholds, the 25% DD cap, Sharpe bar 1.5, basket rules,
disclaimers, masked addresses. The smoke test (`python smoke_test.py`) now covers
the new integrity layer — run it locally any time.
