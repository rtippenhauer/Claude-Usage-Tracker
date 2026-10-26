"""Minimal cron parsing: enough to classify a 5-field expression and estimate
its firing interval. Supports an optional leading ``CRON_TZ=<zone>`` token.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TZ_RE = re.compile(r"^\s*CRON_TZ=(\S+)\s+")


@dataclass
class CronInfo:
    raw: str
    tz: str | None
    minute: str
    hour: str
    dom: str
    month: str
    dow: str
    valid: bool

    @property
    def interval_minutes(self) -> float | None:
        """Approximate average minutes between firings."""
        if not self.valid:
            return None
        m, h = self.minute, self.hour
        mi = _step(m)
        if mi and h == "*":
            return float(mi)
        if m == "*" and h == "*":
            return 1.0
        hi = _step(h)
        if hi and _is_fixed(m):
            return hi * 60.0
        if _is_fixed(m) and _is_fixed(h):
            per_day = len(_values(m, 0, 59)) * len(_values(h, 0, 23))
            days_per_week = len(_values(self.dow, 0, 7)) if self.dow != "*" else 7
            if self.dow != "*" and 7 in _values(self.dow, 0, 7) and 0 in _values(self.dow, 0, 7):
                days_per_week -= 1
            return (7 * 24 * 60) / max(per_day * days_per_week, 1)
        if _is_fixed(m) and h == "*":
            return 60.0 / max(len(_values(m, 0, 59)), 1)
        return None

    @property
    def description(self) -> str:
        if not self.valid:
            return f"unparseable ({self.raw})"
        iv = self.interval_minutes
        if iv is None:
            return self.raw
        if iv < 60:
            return f"every {iv:.0f} min"
        if iv < 24 * 60:
            return f"every {iv/60:.0f} h" if iv % 60 == 0 else f"every {iv/60:.1f} h"
        if abs(iv - 24 * 60) < 1:
            return "daily"
        if self.dow not in ("*",):
            return f"{len(_values(self.dow, 0, 7))}x/week"
        return f"every {iv/1440:.1f} days"

    @property
    def fires_on_round_minute(self) -> bool:
        """True when every firing minute is :00 or :30 (contended slots)."""
        if not self.valid or self.minute == "*":
            return False
        vals = _values(self.minute, 0, 59)
        return bool(vals) and all(v in (0, 30) for v in vals)

    @property
    def weekdays_only(self) -> bool:
        if not self.valid:
            return False
        return self.dow.replace(" ", "") in ("1-5", "mon-fri", "MON-FRI")


def _step(field: str) -> int | None:
    m = re.fullmatch(r"\*/(\d+)", field)
    return int(m.group(1)) if m else None


def _is_fixed(field: str) -> bool:
    return field != "*" and "/" not in field


def _values(field: str, lo: int, hi: int) -> list[int]:
    out: set[int] = set()
    names = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
    for part in field.split(","):
        part = part.strip().lower()
        if part in names:
            out.add(names[part])
            continue
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s) if s.isdigit() else 1
        if part == "*":
            out.update(range(lo, hi + 1, step))
        elif "-" in part:
            a, b = part.split("-", 1)
            a, b = names.get(a, a), names.get(b, b)
            try:
                out.update(range(int(a), int(b) + 1, step))
            except ValueError:
                pass
        elif part.isdigit():
            out.add(int(part))
    return sorted(out)


def parse(expr: str | None) -> CronInfo:
    raw = expr or ""
    tz = None
    m = _TZ_RE.match(raw)
    body = raw
    if m:
        tz = m.group(1)
        body = raw[m.end():]
    fields = body.split()
    if len(fields) != 5:
        return CronInfo(raw, tz, "", "", "", "", "", False)
    return CronInfo(raw, tz, *fields, True)


def with_jitter(info: CronInfo, name: str) -> str:
    """Suggested cron with a deterministic non-round minute (1-29 min earlier)."""
    if not info.valid:
        return info.raw
    shift = (sum(ord(c) for c in name) % 29) + 1
    vals = _values(info.minute, 0, 59)
    if len(vals) != 1:
        return info.raw
    minute = vals[0] - shift
    hour = info.hour
    if minute < 0:
        minute += 60
        if hour.isdigit():
            hour = str((int(hour) - 1) % 24)
    tzp = f"CRON_TZ={info.tz} " if info.tz else ""
    return f"{tzp}{minute} {hour} {info.dom} {info.month} {info.dow}"


def lengthen(info: CronInfo) -> str:
    """Suggest a cron roughly 2-3x less frequent than the current one."""
    if not info.valid:
        return info.raw
    tzp = f"CRON_TZ={info.tz} " if info.tz else ""
    iv = info.interval_minutes or 60
    if iv < 60:
        return f"{tzp}{info.minute if _is_fixed(info.minute) else '7'} */2 * * *"  # from sub-hourly to 2-hourly
    if iv < 24 * 60:
        step = _step(info.hour) or max(1, int(iv // 60))
        new_step = min(24, step * 3)
        if new_step >= 24:
            return f"{tzp}{info.minute if _is_fixed(info.minute) else '7'} 8 * * *"
        return f"{tzp}{info.minute if _is_fixed(info.minute) else '7'} */{new_step} * * *"
    if iv <= 24 * 60 + 1:
        if info.dow == "*":
            return f"{tzp}{info.minute} {info.hour} * * 1-5"  # daily -> weekdays
        return f"{tzp}{info.minute} {info.hour} * * 1,4"      # weekdays -> twice a week
    return f"{tzp}{info.minute} {info.hour} * * 1"            # anything slower -> weekly
