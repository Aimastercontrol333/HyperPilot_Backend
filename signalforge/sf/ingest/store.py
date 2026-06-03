"""
Persistence layer (Phase 1) — SQLite.

Zero-config, single-file, no server to run. Perfect for one always-on worker on
a budget. Two tables:

  discovered_addresses  every wallet the harvester has ever seen on the trade
                        stream, with how often and on which coins (so we can
                        prioritise the most-active wallets to audit first).
  wallet_scores         the latest Safety Score + key metrics per wallet, so the
                        system remembers its audits instead of starting fresh.

WAL mode lets the harvester keep writing while the scoring pipeline reads.
Migrating to Postgres/Timescale later is a drop-in: same function signatures.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

DEFAULT_DB = "signalforge.db"


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS discovered_addresses (
            address     TEXT PRIMARY KEY,
            first_seen  INTEGER NOT NULL,
            last_seen   INTEGER NOT NULL,
            hits        INTEGER NOT NULL DEFAULT 1,
            coins       TEXT NOT NULL DEFAULT '',
            audited_at  INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_addr_hits ON discovered_addresses(hits DESC);

        CREATE TABLE IF NOT EXISTS wallet_scores (
            address     TEXT PRIMARY KEY,
            scored_at   INTEGER NOT NULL,
            score       REAL NOT NULL,
            eligible    INTEGER NOT NULL,
            banned      INTEGER NOT NULL,
            archetype   TEXT,
            n_trades    INTEGER,
            max_dd      REAL,
            expectancy  REAL,
            data_json   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_score ON wallet_scores(score DESC);
        CREATE INDEX IF NOT EXISTS idx_eligible ON wallet_scores(eligible);
        """
    )
    # migration: older DBs (created before audit-tracking) lack audited_at
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(discovered_addresses)")]
    if "audited_at" not in cols:
        conn.execute("ALTER TABLE discovered_addresses ADD COLUMN audited_at INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_addr_audited ON discovered_addresses(audited_at)")
    conn.commit()


@contextmanager
def session(path: str = DEFAULT_DB):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


# ---- discovered addresses --------------------------------------------------
def record_addresses(conn: sqlite3.Connection, addrs: list[str], coin: str,
                     ts: int | None = None) -> int:
    """Upsert a batch of addresses seen on `coin`. Returns count of new addresses."""
    ts = ts or int(time.time() * 1000)
    new = 0
    cur = conn.cursor()
    for a in addrs:
        a = (a or "").lower()
        if not (a.startswith("0x") and len(a) == 42):
            continue
        row = cur.execute("SELECT coins FROM discovered_addresses WHERE address=?", (a,)).fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO discovered_addresses(address,first_seen,last_seen,hits,coins)"
                " VALUES(?,?,?,1,?)", (a, ts, ts, coin))
            new += 1
        else:
            coins = set(filter(None, row["coins"].split(",")))
            coins.add(coin)
            cur.execute(
                "UPDATE discovered_addresses SET last_seen=?, hits=hits+1, coins=? WHERE address=?",
                (ts, ",".join(sorted(coins)), a))
    conn.commit()
    return new


def top_addresses(conn: sqlite3.Connection, limit: int = 500, min_hits: int = 2) -> list[str]:
    """Most-active discovered wallets first — best audit candidates."""
    rows = conn.execute(
        "SELECT address FROM discovered_addresses WHERE hits>=? ORDER BY hits DESC LIMIT ?",
        (min_hits, limit)).fetchall()
    return [r["address"] for r in rows]


# ---- wallet scores ---------------------------------------------------------
def upsert_score(conn: sqlite3.Connection, sr) -> None:
    """sr is a scoring.safety_score.ScoreResult."""
    m = sr.metrics
    conn.execute(
        """INSERT INTO wallet_scores
           (address,scored_at,score,eligible,banned,archetype,n_trades,max_dd,expectancy,data_json)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(address) DO UPDATE SET
             scored_at=excluded.scored_at, score=excluded.score, eligible=excluded.eligible,
             banned=excluded.banned, archetype=excluded.archetype, n_trades=excluded.n_trades,
             max_dd=excluded.max_dd, expectancy=excluded.expectancy, data_json=excluded.data_json
        """,
        (sr.address, int(time.time() * 1000), sr.score, int(sr.eligible), int(sr.banned),
         sr.archetype, m.n_trades if m else None, m.max_drawdown_pct if m else None,
         m.expectancy_pct if m else None,
         json.dumps({"factors": sr.factors, "ban_reasons": sr.ban_reasons,
                     "notes": getattr(sr, "notes", []), "raw_score": sr.raw_score,
                     "venue": "Hyperliquid",
                     "win_rate": m.win_rate if m else None,
                     "avg_lev": m.avg_leverage_proxy if m else None,
                     "leverage_known": (m.extra.get("leverage_known", False) if m else False),
                     "maker_ratio": (m.extra.get("maker_ratio") if m else None),
                     "sharpe": m.sharpe if m else None,
                     "sortino": m.sortino if m else None,
                     "history_days": m.history_days if m else None})),
    )
    conn.commit()


