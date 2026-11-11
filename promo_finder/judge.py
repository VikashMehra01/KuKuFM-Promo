"""Scoring: an LLM rubric (blind "stranger" judge) blended with heuristics.

The judge sees ONLY the clip transcript - no story bible - so its
``standalone`` score and ``stranger_view`` double as the "makes sense to
someone who has never seen the show" check.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import RankingConfig
from .beats import new_camera_shots
from .heuristics import beats_in, heuristic_score
from .llm import LLMClient, LLMError
from .models import Beat, BeatKind, Promo
from .prompts import load_prompt
from .schemas import judge_schema

log = logging.getLogger(__name__)

RUBRIC = ("hook", "cliffhanger", "standalone", "intensity", "trope_fit")


class _RawScore(BaseModel):
    model_config = {"extra": "ignore"}
    clip_id: str
    hook: float = 3
    cliffhanger: float = 3
    standalone: float = 3
    intensity: float = 3
    trope_fit: float = 3
    stranger_view: str = ""
    why_it_works: str = ""

    @field_validator(*RUBRIC, mode="before")
    @classmethod
    def _clamp(cls, v):
        """One out-of-range number should not invalidate a whole batch."""
        try:
            return min(5.0, max(1.0, float(v)))
        except (TypeError, ValueError):
            return 3.0


class _JudgeResponse(BaseModel):
    model_config = {"extra": "ignore"}
    scores: list[_RawScore] = Field(default_factory=list)


CAMERA_NOTE = (
    ' A line ending in "| cam: ..." also says what the camera shows while it plays;'
    " a shot is listed once, on the first line it covers."
)


def clip_transcript(promo: Promo, beats_by_ep: dict[int, list[Beat]], with_camera: bool = True) -> str:
    """What a viewer sees and hears, with times relative to the clip start.

    ``with_camera`` adds the camera shots that overlap dialogue/action lines. Without it
    (the original behaviour) only camera shots with no overlapping line reach the judge,
    so a slap or reveal written only as a shot is invisible to it.
    """
    lines = [f"CLIP {promo.promo_id} ({promo.duration:.0f}s)"]
    offset = 0.0
    for n, seg in enumerate(promo.segments):
        if n:
            lines.append("--- CUT ---")
        seg_beats = beats_in(seg, beats_by_ep.get(seg.episode, []))
        cams = new_camera_shots(seg_beats) if with_camera else [""] * len(seg_beats)
        for b, cam in zip(seg_beats, cams):
            t = offset + (b.start - seg.start)
            if b.kind is BeatKind.DIALOGUE:
                emo = f" ({b.emotion})" if b.emotion else ""
                text = f'{b.speaker}{emo}: "{b.text}"'
            else:
                text = f"[{b.kind.value.lower()}] {b.text}"
            if cam:
                text += f" | cam: {cam}"
            lines.append(f"  {t:5.1f}s {text}")
        offset += seg.duration
    return "\n".join(lines)


def _rubric_norm(promo: Promo, weights: dict[str, float]) -> float | None:
    values = {k: getattr(promo.scores, k) for k in RUBRIC}
    if any(v is None for v in values.values()):
        return None
    total_w = sum(weights.get(k, 0) for k in RUBRIC) or 1.0
    return sum(weights.get(k, 0) * (values[k] - 1) / 4 for k in RUBRIC) / total_w


def apply_heuristics(promos: Sequence[Promo], beats_by_ep: dict[int, list[Beat]], hook_window: float) -> None:
    for p in promos:
        p.scores.heuristic = heuristic_score(p, beats_by_ep, hook_window)


def combine_scores(promos: Sequence[Promo], cfg: RankingConfig) -> None:
    for p in promos:
        rubric = _rubric_norm(p, cfg.rubric_weights)
        if rubric is None:
            p.scores.quality = p.scores.heuristic
        else:
            w = cfg.llm_weight + cfg.heuristic_weight or 1.0
            p.scores.quality = round((cfg.llm_weight * rubric + cfg.heuristic_weight * p.scores.heuristic) / w, 4)
        p.scores.final = p.scores.quality


def judge_with_llm(
    llm: LLMClient,
    promos: Sequence[Promo],
    beats_by_ep: dict[int, list[Beat]],
    *,
    batch_size: int,
    temperature: float | None,
    seed: int | None = 7,
    with_camera: bool = True,
) -> int:
    """Fill rubric scores in place. Returns how many promos were judged.

    Clips are shuffled with a fixed seed before batching: input sorted by a
    prior score would bias the judge by position and cluster similar clips in
    one batch (compressing their scores). The fixed seed keeps runs - and the
    prompt cache - reproducible.
    """
    prompt = load_prompt("judge")
    order = list(promos)
    if seed is not None:
        random.Random(seed).shuffle(order)
    judged = 0
    for i in range(0, len(order), batch_size):
        batch = order[i : i + batch_size]
        clips = "\n\n".join(clip_transcript(p, beats_by_ep, with_camera) for p in batch)
        system, user = prompt.render(clips=clips, camera_note=CAMERA_NOTE if with_camera else "")
        try:
            raw = llm.complete_json(
                system, user, temperature=temperature,
                schema=judge_schema([p.promo_id for p in batch]), schema_name="clip_scores",
            )
            resp = _JudgeResponse.model_validate(raw)
        except (LLMError, ValidationError) as exc:
            log.warning("Judge batch %d failed (%s); falling back to heuristics", i // batch_size + 1, exc)
            for p in batch:
                p.flags.append("not scored by LLM judge (heuristic score only)")
            continue

        by_id = {s.clip_id.strip(): s for s in resp.scores}
        for p in batch:
            s = by_id.get(p.promo_id)
            if s is None:
                p.flags.append("judge skipped this clip (heuristic score only)")
                continue
            for k in RUBRIC:
                setattr(p.scores, k, float(getattr(s, k)))
            p.stranger_view = s.stranger_view.strip()
            if not p.why_it_works and s.why_it_works:
                p.why_it_works = s.why_it_works.strip()
            if s.standalone <= 2:
                p.flags.append("judge: may not make sense to a new viewer")
            judged += 1
    return judged
