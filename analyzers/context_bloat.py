"""Instruction-file bloat.

Checks (on the latest snapshot of each context file)
  * Size thresholds by kind/scope. Global CLAUDE.md > GLOBAL_MEDIUM tokens is
    medium, > GLOBAL_HIGH is high; project CLAUDE.md > PROJECT_MEDIUM; commands,
    agents, skills and scheduled-task prompts > OTHER_MEDIUM.
  * Tokens paid per 30 days = est_tokens x sessions in that scope in the last
    30 days (global files count every session for the account).
  * Cheap heuristics for superfluous content inside CLAUDE.md files:
      - lines that restate obvious defaults ("be helpful", "write clean code")
      - long runs of file-path lines that Claude could discover itself
      - bullets duplicated inside the same file
      - very long fenced code blocks
    Evidence quotes line numbers, never whole content.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

from .common import Finding, acct_clause, latest_context_files, rows, since_iso
from .textutil import normalize, split_blocks

NAME = "context_bloat"
WINDOW_DAYS = 30
GLOBAL_MEDIUM = 1_500
GLOBAL_HIGH = 3_000
PROJECT_MEDIUM = 2_500
PROJECT_HIGH = 5_000
OTHER_MEDIUM = 2_000
OTHER_HIGH = 4_000
KINDS = ("claude_md", "command", "agent", "skill", "scheduled_task")

OBVIOUS_PATTERNS = [
    r"\bbe (helpful|concise|careful|thorough|accurate|professional|polite)\b",
    r"\bwrite (clean|good|readable|maintainable|quality) code\b",
    r"\bfollow best practices\b",
    r"\buse (good|clear|descriptive) (variable |function )?names\b",
    r"\bdo(n't| not) (make|introduce) (bugs|errors|mistakes)\b",
    r"\btest your (code|changes)\b",
    r"\bthink (step by step|carefully)\b",
    r"\byou are (a|an) (helpful|expert|senior|skilled) ",
    r"\balways (double[- ]check|verify) your work\b",
    r"\bhandle errors (properly|gracefully|correctly)\b",
    r"\bcomment your code\b",
    r"\bkeep (it|code|things) (simple|dry)\b",
]
_OBVIOUS = [re.compile(p, re.I) for p in OBVIOUS_PATTERNS]
_PATH_LINE = re.compile(r"^\s*(?:[-*]\s*)?`?(?:\.{0,2}/)?[\w.@-]+(?:/[\w.@-]+){1,}(?:\.\w+)?`?\s*(?:[-:—]\s*.{0,60})?$")
PATH_RUN_MIN = 8
LONG_CODE_LINES = 25


def _thresholds(kind: str, scope: str | None) -> tuple[int, int]:
    if kind == "claude_md":
        if (scope or "").startswith("project"):
            return PROJECT_MEDIUM, PROJECT_HIGH
        return GLOBAL_MEDIUM, GLOBAL_HIGH
    return OTHER_MEDIUM, OTHER_HIGH


def _sessions_by_scope(conn: sqlite3.Connection, account: str) -> tuple[int, dict[str, int]]:
    """(total sessions, sessions per project_dir) for the account in the window."""
    data = rows(conn, "SELECT project_dir, COUNT(*) n FROM sessions WHERE account=? AND first_ts>=? GROUP BY project_dir",
                [account, since_iso(WINDOW_DAYS)])
    per = {(r["project_dir"] or ""): r["n"] for r in data}
    return sum(per.values()), per


def _sessions_for(cf: dict, total: int, per: dict[str, int]) -> int:
    scope = cf.get("scope") or "global"
    if scope.startswith("project:"):
        d = scope.split(":", 1)[1].rstrip("/")
        return sum(n for pd, n in per.items() if pd.rstrip("/") == d or pd.startswith(d + "/"))
    if scope == "cloud":
        return 0
    return total


def _heuristics(content: str) -> dict[str, list]:
    """Return {issue: [line refs...]} for cheap superfluous-content checks."""
    lines = content.splitlines()
    issues: dict[str, list] = defaultdict(list)
    for i, line in enumerate(lines, 1):
        if any(p.search(line) for p in _OBVIOUS):
            issues["obvious_defaults"].append(i)
    # runs of path-like lines
    run_start = None
    for i, line in enumerate(lines + [""], 1):
        if _PATH_LINE.match(line) and ("/" in line):
            run_start = run_start or i
        else:
            if run_start and i - run_start >= PATH_RUN_MIN:
                issues["path_lists"].append([run_start, i - 1])
            run_start = None
    blocks = split_blocks(content)
    seen: dict[str, int] = {}
    for b in blocks:
        if b.kind != "bullet":
            continue
        key = normalize(b.text)
        if len(key) < 15:
            continue
        if key in seen:
            issues["duplicate_bullets"].append([seen[key], b.start])
        else:
            seen[key] = b.start
    for b in blocks:
        if b.kind == "code" and b.lines > LONG_CODE_LINES:
            issues["long_code_blocks"].append([b.start, b.end, b.lines])
    return dict(issues)


def run(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    out: list[Finding] = []
    files = latest_context_files(conn, account, KINDS)
    cache: dict[str, tuple[int, dict[str, int]]] = {}
    for cf in files:
        acct = cf["account"]
        if acct not in cache:
            cache[acct] = _sessions_by_scope(conn, acct)
        total, per = cache[acct]
        est = cf["est_tokens"] or 0
        med, high = _thresholds(cf["kind"], cf["scope"])
        n_sessions = _sessions_for(cf, total, per)
        per_period = est * n_sessions
        content = cf.get("content") or ""
        heur = _heuristics(content) if content and cf["kind"] == "claude_md" else {}

        if est > med:
            sev = "high" if est > high else "medium"
            over = est - med
            out.append(Finding(
                account=acct, analyzer=NAME, severity=sev,
                title=f"{cf['kind']} is {est:,} tokens ({cf['path']})",
                fingerprint=f"{NAME}:size:{acct}:{cf['path']}",
                detail=(f"`{cf['path']}` ({cf['scope']}) is ~{est:,} tokens, threshold {med:,}. It was loaded into "
                        f"~{n_sessions} session(s) in the last {WINDOW_DAYS} days, so it costs roughly "
                        f"{per_period:,} context tokens per {WINDOW_DAYS} days before any cache effects "
                        f"(and every cache re-create pays it again)."),
                evidence={"path": cf["path"], "kind": cf["kind"], "scope": cf["scope"], "est_tokens": est,
                          "threshold": med, "sessions_30d": n_sessions, "tokens_per_30d": per_period,
                          "heuristics": heur},
                est_tokens_wasted=int(over * max(n_sessions, 1)),
                recommendation=("Trim to what every session needs. Move task-specific sections into a slash "
                                "command or skill (loaded on demand), delete anything Claude can discover from the "
                                "repo, and run the `claude_md_audit` LLM audit for a per-section verdict."),
            ))

        if heur:
            n_issues = sum(len(v) for v in heur.values())
            parts = []
            if heur.get("obvious_defaults"):
                parts.append(f"{len(heur['obvious_defaults'])} line(s) restating defaults (lines "
                             f"{', '.join(map(str, heur['obvious_defaults'][:10]))})")
            if heur.get("path_lists"):
                parts.append("path lists at " + ", ".join(f"L{a}-{b}" for a, b in heur["path_lists"][:5]))
            if heur.get("duplicate_bullets"):
                parts.append("duplicated bullets " + ", ".join(f"L{a}=L{b}" for a, b in heur["duplicate_bullets"][:8]))
            if heur.get("long_code_blocks"):
                parts.append("long code blocks " + ", ".join(f"L{a}-{b} ({n} lines)" for a, b, n in heur["long_code_blocks"][:5]))
            # approximate tokens of the flagged lines
            lines = content.splitlines()
            flagged: set[int] = set(heur.get("obvious_defaults", []))
            for a, b in heur.get("path_lists", []):
                flagged.update(range(a, b + 1))
            for _, b in heur.get("duplicate_bullets", []):
                flagged.add(b)
            for a, b, _ in heur.get("long_code_blocks", []):
                flagged.update(range(a, b + 1))
            flagged_chars = sum(len(lines[i - 1]) for i in flagged if 0 < i <= len(lines))
            flagged_tokens = int(flagged_chars / 3.6)
            sev = "medium" if flagged_tokens > 300 else "low"
            out.append(Finding(
                account=acct, analyzer=NAME, severity=sev,
                title=f"Superfluous content in {cf['path']} ({n_issues} spots, ~{flagged_tokens} tokens)",
                fingerprint=f"{NAME}:superfluous:{acct}:{cf['path']}",
                detail="; ".join(parts) + ".",
                evidence={"path": cf["path"], "issues": heur, "flagged_tokens": flagged_tokens,
                          "sessions_30d": n_sessions},
                est_tokens_wasted=flagged_tokens * max(n_sessions, 1),
                recommendation=("Delete generic advice (Claude already does it), replace path inventories with one "
                                "sentence on where to look, dedupe bullets, and move long code samples into the "
                                "repo or a skill that is loaded only when needed."),
            ))
    return out
