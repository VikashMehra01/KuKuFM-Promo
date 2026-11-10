"""Convert between screenplay timecodes (``mm:ss.xx`` / ``hh:mm:ss.xx``) and seconds."""

from __future__ import annotations

import re

_TC_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$")


def parse_timecode(value: str) -> float:
    """Parse ``"01:05.60"`` -> ``65.6``. Raises ``ValueError`` on bad input."""
    match = _TC_RE.match(value)
    if not match:
        raise ValueError(f"Invalid timecode: {value!r}")
    hours, minutes, seconds = match.groups()
    total = int(minutes) * 60 + float(seconds)
    if hours:
        total += int(hours) * 3600
    return round(total, 3)


def format_timecode(seconds: float) -> str:
    """Format ``65.6`` -> ``"01:05.60"`` (matches the screenplay style)."""
    if seconds < 0:
        raise ValueError(f"Negative time: {seconds}")
    centis = int(round(seconds * 100))
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{minutes:02d}:{secs:02d}.{centis:02d}"


def short_tc(seconds: float) -> str:
    """Compact form used in LLM prompts to save tokens: ``"01:05.6"``."""
    return format_timecode(seconds)[:-1]
