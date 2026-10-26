"""Scheduled-task review (scheduled_tasks + task_runs).

Checks
  * Runs mostly end with outcome 'nothing_new' (>= NOTHING_NEW_SHARE of
    MIN_RUNS+ runs): recommend a longer interval and give a concrete cron.
  * Opus-class model with small average output (< SMALL_OUTPUT): recommend a
    cheaper model.
  * Cron fires on :00 or :30: suggest a jittered minute.
  * Near-identical prompts across tasks (difflib ratio > NEAR_RATIO):
    recommend consolidation.
  * Prompts that do not state a model or scope: info finding.
"""
from __future__ import annotations

import difflib
import sqlite3

from . import cronutil
from .common import EXPENSIVE_FAMILIES, Finding, acct_clause, model_family, relative_cost, rows
from .textutil import normalize

NAME = "scheduled_tasks"
MIN_RUNS = 4
NOTHING_NEW_SHARE = 0.7
SMALL_OUTPUT = 800
NEAR_RATIO = 0.8
SCOPE_WORDS = ("only", "limit", "scope", "just", "last", "since", "up to", "at most", "no more than", "skip", "ignore")
MODEL_WORDS = ("haiku", "sonnet", "opus", "model")


def _stats(conn: sqlite3.Connection, task_id: str) -> dict:
    r = rows(conn, """SELECT COUNT(*) runs, SUM(outcome='nothing_new') nothing_new, SUM(outcome='error') errors,
                      SUM(outcome='noteworthy') noteworthy, AVG(output_tokens) avg_out,
                      SUM(output_tokens) out_tokens,
                      SUM(input_tokens+cache_read_tokens+cache_create_tokens) ctx_tokens,
                      AVG(input_tokens+cache_read_tokens+cache_create_tokens) avg_ctx,
                      MAX(started_ts) last_run FROM task_runs WHERE task_id=?""", [task_id])[0]
    return {k: (v or 0) if k != "last_run" else v for k, v in r.items()}


