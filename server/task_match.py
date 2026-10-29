"""Link local scheduled-task runs to their task definitions by start time.

Scheduled tasks that run through the desktop app on your own machine leave an
ordinary transcript (entrypoint 'claude-desktop'), so the collector already has
every run. What it lacks is the task name. This module matches each such
session to the task whose cron fires within MATCH_WINDOW minutes before the
session started, and writes a task_runs row for it.

Outcome is inferred: a run with very little output relative to its context is
'nothing_new'; otherwise 'noteworthy'. Runs that reported through report_run.py
keep their explicit outcome (they share the run_id and are not overwritten).

Usage:  python -m server.task_match --db /data/tracker.db [--account work] [--window 20]
Runs automatically at the end of relay_pull.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analyzers.cronutil import _values, parse  # noqa: E402

MATCH_WINDOW_MIN = 20          # session may start this long after the cron minute
NOTHING_NEW_OUTPUT = 1500      # output tokens at or below this = run found nothing to do
DEFAULT_TZ = "America/New_York"


def fires_at(cron: str, when: datetime) -> bool:
    """True if `cron` fires at the minute `when` (already in the cron's timezone)."""
    info = parse(cron)
    if not info.valid:
        return False
    return (when.minute in _values(info.minute, 0, 59) and when.hour in _values(info.hour, 0, 23)
            and when.day in _values(info.dom, 1, 31) and when.month in _values(info.month, 1, 12)
            and when.isoweekday() % 7 in _values(info.dow, 0, 6))


def match_task(tasks: list[sqlite3.Row], started: datetime, window: int) -> sqlite3.Row | None:
    """Most recent task whose cron fired within `window` minutes before `started`."""
    best, best_lag = None, None
    for t in tasks:
        if not t["cron"]:
            continue
        info = parse(t["cron"])
        tz = ZoneInfo(info.tz or DEFAULT_TZ)
        local = started.astimezone(tz).replace(second=0, microsecond=0)
        for lag in range(0, window + 1):
            if fires_at(t["cron"], local - timedelta(minutes=lag)):
                if best_lag is None or lag < best_lag:
                    best, best_lag = t, lag
                break
    return best


def run(db: str, account: str | None, window: int) -> int:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    acct = " AND account = ?" if account else ""
    params = [account] if account else []
    tasks = c.execute(f"SELECT * FROM scheduled_tasks WHERE enabled = 1{acct}", params).fetchall()
    if not tasks:
        return 0
    sessions = c.execute(f"""SELECT * FROM v_session_totals
                             WHERE entrypoint IN ('claude-desktop', 'local-agent') AND calls > 0{acct}
                               AND session_id NOT IN (SELECT substr(run_id, instr(run_id, ':') + 1) FROM task_runs)""",
                         params).fetchall()
    n = 0
    for s in sessions:
        started = datetime.fromisoformat(s["first_ts"].replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        t = match_task([x for x in tasks if x["account"] == s["account"]], started, window)
        if not t:
            continue
        outcome = "nothing_new" if (s["output_tokens"] or 0) <= NOTHING_NEW_OUTPUT else "noteworthy"
        c.execute("""INSERT OR IGNORE INTO task_runs(run_id, task_id, account, started_ts, ended_ts, model, input_tokens,
                     cache_read_tokens, cache_create_tokens, output_tokens, turns, outcome, summary)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (f"{s['account']}:{s['session_id']}", t["task_id"], s["account"], s["first_ts"], s["last_ts"],
                   (s["models"] or "").split(",")[0] or None, s["input_tokens"], s["cache_read_tokens"],
                   s["cache_create_tokens"], s["output_tokens"], s["turns"], outcome,
                   f"matched by start time to '{t['name']}'"))
        n += 1
    c.commit()
    c.close()
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--account")
    ap.add_argument("--window", type=int, default=MATCH_WINDOW_MIN)
    a = ap.parse_args()
    print(f"task_match: {run(a.db, a.account, a.window)} new runs linked")


if __name__ == "__main__":
    main()
