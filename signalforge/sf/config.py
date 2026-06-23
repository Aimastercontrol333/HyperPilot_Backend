"""
SignalForge — central configuration.

Everything tunable lives here so the rest of the codebase stays declarative.
Phase 1 uses ONLY the free public Hyperliquid API. No node, no paid plan,
no custody, no token. This is the legally-clean "prove the model" scope.
"""
from __future__ import annotations

# ----------------------------------------------------------------------------
# Venues / endpoints  (Phase 1 = Hyperliquid only; others are stubs for Phase 3)
# ----------------------------------------------------------------------------
HL_API = "https://api.hyperliquid.xyz/info"          # POST {type: ...}
HL_WS = "wss://api.hyperliquid.xyz/ws"               # realtime subscriptions
# Public leaderboard snapshot (universe discovery). URL can change; keep override-able.
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# Polite client behaviour. HL info endpoint is weight-limited (~1200 weight/min/IP).
REQUEST_TIMEOUT_S = 20
RATE_LIMIT_PER_MIN = 600      # stay well under the cap; one request ~= 20 weight
MAX_RETRIES = 4
RETRY_BACKOFF_S = 1.5

# ----------------------------------------------------------------------------
# Audit universe / sampling
# ----------------------------------------------------------------------------
MIN_HISTORY_DAYS = 30         # auto-pass needs >= this much history (30d still shows consistency; shrinkage + wallet_trust down-weight young wallets)
PREFERRED_TRADES = 200        # confidence ramps toward this many closed trades
MIN_HOLD_MINUTES = 3.0   # anti-scalp: exclude avg hold < 3 min (latency bots, uncopyable)
MIN_TRADES = 15               # below this we don't even score (pure noise); shrinkage (K) heavily discounts small samples so thin wallets can't post inflated scores
LOOKBACK_DAYS = 120           # how far back we pull fills when auditing a wallet

