"""Dig into token waste from the command line.

Usage
  python -m analyzers.investigate --db PATH [--account A] [--days N] COMMAND ...

Commands
  top-sessions --by waste|context|cache|output [--limit N]
  session <session_id>          per-call timeline; '!' marks cache_create spikes
  tools                         tool usage ranked by result size
  context-files                 instruction files, sizes and 30-day token cost
  compare                       work vs personal side by side
  findings [--resolved]         open findings from the DB

Plain text, stdlib only.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from .common import accounts_in, acct_clause, open_db, rows, since_iso, table

SORTS = {
    "waste": "(context_tokens - cache_read_tokens) DESC",
    "context": "context_tokens DESC",
    "cache": "cache_hit_ratio ASC",
    "output": "output_tokens DESC",
}
SPIKE_FACTOR = 2.0        # cache_create > factor x session median (and > 5k) is marked
SPIKE_MIN = 5_000


def cmd_top_sessions(conn, args) -> None:
    w, p = acct_clause(args.account)
    data = rows(conn, f"""SELECT session_id, account, project_dir, entrypoint, first_ts, calls, context_tokens,
                          cache_read_tokens, cache_create_tokens, input_tokens, output_tokens, cache_hit_ratio, models
                          FROM v_session_totals WHERE calls > 0 AND first_ts >= ?{w}
                          ORDER BY {SORTS[args.by]} LIMIT ?""", [since_iso(args.days)] + p + [args.limit])
    print(table(["session", "acct", "project", "started", "calls", "context", "cache_read", "cache_create", "input",
                 "output", "hit%", "models"],
                [[r["session_id"][:12], r["account"], (r["project_dir"] or "").rsplit("/", 1)[-1][:24],
                  (r["first_ts"] or "")[:10], r["calls"], r["context_tokens"], r["cache_read_tokens"],
                  r["cache_create_tokens"], r["input_tokens"], r["output_tokens"],
                  f"{(r['cache_hit_ratio'] or 0) * 100:.0f}", (r["models"] or "")[:40]] for r in data]))


def cmd_session(conn, args) -> None:
    sid = args.session_id
    s = rows(conn, "SELECT * FROM v_session_totals WHERE session_id LIKE ?", [sid + "%"])
    if not s:
        sys.exit(f"no session matching {sid}")
    if len(s) > 1:
        sys.exit("ambiguous prefix: " + ", ".join(x["session_id"] for x in s[:10]))
    s = s[0]
    sid = s["session_id"]
    print(f"session {sid}  account={s['account']}  project={s['project_dir']}  entry={s['entrypoint']}")
    print(f"started {s['first_ts']}  ended {s['last_ts']}  turns={s['turns']}  calls={s['calls']}  "
          f"context={s['context_tokens']:,}  cache_read={s['cache_read_tokens'] or 0:,}  "
          f"cache_create={s['cache_create_tokens'] or 0:,}  output={s['output_tokens'] or 0:,}  "
          f"hit={((s['cache_hit_ratio'] or 0) * 100):.0f}%")
    if s["first_prompt"]:
        print(f"first prompt: {s['first_prompt'][:200]!r}")
    msgs = rows(conn, """SELECT message_id, ts, model, input_tokens, cache_create_tokens, cache_read_tokens,
                         output_tokens, thinking_tokens, stop_reason, is_sidechain FROM messages
                         WHERE session_id=? ORDER BY ts""", [sid])
    tools = rows(conn, "SELECT message_id, tool_name, result_chars FROM tool_calls WHERE session_id=?", [sid])
    by_msg: dict[str, list[str]] = {}
    for t in tools:
        name = t["tool_name"].replace("mcp__", "")
        if t["result_chars"] and t["result_chars"] > 20_000:
            name += f"({t['result_chars'] // 1000}k)"
        by_msg.setdefault(t["message_id"], []).append(name)
    creates = sorted(m["cache_create_tokens"] or 0 for m in msgs)
    median = creates[len(creates) // 2] if creates else 0
    data = []
    for i, m in enumerate(msgs, 1):
        cc = m["cache_create_tokens"] or 0
        spike = "!" if cc >= SPIKE_MIN and cc > SPIKE_FACTOR * max(median, 1) else ""
        ctx = (m["input_tokens"] or 0) + cc + (m["cache_read_tokens"] or 0)
        data.append([i, (m["ts"] or "")[11:19], ("sub " if m["is_sidechain"] else "") + (m["model"] or "")[:22],
                     ctx, m["cache_read_tokens"], f"{spike}{cc:,}", m["input_tokens"], m["output_tokens"],
                     m["stop_reason"], ", ".join(by_msg.get(m["message_id"], []))[:50]])
    print()
    print(table(["#", "time", "model", "context", "cache_read", "cache_create", "input", "output", "stop", "tools"],
                data))
    n_spikes = sum(1 for r in data if str(r[5]).startswith("!"))
    print(f"\n'!' marks cache_create spikes (>= {SPIKE_MIN:,} and > {SPIKE_FACTOR}x session median {median:,}); "
          f"{n_spikes} spike(s). A spike after call 1 means the cached prefix changed.")


def cmd_tools(conn, args) -> None:
    w, p = acct_clause(args.account)
    data = rows(conn, f"""SELECT account, COALESCE(mcp_server,'builtin') server, tool_name, COUNT(*) calls,
                          SUM(input_chars) input_chars, SUM(COALESCE(result_chars,0)) result_chars,
                          AVG(COALESCE(result_chars,0)) avg_result, MAX(COALESCE(result_chars,0)) max_result,
                          SUM(COALESCE(result_chars,0) > 20000) huge
                          FROM tool_calls WHERE ts >= ?{w} GROUP BY account, server, tool_name
                          ORDER BY result_chars DESC LIMIT ?""", [since_iso(args.days)] + p + [args.limit])
    print(table(["acct", "server", "tool", "calls", "input_chars", "result_chars", "~tokens", "avg_result",
                 "max_result", ">20k"],
                [[r["account"], r["server"], r["tool_name"][:40], r["calls"], r["input_chars"], r["result_chars"],
                  int(r["result_chars"] / 3.6), int(r["avg_result"]), r["max_result"], r["huge"]] for r in data]))


def cmd_context_files(conn, args) -> None:
    from .context_bloat import _sessions_by_scope, _sessions_for  # reuse the same attribution logic
    from .common import latest_context_files
    files = latest_context_files(conn, args.account, with_content=False)
    cache: dict[str, tuple[int, dict[str, int]]] = {}
    data = []
    total = 0
    for cf in files:
        if cf["account"] not in cache:
            cache[cf["account"]] = _sessions_by_scope(conn, cf["account"])
        n = _sessions_for(cf, *cache[cf["account"]])
        cost = (cf["est_tokens"] or 0) * n
        total += cost
        data.append([cf["account"], cf["kind"], (cf["scope"] or "")[:30], cf["path"][-50:], cf["chars"],
                     cf["est_tokens"], n, cost, (cf["snapshot_ts"] or "")[:10]])
    data.sort(key=lambda r: -r[7])
    print(table(["acct", "kind", "scope", "path", "chars", "~tokens", "sessions/30d", "tokens/30d", "snapshot"], data))
    print(f"\nestimated instruction-file tokens loaded per 30 days (before caching): {total:,}")


def cmd_compare(conn, args) -> None:
    accts = accounts_in(conn, None)
    if not accts:
        sys.exit("no accounts in DB")
    since = since_iso(args.days)
    cols: dict[str, dict[str, object]] = {}
    for a in accts:
        m = rows(conn, """SELECT COUNT(*) calls, COUNT(DISTINCT session_id) sessions,
                          SUM(input_tokens) inp, SUM(cache_create_tokens) cc, SUM(cache_read_tokens) cr,
                          SUM(output_tokens) outp, SUM(thinking_tokens) think, SUM(is_sidechain) side,
                          COUNT(DISTINCT substr(ts,1,10)) active_days
                          FROM messages WHERE account=? AND ts>=?""", [a, since])[0]
        ctx = (m["inp"] or 0) + (m["cc"] or 0) + (m["cr"] or 0)
        fam = rows(conn, """SELECT model, SUM(input_tokens+cache_create_tokens+cache_read_tokens) ctx
                            FROM messages WHERE account=? AND ts>=? GROUP BY model ORDER BY ctx DESC LIMIT 3""",
                   [a, since])
        tasks = rows(conn, "SELECT COUNT(*) n FROM scheduled_tasks WHERE account=? AND enabled=1", [a])[0]["n"]
        runs = rows(conn, """SELECT COUNT(*) n, SUM(input_tokens+cache_read_tokens+cache_create_tokens) ctx
                             FROM task_runs WHERE account=? AND started_ts>=?""", [a, since])[0]
        meter = rows(conn, """SELECT surface, MAX(CAST(value AS REAL)) peak, AVG(CAST(value AS REAL)) avg
                              FROM meter_snapshots WHERE account=? AND metric='pct_used' AND date>=?
                              GROUP BY surface""", [a, since[:10]])
        ctxf = rows(conn, """SELECT COUNT(*) n, SUM(est_tokens) toks FROM context_files cf
                             WHERE account=? AND snapshot_ts=(SELECT MAX(snapshot_ts) FROM context_files
                                                              WHERE account=cf.account AND path=cf.path)""", [a])[0]
        findings = rows(conn, """SELECT COUNT(*) n, SUM(COALESCE(est_tokens_wasted,0)) w FROM findings
                                 WHERE account=? AND resolved=0""", [a])[0]
        cols[a] = {
            "sessions": m["sessions"], "calls": m["calls"], "active days": m["active_days"],
            "context tokens": ctx, "  input (uncached)": m["inp"], "  cache create": m["cc"], "  cache read": m["cr"],
            "cache hit ratio": f"{(m['cr'] or 0) / ctx * 100:.0f}%" if ctx else "-",
            "output tokens": m["outp"], "thinking tokens": m["think"], "sidechain calls": m["side"],
            "top models": "; ".join(f"{(f['model'] or '?')[:20]} {f['ctx'] or 0:,}" for f in fam) or "-",
            "scheduled tasks": tasks, "task runs": runs["n"], "task run context": runs["ctx"] or 0,
            "meter peak/avg": "; ".join(f"{x['surface']} {x['peak']:.0f}/{x['avg']:.0f}%" for x in meter) or "-",
            "instruction files": ctxf["n"], "instruction tokens": ctxf["toks"] or 0,
            "open findings": findings["n"], "est wasted (findings)": findings["w"],
        }
    keys = list(next(iter(cols.values())).keys())
    print(f"last {args.days} days")
    print(table(["metric"] + accts, [[k] + [cols[a][k] for a in accts] for k in keys]))


def cmd_findings(conn, args) -> None:
    w, p = acct_clause(args.account)
    data = rows(conn, f"""SELECT id, account, analyzer, severity, est_tokens_wasted, title, updated_ts FROM findings
                          WHERE resolved=?{w}
                          ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
                          COALESCE(est_tokens_wasted,0) DESC LIMIT ?""", [1 if args.resolved else 0] + p + [args.limit])
    print(table(["id", "acct", "analyzer", "sev", "est_wasted", "title", "updated"],
                [[r["id"], r["account"], r["analyzer"], r["severity"], r["est_tokens_wasted"], r["title"],
                  (r["updated_ts"] or "")[:10]] for r in data], max_width=90))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--account", default="all")
    ap.add_argument("--days", type=int, default=30)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("top-sessions"); t.add_argument("--by", choices=SORTS, default="waste")
    t.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("session"); s.add_argument("session_id")
    tl = sub.add_parser("tools"); tl.add_argument("--limit", type=int, default=30)
    sub.add_parser("context-files")
    sub.add_parser("compare")
    f = sub.add_parser("findings"); f.add_argument("--resolved", action="store_true"); f.add_argument("--limit", type=int, default=50)
    args = ap.parse_args(argv)
    conn = open_db(args.db)
    try:
        {"top-sessions": cmd_top_sessions, "session": cmd_session, "tools": cmd_tools,
         "context-files": cmd_context_files, "compare": cmd_compare, "findings": cmd_findings}[args.cmd](conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
