"""Domain models shared by every pipeline stage.

Two families:
* Screenplay: ``Block`` -> ``Scene`` -> ``Episode`` -> ``Show``, plus derived ``Beat``s.
* Promo: ``Segment``/``Moment``/``Scores`` -> ``Promo``.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .timecode import format_timecode, short_tc

VISUAL_SEP = " / "  # joins the camera shots attached to one beat

# --------------------------------------------------------------------------- screenplay


class BlockType(str, Enum):
    CAMERA = "CAMERA"
    ACTION = "ACTION"
    DIALOGUE = "DIALOGUE"
    SFX = "SFX"


class Block(BaseModel):
    """One timestamped entry of the screenplay, exactly as written."""

    id: str
    episode: int
    scene: int
    type: BlockType
    speaker: str | None = None  # raw speaker name (dialogue only)
    delivery: str | None = None  # V.O. / O.S. modifiers
    emotion: str | None = None  # e.g. "Cold, Commanding"
    start: float
    end: float
    text: str


class Scene(BaseModel):
    number: int
    heading: str = ""
    start: float | None = None
    end: float | None = None
    blocks: list[Block] = Field(default_factory=list)


class Episode(BaseModel):
    number: int
    source_file: str
    logline: str = ""
    continuity: str = ""
    scenes: list[Scene] = Field(default_factory=list)

    @property
    def blocks(self) -> list[Block]:
        return [b for s in self.scenes for b in s.blocks]

    @property
    def duration(self) -> float:
        ends = [b.end for b in self.blocks] + [s.end for s in self.scenes if s.end]
        return max(ends, default=0.0)


class Show(BaseModel):
    name: str
    episodes: list[Episode]
    warnings: list[str] = Field(default_factory=list)

    def episode(self, number: int) -> Episode:
        for ep in self.episodes:
            if ep.number == number:
                return ep
        raise KeyError(f"Episode {number} not found")


class BeatKind(str, Enum):
    DIALOGUE = "DIALOGUE"
    ACTION = "ACTION"
    SFX = "SFX"
    VISUAL = "VISUAL"  # a camera shot with no concurrent action/dialogue


class BeatFeatures(BaseModel):
    """Cheap, deterministic signals computed in ``heuristics.py``."""

    hook: float = 0.0  # 0..1 how attention-grabbing this beat is on its own
    cliff: float = 0.0  # 0..1 how unresolved / tense this beat leaves things
    intensity: float = 0.0  # 0..1 emotional intensity
    tags: list[str] = Field(default_factory=list)  # e.g. ["physical", "reveal"]


class Beat(BaseModel):
    """The unit the LLM reasons about and the unit cuts snap to."""

    id: str  # "E01-B023"
    episode: int
    index: int  # position within the episode
    kind: BeatKind
    speaker: str | None = None  # canonical speaker
    emotion: str | None = None
    start: float
    end: float
    text: str
    visual: str = ""  # overlapping camera description
    features: BeatFeatures = Field(default_factory=BeatFeatures)

    def camera_shots(self) -> list[str]:
        """The camera shots overlapping this beat (empty for VISUAL beats, whose text *is* the shot)."""
        if self.kind is BeatKind.VISUAL or not self.visual:
            return []
        return [s for s in self.visual.split(VISUAL_SEP) if s]

    def render(self, max_visual: int | None = None, visual: str | None = None) -> str:
        """Compact one-line form used in prompts.

        ``visual`` overrides the attached camera text (e.g. with repeated shots removed);
        ``max_visual`` truncates it (None = full text).
        """
        head = f"{self.id} [{short_tc(self.start)}-{short_tc(self.end)}]"
        if self.kind is BeatKind.DIALOGUE:
            emo = f" ({self.emotion})" if self.emotion else ""
            body = f'{self.speaker}{emo}: "{self.text}"'
        else:
            body = f"{self.kind.value}: {self.text}"
        vis = self.visual if visual is None else visual
        if vis and self.kind is not BeatKind.VISUAL:
            if max_visual is not None and len(vis) > max_visual:
                vis = vis[: max_visual - 1] + "…"
            body += f" | cam: {vis}"
        return f"{head} {body}"


# --------------------------------------------------------------------------- story bible


class Character(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    role: str = ""


class StoryBible(BaseModel):
    premise: str = ""
    characters: list[Character] = Field(default_factory=list)
    arcs: list[str] = Field(default_factory=list)
    tropes: list[str] = Field(default_factory=list)

    def alias_map(self) -> dict[str, str]:
        """UPPERCASE alias -> canonical UPPERCASE name."""
        mapping: dict[str, str] = {}
        for char in self.characters:
            canonical = char.name.upper().strip()
            mapping[canonical] = canonical
            for alias in char.aliases:
                mapping[alias.upper().strip()] = canonical
        return mapping

    def render(self) -> str:
        chars = "; ".join(
            f"{c.name}" + (f" (aka {', '.join(c.aliases)})" if c.aliases else "") + (f": {c.role}" if c.role else "")
            for c in self.characters
        )
        parts = [f"Premise: {self.premise}", f"Characters: {chars}"]
        if self.arcs:
            parts.append("Arcs: " + " | ".join(self.arcs))
        if self.tropes:
            parts.append("Tropes: " + ", ".join(self.tropes))
        return "\n".join(parts)


# --------------------------------------------------------------------------- promos


class HookType(str, Enum):
    PHYSICAL = "physical"  # slap, grab, fight, collapse
    REVEAL = "reveal"
    SHOCK_LINE = "shock_line"
    THREAT = "threat"
    HUMILIATION = "humiliation"
    ROMANCE = "romance"
    DANGER = "danger"
    OTHER = "other"

    @classmethod
    def coerce(cls, value: str | None) -> "HookType":
        key = (value or "other").strip().lower()
        key = {"slap": "physical", "violence": "physical", "fight": "physical"}.get(key, key)
        try:
            return cls(key)
        except ValueError:
            return cls.OTHER


class PromoStatus(str, Enum):
    DRAFT = "draft"
    VALID = "valid"
    REJECTED = "rejected"


class Segment(BaseModel):
    episode: int
    start_beat: str
    end_beat: str
    start: float = 0.0
    end: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def label(self) -> str:
        return f"E{self.episode:02d} {format_timecode(self.start)}-{format_timecode(self.end)}"


class Moment(BaseModel):
    beat_id: str
    episode: int = 0
    t: float = 0.0
    text: str = ""


class Scores(BaseModel):
    # LLM rubric, 1..5 (None when the judge did not score this promo)
    hook: float | None = None
    cliffhanger: float | None = None
    standalone: float | None = None
    intensity: float | None = None
    trope_fit: float | None = None
    # deterministic, 0..1
    heuristic: float = 0.0
    # combined 0..1 before / after ranking adjustments
    quality: float = 0.0
    final: float = 0.0


class Promo(BaseModel):
    promo_id: str
    source: str  # "llm" | "llm-cross" | "heuristic"
    title: str = ""
    segments: list[Segment]
    hook: Moment
    cliffhanger: Moment
    hook_type: HookType = HookType.OTHER
    why_it_works: str = ""
    stranger_view: str = ""  # what a first-time viewer would understand (judge output)
    scores: Scores = Field(default_factory=Scores)
    flags: list[str] = Field(default_factory=list)
    status: PromoStatus = PromoStatus.DRAFT
    reject_reasons: list[str] = Field(default_factory=list)
    rank: int | None = None
    post_reason: str = ""

    @property
    def duration(self) -> float:
        return round(sum(s.duration for s in self.segments), 2)

    @property
    def episodes(self) -> list[int]:
        return sorted({s.episode for s in self.segments})

    @property
    def first_episode(self) -> int:
        return min(self.episodes) if self.segments else 0

    def reject(self, reason: str) -> None:
        self.status = PromoStatus.REJECTED
        self.reject_reasons.append(reason)
