"""Prompt-cache efficiency.

Checks
  * Sessions with a large context but a low cache hit ratio
    (context_tokens > LOW_RATIO_MIN_CONTEXT and cache_hit_ratio < LOW_RATIO).
  * Cache churn: consecutive calls in one session that each re-create a large
    cache block (cache_create_tokens >= CHURN_MIN_CREATE), which usually means
    the cached prefix (system prompt / CLAUDE.md / early turns) keeps changing.

est_tokens_wasted = cache_create tokens that could have been cache reads.
"""
from __future__ import annotations

import sqlite3

from .common import Finding, acct_clause, rows, since_iso

NAME = "cache_efficiency"
WINDOW_DAYS = 30
LOW_RATIO_MIN_CONTEXT = 50_000   # session context tokens
LOW_RATIO = 0.5
CHURN_MIN_CREATE = 5_000         # tokens re-created per call to count as churn
CHURN_MIN_RUN = 3                # consecutive churning calls


def _low_ratio_sessions(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    w, p = acct_clause(account)
    out = []
    for s in rows(conn, f"""SELECT * FROM v_session_totals
                            WHERE calls > 0 AND first_ts >= ? AND context_tokens > ? AND cache_hit_ratio < ?{w}
                            ORDER BY context_tokens DESC LIMIT 50""",
                  [since_iso(WINDOW_DAYS), LOW_RATIO_MIN_CONTEXT, LOW_RATIO] + p):
        ratio = s["cache_hit_ratio"] or 0
        # Tokens that were billed as fresh input/creation but, at a healthy
        # ratio, would have been reads.
        wasted = int(max(0, s["context_tokens"] * (0.8 - ratio))) if ratio < 0.8 else 0
        sev = "high" if s["context_tokens"] > 500_000 or ratio < 0.2 else "medium"
        out.append(Finding(
            account=s["account"], analyzer=NAME, severity=sev,
            title=f"Low cache hit ratio {ratio:.0%} on a {s['context_tokens']:,}-token session",
            fingerprint=f"{NAME}:low_ratio:{s['session_id']}",
            detail=(f"Session `{s['session_id']}` ({s['project_dir'] or '?'}, {s['entrypoint'] or '?'}) ran "
                    f"{s['calls']} calls with {s['context_tokens']:,} context tokens; only {ratio:.0%} were cache "
                    f"reads ({s['cache_read_tokens'] or 0:,}). cache_create={s['cache_create_tokens'] or 0:,}, "
                    f"uncached input={s['input_tokens'] or 0:,}."),
            evidence={"session_id": s["session_id"], "context_tokens": s["context_tokens"],
                      "cache_read_tokens": s["cache_read_tokens"], "cache_create_tokens": s["cache_create_tokens"],
                      "input_tokens": s["input_tokens"], "cache_hit_ratio": round(ratio, 3), "calls": s["calls"],
                      "models": s["models"], "first_ts": s["first_ts"]},
            est_tokens_wasted=wasted,
            recommendation=("Keep the cached prefix stable: avoid editing CLAUDE.md or switching models/effort "
                            "mid-session, keep long tool results out of the early turns, and prefer one long "
                            "session over many short ones on the same project. Use `investigate session <id>` "
                            "to see where cache_create spikes."),
        ))
    return out


def _cache_churn(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    w, p = acct_clause(account)
    sessions = rows(conn, f"""SELECT session_id, account, project_dir FROM sessions
                              WHERE first_ts >= ?{w}""", [since_iso(WINDOW_DAYS)] + p)
    out = []
    for s in sessions:
        msgs = rows(conn, """SELECT ts, model, cache_create_tokens, cache_read_tokens, input_tokens, is_sidechain
                             FROM messages WHERE session_id = ? AND is_sidechain = 0 ORDER BY ts""",
                    [s["session_id"]])
        if len(msgs) < CHURN_MIN_RUN + 1:
            continue
        # Find runs of consecutive calls each re-creating a big block.
        runs: list[tuple[int, int, int]] = []   # (start_idx, end_idx, tokens)
        i = 0
        while i < len(msgs):
            if (msgs[i]["cache_create_tokens"] or 0) >= CHURN_MIN_CREATE:
                j = i
                tok = 0
                while j < len(msgs) and (msgs[j]["cache_create_tokens"] or 0) >= CHURN_MIN_CREATE:
                    tok += msgs[j]["cache_create_tokens"]
                    j += 1
                if j - i >= CHURN_MIN_RUN:
                    runs.append((i, j - 1, tok))
                i = j
            else:
                i += 1
        if not runs:
            continue
        # First creation in a run is legitimate; the rest could have been reads.
        wasted = sum(tok - msgs[a]["cache_create_tokens"] for a, _, tok in runs)
        longest = max(runs, key=lambda r: r[1] - r[0])
        models = sorted({m["model"] for m in msgs[longest[0]:longest[1] + 1] if m["model"]})
        sev = "high" if wasted > 200_000 else "medium" if wasted > 50_000 else "low"
        out.append(Finding(
            account=s["account"], analyzer=NAME, severity=sev,
            title=f"Cache churn: {sum(r[1]-r[0]+1 for r in runs)} consecutive calls re-created >= {CHURN_MIN_CREATE:,} tokens",
            fingerprint=f"{NAME}:churn:{s['session_id']}",
            detail=(f"Session `{s['session_id']}` ({s['project_dir'] or '?'}) has {len(runs)} run(s) of calls where "
                    f"every call wrote a fresh cache block instead of reading the previous one. Longest run: calls "
                    f"{longest[0]+1}-{longest[1]+1} ({longest[2]:,} tokens created). Models in that run: "
                    f"{', '.join(models) or '?'}. Typical causes: a system prompt that changes each turn (dynamic "
                    f"content in CLAUDE.md, timestamps), model switches, or a prefix longer than the cache TTL."),
            evidence={"session_id": s["session_id"], "runs": [{"from_call": a + 1, "to_call": b + 1, "tokens": t}
                                                               for a, b, t in runs],
                      "models": models, "threshold": CHURN_MIN_CREATE},
            est_tokens_wasted=wasted,
            recommendation=("Make the prefix deterministic (no dates/env dumps in CLAUDE.md or hooks), avoid "
                            "switching model/effort mid-session, and keep the session active so the cache does "
                            "not expire between turns."),
        ))
    return out


def run(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    return _low_ratio_sessions(conn, account) + _cache_churn(conn, account)
