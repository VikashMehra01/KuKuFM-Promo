"""Deterministic signals: per-beat features and a whole-promo heuristic score.

These are deliberately simple and explainable. They serve three purposes:
1. a no-API baseline (``generate_heuristic_candidates``),
2. a cheap second opinion blended with the LLM judge,
3. features the validator uses to repair over-long promos.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .models import Beat, BeatKind, HookType, Moment, Promo, Segment

# Lexicons are tuned for melodrama / microdrama screenplays (English).
# Physical confrontation only. Deliberately excludes figurative/ambient words seen
# in this genre's camera notes ("burning outrage", "fire" in a hearth, "faintly").
_PHYSICAL = re.compile(
    r"\b(slap(?:s|ped|ping)?|strikes?|striking|struck|punch(?:es|ed)?|hits|kick(?:s|ed)?|grab(?:s|bed|bing)?|"
    r"seiz(?:e|es|ed|ing)|shov(?:e|es|ed|ing)|yank(?:s|ed)?|throws|threw|hurls?|collaps(?:e|es|ed|ing)|faints|"
    r"fainted|blood(?:y|ied)?|bleeding|knife|dagger|sword|blade|pistol|gun|chok(?:es|ed|ing)|strangl\w+|"
    r"drags?|dragged|crash(?:es|ed)?|shatter(?:s|ed|ing)?|smash(?:es|ed)?|lunges?|stabs?|stabbed|"
    r"wound(?:s|ed)?|slams?|slammed|tackles?|restrain(?:s|ed)?|pins? (?:her|him))\b",
    re.I,
)
_REVEAL = re.compile(
    r"\b(truth|secret\w*|knew|duke|duchess|heir|pregnant|dead|died|murder\w*|lied|liar|betray\w*|marry|married|"
    r"marrying|wife|husband|divorce\w*|disown\w*|who are you|real name|identity|never (?:loved|was)|"
    r"you are (?:the|my|not)|is my|was my|proof|evidence|confess\w*|poison\w*|arsenic|revealed?)\b",
    re.I,
)
_THREAT = re.compile(r"\b(kill|destroy|ruin|regret|pay for|hang|execute|die|punish\w*|warn\w*|guards?|seize her|take her)\b", re.I)
_ROMANCE = re.compile(r"\b(kiss\w*|love[sd]?|embrace\w*|my wife|my husband|marry me|caress\w*)\b", re.I)
_HUMILIATION = re.compile(r"\b(worthless|plain|nothing|disgrace\w*|embarrass\w*|beg\w*|pathetic|shame\w*|low value|tolerate)\b", re.I)
_CLIFF_SFX = re.compile(r"\b(cliffhanger|sting\w*|swell\w*|boom\w*|crash\w*|drone|peak|silence|heartbeat|thud|gasp\w*)\b", re.I)
_CLIFF_CAMERA = re.compile(r"\b(cut to black|freeze|smash cut|fade to black)\b", re.I)

_HIGH_EMOTION = {
    "outraged", "furious", "screaming", "desperate", "devastated", "gloating", "cruel", "venomous", "horrified",
    "shocked", "menacing", "threatening", "crying", "sobbing", "commanding", "enraged", "terrified", "panicked",
    "vicious", "mocking", "broken", "pleading", "begging", "defiant", "seething", "icy", "lethal", "piercing",
    "accusing", "hysterical", "stunned", "gasping", "trembling", "smug", "scornful", "disgusted", "triumphant",
}


def _emotion_intensity(emotion: str | None) -> float:
    if not emotion:
        return 0.0
    words = {w.strip(" ,.").lower() for w in re.split(r"[ ,]+", emotion)}
    hits = len(words & _HIGH_EMOTION)
    return min(1.0, 0.45 * hits)


def score_beat(beat: Beat) -> None:
    """Fill ``beat.features`` in place."""
    text = beat.text
    visual = beat.visual
    both = f"{text} {visual}"
    tags: list[str] = []

    physical = bool(_PHYSICAL.search(text if beat.kind is not BeatKind.DIALOGUE else visual))
    reveal = beat.kind is BeatKind.DIALOGUE and bool(_REVEAL.search(text))
    threat = beat.kind is BeatKind.DIALOGUE and bool(_THREAT.search(text))
    romance = bool(_ROMANCE.search(both))
    humiliation = beat.kind is BeatKind.DIALOGUE and bool(_HUMILIATION.search(text))
    exclaim = beat.kind is BeatKind.DIALOGUE and "!" in text
    question = beat.kind is BeatKind.DIALOGUE and text.rstrip().endswith("?")
    trailing = text.rstrip().endswith(("...", "…", "—", "-"))
    sfx_cliff = beat.kind is BeatKind.SFX and bool(_CLIFF_SFX.search(text))
    cam_cliff = bool(_CLIFF_CAMERA.search(both))

    for name, flag in [
        ("physical", physical), ("reveal", reveal), ("threat", threat), ("romance", romance),
        ("humiliation", humiliation), ("exclaim", exclaim), ("question", question),
        ("sfx_cliff", sfx_cliff), ("cut_to_black", cam_cliff),
    ]:
        if flag:
            tags.append(name)

    intensity = max(_emotion_intensity(beat.emotion), 0.6 if physical else 0.0, 0.3 if exclaim else 0.0)
    short_line = beat.kind is BeatKind.DIALOGUE and len(text.split()) <= 12

    hook = (
        0.40 * physical + 0.35 * reveal + 0.25 * threat + 0.2 * humiliation + 0.15 * exclaim
        + 0.15 * short_line + 0.25 * intensity
    )
    cliff = (
        0.30 * question + 0.25 * trailing + 0.35 * sfx_cliff + 0.35 * cam_cliff + 0.30 * reveal
        + 0.25 * threat + 0.20 * physical + 0.15 * intensity
    )
    beat.features.hook = round(min(1.0, hook), 3)
    beat.features.cliff = round(min(1.0, cliff), 3)
    beat.features.intensity = round(intensity, 3)
    beat.features.tags = tags


def hook_type_for(beat: Beat) -> HookType:
    tags = set(beat.features.tags)
    if "physical" in tags:
        return HookType.PHYSICAL
    if "reveal" in tags:
        return HookType.REVEAL
    if "threat" in tags:
        return HookType.THREAT
    if "humiliation" in tags:
        return HookType.HUMILIATION
    if "romance" in tags:
        return HookType.ROMANCE
    if "exclaim" in tags:
        return HookType.SHOCK_LINE
    return HookType.OTHER


# --------------------------------------------------------------------------- promo-level


def beats_in(segment: Segment, episode_beats: Sequence[Beat]) -> list[Beat]:
    """Beats whose start lies inside the resolved segment window."""
    return [b for b in episode_beats if segment.start - 1e-6 <= b.start < segment.end - 1e-6]


def duration_fit(duration: float, lo: float = 20, hi: float = 60) -> float:
    """1.0 inside the 30-45s sweet spot, tapering to 0.5 at the limits, 0 outside."""
    if duration < lo or duration > hi:
        return 0.0
    if 30 <= duration <= 45:
        return 1.0
    if duration < 30:
        return 0.5 + 0.5 * (duration - lo) / max(1e-6, 30 - lo)
    return 0.5 + 0.5 * (hi - duration) / max(1e-6, hi - 45)


def heuristic_score(promo: Promo, beats_by_ep: dict[int, list[Beat]], hook_window: float = 3.0) -> float:
    """Explainable 0..1 score for a *resolved* promo."""
    if not promo.segments:
        return 0.0
    all_beats: list[Beat] = []
    for seg in promo.segments:
        all_beats.extend(beats_in(seg, beats_by_ep.get(seg.episode, [])))
    if not all_beats:
        return 0.0

    first = promo.segments[0]
    opening = [b for b in all_beats if b.episode == first.episode and b.start < first.start + hook_window]
    hook = max((b.features.hook for b in opening), default=0.0)
    ending = all_beats[-2:]
    cliff = max((b.features.cliff for b in ending), default=0.0)
    intensity = sum(b.features.intensity for b in all_beats) / len(all_beats)

    speakers = {b.speaker for b in all_beats if b.speaker}
    standalone = 1.0 if len(speakers) <= 3 else max(0.3, 1.0 - 0.15 * (len(speakers) - 3))
    segment_penalty = 0.08 * (len(promo.segments) - 1)
    dialogue_ratio = sum(b.kind is BeatKind.DIALOGUE for b in all_beats) / len(all_beats)
    clarity = min(1.0, 0.4 + dialogue_ratio)  # pure-visual stretches are hard to judge from text

    score = (
        0.30 * hook + 0.25 * cliff + 0.15 * intensity + 0.15 * standalone + 0.10 * duration_fit(promo.duration)
        + 0.05 * clarity - segment_penalty
    )
    return round(max(0.0, min(1.0, score)), 4)


def generate_heuristic_candidates(
    beats: Sequence[Beat],
    per_episode: int,
    min_s: float,
    max_s: float,
) -> list[Promo]:
    """Rule-based baseline: start on a strong hook beat, end on the best cliff beat in range.

    Returns draft promos (segments as beat ranges); the validator resolves times.
    """
    if not beats:
        return []
    episode = beats[0].episode
    scored: list[tuple[float, int, int]] = []
    for i, start in enumerate(beats):
        if start.features.hook < 0.35:
            continue
        for j in range(i + 1, len(beats)):
            end = beats[j]
            dur = max(b.end for b in beats[i : j + 1]) - start.start
            if dur > max_s:
                break
            if dur < min_s:
                continue
            value = start.features.hook + 1.2 * end.features.cliff + 0.3 * duration_fit(dur, min_s, max_s)
            scored.append((value, i, j))

    scored.sort(reverse=True)
    picked: list[tuple[int, int]] = []
    for _, i, j in scored:
        if any(not (j < pi or i > pj) for pi, pj in picked):  # overlap -> skip
            continue
        picked.append((i, j))
        if len(picked) >= per_episode:
            break

    promos = []
    for n, (i, j) in enumerate(sorted(picked), start=1):
        hb, cb = beats[i], beats[j]
        promos.append(
            Promo(
                promo_id=f"H-E{episode:02d}-{n}",
                source="heuristic",
                title=f"E{episode:02d} rule-based pick {n}",
                segments=[Segment(episode=episode, start_beat=hb.id, end_beat=cb.id)],
                hook=Moment(beat_id=hb.id, episode=episode, t=hb.start, text=hb.text),
                cliffhanger=Moment(beat_id=cb.id, episode=episode, t=cb.start, text=cb.text),
                hook_type=hook_type_for(hb),
                why_it_works=f"Opens on a {hook_type_for(hb).value} beat and ends on an unresolved beat "
                f"({', '.join(cb.features.tags) or 'tension'}).",
            )
        )
    return promos
