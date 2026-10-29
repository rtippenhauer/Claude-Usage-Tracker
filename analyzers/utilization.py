"""Utilization: under- and over-use per account, plus a cross-account comparison.

Checks (window = WINDOW_DAYS)
  * Under-utilization: weekdays with zero Claude Code usage while an enabled
    scheduled task exists for the account (the subscription is paying for the
    task slot but the account itself is idle), and meter pct_used that never
    exceeds LOW_METER_PCT in the window.
  * Over-utilization: meter pct_used reaching >= HIGH_METER_PCT, and days whose
    context tokens exceed SPIKE_FACTOR x the account's median day.
  * Cross-account: one finding (account='shared') describing the imbalance in
    context/output tokens and meter use between accounts.
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone

from .common import Finding, accounts_in, rows

NAME = "utilization"
WINDOW_DAYS = 30
LOW_METER_PCT = 30.0
HIGH_METER_PCT = 100.0
SPIKE_FACTOR = 3.0
IMBALANCE_RATIO = 3.0


def _daily(conn: sqlite3.Connection, account: str, start: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows(conn, """SELECT day, SUM(context_tokens) ctx, SUM(output_tokens) outp, SUM(calls) calls
                           FROM v_daily WHERE account=? AND day>=? GROUP BY day""", [account, start]):
        out[r["day"]] = {"ctx": r["ctx"] or 0, "out": r["outp"] or 0, "calls": r["calls"] or 0}
    return out


def _meters(conn: sqlite3.Connection, account: str, start: str) -> list[dict]:
    ms = rows(conn, """SELECT date, surface, value FROM meter_snapshots WHERE account=? AND metric='pct_used'
                       AND date>=? ORDER BY date""", [account, start])
    out = []
    for m in ms:
        try:
            out.append({"date": m["date"], "surface": m["surface"], "pct": float(m["value"])})
        except (TypeError, ValueError):
            continue
    return out


def _account_findings(conn: sqlite3.Connection, account: str, today: date) -> tuple[list[Finding], dict]:
    start = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    daily = _daily(conn, account, start)
    meters = _meters(conn, account, start)
    tasks = rows(conn, "SELECT COUNT(*) n FROM scheduled_tasks WHERE account=? AND enabled=1", [account])[0]["n"]
    out: list[Finding] = []

    days = [today - timedelta(days=i) for i in range(1, WINDOW_DAYS + 1)]
    weekdays = [d for d in days if d.weekday() < 5]
    idle = [d.isoformat() for d in weekdays if daily.get(d.isoformat(), {}).get("calls", 0) == 0]
    # Only count idle days inside the span where data exists at all (avoid flagging before collection started).
    first_day = min(daily) if daily else None
    if first_day:
        idle = [d for d in idle if d >= first_day]
    ctx_total = sum(v["ctx"] for v in daily.values())
    out_total = sum(v["out"] for v in daily.values())

    if tasks and len(idle) >= 3:
        out.append(Finding(
            account=account, analyzer=NAME, severity="low" if len(idle) < 8 else "medium",
            title=f"{len(idle)} idle weekdays while {tasks} scheduled task(s) keep running",
            fingerprint=f"{NAME}:idle_weekdays:{account}",
            detail=(f"No Claude Code calls on {len(idle)} of {len(weekdays)} weekdays in the last {WINDOW_DAYS} days "
                    f"(e.g. {', '.join(idle[:5])}), but {tasks} enabled scheduled task(s) consume the account's "
                    f"budget on those days. Either the interactive work moved to the other account or the tasks are "
                    f"running for nobody."),
            evidence={"idle_weekdays": idle, "scheduled_tasks": tasks, "weekdays_in_window": len(weekdays)},
            est_tokens_wasted=None,
            recommendation=("Check whether the tasks' output is actually read on idle days; if not, run them on "
                            "weekdays only (`* * 1-5`) or move them to the account that is active."),
        ))

    by_surface: dict[str, list[dict]] = {}
    for m in meters:
        by_surface.setdefault(m["surface"], []).append(m)
    for surface, ms in by_surface.items():
        peak = max(m["pct"] for m in ms)
        if len(ms) >= 5 and peak < LOW_METER_PCT:
            out.append(Finding(
                account=account, analyzer=NAME, severity="info",
                title=f"{surface} meter never exceeded {peak:.0f}% in {len(ms)} readings",
                fingerprint=f"{NAME}:meter_low:{account}:{surface}",
                detail=(f"Peak pct_used for `{surface}` was {peak:.0f}% across {len(ms)} snapshots since {ms[0]['date']}. "
                        f"Headroom is going unused on this account."),
                evidence={"surface": surface, "peak_pct": peak, "readings": len(ms)},
                est_tokens_wasted=None,
                recommendation=("Shift bursty work (scheduled tasks, big refactors) from the saturated account to "
                                "this one, or run bigger-model sessions here where the limit is not binding."),
            ))
        hits = [m["date"] for m in ms if m["pct"] >= HIGH_METER_PCT]
        if hits:
            out.append(Finding(
                account=account, analyzer=NAME, severity="high" if len(hits) >= 3 else "medium",
                title=f"{surface} meter hit {HIGH_METER_PCT:.0f}% on {len(hits)} day(s)",
                fingerprint=f"{NAME}:meter_max:{account}:{surface}",
                detail=(f"`{surface}` reached its limit on {', '.join(hits[:6])}. Work on those days was blocked or "
                        f"spilled into waiting for the reset."),
                evidence={"surface": surface, "dates": hits, "readings": len(ms)},
                est_tokens_wasted=None,
                recommendation=("Move routine work (subagents, scheduled tasks, slash commands) to cheaper models, "
                                "and spread long sessions so they do not all land inside one reset window; see the "
                                "cross-account finding for rebalancing."),
            ))

    vals = [v["ctx"] for v in daily.values() if v["ctx"] > 0]
    if len(vals) >= 7:
        med = statistics.median(vals)
        spikes = sorted(((d, v["ctx"]) for d, v in daily.items() if med and v["ctx"] > SPIKE_FACTOR * med),
                        key=lambda t: -t[1])
        if spikes:
            out.append(Finding(
                account=account, analyzer=NAME, severity="medium" if len(spikes) >= 3 else "low",
                title=f"{len(spikes)} day(s) spiked above {SPIKE_FACTOR:.0f}x the median ({med:,.0f} ctx tokens/day)",
                fingerprint=f"{NAME}:spikes:{account}",
                detail=("Spike days: " + ", ".join(f"{d} ({v:,})" for d, v in spikes[:5]) +
                        ". Spikes usually come from one or two sessions with huge context; see "
                        "`investigate top-sessions --by context`."),
                evidence={"median_ctx": med, "spikes": spikes[:10]},
                est_tokens_wasted=int(sum(v - SPIKE_FACTOR * med for _, v in spikes) * 0.3),
                recommendation=("Break the spiking sessions up (new session per task), keep large tool outputs out "
                                "of context, and check cache_efficiency findings for those days."),
            ))

    summary = {"ctx": ctx_total, "out": out_total, "active_days": len(vals), "tasks": tasks,
               "meter_peak": max((m["pct"] for m in meters), default=None),
               "meter_avg": round(statistics.mean(m["pct"] for m in meters), 1) if meters else None}
    return out, summary


def _compare(summaries: dict[str, dict]) -> Finding | None:
    if len(summaries) < 2:
        return None
    (a, sa), (b, sb) = sorted(summaries.items())[:2]
    big, small = (a, b) if sa["ctx"] >= sb["ctx"] else (b, a)
    sbig, ssmall = summaries[big], summaries[small]
    ratio = (sbig["ctx"] / ssmall["ctx"]) if ssmall["ctx"] else float("inf")
    lines = [f"| | {a} | {b} |", "|---|---|---|",
             f"| context tokens | {sa['ctx']:,} | {sb['ctx']:,} |",
             f"| output tokens | {sa['out']:,} | {sb['out']:,} |",
             f"| active days | {sa['active_days']} | {sb['active_days']} |",
             f"| scheduled tasks | {sa['tasks']} | {sb['tasks']} |",
             f"| meter peak % | {sa['meter_peak'] if sa['meter_peak'] is not None else '-'} | "
             f"{sb['meter_peak'] if sb['meter_peak'] is not None else '-'} |"]
    imbalanced = ratio >= IMBALANCE_RATIO or (
        (sbig["meter_peak"] or 0) >= HIGH_METER_PCT and (ssmall["meter_peak"] or 0) < LOW_METER_PCT * 2)
    sev = "medium" if imbalanced else "info"
    ratio_txt = f"{ratio:.1f}x" if ratio != float("inf") else "all"
    rec = (f"`{big}` carries {ratio_txt} the context volume of `{small}`. Move account-agnostic work "
           f"(scheduled tasks, personal tooling, experiments) to `{small}` so both limits are used."
           if imbalanced else "Usage is reasonably balanced; no rebalancing needed.")
    return Finding(
        account="shared", analyzer=NAME, severity=sev,
        title=f"Cross-account: {big} uses {ratio:.1f}x the context tokens of {small}" if ratio != float("inf")
        else f"Cross-account: {small} has no usage in the window",
        fingerprint=f"{NAME}:compare:{a}:{b}",
        detail="\n".join(lines),
        evidence={"accounts": summaries, "ratio": None if ratio == float("inf") else round(ratio, 2)},
        est_tokens_wasted=None,
        recommendation=rec,
    )


def run(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    today = datetime.now(timezone.utc).date()
    out: list[Finding] = []
    summaries: dict[str, dict] = {}
    for acct in accounts_in(conn, None):   # compare needs both accounts
        fs, summary = _account_findings(conn, acct, today)
        summaries[acct] = summary
        if not account or account == "all" or account == acct:
            out.extend(fs)
    cmp = _compare(summaries)
    if cmp:
        out.append(cmp)
    return out
