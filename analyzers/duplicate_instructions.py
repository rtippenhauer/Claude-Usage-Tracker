"""Duplicate instructions across instruction files.

Units of comparison are bullets and sentences of paragraphs from the latest
snapshot of every context file (CLAUDE.md, commands, agents, skills, scheduled
task prompts). Units are normalized (lowercase, punctuation and whitespace
stripped) and compared

  * exactly (same normalized text in 2+ files), and
  * nearly (difflib SequenceMatcher ratio > NEAR_RATIO), in which case the
    finding reports which copy carries words the others lack.

Findings are per cluster. A cluster spanning both accounts is emitted with
account='shared'. The recommendation names a shared include location based on
the kinds and scopes involved, and keeps separation where scopes differ.
"""
from __future__ import annotations

import difflib
import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from .common import Finding, est_tokens, latest_context_files
from .textutil import normalize, sentences, split_blocks

NAME = "duplicate_instructions"
KINDS = ("claude_md", "command", "agent", "skill", "scheduled_task")
NEAR_RATIO = 0.85
MIN_UNIT_CHARS = 30      # normalized chars; shorter units are too generic to matter
MAX_UNITS_NEAR = 3000    # cap for the O(n^2) near-duplicate pass
MAX_FINDINGS = 60


@dataclass
class Unit:
    file_id: int
    account: str
    kind: str
    path: str
    scope: str
    line: int
    text: str
    norm: str


def _units(files: list[dict]) -> list[Unit]:
    out: list[Unit] = []
    for cf in files:
        content = cf.get("content") or ""
        if not content:
            continue
        for b in split_blocks(content):
            if b.kind in ("heading", "code"):
                continue
            pieces = [b.text]
            if b.kind == "paragraph":
                sents = sentences(b.text)
                if len(sents) > 1:
                    pieces += sents
            for piece in pieces:
                n = normalize(piece)
                if len(n) < MIN_UNIT_CHARS:
                    continue
                out.append(Unit(cf["id"], cf["account"], cf["kind"], cf["path"], cf.get("scope") or "global",
                                b.start, piece.strip(), n))
    return out


class _DSU:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        self.p[self.find(a)] = self.find(b)


def _clusters(units: list[Unit]) -> list[list[int]]:
    """Union units that are exact or near duplicates across different files."""
    dsu = _DSU(len(units))
    by_norm: dict[str, list[int]] = defaultdict(list)
    for i, u in enumerate(units):
        by_norm[u.norm].append(i)
    for idxs in by_norm.values():
        for i in idxs[1:]:
            dsu.union(idxs[0], i)
    # near-duplicates: one representative per distinct norm, bucketed by length
    reps = [idxs[0] for idxs in by_norm.values()]
    if len(reps) <= MAX_UNITS_NEAR:
        reps.sort(key=lambda i: len(units[i].norm))
        for a_pos, i in enumerate(reps):
            ni = units[i].norm
            sm = difflib.SequenceMatcher(None, autojunk=False)
            sm.set_seq2(ni)
            for j in reps[a_pos + 1:]:
                nj = units[j].norm
                # ratio <= 2*len(ni)/(len(ni)+len(nj)); beyond this length it cannot pass
                if len(nj) > len(ni) * (2 / NEAR_RATIO - 1):
                    break  # list is length-sorted
                sm.set_seq1(nj)
                if sm.real_quick_ratio() < NEAR_RATIO or sm.quick_ratio() < NEAR_RATIO:
                    continue
                if sm.ratio() > NEAR_RATIO:
                    dsu.union(i, j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(units)):
        groups[dsu.find(i)].append(i)
    out = []
    for idxs in groups.values():
        if len({units[i].file_id for i in idxs}) >= 2:
            out.append(sorted(idxs, key=lambda i: (units[i].account, units[i].path, units[i].line)))
    # A sentence cluster whose locations are all inside a larger cluster (the
    # enclosing paragraphs) adds nothing; drop it.
    locsets = [{(units[i].file_id, units[i].line) for i in c} for c in out]
    keep = []
    for k, c in enumerate(out):
        covered = any(j != k and locsets[k] <= locsets[j] and
                      len(units[out[j][0]].norm) > len(units[c[0]].norm) for j in range(len(out)))
        if not covered:
            keep.append(c)
    return keep


def _extra_words(units: list[Unit]) -> dict[str, list[str]]:
    """Words present in one copy but missing from every other copy, keyed by 'path:line'."""
    sets = {f"{u.path}:{u.line}": set(u.norm.split()) for u in units}
    out: dict[str, list[str]] = {}
    for k, s in sets.items():
        others = set().union(*(v for kk, v in sets.items() if kk != k)) if len(sets) > 1 else set()
        extra = sorted(s - others)
        if extra:
            out[k] = extra[:25]
    return out