# ----------------------------------------------------------------------------
# Safety Score — 8 weighted factors (must sum to 1.0)
# ----------------------------------------------------------------------------
WEIGHTS = {
    "drawdown_control": 0.20,
    "stoploss_discipline": 0.15,
    "leverage_discipline": 0.15,
    "pnl_consistency": 0.15,
    "risk_adjusted_return": 0.10,
    "frequency_stability": 0.10,
    "anti_martingale": 0.10,
    "wallet_trust": 0.05,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Safety Score weights must sum to 1.0"

# Auto-pass thresholds (a wallet must clear ALL to be basket-eligible)
AUTO_PASS = {
    "max_single_trade_loss_pct": 10.0,
    "max_drawdown_pct": 25.0,            # KEPT strict (the proposed 40-60% loosening was rejected) — this is a RISK gate, never loosen
    "avg_leverage": 10.0,                # RISK gate — keep
    "min_sharpe": 1.5,
    "min_trades": 30,                    # COPY gate: scored at 15, copied only with a real sample
    "min_history_days": 45,              # WIDENED 60 -> 45: was excluding solid 45-60d wallets; 45d + 30 trades is still a real sample. Lets more wallets into the basket (it was stuck at 5 of ~74 scored).
    "min_expectancy_pct": 0.1,           # COPY gate floor: avg round-trip must clear ~0.1%
    "cost_margin_bps": 5.0,              # ...and must beat OUR estimated copy cost by this margin (cost-aware gate)
    # --- proposed-rules merge: size / recency / not-one-lucky-trade. A check is SKIPPED when its value is unknown. ---
    "min_balance_usd": 5000.0,           # skin in the game (applied only when the account could be measured)
    "min_realized_pnl_usd": 3000.0,      # WIDENED 10000 -> 3000: the $10k floor was the single hardest filter, excluding proven-but-smaller disciplined traders for no risk reason (a $4k-profit wallet with a clean curve is still a valid copy target). Proven profit still required, just not gatekept to whales.
    "max_days_since_trade": 7.0,         # must be currently active
    "max_single_trade_dominance": 0.50,  # reject if one trade is >50% of gross profit (lucky-gambler guard)
    "min_equity_curve_quality": 0.45,    # basket wallet must show a healthy, steady, upward curve (0..1; tunable). Captures "steady upward equity" WITHOUT a win-rate floor.
    # NOTE: a win-rate floor was deliberately NOT added -- our own data shows the bleeders are 80-97% win rate.
}

# Auto-ban triggers (ANY one bans the wallet regardless of weighted score)
AUTO_BAN = {
    "max_leverage_ever": 40.0,    # used 40x+ consistently
    "max_liquidations": 0,        # any forced liquidation in window = ban
    # martingale / no-stop-loss / wash-bot detected -> handled in scoring logic
}

# Empirical-Bayes shrinkage: small samples get pulled toward the population mean.
# effective_score = (n/(n+K))*raw + (K/(n+K))*prior_mean
BUILD_VERSION = "2026-06-23-volstop-widen-basket"  # bump on each shipped build so /health proves what is actually running
LIVE_BREAKER_LOSS_PCT = 4.0   # live circuit breaker: suspend a basket wallet once its copied P&L falls below this % of its allotted slice. Widened 2->4: at 2% a single vol-aware stop (~5-9%) on one slice could trip it, which benched ALL FIVE wallets at once. 4% needs a genuine losing streak, not one stop.
LIVE_BREAKER_MIN_TRADES = 6   # ...but only after this many live trades, so a couple unlucky early trades cannot trip it (was 5)
SUSPENSION_REVIEW_DAYS = 3.0  # auto-reconsider a circuit-breaker suspension after this many days (was 7). Faster review so a wallet that hit a rough patch returns to the basket sooner instead of leaving it dark.

# ----------------------------------------------------------------------------
# Data-integrity / staleness guard (no-fake-data rule, enforced in the engine)
# ----------------------------------------------------------------------------
# Observed in production: builder-dex coins (xyz:GOLD etc.) never appear in the
# main allMids feed, so their paper positions sat frozen at entry for 6+ days —
# unmarkable capital with a stop that can never fire. Separately, the mids cache
# retained the LAST price forever, so a dead feed would silently keep computing
# PnL on stale prices. Both violate the no-fake-data rule.
STALE_MARK_MAX_S = 900        # a mark older than this is untrusted: position flagged stale, PnL frozen, stop/TP disabled until fresh data
ALLOW_UNPRICED_OPENS = False  # NEVER open a paper position in a coin we cannot currently mark (no live mid = no copy)
GLOBAL_KILL_DRAWDOWN_PCT = 15.0  # portfolio kill switch: halt ALL copying + close everything if equity falls 15% from start
TARGET_WALLET_DD_PCT = 50.0      # stop copying a wallet whose OWN account falls >50% from its peak while we follow it
SHRINKAGE_K = 25              # was 60; the walk-forward showed the RAW score separates survivors at predictive grade while the heavily-shrunk score did not — 60 was over-compressing real signal. The 30-trade/30-day COPY gate independently protects the basket from thin samples, so a lighter K is safe.
PRIOR_MEAN = 50.0            # neutral prior on the 0-100 scale

# ----------------------------------------------------------------------------
# Paper-trading FILL MODEL  (this is where credibility is won or lost)
# ----------------------------------------------------------------------------
# You never get the trader's price. You get the market AFTER your detect+decide
# delay, crossed by the spread, plus size-based slippage, plus the drift that
# happened while you were late. delay_drift should ultimately be MEASURED from
# replay; these are conservative starting defaults.
FILL = {
    # Production co-located engine (Phase-2 target: Singapore, next to Hyperliquid).
    "detection_latency_s": 0.2,   # WS fill -> our ingestion on a low-latency box
    "decision_latency_s": 0.1,    # normalize + size + route  (≈300ms total)
    "half_spread_bps": {          # FALLBACK only — the live engine measures the real book
        "major": 1.5,             # BTC, ETH
        "mid": 6.0,               # SOL, major alts
        "thin": 20.0,             # long-tail alts
    },
    "impact_bps_per_100k": {     # slippage in bps when trading $100k notional,
        "major": 4.0,            # scaled by sqrt(notional/100k); realistic clips
        "mid": 12.0,
        "thin": 40.0,
    },
    # Adverse price drift while we are late, charged on entry. Now DERIVED from latency:
    # drift = drift_bps_per_s[tier] * total_latency_s. At 0.3s this is ~1-4 bps (vs ~8 bps
    # at the old 2.5s). Per-tier because thin coins move more per second. Measure to refine.
    "drift_bps_per_s": {"major": 3.0, "mid": 6.0, "thin": 12.0},
    "taker_fee_bps": 4.5,         # per side; HL base perp taker = 0.045% (verified, entry tier <$5M/14d)
}

# Funding settles HOURLY on Hyperliquid. The live engine pulls each coin's real recent
# hourly rate; this is only the fallback when history is missing (≈ the old 1 bp / 8h).
FUNDING_FALLBACK_BPS_PER_HOUR = 0.125

# ----------------------------------------------------------------------------
# Portfolio construction for the paper basket
# ----------------------------------------------------------------------------
PORTFOLIO = {
    "start_equity_usd": 100_000.0,
    "basket_size": 25,            # top-N eligible wallets to mirror
    "weighting": "safety_score",  # equal | safety_score | inverse_vol
    "max_weight_per_trader": 0.10,
    "max_weight_per_asset": 0.15,   # tightened 0.25->0.15: cap any single COIN's share of the book (HYPE-pileup fix), enforced live
    "max_portfolio_leverage": 3.0,
    "own_stop_loss_pct": 5.0,     # FLOOR for the per-position stop (see stop_vol_mult below)
    "take_profit_pct": 15.0,      # take-profit: close a copied position once net gain >= 15% (caps upside, locks gains; tunable)
    # Volatility-aware stop: a flat 5% on a coin that moves 5% in a normal day is a
    # coin-flip, not risk control (12 of the first 21 live closes were -5% whipsaw
    # stops on volatile alts whose traders went on to manage the position fine).
    # Per-position stop = clamp(stop_vol_mult x coin's avg daily move, own_stop_loss_pct .. stop_cap_pct).
    # Falls back to a vol-class default when no candle data (see below). Set stop_vol_mult to 0 to disable.
    "stop_vol_mult": 1.5,
    "stop_cap_pct": 12.0,         # widened 10->12: builder-dex equities/commodities (SPACEX, oil) routinely swing >5%/day
    # FALLBACK stop when a coin has no daily-candle vol data. The old code fell back to
    # the 5% FLOOR, which is exactly wrong: the coins missing candle data are the
    # builder-dex synthetics (xyz:/vntl: — stocks, oil, gold, pre-IPO) that are the MOST
    # volatile, and a tight 5% stop on them caused ~92% of realized losses (4 trades,
    # -$1.5k, all flat -5% whipsaws). Give un-pricable-vol coins a sensible WIDE default
    # instead of the tight floor.
    "stop_fallback_builder_pct": 9.0,   # default stop for xyz:/vntl: coins with no candle vol
    "stop_fallback_other_pct": 6.5,     # default stop for any other coin missing vol data
    "builder_dex_prefixes": ("xyz:", "vntl:"),
    # When a wallet drops out of the eligible basket while we still hold its positions:
    #   "ride"         = keep positions until stop/TP/trader-close (old implicit behavior)
    #   "close"        = close its positions at the next mark
    #   "tighten_stop" = keep them but halve the remaining stop room (default: de-risk, don't panic-exit)
    "on_delist": "tighten_stop",
    "delist_stop_mult": 0.5,
    "capacity_test_sizes": [10_000, 100_000, 1_000_000],
}

# Liquidity tiering for spread/impact (extend as coverage grows)
MAJOR_COINS = {"BTC", "ETH"}
MID_COINS = {"SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK", "ARB", "OP", "SUI", "HYPE"}


def coin_tier(coin: str) -> str:
    c = coin.upper()
    if c in MAJOR_COINS:
        return "major"
    if c in MID_COINS:
        return "mid"
    return "thin"
