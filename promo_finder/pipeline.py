"""End-to-end orchestration: input folder -> ranked promos on disk.

Every LLM stage has a deterministic fallback, so a run always completes:
a failed call degrades that one step to heuristics and is logged in
``run_log.json`` (never silently).
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .beats import build_alias_map, build_show_beats, camera_edges, filter_aliases
from .bible import build_bible
from .config import Config
from .export import export_run
from .generate import (
    CameraText,
    EpisodeAnalysis,
    build_moment_index,
    generate_batch_candidates,
    generate_cross_candidates,
    pack_batches,
)
from .heuristics import generate_heuristic_candidates
from .judge import apply_heuristics, combine_scores, judge_with_llm
from .llm import ConfigError, LLMClient, LLMError, build_client, missing_credentials, served_by
from .models import Beat, Episode, Promo, PromoStatus, Show, StoryBible
from .parse import load_show
from .prompts import load_prompt
from .rank import dedupe, rank_promos, stratified_shortlist
from .validate import ValidationContext, validate_promo

log = logging.getLogger(__name__)

Progress = Callable[[str, float], None]

@dataclass
class RunResult:
    show: Show
    bible: StoryBible
    beats_by_ep: dict[int, list[Beat]]
    ranked: list[Promo]
    all_promos: list[Promo]
    run_log: dict
    out_dir: Path
    paths: dict[str, Path] = field(default_factory=dict)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "show"


MODES = ("llm", "heuristic")  # explicit choice only - never switched automatically


def require_credentials(cfg: Config) -> None:
    """Fail fast, before any work, if a required API key is missing.

    LLM mode never silently degrades to a rule-only run: the rule-only mode
    exists, but only when the user asks for it. (A *rejected* key is caught
    on the first call and also stops the run.)
    """
    for role, spec in (("generator", cfg.llm.generator), ("judge", cfg.llm.judge)):
        if missing := missing_credentials(spec):
            raise ConfigError(
                f"No API key for the {role} model {spec!r}: environment variable {missing} is not set. "
                "Add it to .env (see .env.example) and run again, or choose the rule-based mode explicitly."
            )


def estimate_llm_calls(n_batches: int, n_episodes: int, cfg: Config) -> int:
    """Upper-bound estimate before retries: bible + batches + cross + judge batches."""
    cross = 1 if cfg.promo.cross_episode_candidates > 0 and n_episodes > 1 else 0
    judge = math.ceil(cfg.ranking.judge_top_k / cfg.ranking.judge_batch_size)
    return 1 + n_batches + cross + judge


class _Timer:
    def __init__(self) -> None:
        self.timings: dict[str, float] = {}
        self._t = time.perf_counter()

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        self.timings[name] = round(now - self._t, 2)
        self._t = now


def run_pipeline(
    input_path: str | Path,
    out_root: str | Path,
    cfg: Config,
    *,
    mode: str = "llm",
    episodes: set[int] | None = None,
    generator: LLMClient | None = None,
    judge: LLMClient | None = None,
    progress: Progress | None = None,
) -> RunResult:
    """Run the full pipeline.

    ``mode="llm"`` (default) needs the API key and stops with a clear error if
    it is missing. ``mode="heuristic"`` is the explicit rule-based-only mode
    (no LLM, no key, no quota). ``generator``/``judge`` may be injected (tests).
    """
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {MODES}, got {mode!r}")
    report = progress or (lambda step, frac: None)
    timer = _Timer()
    errors: list[str] = []

    # ---- models --------------------------------------------------------------
    if generator is not None or judge is not None:
        if generator is None:
            raise ValueError("an injected judge requires an injected generator")
        if mode != "llm":
            raise ValueError("injected models require mode='llm'")
        judge = judge or generator
    elif mode == "llm":
        require_credentials(cfg)
        fb = cfg.llm.fallback if cfg.llm.fallback and not missing_credentials(cfg.llm.fallback) else None
        generator = build_client(cfg.llm.generator, cfg.llm, fallback=fb)
        judge = generator if cfg.llm.judge == cfg.llm.generator else build_client(cfg.llm.judge, cfg.llm, fallback=fb)
    use_llm = mode == "llm"

    # ---- 1. parse ------------------------------------------------------------
    report("Parsing screenplays", 0.02)
    show = load_show(input_path)
    if episodes:
        show.episodes = [ep for ep in show.episodes if ep.number in episodes]
        if not show.episodes:
            raise ValueError(f"None of the requested episodes {sorted(episodes)} exist")
    timer.lap("parse")

    # ---- 2. story bible + beats ---------------------------------------------
    report("Building story bible", 0.05)
    bible, bible_llm = build_bible(show, generator if use_llm else None, temperature=cfg.llm.temperature)
    if use_llm and not bible_llm:
        errors.append("story bible: LLM failed, used heuristic bible")
    alias_rejections: list[str] = []
    llm_aliases = None
    if bible_llm:
        llm_aliases, alias_rejections = filter_aliases(show, bible.alias_map())
        for r in alias_rejections:
            log.warning("Rejected story-bible alias %s", r)
        # keep prompts consistent with the script: drop rejected aliases from the bible text too
        for char in bible.characters:
            char.aliases = [a for a in char.aliases if llm_aliases.get(a.upper()) == char.name.upper()]
    aliases = build_alias_map(show, llm_aliases)
    beats_by_ep = build_show_beats(show, aliases)
    beat_index = {b.id: b for beats in beats_by_ep.values() for b in beats}
    ctx = ValidationContext(
        beat_index=beat_index,
        beats_by_ep=beats_by_ep,
        shot_edges={ep.number: camera_edges(ep) for ep in show.episodes},
    )
    timer.lap("bible_and_beats")

    # ---- 3. candidates -------------------------------------------------------
    pc = cfg.promo
    camera = CameraText(max_chars=pc.camera_text_chars, dedupe=pc.camera_dedupe)
    eps = show.episodes
    n_eps = len(eps)
    candidates: list[Promo] = []
    analyses: dict[int, EpisodeAnalysis] = {}
    llm_covered: set[int] = set()
    batches = (
        pack_batches(eps, beats_by_ep, token_budget=pc.batch_token_budget, max_per_call=pc.max_episodes_per_call,
                     camera=camera)
        if use_llm else []
    )
    planned = estimate_llm_calls(len(batches), n_eps, cfg) if use_llm else 0
    if use_llm:
        log.info("Planned LLM calls: ~%d (%d generation batches for %d episodes)", planned, len(batches), n_eps)
    else:
        log.info("Rule-based mode (chosen explicitly): no LLM calls")
    position = {ep.number: i for i, ep in enumerate(eps)}

    def run_batch(batch: list[Episode]) -> None:
        first, last = position[batch[0].number], position[batch[-1].number]
        result = generate_batch_candidates(
            generator, batch, beats_by_ep, bible,
            eps[first - 1].logline if first > 0 else "",
            eps[last + 1].logline if last + 1 < n_eps else "",
            n_candidates=pc.candidates_per_episode, min_s=pc.min_duration_s, max_s=pc.max_duration_s,
            temperature=cfg.llm.temperature, beat_index=beat_index,
            clips_min=pc.clips_min_per_episode, fixed_count=pc.schema_fixed_episode_count,
            camera=camera,
        )
        candidates.extend(result.promos)
        analyses.update(result.analyses)
        llm_covered.update(result.covered)

    def labels(batch: list[Episode]) -> str:
        return f"E{batch[0].number:02d}" + (f"-E{batch[-1].number:02d}" if len(batch) > 1 else "")

    for bi, batch in enumerate(batches):
        report(f"Finding moments in {labels(batch)} (call {bi + 1}/{len(batches)})", 0.08 + 0.55 * bi / len(batches))
        try:
            run_batch(batch)
        except LLMError as exc:
            log.warning("%s: LLM generation failed (%s)", labels(batch), exc)
            errors.append(f"{labels(batch)} generation: {exc}")

    # Quality guard: an episode the batch skipped gets one smaller follow-up call.
    missing = [ep for ep in eps if ep.number not in llm_covered] if use_llm else []
    if missing and pc.retry_missing_episodes:
        retry = pack_batches(missing, beats_by_ep, token_budget=pc.batch_token_budget, max_per_call=pc.max_episodes_per_call,
                     camera=camera)
        log.info("Re-asking for %d episode(s) without usable candidates in %d call(s)", len(missing), len(retry))
        for batch in retry:
            report(f"Re-asking for {labels(batch)}", 0.65)
            try:
                run_batch(batch)
            except LLMError as exc:
                errors.append(f"{labels(batch)} retry: {exc}")
    for ep in eps:
        if use_llm and ep.number not in llm_covered:
            errors.append(f"E{ep.number:02d}: no usable LLM candidates; heuristic candidates only")
        # rule-based candidates: the whole pool in rule mode; competitors (or a failed episode's
        # fallback) in LLM mode
        if not use_llm or ep.number not in llm_covered or pc.include_heuristic_candidates:
            candidates.extend(
                generate_heuristic_candidates(
                    beats_by_ep[ep.number], pc.candidates_per_episode, pc.min_duration_s, pc.max_duration_s
                )
            )
    timer.lap("episode_candidates")

    if use_llm and pc.cross_episode_candidates > 0 and n_eps > 1:
        report("Building cross-episode montages", 0.7)
        index = build_moment_index(show.episodes, analyses, beats_by_ep, beat_index)
        try:
            candidates.extend(
                generate_cross_candidates(
                    generator, bible, index,
                    n_candidates=pc.cross_episode_candidates, min_s=pc.min_duration_s, max_s=pc.max_duration_s,
                    temperature=cfg.llm.temperature, beat_index=beat_index,
                )
            )
        except LLMError as exc:
            log.warning("Cross-episode generation failed: %s", exc)
            errors.append(f"cross-episode generation: {exc}")
    timer.lap("cross_candidates")

    # ---- 4. validate ---------------------------------------------------------
    report("Validating cuts", 0.75)
    for p in candidates:
        validate_promo(p, ctx, pc)
    valid = [p for p in candidates if p.status is PromoStatus.VALID]
    timer.lap("validate")

    # ---- 5. score ------------------------------------------------------------
    report("Scoring", 0.8)
    rc = cfg.ranking
    apply_heuristics(valid, beats_by_ep, pc.hook_window_s)
    for p in valid:  # pre-judge quality = heuristic (+ small prior for LLM-picked clips)
        p.scores.quality = p.scores.heuristic + (0.1 if p.source.startswith("llm") else 0.0)
    if use_llm:
        # quota control: only the best-looking distinct clips go to the (paid/limited) judge
        shortlist = stratified_shortlist(dedupe(valid, rc.dedupe_iou), rc.judge_top_k)
        shortlisted = {id(p) for p in shortlist}
        for p in valid:
            if p.status is PromoStatus.VALID and id(p) not in shortlisted:
                p.reject("below shortlist cut before judging")
        report(f"Judging {len(shortlist)} clips", 0.85)
        judge_with_llm(
            judge, shortlist, beats_by_ep,
            batch_size=rc.judge_batch_size, temperature=cfg.llm.judge_temperature, seed=rc.judge_shuffle_seed,
            with_camera=rc.judge_sees_camera,
        )
    # rule mode: no judge, so the ranking uses the rule-based score alone (combine_scores)
    combine_scores([p for p in valid if p.status is PromoStatus.VALID], rc)
    timer.lap("score")

    # ---- 6. rank + export ----------------------------------------------------
    report("Ranking", 0.95)
    ranked = rank_promos(candidates, rc)
    timer.lap("rank")

    gen_usage = getattr(generator, "usage", None)
    judge_usage = getattr(judge, "usage", None) if judge is not generator else None
    run_log = {
        "tool_version": __version__,
        "show": show.name,
        "input": str(input_path),
        "mode": mode,
        "models": {
            "generator": generator.name if generator else "none (rule-based mode)",
            "judge": judge.name if judge else "none (rule-based mode)",
            "calls_served_by": served_by(generator) if judge is generator
            else {**served_by(generator), **{f"judge:{k}": v for k, v in served_by(judge).items()}},
        },
        "prompt_versions": {n: load_prompt(n).version for n in ("bible", "episode_candidates", "cross_episode", "judge")},
        "config": cfg.model_dump(),
        "llm_calls": {
            "planned": planned,
            "generation_batches": [labels(b) for b in batches],
            "episodes_retried": [f"E{ep.number:02d}" for ep in missing],
            "made": (gen_usage.calls if gen_usage else 0) + (judge_usage.calls if judge_usage else 0),
            "served_from_cache": (gen_usage.cached_calls if gen_usage else 0)
            + (judge_usage.cached_calls if judge_usage else 0),
        },
        "counts": {
            "episodes": n_eps,
            "beats": len(beat_index),
            "candidates": len(candidates),
            "valid_after_validation": len(valid),
            "judged": sum(1 for p in candidates if p.scores.hook is not None),
            "ranked": len(ranked),
        },
        "usage": {
            "generator": vars(gen_usage) if gen_usage else None,
            "judge": vars(judge_usage) if judge_usage else None,
        },
        "parse_warnings": show.warnings,
        "errors": errors,
        "timings_s": timer.timings,
        "bible": bible.model_dump(),
        "aliases_rejected": alias_rejections,
        "aliases_applied": {k: v for k, v in aliases.items() if k != v},
    }

    out_dir = Path(out_root) / slugify(show.name)
    paths = export_run(out_dir, show.name, ranked, candidates, run_log)
    report("Done", 1.0)
    return RunResult(show, bible, beats_by_ep, ranked, candidates, run_log, out_dir, paths)
