# Scheduled tasks: getting cloud runs into the store

Scheduled tasks run in Anthropic's cloud, in a fresh session each time. Nothing lands on your
machines, and the cloud session cannot reach a box on your LAN. So two things need a path in:

1. **Task definitions** (name, cron, model, prompt) — change rarely.
2. **Run results** (tokens, turns, outcome) — one per firing.

## The relay

Use a small private GitHub repo (`claude-usage-relay`) as the drop box. Cloud sessions can push
to GitHub; the Unraid box pulls it on a schedule. Layout:

```
relay/
  tasks/work.json          # task definitions for the work account
  tasks/personal.json
  runs/work/2026-10-26-3f2a9c1e.json
  runs/personal/...
```

### Task definitions

Export once per account, and again whenever you add or change a task. Shape:

```json
[
  {"task_id": "trg_123", "name": "Morning brief", "cron": "CRON_TZ=America/New_York 31 6 * * 1-5",
   "model": "sonnet", "prompt": "...", "permission_mode": "auto", "enabled": 1}
]
```

Fastest way to produce it: in a chat on each account, ask Claude to list your scheduled tasks and
write them to `tasks/<account>.json` in the relay repo (it has the list tool). Or ship it from a
machine with `python3 collect.py tasks --from tasks.json`.

### Run results

Append this to the end of every scheduled-task prompt (adjust account and task name):

```
Finally, run: python3 ~/tracker/report_run.py --task "Morning brief" --account work
  --outcome <noteworthy|nothing_new|error> --summary "<one line>" --relay-dir ~/relay
```

and make the relay repo plus `collector/report_run.py` available in the task's environment
(clone the relay repo and copy the script in a setup step, or keep both in the repo the task
already checks out). The script reads the run's own transcript, totals the tokens, writes one
JSON file and pushes it. If the push fails the file still exists for the next run to push.

`--outcome nothing_new` is the important flag: a task that mostly reports nothing new is the
first thing the schedule review will tell you to run less often.

### Pulling on Unraid

```
git clone git@github.com:<you>/claude-usage-relay /mnt/user/appdata/claude-usage-tracker/relay
# then, hourly (User Scripts plugin or cron):
docker exec claude-usage-tracker python3 server/relay_pull.py --relay /relay --db /data/tracker.db --git-pull
```

## What you get

- Per task: runs, tokens per run, share of runs that found nothing, last run.
- `analyzers/scheduled_tasks.py` recommendations: longer intervals for low-yield tasks, cheaper
  model for small-output tasks, jittered minutes, prompts that duplicate each other, prompts
  missing a stated model or scope.
- The task prompts are stored as `context_files` of kind `scheduled_task`, so the duplicate-
  instruction audit compares them against your CLAUDE.md files and slash commands too.

## If you'd rather not use GitHub

Any store both sides can reach works: a Gist, an S3-compatible bucket, or a Tailscale Funnel to
the tracker's `/ingest` endpoint (then `report_run.py` can POST directly; add `--url` support).
The relay script only cares about the file layout.
