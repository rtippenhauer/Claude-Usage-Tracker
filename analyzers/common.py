"""Shared helpers for the analyzers: DB access, the Finding dataclass, upsert,
token estimation and model-family detection.

Everything here is stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "server" / "schema.sql"

SEVERITIES = ("high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# Approximate relative cost per token by model family, normalised so that
# sonnet == 1.0.  This is NOT a pricing table; it only orders the families so
# the analyzers can say "this is roughly Nx more expensive".  Adjust freely.
RELATIVE_COST: dict[str, float] = {
    "haiku": 0.3,
    "sonnet": 1.0,
    "opus": 5.0,
    "fable": 5.0,    # treated as opus-class
    "mythos": 8.0,   # treated as above opus-class
    "unknown": 1.0,
}

# Families ordered from cheapest to most capable.
FAMILY_ORDER = ("haiku", "sonnet", "opus", "fable", "mythos")
EXPENSIVE_FAMILIES = {"opus", "fable", "mythos"}


def model_family(model: str | None) -> str:
    """Return the model family detected by substring (haiku/sonnet/opus/fable/mythos)."""
    m = (model or "").lower()
    for fam in FAMILY_ORDER:
        if fam in m:
            return fam
    return "unknown"


def relative_cost(model: str | None) -> float:
    return RELATIVE_COST.get(model_family(model), RELATIVE_COST["unknown"])


def est_tokens(text: str | None) -> int:
    """Token estimate matching the collector heuristic (chars / 3.6)."""
    if not text:
        return 0
    return int(len(text) / 3.6) + 1


# ----------------------------------------------------------------------------- db

def open_db(path: str | Path, create: bool = False) -> sqlite3.Connection:
    """Open the tracker DB with Row factory. ``create`` applies schema.sql."""
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    if create:
        conn.executescript(SCHEMA_PATH.read_text())
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def acct_clause(account: str | None, col: str = "account") -> tuple[str, list]:
    """SQL fragment + params restricting to one account ('all'/None = no filter)."""
    if account and account != "all":
        return f" AND {col} = ?", [account]
    return "", []


def accounts_in(conn: sqlite3.Connection, account: str | None) -> list[str]:
    """Resolve 'all'/None into the concrete account names present in the DB."""
    if account and account != "all":
        return [account]
    found = [r["account"] for r in conn.execute(
        "SELECT account FROM accounts UNION SELECT DISTINCT account FROM sessions "
        "UNION SELECT DISTINCT account FROM context_files UNION SELECT DISTINCT account FROM scheduled_tasks")]
    return sorted(a for a in found if a and a != "shared")


def since_iso(days: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).isoformat()


def latest_context_files(conn: sqlite3.Connection, account: str | None, kinds: Iterable[str] | None = None,
                         with_content: bool = True) -> list[dict]:
    """Latest snapshot of every instruction file (per account + path)."""
    w, p = acct_clause(account, "cf.account")
    kw = ""
    if kinds:
        kinds = list(kinds)
        kw = f" AND cf.kind IN ({','.join('?' * len(kinds))})"
        p = p + kinds
    cols = "cf.id, cf.account, cf.machine, cf.kind, cf.path, cf.scope, cf.content_hash, cf.chars, cf.est_tokens, cf.snapshot_ts"
    if with_content:
        cols += ", cf.content"
    return rows(conn, f"""SELECT {cols} FROM context_files cf
                          WHERE cf.snapshot_ts = (SELECT MAX(snapshot_ts) FROM context_files
                                                  WHERE account = cf.account AND path = cf.path){w}{kw}
                          ORDER BY cf.account, cf.kind, cf.path""", p)


# ----------------------------------------------------------------------------- findings

@dataclass
class Finding:
    """One analyzer result. Mirrors the ``findings`` table.

    ``fingerprint`` must be stable across reruns for the same subject so that
    ``upsert_finding`` updates instead of duplicating.
    """
    account: str
    analyzer: str
    severity: str
    title: str
    fingerprint: str
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    est_tokens_wasted: int | None = None
    recommendation: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"bad severity {self.severity!r}")
        if self.est_tokens_wasted is not None:
            self.est_tokens_wasted = int(self.est_tokens_wasted)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any], analyzer: str | None = None, account: str | None = None) -> "Finding":
        """Build a Finding from a loosely-shaped dict (e.g. LLM JSON output)."""
        sev = str(d.get("severity") or "info").lower()
        if sev not in SEVERITIES:
            sev = "info"
        ev = d.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except json.JSONDecodeError:
                ev = {"text": ev}
        an = analyzer or d.get("analyzer") or "llm"
        fp = d.get("fingerprint") or f"{an}:{d.get('title', '')[:120]}"
        if not fp.startswith(f"{an}:"):
            fp = f"{an}:{fp}"
        return cls(account=account or d.get("account") or "shared", analyzer=an, severity=sev,
                   title=str(d.get("title") or "untitled")[:200], fingerprint=fp,
                   detail=str(d.get("detail") or ""), evidence=ev,
                   est_tokens_wasted=d.get("est_tokens_wasted"),
                   recommendation=str(d.get("recommendation") or ""))


def upsert_finding(conn: sqlite3.Connection, f: Finding) -> int:
    """Insert or update a finding keyed on fingerprint. Returns the row id.

    Existing rows keep their ``resolved`` flag and ``created_ts``.
    """
    ev = json.dumps(f.evidence, default=str)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = conn.execute("SELECT id FROM findings WHERE fingerprint = ?", (f.fingerprint,)).fetchone()
    if existing:
        conn.execute("""UPDATE findings SET account=?, analyzer=?, severity=?, title=?, detail=?, evidence=?,
                        est_tokens_wasted=?, recommendation=?, updated_ts=? WHERE id=?""",
                     (f.account, f.analyzer, f.severity, f.title, f.detail, ev, f.est_tokens_wasted,
                      f.recommendation, now, existing["id"]))
        return int(existing["id"])
    cur = conn.execute("""INSERT INTO findings(account, analyzer, severity, title, detail, evidence,
                          est_tokens_wasted, recommendation, fingerprint, updated_ts)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (f.account, f.analyzer, f.severity, f.title, f.detail, ev, f.est_tokens_wasted,
                        f.recommendation, f.fingerprint, now))
    return int(cur.lastrowid)


def upsert_findings(conn: sqlite3.Connection, findings: Iterable[Finding]) -> int:
    n = 0
    for f in findings:
        upsert_finding(conn, f)
        n += 1
    conn.commit()
    return n


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_RANK.get(f.severity, 9), -(f.est_tokens_wasted or 0)))


# ----------------------------------------------------------------------------- text

def fmt_int(n: float | int | None) -> str:
    if n is None:
        return "-"
    return f"{int(n):,}"


def table(headers: list[str], data: list[list[Any]], max_width: int = 60) -> str:
    """Render a plain-text table."""
    def cell(v: Any) -> str:
        s = "-" if v is None else (f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else str(v))
        s = s.replace("\n", " ")
        return s if len(s) <= max_width else s[: max_width - 1] + "~"
    body = [[cell(v) for v in r] for r in data]
    widths = [len(h) for h in headers]
    for r in body:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    def line(r: list[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip()
    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in body]
    return "\n".join(out)
