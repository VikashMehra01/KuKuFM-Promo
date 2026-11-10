"""Rank valid promos into a posting order.

score -> dedupe (time overlap) -> funnel bonus (early episodes) ->
greedy ordering with a diversity penalty (avoid same hook type / same
episode back-to-back).
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import RankingConfig
from .judge import RUBRIC
from .models import Promo, PromoStatus


def overlap_iou(a: Promo, b: Promo) -> float:
    """Temporal IoU of two promos across all their segments."""
    inter = 0.0
    for sa in a.segments:
        for sb in b.segments:
            if sa.episode == sb.episode:
                inter += max(0.0, min(sa.end, sb.end) - max(sa.start, sb.start))
    union = a.duration + b.duration - inter
    return inter / union if union > 0 else 0.0


def funnel_bonus(promo: Promo, cfg: RankingConfig) -> float:
    """New viewers who tap a promo start at E1, so early-episode clips convert best."""
    ep = promo.first_episode
    if cfg.funnel_episodes <= 0 or ep > cfg.funnel_episodes:
        return 0.0
    return cfg.funnel_bonus * (1 - (ep - 1) / cfg.funnel_episodes)


def dedupe(promos: Sequence[Promo], threshold: float) -> list[Promo]:
    """Keep the best of any group of promos that cover mostly the same time."""
    kept: list[Promo] = []
    for p in sorted(promos, key=lambda x: -x.scores.quality):
        dup = next((k for k in kept if overlap_iou(p, k) >= threshold), None)
        if dup is None:
            kept.append(p)
        else:
            p.reject(f"duplicate of {dup.promo_id} (overlap {overlap_iou(p, dup):.0%})")
    return kept


def stratified_shortlist(promos: Sequence[Promo], k: int) -> list[Promo]:
    """Pick ``k`` promos for the judge, alternating between sources.

    Why: the pre-judge ranking uses the heuristic score, and heuristic
    candidates were *selected* to maximise that same score - so a plain
    top-k lets them crowd out LLM candidates before the judge ever sees them
    (real run: 62 LLM clips cut, yet judged LLM and heuristic clips scored
    the same, 3.25 vs 3.26). Interleaving the best of each source gives every
    source a fair hearing; leftover seats go to whoever has candidates left.
    """
    groups: dict[str, list[Promo]] = {}
    for p in sorted(promos, key=lambda x: -x.scores.quality):
        groups.setdefault("llm" if p.source.startswith("llm") else "heuristic", []).append(p)
    queues = list(groups.values())
    picked: list[Promo] = []
    while len(picked) < k and any(queues):
        for q in queues:
            if q and len(picked) < k:
                picked.append(q.pop(0))
    return picked


def _reason(p: Promo, bonus: float, for_variety: bool) -> str:
    parts = [f"score {p.scores.final * 100:.0f}"]
    rubric = {k: getattr(p.scores, k) for k in RUBRIC if getattr(p.scores, k) is not None}
    if rubric:
        best = sorted(rubric.items(), key=lambda kv: -kv[1])[:2]
        parts.append(", ".join(f"{k} {v:.0f}/5" for k, v in best))
    if p.hook.text:
        parts.append(f"opens on a {p.hook_type.value.replace('_', ' ')}")
    if bonus > 0:
        parts.append(f"from E{p.first_episode:02d}, so new viewers land near the start")
    if len(p.segments) > 1:
        parts.append(f"{len(p.segments)}-moment montage")
    if for_variety and bonus == 0:
        parts.append("placed ahead of a higher raw score for variety")
    return "; ".join(parts)


def rank_promos(promos: Sequence[Promo], cfg: RankingConfig) -> list[Promo]:
    """Return the posting order (top ``cfg.top_n``) and set ``rank``/``final``/``post_reason``."""
    valid = [p for p in promos if p.status is PromoStatus.VALID]
    pool = dedupe(valid, cfg.dedupe_iou)
    bonuses = {p.promo_id: funnel_bonus(p, cfg) for p in pool}

    ordered: list[Promo] = []
    while pool and len(ordered) < cfg.top_n:
        recent_eps = {ep for q in ordered[-2:] for ep in q.episodes}
        prev_type = ordered[-1].hook_type if ordered else None

        def adjusted(p: Promo) -> tuple[float, bool]:
            penalty = 0.0
            if prev_type is not None and p.hook_type == prev_type:
                penalty += cfg.diversity_penalty
            if recent_eps & set(p.episodes):
                penalty += cfg.diversity_penalty
            return p.scores.quality + bonuses[p.promo_id] - penalty, penalty > 0

        best = max(pool, key=lambda p: adjusted(p)[0])
        value, _ = adjusted(best)
        best.scores.final = round(value, 4)
        best.rank = len(ordered) + 1
        # picked ahead of a higher raw-quality clip -> chosen for variety / funnel
        for_variety = any(p.scores.quality > best.scores.quality for p in pool if p is not best)
        best.post_reason = _reason(best, bonuses[best.promo_id], for_variety)
        ordered.append(best)
        pool.remove(best)

    for p in pool:  # valid but below the cut
        p.rank = None
    return ordered
