"""Candidate generation: per-episode LLM pass + one cross-episode pass.

The LLM returns beat IDs only. Everything here is converted into draft
``Promo`` objects; ``validate.py`` resolves and checks them.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator, model_validator

from .beats import render_beats
from .llm import LLMClient, LLMError
from .models import Beat, Episode, HookType, Moment, Promo, Segment, StoryBible
from .prompts import load_prompt
from .schemas import batch_schema, cross_schema
from .timecode import short_tc

log = logging.getLogger(__name__)

# ----------------------------------------------------------------- lenient LLM schemas


class _Lenient(BaseModel):
    model_config = {"extra": "ignore"}


def _keep_valid(model: type[BaseModel], items: object, where: str) -> list:
    """Validate list items one by one: a single malformed item is dropped (and
    logged) instead of invalidating the whole batch of episodes."""
    if not isinstance(items, list):
        return []
    kept = []
    for item in items:
        try:
            kept.append(model.model_validate(item))
        except ValidationError as exc:
            log.warning("Dropped malformed %s from LLM: %s", where, exc.errors()[0].get("msg", exc))
    return kept


class RawHook(_Lenient):
    beat_id: str
    type: str = "other"
    strength: float = 3
    note: str = ""


class RawCliff(_Lenient):
    beat_id: str
    strength: float = 3
    unresolved: str = ""


class RawSegment(_Lenient):
    start_beat: str
    end_beat: str


class RawCandidate(_Lenient):
    title: str = ""
    start_beat: str | None = None  # single-segment form
    end_beat: str | None = None
    segments: list[RawSegment] = Field(default_factory=list)  # multi-segment form
    # Redundant by the prompt's own rules (hook = start, cliff = end), so models
    # sometimes omit them; default them instead of rejecting the candidate.
    hook_beat: str | None = None
    cliffhanger_beat: str | None = None
    hook_type: str = "other"
    why_it_works: str = ""

    @field_validator("hook_beat", "cliffhanger_beat", "start_beat", "end_beat", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() or None if isinstance(v, str) else v

    @model_validator(mode="after")
    def _defaults(self) -> "RawCandidate":
        segs = self.resolved_segments()
        if not segs:
            raise ValueError("candidate has no segments")
        self.hook_beat = self.hook_beat or segs[0].start_beat
        self.cliffhanger_beat = self.cliffhanger_beat or segs[-1].end_beat
        return self

    def resolved_segments(self) -> list[RawSegment]:
        if self.segments:
            return self.segments
        if self.start_beat and self.end_beat:
            return [RawSegment(start_beat=self.start_beat, end_beat=self.end_beat)]
        return []


class EpisodeAnalysis(_Lenient):
    episode: int | str | None = None  # "E03" or 3 in batch responses
    # optional extras (older responses / providers without schema support)
    hooks: list[RawHook] = Field(default_factory=list)
    cliffhangers: list[RawCliff] = Field(default_factory=list)
    # "clips" in the lean schema; "candidates" in older cached responses
    candidates: list[RawCandidate] = Field(default_factory=list, validation_alias=AliasChoices("clips", "candidates"))

    @field_validator("hooks", mode="before")
    @classmethod
    def _hooks(cls, v):
        return _keep_valid(RawHook, v, "hook")

    @field_validator("cliffhangers", mode="before")
    @classmethod
    def _cliffs(cls, v):
        return _keep_valid(RawCliff, v, "cliffhanger")

    @field_validator("candidates", mode="before")
    @classmethod
    def _candidates(cls, v):
        return _keep_valid(RawCandidate, v, "candidate")

    def episode_number(self) -> int | None:
        if isinstance(self.episode, int):
            return self.episode
        if isinstance(self.episode, str):
            digits = "".join(ch for ch in self.episode if ch.isdigit())
            return int(digits) if digits else None
        return None


class BatchAnalysis(_Lenient):
    episodes: list[EpisodeAnalysis] = Field(default_factory=list)

    @field_validator("episodes", mode="before")
    @classmethod
    def _episodes(cls, v):
        return _keep_valid(EpisodeAnalysis, v, "episode entry")


class CrossAnalysis(_Lenient):
    candidates: list[RawCandidate] = Field(default_factory=list, validation_alias=AliasChoices("montages", "candidates"))

    @field_validator("candidates", mode="before")
    @classmethod
    def _candidates(cls, v):
        return _keep_valid(RawCandidate, v, "montage candidate")


# ----------------------------------------------------------------- conversion


def _episode_of(beat_id: str) -> int:
    """``"E03-B017"`` -> 3. Raises ``ValueError`` on malformed IDs."""
    head = beat_id.split("-", 1)[0]
    if not head.upper().startswith("E"):
        raise ValueError(f"bad beat id {beat_id!r}")
    return int(head[1:])


def raw_to_promo(raw: RawCandidate, promo_id: str, source: str, beat_index: dict[str, Beat]) -> Promo | None:
    """Convert one LLM candidate to a draft promo; ``None`` if it is unusable."""
    segments: list[Segment] = []
    for seg in raw.resolved_segments():
        try:
            ep_start, ep_end = _episode_of(seg.start_beat), _episode_of(seg.end_beat)
        except ValueError:
            log.debug("%s: malformed beat id in %s", promo_id, seg)
            return None
        if ep_start != ep_end:
            log.debug("%s: segment spans episodes (%s)", promo_id, seg)
            return None
        segments.append(Segment(episode=ep_start, start_beat=seg.start_beat, end_beat=seg.end_beat))
    if not segments:
        return None

    def moment(beat_id: str) -> Moment:
        beat = beat_index.get(beat_id)
        if beat is None:
            return Moment(beat_id=beat_id)
        return Moment(beat_id=beat_id, episode=beat.episode, t=beat.start, text=beat.text)

    return Promo(
        promo_id=promo_id,
        source=source,
        title=raw.title.strip(),
        segments=segments,
        hook=moment(raw.hook_beat),
        cliffhanger=moment(raw.cliffhanger_beat),
        hook_type=HookType.coerce(raw.hook_type),
        why_it_works=raw.why_it_works.strip(),
    )


# ----------------------------------------------------------------- batching


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 chars/token for English). Good enough for packing."""
    return len(text) // 4 + 1


