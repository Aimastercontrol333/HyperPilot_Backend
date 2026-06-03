#!/usr/bin/env python3
"""
HyperPilot uptime / freshness monitor.

Liveness + freshness only (selfcheck.py covers data CONSISTENCY). Designed to be
run on a schedule (GitHub Actions / any cron): it exits non-zero when the live
backend is DOWN or STALE, so a failed run turns into an email alert automatically.

It is deliberately tolerant so it does NOT cry wolf:
  * Only UNREACHABLE endpoints or a STALE dashboard (no fresh scoring pass for a
    long time) count as hard FAILs.
  * A single failed scoring pass, an empty basket, or a stale live snapshot are
    WARN only (these happen legitimately during warmup / 0-eligible periods).

Usage:
    python monitor.py https://hyperpilot-backend.onrender.com [max_score_age_min]
"""
import json
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://hyperpilot-backend.onrender.com").rstrip("/")
MAX_SCORE_AGE_MIN = int(sys.argv[2]) if len(sys.argv) > 2 else 60   # scoring runs ~every 10 min
MAX_LIVE_AGE_MIN = 20                                              # live snapshot writes ~every 5 s

fails, warns, notes = [], [], []
now = time.time()


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def age_min(ts, ms=False):
    if not ts:
        return None
    return (now - (ts / 1000.0 if ms else ts)) / 60.0


# 1) /health must be reachable -------------------------------------------------
try:
    h = get("/health")
    notes.append("ok: /health reachable")
except Exception as e:  # noqa: BLE001
    print(f"FAIL: /health unreachable: {e}")
    sys.exit(1)

lr = h.get("last_run", {}) or {}
db = h.get("db", {}) or {}
if (db.get("scored") or 0) <= 0:
    warns.append("DB shows 0 scored wallets (still warming up?)")
else:
    notes.append(f"ok: {db.get('scored')} wallets scored, {db.get('eligible')} eligible, "
                 f"{db.get('discovered')} discovered")
if lr.get("ok") is not True:
    warns.append("most recent scoring pass did not finish ok (transient if dashboard is fresh)")

# 2) /dashboard.json must be reachable AND fresh -------------------------------
try:
    d = get("/dashboard.json")
except Exception as e:  # noqa: BLE001
    print(f"FAIL: /dashboard.json unreachable: {e}")
    sys.exit(1)

gen_age = age_min(d.get("generated_at"))
if gen_age is None:
    fails.append("dashboard has no generated_at timestamp")
elif gen_age > MAX_SCORE_AGE_MIN:
    fails.append(f"dashboard is STALE — last scoring pass {gen_age:.0f} min ago "
                 f"(> {MAX_SCORE_AGE_MIN}); the scorer thread is likely stuck")
else:
    notes.append(f"ok: dashboard fresh ({gen_age:.0f} min old)")

# 3) live engine freshness (WARN only — empty basket legitimately pauses it) ---
live = d.get("live") or {}
if live.get("status") == "STREAMING":
    la = age_min(live.get("updated_at"), ms=True)
    if la is None:
        warns.append("live snapshot has no updated_at")
    elif la > MAX_LIVE_AGE_MIN:
        warns.append(f"live snapshot {la:.0f} min old (live engine paused or stalled)")
    else:
        notes.append(f"ok: live engine streaming ({la:.0f} min old, "
                     f"{live.get('open_count', 0)} open, equity ${live.get('equity')})")
else:
    notes.append("note: live engine not streaming yet (no eligible basket / warming)")

# ---- report ------------------------------------------------------------------
print(f"\nHyperPilot monitor — {BASE}")
print("=" * 60)
for n in notes:
    print(" ", n)
for w in warns:
    print("  WARN:", w)
for f in fails:
    print("  FAIL:", f)
print("=" * 60)
if fails:
    print(f"{len(fails)} FAIL(s), {len(warns)} warning(s) — backend is DOWN or STALE.")
    sys.exit(1)
print(f"backend healthy ({len(warns)} warning(s)).")
