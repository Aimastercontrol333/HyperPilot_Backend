# SignalForge / HyperPilot — Backend (Phase 1)

**The data-ingestion → Safety Score → live paper-trading engine that proves the model.**
Free public Hyperliquid API only. No token, no custody, no KYC. This is the
legally-clean, budget-feasible scope from the project context — and the part that
proves the thesis before any real capital moves.

---

## What this is (and isn't)

This repo is the **brain** of Phase 1: it audits Hyperliquid wallets with the
8-factor Safety Score, selects a basket of disciplined operators, and paper-trades
them through an **honest fill model** (delay, slippage, fees, funding). It emits a
`dashboard.json` the existing website renders instead of the simulated placeholders.

It is **not** the execution engine, the token, the staking contracts, or KYC —
those are Phase 2+ and gated behind a fundraise, an audit, and a legal opinion
(all non-negotiable per the project context).

## The pipeline (mirrors the "How It Works" page)

```
discover universe ─▶ ingest fills/positions/funding ─▶ engineer behavioral features
      │                                                          │
      ▼                                                          ▼
  leaderboard + seeds                              round-trips, drawdown, leverage,
  (API is address-keyed:                           expectancy, cadence, martingale
   no "list all traders")                                       │
                                                                ▼
                                  ┌──────────────  Safety Score (8 factors)  ◀── auto-ban gates
                                  │                 + sample-size shrinkage
                                  ▼
                          select basket ─▶ PAPER SIM (delay/slippage/fee/funding
                                            + our own stop overlay + capacity test)
                                                                │
                                                                ▼
                                                         dashboard.json ─▶ website
```

## Module map

| Module | Job |
|---|---|
| `sf/config.py` | every tunable: endpoints, weights, thresholds, fill-model assumptions |
| `sf/ingest/hyperliquid.py` | rate-limited Info API client: discovery, fills, positions, funding, book, candles |
| `sf/ingest/harvester.py` | **WebSocket trade-stream → address harvester** (discovery widener, self-healing) |
| `sf/ingest/store.py` | **SQLite persistence**: discovered addresses + remembered Safety Scores |
| `sf/features/metrics.py` | fills → round-trips → behavioral metrics |
| `sf/scoring/safety_score.py` | 8-factor score, auto-ban gates, empirical-Bayes shrinkage, archetype |
| `sf/sim/fill_model.py` | the honest cost of being a follower (the credibility core) |
| `sf/sim/paper_trader.py` | mirror basket round-trips, own risk overlay, capacity curve (replay/proof) |
| `sf/sim/live_copy.py` | **live copy paper-trading**: mirrors the basket in real time, marks to market, streams open/closed positions + equity |
| `sf/validation/walkforward.py` | **the make-or-break test** + measured delay-drift |
| `sf/pipeline.py` | orchestrates everything → `dashboard.json` (reads harvested wallets from DB) |
| `sf/worker.py` | **always-on worker**: harvester + periodic scorer + serves `/dashboard.json` |
| `smoke_test.py` | end-to-end proof with synthetic fills (no API needed) |

## Run it

```bash
pip install -r requirements.txt

# 1) Prove the engine works offline (no network):
python smoke_test.py

# 2) Run against the LIVE Hyperliquid API (seed with wallets you want to watch;
#    --max caps how many you audit while testing rate limits):
python -m sf.pipeline --seeds 0xWALLET1,0xWALLET2 --max 50 --out dashboard.json

# 3) Point the website at dashboard.json (replace the simulated placeholders).
```

> **Network note:** `api.hyperliquid.xyz` must be reachable. The smoke test runs
> fully offline so you can validate logic on a laptop / in CI before wiring the API.

## The two things that decide whether the numbers are real

1. **`delay_drift_bps` must be measured, not assumed.** Run
   `validation.walkforward.measure_delay_drift(client, fills)` on real entries and
   replace the placeholder in `config.FILL`. This is the true cost of being late,
   and it's the term that quietly kills most copy systems.