def pack_batches(
    episodes: Sequence[Episode],
    beats_by_ep: dict[int, list[Beat]],
    *,
    token_budget: int,
    max_per_call: int,
    camera: CameraText | None = None,
) -> list[list[Episode]]:
    """Group consecutive episodes into batches bounded by beat tokens and count.

    Consecutive order is kept on purpose: neighbouring episodes give the model
    context for each other's cliffhangers. An episode larger than the budget
    still gets a batch of its own.
    """
    camera = camera or CameraText()
    batches: list[list[Episode]] = []
    current: list[Episode] = []
    used = 0
    for ep in episodes:
        cost = estimate_tokens(camera.render(beats_by_ep.get(ep.number, [])))
        if current and (used + cost > token_budget or len(current) >= max_per_call):
            batches.append(current)
            current, used = [], 0
        current.append(ep)
        used += cost
    if current:
        batches.append(current)
    return batches


@dataclass(frozen=True)
class CameraText:
    """How camera lines appear in generator prompts (see ``PromoConfig.camera_*``)."""

    max_chars: int | None = None  # None = full text
    dedupe: bool = True  # list a shot once, on the first line it covers

    def render(self, beats: Sequence[Beat]) -> str:
        return render_beats(beats, max_visual=self.max_chars, dedupe_camera=self.dedupe)

    @property
    def format_note(self) -> str:
        note = "| cam: visual"
        return note + "; a camera shot is listed once, on the first line it covers" if self.dedupe else note


def _episode_block(ep: Episode, beats: Sequence[Beat], camera: CameraText) -> str:
    label = f"E{ep.number:02d}"
    return (
        f"=== EPISODE {label} ===\n"
        f"SUMMARY: {ep.logline}\n"
        f"BEATS (format: ID [start-end] SPEAKER (emotion): \"line\" {camera.format_note})\n"
        f"{camera.render(beats)}"
    )


# ----------------------------------------------------------------- per batch


@dataclass
class BatchResult:
    promos: list[Promo] = field(default_factory=list)
    analyses: dict[int, EpisodeAnalysis] = field(default_factory=dict)
    covered: set[int] = field(default_factory=set)  # episodes that got >= 1 usable candidate


