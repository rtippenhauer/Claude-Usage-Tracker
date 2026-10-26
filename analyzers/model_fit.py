"""Model fit: expensive models on low-complexity work, and cheap models that
struggle.

Expensive-on-easy (per session, opus-class family):
  * average output < SHORT_OUTPUT tokens per call
  * mostly tool-only turns (stop_reason='tool_use' with small output)
  * sessions driven entirely by slash commands
  * sidechain (subagent) traffic on opus-class models

Cheap-but-struggling (per session, haiku/sonnet):
  * a run of > RETRY_RUN consecutive assistant calls that all stop with
    tool_use and small outputs, which looks like thrashing.

Model families are detected by substring; see common.RELATIVE_COST for the
approximate cost ordering used in the detail text.
"""
from __future__ import annotations

import sqlite3

from .common import (EXPENSIVE_FAMILIES, Finding, acct_clause, model_family, relative_cost, rows, since_iso)

NAME = "model_fit"
WINDOW_DAYS = 30
SHORT_OUTPUT = 200          # avg output tokens per call
TOOL_ONLY_SMALL = 150       # output tokens for a call to count as tool-only
TOOL_ONLY_SHARE = 0.8
RETRY_RUN = 15
MIN_CALLS = 4


def _session_calls(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    return rows(conn, """SELECT ts, model, output_tokens, input_tokens, cache_read_tokens, cache_create_tokens,
                         stop_reason, is_sidechain FROM messages WHERE session_id=? ORDER BY ts""", [session_id])


def _expensive_easy(conn: sqlite3.Connection, s: dict, calls: list[dict]) -> Finding | None:
    main = [c for c in calls if not c["is_sidechain"]]
    if len(main) < MIN_CALLS:
        return None
    fam_counts: dict[str, int] = {}
    for c in main:
        fam_counts[model_family(c["model"])] = fam_counts.get(model_family(c["model"]), 0) + 1
    fam = max(fam_counts, key=fam_counts.get)
    if fam not in EXPENSIVE_FAMILIES:
        return None
    out_total = sum(c["output_tokens"] or 0 for c in main)
    avg_out = out_total / len(main)
    tool_only = sum(1 for c in main if c["stop_reason"] == "tool_use" and (c["output_tokens"] or 0) < TOOL_ONLY_SMALL)
    tool_share = tool_only / len(main)
    prompts = rows(conn, "SELECT is_slash, slash_name FROM prompts WHERE session_id=?", [s["session_id"]])
    slash_only = bool(prompts) and all(p["is_slash"] for p in prompts)
    signals = []
    if avg_out < SHORT_OUTPUT:
        signals.append(f"avg output {avg_out:.0f} tokens/call")
    if tool_share >= TOOL_ONLY_SHARE:
        signals.append(f"{tool_share:.0%} tool-only turns")
    if slash_only:
        names = sorted({p["slash_name"] for p in prompts if p["slash_name"]})
        signals.append(f"slash-command-only session ({', '.join(names)[:60]})")
    if not signals:
        return None
    ctx = sum((c["input_tokens"] or 0) + (c["cache_read_tokens"] or 0) + (c["cache_create_tokens"] or 0) for c in main)
    # Tokens are not "wasted" per se; express the cost-equivalent excess over sonnet in token units.
    factor = relative_cost(fam) / relative_cost("sonnet")
    excess = int(ctx * (1 - 1 / factor)) if factor > 1 else 0
    sev = "high" if ctx > 1_000_000 else "medium" if ctx > 200_000 else "low"
    return Finding(
        account=s["account"], analyzer=NAME, severity=sev,
        title=f"{fam} used for low-complexity session ({'; '.join(signals)})",
        fingerprint=f"{NAME}:expensive_easy:{s['session_id']}",
        detail=(f"Session `{s['session_id']}` ({s['project_dir'] or '?'}, {s['entrypoint'] or '?'}) ran {len(main)} "
                f"calls on {fam} ({', '.join(sorted({c['model'] for c in main if c['model']}))}) with "
                f"{ctx:,} context tokens and {out_total:,} output tokens. Signals: {'; '.join(signals)}. "
                f"At ~{factor:.0f}x the relative cost of sonnet, the same work on sonnet would have cost the "
                f"equivalent of ~{ctx - excess:,} tokens."),
        evidence={"session_id": s["session_id"], "family": fam, "calls": len(main), "avg_output": round(avg_out),
                  "tool_only_share": round(tool_share, 2), "slash_only": slash_only, "context_tokens": ctx,
                  "first_prompt": (s.get("first_prompt") or "")[:120]},
        est_tokens_wasted=excess,
        recommendation=("Start routine/tool-heavy sessions with `claude --model sonnet` (or set the default model "
                        "in settings) and switch to opus-class only for design or hard debugging. For slash "
                        "commands, pin a cheaper model in the command's frontmatter."),
    )


def _sidechain_expensive(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    w, p = acct_clause(account)
    out = []
    for r in rows(conn, f"""SELECT account, model, COUNT(*) calls, SUM(output_tokens) output_tokens,
                            SUM(input_tokens+cache_read_tokens+cache_create_tokens) context_tokens,
                            COUNT(DISTINCT session_id) sessions
                            FROM messages WHERE is_sidechain=1 AND ts>=?{w} GROUP BY account, model""",
                  [since_iso(WINDOW_DAYS)] + p):
        fam = model_family(r["model"])
        if fam not in EXPENSIVE_FAMILIES or r["calls"] < MIN_CALLS:
            continue
        avg_out = (r["output_tokens"] or 0) / r["calls"]
        factor = relative_cost(fam) / relative_cost("sonnet")
        ctx = r["context_tokens"] or 0
        excess = int(ctx * (1 - 1 / factor))
        sev = "high" if ctx > 2_000_000 else "medium" if ctx > 300_000 else "low"
        out.append(Finding(
            account=r["account"], analyzer=NAME, severity=sev,
            title=f"Subagents running on {fam}: {r['calls']} sidechain calls, {ctx:,} context tokens",
            fingerprint=f"{NAME}:sidechain:{r['account']}:{r['model']}",
            detail=(f"{r['calls']} sidechain (subagent) calls across {r['sessions']} sessions used `{r['model']}` "
                    f"with avg output {avg_out:.0f} tokens. Subagents mostly search and summarize, which rarely "
                    f"needs an opus-class model."),
            evidence={"model": r["model"], "calls": r["calls"], "sessions": r["sessions"], "context_tokens": ctx,
                      "avg_output": round(avg_out)},
            est_tokens_wasted=excess,
            recommendation=("Set `model: sonnet` (or haiku for pure search) in the frontmatter of custom agents "
                            "under `.claude/agents/`, and pass an explicit model when spawning subagents."),
        ))
    return out


def _cheap_struggling(s: dict, calls: list[dict]) -> Finding | None:
    main = [c for c in calls if not c["is_sidechain"]]
    if len(main) <= RETRY_RUN:
        return None
    fams = {model_family(c["model"]) for c in main if c["model"]}
    if not fams or fams & EXPENSIVE_FAMILIES:
        return None
    best = 0
    best_end = 0
    run = 0
    for i, c in enumerate(main):
        if c["stop_reason"] == "tool_use" and (c["output_tokens"] or 0) < TOOL_ONLY_SMALL:
            run += 1
            if run > best:
                best, best_end = run, i
        else:
            run = 0
    if best <= RETRY_RUN:
        return None
    seg = main[best_end - best + 1: best_end + 1]
    ctx = sum((c["input_tokens"] or 0) + (c["cache_read_tokens"] or 0) + (c["cache_create_tokens"] or 0) for c in seg)
    return Finding(
        account=s["account"], analyzer=NAME, severity="medium" if best > 2 * RETRY_RUN else "low",
        title=f"{'/'.join(sorted(fams))} session thrashed: {best} consecutive small tool-use turns",
        fingerprint=f"{NAME}:struggling:{s['session_id']}",
        detail=(f"Session `{s['session_id']}` ({s['project_dir'] or '?'}) on {', '.join(sorted(fams))} ran {best} "
                f"consecutive assistant turns that each stopped for a tool call with < {TOOL_ONLY_SMALL} output "
                f"tokens (calls {best_end - best + 2}-{best_end + 1}, {ctx:,} context tokens in that stretch). "
                f"This pattern often means the model is looping or retrying rather than converging."),
        evidence={"session_id": s["session_id"], "run_length": best, "from_call": best_end - best + 2,
                  "to_call": best_end + 1, "context_tokens_in_run": ctx, "models": sorted(fams)},
        est_tokens_wasted=int(ctx * 0.5),
        recommendation=("For tasks that need multi-step reasoning or large-codebase navigation, start on sonnet "
                        "or opus; if a cheap session loops, interrupt early and restate the goal or escalate "
                        "the model instead of letting it burn turns."),
    )


def run(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    w, p = acct_clause(account)
    out: list[Finding] = []
    sessions = rows(conn, f"SELECT * FROM sessions WHERE first_ts>=?{w}", [since_iso(WINDOW_DAYS)] + p)
    for s in sessions:
        calls = _session_calls(conn, s["session_id"])
        if not calls:
            continue
        f = _expensive_easy(conn, s, calls)
        if f:
            out.append(f)
        f = _cheap_struggling(s, calls)
        if f:
            out.append(f)
    out += _sidechain_expensive(conn, account)
    return out