def all_scores(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Cumulative scored wallets, best first (eligible, then by score). Returns
    rows shaped for the dashboard table — so the site shows everything ever
    audited, not just the latest pass."""
    rows = conn.execute(
        "SELECT * FROM wallet_scores ORDER BY eligible DESC, score DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["data_json"] or "{}")
        out.append({
            "wallet": r["address"][:6] + "…" + r["address"][-4:],
            "venue": d.get("venue", "Hyperliquid"), "archetype": r["archetype"],
            "safety": r["score"], "raw_score": d.get("raw_score", r["score"]),
            "eligible": bool(r["eligible"]), "banned": bool(r["banned"]),
            "ban_reasons": d.get("ban_reasons", []), "notes": d.get("notes", []),
            "factors": d.get("factors", {}),
            "avg_lev": round(d["avg_lev"], 1) if d.get("avg_lev") is not None else None,
            "leverage_known": d.get("leverage_known", False),
            "maker_ratio": round(d["maker_ratio"], 2) if d.get("maker_ratio") is not None else None,
            "max_dd": round(r["max_dd"], 1) if r["max_dd"] is not None else None,
            "win_pct": round(d["win_rate"] * 100, 1) if d.get("win_rate") is not None else None,
            "expectancy_pct": round(r["expectancy"], 2) if r["expectancy"] is not None else None,
            "sharpe": round(d["sharpe"], 2) if d.get("sharpe") is not None else None,
            "sortino": round(d["sortino"], 2) if d.get("sortino") is not None else None,
            "n_trades": r["n_trades"],
            "history_days": round(d["history_days"], 0) if d.get("history_days") is not None else None,
        })
    return out


def mark_audited(conn: sqlite3.Connection, addresses: list[str], ts: int | None = None) -> None:
    """Record that we ATTEMPTED to audit these wallets — whether or not they
    scored. Without this, wallets too small to score never enter any table and
    get re-audited every pass, so the rotation never advances. Leaderboard-only
    wallets (not yet harvested) are inserted so they're remembered too."""
    ts = ts or int(time.time() * 1000)
    conn.executemany(
        """INSERT INTO discovered_addresses(address, first_seen, last_seen, hits, coins, audited_at)
           VALUES(?, ?, ?, 0, '', ?)
           ON CONFLICT(address) DO UPDATE SET audited_at=excluded.audited_at""",
        [(a, ts, ts, ts) for a in addresses])
    conn.commit()


def rotate_unscored_first(conn: sqlite3.Connection, candidates: list[str],
                          limit: int, restale_hours: int = 24) -> list[str]:
    """Spread audits across the universe by AUDIT RECENCY (not score status):
    never-audited candidates first, then those audited longest ago (older than
    restale_hours), so every pass sweeps NEW wallets instead of re-trying the
    same low-activity names forever."""
    rows = conn.execute("SELECT address, audited_at FROM discovered_addresses").fetchall()
    audited_at = {r["address"]: (r["audited_at"] or 0) for r in rows}
    now = int(time.time() * 1000)
    stale_cutoff = now - restale_hours * 3600 * 1000
    never, stale, fresh = [], [], []
    for a in candidates:
        t = audited_at.get(a, 0)
        if t == 0:
            never.append(a)
        elif t < stale_cutoff:
            stale.append(a)
        else:
            fresh.append(a)
    stale.sort(key=lambda a: audited_at.get(a, 0))     # oldest audit first
    return (never + stale + fresh)[:limit]


def get_eligible_scores(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM wallet_scores WHERE eligible=1 AND banned=0 ORDER BY score DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def realign_eligibility(conn: sqlite3.Connection, min_trades: int, min_history_days: float) -> int:
    """Demote any stored-eligible wallet that no longer clears the current COPY gate
    (e.g. after a rule change), using the metrics already saved with each score — so
    verdicts reflect today's rules immediately instead of waiting for the slow re-audit
    sweep. Only demotes; promotions still happen through normal re-scoring."""
    rows = conn.execute(
        "SELECT address, n_trades, data_json FROM wallet_scores WHERE eligible=1 AND banned=0"
    ).fetchall()
    demote = []
    for r in rows:
        nt = r["n_trades"] or 0
        try:
            hist = (json.loads(r["data_json"] or "{}")).get("history_days")
        except Exception:  # noqa: BLE001
            hist = None
        if nt < min_trades or (hist is not None and hist < min_history_days):
            demote.append((r["address"],))
    if demote:
        conn.executemany("UPDATE wallet_scores SET eligible=0 WHERE address=?", demote)
        conn.commit()
    return len(demote)


def stats(conn: sqlite3.Connection) -> dict:
    d = conn.execute("SELECT COUNT(*) n FROM discovered_addresses").fetchone()["n"]
    s = conn.execute("SELECT COUNT(*) n FROM wallet_scores").fetchone()["n"]
    e = conn.execute("SELECT COUNT(*) n FROM wallet_scores WHERE eligible=1 AND banned=0").fetchone()["n"]
    b = conn.execute("SELECT COUNT(*) n FROM wallet_scores WHERE banned=1").fetchone()["n"]
    return {"discovered": d, "scored": s, "eligible": e, "banned": b}
