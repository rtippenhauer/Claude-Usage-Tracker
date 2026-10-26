# Claude Usage Tracker

Track Claude usage across two accounts (work, personal), keep the data offline on your Unraid
box, find wasted tokens, and get concrete fixes: schedule and model changes for scheduled tasks,
CLAUDE.md trimming, duplicated instructions across jobs.

```
collector/      runs on each machine (stdlib Python 3.11+); ships transcripts + instruction files
server/         FastAPI + SQLite, runs on Unraid in Docker; ingest API, query API, relay puller
analyzers/      rule-based checks, nightly, no LLM; plus investigate.py for digging in
agents/         LLM-backed audits via `claude -p`, routed to Haiku / Sonnet / Opus by complexity
dashboard/      single-file review page served at /
deploy/         Dockerfile + docker-compose for Unraid
docs/           analyzers, scheduled-task relay, agents
```

## Quick start

**Unraid**

```
mkdir -p /mnt/user/appdata/claude-usage-tracker && cd /mnt/user/appdata/claude-usage-tracker
git clone <this repo> src && cd src/deploy
TRACKER_TOKEN=$(openssl rand -hex 16) docker compose up -d --build
# dashboard: http://<unraid>:8787   health: /healthz
```

**Each machine** (work laptop, personal desktop)

```
cp collector/collector.example.toml collector/collector.toml
# edit: account.name = "work" or "personal", server.url, server.token
python3 collector/collect.py --dry-run run     # see what would ship
python3 collector/collect.py run               # ship it
```

Then schedule `collect.py run` every hour or so (`deploy/collector.cron`, `deploy/collector.ps1`
for Windows Task Scheduler). It only ships new transcript lines and changed instruction files,
and spools locally if the server is unreachable (`collect.py flush` later).

**Daily meter reading** (claude.ai has no per-conversation data):

```
python3 collector/collect.py meter --surface claude_ai --pct 42
```

**Scheduled tasks**: see `docs/scheduled-tasks.md` for the git relay that gets cloud runs in.

**Analyze**

```
docker exec claude-usage-tracker python -m analyzers.run --db /data/tracker.db --rules   # also runs nightly
docker exec claude-usage-tracker python -m analyzers.investigate --db /data/tracker.db compare
# LLM audits run on a machine logged into the account (subscription, no API key):
python -m agents.run_agent --db <copy of tracker.db> --audit claude_md --account work --dry-run
```

## Privacy and separation

- Every row is tagged with `account`; the dashboard and analyzers show one account unless you
  pick All. Findings that span both accounts are tagged `shared`.
- The collector never ships message bodies or tool outputs. It ships token counts, tool names
  and sizes, the first 200 chars of your prompts (optional), and the text of instruction files
  (optional, needed for the audits). See `[privacy]` in `collector.toml`.
- The server has no Claude credentials. LLM audits run on your machines under the matching login.

## Data

One SQLite file at `/data/tracker.db` (schema in `server/schema.sql`). Back it up with appdata.
`GET /api/export` dumps the aggregates as JSON if you want to analyze elsewhere.
