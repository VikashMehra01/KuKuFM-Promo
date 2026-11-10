"""Story bible: who is who, built once per show from loglines (1 LLM call)."""

from __future__ import annotations

import logging
from collections import Counter

from pydantic import ValidationError

from .llm import LLMClient, LLMError
from .models import Character, Show, StoryBible
from .prompts import load_prompt
from .schemas import bible_schema

log = logging.getLogger(__name__)


def _speaker_counts(show: Show) -> Counter:
    return Counter(b.speaker for ep in show.episodes for b in ep.blocks if b.speaker)


def heuristic_bible(show: Show) -> StoryBible:
    """No-API fallback: top speakers as characters, first logline as premise."""
    top = [name for name, _ in _speaker_counts(show).most_common(12)]
    premise = show.episodes[0].logline if show.episodes else ""
    return StoryBible(premise=premise, characters=[Character(name=n) for n in top])


def build_bible(show: Show, llm: LLMClient | None, temperature: float | None = None) -> tuple[StoryBible, bool]:
    """Return (bible, used_llm). Falls back to the heuristic bible on any LLM failure."""
    if llm is None:
        return heuristic_bible(show), False

    speakers = "\n".join(f"- {name} ({n})" for name, n in _speaker_counts(show).most_common(40))
    loglines = "\n".join(f"E{ep.number:02d}: {ep.logline}" for ep in show.episodes)
    system, user = load_prompt("bible").render(show_name=show.name, speakers=speakers, loglines=loglines)
    try:
        data = llm.complete_json(system, user, temperature=temperature, schema=bible_schema(), schema_name="story_bible")
        bible = StoryBible.model_validate(data)
    except (LLMError, ValidationError) as exc:
        log.warning("Story bible LLM call failed (%s); using heuristic bible", exc)
        return heuristic_bible(show), False
    if not bible.characters:
        log.warning("Story bible came back empty; using heuristic bible")
        return heuristic_bible(show), False
    return bible, True
