import json

import pytest

from promo_finder.evaluate import evaluate
from promo_finder.review import (
    CLEAR,
    adjusted_segments,
    append_review,
    approved_posting_order,
    clip_key,
    gold_from_reviews,
    load_reviews,
    review_summary,
)


def _promo(pid, rank, ep, start, end):
    return {"promo_id": pid, "rank": rank, "segments": [{"episode": ep, "start": start, "end": end}],
            "hook": {"text": f"hook {pid}"}, "cliffhanger": {"text": f"cliff {pid}"}}


def test_reviews_follow_the_clip_not_the_id(tmp_path):
    """Regression: IDs are reassigned on re-runs; a review must stay with its clip."""
    fb = tmp_path / "feedback.jsonl"
    append_review(fb, _promo("L-E05-1", 3, 5, 10.0, 40.0), "approve")
    rerun = [_promo("L-E05-1", 1, 5, 90.0, 120.0), _promo("L-E05-2", 2, 5, 10.0, 40.0)]  # IDs swapped clips
    reviews = load_reviews(fb)
    assert clip_key(rerun[0]["segments"]) not in reviews
    assert reviews[clip_key(rerun[1]["segments"])]["verdict"] == "approve"


def test_latest_review_wins_and_old_records_without_key_still_match(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    p = _promo("H-E01-1", 1, 1, 46.8, 70.4)
    # an old-format record (no clip_key), then a newer verdict
    fb.write_text(json.dumps({"promo_id": "H-E01-1", "verdict": "approve", "segments": p["segments"]}) + "\n"
                  + "not json\n", encoding="utf-8")
    append_review(fb, p, "reject", note="cuts on the answer")
    rev = load_reviews(fb)[clip_key(p["segments"])]
    assert rev["verdict"] == "reject" and rev["note"] == "cuts on the answer"


def test_invalid_verdict_rejected(tmp_path):
    with pytest.raises(ValueError):
        append_review(tmp_path / "f.jsonl", _promo("X", 1, 1, 0, 30), "maybe")


def test_nudges_are_applied_and_clamped():
    p = _promo("A", 1, 1, 1.0, 30.0)
    segs = adjusted_segments(p, {"nudges": [{"segment": 0, "start_nudge_s": -5.0, "end_nudge_s": 1.5}]})
    assert segs[0]["start"] == 0.0 and segs[0]["end"] == 31.5  # start clamped at 0
    assert segs[0]["start_tc"] == "00:00.00" and segs[0]["end_tc"] == "00:31.50"
    assert adjusted_segments(p, None)[0]["end"] == 30.0


def test_approved_posting_order_and_gold_and_summary(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    promos = [_promo("A", 1, 1, 0.0, 30.0), _promo("B", 2, 2, 10.0, 40.0), _promo("C", 3, 3, 5.0, 35.0)]
    append_review(fb, promos[0], "reject")
    append_review(fb, promos[1], "adjust", nudges=[{"segment": 0, "start_nudge_s": 1.0, "end_nudge_s": 0.0}])
    append_review(fb, promos[2], "approve")
    reviews = load_reviews(fb)

    rows = approved_posting_order(promos, reviews)
    assert [r["promo_id"] for r in rows] == ["B", "C"] and [r["post_order"] for r in rows] == [1, 2]
    assert rows[0]["segments"] == "E02 00:11.00-00:40.00" and rows[0]["duration_s"] == 29.0

    assert review_summary(promos, reviews) == {"reviewed": 3, "approved": 2, "rejected": 1, "acceptance_rate": 0.667}

    gold = gold_from_reviews("Show", promos, reviews)
    assert [g["episode"] for g in gold["promos"]] == [2, 3]
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    preds = tmp_path / "promos.json"
    preds.write_text(json.dumps({"promos": [{**p, "segments": [{**p["segments"][0]}]} for p in promos]}), encoding="utf-8")
    assert evaluate(preds, gold_path, top_k=3)["gold_recall"] == 1.0  # the gold file works with `eval`


def test_clear_undoes_a_mark_and_keeps_the_history(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    p = _promo("A", 1, 1, 0.0, 30.0)
    append_review(fb, p, "approve")
    append_review(fb, p, CLEAR)
    assert load_reviews(fb) == {}  # unreviewed again
    assert review_summary([p], load_reviews(fb))["reviewed"] == 0
    append_review(fb, p, "reject")  # can be re-marked after clearing
    assert load_reviews(fb)[clip_key(p["segments"])]["verdict"] == "reject"
    assert len(fb.read_text(encoding="utf-8").splitlines()) == 3  # append-only: nothing deleted
