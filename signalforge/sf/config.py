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
    "max_drawdown_pct": 25.0,
    "avg_leverage": 10.0,
    "min_sharpe": 1.5,
    "min_trades": 30,            # COPY gate: scored at 15, but only copied with a real sample
    "min_history_days": 30,      # COPY gate: 30 days of live history before capital follows
    "min_expectancy_pct": 0.1,   # COPY gate: avg round-trip must clear ~0.1% (a buffer for follower costs) — keeps breakeven high-frequency flippers out of the basket
}

# Auto-ban triggers (ANY one bans the wallet regardless of weighted score)
AUTO_BAN = {
    "max_leverage_ever": 40.0,    # used 40x+ consistently
    "max_liquidations": 0,        # any forced liquidation in window = ban
    # martingale / no-stop-loss / wash-bot detected -> handled in scoring logic
}

# Empirical-Bayes shrinkage: small samples get pulled toward the population mean.
# effective_score = (n/(n+K))*raw + (K/(n+K))*prior_mean
BUILD_VERSION = "2026-06-04-stream-k25-gate"  # bump on each shipped build so /health proves what is actually running
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
    "max_weight_per_asset": 0.25,
    "max_portfolio_leverage": 3.0,
    "own_stop_loss_pct": 12.0,    # OUR discipline overlay, independent of the trader
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
