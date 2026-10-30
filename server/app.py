"""Claude Usage Tracker — central server (runs on Unraid in Docker).

Endpoints
  POST /ingest                 collector batches (bearer token)
  GET  /api/accounts
  GET  /api/summary?account=&days=30
  GET  /api/daily?account=&days=30
  GET  /api/sessions?account=&days=30&sort=waste&limit=50
  GET  /api/session/{id}
  GET  /api/tools?account=&days=30
  GET  /api/findings?account=&resolved=0
  POST /api/findings/{id}/resolve
  GET  /api/context?account=        latest snapshot of each instruction file
  GET  /api/tasks?account=
  GET  /                            dashboard
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import base64
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

DB_PATH = os.environ.get("TRACKER_DB", "/data/tracker.db")
TOKEN = os.environ.get("TRACKER_TOKEN", "change-me")
# Optional basic auth for the dashboard and /api/* (set both to enable). /ingest keeps the bearer
# token, /healthz stays open. Set these before exposing the tracker beyond your LAN.
UI_USER = os.environ.get("TRACKER_UI_USER", "")
UI_PASS = os.environ.get("TRACKER_UI_PASS", "")
HERE = Path(__file__).parent
DASHBOARD = HERE.parent / "dashboard"

app = FastAPI(title="Claude Usage Tracker")


@app.middleware("http")
async def ui_auth(request: Request, call_next):
    path = request.url.path
    if UI_USER and UI_PASS and not (path == "/ingest" or path == "/healthz"):
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, UI_USER) and secrets.compare_digest(pw, UI_PASS)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="claude-usage-tracker"'})
    return await call_next(request)


# ----------------------------------------------------------------------------- db

def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.executescript((HERE / "schema.sql").read_text())


@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def rows(c, sql, params=()):
    return [dict(r) for r in c.execute(sql, params).fetchall()]


init_db()


def auth(authorization: str | None = Header(default=None)):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad token")


def since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def acct_clause(account: str | None, col: str = "account") -> tuple[str, list]:
    if account and account != "all":
        return f" AND {col} = ?", [account]
    return "", []


# ----------------------------------------------------------------------------- ingest

UPSERTS = {
    "sessions": ("sessions", ["session_id", "account", "machine", "project_dir", "entrypoint", "cc_version",
                              "git_branch", "first_ts", "last_ts", "turns", "first_prompt", "source_file"],
                 ["session_id"]),
    "messages": ("messages", ["message_id", "request_id", "session_id", "account", "ts", "model", "requested_model",
                              "effort", "input_tokens", "cache_create_tokens", "cache_read_tokens", "output_tokens",
                              "thinking_tokens", "stop_reason", "is_sidechain", "parent_uuid"],
                 ["message_id", "request_id"]),
    "tool_calls": ("tool_calls", ["tool_use_id", "message_id", "session_id", "account", "ts", "tool_name",
                                  "mcp_server", "input_chars", "result_chars"], ["tool_use_id"]),
    "prompts": ("prompts", ["prompt_uuid", "session_id", "account", "ts", "chars", "is_slash", "slash_name",
                            "prompt_hash", "excerpt"], ["prompt_uuid"]),
    "context_files": ("context_files", ["account", "machine", "kind", "path", "scope", "content_hash", "chars",
                                        "est_tokens", "content", "snapshot_ts"], ["account", "path", "content_hash"]),
    "scheduled_tasks": ("scheduled_tasks", ["task_id", "account", "name", "cron", "model", "prompt",
                                            "permission_mode", "enabled", "updated_ts"], ["task_id"]),
    "task_runs": ("task_runs", ["run_id", "task_id", "account", "started_ts", "ended_ts", "model", "input_tokens",
                                "cache_read_tokens", "cache_create_tokens", "output_tokens", "turns", "outcome",
                                "summary"], ["run_id"]),
    "meter_snapshots": ("meter_snapshots", ["account", "date", "surface", "metric", "value"],
                        ["account", "date", "surface", "metric"]),
}


def upsert(c, table, cols, keys, items):
    if not items:
        return 0
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{k}=excluded.{k}" for k in cols if k not in keys)
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}"
    c.executemany(sql, [[it.get(k) for k in cols] for it in items])
    return len(items)


@app.post("/ingest", dependencies=[Depends(auth)])
def ingest(batch: dict):
    account = batch.get("account")
    if not account:
        raise HTTPException(400, "account required")
    counts = {}
    with db() as c:
        c.execute("INSERT INTO accounts(account,label,plan) VALUES(?,?,?) ON CONFLICT(account) DO UPDATE SET "
                  "label=COALESCE(excluded.label,label), plan=COALESCE(excluded.plan,plan)",
                  (account, batch.get("label"), batch.get("plan")))
        if batch.get("machine"):
            c.execute("INSERT INTO machines(machine,account,last_seen,collector_version) VALUES(?,?,?,?) "
                      "ON CONFLICT(machine) DO UPDATE SET last_seen=excluded.last_seen, collector_version=excluded.collector_version",
                      (batch["machine"], account, batch.get("sent_ts"), batch.get("collector_version")))
        # sessions: don't let a later partial batch shrink turns/first_prompt
        for key, (table, cols, keys) in UPSERTS.items():
            items = batch.get(key) or []
            if key == "sessions":
                for s in items:
                    c.execute("""INSERT INTO sessions(session_id,account,machine,project_dir,entrypoint,cc_version,git_branch,
                                 first_ts,last_ts,turns,first_prompt,source_file) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                                 ON CONFLICT(session_id) DO UPDATE SET
                                   last_ts=MAX(last_ts,excluded.last_ts), first_ts=MIN(first_ts,excluded.first_ts),
                                   turns=turns+excluded.turns, first_prompt=COALESCE(first_prompt,excluded.first_prompt),
                                   project_dir=COALESCE(project_dir,excluded.project_dir), entrypoint=COALESCE(entrypoint,excluded.entrypoint)""",
                              [s.get(k) for k in cols])
                counts[key] = len(items)
            else:
                counts[key] = upsert(c, table, cols, keys, items)
    return {"ok": True, "counts": counts}


# ----------------------------------------------------------------------------- queries

@app.get("/api/accounts")
def accounts():
    with db() as c:
        return rows(c, """SELECT a.*, (SELECT COUNT(*) FROM sessions s WHERE s.account=a.account) AS sessions,
                          (SELECT MAX(last_seen) FROM machines m WHERE m.account=a.account) AS last_seen FROM accounts a""")


@app.get("/api/summary")
def summary(account: str | None = None, days: int = 30):
    w, p = acct_clause(account)
    with db() as c:
        tot = rows(c, f"""SELECT account, COUNT(*) calls, COUNT(DISTINCT session_id) sessions,
                          SUM(input_tokens) input_tokens, SUM(cache_create_tokens) cache_create_tokens,
                          SUM(cache_read_tokens) cache_read_tokens, SUM(output_tokens) output_tokens,
                          SUM(thinking_tokens) thinking_tokens,
                          SUM(input_tokens+cache_create_tokens+cache_read_tokens) context_tokens,
                          SUM(is_sidechain) sidechain_calls
                          FROM messages WHERE ts >= ?{w} GROUP BY account""", [since(days)] + p)
        models = rows(c, f"""SELECT account, model, COUNT(*) calls, SUM(output_tokens) output_tokens,
                             SUM(input_tokens+cache_create_tokens+cache_read_tokens) context_tokens
                             FROM messages WHERE ts >= ?{w} GROUP BY account, model ORDER BY context_tokens DESC""",
                      [since(days)] + p)
        findings = rows(c, f"""SELECT account, severity, COUNT(*) n, SUM(COALESCE(est_tokens_wasted,0)) est_tokens_wasted
                               FROM findings WHERE resolved=0{w} GROUP BY account, severity""", p)
        meters = rows(c, f"""SELECT account, surface, date, value FROM meter_snapshots
                             WHERE metric='pct_used' AND date >= date('now', ?){w} ORDER BY date""",
                      [f"-{days} days"] + p)
    for t in tot:
        t["cache_hit_ratio"] = (t["cache_read_tokens"] or 0) / t["context_tokens"] if t["context_tokens"] else 0
    return {"days": days, "totals": tot, "models": models, "findings": findings, "meters": meters}


@app.get("/api/daily")
def daily(account: str | None = None, days: int = 30):
    w, p = acct_clause(account)
    with db() as c:
        return rows(c, f"""SELECT account, substr(ts,1,10) day, model, COUNT(*) calls,
                           SUM(input_tokens) input_tokens, SUM(cache_create_tokens) cache_create_tokens,
                           SUM(cache_read_tokens) cache_read_tokens, SUM(output_tokens) output_tokens
                           FROM messages WHERE ts >= ?{w} GROUP BY account, day, model ORDER BY day""",
                    [since(days)] + p)


SESSION_SORTS = {
    "waste": "(context_tokens - cache_read_tokens) DESC",
    "context": "context_tokens DESC",
    "output": "output_tokens DESC",
    "recent": "first_ts DESC",
    "cache": "cache_hit_ratio ASC",
}


@app.get("/api/sessions")
def sessions(account: str | None = None, days: int = 30, sort: str = "recent", limit: int = 50):
    w, p = acct_clause(account)
    order = SESSION_SORTS.get(sort, SESSION_SORTS["recent"])
    with db() as c:
        return rows(c, f"""SELECT * FROM v_session_totals WHERE first_ts >= ?{w} AND calls > 0
                           ORDER BY {order} LIMIT ?""", [since(days)] + p + [limit])


@app.get("/api/session/{session_id}")
def session(session_id: str):
    with db() as c:
        s = rows(c, "SELECT * FROM v_session_totals WHERE session_id=?", [session_id])
        if not s:
            raise HTTPException(404)
        msgs = rows(c, "SELECT * FROM messages WHERE session_id=? ORDER BY ts", [session_id])
        tools = rows(c, """SELECT tool_name, mcp_server, COUNT(*) calls, SUM(input_chars) input_chars,
                           SUM(COALESCE(result_chars,0)) result_chars FROM tool_calls WHERE session_id=?
                           GROUP BY tool_name ORDER BY result_chars DESC""", [session_id])
        prompts = rows(c, "SELECT ts, chars, is_slash, slash_name, excerpt FROM prompts WHERE session_id=? ORDER BY ts",
                       [session_id])
    return {"session": s[0], "messages": msgs, "tools": tools, "prompts": prompts}


@app.get("/api/tools")
def tools(account: str | None = None, days: int = 30):
    w, p = acct_clause(account)
    with db() as c:
        return rows(c, f"""SELECT account, COALESCE(mcp_server,'builtin') server, tool_name, COUNT(*) calls,
                           SUM(input_chars) input_chars, SUM(COALESCE(result_chars,0)) result_chars,
                           AVG(COALESCE(result_chars,0)) avg_result_chars
                           FROM tool_calls WHERE ts >= ?{w} GROUP BY account, server, tool_name
                           ORDER BY result_chars DESC""", [since(days)] + p)


@app.get("/api/findings")
def findings(account: str | None = None, resolved: int = 0):
    w, p = acct_clause(account)
    with db() as c:
        return rows(c, f"""SELECT * FROM findings WHERE resolved=?{w}
                           ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
                           COALESCE(est_tokens_wasted,0) DESC""", [resolved] + p)


@app.post("/api/findings/{fid}/resolve")
def resolve(fid: int, body: dict | None = None):
    with db() as c:
        c.execute("UPDATE findings SET resolved=?, updated_ts=datetime('now') WHERE id=?",
                  ((body or {}).get("resolved", 1), fid))
    return {"ok": True}


@app.get("/api/context")
def context(account: str | None = None, kind: str | None = None, content: int = 0):
    w, p = acct_clause(account)
    kw = " AND kind = ?" if kind else ""
    cols = "id, account, machine, kind, path, scope, content_hash, chars, est_tokens, snapshot_ts" + (", content" if content else "")
    with db() as c:
        return rows(c, f"""SELECT {cols} FROM context_files cf WHERE snapshot_ts = (
                               SELECT MAX(snapshot_ts) FROM context_files WHERE account=cf.account AND path=cf.path)
                           {w}{kw} ORDER BY est_tokens DESC""", p + ([kind] if kind else []))


@app.get("/api/tasks")
def tasks(account: str | None = None):
    w, p = acct_clause(account)
    with db() as c:
        t = rows(c, f"SELECT * FROM scheduled_tasks WHERE 1=1{w} ORDER BY name", p)
        runs = rows(c, f"""SELECT task_id, COUNT(*) runs, SUM(output_tokens) output_tokens,
                           SUM(input_tokens+cache_read_tokens+cache_create_tokens) context_tokens,
                           SUM(outcome='nothing_new') nothing_new, SUM(outcome='error') errors, MAX(started_ts) last_run
                           FROM task_runs WHERE 1=1{w} GROUP BY task_id""", p)
    by = {r["task_id"]: r for r in runs}
    for x in t:
        x["stats"] = by.get(x["task_id"], {})
    return t


@app.get("/api/export")
def export(account: str | None = None, days: int = 365):
    """Full dump for offline analysis elsewhere."""
    w, p = acct_clause(account)
    with db() as c:
        return {"sessions": rows(c, f"SELECT * FROM v_session_totals WHERE first_ts >= ?{w}", [since(days)] + p),
                "daily": daily(account, days), "tools": tools(account, days), "findings": findings(account, 0)}


# ----------------------------------------------------------------------------- dashboard

@app.get("/")
def index():
    return FileResponse(DASHBOARD / "index.html")


if DASHBOARD.exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD)), name="static")


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "db": DB_PATH})
