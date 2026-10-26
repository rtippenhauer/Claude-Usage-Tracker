#!/usr/bin/env python3
"""Claude Usage Tracker — per-machine collector.

Stdlib only (Python 3.11+). Reads Claude Code transcripts and instruction files,
dedupes, tags with the account, and ships batches to the Unraid server
(or spools them locally when offline).

Usage:
  collect.py run            # incremental: new transcript lines + changed context files
  collect.py run --full     # re-read everything (server dedupes)
  collect.py tasks --from tasks.json   # ship a scheduled-task export
  collect.py meter --surface claude_ai --pct 42 [--resets "2026-10-26T18:00"]
  collect.py flush          # push spooled batches
  collect.py --dry-run run  # print what would be shipped
"""
from __future__ import annotations

import argparse
import fnmatch
import glob
import hashlib
import json
import os
import platform
import re
import socket
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

COLLECTOR_VERSION = "0.1.0"
STATE_DIR = Path("~/.claude-usage-tracker").expanduser()
STATE_FILE = STATE_DIR / "state.json"
BATCH_LIMIT = 500  # messages per POST


# ----------------------------------------------------------------------------- config

def load_config(path: str | None) -> dict:
    p = Path(path) if path else Path(__file__).with_name("collector.toml")
    if not p.exists():
        sys.exit(f"config not found: {p} (copy collector.example.toml)")
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("server", {})
    cfg.setdefault("sources", {})
    cfg.setdefault("privacy", {})
    return cfg


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"files": {}, "context_hashes": {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()


def est_tokens(text: str) -> int:
    # Rough: ~4 chars/token for English prose, ~3.3 for code/markdown with symbols.
    return int(len(text) / 3.6) + 1


# ----------------------------------------------------------------------------- transcripts

TOOL_RE = re.compile(r"^mcp__([^_]+(?:_[^_]+)*?)__(.+)$")


def mcp_server_of(tool_name: str) -> str | None:
    if tool_name.startswith("mcp__"):
        rest = tool_name[5:]
        # server names may contain underscores; the separator is a double underscore
        if "__" in rest:
            return rest.split("__", 1)[0]
    return None


def parse_transcript(path: Path, account: str, machine: str, start_offset: int, store_excerpts: bool):
    """Yield (session, messages, tool_calls, prompts, new_offset) from a JSONL file."""
    session = None
    messages: dict[tuple, dict] = {}
    tool_calls: dict[str, dict] = {}
    results: dict[str, int] = {}
    prompts: list[dict] = []
    seen_prompt_hashes = set()

    with open(path, "rb") as f:
        f.seek(start_offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # partial line still being written; pick it up next run
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            sid = d.get("sessionId")
            if not sid:
                continue
            if session is None:
                session = {
                    "session_id": sid, "account": account, "machine": machine,
                    "project_dir": d.get("cwd"), "entrypoint": d.get("entrypoint"),
                    "cc_version": d.get("version"), "git_branch": d.get("gitBranch"),
                    "first_ts": d.get("timestamp"), "last_ts": d.get("timestamp"),
                    "turns": 0, "first_prompt": None, "source_file": str(path),
                }
            ts = d.get("timestamp")
            if ts:
                session["first_ts"] = min(session["first_ts"] or ts, ts)
                session["last_ts"] = max(session["last_ts"] or ts, ts)
            for k in ("cwd", "entrypoint", "version", "gitBranch"):
                if d.get(k) and not session.get({"cwd": "project_dir", "entrypoint": "entrypoint",
                                                  "version": "cc_version", "gitBranch": "git_branch"}[k]):
                    session[{"cwd": "project_dir", "entrypoint": "entrypoint",
                             "version": "cc_version", "gitBranch": "git_branch"}[k]] = d[k]

            msg = d.get("message")
            if t == "user" and isinstance(msg, dict) and not d.get("isMeta"):
                content = msg.get("content")
                text = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    # tool results come back as user turns; capture their size
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            c = b.get("content")
                            size = len(c) if isinstance(c, str) else len(json.dumps(c)) if c else 0
                            results[b.get("tool_use_id", "")] = size
                        elif isinstance(b, dict) and b.get("type") == "text":
                            text = (text or "") + b.get("text", "")
                if text:
                    # strip injected reminders so only the human's words are measured
                    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S).strip()
                if text and not text.startswith("<") and d.get("uuid"):
                    norm = re.sub(r"\s+", " ", text.strip().lower())
                    h = sha1(norm)
                    is_slash = text.lstrip().startswith("/")
                    slash = text.lstrip().split()[0][1:] if is_slash else None
                    prompts.append({
                        "prompt_uuid": d["uuid"], "session_id": sid, "account": account, "ts": ts,
                        "chars": len(text), "is_slash": int(is_slash), "slash_name": slash,
                        "prompt_hash": h, "excerpt": text[:200] if store_excerpts else None,
                    })
                    session["turns"] += 1
                    if session["first_prompt"] is None and store_excerpts:
                        session["first_prompt"] = text[:300]

            elif t == "assistant" and isinstance(msg, dict):
                usage = msg.get("usage") or {}
                mid = msg.get("id")
                rid = d.get("requestId") or ""
                if not mid:
                    continue
                key = (mid, rid)
                rec = messages.get(key)
                if rec is None:
                    rec = {
                        "message_id": mid, "request_id": rid, "session_id": sid, "account": account,
                        "ts": ts, "model": msg.get("model"), "requested_model": d.get("requestedModel"),
                        "effort": d.get("effort"), "stop_reason": msg.get("stop_reason"),
                        "is_sidechain": int(bool(d.get("isSidechain"))), "parent_uuid": d.get("parentUuid"),
                        "input_tokens": 0, "cache_create_tokens": 0, "cache_read_tokens": 0,
                        "output_tokens": 0, "thinking_tokens": 0,
                    }
                    messages[key] = rec
                # Streamed messages repeat usage per content block; keep the max, not the sum.
                rec["input_tokens"] = max(rec["input_tokens"], usage.get("input_tokens") or 0)
                rec["cache_create_tokens"] = max(rec["cache_create_tokens"], usage.get("cache_creation_input_tokens") or 0)
                rec["cache_read_tokens"] = max(rec["cache_read_tokens"], usage.get("cache_read_input_tokens") or 0)
                rec["output_tokens"] = max(rec["output_tokens"], usage.get("output_tokens") or 0)
                rec["thinking_tokens"] = max(rec["thinking_tokens"],
                                             (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
                if msg.get("stop_reason"):
                    rec["stop_reason"] = msg["stop_reason"]
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        name = b.get("name", "")
                        tool_calls[b["id"]] = {
                            "tool_use_id": b["id"], "message_id": mid, "session_id": sid, "account": account,
                            "ts": ts, "tool_name": name, "mcp_server": mcp_server_of(name),
                            "input_chars": len(json.dumps(b.get("input") or {})), "result_chars": None,
                        }
        new_offset = f.tell()

    for tid, size in results.items():
        if tid in tool_calls:
            tool_calls[tid]["result_chars"] = size
    # Drop zero-token helper records (side models that log but don't bill).
    msgs = [m for m in messages.values() if (m["output_tokens"] + m["input_tokens"] + m["cache_read_tokens"]) > 0]
    return session, msgs, list(tool_calls.values()), prompts, new_offset


# ----------------------------------------------------------------------------- context files

def classify(path: str) -> tuple[str, str]:
    p = path.replace("\\", "/")
    if p.endswith("CLAUDE.md") or p.endswith("CLAUDE.local.md"):
        scope = "global" if "/.claude/CLAUDE" in p else f"project:{os.path.dirname(p)}"
        return "claude_md", scope
    if "/commands/" in p:
        return "command", "global" if "/.claude/commands/" in p else f"project:{p.split('/.claude/')[0]}"
    if p.endswith("SKILL.md"):
        return "skill", "global"
    if "/agents/" in p:
        return "agent", "global"
    return "other", "global"


def collect_context_files(cfg: dict, state: dict, account: str, machine: str, project_dirs: set[str]):
    patterns = list(cfg["sources"].get("claude_md_globs", []))
    if cfg["sources"].get("discover_project_claude_md", True):
        for d in project_dirs:
            patterns += [f"{d}/CLAUDE.md", f"{d}/CLAUDE.local.md", f"{d}/.claude/CLAUDE.md",
                         f"{d}/.claude/commands/**/*.md", f"{d}/.claude/agents/**/*.md", f"{d}/.claude/settings.json"]
    out = []
    store = cfg["privacy"].get("store_context_content", True)
    seen = set()
    for pat in patterns:
        for fp in glob.glob(os.path.expanduser(pat), recursive=True):
            if fp in seen or not os.path.isfile(fp):
                continue
            seen.add(fp)
            try:
                text = Path(fp).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            h = sha1(text)
            if state["context_hashes"].get(fp) == h:
                continue
            kind, scope = classify(fp)
            if fp.endswith("settings.json"):
                kind = "settings"
            out.append({
                "account": account, "machine": machine, "kind": kind, "path": fp, "scope": scope,
                "content_hash": h, "chars": len(text), "est_tokens": est_tokens(text),
                "content": text if store else None, "snapshot_ts": now_iso(),
            })
            state["context_hashes"][fp] = h
    return out


# ----------------------------------------------------------------------------- shipping

def post(cfg: dict, endpoint: str, payload: dict, dry_run: bool) -> bool:
    url = (cfg["server"].get("url") or "").rstrip("/")
    body = json.dumps(payload).encode()
    if dry_run or not url:
        spool(cfg, endpoint, payload)
        if dry_run:
            print(f"[dry-run] {endpoint}: {len(body):,} bytes")
        return True
    req = urllib.request.Request(f"{url}{endpoint}", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {cfg['server'].get('token','')}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        print(f"server unreachable ({e}); spooling", file=sys.stderr)
        spool(cfg, endpoint, payload)
        return False


def spool(cfg: dict, endpoint: str, payload: dict) -> None:
    d = Path(os.path.expanduser(cfg["server"].get("spool_dir") or str(STATE_DIR / "spool")))
    d.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}{endpoint.replace('/', '_')}.json"
    (d / name).write_text(json.dumps({"endpoint": endpoint, "payload": payload}))


def flush(cfg: dict) -> None:
    d = Path(os.path.expanduser(cfg["server"].get("spool_dir") or str(STATE_DIR / "spool")))
    for fp in sorted(d.glob("*.json")) if d.exists() else []:
        item = json.loads(fp.read_text())
        url = (cfg["server"].get("url") or "").rstrip("/")
        if not url:
            print("no server url configured; nothing to flush")
            return
        req = urllib.request.Request(f"{url}{item['endpoint']}", data=json.dumps(item["payload"]).encode(),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "Authorization": f"Bearer {cfg['server'].get('token','')}"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
            fp.unlink()
            print(f"flushed {fp.name}")
        except Exception as e:  # noqa: BLE001
            print(f"still unreachable: {e}")
            return


# ----------------------------------------------------------------------------- commands

def cmd_run(cfg: dict, args) -> None:
    account = cfg["account"]["name"]
    machine = socket.gethostname()
    state = load_state() if not args.full else {"files": {}, "context_hashes": {}}
    store_excerpts = cfg["privacy"].get("store_prompt_excerpts", True)
    excludes = cfg["privacy"].get("exclude_project_globs", [])

    files = []
    for root in cfg["sources"].get("transcript_dirs", ["~/.claude/projects"]):
        files += glob.glob(os.path.join(os.path.expanduser(root), "**", "*.jsonl"), recursive=True)

    sessions, messages, tools, prompts = [], [], [], []
    project_dirs: set[str] = set()
    for fp in sorted(files):
        st = state["files"].get(fp, {"offset": 0, "size": 0})
        size = os.path.getsize(fp)
        if size == st["offset"]:
            continue
        if size < st["offset"]:
            st["offset"] = 0  # truncated/rewritten
        sess, msgs, tcs, prs, new_off = parse_transcript(Path(fp), account, machine, st["offset"], store_excerpts)
        state["files"][fp] = {"offset": new_off, "size": size}
        if not sess:
            continue
        if any(fnmatch.fnmatch(sess.get("project_dir") or "", g) for g in excludes):
            continue
        if sess.get("project_dir"):
            project_dirs.add(sess["project_dir"])
        sessions.append(sess); messages += msgs; tools += tcs; prompts += prs

    ctx = collect_context_files(cfg, state, account, machine, project_dirs)

    header = {"account": account, "label": cfg["account"].get("label"), "plan": cfg["account"].get("plan"),
              "machine": machine, "collector_version": COLLECTOR_VERSION, "os": platform.platform(),
              "sent_ts": now_iso()}
    ok = True
    # ship in chunks so a big backfill doesn't make one giant request
    for i in range(0, max(len(messages), 1), BATCH_LIMIT):
        chunk_msgs = messages[i:i + BATCH_LIMIT]
        mids = {m["message_id"] for m in chunk_msgs}
        sids = {m["session_id"] for m in chunk_msgs} if chunk_msgs else {s["session_id"] for s in sessions}
        payload = {
            **header,
            "sessions": [s for s in sessions if s["session_id"] in sids] if i == 0 or chunk_msgs else [],
            "messages": chunk_msgs,
            "tool_calls": [t for t in tools if t["message_id"] in mids],
            "prompts": [p for p in prompts if p["session_id"] in sids] if i == 0 else [],
            "context_files": ctx if i == 0 else [],
        }
        if i == 0:
            payload["sessions"] = sessions  # all sessions on the first batch
        ok &= post(cfg, "/ingest", payload, args.dry_run)
    if not args.dry_run:
        save_state(state)
    print(f"{account}@{machine}: {len(sessions)} sessions, {len(messages)} messages, "
          f"{len(tools)} tool calls, {len(prompts)} prompts, {len(ctx)} context files "
          f"{'shipped' if ok else 'spooled'}")


def cmd_tasks(cfg: dict, args) -> None:
    """Ship a scheduled-task export. Expected shape: a list of
    {task_id,name,cron,model,prompt,permission_mode,enabled}. Produce it from the
    Claude app's scheduled-task list (see docs/scheduled-tasks.md)."""
    src = Path(os.path.expanduser(args.source or cfg["sources"].get("tasks_export", "")))
    items = json.loads(src.read_text())
    account = cfg["account"]["name"]
    payload = {"account": account, "machine": socket.gethostname(), "sent_ts": now_iso(),
               "scheduled_tasks": [{**t, "account": account, "updated_ts": now_iso()} for t in items],
               "context_files": [{
                   "account": account, "machine": "cloud", "kind": "scheduled_task", "path": f"task:{t.get('name') or t.get('task_id')}",
                   "scope": "cloud", "content_hash": sha1(t.get("prompt", "")), "chars": len(t.get("prompt", "")),
                   "est_tokens": est_tokens(t.get("prompt", "")), "content": t.get("prompt"), "snapshot_ts": now_iso(),
               } for t in items]}
    post(cfg, "/ingest", payload, args.dry_run)
    print(f"shipped {len(items)} scheduled tasks")


def cmd_meter(cfg: dict, args) -> None:
    account = cfg["account"]["name"]
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    rows = [{"account": account, "date": date, "surface": args.surface, "metric": "pct_used", "value": str(args.pct)}]
    if args.resets:
        rows.append({"account": account, "date": date, "surface": args.surface, "metric": "resets_at", "value": args.resets})
    if args.note:
        rows.append({"account": account, "date": date, "surface": args.surface, "metric": "note", "value": args.note})
    post(cfg, "/ingest", {"account": account, "machine": socket.gethostname(), "sent_ts": now_iso(),
                          "meter_snapshots": rows}, args.dry_run)
    print(f"recorded {args.surface} {args.pct}% for {account} on {date}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--full", action="store_true")
    t = sub.add_parser("tasks"); t.add_argument("--from", dest="source")
    m = sub.add_parser("meter"); m.add_argument("--surface", default="claude_ai"); m.add_argument("--pct", type=float, required=True)
    m.add_argument("--resets"); m.add_argument("--date"); m.add_argument("--note")
    sub.add_parser("flush")
    args = ap.parse_args()
    cfg = load_config(args.config)
    {"run": cmd_run, "tasks": cmd_tasks, "meter": cmd_meter, "flush": lambda c, a: flush(c)}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
