# Scheduled task review

Below are the scheduled tasks configured for the `{account}` account(s), each
with its cron, model, prompt and run statistics (runs, share ending
`nothing_new`, errors, average context and output tokens, last run). The
account is on a Pro subscription, so every run draws from the same budget as
interactive sessions.

For each task propose:

- **cron**: the schedule that matches how often the task actually finds
  something. A task whose runs are mostly `nothing_new` should run less often
  or gain a cheap pre-check; a task that always finds something and is read
  daily can stay daily. Avoid :00/:30 minutes (give a jittered minute). Keep
  any `CRON_TZ=` prefix. If the task reads things people produce on weekdays,
  use `1-5`.
- **model**: `haiku` for check-and-notify or summarize-a-feed jobs with short
  outputs; `sonnet` for drafting or multi-step tool use; `opus` only when the
  task writes or reviews code that matters or reasons across many sources.
  Justify any `opus`.
- **prompt fixes**: missing scope/time window, missing early-exit instruction
  ("reply nothing_new when ..."), missing output limits.
- **consolidation**: tasks whose prompts differ only by a parameter (namespace,
  repo, label) should become one task; say which and give the merged prompt.
- **disable** when the task's output is not being used (no noteworthy runs in
  the window and no evidence anyone reads it).

Emit one finding per task (plus one per consolidation proposal) with
`evidence` containing `task_id`, `current_cron`, `proposed_cron`,
`current_model`, `proposed_model`, `prompt_fixes` (list). `est_tokens_wasted`
= context tokens of runs that the new schedule/model would have avoided over
the window. `fingerprint` = `"task:<task_id>"` or `"merge:<id1>+<id2>"`.

{output_format}

## Tasks

{tasks}
