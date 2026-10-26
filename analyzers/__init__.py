"""Rule-based analyzers for the Claude usage tracker.

Each analyzer module exposes ``run(conn, account) -> list[Finding]``.  The
``run`` CLI (``python -m analyzers.run``) executes them and upserts the
findings into the ``findings`` table that the dashboard reads.
"""
from __future__ import annotations

RULE_ANALYZERS: tuple[str, ...] = (
    "cache_efficiency",
    "context_bloat",
    "duplicate_instructions",
    "model_fit",
    "scheduled_tasks",
    "repetition",
    "utilization",
)
