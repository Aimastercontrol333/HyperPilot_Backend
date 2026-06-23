# Build 2026-06-23-volstop-widen-basket — Deploy Notes

Three fixes, all driven by your 13-day live data audit. NONE of these loosen a
risk gate (drawdown 25%, single-trade-loss 10%, leverage all UNCHANGED). They fix
miscalibration, not discipline.

## FIX 1 — The stop-loss that caused 92% of your losses
PROBLEM: 4 of your trades closed at exactly -5% for a combined -$1,501 (92% of all
realized loss). They were SPACEX, oil (BRENTOIL), CL, ETH. Cause: the vol-aware stop
can't fetch daily candles for builder-dex synthetics (xyz:/vntl:), so it silently
fell back to the TIGHT 5% floor — on exactly the MOST volatile coins, which need the
WIDEST stops. Meanwhile your SOL trade (a normal coin that got a proper 5.18% vol
stop) is your big winner.
FIX:
- Builder-dex coins with no vol data now get a 9% fallback stop (was 5%).
- Other coins missing vol data get 6.5% (was 5%).
- Stop cap raised 10% -> 12% (SPACEX/oil routinely swing >5%/day).
EXPECT: far fewer "our_stop" whipsaw closes; the traders' positions get room to work.

## FIX 2 — All 5 wallets were suspended (basket went dark)
PROBLEM: the live circuit breaker benched a wallet at just -2% of its slice. With the
miscalibrated stop bleeding -5% per stopped trade, all 5 wallets tripped it.
FIX:
- Breaker threshold -2% -> -4% (needs a real losing streak, not one stop).
- Min trades before it can fire 5 -> 6.
- Suspension auto-review 7 days -> 3 days (benched wallets return sooner).
EXPECT: wallets stop getting mass-benched; basket stays populated. NOTE: the 5
currently-suspended wallets will auto-review within 3 days and rejoin if healthy.

## FIX 3 — Widen the basket (was stuck at 5 of ~74 scored wallets)
PROBLEM: the basket cap was already 25, but only 5 wallets passed the copy gates.
The two HARDEST filters were excluding good traders for no RISK reason:
- min_realized_pnl_usd $10,000 -> $3,000 (a $4k-profit wallet with a clean curve is
  a valid copy target; the $10k floor only let whales through).
- min_history_days 60 -> 45 (45 days + 30 trades is still a real sample).
All RISK gates unchanged. EXPECT: more wallets qualify over the next re-audit sweep,
giving a bigger, more active, more diversified basket.

## FIX 4 (no code) — The autopsy that "never finished"
It didn't hang — the Render WEB SHELL times out idle connections on long jobs (the
autopsy pulls up to 400 wallets one-by-one, ~15 min). Run it in the BACKGROUND so a
dropped shell can't kill it:

    nohup python -m sf.validation.autopsy --db /var/data/signalforge.db --out autopsy.json > autopsy.log 2>&1 &

Check progress:   tail -20 autopsy.log
Read result:      cat autopsy.json | head -c 4000

This tells us which features separate the wallets that blew up forward from the ones
that survived — the data behind any ban-gate fix. (It changes nothing in production;
it's a research report.)

## DEPLOY
1. GitHub -> HyperPilot_Backend -> delete old `signalforge` folder -> upload this one -> Commit.
2. Render auto-redeploys (~2-3 min).
3. DO NOT touch SF_RESET_LIVE. Leave it. (We are NOT resetting — the 13-day record stays;
   these fixes apply going forward.)
4. Verify /health shows "build": "2026-06-23-volstop-widen-basket".
5. Over the next 1-3 days watch: fewer "our_stop" closes in /live.json, suspended list
   shrinking, eligible count rising above 5.

## HONEST NOTE (unchanged, and it matters)
These fixes make the system bleed less and run wider. They do NOT manufacture an edge.
Your 4th walk-forward still says: predicts SURVIVAL, not PROFIT, regime-dependent. A
better-calibrated stop and a wider basket give the real edge (if any) a fair chance to
show — but if the honest answer stays "survival not profit," the fundable product is
risk-intelligence, not a profit machine. The data leads; we follow it.
