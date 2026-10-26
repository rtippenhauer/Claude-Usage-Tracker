"""Synthetic fixture + smoke test for every rule analyzer.

``seed(conn)`` fills a schema.sql database with deterministic synthetic data for
two accounts (work, personal): ~40 sessions on mixed models, bloated CLAUDE.md
files with paragraphs duplicated across accounts, four scheduled tasks with
runs, repeated prompts, huge tool results and meter snapshots.

Run:  python -m analyzers.test_fixture [--keep PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import random
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from . import RULE_ANALYZERS
from .common import Finding, est_tokens, open_db, upsert_findings

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


SHARED_PARA = ("Always run the test suite before committing. Use `make test` and fix every failure; never "
               "skip tests with markers unless the change is documented in the PR description.")
SHARED_PARA_VARIANT = ("Always run the test suite before committing. Use `make test` and fix every failure; never "
                       "skip tests with markers unless the change is documented in the PR description and "
                       "approved by the on-call reviewer.")
SHARED_BULLET = "- Prefer small, focused commits with conventional commit messages (feat:, fix:, chore:)."
PATH_LIST = "\n".join(f"- `src/services/{n}/handler.py` - {n} service entrypoint" for n in
                      ["auth", "billing", "calls", "cdr", "dialplan", "media", "presence", "provisioning",
                       "registrar", "routing", "sms", "trunks"])

WORK_GLOBAL_CLAUDE = f"""# Global instructions

You are a helpful, expert senior engineer. Be concise and professional.
Write clean code and follow best practices. Always double-check your work.

## Workflow
{SHARED_PARA}

{SHARED_BULLET}
- Never force-push to main.
- Use descriptive variable names.
{SHARED_BULLET}

## Repository map
{PATH_LIST}

## Deploy procedure
```bash
{os.linesep.join(f'kubectl -n voip rollout status deploy/asterisk-{i}' for i in range(30))}
```

## Style
- Handle errors gracefully.
- Comment your code.
- Keep it simple.
""" + ("\nFiller paragraph about historical context of the platform and migration notes. " * 60)

WORK_PROJECT_CLAUDE = f"""# VoIP platform

## Workflow
{SHARED_PARA_VARIANT}

{SHARED_BULLET}
- Run `make lint` before `make test`.

## Context
This repo holds the OpenSIPS and Asterisk configuration rendered into ConfigMaps.
"""

PERSONAL_GLOBAL_CLAUDE = f"""# Personal defaults

Be helpful and think step by step.

## Workflow
{SHARED_PARA}

{SHARED_BULLET}
- Write tests for new features.
""" + ("\nLong description of home-lab conventions repeated for emphasis. " * 40)

PERSONAL_PROJECT_CLAUDE = f"""# DinnerBears

## Workflow
{SHARED_PARA}
- Angular frontend lives in `web/`, NestJS API in `api/`.
"""

COMMAND_REVIEW = f"""Review the diff on the current branch.

{SHARED_PARA}

Report findings as a bullet list grouped by severity.
"""

COMMAND_REVIEW_PERSONAL = f"""Review the diff on the current branch for the DinnerBears app.

{SHARED_PARA}

