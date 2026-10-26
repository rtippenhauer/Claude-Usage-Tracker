"""Run an LLM-backed audit through `claude -p` and store the findings.

Usage
  python -m agents.run_agent --db PATH --audit claude_md_audit|duplicate_merge|schedule_review|weekly_review
                             [--account work|personal|all] [--model haiku|sonnet|opus] [--dry-run] [--json]

Each audit builds a prompt from a template in agents/prompts/ plus evidence
pulled from the DB, routes it to a model with agents.router, invokes
`claude -p --model <id> --output-format json` (Claude Code headless, uses the
machine's existing login; no API key), parses the returned JSON into
Finding objects and upserts them. `--dry-run` prints the prompt and the chosen
model and exits without calling claude.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyzers.common import (Finding, acct_clause, est_tokens, latest_context_files, open_db, rows,  # noqa: E402
                              since_iso, sort_findings, upsert_findings)
from analyzers.textutil import sections  # noqa: E402
from agents import router  # noqa: E402

PROMPTS = Path(__file__).parent / "prompts"
AUDITS = ("claude_md_audit", "duplicate_merge", "schedule_review", "weekly_review")
CLAUDE_TIMEOUT = 600
MAX_FILE_CHARS = 40_000     # truncate a single instruction file beyond this (evidence, not the whole thing)


class ClaudeMissing(RuntimeError):
    """Raised when the `claude` CLI is not on PATH."""


# ----------------------------------------------------------------------------- evidence builders

def _sessions_30d(conn, account: str) -> int:
    return rows(conn, "SELECT COUNT(*) n FROM sessions WHERE account=? AND first_ts>=?",
                [account, since_iso(30)])[0]["n"]


def build_claude_md_audit(conn, account: str | None) -> tuple[dict, dict]:
    """Prompt vars + routing job for the CLAUDE.md audit."""
    files = latest_context_files(conn, account, ("claude_md",))
    parts = []
    n_sections = 0
    for cf in files:
        content = (cf.get("content") or "")
        if not content:
            continue
        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + "\n[... truncated ...]"
        secs = sections(content)
        n_sections += len(secs)
        outline = "\n".join(f"  - L{a}-{b} `{h}` (~{est_tokens(body)} tokens)" for h, a, b, body in secs)
        numbered = "\n".join(f"{i:4d}| {line}" for i, line in enumerate(content.splitlines(), 1))
        parts.append(f"### {cf['path']}\naccount: {cf['account']}  scope: {cf['scope']}  est_tokens: {cf['est_tokens']}  "
                     f"sessions per 30 days: {_sessions_30d(conn, cf['account'])}\n\nSections:\n{outline}\n\n"
                     f"```\n{numbered}\n```\n")
    text = "\n".join(parts) or "(no CLAUDE.md snapshots in the database)"
    job = {"input_tokens": est_tokens(text), "judgment": True, "cross_file": False, "n_items": n_sections}
    return {"account": account or "all", "files": text}, job


def build_duplicate_merge(conn, account: str | None) -> tuple[dict, dict]:
    from analyzers import duplicate_instructions as di
    fs = di.run(conn, account)
    files = {cf["id"]: cf for cf in latest_context_files(conn, None, di.KINDS)}
    parts = []
    for k, f in enumerate(sort_findings(fs)[:12], 1):
        locs = f.evidence.get("locations", [])
        copies = []
        for loc in locs:
            cf = next((c for c in files.values() if c["account"] == loc["account"] and c["path"] == loc["path"]), None)
            lines = (cf.get("content") or "").splitlines() if cf else []
            ln = loc["line"]
            end = ln
            while end < len(lines) and lines[end].strip():   # through the end of the block
                end += 1
            snippet = "\n".join(lines[max(0, ln - 1): end]) if lines else "(content not stored)"
            sess = _sessions_30d(conn, loc["account"]) if loc.get("scope") != "cloud" else 0
            copies.append(f"- account={loc['account']} kind={loc['kind']} scope={loc['scope']} path={loc['path']} "
                          f"line={ln} sessions/30d={sess}\n  text: {snippet.strip()[:600]}")
        parts.append(f"### cluster {k} (fingerprint {f.fingerprint})\n{f.title}\n" + "\n".join(copies) + "\n")
    text = "\n".join(parts) or "(the rule analyzer found no duplicate clusters)"
    job = {"input_tokens": est_tokens(text), "judgment": True, "cross_file": True, "n_items": len(parts)}
    return {"account": account or "all", "clusters": text}, job


def build_schedule_review(conn, account: str | None) -> tuple[dict, dict]:
    w, p = acct_clause(account)
    tasks = rows(conn, f"SELECT * FROM scheduled_tasks WHERE 1=1{w} ORDER BY account, name", p)
    parts = []
    for t in tasks:
        st = rows(conn, """SELECT COUNT(*) runs, SUM(outcome='nothing_new') nothing_new, SUM(outcome='error') errors,
                           AVG(input_tokens+cache_read_tokens+cache_create_tokens) avg_ctx, AVG(output_tokens) avg_out,
                           SUM(input_tokens+cache_read_tokens+cache_create_tokens) ctx, MAX(started_ts) last_run
                           FROM task_runs WHERE task_id=?""", [t["task_id"]])[0]
        parts.append(f"### {t['name']} (task_id={t['task_id']}, account={t['account']}, enabled={t['enabled']})\n"
                     f"cron: `{t['cron']}`  model: {t['model']}  permission: {t['permission_mode']}\n"
                     f"runs: {st['runs'] or 0}  nothing_new: {st['nothing_new'] or 0}  errors: {st['errors'] or 0}  "
                     f"avg context: {st['avg_ctx'] or 0:,.0f}  avg output: {st['avg_out'] or 0:,.0f}  "
                     f"total context: {st['ctx'] or 0:,}  last run: {st['last_run']}\n"
                     f"prompt:\n```\n{(t['prompt'] or '').strip()[:3000]}\n```\n")
    text = "\n".join(parts) or "(no scheduled tasks in the database)"
    job = {"input_tokens": est_tokens(text), "judgment": True, "cross_file": len(tasks) > 1, "n_items": len(tasks)}
    return {"account": account or "all", "tasks": text}, job


def build_weekly_review(conn, account: str | None) -> tuple[dict, dict]:
    w, p = acct_clause(account)
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).date().isoformat()
    cur = rows(conn, f"""SELECT account, COUNT(*) calls, COUNT(DISTINCT session_id) sessions,
                         SUM(input_tokens+cache_create_tokens+cache_read_tokens) ctx, SUM(cache_read_tokens) cr,
                         SUM(output_tokens) outp FROM messages WHERE ts>=?{w} GROUP BY account""", [since_iso(7)] + p)
    prev = {r["account"]: r for r in rows(conn, f"""SELECT account, SUM(input_tokens+cache_create_tokens+cache_read_tokens) ctx,
                         SUM(output_tokens) outp FROM messages WHERE ts>=? AND ts<?{w} GROUP BY account""",
                                                  [since_iso(14), since_iso(7)] + p)}
    lines = []
    for r in cur:
        pv = prev.get(r["account"], {})
        d = ((r["ctx"] or 0) - (pv.get("ctx") or 0)) / (pv.get("ctx") or 1) * 100
        lines.append(f"- {r['account']}: {r['sessions']} sessions, {r['calls']} calls, context {r['ctx'] or 0:,} "
                     f"({d:+.0f}% vs prior week), cache hit {((r['cr'] or 0) / (r['ctx'] or 1)):.0%}, "
                     f"output {r['outp'] or 0:,}")
    for m in rows(conn, f"""SELECT account, surface, MAX(CAST(value AS REAL)) peak FROM meter_snapshots
                            WHERE metric='pct_used' AND date>=?{w} GROUP BY account, surface""", [week_start] + p):
        lines.append(f"- {m['account']} meter {m['surface']}: peak {m['peak']:.0f}%")
    tr = rows(conn, f"""SELECT account, COUNT(*) runs, SUM(outcome='nothing_new') nn,
                        SUM(input_tokens+cache_read_tokens+cache_create_tokens) ctx FROM task_runs
                        WHERE started_ts>=?{w} GROUP BY account""", [since_iso(7)] + p)
    for r in tr:
        lines.append(f"- {r['account']} scheduled runs: {r['runs']} ({r['nn'] or 0} nothing_new), context {r['ctx'] or 0:,}")
    fw, fp = acct_clause(account)
    if account and account != "all":
        fw, fp = " AND account IN (?, 'shared')", [account]
    fnd = rows(conn, f"""SELECT id, account, analyzer, severity, est_tokens_wasted, title, recommendation FROM findings
                         WHERE resolved=0{fw} ORDER BY COALESCE(est_tokens_wasted,0) DESC LIMIT 40""", fp)
    ftext = "\n".join(f"- #{f['id']} [{f['severity']}] ({f['account']}/{f['analyzer']}, ~{f['est_tokens_wasted'] or 0:,} "
                      f"tokens) {f['title']} -> {f['recommendation'][:160]}" for f in fnd) or "(no open findings)"
    text = "\n".join(lines) or "(no usage this week)"
    job = {"input_tokens": est_tokens(text + ftext), "judgment": True, "cross_file": True, "n_items": len(fnd)}
    return {"account": account or "all", "week": f"week starting {week_start}\n{text}", "findings": ftext}, job


BUILDERS = {
    "claude_md_audit": build_claude_md_audit,
    "duplicate_merge": build_duplicate_merge,
    "schedule_review": build_schedule_review,
    "weekly_review": build_weekly_review,
}


# ----------------------------------------------------------------------------- prompt + claude

def render(audit: str, vars_: dict) -> str:
    tpl = (PROMPTS / f"{audit}.md").read_text()
    vars_ = {**vars_, "output_format": (PROMPTS / "_output_format.md").read_text()}
    # str.format would choke on braces in file contents; substitute by hand.
    for k, v in vars_.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


def invoke_claude(prompt: str, model_id: str, timeout: int = CLAUDE_TIMEOUT) -> str:
    """Run `claude -p` and return the assistant's text result."""
    exe = shutil.which("claude")
    if not exe:
        raise ClaudeMissing("the `claude` CLI is not on PATH; install Claude Code and log in, or use --dry-run")
    cmd = [exe, "-p", "--model", model_id, "--output-format", "json"]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"claude timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    out = proc.stdout.strip()
    try:
        env = json.loads(out)
    except json.JSONDecodeError:
        return out
    # --output-format json wraps the answer in {"type":"result","result": "..."} (plus cost/usage fields)
    if isinstance(env, dict) and "result" in env:
        return str(env["result"])
    return out


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_findings(text: str, audit: str, account: str) -> tuple[list[Finding], str]:
    """Extract {"findings": [...], "summary": ...} from model output (tolerates fences/prose)."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.S)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        m = _JSON_RE.search(candidate)
        if not m:
            raise ValueError("no JSON object in model output")
        data = json.loads(m.group(0))
    items = data.get("findings", []) if isinstance(data, dict) else data
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        acct = d.get("account") or (account if account and account != "all" else "shared")
        if acct not in ("work", "personal", "shared"):
            acct = "shared"
        out.append(Finding.from_dict({**d, "account": acct}, analyzer=f"llm:{audit}"))
    return out, (data.get("summary", "") if isinstance(data, dict) else "")


def run_audit(db_path: str, audit: str, account: str | None, *, dry_run: bool = False, model: str | None = None,
              persist: bool = True, quiet: bool = False) -> list[Finding]:
    """Build, route, invoke and (optionally) persist one audit. Returns findings."""
    if audit not in BUILDERS:
        raise ValueError(f"unknown audit {audit}; choose from {', '.join(AUDITS)}")
    conn = open_db(db_path)
    try:
        vars_, job = BUILDERS[audit](conn, account)
        if model:
            job["model"] = model
        alias = router.route(job)
        model_id = router.model_id(alias)
        prompt = render(audit, vars_)
        log = (lambda *a, **k: None) if quiet else (lambda *a, **k: print(*a, file=sys.stderr, **k))
        log(f"[{audit}] routed to {router.explain(job)} -> {model_id}; prompt ~{est_tokens(prompt):,} tokens")
        if dry_run:
            print(f"# audit: {audit}\n# model: {alias} ({model_id})\n# route: {router.explain(job)}\n")
            print(prompt)
            return []
        try:
            text = invoke_claude(prompt, model_id)
        except ClaudeMissing as e:
            log(f"[{audit}] skipped: {e}")
            return []
        findings, summary = parse_findings(text, audit, account or "all")
        if summary:
            log(f"[{audit}] summary: {summary}")
        if persist:
            upsert_findings(conn, findings)
            log(f"[{audit}] upserted {len(findings)} findings")
        return findings
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--audit", required=True, choices=AUDITS)
    ap.add_argument("--account", default="all")
    ap.add_argument("--model", choices=list(router.MODELS), help="override routing")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and chosen model; do not call claude")
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args(argv)
    fs = run_audit(args.db, args.audit, args.account, dry_run=args.dry_run, model=args.model,
                   persist=not args.no_persist)
    if args.dry_run:
        return 0
    if args.json:
        print(json.dumps([f.to_dict() for f in fs], indent=1, default=str))
    else:
        for f in sort_findings(fs):
            print(f"[{f.severity}] {f.account} {f.title}\n    {f.recommendation}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
