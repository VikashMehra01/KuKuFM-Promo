"""JSON Schemas sent as structured output (``response_format: json_schema``).

Design rules (from real runs, see docs/RUN_REPORT.md):

* **Lean: ask only for what the model must decide.** Anything code can derive
  is not requested - e.g. the cliffhanger beat *is* ``end_beat`` (it matched in
  25/25 responses when it was asked for separately). Fewer fields and less
  nesting keep the schema under Gemini's grammar-complexity limit (exceeding
  it returns a bare ``400 INVALID_ARGUMENT``) and keep output short.
* **The schema is the only format spec.** Prompts describe the task, not the
  JSON. If a provider rejects a schema, the client appends this same schema to
  the prompt (see ``openai_compat``), so there is never a second, drifting copy.
* Widely supported subset only: type / properties / required / items / enum /
  minItems / maxItems / minimum / maximum / description.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import HookType

HOOK_TYPES = [h.value for h in HookType]
BEAT_ID = "beat ID exactly as listed, e.g. E03-B017"


def _obj(properties: dict, required: Sequence[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(required or properties)}


def _str(description: str = "") -> dict:
    return {"type": "string", "description": description} if description else {"type": "string"}


def _score() -> dict:
    return {"type": "integer", "minimum": 1, "maximum": 5}


_HOOK_TYPE = {"type": "string", "enum": HOOK_TYPES}

# Fields of one single-segment clip. ``generate.RawCandidate`` must accept all of them
# (enforced by tests/test_schemas.py).
CLIP_FIELDS = ("start_beat", "end_beat", "hook_beat", "hook_type", "title", "why_it_works")


def _clip() -> dict:
    return _obj({
        "start_beat": _str(BEAT_ID),
        "end_beat": _str(BEAT_ID + "; the clip cuts here, on the unresolved moment"),
        "hook_beat": _str(BEAT_ID + "; the attention-grabbing beat, = start_beat or starts within 3s of it"),
        "hook_type": _HOOK_TYPE,
        "title": _str("max 8 words"),
        "why_it_works": _str("one sentence, max 25 words"),
    })


def bible_schema() -> dict:
    return _obj({
        "premise": _str("2 sentences: the hook of the whole show, for a stranger"),
        "characters": {
            "type": "array",
            "maxItems": 15,
            "items": _obj({
                "name": _str("canonical UPPERCASE speaker name"),
                "aliases": {"type": "array", "items": _str("other UPPERCASE speaker name for the SAME person")},
                "role": _str("max 12 words"),
            }),
        },
    })


def batch_schema(episode_labels: Sequence[str], clips_min: int, clips_max: int, *, fixed_count: bool) -> dict:
    """Clips per episode for one batch.

    ``fixed_count`` adds ``minItems = maxItems = len(labels)`` so the decoder
    must cover every episode. It made the old (heavier) schema exceed Gemini's
    limit at 6 episodes; with the lean schema it is a config switch, and the
    pipeline's re-ask still enforces coverage when it is off.
    """
    labels = list(episode_labels)
    episodes: dict = {
        "type": "array",
        "description": f"one entry for EACH of: {', '.join(labels)}",
        "items": _obj({
            "episode": {"type": "string", "enum": labels},
            "clips": {"type": "array", "minItems": clips_min, "maxItems": clips_max, "items": _clip()},
        }),
    }
    if fixed_count:
        episodes["minItems"] = episodes["maxItems"] = len(labels)
    return _obj({"episodes": episodes})


def cross_schema(n_candidates: int, max_segments: int) -> dict:
    """Montages: hook = first segment's start, cliffhanger = last segment's end (derived)."""
    return _obj({
        "montages": {
            "type": "array",
            "maxItems": n_candidates,
            "items": _obj({
                "segments": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": max_segments,
                    "items": _obj({"start_beat": _str(BEAT_ID), "end_beat": _str(BEAT_ID)}),
                },
                "hook_type": _HOOK_TYPE,
                "title": _str("max 8 words"),
                "why_it_works": _str("one sentence, max 25 words"),
            }),
        }
    })


def judge_schema(clip_ids: Sequence[str]) -> dict:
    ids = list(clip_ids)
    return _obj({
        "scores": {
            "type": "array",
            "minItems": len(ids),
            "maxItems": len(ids),
            "items": _obj({
                "clip_id": {"type": "string", "enum": ids},
                "hook": _score(),
                "cliffhanger": _score(),
                "standalone": _score(),
                "intensity": _score(),
                "trope_fit": _score(),
                "stranger_view": _str("what a stranger thinks is happening, max 20 words"),
                "why_it_works": _str("one sentence on why it works (or fails), max 25 words"),
            }),
        }
    })
