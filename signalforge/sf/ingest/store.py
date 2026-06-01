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
            coins       TEXT NOT NULL DEFAULT ''
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
                     "win_rate": m.win_rate if m else None,
                     "avg_lev": m.avg_leverage_proxy if m else None})),
    )
    conn.commit()


def get_eligible_scores(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM wallet_scores WHERE eligible=1 AND banned=0 ORDER BY score DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    d = conn.execute("SELECT COUNT(*) n FROM discovered_addresses").fetchone()["n"]
    s = conn.execute("SELECT COUNT(*) n FROM wallet_scores").fetchone()["n"]
    e = conn.execute("SELECT COUNT(*) n FROM wallet_scores WHERE eligible=1 AND banned=0").fetchone()["n"]
    return {"discovered": d, "scored": s, "eligible": e}
