"""Validator + snapper: map beat IDs to real times and enforce the hard rules.

Rules (from the brief), each with an automatic repair where one is safe:

=================================  ==============================================
Rule                               Repair
=================================  ==============================================
beat IDs exist, one ep / segment   reject
<= max_segments segments           reject
hook inside segment 1, <= 3 s in   move segment start to the hook beat
ends on the cliffhanger beat       trim end to cliff beat / extend to it
total 20-60 s                      too long: trim to an earlier cliff-like beat or
                                   start on a later hook; too short: reject
no cut in the middle of a line     extend to finish the line (<= 2 s)
cuts on camera-shot edges          snap within ``shot_snap_s`` if rules still hold
segments do not overlap            reject
=================================  ==============================================

Every repair is recorded in ``promo.flags`` so the editor sees what changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import PromoConfig
from .models import Beat, BeatKind, Moment, Promo, PromoStatus, Segment

_EPS = 1e-6
_CLIFF_MIN = 0.35  # minimum heuristic cliff score to accept a repaired end beat
_HOOK_MIN = 0.35


@dataclass
class ValidationContext:
    beat_index: dict[str, Beat]
    beats_by_ep: dict[int, list[Beat]]
    shot_edges: dict[int, list[float]] = field(default_factory=dict)


@dataclass
class _Seg:
    """Mutable working copy of a segment during validation."""

    episode: int
    first: int  # beat index within the episode
    last: int
    start: float = 0.0
    end: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start


def _times(seg: _Seg, beats: list[Beat]) -> tuple[float, float]:
    span = beats[seg.first : seg.last + 1]
    return span[0].start, max(b.end for b in span)


def _total(segs: list[_Seg]) -> float:
    return sum(s.duration for s in segs)


def _finish_lines(seg: _Seg, beats: list[Beat], flags: list[str], max_extend: float = 2.0) -> None:
    """Avoid cutting a line of dialogue in half at the segment's end."""
    for b in beats[seg.last + 1 :]:
        if b.start >= seg.end - _EPS:
            break
        if b.kind is BeatKind.DIALOGUE and b.end > seg.end + 0.3:
            if b.end - seg.end <= max_extend:
                flags.append(f"E{seg.episode:02d}: end extended {b.end - seg.end:.1f}s to finish a line")
                seg.end = b.end
            else:
                flags.append(f"E{seg.episode:02d}: cut lands mid-line ({b.id}); check edit")


def _snap(seg: _Seg, edges: list[float], window: float, beats: list[Beat]) -> None:
    """Move cuts onto nearby camera-shot boundaries (start earlier / end later only)."""
    if not edges or window <= 0:
        return
    before = [e for e in edges if seg.start - window - _EPS <= e <= seg.start + _EPS]
    if before:
        new_start = before[-1]  # closest edge at/before the cut = start of the shot containing it
        mid_line = any(
            b.kind is BeatKind.DIALOGUE and b.start < new_start - _EPS < b.end - 0.3 for b in beats[: seg.first]
        )
        if not mid_line:
            seg.start = new_start
    after = [e for e in edges if seg.end - _EPS <= e <= seg.end + window + _EPS]
    if after:
        seg.end = after[0]


def _shrink_montage(segs: list[_Seg], ctx: "ValidationContext", cfg: PromoConfig, hook: Beat) -> bool:
    """Fit a multi-segment promo into ~75% of max length by trimming whole beats.

    Models are poor at summing durations, so code does it: each segment gets an
    equal share of the budget. Segment 1 keeps its start (the hook) and loses
    beats from its end; the last segment keeps its end (the cliffhanger) and
    loses beats from its start; middle segments lose beats from their end.
    """
    if len(segs) < 2:
        return False
    budget = cfg.max_duration_s * 0.75 / len(segs)
    changed = False
    for k, s in enumerate(segs):
        beats = ctx.beats_by_ep[s.episode]
        is_last = k == len(segs) - 1
        floor = hook.index if k == 0 else s.first  # never cut away the hook
        while s.duration > budget and s.first < s.last:
            if is_last:
                s.first += 1
            elif s.last > floor:
                s.last -= 1
            else:
                break
            s.start, s.end = _times(s, beats)
            changed = True
    return changed


def _trim_to_earlier_cliff(segs: list[_Seg], beats: list[Beat], cfg: PromoConfig) -> Beat | None:
    """Shorten the last segment so it ends on the latest cliff-like beat that fits."""
    last = segs[-1]
    others = _total(segs[:-1])
    for j in range(last.last - 1, last.first - 1, -1):
        cand = beats[j]
        if cand.features.cliff < _CLIFF_MIN:
            continue
        end = max(b.end for b in beats[last.first : j + 1])
        total = others + (end - last.start)
        if cfg.min_duration_s <= total <= cfg.max_duration_s:
            last.last, last.end = j, end
            return cand
    return None


def _start_on_later_hook(segs: list[_Seg], beats: list[Beat], cfg: PromoConfig) -> Beat | None:
    """Shorten the first segment by starting on a later strong hook beat."""
    first = segs[0]
    rest = _total(segs[1:])
    for i in range(first.first + 1, first.last + 1):
        cand = beats[i]
        if cand.features.hook < _HOOK_MIN:
            continue
        total = rest + (first.end - cand.start)
        if cfg.min_duration_s <= total <= cfg.max_duration_s:
            first.first, first.start = i, cand.start
            return cand
        if total < cfg.min_duration_s:
            break
    return None


