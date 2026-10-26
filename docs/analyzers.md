# Analyzers

The analysis layer reads the tracker SQLite DB (`server/schema.sql`) and writes
rows into the `findings` table that the dashboard shows. There are two tiers:

* **Rule analyzers** (`analyzers/`): cheap, deterministic, stdlib-only. Run them
  from cron on the Unraid box.
* **LLM audits** (`agents/`): build a prompt from DB evidence, pick a model by
  complexity, and run it through `claude -p` on a logged-in machine (Pro
  subscription, no API key). Run these by hand or from a scheduled task.

Findings are keyed by `fingerprint` (`analyzer:subject`), so reruns update the
existing row and keep its `resolved` flag instead of creating duplicates.
Cross-account findings use `account='shared'`.

## Running

```bash
# all rule analyzers, both accounts, write to DB, print ranked table
python -m analyzers.run --db /mnt/user/appdata/tracker/tracker.db --rules

# one account, one analyzer, no DB writes, JSON out
python -m analyzers.run --db tracker.db --account work --analyzer model_fit --dry-run --json

# LLM audits (needs `claude` on PATH); --dry-run prints the prompt + chosen model instead
python -m analyzers.run --db tracker.db --llm --analyzer schedule_review --dry-run
python -m agents.run_agent --db tracker.db --audit claude_md_audit --account work
python -m agents.run_agent --db tracker.db --audit weekly_review --model opus

# dig in
python -m analyzers.investigate --db tracker.db compare
python -m analyzers.investigate --db tracker.db top-sessions --by waste|context|cache|output
python -m analyzers.investigate --db tracker.db session <id-or-prefix>
python -m analyzers.investigate --db tracker.db tools --account work
python -m analyzers.investigate --db tracker.db context-files
python -m analyzers.investigate --db tracker.db findings

# synthetic fixture + smoke test (also usable for dashboard development)
python -m analyzers.test_fixture --keep /tmp/fixture.db
```

All commands take `--db PATH` and most take `--account work|personal|all`
(default `all`). `analyzers.test_fixture.seed(conn)` is importable and fills an
empty schema DB with two accounts of synthetic data.

## Rule analyzers

Thresholds are module-level constants at the top of each file.

| Analyzer | What it checks | Key thresholds |
|---|---|---|
| `cache_efficiency` | Sessions with large context but low cache hit ratio; cache churn (consecutive calls each re-creating a big cache block, typically a changing system prompt / CLAUDE.md). `est_tokens_wasted` = cache_create tokens that should have been reads. | `LOW_RATIO_MIN_CONTEXT=50k`, `LOW_RATIO=0.5`, `CHURN_MIN_CREATE=5k` tokens, `CHURN_MIN_RUN=3` calls |
| `context_bloat` | Latest snapshot of each CLAUDE.md / command / agent / skill / task prompt over size thresholds; tokens per 30 days = size x sessions in that scope. Cheap heuristics on CLAUDE.md: lines restating defaults ("be helpful", "write clean code"), runs of 8+ path lines, duplicated bullets in the same file, code blocks over 25 lines. Evidence quotes line numbers only. | global CLAUDE.md > 1,500 medium / > 3,000 high; project CLAUDE.md > 2,500 / > 5,000; others > 2,000 / > 4,000 |
| `duplicate_instructions` | Bullets and sentences that appear in 2+ instruction files, exact (normalized text) or near (difflib ratio > 0.85). Reports which copy has words the others lack. Emits `account='shared'` when the copies span accounts. Recommends `~/.claude/CLAUDE.md`, `.claude/commands/_shared.md` or a skill depending on the kinds/scopes involved. | `NEAR_RATIO=0.85`, `MIN_UNIT_CHARS=30` |
| `model_fit` | Opus-class sessions with short outputs (< 200 avg), mostly tool-only turns, slash-command-only sessions; opus-class sidechain (subagent) traffic; haiku/sonnet sessions with > 15 consecutive small tool-use turns (thrashing). Families detected by substring (haiku/sonnet/opus/fable/mythos); `common.RELATIVE_COST` is an approximate ordering, not a price list. | `SHORT_OUTPUT=200`, `TOOL_ONLY_SHARE=0.8`, `RETRY_RUN=15` |
| `scheduled_tasks` | Tasks whose runs are mostly `nothing_new` (suggests a longer cron), opus-class tasks with small output, crons firing at :00/:30 (jittered suggestion), near-identical prompts across tasks, prompts without stated model/scope. `cronutil` parses `*/N`, fixed hours, weekday ranges and a `CRON_TZ=` prefix. | `MIN_RUNS=4`, `NOTHING_NEW_SHARE=0.7`, `SMALL_OUTPUT=800`, `NEAR_RATIO=0.8` |
| `repetition` | Same `prompt_hash` 3+ times across 2+ sessions (slash command / skill), long prompts (> 4,000 chars) that recur (file or CLAUDE.md pointer), tools returning > 20k chars repeatedly (filter / head / limits). | `REPEAT_MIN=3`, `LONG_PROMPT=4000`, `HUGE_RESULT=20000` |
| `utilization` | Idle weekdays while scheduled tasks run, meter `pct_used` never above 30%, meter at 100%, days above 3x the median context; plus a `shared` work-vs-personal comparison with an imbalance verdict. | `LOW_METER_PCT=30`, `HIGH_METER_PCT=100`, `SPIKE_FACTOR=3`, `IMBALANCE_RATIO=3` |

