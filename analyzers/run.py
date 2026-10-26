"""Run the analyzers and upsert findings.

Usage
  python -m analyzers.run --db PATH [--account work|personal|all] [--rules] [--llm]
                          [--analyzer name] [--dry-run] [--json]

--rules runs the rule-based analyzers (default when neither flag is given).
--llm   runs the LLM-backed audits in agents/ (claude_md_audit, duplicate_merge,
        schedule_review) through `claude -p`; combine with --dry-run to print
        the prompts without invoking claude.
--dry-run computes findings but does not write them to the DB.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys

from . import RULE_ANALYZERS
from .common import Finding, open_db, sort_findings, table, upsert_findings

LLM_AUDITS = ("claude_md_audit", "duplicate_merge", "schedule_review")


def run_rules(conn, account: str | None, names: list[str]) -> list[Finding]:
    out: list[Finding] = []
    for name in names:
        mod = importlib.import_module(f"analyzers.{name}")
        try:
            fs = mod.run(conn, account)
        except Exception as e:  # noqa: BLE001 - keep other analyzers running
            print(f"[{name}] failed: {e!r}", file=sys.stderr)
            continue
        out.extend(fs)
    return out


def run_llm(db_path: str, account: str | None, names: list[str], dry_run: bool) -> list[Finding]:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from agents.run_agent import run_audit  # noqa: WPS433 - local package
    out: list[Finding] = []
    for name in names:
        out.extend(run_audit(db_path, name, account, dry_run=dry_run, persist=False))
    return out


def print_table(findings: list[Finding]) -> None:
    data = [[f.severity, f.account, f.analyzer, f.est_tokens_wasted, f.title, f.recommendation]
            for f in sort_findings(findings)]
    print(table(["sev", "account", "analyzer", "est_wasted", "title", "recommendation"], data, max_width=70))
    total = sum(f.est_tokens_wasted or 0 for f in findings)
    print(f"\n{len(findings)} findings; est tokens wasted (sum): {total:,}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--account", default="all")
    ap.add_argument("--rules", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--analyzer", action="append", help="restrict to this analyzer/audit (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.rules and not args.llm:
        args.rules = True

    selected = args.analyzer or []
    rule_names = [n for n in RULE_ANALYZERS if not selected or n in selected]
    llm_names = [n for n in LLM_AUDITS if not selected or n in selected]
    unknown = [n for n in selected if n not in RULE_ANALYZERS and n not in LLM_AUDITS]
    if unknown:
        ap.error(f"unknown analyzer(s): {', '.join(unknown)}; known: {', '.join(RULE_ANALYZERS + LLM_AUDITS)}")

    findings: list[Finding] = []
    conn = open_db(args.db)
    try:
        if args.rules:
            findings += run_rules(conn, args.account, rule_names)
        if args.llm:
            findings += run_llm(args.db, args.account, llm_names, args.dry_run)
        if not args.dry_run:
            n = upsert_findings(conn, findings)
            print(f"upserted {n} findings into {args.db}", file=sys.stderr)
    finally:
        conn.close()

    if args.json:
        print(json.dumps([f.to_dict() for f in sort_findings(findings)], indent=1, default=str))
    else:
        print_table(findings)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # piped into head
        sys.exit(0)
