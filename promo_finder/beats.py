"""Turn raw screenplay blocks into *beats*: the unit the LLM reasons about.

* Every DIALOGUE / ACTION / SFX block becomes a beat.
* CAMERA blocks are attached to the beats they overlap as ``visual`` context.
* CAMERA blocks that overlap nothing become ``VISUAL`` beats, so purely visual
  moments (a look, an insert shot) are not lost.
* Speaker names are canonicalised (``LADY MARA`` -> ``MARA``) so the
  model and the "stands alone" check see one character, not two.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from .heuristics import score_beat
from .models import Beat, BeatKind, Block, BlockType, Episode, Show

_TITLE_PREFIX = re.compile(r"^(LADY|LORD|SIR|MISS|MISTRESS|MASTER|THE)\s+")


def build_alias_map(show: Show, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Raw speaker -> canonical speaker.

    Rule: drop an honorific prefix when the remaining name is itself a speaker
    (``LADY MARA`` -> ``MARA`` but ``LORD`` stays ``LORD``). ``extra``
    (from the story bible) wins over the rule.
    """
    speakers = Counter(b.speaker for ep in show.episodes for b in ep.blocks if b.speaker)
    mapping: dict[str, str] = {}
    for name in speakers:
        stripped = _TITLE_PREFIX.sub("", name)
        mapping[name] = stripped if stripped != name and stripped in speakers else name
    for alias, canonical in (extra or {}).items():
        mapping[alias.upper()] = canonical.upper()
    return mapping


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return a_start < b_end and b_start < a_end


def _attach_visual(beat_start: float, beat_end: float, cameras: Iterable[Block]) -> str:
    texts = [c.text for c in cameras if _overlaps(beat_start, beat_end, c.start, c.end)]
    return " / ".join(texts)


_KIND = {
    BlockType.DIALOGUE: BeatKind.DIALOGUE,
    BlockType.ACTION: BeatKind.ACTION,
    BlockType.SFX: BeatKind.SFX,
}


def build_episode_beats(episode: Episode, aliases: dict[str, str]) -> list[Beat]:
    blocks = episode.blocks
    cameras = [b for b in blocks if b.type is BlockType.CAMERA]
    content = [b for b in blocks if b.type is not BlockType.CAMERA]

    raw: list[tuple[float, float, BeatKind, Block]] = [(b.start, b.end, _KIND[b.type], b) for b in content]
    for cam in cameras:
        if not any(_overlaps(cam.start, cam.end, b.start, b.end) for b in content):
            raw.append((cam.start, cam.end, BeatKind.VISUAL, cam))

    # stable order: by start time, then dialogue before action at the same instant
    order = {BeatKind.DIALOGUE: 0, BeatKind.ACTION: 1, BeatKind.SFX: 2, BeatKind.VISUAL: 3}
    raw.sort(key=lambda r: (r[0], order[r[2]], r[1]))

    beats: list[Beat] = []
    for idx, (start, end, kind, block) in enumerate(raw, start=1):
        speaker = aliases.get(block.speaker, block.speaker) if block.speaker else None
        beat = Beat(
            id=f"E{episode.number:02d}-B{idx:03d}",
            episode=episode.number,
            index=idx - 1,
            kind=kind,
            speaker=speaker,
            emotion=block.emotion,
            start=start,
            end=end,
            text=block.text,
            visual="" if kind is BeatKind.VISUAL else _attach_visual(start, end, cameras),
        )
        score_beat(beat)
        beats.append(beat)
    return beats


def build_show_beats(show: Show, aliases: dict[str, str] | None = None) -> dict[int, list[Beat]]:
    aliases = aliases or build_alias_map(show)
    return {ep.number: build_episode_beats(ep, aliases) for ep in show.episodes}


def camera_edges(episode: Episode) -> list[float]:
    """Sorted unique shot boundaries; the validator snaps cuts to these."""
    edges = {round(t, 3) for b in episode.blocks if b.type is BlockType.CAMERA for t in (b.start, b.end)}
    return sorted(edges)


def render_beats(beats: Iterable[Beat]) -> str:
    return "\n".join(b.render() for b in beats)