All analyzers look at the last `WINDOW_DAYS=30`.

## LLM audits and model routing

`agents/run_agent.py` has one builder per audit that pulls evidence from the DB
and fills a template in `agents/prompts/`:

| Audit | Template | Evidence | Output |
|---|---|---|---|
| `claude_md_audit` | `claude_md_audit.md` | each CLAUDE.md with numbered lines, section outline, sessions/30d | keep / move:command / move:skill / move:global / shorten / delete per section |
| `duplicate_merge` | `duplicate_merge.md` | top clusters from `duplicate_instructions` with every copy's text | one canonical version, per-scope deltas, info present in only one copy, suggested home |
| `schedule_review` | `schedule_review.md` | tasks + run stats | proposed cron + model per task, prompt fixes, consolidations |
| `weekly_review` | `weekly_review.md` | last 7 days per account, week-over-week delta, top 40 open findings | up to 5 prioritized changes with expected monthly saving |

Every template ends with the shared `_output_format.md`, which asks for
`{"findings": [...], "summary": "..."}` where each finding has the same fields
as the `Finding` dataclass (`severity`, `title`, `detail`, `recommendation`,
`est_tokens_wasted`, `evidence`, `fingerprint`). Findings are stored with
`analyzer='llm:<audit>'`.

### Routing (`agents/router.py`)

`route(job)` looks at four signals the builder supplies:
`input_tokens` (size of the evidence), `judgment` (opinion needed vs
classification), `cross_file` (must reason across several files/tasks at
once), `n_items`. Rules fire in order (first match wins):

1. no judgment and input <= 3k tokens -> **haiku**
2. no judgment -> **sonnet**
3. judgment, single file, input <= 12k -> **sonnet**
4. judgment, cross-file, input <= 6k and <= 8 items -> **sonnet**
5. judgment, cross-file (bigger) -> **opus**
6. judgment, input > 12k -> **opus**
7. otherwise -> **sonnet**

`--model haiku|sonnet|opus` overrides. Aliases map to model ids in
`router.MODELS`; update those to the ids your account currently offers.

### Invocation

```
claude -p --model <id> --output-format json  < prompt
```

The runner reads the `result` field of the JSON envelope, tolerates code
fences or prose around the JSON, parses the findings and upserts them. If
`claude` is not on PATH the audit is skipped with a message (exit 0), so a cron
job on the server without Claude Code does not fail. Dry runs print the prompt
and the routing decision and make no DB writes.

Because the audits spend tokens from the same Pro budget they are trying to
save, run `claude_md_audit` and `duplicate_merge` only after the rule
analyzers show something worth judging, and `weekly_review` once a week.
