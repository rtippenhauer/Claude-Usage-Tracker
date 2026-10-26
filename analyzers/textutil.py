"""Small markdown/text helpers shared by context_bloat and duplicate_instructions."""
from __future__ import annotations

import re
from dataclasses import dataclass

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def normalize(text: str) -> str:
    """Lowercase, strip bullets/punctuation/extra whitespace for comparison."""
    t = _BULLET_RE.sub("", text.strip()).lower()
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


@dataclass
class Block:
    """A paragraph, bullet or code fence with its line span (1-based, inclusive)."""
    kind: str            # paragraph | bullet | code | heading
    text: str
    start: int
    end: int
    heading: str = ""    # nearest enclosing heading text

    @property
    def lines(self) -> int:
        return self.end - self.start + 1


def split_blocks(content: str) -> list[Block]:
    """Split markdown into blocks. Bullets are one block each (with continuation
    lines), paragraphs break on blank lines, fenced code is one block."""
    blocks: list[Block] = []
    lines = content.splitlines()
    heading = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _FENCE_RE.match(line):
            start = i
            i += 1
            while i < n and not _FENCE_RE.match(lines[i]):
                i += 1
            end = min(i, n - 1)
            blocks.append(Block("code", "\n".join(lines[start:end + 1]), start + 1, end + 1, heading))
            i += 1
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            heading = hm.group(2).strip()
            blocks.append(Block("heading", heading, i + 1, i + 1, heading))
            i += 1
            continue
        if _BULLET_RE.match(line):
            start = i
            buf = [line]
            i += 1
            # continuation lines are indented and not themselves bullets
            while i < n and lines[i].strip() and not _BULLET_RE.match(lines[i]) and lines[i][:1].isspace():
                buf.append(lines[i])
                i += 1
            blocks.append(Block("bullet", "\n".join(buf), start + 1, i, heading))
            continue
        start = i
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not _BULLET_RE.match(lines[i]) and not _HEADING_RE.match(lines[i]) \
                and not _FENCE_RE.match(lines[i]):
            buf.append(lines[i])
            i += 1
        blocks.append(Block("paragraph", "\n".join(buf), start + 1, i, heading))
    return blocks


_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'(])")


def sentences(text: str) -> list[str]:
    """Crude sentence split for near-duplicate comparison."""
    flat = _WS_RE.sub(" ", _BULLET_RE.sub("", text)).strip()
    return [s.strip() for s in _SENT_RE.split(flat) if len(s.strip()) >= 20]


def sections(content: str) -> list[tuple[str, int, int, str]]:
    """Return (heading, start_line, end_line, body) for each heading-delimited section."""
    lines = content.splitlines()
    out = []
    cur_head, cur_start = "(preamble)", 1
    for i, line in enumerate(lines, 1):
        if _HEADING_RE.match(line):
            if i > cur_start or cur_head != "(preamble)":
                out.append((cur_head, cur_start, i - 1, "\n".join(lines[cur_start - 1:i - 1])))
            cur_head, cur_start = _HEADING_RE.match(line).group(2).strip(), i
    out.append((cur_head, cur_start, len(lines), "\n".join(lines[cur_start - 1:])))
    return [s for s in out if s[3].strip()]
