"""End-to-end tests: heuristic mode and LLM mode with a fake model."""

import json
import re

from promo_finder.config import Config
from promo_finder.evaluate import evaluate, stability
from promo_finder.llm import FakeLLM, LLMError
from promo_finder.models import HookType, Promo, PromoStatus, Scores, Segment, Moment
from promo_finder.pipeline import run_pipeline
from promo_finder.rank import overlap_iou, rank_promos


def test_heuristic_pipeline_end_to_end(show_dir, tmp_path):
    result = run_pipeline(show_dir, tmp_path / "out", Config(), mode="heuristic")
    assert result.run_log["mode"] == "heuristic"
    assert result.ranked, "should find at least one promo"
    cfg = Config().promo
    for p in result.ranked:
        assert p.status is PromoStatus.VALID
        assert cfg.min_duration_s <= p.duration <= cfg.max_duration_s
        assert p.hook.t - p.segments[0].start <= cfg.hook_window_s + 1e-6
    for name in ("promos_json", "promos_csv", "report", "candidates", "run_log"):
        assert result.paths[name].exists()
    data = json.loads(result.paths["promos_json"].read_text(encoding="utf-8"))
    assert data["promos"][0]["rank"] == 1


def _episode_answer(label: str, section: str) -> dict:
    ids = re.findall(r"^(E\d+-B\d+) ", section, re.M)
    slap = next(i for i in ids if re.search(rf"^{i} .*slaps", section, re.M))
    cliff = next(i for i in ids if re.search(rf"^{i} .*cliffhanger peak", section, re.M))
    return {
        "episode": label,
        "hooks": [{"beat_id": slap, "type": "slap", "strength": 5}],
        "cliffhangers": [{"beat_id": cliff, "strength": 5, "unresolved": "Will she escape?"}],
        "candidates": [{"title": "The slap", "start_beat": slap, "end_beat": cliff,
                        "hook_beat": slap, "cliffhanger_beat": cliff, "hook_type": "slap",
                        "why_it_works": "Instant violence, cut before the escape."}],
    }


def make_fake_handler(skip_once: set[str] | None = None):
    """Plays the LLM using the beat IDs in the prompt. ``skip_once`` episodes are
    left out of the first batch answer that contains them (simulates a lazy model)."""
    skip_once = set(skip_once or ())

    def handler(system: str, user: str) -> dict:
        if "story bible" in system.lower():
            return {"premise": "A woman betrayed by her brother.",
                    "characters": [{"name": "MARA", "aliases": ["LADY MARA"]}]}
        if "Score each clip" in user:
            ids = re.findall(r"^CLIP (\S+)", user, re.M)
            return {"scores": [{"clip_id": i, "hook": 5, "cliffhanger": 4, "standalone": 4, "intensity": 5,
                                "trope_fit": 4, "stranger_view": "A brother betrays his sister."} for i in ids]}
        if "multi-segment" in user:
            return {"candidates": []}
        sections = re.split(r"^=== EPISODE (E\d+) ===$", user, flags=re.M)[1:]
        answers = []
        for label, section in zip(sections[::2], sections[1::2]):
            if label in skip_once:
                skip_once.discard(label)
                continue
            answers.append(_episode_answer(label, section))
        return {"episodes": answers}

    return handler


def test_llm_pipeline_with_fake_model(show_dir, tmp_path):
    fake = FakeLLM(handler=make_fake_handler())
    result = run_pipeline(show_dir, tmp_path / "out", Config(), generator=fake)
    assert result.run_log["mode"] == "llm"
    assert not result.run_log["errors"]
    top = result.ranked[0]
    assert top.source == "llm" and top.hook_type is HookType.PHYSICAL
    assert top.scores.hook == 5 and top.stranger_view
    assert result.bible.premise.startswith("A woman")
    # 3 small episodes fit one batch: bible + 1 generation + 1 cross + 1 judge
    assert result.run_log["llm_calls"]["generation_batches"] == ["E01-E03"]
    assert fake.usage.calls == 4


def test_every_stage_sends_a_structured_output_schema(show_dir, tmp_path):
    fake = FakeLLM(handler=make_fake_handler())
    run_pipeline(show_dir, tmp_path / "out", Config(), generator=fake)
    names = [n for n, s in fake.schemas if s is not None]
    assert names == ["story_bible", "episode_batch", "montages", "clip_scores"]
    batch = dict(fake.schemas)["episode_batch"]["properties"]["episodes"]
    # coverage is enforced by the schema itself: exactly one entry per requested episode
    assert batch["minItems"] == batch["maxItems"] == 3
    assert batch["items"]["properties"]["episode"]["enum"] == ["E01", "E02", "E03"]
    assert "cliffhanger_beat" in batch["items"]["properties"]["candidates"]["items"]["required"]


def test_no_batching_mode_makes_one_call_per_episode(show_dir, tmp_path):
    cfg = Config()
    cfg.promo.max_episodes_per_call = 1
    fake = FakeLLM(handler=make_fake_handler())
    result = run_pipeline(show_dir, tmp_path / "out", cfg, generator=fake)
    assert result.run_log["llm_calls"]["generation_batches"] == ["E01", "E02", "E03"]
    assert {p.promo_id for p in result.all_promos if p.source == "llm"} >= {"L-E01-1", "L-E02-1", "L-E03-1"}


