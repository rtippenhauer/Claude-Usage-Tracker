# CLAUDE.md audit

You are auditing instruction files that Claude Code loads into EVERY session
for the `{account}` account. Each token in these files is paid on every call
(and again whenever the prompt cache is rebuilt). The goal is to keep only what
every session needs and move or delete the rest.

For each section (heading) of each file below decide ONE verdict:

- **keep**: needed in every session (project conventions, hard constraints, safety rules).
- **move:command**: only needed when a specific task is run; belongs in a slash command under `.claude/commands/`.
- **move:skill**: a procedure or reference that is needed occasionally; belongs in a skill loaded on demand.
- **move:global**: appears in a project file but applies to every project; belongs in `~/.claude/CLAUDE.md`.
- **shorten**: keep the rule but it is stated at 3x the length needed (give the shortened text).
- **delete**: restates Claude's defaults ("be helpful", "write clean code"), lists things discoverable from the repo, or is stale.

Emit one finding per section whose verdict is not `keep`. Put the verdict in
`evidence.verdict`, the section heading and line range in `evidence.section`
and `evidence.lines`, and the token estimate of the section in
`evidence.section_tokens`. `est_tokens_wasted` = section tokens x sessions per 30
days (given per file below) for `delete`, half of that for `move:*` and
`shorten`. Use `fingerprint` = `"<path>#<heading>"`.

Also emit ONE finding per file with severity "info" and `evidence.verdict =
"summary"` giving the projected size after applying all verdicts.

{output_format}

## Files

{files}
