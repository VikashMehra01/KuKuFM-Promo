"""Deterministic screenplay parser: ``.txt`` files -> ``Show``.

Expected layout per file (case-insensitive keys)::

    EPISODE NUMBER: Episode 1
    EPISODE LOGLINE: ...
    CONTINUITY NOTES: ...
    --- SCRIPT START ---
    SCENE 01
    Scene heading: INT. CATHEDRAL – DAY
    Scene start: 00:00.00
    Scene end: 02:18.00
    **SPEAKER (Emotion):**            <- or **CAMERA:** / **ACTION:** / **SOUND FX:**
    Start: (00:11.30) | End: (00:13.10)
    "Line of dialogue."
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from .models import Block, BlockType, Episode, Scene, Show
from .timecode import parse_timecode

log = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^(EPISODE NUMBER|EPISODE LOGLINE|CONTINUITY NOTES)\s*:\s*(.*)$", re.I)
_SCENE_RE = re.compile(r"^SCENE\s+(\d+)\s*$", re.I)
_SCENE_FIELD_RE = re.compile(r"^SCENE\s+(HEADING|START|END)\s*:\s*(.*)$", re.I)
_HEADER_RE = re.compile(r"^\*\*(.+?):\*\*\s*$")
_TIMING_RE = re.compile(r"^START:\s*\(([^)]+)\)\s*\|\s*END:\s*\(([^)]+)\)\s*$", re.I)
_PAREN_RE = re.compile(r"\(([^)]*)\)")
_EP_FROM_FILE_RE = re.compile(r"episode[-_ ]?(\d+)", re.I)
_IGNORED_RE = re.compile(r"^(---.*---|Actions and dialogue:?)$", re.I)

_DELIVERY_MODIFIERS = {"V.O.", "O.S.", "O.C.", "CONT'D", "CONT’D", "V.O", "O.S"}
_TYPE_ALIASES = {
    "CAMERA": BlockType.CAMERA,
    "ACTION": BlockType.ACTION,
    "SOUND FX": BlockType.SFX,
    "SFX": BlockType.SFX,
    "SOUND": BlockType.SFX,
    "MUSIC": BlockType.SFX,
}


class ParseError(ValueError):
    """Raised when a file cannot be interpreted. Carries file and line for debugging."""

    def __init__(self, path: str, line_no: int, message: str):
        super().__init__(f"{path}:{line_no}: {message}")


def _parse_header(raw: str) -> tuple[BlockType, str | None, str | None, str | None]:
    """``"MATRIARCH (O.S.) (Firm)"`` -> (DIALOGUE, "MATRIARCH", "O.S.", "Firm")."""
    name = _PAREN_RE.sub("", raw).strip()
    groups = [g.strip() for g in _PAREN_RE.findall(raw)]
    block_type = _TYPE_ALIASES.get(name.upper())
    if block_type is not None:
        return block_type, None, None, None

    delivery = [g for g in groups if g.upper() in _DELIVERY_MODIFIERS]
    emotion = [g for g in groups if g.upper() not in _DELIVERY_MODIFIERS]
    return (
        BlockType.DIALOGUE,
        name.upper(),
        ", ".join(delivery) or None,
        ", ".join(emotion) or None,
    )


def _clean_text(text: str, block_type: BlockType) -> str:
    text = " ".join(text.split())
    if block_type is BlockType.DIALOGUE and len(text) >= 2 and text[0] in "\"“" and text[-1] in "\"”":
        text = text[1:-1].strip()
    return text


def parse_episode_text(text: str, source: str = "<memory>") -> Episode:
    """Parse one screenplay file's contents."""
    number: int | None = None
    logline = continuity = ""
    scenes: list[Scene] = []
    pending: dict | None = None  # block under construction
    last_key: str | None = None  # for multi-line header values

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        if "start" not in pending:
            raise ParseError(source, pending["line"], f"block {pending['raw']!r} has no Start/End line")
        if not scenes:
            scenes.append(Scene(number=1))
        scene = scenes[-1]
        btype = pending["type"]
        scene.blocks.append(
            Block(
                id=f"E{number or 0:02d}-S{scene.number:02d}-K{len(scene.blocks) + 1:03d}",
                episode=number or 0,
                scene=scene.number,
                type=btype,
                speaker=pending["speaker"],
                delivery=pending["delivery"],
                emotion=pending["emotion"],
                start=pending["start"],
                end=pending["end"],
                text=_clean_text(" ".join(pending["text"]), btype),
            )
        )
        pending = None

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or _IGNORED_RE.match(line):
            last_key = None if not line else last_key
            continue

        if m := _KEY_RE.match(line):
            key, value = m.group(1).upper(), m.group(2).strip()
            if key == "EPISODE NUMBER":
                digits = re.search(r"\d+", value)
                number = int(digits.group()) if digits else None
            elif key == "EPISODE LOGLINE":
                logline = value
            else:
                continuity = value
            last_key = key
            continue

        if m := _SCENE_RE.match(line):
            flush()
            scenes.append(Scene(number=int(m.group(1))))
            last_key = None
            continue

        if m := _SCENE_FIELD_RE.match(line):
            field, value = m.group(1).upper(), m.group(2).strip()
            if not scenes:
                scenes.append(Scene(number=1))
            scene = scenes[-1]
            try:
                if field == "HEADING":
                    scene.heading = value
                elif field == "START":
                    scene.start = parse_timecode(value)
                else:
                    scene.end = parse_timecode(value)
            except ValueError as exc:
                raise ParseError(source, line_no, str(exc)) from exc
            continue

        if m := _HEADER_RE.match(line):
            flush()
            btype, speaker, delivery, emotion = _parse_header(m.group(1))
            pending = {
                "raw": m.group(1),
                "line": line_no,
                "type": btype,
                "speaker": speaker,
                "delivery": delivery,
                "emotion": emotion,
                "text": [],
            }
            last_key = None
            continue

        if (m := _TIMING_RE.match(line)) and pending is not None and "start" not in pending:
            try:
                start, end = parse_timecode(m.group(1)), parse_timecode(m.group(2))
            except ValueError as exc:
                raise ParseError(source, line_no, str(exc)) from exc
            if end < start:
                start, end = end, start
            pending["start"], pending["end"] = start, end
            continue

        if pending is not None:
            pending["text"].append(line)
        elif last_key == "EPISODE LOGLINE":
            logline += " " + line
        elif last_key == "CONTINUITY NOTES":
            continuity += " " + line
        # any other stray line before the first block is ignored on purpose

    flush()

    if number is None:
        m = _EP_FROM_FILE_RE.search(Path(source).name)
        if not m:
            raise ParseError(source, 1, "no EPISODE NUMBER and none in filename")
        number = int(m.group(1))
        # re-stamp ids/episode now that we know the number
        for scene in scenes:
            for i, block in enumerate(scene.blocks, start=1):
                block.episode = number
                block.id = f"E{number:02d}-S{scene.number:02d}-K{i:03d}"

    return Episode(number=number, source_file=Path(source).name, logline=logline, continuity=continuity, scenes=scenes)


