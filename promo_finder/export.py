"""Write run outputs: promos.json, promos.csv, report.md, candidates_all.json, run_log.json."""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from .models import Promo, PromoStatus
from .timecode import format_timecode


def promo_to_dict(p: Promo) -> dict:
    data = p.model_dump(mode="json")
    data["duration_s"] = p.duration
    data["episodes"] = p.episodes
    for seg, raw in zip(p.segments, data["segments"]):
        raw["start_tc"] = format_timecode(seg.start)
        raw["end_tc"] = format_timecode(seg.end)
        raw["duration_s"] = round(seg.duration, 2)
    data["hook"]["t_tc"] = format_timecode(p.hook.t)
    data["cliffhanger"]["t_tc"] = format_timecode(p.cliffhanger.t)
    return data


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _segments_str(p: Promo) -> str:
    return " + ".join(s.label() for s in p.segments)


def write_csv(path: Path, ranked: Sequence[Promo]) -> None:
    fields = [
        "rank", "promo_id", "source", "segments", "duration_s", "hook_type", "hook", "cliffhanger",
        "why_it_works", "post_reason", "final_score", "hook_score", "cliff_score", "standalone_score", "flags",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel reads UTF-8
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for p in ranked:
            writer.writerow({
                "rank": p.rank,
                "promo_id": p.promo_id,
                "source": p.source,
                "segments": _segments_str(p),
                "duration_s": p.duration,
                "hook_type": p.hook_type.value,
                "hook": p.hook.text,
                "cliffhanger": p.cliffhanger.text,
                "why_it_works": p.why_it_works,
                "post_reason": p.post_reason,
                "final_score": round(p.scores.final * 100, 1),
                "hook_score": p.scores.hook,
                "cliff_score": p.scores.cliffhanger,
                "standalone_score": p.scores.standalone,
                "flags": " | ".join(p.flags),
            })


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def write_report(path: Path, show_name: str, ranked: Sequence[Promo], all_promos: Sequence[Promo], run_log: dict) -> None:
    status = Counter(p.status.value for p in all_promos)
    sources = Counter(p.source for p in all_promos)
    reasons = Counter(r.split(" (")[0] for p in all_promos for r in p.reject_reasons)
    flag_counts = Counter(f.split(";")[0].split(" (")[0] for p in ranked for f in p.flags)

    lines = [
        f"# Promo report — {show_name}",
        "",
        f"Mode: **{run_log['mode']}** · generator: `{run_log['models']['generator']}` · "
        f"judge: `{run_log['models']['judge']}` · episodes: {run_log['counts']['episodes']}",
        "",
        f"Candidates: {len(all_promos)} ({', '.join(f'{k}: {v}' for k, v in sources.items())}) → "
        f"valid {status.get('valid', 0)}, rejected {status.get('rejected', 0)}; ranked top {len(ranked)}.",
        "",
        "## Posting order",
        "",
        "| # | Segments | Len | Hook (first 3s) | Cliffhanger | Why it works | Score |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in ranked:
        lines.append(
            f"| {p.rank} | {_segments_str(p)} | {p.duration:.0f}s | {_md_escape(p.hook.text[:90])} | "
            f"{_md_escape(p.cliffhanger.text[:90])} | {_md_escape(p.why_it_works)} | {p.scores.final * 100:.0f} |"
        )

    lines += ["", "## Why post these first", ""]
    for p in ranked[:5]:
        lines.append(f"{p.rank}. **{_md_escape(p.title or p.promo_id)}** ({_segments_str(p)}) — {p.post_reason}.")
        if p.stranger_view:
            lines.append(f"   - Stranger test: _{_md_escape(p.stranger_view)}_")

    lines += ["", "## Quality notes (auto-generated)", ""]
    if reasons:
        lines.append("Rejections: " + "; ".join(f"{k} ×{v}" for k, v in reasons.most_common()) + ".")
    if flag_counts:
        lines.append("")
        lines.append("Repairs/flags on ranked promos: " + "; ".join(f"{k} ×{v}" for k, v in flag_counts.most_common(8)) + ".")
    unclear = [p for p in ranked if p.scores.standalone is not None and p.scores.standalone <= 2]
    if unclear:
        lines.append("")
        lines.append("May not stand alone: " + ", ".join(f"#{p.rank}" for p in unclear) + ".")
    if run_log.get("errors"):
        lines.append("")
        lines.append(f"LLM errors (fell back to heuristics): {len(run_log['errors'])} — see run_log.json.")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def export_run(
    out_dir: Path,
    show_name: str,
    ranked: Sequence[Promo],
    all_promos: Sequence[Promo],
    run_log: dict,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "promos_json": out_dir / "promos.json",
        "promos_csv": out_dir / "promos.csv",
        "report": out_dir / "report.md",
        "candidates": out_dir / "candidates_all.json",
        "run_log": out_dir / "run_log.json",
    }
    _write_json(paths["promos_json"], {"show": show_name, "promos": [promo_to_dict(p) for p in ranked]})
    write_csv(paths["promos_csv"], ranked)
    write_report(paths["report"], show_name, ranked, all_promos, run_log)
    ordered_all = sorted(all_promos, key=lambda p: (p.status is not PromoStatus.VALID, -(p.scores.quality or 0)))
    _write_json(paths["candidates"], [promo_to_dict(p) for p in ordered_all])
    _write_json(paths["run_log"], run_log)
    return paths
