"""Camera lines in prompts: full text, each shot listed once, and shown to the judge."""
from promo_finder.beats import new_camera_shots, render_beats
from promo_finder.judge import clip_transcript
from promo_finder.models import Beat, BeatKind, Moment, Promo, Segment

LONG = "Close-up on the bride as the groom raises a hand and slaps her across the face in front of every guest"
SHOT_B = "Wide shot of the guests gasping"


def _beats() -> list[Beat]:
    mk = lambda i, kind, s, e, text, vis="", speaker=None: Beat(  # noqa: E731
        id=f"E01-B{i:03d}", episode=1, index=i - 1, kind=kind, speaker=speaker, start=s, end=e, text=text, visual=vis
    )
    return [
        mk(1, BeatKind.DIALOGUE, 0.0, 2.0, "How dare you.", LONG, "GROOM"),
        mk(2, BeatKind.DIALOGUE, 2.0, 4.0, "You knew.", f"{LONG} / {SHOT_B}", "BRIDE"),  # same shot spans both lines
        mk(3, BeatKind.VISUAL, 4.0, 6.0, "Insert shot of a ring falling to the floor."),
        mk(4, BeatKind.ACTION, 6.0, 25.0, "She runs out.", SHOT_B),
    ]


def test_full_camera_text_and_each_shot_listed_once():
    out = render_beats(_beats()).splitlines()
    assert out[0].endswith(f"| cam: {LONG}")  # no 110-char cap
    assert out[1].endswith(f"| cam: {SHOT_B}")  # LONG not repeated on the next line
    assert "| cam:" not in out[2]  # a VISUAL beat's text is the shot itself
    assert "| cam:" not in out[3]  # same shot as the previous line (VISUAL beats are skipped when comparing)


def test_legacy_settings_reproduce_the_original_rendering():
    beats = _beats()
    legacy = render_beats(beats, max_visual=110, dedupe_camera=False).splitlines()
    assert legacy == [b.render(max_visual=110) for b in beats]
    assert legacy[1].endswith("…") and len(LONG) + len(SHOT_B) > 110


def test_new_camera_shots_without_dedupe_returns_everything():
    assert new_camera_shots(_beats(), dedupe=False)[1] == f"{LONG} / {SHOT_B}"


def test_judge_sees_camera_text_only_when_enabled():
    promo = Promo(
        promo_id="T1", source="llm",
        segments=[Segment(episode=1, start_beat="E01-B001", end_beat="E01-B004", start=0.0, end=25.0)],
        hook=Moment(beat_id="E01-B001"), cliffhanger=Moment(beat_id="E01-B004"),
    )
    beats = {1: _beats()}
    with_cam = clip_transcript(promo, beats)
    assert "slaps her" in with_cam and with_cam.count(LONG) == 1
    without = clip_transcript(promo, beats, with_camera=False)
    assert "slaps her" not in without and "[visual] Insert shot" in without