def _validate_episode(ep: Episode) -> list[str]:
    """Soft checks: return warnings instead of failing the whole run."""
    warnings: list[str] = []
    if not ep.blocks:
        warnings.append(f"E{ep.number:02d}: no blocks parsed")
    for scene in ep.scenes:
        if scene.end is None and scene.blocks:
            scene.end = max(b.end for b in scene.blocks)
            warnings.append(f"E{ep.number:02d} scene {scene.number}: missing 'Scene end', inferred {scene.end:.2f}s")
    return warnings


def _iter_text_files(path: Path) -> list[tuple[str, str]]:
    """Return (name, contents) for every .txt in a folder (recursive) or a .zip."""
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            return [
                (name, zf.read(name).decode("utf-8-sig"))
                for name in sorted(zf.namelist())
                if name.lower().endswith(".txt") and not name.startswith("__MACOSX")
            ]
    if path.is_dir():
        return [(str(p), p.read_text(encoding="utf-8-sig")) for p in sorted(path.rglob("*.txt"))]
    if path.is_file() and path.suffix.lower() == ".txt":
        return [(str(path), path.read_text(encoding="utf-8-sig"))]
    raise FileNotFoundError(f"No screenplay .txt files at {path}")


def _show_name(path: Path, files: list[tuple[str, str]]) -> str:
    if path.is_dir():
        subdirs = {Path(n).parent.name for n, _ in files}
        return subdirs.pop() if len(subdirs) == 1 else path.name
    if path.suffix.lower() == ".zip":
        top = {Path(n).parts[0] for n, _ in files if len(Path(n).parts) > 1}
        return top.pop() if len(top) == 1 else path.stem
    return path.stem


def load_show(path: str | Path) -> Show:
    """Load every episode from a folder, a single .txt or a .zip."""
    path = Path(path)
    files = _iter_text_files(path)
    if not files:
        raise FileNotFoundError(f"No screenplay .txt files at {path}")

    episodes: dict[int, Episode] = {}
    warnings: list[str] = []
    for name, contents in files:
        ep = parse_episode_text(contents, source=name)
        if ep.number in episodes:
            warnings.append(f"duplicate episode {ep.number} in {name}; keeping first")
            continue
        warnings.extend(_validate_episode(ep))
        episodes[ep.number] = ep

    for w in warnings:
        log.warning(w)
    ordered = [episodes[n] for n in sorted(episodes)]
    return Show(name=_show_name(path, files), episodes=ordered, warnings=warnings)
