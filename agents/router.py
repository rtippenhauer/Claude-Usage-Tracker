"""Model routing for LLM-backed audits.

``route(job)`` returns one of the aliases ``haiku``, ``sonnet`` or ``opus``
from a few complexity signals carried in the job dict:

  input_tokens   estimated size of the evidence block (int)
  judgment       True when the task needs an opinion (keep/move/delete, plan),
                 False when it is classification/extraction with a fixed label set
  cross_file     True when the answer depends on reasoning across several files
                 or tasks at once (merging, consolidation, prioritization)
  n_items        number of independent items to judge (sections, tasks, ...)

Rules (first match wins; see ROUTING):

  1. no judgment, small input                        -> haiku
  2. no judgment, any size                           -> sonnet
  3. judgment, no cross-file, input <= 12k tokens    -> sonnet
  4. judgment, cross-file, input <= 6k, n_items <= 8 -> sonnet
  5. judgment + cross-file (bigger)                  -> opus
  6. judgment, input > 12k                           -> opus
  7. otherwise                                       -> sonnet

Rationale: classification and extraction over a bounded label set is cheap
and haiku does it reliably; per-section verdicts on one file are sonnet-grade;
only merge/consolidation decisions that must hold several files in mind at
once, or very large inputs, justify opus on a Pro subscription.
"""
from __future__ import annotations

# Alias -> concrete model id passed to `claude -p --model`.  UPDATE THESE to
# the model ids current for your account (`claude --help` or /model lists
# them); aliases like "sonnet"/"opus" also work with the CLI if you prefer.
MODELS: dict[str, str] = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

SMALL_INPUT = 3_000
MEDIUM_INPUT = 6_000
LARGE_INPUT = 12_000
FEW_ITEMS = 8

# (name, predicate, alias) evaluated in order.
ROUTING: list[tuple[str, str, str]] = [
    ("classify-small", "not judgment and input_tokens <= SMALL_INPUT", "haiku"),
    ("classify", "not judgment", "sonnet"),
    ("judge-single-file", "judgment and not cross_file and input_tokens <= LARGE_INPUT", "sonnet"),
    ("judge-few-cross", "judgment and cross_file and input_tokens <= MEDIUM_INPUT and n_items <= FEW_ITEMS", "sonnet"),
    ("judge-cross-file", "judgment and cross_file", "opus"),
    ("judge-large", "judgment and input_tokens > LARGE_INPUT", "opus"),
    ("default", "True", "sonnet"),
]


def _signals(job: dict) -> dict:
    return {
        "input_tokens": int(job.get("input_tokens") or 0),
        "judgment": bool(job.get("judgment", True)),
        "cross_file": bool(job.get("cross_file", False)),
        "n_items": int(job.get("n_items") or 1),
    }


def _match(job: dict) -> tuple[str, str, str]:
    """Return the (name, predicate, alias) of the first ROUTING rule that fires."""
    env = {**_signals(job), "SMALL_INPUT": SMALL_INPUT, "MEDIUM_INPUT": MEDIUM_INPUT,
           "LARGE_INPUT": LARGE_INPUT, "FEW_ITEMS": FEW_ITEMS}
    for rule in ROUTING:
        if eval(rule[1], {"__builtins__": {}}, env):  # noqa: S307 - predicates are module constants
            return rule
    return ("default", "True", "sonnet")


def route(job: dict) -> str:
    """Return the model alias for a job dict (see module docstring).

    ``job['model']`` set to a known alias overrides the rules.
    """
    forced = job.get("model")
    if forced in MODELS:
        return forced
    return _match(job)[2]


def explain(job: dict) -> str:
    """Human-readable line saying which rule fired."""
    if job.get("model") in MODELS:
        return f"{job['model']} (forced)"
    name, pred, alias = _match(job)
    return f"{alias} via rule '{name}' [{pred}] signals={_signals(job)}"


def model_id(alias: str) -> str:
    return MODELS.get(alias, MODELS["sonnet"])
