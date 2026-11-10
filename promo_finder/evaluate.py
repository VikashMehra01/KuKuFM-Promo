"""Evaluation against a hand-labelled gold set, and run-to-run stability.

Gold file format (``eval/gold.json``)::

    {"show": "...", "promos": [
        {"episode": 1, "start": "00:33.70", "end": "01:12.40", "note": "altar betrayal"}
    ]}

A predicted promo *matches* a gold promo when they share an episode and the
overlap covers >= ``min_overlap`` of the gold clip.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .timecode import parse_timecode


@dataclass
class Clip:
    label: str
    episode: int
    start: float
    end: float


def _load_predictions(path: Path, top_k: int) -> list[list[Clip]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    promos = sorted(data["promos"], key=lambda p: p["rank"] or 1e9)[:top_k]
    return [
        [Clip(p["promo_id"], s["episode"], float(s["start"]), float(s["end"])) for s in p["segments"]]
        for p in promos
    ]


def _load_gold(path: Path) -> list[Clip]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Clip(g.get("note", f"gold-{i}"), int(g["episode"]), parse_timecode(g["start"]), parse_timecode(g["end"]))
        for i, g in enumerate(data["promos"], start=1)
    ]


def _overlap(a: Clip, b: Clip) -> float:
    if a.episode != b.episode:
        return 0.0
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def evaluate(promos_path: str | Path, gold_path: str | Path, top_k: int = 10, min_overlap: float = 0.5) -> dict:
    preds = _load_predictions(Path(promos_path), top_k)
    gold = _load_gold(Path(gold_path))
    if not gold:
        raise ValueError("gold set is empty - add hand-picked promos first")

    matched_gold: set[int] = set()
    hits, boundary_errors = 0, []
    details = []
    for segs in preds:
        best_g, best_cov = None, 0.0
        for gi, g in enumerate(gold):
            cov = sum(_overlap(s, g) for s in segs) / max(1e-6, g.end - g.start)
            if cov > best_cov:
                best_g, best_cov = gi, cov
        hit = best_g is not None and best_cov >= min_overlap
        if hit:
            hits += 1
            matched_gold.add(best_g)
            g = gold[best_g]
            boundary_errors.append((abs(segs[0].start - g.start) + abs(segs[-1].end - g.end)) / 2)
        details.append({
            "promo": segs[0].label,
            "matched_gold": gold[best_g].label if hit else None,
            "coverage": round(best_cov, 2),
        })

    return {
        "top_k": len(preds),
        "precision_at_k": round(hits / len(preds), 3) if preds else 0.0,
        "gold_recall": round(len(matched_gold) / len(gold), 3),
        "mean_boundary_error_s": round(sum(boundary_errors) / len(boundary_errors), 2) if boundary_errors else None,
        "details": details,
    }


def stability(run_a: str | Path, run_b: str | Path, top_k: int = 10, min_iou: float = 0.5) -> dict:
    """How many of run A's top-k have a near-identical promo in run B's top-k."""
    a, b = _load_predictions(Path(run_a), top_k), _load_predictions(Path(run_b), top_k)

    def iou(x: list[Clip], y: list[Clip]) -> float:
        inter = sum(_overlap(s, t) for s in x for t in y)
        union = sum(s.end - s.start for s in x) + sum(t.end - t.start for t in y) - inter
        return inter / union if union > 0 else 0.0

    shared = sum(1 for x in a if any(iou(x, y) >= min_iou for y in b))
    return {"top_k": len(a), "shared": shared, "agreement": round(shared / len(a), 3) if a else 0.0}
