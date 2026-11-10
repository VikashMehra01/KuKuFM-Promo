"""Shared fixtures. Uses a synthetic screenplay (not Kuku content) so tests run anywhere."""

from __future__ import annotations

from pathlib import Path

import pytest

from promo_finder.beats import build_alias_map, build_show_beats, camera_edges
from promo_finder.config import Config
from promo_finder.models import Show
from promo_finder.parse import parse_episode_text
from promo_finder.validate import ValidationContext


def _line(kind: str, start: float, end: float, text: str) -> str:
    def tc(t: float) -> str:
        return f"{int(t // 60):02d}:{t % 60:05.2f}"

    return f"**{kind}:**\nStart: ({tc(start)}) | End: ({tc(end)})\n{text}\n"


def make_episode(number: int, *, length: float = 70.0, uppercase_keys: bool = False) -> str:
    """A ~70s synthetic episode with a clear hook at 10s and a cliffhanger near 45s."""
    sc = "SCENE START" if uppercase_keys else "Scene start"
    se = "SCENE END" if uppercase_keys else "Scene end"
    sh = "SCENE HEADING" if uppercase_keys else "Scene heading"
    body = [
        f"EPISODE NUMBER: Episode {number}",
        f"EPISODE LOGLINE: Synthetic episode {number}: Mara is betrayed by her brother Tom.",
        "CONTINUITY NOTES: Test fixture.",
        "",
        "--- SCRIPT START ---",
        "",
        "SCENE 01",
        f"{sh}: INT. HALL – DAY",
        f"{sc}: 00:00.00",
        f"{se}: {int(length // 60):02d}:{length % 60:05.2f}",
        "",
        "Actions and dialogue:",
        "",
    ]
    t = 0.0
    while t < length:  # camera shots every 4s
        body.append(_line("CAMERA", t, min(t + 4, length), f"Shot at {t:.0f}s of the hall."))
        t += 4
    events = [
        ("MARA (Calm)", 2.0, 4.0, '"Good morning."'),
        ("TOM (Calm)", 5.0, 7.0, '"Morning."'),
        ("ACTION", 10.0, 11.5, "Tom slaps Mara across the face."),
        ("MARA (Shocked, Horrified)", 12.0, 14.0, '"You struck me!"'),
        ("TOM (Cold)", 15.0, 18.0, '"You were never my sister."'),
        ("LADY MARA (Desperate)", 19.0, 22.0, '"What do you mean?"'),
        ("TOM (Cruel)", 23.0, 27.0, '"Father left everything to me. You get nothing."'),
        ("MARA (Pleading)", 28.0, 31.0, '"Please, there must be a mistake."'),
        ("TOM (Menacing)", 32.0, 36.0, '"Guards, take her away."'),
        ("MARA (Terrified)", 37.0, 40.0, '"Who are you really?"'),
        ("SOUND FX", 41.0, 44.0, "A low drone swells to a cliffhanger peak."),
        ("TOM (Calm)", 50.0, 53.0, '"Let us eat."'),
        ("MARA (Calm)", 55.0, 58.0, '"Fine."'),
    ]
    for kind, s, e, text in events:
        if s < length:
            body.append(_line(kind, s, min(e, length), text))
    body.append("--- SCRIPT END ---")
    return "\n".join(body)


@pytest.fixture
def episode_text() -> str:
    return make_episode(1)


@pytest.fixture
def show_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "Test Show"
    folder.mkdir()
    for n in (1, 2, 3):
        (folder / f"episode-{n:03d}.txt").write_text(make_episode(n, uppercase_keys=(n == 2)), encoding="utf-8")
    return folder


@pytest.fixture
def show(show_dir: Path) -> Show:
    from promo_finder.parse import load_show

    return load_show(show_dir)


@pytest.fixture
def ctx(show: Show) -> ValidationContext:
    beats = build_show_beats(show, build_alias_map(show))
    return ValidationContext(
        beat_index={b.id: b for bl in beats.values() for b in bl},
        beats_by_ep=beats,
        shot_edges={ep.number: camera_edges(ep) for ep in show.episodes},
    )


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def single_episode(episode_text: str):
    return parse_episode_text(episode_text, "episode-001.txt")