def test_skipped_episode_is_reasked_once(show_dir, tmp_path):
    fake = FakeLLM(handler=make_fake_handler(skip_once={"E02"}))
    result = run_pipeline(show_dir, tmp_path / "out", Config(), generator=fake)
    assert result.run_log["llm_calls"]["episodes_retried"] == ["E02"]
    assert not result.run_log["errors"]  # the follow-up call recovered it
    assert any(p.promo_id == "L-E02-1" for p in result.all_promos)


def test_candidates_are_assigned_by_beat_id_not_label(show, ctx):
    """A model that mislabels an entry must not corrupt another episode."""
    from promo_finder.generate import generate_batch_candidates
    from promo_finder.models import StoryBible

    eps = show.episodes[:2]
    def handler(system, user):
        ans = _episode_answer("E02", user.split("=== EPISODE E02 ===")[0])  # E01 beats, labelled E02
        return {"episodes": [ans]}

    res = generate_batch_candidates(
        FakeLLM(handler=handler), eps, ctx.beats_by_ep, StoryBible(), "", "",
        n_candidates=3, min_s=20, max_s=60, temperature=0, beat_index=ctx.beat_index,
    )
    assert res.covered == {1}
    assert res.promos[0].promo_id == "L-E01-1"


def test_llm_failure_falls_back_to_heuristics(show_dir, tmp_path):
    def broken(system, user):
        raise LLMError("provider down")

    result = run_pipeline(show_dir, tmp_path / "out", Config(), generator=FakeLLM(handler=broken))
    assert result.ranked, "heuristic fallback should still produce promos"
    assert len(result.run_log["errors"]) >= 3  # bible + each episode logged, never silent


def _p(pid, ep, start, end, quality, hook_type=HookType.REVEAL):
    return Promo(
        promo_id=pid, source="test", status=PromoStatus.VALID, hook_type=hook_type,
        segments=[Segment(episode=ep, start_beat="a", end_beat="b", start=start, end=end)],
        hook=Moment(beat_id="a"), cliffhanger=Moment(beat_id="b"), scores=Scores(quality=quality),
    )


def test_rank_dedupes_and_prefers_early_episodes():
    a = _p("A", 30, 0, 30, 0.80)
    dup = _p("A2", 30, 2, 31, 0.70)
    early = _p("B", 1, 0, 30, 0.76, HookType.PHYSICAL)
    assert overlap_iou(a, dup) > 0.5
    ranked = rank_promos([a, dup, early], Config().ranking)
    assert [p.promo_id for p in ranked] == ["B", "A"]  # funnel bonus lifts E1; dup removed
    assert dup.status is PromoStatus.REJECTED


def test_batch_survives_missing_and_malformed_fields():
    """Regression (gemini-3.5-flash): candidates without cliffhanger_beat made the whole
    6-episode batch fail validation. Now redundant fields default, bad items are dropped."""
    from promo_finder.generate import BatchAnalysis

    batch = BatchAnalysis.model_validate({"episodes": [
        {"episode": "E06", "candidates": [
            {"title": "no cliff field", "start_beat": "E06-B027", "end_beat": "E06-B047", "hook_beat": "E06-B027"},
            {"title": "no segments at all"},  # unusable -> dropped, not fatal
        ], "hooks": [{"beat_id": "E06-B027"}, {"strength": 5}]},  # second hook has no id -> dropped
        "not even an object",
    ]})
    assert len(batch.episodes) == 1
    cand = batch.episodes[0].candidates
    assert len(cand) == 1 and cand[0].cliffhanger_beat == "E06-B047" and cand[0].hook_beat == "E06-B027"
    assert len(batch.episodes[0].hooks) == 1


def test_pack_batches_respects_budget_and_cap(show, ctx):
    from promo_finder.beats import render_beats
    from promo_finder.generate import estimate_tokens, pack_batches

    per_ep = estimate_tokens(render_beats(ctx.beats_by_ep[1]))
    assert [len(b) for b in pack_batches(show.episodes, ctx.beats_by_ep, token_budget=10**6, max_per_call=2)] == [2, 1]
    assert [len(b) for b in pack_batches(show.episodes, ctx.beats_by_ep, token_budget=per_ep, max_per_call=10)] == [1, 1, 1]
    # an episode bigger than the budget still gets its own batch (never dropped)
    assert sum(len(b) for b in pack_batches(show.episodes, ctx.beats_by_ep, token_budget=500, max_per_call=6)) == 3


def test_eval_and_stability(show_dir, tmp_path):
    result = run_pipeline(show_dir, tmp_path / "out", Config(), mode="heuristic")
    top = result.ranked[0].segments[0]
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps({"promos": [
        {"episode": top.episode, "start": "00:00.00", "end": "01:00.00", "note": "whole scene"},
    ]}), encoding="utf-8")
    metrics = evaluate(result.paths["promos_json"], gold, top_k=3)
    assert metrics["gold_recall"] == 1.0
    st = stability(result.paths["promos_json"], result.paths["promos_json"])
    assert st["agreement"] == 1.0