def run(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    w, p = acct_clause(account)
    tasks = rows(conn, f"SELECT * FROM scheduled_tasks WHERE enabled=1{w} ORDER BY account, name", p)
    out: list[Finding] = []
    for t in tasks:
        st = _stats(conn, t["task_id"])
        info = cronutil.parse(t["cron"])
        label = t["name"] or t["task_id"]
        fam = model_family(t["model"])

        # 1. mostly nothing_new
        if st["runs"] >= MIN_RUNS and st["nothing_new"] / st["runs"] >= NOTHING_NEW_SHARE:
            share = st["nothing_new"] / st["runs"]
            suggested = cronutil.lengthen(info)
            wasted = int(st["ctx_tokens"] * share * 0.5)   # half of the empty runs could be skipped
            out.append(Finding(
                account=t["account"], analyzer=NAME, severity="high" if wasted > 500_000 else "medium",
                title=f"Task '{label}' found nothing new in {share:.0%} of {st['runs']} runs",
                fingerprint=f"{NAME}:nothing_new:{t['task_id']}",
                detail=(f"`{label}` runs `{t['cron']}` ({info.description}) on {t['model'] or '?'}; {st['nothing_new']} "
                        f"of {st['runs']} runs ended 'nothing_new' and consumed {st['ctx_tokens']:,} context tokens in "
                        f"total (avg {st['avg_ctx']:,.0f} per run)."),
                evidence={"task_id": t["task_id"], "cron": t["cron"], "interval_min": info.interval_minutes,
                          "runs": st["runs"], "nothing_new": st["nothing_new"], "ctx_tokens": st["ctx_tokens"],
                          "suggested_cron": suggested},
                est_tokens_wasted=wasted,
                recommendation=(f"Lengthen the interval: `{suggested}`. Alternatively add a cheap pre-check (e.g. "
                                f"compare a hash or timestamp first) so the run exits before loading context."),
            ))

        # 2. expensive model, small output
        if fam in EXPENSIVE_FAMILIES and st["runs"] >= 2 and st["avg_out"] < SMALL_OUTPUT:
            factor = relative_cost(fam) / relative_cost("sonnet")
            excess = int(st["ctx_tokens"] * (1 - 1 / factor))
            out.append(Finding(
                account=t["account"], analyzer=NAME, severity="medium" if excess > 100_000 else "low",
                title=f"Task '{label}' runs on {fam} but averages {st['avg_out']:.0f} output tokens",
                fingerprint=f"{NAME}:model:{t['task_id']}",
                detail=(f"`{label}` is configured for `{t['model']}` and produced on average {st['avg_out']:.0f} output "
                        f"tokens over {st['runs']} runs ({st['ctx_tokens']:,} context tokens total). Short, routine "
                        f"outputs rarely need an opus-class model."),
                evidence={"task_id": t["task_id"], "model": t["model"], "avg_output": round(st["avg_out"]),
                          "runs": st["runs"], "ctx_tokens": st["ctx_tokens"]},
                est_tokens_wasted=excess,
                recommendation=("Switch the task to sonnet (or haiku if it is a check/notify job) and keep opus for "
                                "tasks that write code or long analyses."),
            ))

        # 3. round-minute firing
        if info.fires_on_round_minute:
            out.append(Finding(
                account=t["account"], analyzer=NAME, severity="low",
                title=f"Task '{label}' fires on a round minute ({t['cron']})",
                fingerprint=f"{NAME}:jitter:{t['task_id']}",
                detail=("Runs scheduled at :00/:30 compete with everyone else's and can be delayed. A jittered "
                        "minute avoids the contended slot."),
                evidence={"task_id": t["task_id"], "cron": t["cron"], "suggested_cron": cronutil.with_jitter(info, label)},
                est_tokens_wasted=0,
                recommendation=f"Use `{cronutil.with_jitter(info, label)}` instead.",
            ))

        # 4. prompt lacks model/scope
        prompt = (t["prompt"] or "").lower()
        missing = []
        if not any(wd in prompt for wd in MODEL_WORDS) and not t["model"]:
            missing.append("model")
        if not any(wd in prompt for wd in SCOPE_WORDS):
            missing.append("scope/limits")
        if missing and prompt:
            out.append(Finding(
                account=t["account"], analyzer=NAME, severity="info",
                title=f"Task '{label}' prompt does not state {' or '.join(missing)}",
                fingerprint=f"{NAME}:prompt:{t['task_id']}",
                detail=(f"The prompt ({len(prompt)} chars) gives no explicit {' or '.join(missing)}. Unbounded "
                        f"prompts tend to read more files and run more turns than needed."),
                evidence={"task_id": t["task_id"], "missing": missing, "prompt_chars": len(prompt)},
                est_tokens_wasted=None,
                recommendation=("State the time window and the maximum amount of work ('look only at the last 24h', "
                                "'stop after N files', 'reply nothing_new if ...') and pin the model in the task "
                                "settings."),
            ))

    # 5. near-identical prompts
    for i in range(len(tasks)):
        for j in range(i + 1, len(tasks)):
            a, b = tasks[i], tasks[j]
            na, nb = normalize(a["prompt"] or ""), normalize(b["prompt"] or "")
            if len(na) < 40 or len(nb) < 40:
                continue
            ratio = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
            if ratio > NEAR_RATIO:
                cross = a["account"] != b["account"]
                sa, sb = _stats(conn, a["task_id"]), _stats(conn, b["task_id"])
                smaller = min(sa["ctx_tokens"], sb["ctx_tokens"])
                out.append(Finding(
                    account="shared" if cross else a["account"], analyzer=NAME, severity="medium",
                    title=f"Tasks '{a['name']}' and '{b['name']}' have {ratio:.0%} identical prompts",
                    fingerprint=f"{NAME}:dup:{min(a['task_id'], b['task_id'])}:{max(a['task_id'], b['task_id'])}",
                    detail=(f"Prompts differ by {1-ratio:.0%}. Crons: `{a['cron']}` vs `{b['cron']}`; models "
                            f"{a['model']} vs {b['model']}; accounts {a['account']} / {b['account']}."),
                    evidence={"tasks": [a["task_id"], b["task_id"]], "ratio": round(ratio, 3),
                              "crons": [a["cron"], b["cron"]], "models": [a["model"], b["model"]]},
                    est_tokens_wasted=int(smaller * 0.5),
                    recommendation=("Consolidate into one task that covers both scopes (or one task with a "
                                    "parameterized prompt), or keep both but share the common instructions via a "
                                    "skill so the prompts are only the delta."),
                ))
    return out
