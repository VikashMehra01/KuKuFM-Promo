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
from .models import VISUAL_SEP, Beat, BeatKind, Block, BlockType, Episode, Show

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


def filter_aliases(show: Show, aliases: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Drop LLM-proposed aliases that contradict the script.

    Rule: two speaker names that both speak in the same scene are different
    people (characters don't talk to themselves). This catches real model
    errors such as merging a rival into the heroine she is arguing with.
    Returns (accepted aliases, human-readable rejections).
    """
    speakers_by_scene: list[set[str]] = [
        {b.speaker for b in scene.blocks if b.speaker} for ep in show.episodes for scene in ep.scenes
    ]
    def clash(a: str, b: str) -> bool:
        return any(a in s and b in s for s in speakers_by_scene)

    # names the deterministic rule already ties to each character (trusted)
    rule = build_alias_map(show)
    trusted: dict[str, set[str]] = {}
    for raw, canon in rule.items():
        trusted.setdefault(canon, {canon}).add(raw)

    accepted: dict[str, str] = {}
    rejected: list[str] = []
    # pass 1: an alias must never share a scene with any trusted name of its character
    for alias, canonical in aliases.items():
        alias, canonical = alias.upper(), canonical.upper()
        group = trusted.get(canonical, {canonical}) - {alias}
        other = next((g for g in group if clash(alias, g)), None) if alias != canonical else None
        if other is not None:
            rejected.append(f"{alias} -> {canonical}: speaks in the same scene as {other}")
        else:
            accepted[alias] = canonical
    # pass 2: surviving LLM aliases of one character must not share a scene either
    for alias, canonical in list(accepted.items()):
        if alias == canonical or alias not in accepted:
            continue
        rival = next(
            (o for o, c in accepted.items() if c == canonical and o not in (alias, canonical) and clash(alias, o)),
            None,
        )
        if rival is not None:
            del accepted[alias]
            rejected.append(f"{alias} -> {canonical}: shares a scene with alias {rival}")
    return accepted, rejected


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return a_start < b_end and b_start < a_end


def _attach_visual(beat_start: float, beat_end: float, cameras: Iterable[Block]) -> str:
    texts = [c.text for c in cameras if _overlaps(beat_start, beat_end, c.start, c.end)]
    return VISUAL_SEP.join(texts)


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


def new_camera_shots(beats: Iterable[Beat], dedupe: bool = True) -> list[str]:
    """Attached camera text per beat, with shots already listed on the previous beat removed.

    One camera shot often spans several lines, so attaching it to each repeats it
    (about 25% of all camera text on the input pack). Listing it once, on the first
    line it covers, keeps the information and saves the tokens.
    """
    out: list[str] = []
    prev: list[str] = []
    for b in beats:
        shots = b.camera_shots()
        shown = [s for s in shots if s not in prev] if dedupe else shots
        out.append(VISUAL_SEP.join(shown))
        if b.kind is not BeatKind.VISUAL:
            prev = shots
    return out


def render_beats(beats: Iterable[Beat], max_visual: int | None = None, dedupe_camera: bool = True) -> str:
    """Beats as prompt lines. Defaults: full camera text, each shot listed once.

    ``max_visual=110, dedupe_camera=False`` reproduces the original (pre-Nov-11) prompts exactly.
    """
    beats = list(beats)
    visuals = new_camera_shots(beats, dedupe_camera)
    return "\n".join(b.render(max_visual=max_visual, visual=v) for b, v in zip(beats, visuals))