2. **Run the walk-forward test before going public.** `walk_forward()` scores
   wallets on a training window and measures their *forward* net return on the
   next window, bucketed by score decile. If the top decile doesn't beat the
   bottom decile forward (`verdict: predictive`), the Safety Score isn't
   predictive yet and the dashboard would just be broadcasting noise. Fix the
   model first. With only a handful of wallets the deciles are degenerate; this
   becomes meaningful at hundreds of audited wallets.

---

## Head-developer build plan (phase by phase)

### Phase 1 — Prove the model  *(this repo + the live site)*
- [x] Hyperliquid ingestion client (free Info API)
- [x] Round-trip reconstruction + behavioral metrics
- [x] 8-factor Safety Score with auto-ban gates + sample-size shrinkage
- [x] Honest fill model (delay / slippage / fees / funding) + own-stop overlay
- [x] Replay paper-trading engine + capacity curve (historical proof)
- [x] **Live copy paper-trading engine** (`live_copy.py`) — mirrors the basket in
      real time via `userFills`+`allMids`, marks to market, fires our own stop, and
      streams open/closed positions + live equity to `/dashboard.json` (`live` section)
- [x] Walk-forward validation harness + delay-drift measurement
- [x] `dashboard.json` emitter for the existing site
- [x] **WebSocket discovery widener** (`harvester.py`) — addresses from the live trade stream
- [x] **Persistence layer** (`store.py`) — remembers discovered wallets + scores (SQLite)
- [x] **Always-on worker** (`worker.py`) — harvester + scorer + serves `/dashboard.json`
- [ ] **Next:** wire the website to fetch `/dashboard.json`; run the walk-forward on
      300+ real wallets and publish only if `verdict: predictive`; (later) migrate
      SQLite → Postgres/Timescale when one box isn't enough.

**Two ways to run Phase 1:**
- **Quick/free:** GitHub Actions runs `sf.pipeline` every 30 min, commits
  `dashboard.json` to the site repo (see `DEPLOY.md` Part 2). Discovery = leaderboard.
- **Full discovery:** run `sf.worker` as one always-on service. The harvester
  continuously widens the universe from the trade stream; the scorer re-runs on a
  timer; the site fetches `/dashboard.json` from the worker (see `DEPLOY.md` Part 3).

**Infra for Phase 1 (cheap):** one small always-on worker (Railway/Fly/a $5–10
VPS) running the pipeline on a schedule + a tiny Postgres/Timescale. Vercel keeps
serving the static site, now reading real `dashboard.json`. Founder's 2015 Mac is
fine for dev via Claude Code; the worker runs in the cloud.

### Phase 2 — Build for real capital  *(after fundraise; ~$50–150k)*
- Non-custodial execution engine (Rust/Go), sub-300ms on Hyperliquid, delegated
  trade-only permissions, **never holds principal**
- Token (ERC-20 on Arbitrum) + staking contract with tiered lockups
- KYC/AML (Sumsub or Persona) + geo-blocking (IP + KYC enforced)
- **Independent smart-contract audit — committed, non-negotiable**
- Private beta: 50–200 invited users, small caps
- BVI entity + finalized legal opinion **before** any token/custody goes live

### Phase 3 — Public launch
- KYC'd, geo-blocked presale · DEX listing (Arbitrum)
- 3-level, fee-funded, stake-gated referral program activation
- Insurance fund seeded **before** real money opens · production cap scale-up

### Phase 4 — Scale & decentralize
- Additional on-chain perp DEXs (GMX, dYdX, Vertex, Drift) as verified enrichment
- Binance leaderboard as *lower-confidence* enrichment only (no official API;
  pseudonymous — never anchors a basket pick)
- Mobile (React Native) · DAO governance

---

## Guardrails baked into the code

- All outputs carry `"Research preview · simulated · not financial advice."`
- The product optimizes for **positive expectancy + bounded loss**, never a
  win-rate promise. `expectancy_pct` is the headline metric, not win rate.
- Capacity-aware numbers are first-class: we publish returns at $10k / $100k / $1M
  so the track record can't be quietly inflated by ignoring size.
- Our **own** stop-loss / leverage caps are enforced independently of the trader —
  imposing discipline even when a basket member slips is part of the value prop.

*Update the project context document whenever a decision here changes.*