def generate_batch_candidates(
    llm: LLMClient,
    episodes: Sequence[Episode],
    beats_by_ep: dict[int, list[Beat]],
    bible: StoryBible,
    prev_logline: str,
    next_logline: str,
    *,
    n_candidates: int,
    min_s: float,
    max_s: float,
    temperature: float | None,
    beat_index: dict[str, Beat],
    clips_min: int = 2,
    fixed_count: bool = False,
    camera: CameraText | None = None,
) -> BatchResult:
    """One LLM call for 1..N consecutive episodes.

    Candidates are assigned to episodes by their beat-ID prefix (not by the
    label the model wrote), so a mislabelled entry cannot corrupt another
    episode. Callers should re-ask for any episode missing from ``covered``.
    """
    wanted = {ep.number for ep in episodes}
    labels = ", ".join(f"E{n:02d}" for n in sorted(wanted))
    system, user = load_prompt("episode_candidates").render(
        bible=bible.render(),
        prev_logline=prev_logline or "(none - the batch starts at the first episode)",
        next_logline=next_logline or "(none)",
        episodes="\n\n".join(
            _episode_block(ep, beats_by_ep.get(ep.number, []), camera or CameraText()) for ep in episodes
        ),
        episode_labels=labels,
        clips_min=min(clips_min, n_candidates),
        n_candidates=n_candidates,
        min_s=int(min_s),
        max_s=int(max_s),
    )
    schema = batch_schema(
        [f"E{n:02d}" for n in sorted(wanted)], min(clips_min, n_candidates), n_candidates, fixed_count=fixed_count
    )
    data = llm.complete_json(system, user, temperature=temperature, schema=schema, schema_name="episode_batch")
    try:
        # tolerate a single-episode answer without the "episodes" wrapper
        batch = BatchAnalysis.model_validate(data if "episodes" in data else {"episodes": [data]})
    except ValidationError as exc:
        raise LLMError(f"{labels}: response did not match schema: {exc.error_count()} errors") from exc

    result = BatchResult()
    per_episode_count: dict[int, int] = {}
    for analysis in batch.episodes:
        ep_num = analysis.episode_number()
        if ep_num is None and len(wanted) == 1:
            ep_num = next(iter(wanted))
        if ep_num in wanted:
            result.analyses[ep_num] = analysis
        for raw in analysis.candidates:
            segs = raw.resolved_segments()
            try:
                owner = _episode_of(segs[0].start_beat) if segs else None
            except ValueError:
                owner = None
            if owner not in wanted:
                log.debug("dropping candidate for unexpected episode %s", owner)
                continue
            n = per_episode_count.get(owner, 0) + 1
            if n > n_candidates:
                continue
            promo = raw_to_promo(raw, f"L-E{owner:02d}-{n}", "llm", beat_index)
            if promo is not None:
                per_episode_count[owner] = n
                result.promos.append(promo)
                result.covered.add(owner)
    return result


# ----------------------------------------------------------------- cross episode


def build_moment_index(
    episodes: Sequence[Episode],
    analyses: dict[int, EpisodeAnalysis],
    beats_by_ep: dict[int, list[Beat]],
    beat_index: dict[str, Beat],
    per_episode: int = 2,
) -> str:
    """Compact per-episode index of the best hook and cliff beats (LLM or heuristic)."""
    lines: list[str] = []
    for ep in episodes:
        analysis = analyses.get(ep.number)
        # The model's own clips are the best evidence of strong moments: use their hook and
        # end beats, then top up with the strongest rule-based beats (deduplicated).
        beats = beats_by_ep.get(ep.number, [])
        llm_hooks = [c.hook_beat for c in analysis.candidates] if analysis else []
        llm_cliffs = [c.cliffhanger_beat for c in analysis.candidates] if analysis else []
        rule_hooks = [b.id for b in sorted(beats, key=lambda b: -b.features.hook)]
        rule_cliffs = [b.id for b in sorted(beats, key=lambda b: -b.features.cliff)]
        hook_ids = list(dict.fromkeys(x for x in llm_hooks + rule_hooks if x))[:per_episode]
        cliff_ids = list(dict.fromkeys(x for x in llm_cliffs + rule_cliffs if x))[:per_episode]

        lines.append(f"E{ep.number:02d}: {ep.logline}")
        for tag, ids in (("HOOK", hook_ids), ("CLIFF", cliff_ids)):
            for bid in ids:
                beat = beat_index.get(bid)
                if beat is None:
                    continue
                who = f"{beat.speaker}: " if beat.speaker else ""
                lines.append(f"  {tag} {beat.id} [{short_tc(beat.start)}-{short_tc(beat.end)}] {who}{beat.text[:120]}")
    return "\n".join(lines)


def generate_cross_candidates(
    llm: LLMClient,
    bible: StoryBible,
    index: str,
    *,
    n_candidates: int,
    min_s: float,
    max_s: float,
    temperature: float | None,
    beat_index: dict[str, Beat],
) -> list[Promo]:
    system, user = load_prompt("cross_episode").render(
        bible=bible.render(), index=index, n_candidates=n_candidates, min_s=int(min_s), max_s=int(max_s)
    )
    data = llm.complete_json(
        system, user, temperature=temperature, schema=cross_schema(n_candidates, max_segments=3), schema_name="montages"
    )
    try:
        analysis = CrossAnalysis.model_validate(data)
    except ValidationError as exc:
        raise LLMError(f"cross-episode response did not match schema: {exc.error_count()} errors") from exc

    promos = []
    for n, raw in enumerate(analysis.candidates[:n_candidates], start=1):
        promo = raw_to_promo(raw, f"X-{n:02d}", "llm-cross", beat_index)
        if promo is not None:
            promos.append(promo)
    return promos
