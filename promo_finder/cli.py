"""Command-line interface.

    python -m promo_finder run   --input input_pack --out outputs [--mode llm|heuristic] [--episodes 1-10]
    python -m promo_finder check --input input_pack
    python -m promo_finder eval  --promos outputs/<show>/promos.json --gold eval/gold.json
    python -m promo_finder stability --a run1/promos.json --b run2/promos.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from dotenv import load_dotenv

from .config import load_config
from .llm import ConfigError, LLMAuthError


def parse_episode_range(text: str | None) -> set[int] | None:
    """``"1-5,8,10-12"`` -> {1,2,3,4,5,8,10,11,12}."""
    if not text:
        return None
    result: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            if lo > hi:
                raise argparse.ArgumentTypeError(f"bad range {part!r}")
            result.update(range(lo, hi + 1))
        else:
            result.add(int(part))
    return result


def _cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import run_pipeline

    cfg = load_config(args.config)
    if args.generator:
        cfg.llm.generator = args.generator
    if args.judge:
        cfg.llm.judge = args.judge
    elif args.generator:
        cfg.llm.judge = args.generator
    if args.fallback:
        cfg.llm.fallback = None if args.fallback.lower() == "none" else args.fallback
    if args.episodes_per_call:
        cfg.promo.max_episodes_per_call = args.episodes_per_call

    def progress(step: str, frac: float) -> None:
        logging.getLogger("promo_finder").info("[%3.0f%%] %s", frac * 100, step)

    result = run_pipeline(
        args.input, args.out, cfg, mode=args.mode, episodes=parse_episode_range(args.episodes), progress=progress
    )
    calls = result.run_log["llm_calls"]
    print(f"\nShow: {result.show.name}  |  mode: {result.run_log['mode']}  |  model: {result.run_log['models']['generator']}  |  "
          f"candidates: {len(result.all_promos)}  |  ranked: {len(result.ranked)}  |  "
          f"LLM calls: {calls['made']} made, {calls['served_from_cache']} cached (planned ~{calls['planned']})")
    for p in result.ranked[:10]:
        segs = " + ".join(s.label() for s in p.segments)
        print(f"  #{p.rank:<2} {p.scores.final * 100:5.1f}  {segs:<40} {p.duration:4.0f}s  {p.hook.text[:60]}")
    print(f"\nOutputs written to {result.out_dir}")
    if result.run_log["errors"]:
        print(f"WARNING: {len(result.run_log['errors'])} LLM step(s) fell back to heuristics; see run_log.json")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from .beats import build_show_beats
    from .parse import load_show

    show = load_show(args.input)
    beats = build_show_beats(show)
    print(f"Show: {show.name}\nEpisodes: {len(show.episodes)}\nWarnings: {len(show.warnings)}")
    for w in show.warnings:
        print(f"  - {w}")
    for ep in show.episodes:
        print(f"  E{ep.number:02d}  {ep.duration:6.1f}s  scenes={len(ep.scenes)}  blocks={len(ep.blocks)}  beats={len(beats[ep.number])}")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from .evaluate import evaluate

    print(json.dumps(evaluate(args.promos, args.gold, top_k=args.top_k), indent=2))
    return 0


def _cmd_stability(args: argparse.Namespace) -> int:
    from .evaluate import stability

    print(json.dumps(stability(args.a, args.b, top_k=args.top_k), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="promo_finder", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="find and rank promos")
    run.add_argument("--input", required=True, help="folder of .txt screenplays, a .zip, or one .txt")
    run.add_argument("--out", default="outputs", help="output root (a sub-folder per show is created)")
    run.add_argument("--config", default="config.yaml")
    run.add_argument("--episodes", help="subset, e.g. '1-10' or '1,3,5-7'")
    run.add_argument(
        "--mode", choices=["llm", "heuristic"], default="llm",
        help="llm (default; needs the API key, errors if missing) or heuristic (rule-based only, no LLM/key)",
    )
    run.add_argument("--generator", help="override model spec, e.g. groq:llama-3.3-70b-versatile")
    run.add_argument("--judge", help="override judge model spec (defaults to --generator if given)")
    run.add_argument("--fallback", help="model spec used when the primary is overloaded/out of quota ('none' disables)")
    run.add_argument("--episodes-per-call", type=int, help="max episodes per generation call (1 = no batching)")
    run.set_defaults(func=_cmd_run)

    check = sub.add_parser("check", help="parse the input and print per-episode stats")
    check.add_argument("--input", required=True)
    check.set_defaults(func=_cmd_check)

    ev = sub.add_parser("eval", help="compare a run against a hand-labelled gold set")
    ev.add_argument("--promos", required=True)
    ev.add_argument("--gold", required=True)
    ev.add_argument("--top-k", type=int, default=10)
    ev.set_defaults(func=_cmd_eval)

    st = sub.add_parser("stability", help="top-k agreement between two runs")
    st.add_argument("--a", required=True)
    st.add_argument("--b", required=True)
    st.add_argument("--top-k", type=int, default=10)
    st.set_defaults(func=_cmd_stability)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return args.func(args)
    except (ConfigError, LLMAuthError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
