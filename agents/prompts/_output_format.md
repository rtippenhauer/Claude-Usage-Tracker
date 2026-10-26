## Output format

Return ONLY a JSON object (no prose, no markdown fence) of this shape:

```
{
  "findings": [
    {
      "severity": "high" | "medium" | "low" | "info",
      "title": "one line, <= 120 chars",
      "detail": "markdown; quote line numbers or task ids, never whole file contents",
      "recommendation": "the concrete change to make",
      "est_tokens_wasted": <integer or null>,
      "evidence": { "...": "any small JSON object with the references you relied on" },
      "fingerprint": "stable id for this subject, e.g. '<path>#<section>' or '<task_id>'"
    }
  ],
  "summary": "two sentences at most"
}
```

Rules: keep findings specific and actionable; do not invent file contents; when
unsure, say so in `detail` and use severity "info". `fingerprint` must be the
same every time the same subject is audited so reruns update rather than
duplicate.