def validate_promo(promo: Promo, ctx: ValidationContext, cfg: PromoConfig) -> Promo:
    """Resolve and check ``promo`` in place; returns it with status VALID or REJECTED."""
    promo.status = PromoStatus.DRAFT
    promo.reject_reasons.clear()
    flags = promo.flags

    if not promo.segments:
        promo.reject("no segments")
        return promo
    if len(promo.segments) > cfg.max_segments:
        promo.reject(f"{len(promo.segments)} segments > max {cfg.max_segments}")
        return promo

    # ---- 1. resolve beat IDs -------------------------------------------------
    segs: list[_Seg] = []
    for seg in promo.segments:
        sb, eb = ctx.beat_index.get(seg.start_beat), ctx.beat_index.get(seg.end_beat)
        if sb is None or eb is None:
            promo.reject(f"unknown beat id in segment {seg.start_beat}..{seg.end_beat}")
            return promo
        if sb.episode != eb.episode:
            promo.reject(f"segment spans episodes {seg.start_beat}..{seg.end_beat}")
            return promo
        if sb.index > eb.index:
            sb, eb = eb, sb
            flags.append(f"segment {sb.id}..{eb.id} was reversed; swapped")
        segs.append(_Seg(episode=sb.episode, first=sb.index, last=eb.index))

    # ---- 2. hook must be in segment 1 ---------------------------------------
    first = segs[0]
    first_beats = ctx.beats_by_ep[first.episode]
    hook = ctx.beat_index.get(promo.hook.beat_id)
    if hook is None or hook.episode != first.episode or hook.index > first.last:
        flags.append("hook beat not in first segment; using segment's first beat")
        hook = first_beats[first.first]
    elif hook.index < first.first:
        flags.append("segment start moved back to the hook beat")
        first.first = hook.index

    # ---- 3. cliffhanger must end the last segment ----------------------------
    last = segs[-1]
    last_beats = ctx.beats_by_ep[last.episode]
    cliff = ctx.beat_index.get(promo.cliffhanger.beat_id)
    if cliff is None or cliff.episode != last.episode or cliff.index < last.first:
        flags.append("cliffhanger beat not in last segment; using segment's last beat")
        cliff = last_beats[last.last]
    elif cliff.index > last.last:
        flags.append("end extended to reach the cliffhanger beat")
        last.last = cliff.index
    elif cliff.index < last.last:
        flags.append("end trimmed to land on the cliffhanger beat")
        last.last = cliff.index

    for s in segs:
        s.start, s.end = _times(s, ctx.beats_by_ep[s.episode])

    # ---- 4. hook within the first N seconds ---------------------------------
    if hook.start - first.start > cfg.hook_window_s + _EPS:
        flags.append(f"start moved {hook.start - first.start:.1f}s later so the hook lands in the first {cfg.hook_window_s:g}s")
        first.first = hook.index
        first.start, first.end = _times(first, first_beats)

    # ---- 5. duration ---------------------------------------------------------
    if _total(segs) > cfg.max_duration_s and len(segs) > 1:
        before = _total(segs)
        if _shrink_montage(segs, ctx, cfg, hook):
            flags.append(f"montage trimmed from {before:.0f}s to {_total(segs):.0f}s (equal share per segment)")
    if _total(segs) > cfg.max_duration_s:
        if (new_cliff := _trim_to_earlier_cliff(segs, last_beats, cfg)) is not None:
            flags.append(f"too long; trimmed to end on earlier cliff beat {new_cliff.id}")
            cliff = new_cliff
        elif (new_hook := _start_on_later_hook(segs, first_beats, cfg)) is not None:
            flags.append(f"too long; now starts on later hook beat {new_hook.id}")
            hook = new_hook
        else:
            promo.reject(f"too long ({_total(segs):.1f}s) and no safe trim")
            return promo
    if _total(segs) < cfg.min_duration_s:
        promo.reject(f"too short ({_total(segs):.1f}s)")
        return promo

    # ---- 6. clean cuts: finish lines, snap to shots (only if still valid) ----
    for s in segs:
        beats = ctx.beats_by_ep[s.episode]
        _finish_lines(s, beats, flags)
        before = (s.start, s.end)
        _snap(s, ctx.shot_edges.get(s.episode, []), cfg.shot_snap_s, beats)
        hook_ok = s is not first or hook.start - s.start <= cfg.hook_window_s + _EPS
        if not hook_ok or _total(segs) > cfg.max_duration_s:
            s.start, s.end = before
    if _total(segs) > cfg.max_duration_s + _EPS:
        promo.reject(f"too long after finishing lines ({_total(segs):.1f}s)")
        return promo

    # ---- 7. segments must not overlap ---------------------------------------
    for i, a in enumerate(segs):
        for b in segs[i + 1 :]:
            if a.episode == b.episode and a.start < b.end - _EPS and b.start < a.end - _EPS:
                promo.reject(f"segments overlap in E{a.episode:02d}")
                return promo

    # ---- write back ----------------------------------------------------------
    promo.segments = [
        Segment(
            episode=s.episode,
            start_beat=ctx.beats_by_ep[s.episode][s.first].id,
            end_beat=ctx.beats_by_ep[s.episode][s.last].id,
            start=round(s.start, 3),
            end=round(s.end, 3),
        )
        for s in segs
    ]
    promo.hook = Moment(beat_id=hook.id, episode=hook.episode, t=hook.start, text=hook.text)
    promo.cliffhanger = Moment(beat_id=cliff.id, episode=cliff.episode, t=cliff.start, text=cliff.text)
    promo.status = PromoStatus.VALID
    return promo
