"""The schemas sent to the model and the models that parse the answers must agree.

This is what keeps a single source of truth: if a field is added to a schema
but not to the parser (or vice versa), these tests fail.
"""

import json

from promo_finder.generate import BatchAnalysis, CrossAnalysis, RawCandidate
from promo_finder.judge import _RawScore
from promo_finder.models import StoryBible
from promo_finder.schemas import CLIP_FIELDS, batch_schema, bible_schema, cross_schema, judge_schema


def _props(schema: dict) -> set[str]:
    return set(schema["properties"])


def test_clip_schema_fields_are_exactly_what_the_parser_reads():
    clip = batch_schema(["E01"], 2, 3, fixed_count=False)["properties"]["episodes"]["items"]["properties"]["clips"]["items"]
    assert _props(clip) == set(CLIP_FIELDS)
    assert set(CLIP_FIELDS) <= set(RawCandidate.model_fields)
    assert "cliffhanger_beat" not in clip["properties"]  # derived from end_beat in code, not asked for


def test_lean_schema_has_no_redundant_lists_or_openai_only_flags():
    text = json.dumps(batch_schema([f"E{n:02d}" for n in range(1, 7)], 2, 3, fixed_count=False))
    assert '"hooks"' not in text and '"cliffhangers"' not in text
    assert "strict" not in text and "additionalProperties" not in text


def test_fixed_count_is_a_switch():
    labels = ["E01", "E02", "E03"]
    on = batch_schema(labels, 2, 3, fixed_count=True)["properties"]["episodes"]
    off = batch_schema(labels, 2, 3, fixed_count=False)["properties"]["episodes"]
    assert on["minItems"] == on["maxItems"] == 3
    assert "minItems" not in off and "maxItems" not in off


def test_a_schema_shaped_answer_parses_with_derived_fields():
    answer = {"episodes": [{"episode": "E01", "clips": [
        {"start_beat": "E01-B005", "end_beat": "E01-B020", "hook_beat": "E01-B005",
         "hook_type": "physical", "title": "t", "why_it_works": "w"}]}]}
    cand = BatchAnalysis.model_validate(answer).episodes[0].candidates[0]
    assert cand.cliffhanger_beat == "E01-B020"  # derived = end_beat

    montage = {"montages": [{"segments": [{"start_beat": "E01-B005", "end_beat": "E01-B009"},
                                          {"start_beat": "E02-B030", "end_beat": "E02-B034"}],
                             "hook_type": "reveal", "title": "t", "why_it_works": "w"}]}
    m = CrossAnalysis.model_validate(montage).candidates[0]
    assert m.hook_beat == "E01-B005" and m.cliffhanger_beat == "E02-B034"  # derived from segments


def test_other_schemas_match_their_parsers():
    assert _props(bible_schema()) <= set(StoryBible.model_fields)
    montage_item = cross_schema(6, 3)["properties"]["montages"]["items"]
    assert _props(montage_item) <= set(RawCandidate.model_fields)
    score_item = judge_schema(["A", "B"])["properties"]["scores"]["items"]
    assert _props(score_item) <= set(_RawScore.model_fields)