Report findings as a bullet list grouped by severity, then suggest a commit message.
"""

AGENT_RESEARCH = """---
model: opus
---
You are a research subagent. Search the codebase for the requested symbol and summarize usages.
""" + ("Additional long boilerplate about tone and formatting that never changes. " * 50)

TASK_PROMPT_A = ("Check the OpenSIPS and Asterisk pods in the voip namespace for restarts or error logs since the "
                 "last run. If nothing changed, reply nothing_new. Summarize anything noteworthy in 5 bullets.")
TASK_PROMPT_B = ("Check the OpenSIPS and Asterisk pods in the voip-staging namespace for restarts or error logs since "
                 "the last run. If nothing changed, reply nothing_new. Summarize anything noteworthy in 5 bullets.")
TASK_PROMPT_C = "Read my inbox labels and summarize unread newsletters."
TASK_PROMPT_D = "Summarize new GitHub issues on robs3dprintedworld and draft replies for the top three."

MODELS = {
    "opus": "claude-opus-4-1",
    "sonnet": "claude-sonnet-4-5",
    "haiku": "claude-haiku-4-5",
}


def _add_context(conn: sqlite3.Connection, account: str, kind: str, path: str, scope: str, content: str,
                 machine: str = "box") -> None:
    conn.execute("""INSERT OR IGNORE INTO context_files(account, machine, kind, path, scope, content_hash, chars,
                    est_tokens, content, snapshot_ts) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (account, machine, kind, path, scope, _sha(content), len(content), est_tokens(content), content,
                  _iso(NOW - timedelta(days=1))))


