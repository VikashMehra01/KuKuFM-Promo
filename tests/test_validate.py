import pytest

from promo_finder.config import PromoConfig
from promo_finder.models import Moment, Promo, PromoStatus, Segment
from promo_finder.validate import validate_promo


def _beat_at(ctx, ep, text_contains):
    return next(b for b in ctx.beats_by_ep[ep] if text_contains in b.text)


def _promo(ctx, start, end, hook=None, cliff=None, ep=1, segments=None):
    s, e = _beat_at(ctx, ep, start), _beat_at(ctx, ep, end)
    h = _beat_at(ctx, ep, hook) if hook else s
    c = _beat_at(ctx, ep, cliff) if cliff else e
    return Promo(
        promo_id="T-1",
        source="test",
        segments=segments or [Segment(episode=ep, start_beat=s.id, end_beat=e.id)],
        hook=Moment(beat_id=h.id),
        cliffhanger=Moment(beat_id=c.id),
    )


def test_valid_promo_resolves_times(ctx, cfg):
    p = validate_promo(_promo(ctx, "slaps", "cliffhanger peak"), ctx, cfg.promo)
    assert p.status is PromoStatus.VALID, p.reject_reasons
    seg = p.segments[0]
    assert seg.start == pytest.approx(10.0)  # shot edge at 8s is outside the 1s snap window
    assert cfg.promo.min_duration_s <= p.duration <= cfg.promo.max_duration_s
    assert p.hook.t - seg.start <= cfg.promo.hook_window_s
    assert p.cliffhanger.text.startswith("A low drone")


def test_cuts_snap_to_shot_edges_within_window(ctx):
    wide = PromoConfig(shot_snap_s=2.5)
    p = validate_promo(_promo(ctx, "slaps", "cliffhanger peak"), ctx, wide)
    assert p.status is PromoStatus.VALID, p.reject_reasons
    assert p.segments[0].start == pytest.approx(8.0)  # shot containing the slap starts at 8s
    assert p.segments[0].end == pytest.approx(44.0)  # cliff ends exactly on a shot edge
    assert p.hook.t - p.segments[0].start <= wide.hook_window_s


def test_unknown_beat_rejected(ctx, cfg):
    p = _promo(ctx, "slaps", "cliffhanger peak")
    p.segments[0].end_beat = "E01-B999"
    assert validate_promo(p, ctx, cfg.promo).status is PromoStatus.REJECTED


def test_too_short_rejected(ctx, cfg):
    p = validate_promo(_promo(ctx, "slaps", "You were never"), ctx, cfg.promo)
    assert p.status is PromoStatus.REJECTED and "too short" in p.reject_reasons[0]


def test_hook_too_late_moves_start(ctx, cfg):
    # starts at 2s but the hook (slap) is at 10s -> start must move to the hook
    p = validate_promo(_promo(ctx, "Good morning", "cliffhanger peak", hook="slaps"), ctx, cfg.promo)
    assert p.status is PromoStatus.VALID, p.reject_reasons
    assert p.hook.t - p.segments[0].start <= cfg.promo.hook_window_s
    assert any("hook lands" in f for f in p.flags)


def test_end_trimmed_to_cliffhanger(ctx, cfg):
    p = validate_promo(_promo(ctx, "slaps", "Fine.", cliff="cliffhanger peak"), ctx, cfg.promo)
    assert p.status is PromoStatus.VALID, p.reject_reasons
    assert p.cliffhanger.text.startswith("A low drone")
    assert p.segments[0].end <= 45.0
    assert any("trimmed" in f for f in p.flags)


def test_too_long_is_trimmed_to_earlier_cliff(ctx):
    tight = PromoConfig(min_duration_s=10, max_duration_s=25)
    p = validate_promo(_promo(ctx, "slaps", "cliffhanger peak"), ctx, tight)
    assert p.status is PromoStatus.VALID, p.reject_reasons
    assert p.duration <= 25
    assert any("too long" in f for f in p.flags)


def test_multi_segment_across_episodes(ctx, cfg):
    s1a, s1b = _beat_at(ctx, 1, "slaps"), _beat_at(ctx, 1, "Father left")
    s2a, s2b = _beat_at(ctx, 2, "Guards"), _beat_at(ctx, 2, "cliffhanger peak")
    p = Promo(
        promo_id="X-1",
        source="test",
        segments=[Segment(episode=1, start_beat=s1a.id, end_beat=s1b.id), Segment(episode=2, start_beat=s2a.id, end_beat=s2b.id)],
        hook=Moment(beat_id=s1a.id),
        cliffhanger=Moment(beat_id=s2b.id),
    )
    validate_promo(p, ctx, cfg.promo)
    assert p.status is PromoStatus.VALID, p.reject_reasons
    assert p.episodes == [1, 2]
    assert cfg.promo.min_duration_s <= p.duration <= cfg.promo.max_duration_s


def test_overlong_montage_is_trimmed_per_segment(ctx, cfg):
    """Mirrors a real model error: two near-whole-episode segments (~110s total)."""
    s1a, s1b = _beat_at(ctx, 1, "slaps"), _beat_at(ctx, 1, "Fine.")
    s2a, s2b = _beat_at(ctx, 2, "Good morning"), _beat_at(ctx, 2, "cliffhanger peak")
    p = Promo(
        promo_id="X-long", source="test",
        segments=[Segment(episode=1, start_beat=s1a.id, end_beat=s1b.id), Segment(episode=2, start_beat=s2a.id, end_beat=s2b.id)],
        hook=Moment(beat_id=s1a.id), cliffhanger=Moment(beat_id=s2b.id),
    )
    validate_promo(p, ctx, cfg.promo)
    assert p.status is PromoStatus.VALID, p.reject_reasons
    assert p.duration <= cfg.promo.max_duration_s
    assert p.segments[0].start_beat == s1a.id  # hook kept
    assert p.segments[-1].end_beat == s2b.id  # cliffhanger kept
    assert any("montage trimmed" in f for f in p.flags)


def test_overlapping_segments_rejected(ctx, cfg):
    a, b = _beat_at(ctx, 1, "slaps"), _beat_at(ctx, 1, "cliffhanger peak")
    p = Promo(
        promo_id="O-1",
        source="test",
        segments=[Segment(episode=1, start_beat=a.id, end_beat=b.id), Segment(episode=1, start_beat=a.id, end_beat=b.id)],
        hook=Moment(beat_id=a.id),
        cliffhanger=Moment(beat_id=b.id),
    )
    assert validate_promo(p, ctx, PromoConfig(max_duration_s=120)).status is PromoStatus.REJECTED


def test_too_many_segments_rejected(ctx, cfg):
    a = _beat_at(ctx, 1, "slaps")
    p = Promo(
        promo_id="M-1",
        source="test",
        segments=[Segment(episode=1, start_beat=a.id, end_beat=a.id)] * 4,
        hook=Moment(beat_id=a.id),
        cliffhanger=Moment(beat_id=a.id),
    )
    assert validate_promo(p, ctx, cfg.promo).status is PromoStatus.REJECTED
