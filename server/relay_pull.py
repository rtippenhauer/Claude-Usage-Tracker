#!/usr/bin/env python3
"""Pull scheduled-task run reports from the relay repo into the store.

Run on the Unraid box (cron, or the analyzers container):
  python3 server/relay_pull.py --relay /relay --db /data/tracker.db [--git-pull]

Reads relay/runs/<account>/*.json written by collector/report_run.py and
relay/tasks/<account>.json (task definitions, optional), upserts into
task_runs / scheduled_tasks / context_files. Idempotent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--git-pull", action="store_true")
    a = ap.parse_args()
    relay = Path(a.relay)
    if a.git_pull and (relay / ".git").exists():
        subprocess.run(["git", "-C", str(relay), "pull", "-q", "--ff-only"], check=False)

    c = sqlite3.connect(a.db)
    n_runs = n_tasks = 0
    for fp in sorted(relay.glob("runs/*/*.json")):
        r = json.loads(fp.read_text())
        c.execute("""INSERT INTO task_runs(run_id,task_id,account,started_ts,ended_ts,model,input_tokens,cache_read_tokens,
                     cache_create_tokens,output_tokens,turns,outcome,summary) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(run_id) DO UPDATE SET outcome=excluded.outcome, summary=excluded.summary,
                     output_tokens=excluded.output_tokens, ended_ts=excluded.ended_ts""",
                  (r["run_id"], r.get("task"), r["account"], r.get("started_ts"), r.get("ended_ts"), r.get("model"),
                   r.get("input_tokens", 0), r.get("cache_read_tokens", 0), r.get("cache_create_tokens", 0),
                   r.get("output_tokens", 0), r.get("turns"), r.get("outcome"), r.get("summary")))
        n_runs += 1
    for fp in sorted(relay.glob("tasks/*.json")):
        account = fp.stem
        now = datetime.now(timezone.utc).isoformat()
        for t in json.loads(fp.read_text()):
            tid = t.get("task_id") or t.get("name")
            c.execute("""INSERT INTO scheduled_tasks(task_id,account,name,cron,model,prompt,permission_mode,enabled,updated_ts)
                         VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET name=excluded.name, cron=excluded.cron,
                         model=excluded.model, prompt=excluded.prompt, permission_mode=excluded.permission_mode,
                         enabled=excluded.enabled, updated_ts=excluded.updated_ts""",
                      (tid, account, t.get("name"), t.get("cron"), t.get("model"), t.get("prompt"),
                       t.get("permission_mode"), int(t.get("enabled", 1)), now))
            prompt = t.get("prompt") or ""
            h = hashlib.sha1(prompt.encode()).hexdigest()
            c.execute("""INSERT OR IGNORE INTO context_files(account,machine,kind,path,scope,content_hash,chars,est_tokens,content,snapshot_ts)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (account, "cloud", "scheduled_task", f"task:{t.get('name') or tid}", "cloud", h, len(prompt),
                       int(len(prompt) / 3.6) + 1, prompt, now))
            n_tasks += 1
    c.commit()
    c.close()

    # Collector spool files pushed by machines in git mode (work PC with no LAN access).
    # Each is a complete /ingest payload; ingest it through the same code path the API uses,
    # then delete it from the relay so the repo doesn't grow forever.
    import os, sys
    os.environ.setdefault("TRACKER_DB", a.db)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    n_spool = 0
    spool_files = sorted(relay.glob("spool/*/*.json"))
    if spool_files:
        from server.app import ingest  # noqa: E402
        for fp in spool_files:
            try:
                item = json.loads(fp.read_text())
                ingest(item["payload"])
                n_spool += 1
            except Exception as e:  # noqa: BLE001
                print(f"spool {fp.name}: ingest failed: {e}")
                continue
            try:
                fp.unlink()
            except OSError as e:
                print(f"spool {fp.name}: ingested but could not delete ({e}); mount /relay read-write")
        if n_spool and (relay / ".git").exists() and a.git_pull:
            subprocess.run(["git", "-C", str(relay), "add", "-A", "spool"], check=False)
            subprocess.run(["git", "-C", str(relay), "commit", "-qm", f"ingested {n_spool} spool files"], check=False)
            subprocess.run(["git", "-C", str(relay), "push", "-q"], check=False)
    from server.task_match import run as match_runs  # noqa: E402
    n_match = match_runs(a.db, None, 20)
    print(f"relay: {n_runs} runs, {n_tasks} task definitions, {n_spool} spool batches, {n_match} local runs linked to tasks")


if __name__ == "__main__":
    main()
