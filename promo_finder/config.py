"""Typed configuration, loaded from YAML with environment-variable overrides."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class LLMConfig(BaseModel):
    generator: str = "gemini:gemini-3.5-flash-lite"
    judge: str = "gemini:gemini-3.5-flash-lite"
    fallback: str | None = "gemini:gemini-3.1-flash-lite"  # used when the primary is overloaded
    # None = don't send the parameter (required for Gemini 3.x, which degrades below 1.0)
    temperature: float | None = Field(None, ge=0, le=2)
    judge_temperature: float | None = Field(None, ge=0, le=2)
    requests_per_minute: float = Field(5, gt=0)
    max_retries: int = Field(4, ge=0)
    timeout_s: float = Field(300, gt=0)
    # when a fallback exists, the primary fails over fast instead of waiting out overload
    primary_retries_with_fallback: int = Field(1, ge=0)
    primary_timeout_with_fallback_s: float = Field(120, gt=0)
    primary_trip_after: int = Field(2, ge=0)  # consecutive primary failures before skipping it (0 = never)
    fallback_timeout_s: float = Field(120, gt=0)  # fallbacks are fast Lite models; a stalled call must not cost 5 min
    cache_dir: str = ".cache/llm"


class PromoConfig(BaseModel):
    min_duration_s: float = 20
    max_duration_s: float = 60
    hook_window_s: float = 3
    max_segments: int = Field(3, ge=1)
    shot_snap_s: float = Field(1.0, ge=0)
    candidates_per_episode: int = Field(3, ge=1)
    cross_episode_candidates: int = Field(6, ge=0)
    include_heuristic_candidates: bool = True
    # Batching: episodes are packed into one LLM call until either limit is hit.
    # max_episodes_per_call=1 restores one call per episode (best for strong paid models).
    batch_token_budget: int = Field(12_000, ge=500)
    max_episodes_per_call: int = Field(6, ge=1)
    retry_missing_episodes: bool = True  # one follow-up call for episodes a batch skipped
    clips_min_per_episode: int = Field(2, ge=1)  # schema minItems; with 1 the model returned the bare minimum
    schema_fixed_episode_count: bool = True  # minItems=maxItems=N: decoder-enforced coverage (fits with the lean schema)
    # Camera lines in generator prompts. None = full text (old prompts: 110). dedupe lists a shot once
    # instead of on every line it spans (old prompts: false). See docs/CAMERA_TEXT_EXPERIMENT.md.
    camera_text_chars: int | None = Field(None, ge=10)
    camera_dedupe: bool = True

    @model_validator(mode="after")
    def _check_range(self) -> "PromoConfig":
        if self.min_duration_s >= self.max_duration_s:
            raise ValueError("min_duration_s must be < max_duration_s")
        return self


class RankingConfig(BaseModel):
    llm_weight: float = Field(0.7, ge=0)
    heuristic_weight: float = Field(0.3, ge=0)
    rubric_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "hook": 0.30,
            "cliffhanger": 0.25,
            "standalone": 0.20,
            "intensity": 0.15,
            "trope_fit": 0.10,
        }
    )
    funnel_episodes: int = Field(10, ge=0)
    funnel_bonus: float = Field(0.08, ge=0)
    diversity_penalty: float = Field(0.05, ge=0)
    dedupe_iou: float = Field(0.5, gt=0, le=1)
    judge_batch_size: int = Field(20, ge=1)  # ~300 tokens per clip -> ~6K tokens per call
    judge_top_k: int = Field(40, ge=1)  # only the best-looking K candidates are sent to the judge
    judge_shuffle_seed: int = 7  # shuffle before batching to avoid position/ordering bias
    judge_sees_camera: bool = True  # include camera lines in the judge's clip transcripts (old prompts: false)
    top_n: int = Field(20, ge=1)


class Config(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    promo: PromoConfig = Field(default_factory=PromoConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML (if it exists), then apply env overrides."""
    data: dict = {}
    if path is not None and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(data)

    if gen := os.getenv("PROMO_GENERATOR"):
        cfg.llm.generator = gen
    if judge := os.getenv("PROMO_JUDGE"):
        cfg.llm.judge = judge
    return cfg
