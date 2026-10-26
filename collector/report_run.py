#!/usr/bin/env python3
"""Scheduled-task run reporter.

Runs at the END of a scheduled task (cloud session) and records what that run
cost. Cloud sessions cannot reach the Unraid box, so the report goes to a relay:
a small private git repo the server pulls (see docs/scheduled-tasks.md).

Add to the end of each scheduled task's prompt:

  Finally run: python3 ~/tracker/report_run.py --task "<task name>" --account work \
      --outcome <noteworthy|nothing_new|error> --relay-dir ~/relay

The script reads this session's own transcript, totals its tokens, writes
<relay-dir>/runs/<account>/<date>-<session>.json and commits/pushes if the dir
is a git checkout. Stdlib only.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def newest_transcript() -> Path | None:
    files = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
    if not files:
        return None
    return Path(max(files, key=os.path.getmtime))


def total(path: Path) -> dict:
    seen = {}
    first = last = None
    turns = 0
    model = None
    for raw in open(path, "rb"):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts = d.get("timestamp")
        if ts:
            first = first or ts
            last = ts
        m = d.get("message") or {}
        if d.get("type") == "user" and not d.get("isMeta") and isinstance(m.get("content"), str):
            turns += 1
        if d.get("type") == "assistant" and isinstance(m, dict) and m.get("id"):
            u = m.get("usage") or {}
            rec = seen.setdefault((m["id"], d.get("requestId")), {"i": 0, "cc": 0, "cr": 0, "o": 0})
            rec["i"] = max(rec["i"], u.get("input_tokens") or 0)
            rec["cc"] = max(rec["cc"], u.get("cache_creation_input_tokens") or 0)
            rec["cr"] = max(rec["cr"], u.get("cache_read_input_tokens") or 0)
            rec["o"] = max(rec["o"], u.get("output_tokens") or 0)
            model = model or m.get("model")
    return {
        "session_id": path.stem, "started_ts": first, "ended_ts": last, "turns": turns, "model": model,
        "input_tokens": sum(r["i"] for r in seen.values()),
        "cache_create_tokens": sum(r["cc"] for r in seen.values()),
        "cache_read_tokens": sum(r["cr"] for r in seen.values()),
        "output_tokens": sum(r["o"] for r in seen.values()),
        "calls": sum(1 for r in seen.values() if r["o"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="scheduled task name (matches scheduled_tasks.name)")
    ap.add_argument("--account", required=True)
    ap.add_argument("--outcome", choices=["noteworthy", "nothing_new", "error"], default="noteworthy")
    ap.add_argument("--summary", default="", help="one line about what the run did")
    ap.add_argument("--relay-dir", default=os.environ.get("TRACKER_RELAY_DIR", "~/relay"))
    ap.add_argument("--transcript", help="override; default = newest transcript on this machine")
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()

    t = Path(a.transcript) if a.transcript else newest_transcript()
    if not t:
        sys.exit("no transcript found")
    rec = {**total(t), "task": a.task, "account": a.account, "outcome": a.outcome,
           "summary": a.summary[:500], "reported_ts": datetime.now(timezone.utc).isoformat()}
    rec["run_id"] = f"{a.account}:{rec['session_id']}"

    relay = Path(os.path.expanduser(a.relay_dir))
    out = relay / "runs" / a.account
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"{(rec['started_ts'] or 'unknown')[:10]}-{rec['session_id'][:8]}.json"
    fp.write_text(json.dumps(rec, indent=1))
    print(f"wrote {fp}: {rec['output_tokens']} out / {rec['cache_read_tokens']} cached / {rec['cache_create_tokens']} created")

    if (relay / ".git").exists() and not a.no_push:
        try:
            subprocess.run(["git", "-C", str(relay), "add", str(fp)], check=True)
            subprocess.run(["git", "-C", str(relay), "commit", "-qm", f"run: {a.account}/{a.task} {rec['outcome']}"], check=True)
            subprocess.run(["git", "-C", str(relay), "push", "-q"], check=True)
            print("pushed to relay")
        except subprocess.CalledProcessError as e:
            print(f"git failed ({e}); file is written, push it later", file=sys.stderr)


if __name__ == "__main__":
    main()
