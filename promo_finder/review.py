"""Editor reviews: store them, match them to clips, and turn them into outputs.

Reviews are matched to a clip by its *content* (episode + start/end of every
segment), not by promo ID: IDs such as ``L-E05-1`` are reassigned on every
run, so an ID-keyed review could attach itself to a different clip after a
re-run. Older records (written before ``clip_key`` existed) still carry their
segments, so their key is recomputed from those.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .timecode import format_timecode

VERDICTS = ("approve", "reject", "adjust")
APPROVED = ("approve", "adjust")


def clip_key(segments: list[dict]) -> str:
    """Stable identity of a clip: 'E01@46.8-70.4|E03@12.0-20.5'."""
    return "|".join(f"E{int(s['episode']):02d}@{float(s['start']):.1f}-{float(s['end']):.1f}" for s in segments)


def load_reviews(path: Path) -> dict[str, dict]:
    """Latest review per clip key (later lines win). Malformed lines are skipped."""
    latest: dict[str, dict] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            key = rec.get("clip_key") or clip_key(rec["segments"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if rec.get("verdict") in VERDICTS:
            latest[key] = rec
    return latest


def append_review(path: Path, promo: dict, verdict: str, nudges: list[dict] | None = None, note: str = "") -> dict:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "clip_key": clip_key(promo["segments"]),
        "promo_id": promo["promo_id"],
        "rank": promo.get("rank"),
        "verdict": verdict,
        "segments": promo["segments"],
        "nudges": nudges or [],
        "note": note,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def adjusted_segments(promo: dict, review: dict | None) -> list[dict]:
    """The clip's segments with the editor's start/end nudges applied (clamped to stay valid)."""
    nudges = {n.get("segment"): n for n in (review or {}).get("nudges", [])}
    out = []
    for i, seg in enumerate(promo["segments"]):
        n = nudges.get(i, {})
        start = max(0.0, float(seg["start"]) + float(n.get("start_nudge_s", 0.0)))
        end = max(start + 0.1, float(seg["end"]) + float(n.get("end_nudge_s", 0.0)))
        out.append({"episode": seg["episode"], "start": round(start, 2), "end": round(end, 2),
                    "start_tc": format_timecode(start), "end_tc": format_timecode(end)})
    return out


def approved_posting_order(promos: list[dict], reviews: dict[str, dict]) -> list[dict]:
    """Approved/adjusted promos in rank order, with nudged timestamps - the hand-off list."""
    rows = []
    for p in sorted(promos, key=lambda x: x.get("rank") or 10**9):
        rev = reviews.get(clip_key(p["segments"]))
        if not rev or rev["verdict"] not in APPROVED:
            continue
        segs = adjusted_segments(p, rev)
        rows.append({
            "post_order": len(rows) + 1,
            "tool_rank": p.get("rank"),
            "promo_id": p["promo_id"],
            "verdict": rev["verdict"],
            "segments": " + ".join(f"E{s['episode']:02d} {s['start_tc']}-{s['end_tc']}" for s in segs),
            "duration_s": round(sum(s["end"] - s["start"] for s in segs), 1),
            "hook": p["hook"]["text"],
            "cliffhanger": p["cliffhanger"]["text"],
            "note": rev.get("note", ""),
        })
    return rows


def gold_from_reviews(show: str, promos: list[dict], reviews: dict[str, dict]) -> dict:
    """Approved clips in the ``eval/gold.json`` format (one entry per segment, nudges applied).

    Use it to score *future* runs (other models, prompts, settings) against what
    an editor accepted. Scoring the same run against its own approvals only
    gives the acceptance rate, not recall.
    """
    entries = []
    for p in promos:
        rev = reviews.get(clip_key(p["segments"]))
        if not rev or rev["verdict"] not in APPROVED:
            continue
        for i, s in enumerate(adjusted_segments(p, rev)):
            entries.append({"episode": s["episode"], "start": s["start_tc"], "end": s["end_tc"],
                            "note": f"{p['promo_id']} seg {i + 1} ({rev['verdict']})"})
    return {"show": show, "source": "editor reviews", "promos": entries}


def review_summary(promos: list[dict], reviews: dict[str, dict]) -> dict:
    verdicts = [reviews[k]["verdict"] for k in (clip_key(p["segments"]) for p in promos) if k in reviews]
    approved = sum(v in APPROVED for v in verdicts)
    return {
        "reviewed": len(verdicts),
        "approved": approved,
        "rejected": sum(v == "reject" for v in verdicts),
        "acceptance_rate": round(approved / len(verdicts), 3) if verdicts else None,
    }