def _session(conn: sqlite3.Connection, rng: random.Random, *, sid: str, account: str, project: str, start: datetime,
             model: str, calls: int, base_ctx: int, out_per_call: int, hit: float, churn: bool = False,
             tool_only: bool = False, sidechain: int = 0, prompts: list[str] | None = None,
             huge_tool: int = 0, thrash: bool = False, entrypoint: str = "cli") -> None:
    prompts = prompts or [f"work on {project} task {sid[-3:]}"]
    conn.execute("""INSERT INTO sessions(session_id, account, machine, project_dir, entrypoint, cc_version, git_branch,
                    first_ts, last_ts, turns, first_prompt, source_file) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (sid, account, "box-" + account, project, entrypoint, "2.1.0", "main", _iso(start),
                  _iso(start + timedelta(minutes=calls * 2)), len(prompts), prompts[0][:300], f"/tmp/{sid}.jsonl"))
    for k, text in enumerate(prompts):
        ts = start + timedelta(minutes=k * (calls * 2 // max(len(prompts), 1)))
        norm = " ".join(text.lower().split())
        conn.execute("""INSERT INTO prompts(prompt_uuid, session_id, account, ts, chars, is_slash, slash_name,
                        prompt_hash, excerpt) VALUES(?,?,?,?,?,?,?,?,?)""",
                     (f"{sid}-p{k}", sid, account, _iso(ts), len(text), int(text.startswith("/")),
                      text.split()[0][1:] if text.startswith("/") else None, _sha(norm), text[:200]))
    cached_prefix = int(base_ctx * 0.9)
    for i in range(calls):
        ts = start + timedelta(minutes=i * 2)
        growth = i * 1_500
        if churn:
            cc, cr = cached_prefix + growth, 0
            inp = 500
        elif i == 0:
            cc, cr, inp = cached_prefix, 0, base_ctx - cached_prefix
        else:
            # Realistic steady state: the prefix is read from cache, only the
            # new turn is created, and a low hit ratio shows up as uncached input.
            total = base_ctx + growth
            cr = int(total * hit)
            cc = min(1_500 + rng.randint(0, 800), total - cr)
            inp = total - cr - cc
        out = max(20, int(rng.gauss(out_per_call, out_per_call * 0.3)))
        stop = "tool_use" if (tool_only or thrash or (i < calls - 1 and rng.random() < 0.6)) else "end_turn"
        if tool_only or thrash:
            out = min(out, 80)
        mid = f"msg-{sid}-{i}"
        conn.execute("""INSERT INTO messages(message_id, request_id, session_id, account, ts, model, requested_model,
                        effort, input_tokens, cache_create_tokens, cache_read_tokens, output_tokens, thinking_tokens,
                        stop_reason, is_sidechain, parent_uuid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (mid, f"req-{sid}-{i}", sid, account, _iso(ts), model, model, None, inp, cc, cr, out,
                      0, stop, 0, None))
        if stop == "tool_use":
            tool = rng.choice(["Bash", "Read", "Grep", "mcp__memory__memory_read"])
            res = rng.randint(200, 6_000)
            if huge_tool and i < huge_tool:
                tool, res = "Bash", rng.randint(25_000, 90_000)
            conn.execute("""INSERT INTO tool_calls(tool_use_id, message_id, session_id, account, ts, tool_name,
                            mcp_server, input_chars, result_chars) VALUES(?,?,?,?,?,?,?,?,?)""",
                         (f"tu-{sid}-{i}", mid, sid, account, _iso(ts), tool,
                          "memory" if tool.startswith("mcp__memory") else None, rng.randint(50, 400), res))
    for j in range(sidechain):
        ts = start + timedelta(minutes=calls * 2 + j)
        conn.execute("""INSERT INTO messages(message_id, request_id, session_id, account, ts, model, requested_model,
                        effort, input_tokens, cache_create_tokens, cache_read_tokens, output_tokens, thinking_tokens,
                        stop_reason, is_sidechain, parent_uuid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (f"msg-{sid}-s{j}", f"req-{sid}-s{j}", sid, account, _iso(ts), model, model, None,
                      2_000, 30_000, 20_000 * j, 120, 0, "tool_use", 1, None))


def seed(conn: sqlite3.Connection) -> None:
    """Seed deterministic synthetic data for accounts 'work' and 'personal'."""
    rng = random.Random(42)
    conn.execute("INSERT OR IGNORE INTO accounts(account, label, plan) VALUES('work','Work (Pro)','pro')")
    conn.execute("INSERT OR IGNORE INTO accounts(account, label, plan) VALUES('personal','Personal (Pro)','pro')")
    conn.execute("INSERT OR IGNORE INTO machines(machine, account, last_seen) VALUES('box-work','work',?)", (_iso(NOW),))
    conn.execute("INSERT OR IGNORE INTO machines(machine, account, last_seen) VALUES('box-personal','personal',?)",
                 (_iso(NOW),))

    # context files --------------------------------------------------------
    _add_context(conn, "work", "claude_md", "/home/rob/.claude/CLAUDE.md", "global", WORK_GLOBAL_CLAUDE, "box-work")
    _add_context(conn, "work", "claude_md", "/home/rob/src/voip-platform/CLAUDE.md", "project:/home/rob/src/voip-platform",
                 WORK_PROJECT_CLAUDE, "box-work")
    _add_context(conn, "work", "command", "/home/rob/.claude/commands/review.md", "global", COMMAND_REVIEW, "box-work")
    _add_context(conn, "work", "agent", "/home/rob/.claude/agents/research.md", "global", AGENT_RESEARCH, "box-work")
    _add_context(conn, "personal", "claude_md", "/Users/rob/.claude/CLAUDE.md", "global", PERSONAL_GLOBAL_CLAUDE,
                 "box-personal")
    _add_context(conn, "personal", "claude_md", "/Users/rob/code/dinnerbears/CLAUDE.md",
                 "project:/Users/rob/code/dinnerbears", PERSONAL_PROJECT_CLAUDE, "box-personal")
    _add_context(conn, "personal", "command", "/Users/rob/.claude/commands/review.md", "global",
                 COMMAND_REVIEW_PERSONAL, "box-personal")
    # an older snapshot that must be ignored by "latest" queries
    conn.execute("""INSERT OR IGNORE INTO context_files(account, machine, kind, path, scope, content_hash, chars,
                    est_tokens, content, snapshot_ts) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 ("work", "box-work", "claude_md", "/home/rob/.claude/CLAUDE.md", "global", "old", 10, 3, "# old",
                  _iso(NOW - timedelta(days=20))))

    # scheduled tasks ------------------------------------------------------
    tasks = [
        ("t-voip-prod", "work", "VoIP prod health", "0 */2 * * *", MODELS["opus"], TASK_PROMPT_A),
        ("t-voip-stg", "work", "VoIP staging health", "30 */2 * * *", MODELS["opus"], TASK_PROMPT_B),
        ("t-inbox", "personal", "Inbox digest", "CRON_TZ=America/New_York 0 8 * * *", MODELS["sonnet"], TASK_PROMPT_C),
        ("t-issues", "personal", "GitHub issues", "17 9 * * 1-5", MODELS["sonnet"], TASK_PROMPT_D),
    ]
    for tid, acct, name, cron, model, prompt in tasks:
        conn.execute("""INSERT OR REPLACE INTO scheduled_tasks(task_id, account, name, cron, model, prompt,
                        permission_mode, enabled, updated_ts) VALUES(?,?,?,?,?,?,?,?,?)""",
                     (tid, acct, name, cron, model, prompt, "auto", 1, _iso(NOW)))
        _add_context(conn, acct, "scheduled_task", f"task:{name}", "cloud", prompt, "cloud")
    outcomes = {"t-voip-prod": ["nothing_new"] * 9 + ["noteworthy"],
                "t-voip-stg": ["nothing_new"] * 8 + ["noteworthy", "error"],
                "t-inbox": ["noteworthy"] * 6 + ["nothing_new"] * 4,
                "t-issues": ["noteworthy", "nothing_new"] * 5}
    for tid, acct, name, cron, model, _ in tasks:
        for k, oc in enumerate(outcomes[tid]):
            st = NOW - timedelta(days=20 - k * 2, hours=rng.randint(0, 5))
            conn.execute("""INSERT OR REPLACE INTO task_runs(run_id, task_id, account, started_ts, ended_ts, model,
                            input_tokens, cache_read_tokens, cache_create_tokens, output_tokens, turns, outcome, summary)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (f"{tid}-run{k}", tid, acct, _iso(st), _iso(st + timedelta(minutes=3)), model,
                          rng.randint(3_000, 8_000), rng.randint(20_000, 60_000), rng.randint(5_000, 15_000),
                          rng.randint(150, 500) if "voip" in tid else rng.randint(800, 2_500), rng.randint(3, 9),
                          oc, f"{name}: {oc}"))

    # sessions -------------------------------------------------------------
    work_proj = "/home/rob/src/voip-platform"
    pers_proj = "/Users/rob/code/dinnerbears"
    repeated = ("Summarize the current state of the branch, list open TODOs, and propose next steps for the "
                "OpenSIPS routing refactor.")
    long_paste = "Here is the incident runbook for reference:\n" + ("step: check registrar pod logs for SIP 503s and " * 120)

    n = 0
    for d in range(0, 28):
        day = NOW - timedelta(days=d, hours=rng.randint(9, 17))
        if day.weekday() >= 5:
            continue  # work sessions only on weekdays
        n += 1
        sid = f"w{n:03d}-{_sha(str(n))[:8]}"
        fam = rng.choice(["opus", "opus", "sonnet", "haiku"])
        kw: dict = {}
        if n % 7 == 0:
            kw.update(churn=True, calls=rng.randint(8, 14), base_ctx=rng.randint(40_000, 80_000), hit=0.0)
        elif n % 5 == 0:
            kw.update(calls=rng.randint(22, 34), base_ctx=rng.randint(60_000, 120_000), hit=rng.uniform(0.1, 0.35), huge_tool=6)
        elif n % 4 == 0:
            kw.update(calls=8, base_ctx=30_000, hit=0.85, tool_only=True, out_per_call=60,
                      prompts=["/review", "/review"])
        elif n % 3 == 0:
            kw.update(calls=rng.randint(30, 45), base_ctx=rng.randint(30_000, 60_000), hit=0.9, out_per_call=50, thrash=True)
            fam = "haiku"
        else:
            kw.update(calls=rng.randint(6, 20), base_ctx=rng.randint(20_000, 120_000), hit=rng.uniform(0.6, 0.95))
        kw.setdefault("out_per_call", rng.choice([120, 400, 900]))
        if n % 6 == 0:
            kw.update(sidechain=6)
            fam = "opus"
        if n % 2 == 0:
            kw.setdefault("prompts", [repeated, "continue"])
        if n % 4 == 1:
            kw["prompts"] = [long_paste, "now apply it"]
        _session(conn, rng, sid=sid, account="work", project=work_proj, start=day, model=MODELS[fam], **kw)

    m = 0
    for d in range(0, 28, 2):
        day = NOW - timedelta(days=d, hours=rng.randint(18, 23))
        m += 1
        sid = f"p{m:03d}-{_sha('p' + str(m))[:8]}"
        fam = rng.choice(["sonnet", "sonnet", "opus", "haiku"])
        kw = {"calls": rng.randint(4, 16), "base_ctx": rng.randint(10_000, 60_000), "hit": rng.uniform(0.5, 0.95),
              "out_per_call": rng.choice([150, 500, 1200])}
        if m % 4 == 0:
            kw.update(prompts=[repeated])
        if m == 3:
            kw.update(calls=12, base_ctx=70_000, hit=0.1)
        _session(conn, rng, sid=sid, account="personal", project=pers_proj, start=day, model=MODELS[fam], **kw)
    # one personal spike day
    _session(conn, rng, sid="p-spike-" + _sha("spike")[:8], account="personal", project=pers_proj,
             start=NOW - timedelta(days=5, hours=20), model=MODELS["opus"], calls=25, base_ctx=150_000, hit=0.7,
             out_per_call=800)

    # meter snapshots ------------------------------------------------------
    for d in range(0, 28):
        date = (NOW - timedelta(days=d)).date().isoformat()
        work_pct = 100 if d in (3, 9, 16) else rng.randint(55, 95)
        pers_pct = rng.randint(5, 25)
        for acct, pct in (("work", work_pct), ("personal", pers_pct)):
            conn.execute("INSERT OR REPLACE INTO meter_snapshots(account, date, surface, metric, value) VALUES(?,?,?,?,?)",
                         (acct, date, "claude_code", "pct_used", str(pct)))
    conn.commit()


def make_fixture(path: str | None = None) -> str:
    """Create a seeded DB at ``path`` (temp file when None) and return its path."""
    if path is None:
        fd, path = tempfile.mkstemp(prefix="tracker-fixture-", suffix=".db")
        os.close(fd)
        os.unlink(path)
    conn = open_db(path, create=True)
    seed(conn)
    conn.close()
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", help="write the fixture DB to this path and leave it in place")
    args = ap.parse_args(argv)
    path = make_fixture(args.keep)
    conn = open_db(path)
    failures = 0
    all_findings: list[Finding] = []
    for name in RULE_ANALYZERS:
        mod = importlib.import_module(f"analyzers.{name}")
        fs = mod.run(conn, "all")
        all_findings += fs
        ok = len(fs) >= 1
        print(f"{'PASS' if ok else 'FAIL'}  {name:24s} {len(fs):3d} findings  "
              f"{sorted({f.severity for f in fs})}")
        if not ok:
            failures += 1
        for f in fs:
            assert f.fingerprint.startswith(name + ":"), f"bad fingerprint {f.fingerprint}"
    # upsert twice; row count must not grow
    upsert_findings(conn, all_findings)
    n1 = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    upsert_findings(conn, all_findings)
    n2 = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    fps = {f.fingerprint for f in all_findings}
    print(f"upsert idempotent: {n1} == {n2} == {len(fps)} unique fingerprints -> {n1 == n2 == len(fps)}")
    if not (n1 == n2 == len(fps)):
        failures += 1
    shared = [f for f in all_findings if f.account == "shared"]
    print(f"shared (cross-account) findings: {len(shared)}")
    if not shared:
        failures += 1
    conn.close()
    if not args.keep:
        os.unlink(path)
    else:
        print(f"fixture kept at {path}")
    print("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
