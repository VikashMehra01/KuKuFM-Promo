from pathlib import Path

import pytest

from promo_finder.beats import build_alias_map, build_show_beats
from promo_finder.models import BeatKind, BlockType
from promo_finder.parse import ParseError, _parse_header, load_show, parse_episode_text
from promo_finder.timecode import format_timecode, parse_timecode


@pytest.mark.parametrize(
    "tc, seconds",
    [("00:00.00", 0.0), ("01:05.60", 65.6), ("02:18.5", 138.5), ("1:02:03.25", 3723.25)],
)
def test_timecode_roundtrip(tc, seconds):
    assert parse_timecode(tc) == pytest.approx(seconds)


def test_format_timecode():
    assert format_timecode(65.6) == "01:05.60"
    with pytest.raises(ValueError):
        format_timecode(-1)
    with pytest.raises(ValueError):
        parse_timecode("abc")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CAMERA", (BlockType.CAMERA, None, None, None)),
        ("SOUND FX", (BlockType.SFX, None, None, None)),
        ("MATRIARCH (O.S.) (Firm)", (BlockType.DIALOGUE, "MATRIARCH", "O.S.", "Firm")),
        ("LADY MARA (Confused, Breathless)", (BlockType.DIALOGUE, "LADY MARA", None, "Confused, Breathless")),
        ("TOM'S MOTHER", (BlockType.DIALOGUE, "TOM'S MOTHER", None, None)),
    ],
)
def test_parse_header(raw, expected):
    assert _parse_header(raw) == expected


def test_parse_episode(single_episode):
    ep = single_episode
    assert ep.number == 1
    assert "betrayed" in ep.logline
    assert len(ep.scenes) == 1 and ep.scenes[0].heading == "INT. HALL – DAY"
    dialogue = [b for b in ep.blocks if b.type is BlockType.DIALOGUE]
    assert dialogue[0].text == "Good morning."  # quotes stripped
    assert dialogue[0].emotion == "Calm"
    assert ep.duration == pytest.approx(70.0)


def test_uppercase_scene_keys(show):
    ep2 = show.episode(2)
    assert ep2.scenes[0].start == 0.0 and ep2.scenes[0].end == pytest.approx(70.0)


def test_missing_timing_raises():
    bad = "EPISODE NUMBER: Episode 1\nSCENE 01\n**CAMERA:**\nno timing here\n"
    with pytest.raises(ParseError, match="no Start/End"):
        parse_episode_text(bad, "bad.txt")


def test_episode_number_from_filename():
    text = "SCENE 01\n**ACTION:**\nStart: (00:01.00) | End: (00:02.00)\nShe runs.\n"
    ep = parse_episode_text(text, "episode-007-x.txt")
    assert ep.number == 7 and ep.blocks[0].id.startswith("E07-")


def test_load_show_folder_and_zip(show_dir: Path, tmp_path: Path):
    import zipfile

    show = load_show(show_dir)
    assert show.name == "Test Show" and [e.number for e in show.episodes] == [1, 2, 3]

    zpath = tmp_path / "pack.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for f in show_dir.glob("*.txt"):
            zf.write(f, f"Test Show/{f.name}")
    zshow = load_show(zpath)
    assert zshow.name == "Test Show" and len(zshow.episodes) == 3


def test_missing_input_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_show(tmp_path / "nope")


def test_beats_aliases_and_visual(show):
    aliases = build_alias_map(show)
    assert aliases["LADY MARA"] == "MARA"  # honorific stripped when base name exists
    beats = build_show_beats(show, aliases)[1]
    assert all(b.speaker != "LADY MARA" for b in beats)
    assert [b.start for b in beats] == sorted(b.start for b in beats)
    slap = next(b for b in beats if "slaps" in b.text)
    assert slap.kind is BeatKind.ACTION and "physical" in slap.features.tags
    assert slap.features.hook > 0.3
    # camera shots with nothing else happening become VISUAL beats
    assert any(b.kind is BeatKind.VISUAL for b in beats)
    # dialogue beats carry the overlapping camera description
    assert next(b for b in beats if b.text == "Good morning.").visual.startswith("Shot at 0s")


def test_filter_aliases_rejects_people_who_talk_to_each_other(show):
    from promo_finder.beats import filter_aliases

    # MARA and TOM argue in the same scene -> they cannot be one person.
    # NARRATOR never speaks, so nothing contradicts that alias.
    accepted, rejected = filter_aliases(show, {"TOM": "MARA", "NARRATOR": "MARA", "MARA": "MARA"})
    assert "TOM" not in accepted and any(r.startswith("TOM -> MARA") for r in rejected)
    assert accepted["NARRATOR"] == "MARA" and accepted["MARA"] == "MARA"


def test_filter_aliases_uses_title_stripped_names():
    """Regression: the model aliased the heroine's rival to the heroine. In that
    scene the heroine is credited as 'LADY MARA', not 'MARA'."""
    from promo_finder.beats import filter_aliases
    from promo_finder.models import Show

    def ep(n, lines):
        body = "\n".join(f"**{who}:**\nStart: (00:0{i}.00) | End: (00:0{i}.50)\n\"Line.\"" for i, who in enumerate(lines))
        return parse_episode_text(f"EPISODE NUMBER: Episode {n}\nSCENE 01\n{body}\n", f"e{n}.txt")

    show = Show(name="t", episodes=[ep(1, ["LADY MARA", "WOMAN IN RED"]), ep(2, ["MARA", "TOM"])])
    accepted, rejected = filter_aliases(show, {"WOMAN IN RED": "MARA", "LADY MARA": "MARA"})
    assert "WOMAN IN RED" not in accepted and "LADY MARA" in accepted
    assert any("WOMAN IN RED" in r and "LADY MARA" in r for r in rejected)


@pytest.mark.skipif(not Path("input_pack").exists(), reason="confidential input pack not present")
def test_real_input_pack_parses_cleanly():
    show = load_show("input_pack")
    assert len(show.episodes) == 50
    assert not show.warnings
    assert all(ep.blocks and ep.logline for ep in show.episodes)
