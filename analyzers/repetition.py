"""Repetition: prompts and tool results that keep coming back.

Checks
  * prompt_hash seen >= REPEAT_MIN times across >= 2 sessions (non-slash):
    recommend a slash command or skill.
  * long prompts (> LONG_PROMPT chars) whose hash recurs: recommend a file or
    CLAUDE.md pointer instead of re-pasting.
  * tools whose results exceed HUGE_RESULT chars repeatedly: recommend
    filtering/head/limits.
"""
from __future__ import annotations

import sqlite3

from .common import Finding, acct_clause, est_tokens, rows, since_iso

NAME = "repetition"
WINDOW_DAYS = 30
REPEAT_MIN = 3
LONG_PROMPT = 4_000
HUGE_RESULT = 20_000
HUGE_MIN_COUNT = 3
MIN_PROMPT_CHARS = 40   # 'continue', 'yes' etc. are not worth a command


def _repeated_prompts(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    w, p = acct_clause(account)
    out = []
    for r in rows(conn, f"""SELECT account, prompt_hash, COUNT(*) n, COUNT(DISTINCT session_id) sessions,
                            MAX(chars) chars, MIN(excerpt) excerpt, MAX(is_slash) is_slash
                            FROM prompts WHERE ts>=? AND prompt_hash IS NOT NULL{w}
                            GROUP BY account, prompt_hash HAVING n >= ? AND sessions >= 2 AND chars >= ?
                            ORDER BY n * chars DESC LIMIT 40""",
                  [since_iso(WINDOW_DAYS)] + p + [REPEAT_MIN, MIN_PROMPT_CHARS]):
        if r["is_slash"]:
            continue
        chars = r["chars"] or 0
        toks = est_tokens("x" * chars)
        long_paste = chars > LONG_PROMPT
        wasted = toks * (r["n"] - 1)
        sev = "high" if wasted > 50_000 else "medium" if long_paste or r["n"] >= 6 else "low"
        excerpt = (r["excerpt"] or "").replace("\n", " ")[:90]
        out.append(Finding(
            account=r["account"], analyzer=NAME, severity=sev,
            title=(f"Long prompt ({chars:,} chars) re-pasted {r['n']}x" if long_paste
                   else f"Prompt repeated {r['n']}x across {r['sessions']} sessions: \"{excerpt[:50]}\""),
            fingerprint=f"{NAME}:prompt:{r['account']}:{r['prompt_hash']}",
            detail=(f"The same prompt (hash {r['prompt_hash'][:10]}, {chars:,} chars, ~{toks:,} tokens) was sent "
                    f"{r['n']} times in {r['sessions']} sessions. Excerpt: \"{excerpt}\""),
            evidence={"prompt_hash": r["prompt_hash"], "count": r["n"], "sessions": r["sessions"], "chars": chars,
                      "excerpt": excerpt},
            est_tokens_wasted=wasted,
            recommendation=(("Save the text as a file in the repo (or a section of CLAUDE.md) and reference it by "
                             "path; Claude can read it on demand instead of carrying it in every prompt.")
                            if long_paste else
                            ("Turn it into a slash command under `.claude/commands/` (or `~/.claude/commands/` "
                             "if it is used across projects) or a skill, so it is one short invocation.")),
        ))
    return out


def _huge_tool_results(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    w, p = acct_clause(account)
    out = []
    for r in rows(conn, f"""SELECT account, tool_name, COUNT(*) n, SUM(result_chars) chars, AVG(result_chars) avg_chars,
                            MAX(result_chars) max_chars, COUNT(DISTINCT session_id) sessions
                            FROM tool_calls WHERE ts>=? AND result_chars > ?{w}
                            GROUP BY account, tool_name HAVING n >= ? ORDER BY chars DESC LIMIT 20""",
                  [since_iso(WINDOW_DAYS), HUGE_RESULT] + p + [HUGE_MIN_COUNT]):
        toks = est_tokens("x" * int(r["chars"]))
        # Everything beyond the threshold is treated as avoidable.
        wasted = est_tokens("x" * int(max(0, r["chars"] - HUGE_RESULT * r["n"])))
        sev = "high" if wasted > 300_000 else "medium" if wasted > 60_000 else "low"
        out.append(Finding(
            account=r["account"], analyzer=NAME, severity=sev,
            title=f"{r['tool_name']} returned > {HUGE_RESULT//1000}k chars {r['n']}x (~{toks:,} tokens)",
            fingerprint=f"{NAME}:tool:{r['account']}:{r['tool_name']}",
            detail=(f"{r['n']} calls to `{r['tool_name']}` in {r['sessions']} sessions returned more than "
                    f"{HUGE_RESULT:,} chars each (avg {r['avg_chars']:,.0f}, max {r['max_chars']:,}). Each result "
                    f"stays in context for the rest of the session and is re-read on every subsequent call."),
            evidence={"tool_name": r["tool_name"], "count": r["n"], "sessions": r["sessions"], "chars": r["chars"],
                      "avg_chars": round(r["avg_chars"]), "max_chars": r["max_chars"]},
            est_tokens_wasted=wasted,
            recommendation=_tool_advice(r["tool_name"]),
        ))
    return out


def _tool_advice(tool: str) -> str:
    t = tool.lower()
    if t == "bash":
        return ("Pipe long output through `head`/`tail`/`grep` or `| wc -l`, redirect build logs to a file and read "
                "only the failing part, and add a CLAUDE.md rule asking for truncated command output.")
    if t == "read":
        return "Read with offset/limit or grep for the relevant symbols first instead of reading whole large files."
    if t in ("grep", "glob"):
        return "Use head_limit / narrower globs and `files_with_matches` mode; search by directory instead of the repo root."
    if t.startswith("mcp__") or t.startswith("webfetch"):
        return ("Ask the tool for a summary or a bounded page (limit/offset) rather than the full payload, and "
                "prefer a subagent to digest big results so they do not sit in the main context.")
    return "Bound the result size (limits, filters, pagination) or run the call in a subagent that returns a summary."


def run(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    return _repeated_prompts(conn, account) + _huge_tool_results(conn, account)
