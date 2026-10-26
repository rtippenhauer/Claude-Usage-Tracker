# Duplicate instruction merge

The rule analyzer found the same (or nearly the same) instruction in several
instruction files: CLAUDE.md files, slash commands, agents, skills and
scheduled-task prompts, across the `work` and `personal` accounts. Each
cluster below lists every copy with its account, file, scope and line number,
plus the exact text of each copy.

For each cluster:

1. Write ONE canonical version that preserves every piece of information that
   appears in any copy (do not drop a constraint just because only one copy has it).
2. List the per-scope deltas: text that must stay local to one file because it
   is specific to that project, account or task (for example a namespace name
   or an extra approval step). Give the delta as the sentence to keep in that file.
3. Flag information present in only one copy (`evidence.only_in`), since the
   other scopes are currently missing it; say whether it should propagate
   or stay local.
4. Name the home for the canonical copy and justify it:
   - `~/.claude/CLAUDE.md` on both machines (synced via dotfiles) for cross-account, cross-project rules
   - the project CLAUDE.md for one-project rules
   - `.claude/commands/_shared.md` or a skill for procedures used by several commands/agents
   - a skill in the task environment for boilerplate shared by scheduled tasks
5. Keep separation where scope genuinely differs: if two copies look alike but
   govern different environments (prod vs staging, work vs personal data),
   say so and recommend parameterizing rather than merging.

Emit one finding per cluster. `evidence` must contain `canonical` (string),
`deltas` (object: path -> sentence), `only_in` (object: path -> list of facts),
`home` (string). `est_tokens_wasted` = tokens of one copy x (copies - 1) x
sessions per 30 days (given per cluster). `fingerprint` = `"dup:<cluster_id>"`.

{output_format}

## Clusters

{clusters}
