"""JSON Schemas sent to the model as ``response_format: json_schema``.

With structured output the provider *constrains decoding* to the schema, so
required fields cannot be omitted and enums cannot be violated. This removes
a whole failure class (e.g. a missing ``cliffhanger_beat`` invalidating a
6-episode batch). The lenient pydantic parsing in ``generate.py`` stays as a
second safety net for providers that ignore the schema.

Kept to the widely supported subset (type / properties / required / items /
enum / minItems / maxItems / description): no ``$ref``, ``oneOf`` or
``additionalProperties``, which some OpenAI-compatible backends reject.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import HookType

HOOK_TYPES = [h.value for h in HookType]


def _obj(properties: dict, required: Sequence[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(required or properties)}


def _str(description: str = "") -> dict:
    return {"type": "string", "description": description} if description else {"type": "string"}


def _strength() -> dict:
    return {"type": "integer", "minimum": 1, "maximum": 5}


_CANDIDATE_SINGLE = _obj({
    "title": _str("max 8 words"),
    "start_beat": _str("beat ID, e.g. E03-B017"),
    "end_beat": _str("beat ID; this is the cliffhanger beat"),
    "hook_beat": _str("beat ID; start_beat or within 3s of it"),
    "cliffhanger_beat": _str("beat ID; must equal end_beat"),
    "hook_type": {"type": "string", "enum": HOOK_TYPES},
    "why_it_works": _str("one sentence, max 25 words"),
})


def bible_schema() -> dict:
    return _obj({
        "premise": _str(),
        "characters": {
            "type": "array",
            "maxItems": 15,
            "items": _obj({"name": _str(), "aliases": {"type": "array", "items": _str()}, "role": _str()}),
        },
        "arcs": {"type": "array", "items": _str()},
        "tropes": {"type": "array", "items": _str()},
    })


def batch_schema(episode_labels: Sequence[str], n_candidates: int) -> dict:
    """Per-episode entries, labels restricted to this batch.

    Measured limit (Gemini, Nov 2026): fixing the outer array size
    (``minItems = maxItems = N``) is rejected with a bare INVALID_ARGUMENT for
    N >= 5-6 episodes, and so is an object with one required key per episode;
    the total schema complexity is the limit. Field-level constraints (required
    fields, enums, >= 1 candidate) stay in the schema; per-episode *coverage* is
    enforced in code instead (``pipeline`` re-asks any episode that is missing).
    """
    labels = list(episode_labels)
    return _obj({
        "episodes": {
            "type": "array",
            "description": f"one entry for EACH of: {', '.join(labels)}",
            "items": _obj({
                "episode": {"type": "string", "enum": labels},
                "hooks": {
                    "type": "array",
                    "items": _obj({
                        "beat_id": _str(),
                        "type": {"type": "string", "enum": HOOK_TYPES},
                        "strength": _strength(),
                        "note": _str(),
                    }),
                },
                "cliffhangers": {
                    "type": "array",
                    "items": _obj({"beat_id": _str(), "strength": _strength(), "unresolved": _str()}),
                },
                "candidates": {"type": "array", "minItems": 1, "maxItems": n_candidates, "items": _CANDIDATE_SINGLE},
            }),
        }
    })


def cross_schema(n_candidates: int, max_segments: int) -> dict:
    return _obj({
        "candidates": {
            "type": "array",
            "maxItems": n_candidates,
            "items": _obj({
                "title": _str(),
                "segments": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": max_segments,
                    "items": _obj({"start_beat": _str(), "end_beat": _str()}),
                },
                "hook_beat": _str("= segment 1 start_beat"),
                "cliffhanger_beat": _str("= last segment end_beat"),
                "hook_type": {"type": "string", "enum": HOOK_TYPES},
                "why_it_works": _str(),
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
                "hook": _strength(),
                "cliffhanger": _strength(),
                "standalone": _strength(),
                "intensity": _strength(),
                "trope_fit": _strength(),
                "stranger_view": _str(),
                "why_it_works": _str(),
            }),
        }
    })