def _recommend(units: list[Unit], cross_account: bool) -> str:
    kinds = {u.kind for u in units}
    scopes = {u.scope for u in units}
    projects = {s for s in scopes if s.startswith("project:")}
    if cross_account:
        base = ("The same instruction lives on both the work and personal machines. Keep one canonical copy in a "
                "dotfiles repo (or a skill) synced to both `~/.claude/` directories, and reference it instead of "
                "re-typing it. ")
    else:
        base = ""
    if kinds == {"claude_md"}:
        if len(projects) >= 2 or (projects and "global" in scopes):
            return base + ("Move the shared text to `~/.claude/CLAUDE.md` (loaded for every project) and keep only "
                           "project-specific deltas in each project CLAUDE.md. If the projects differ in scope, keep "
                           "the delta sentence local and link to the global rule.")
        return base + "Keep one copy and delete the other; both files load into the same context."
    if kinds <= {"command", "skill", "agent"}:
        return base + ("Factor the shared preamble into `.claude/commands/_shared.md` (or a skill with the common "
                       "procedure) and have each command/agent reference it with one line, keeping only the "
                       "step that differs.")
    if "scheduled_task" in kinds:
        return base + ("Scheduled-task prompts repeat the same boilerplate; put it in a skill or a CLAUDE.md in the "
                       "task's environment and keep each task prompt to the part that differs (schedule, scope, "
                       "model).")
    return base + ("Pick the most complete copy as canonical, place it where every consumer loads it "
                   "(global CLAUDE.md for cross-project rules, a skill for on-demand procedures), and replace the "
                   "others with a pointer.")


def run(conn: sqlite3.Connection, account: str | None) -> list[Finding]:
    # Always load every account so cross-account duplicates are visible; filter
    # clusters to those touching the requested account.
    files = latest_context_files(conn, None, KINDS)
    units = _units(files)
    findings: list[Finding] = []
    clusters = _clusters(units)
    scored = []
    for idxs in clusters:
        # one unit per location: keep the longest (paragraph over its own sentence)
        by_loc: dict[tuple[int, int], Unit] = {}
        for i in idxs:
            u = units[i]
            k = (u.file_id, u.line)
            if k not in by_loc or len(u.norm) > len(by_loc[k].norm):
                by_loc[k] = u
        us = sorted(by_loc.values(), key=lambda u: (u.account, u.path, u.line))
        accts = {u.account for u in us}
        if account and account != "all" and account not in accts:
            continue
        n_files = len({u.file_id for u in us})
        tokens = est_tokens(max(us, key=lambda u: len(u.text)).text)
        scored.append((tokens * (n_files - 1), us, accts))
    scored.sort(key=lambda t: -t[0])
    for wasted, us, accts in scored[:MAX_FINDINGS]:
        cross = len(accts) > 1
        exact = len({u.norm for u in us}) == 1
        rep = min(us, key=lambda u: (u.account, u.path, u.line))
        key = hashlib.sha1(rep.norm.encode()).hexdigest()[:12]
        locs = [{"account": u.account, "kind": u.kind, "path": u.path, "line": u.line, "scope": u.scope} for u in us]
        extra = {} if exact else _extra_words(us)
        where = "; ".join(f"{u.account}:{u.path}:L{u.line}" for u in us[:6])
        if len(us) > 6:
            where += f"; +{len(us) - 6} more"
        detail = (f"{'Exact' if exact else 'Near'}-duplicate instruction in {len({u.file_id for u in us})} files: "
                  f"\"{rep.text[:140]}{'...' if len(rep.text) > 140 else ''}\"\nLocations: {where}.")
        if extra:
            detail += "\nInfo only in one copy: " + "; ".join(f"{k} has [{', '.join(v)}]" for k, v in list(extra.items())[:4])
        sev = "medium" if (cross or len(us) >= 3 or wasted > 150) else "low"
        findings.append(Finding(
            account="shared" if cross else rep.account, analyzer=NAME, severity=sev,
            title=f"{'Exact' if exact else 'Near'} duplicate across {len({u.file_id for u in us})} files: {rep.text[:70]}",
            fingerprint=f"{NAME}:{key}",
            detail=detail,
            evidence={"locations": locs, "exact": exact, "extra_words": extra, "accounts": sorted(accts)},
            est_tokens_wasted=wasted,
            recommendation=_recommend(us, cross),
        ))
    return findings
