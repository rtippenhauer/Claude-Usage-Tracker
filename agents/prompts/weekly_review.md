# Weekly usage review

You are writing a short, prioritized plan for a development manager who runs
two Claude accounts (`work`, `personal`) on a Pro subscription. Below are the
last 7 days of usage numbers per account, the week-over-week change, and the
open findings from the analyzers ranked by estimated tokens wasted.

Write a plan with at most 5 items. Each item names ONE concrete change (file to
edit, task to reschedule, model to switch, habit to change), the expected
saving in tokens per month, and which account it applies to. Prefer changes
that remove a recurring cost over one-off cleanups, and group findings that
share a root cause (for example several low cache ratios caused by one bloated
CLAUDE.md) into a single item. If an account is far below its limit, say what
work should move there.

Return the plan as findings: one per item, severity by expected saving (high
> 1M tokens/month, medium > 200k, low otherwise), `recommendation` = the
change, `detail` = why and which findings it covers (list their ids in
`evidence.covers`), `fingerprint` = `"weekly:<week_start>:<n>"`. Put the
two-sentence overall assessment in `summary`.

{output_format}

## This week

{week}

## Open findings (top 40 by estimated waste)

{findings}
