"""Streamlit review UI: run the finder, inspect each promo's script, approve/reject.

    streamlit run app.py
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from promo_finder.beats import build_alias_map, build_show_beats
from promo_finder.cli import parse_episode_range
from promo_finder.config import load_config
from promo_finder.heuristics import beats_in
from promo_finder.llm import ConfigError, LLMAuthError, missing_credentials
from promo_finder.models import Beat, BeatKind, Segment
from promo_finder.parse import load_show
from promo_finder.pipeline import run_pipeline
from promo_finder.timecode import format_timecode

load_dotenv()
st.set_page_config(page_title="Promo Finder", layout="wide")
OUT_ROOT = Path("outputs")

SOURCE_LABELS = {"llm": "AI", "llm-cross": "AI montage", "heuristic": "Rule-based"}
SCORE_LABELS = {
    "hook": "Hook (first 3 s)",
    "cliffhanger": "Cliffhanger",
    "standalone": "Makes sense alone",
    "intensity": "Intensity",
    "trope_fit": "Trope fit",
}

# Less empty space above the title; wrap long text in tables instead of cutting it.
st.markdown(
    """
    <style>
      .block-container { padding-top: 2.2rem; padding-bottom: 2rem; }
      table.promo-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
      table.promo-table th { text-align: left; font-weight: 600; padding: 0.45rem 0.5rem;
                             border-bottom: 2px solid rgba(128,128,128,0.35); }
      table.promo-table td { vertical-align: top; padding: 0.4rem 0.5rem;
                             border-bottom: 1px solid rgba(128,128,128,0.18); }
      table.promo-table td:nth-child(1), table.promo-table td:nth-child(3),
      table.promo-table td:nth-child(6) { text-align: right; }
      /* hook and cliffhanger share the remaining width equally */
      table.promo-table th:nth-child(4), table.promo-table th:nth-child(5) { width: 32%; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Promo Finder")
status = st.container()  # run progress / results appear under the title, not above it


@st.cache_data(show_spinner=False)
def _load_beats(input_path: str, aliases: tuple[tuple[str, str], ...]) -> dict:
    """Beats exactly as the run saw them: same speaker merges (aliases) as the pipeline used."""
    show = load_show(input_path)
    alias_map = build_alias_map(show, dict(aliases))
    return {ep: [b.model_dump() for b in bl] for ep, bl in build_show_beats(show, alias_map).items()}


def _existing_runs() -> list[Path]:
    return sorted(OUT_ROOT.glob("*/promos.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def _run_label(path: Path) -> str:
    try:
        show = json.loads(path.read_text(encoding="utf-8")).get("show", path.parent.name)
    except (OSError, json.JSONDecodeError):
        show = path.parent.name
    when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d %b %H:%M")
    return f"{when} · {show}"


def _segments_text(promo: dict, *, nowrap: bool = False) -> str:
    """'E01 00:46.80–01:10.40'; with nowrap the table never breaks a segment across lines."""
    sp, dash = (" ", "⁠–⁠") if nowrap else (" ", "–")
    return " + ".join(f"E{s['episode']:02d}{sp}{s['start_tc']}{dash}{s['end_tc']}" for s in promo["segments"])


def _nowrap(text: str) -> str:
    return text.replace(" ", " ").replace("-", "‑")


# ------------------------------------------------------------------ sidebar: run
st.sidebar.header("Run")
cfg = load_config("config.yaml")
upload = st.sidebar.file_uploader("Upload screenplays (.zip)", type=["zip"])
input_path = st.sidebar.text_input("…or local folder", value="input_pack", disabled=upload is not None)
if upload is not None:
    st.sidebar.caption(f"Using the uploaded file **{upload.name}** instead of the folder.")

MODE_LABELS = {"LLM (needs API key)": "llm", "Rule-based only (no LLM, no key)": "heuristic"}
mode = MODE_LABELS[st.sidebar.radio(
    "Mode", list(MODE_LABELS),
    help="An explicit choice: LLM mode never switches to rules on its own - a missing key is reported instead.",
)]
if mode == "llm":
    cfg.llm.generator = st.sidebar.text_input("Generator model", value=cfg.llm.generator)
    cfg.llm.judge = st.sidebar.text_input("Judge model", value=cfg.llm.judge)
    fallback_text = st.sidebar.text_input(
        "Fallback model", value=cfg.llm.fallback or "", help="used when the main model is overloaded; blank = none"
    )
    cfg.llm.fallback = fallback_text.strip() or None
top_n_cap = cfg.ranking.judge_top_k if mode == "llm" else 100
cfg.ranking.top_n = int(st.sidebar.number_input(
    "Promos to rank (top N)", min_value=1, max_value=top_n_cap, value=min(cfg.ranking.top_n, top_n_cap),
    step=1,
    help=(f"How many promos the posting order contains. In LLM mode at most {cfg.ranking.judge_top_k} "
          "clips reach the judge (ranking.judge_top_k in config.yaml), minus duplicates, so the list can be shorter."),
))
episodes_text = st.sidebar.text_input("Episodes (blank = all)", placeholder="e.g. 1-10 or 1,3,5-7")
missing_keys = (
    sorted({m for spec in (cfg.llm.generator, cfg.llm.judge) if (m := missing_credentials(spec))})
    if mode == "llm" else []
)
if missing_keys:
    st.sidebar.error(
        f"API key missing: {', '.join(missing_keys)} is not set. Add it to .env (see .env.example) "
        "and restart the app, or choose the rule-based mode."
    )

if st.sidebar.button("Find promos", type="primary", disabled=bool(missing_keys), width="stretch"):
    with status:
        try:
            episodes = parse_episode_range(episodes_text)
            source = input_path
            if upload is not None:
                tmp = Path(tempfile.mkdtemp(prefix="promo_upload_")) / upload.name
                tmp.write_bytes(upload.getvalue())
                source = str(tmp)
            bar = st.progress(0.0, text="Starting…")
            result = run_pipeline(
                source, OUT_ROOT, cfg, mode=mode, episodes=episodes,
                progress=lambda step, frac: bar.progress(min(frac, 1.0), text=step),
            )
            st.session_state["run_file"] = str(result.paths["promos_json"])
            label = "rule-based mode" if mode == "heuristic" else "LLM mode"
            st.success(f"Done: {len(result.ranked)} promos ranked ({label}).")
        except argparse.ArgumentTypeError as exc:
            st.error(f"Episodes: {exc}. Use e.g. 1-10 or 1,3,5-7.")
        except (ConfigError, LLMAuthError, FileNotFoundError, ValueError) as exc:
            st.error(str(exc))

st.sidebar.header("Open a run")
runs = _existing_runs()
if runs:
    keys = [str(p) for p in runs]
    current = st.session_state.get("run_file")
    choice = st.sidebar.selectbox(
        "Saved runs", keys, index=keys.index(current) if current in keys else 0,
        format_func=lambda k: _run_label(Path(k)),
    )
    st.session_state["run_file"] = choice
    st.sidebar.caption(_run_label(Path(choice)))  # full name: the box itself can be too narrow

# ------------------------------------------------------------------ main: run summary
if "run_file" not in st.session_state:
    st.info("Run the finder from the sidebar, or open a saved run.")
    st.stop()

run_file = Path(st.session_state["run_file"])
data = json.loads(run_file.read_text(encoding="utf-8"))
run_log = json.loads((run_file.parent / "run_log.json").read_text(encoding="utf-8"))
promos = data["promos"]
run_mode = run_log.get("mode", "llm")

st.subheader(data["show"])  # full title, wraps instead of being cut
main_model = run_log["models"]["generator"].split("|")[0]
calls = run_log.get("llm_calls", {})
if run_mode == "heuristic":
    st.caption("Mode: **rule-based only** · no LLM calls")
else:
    served = dict(run_log.get("models", {}).get("calls_served_by", {}))
    tripped = served.pop("primary_tripped", None)
    line = (f"Mode: **LLM** · main model `{main_model}` · LLM calls: {calls.get('made', 0)} new, "
            f"{calls.get('served_from_cache', 0)} from cache")
    if served:
        line += " · new calls answered by " + ", ".join(f"`{m}` × {n}" for m, n in served.items())
    downgrades = ((run_log.get("usage") or {}).get("generator") or {}).get("schema_downgrades")
    if downgrades:
        line += f" · {downgrades} schema downgrade(s)"
    st.caption(line)
    if tripped:
        st.info("The main model failed repeatedly in this run (circuit breaker), so the fallback model answered the rest.")

counts = run_log.get("counts", {})
m1, m2, m3, m4 = st.columns(4)
m1.metric("Episodes", counts.get("episodes", "—"))
m2.metric("Candidates", counts.get("candidates", "—"))
m3.metric("Passed checks", counts.get("valid_after_validation", "—"))
m4.metric("Ranked", len(promos))

if run_log.get("errors"):
    with st.expander(f"⚠️ {len(run_log['errors'])} LLM step(s) fell back to rule-based results"):
        for err in run_log["errors"]:
            st.caption(f"• {err}")

# ------------------------------------------------------------------ ranking table
st.subheader("Posting order")
table = pd.DataFrame(
    [
        {
            "#": p["rank"],
            "Segments": _segments_text(p, nowrap=True),
            "Len": f"{p['duration_s']:.0f}s",
            "Hook (first 3 s)": p["hook"]["text"],
            "Cuts on (cliffhanger)": p["cliffhanger"]["text"],
            "Score": round(p["scores"]["final"] * 100),
            "Source": _nowrap(SOURCE_LABELS.get(p["source"], p["source"])),
        }
        for p in promos
    ]
)
# Plain HTML table: wraps long lines, shows every row and column, no empty index-header band
# (st.table reserves one). Cell text is HTML-escaped by pandas.
st.markdown(table.to_html(index=False, escape=True, classes="promo-table", border=0), unsafe_allow_html=True)
st.download_button(
    "Download CSV", (run_file.parent / "promos.csv").read_bytes(), file_name=f"{run_file.parent.name}_promos.csv",
    mime="text/csv",
)

# ------------------------------------------------------------------ detail + review
st.divider()
st.subheader("Inspect a promo")
idx = st.selectbox(
    "Promo", range(len(promos)), label_visibility="collapsed",
    format_func=lambda i: (f"#{promos[i]['rank']} · {_segments_text(promos[i])} · "
                           f"{SOURCE_LABELS.get(promos[i]['source'], promos[i]['source'])} · "
                           f"{promos[i]['hook']['text'][:70]}"),
)
promo = promos[idx]
source_label = SOURCE_LABELS.get(promo["source"], promo["source"])
title = promo["title"] if promo["source"] != "heuristic" else f"{source_label} pick"
st.markdown(f"#### #{promo['rank']} · {title}  \n:gray-background[{source_label}] :gray-background[{promo['promo_id']}] "
            f":gray-background[{promo['duration_s']:.1f}s] :gray-background[hook type: {promo['hook_type']}]")

left, right = st.columns([3, 2], gap="large")
with left:
    st.markdown("**Script**  \n:green-background[**HOOK**] = the thumb-stopping beat · "
                ":orange-background[**CUT**] = where the clip ends (cliffhanger)")
    aliases = tuple(sorted((run_log.get("aliases_applied") or {}).items()))
    try:
        beats_by_ep = _load_beats(run_log["input"], aliases)
    except FileNotFoundError:
        beats_by_ep = {}
        st.caption("Original input not found; script excerpt unavailable.")
    for i, seg in enumerate(promo["segments"]):
        if i:
            st.markdown("**— cut to —**")
        with st.container(border=True):
            st.markdown(f"**E{seg['episode']:02d} {seg['start_tc']} → {seg['end_tc']}** ({seg['duration_s']}s)")
            beats = [Beat.model_validate(b) for b in beats_by_ep.get(seg["episode"], [])]
            lines = []
            for b in beats_in(Segment.model_validate(seg), beats):
                if b.id == promo["hook"]["beat_id"]:
                    marker = ":green-background[**HOOK**] "
                elif b.id == promo["cliffhanger"]["beat_id"]:
                    marker = ":orange-background[**CUT**] "
                else:
                    marker = ""
                who = f"**{b.speaker}**: " if b.kind is BeatKind.DIALOGUE else f"*{b.kind.value.lower()}*: "
                lines.append(f"`{format_timecode(b.start)}` {marker}{who}{b.text}")
            st.markdown("  \n".join(lines) or "_no beats_")

with right:
    st.markdown("**Why it works**")
    st.write(promo["why_it_works"] or "—")
    if promo.get("stranger_view"):
        st.markdown(f"**Stranger test** (judge saw only this clip)  \n{promo['stranger_view']}")
    st.markdown(f"**Why this position**  \n{promo['post_reason']}")

    s = promo["scores"]
    rows = [(label, f"{s[k]:.0f} / 5") for k, label in SCORE_LABELS.items() if s.get(k) is not None]
    rows += [("Rule-based score", f"{s['heuristic'] * 100:.0f} / 100"), ("**Final score**", f"**{s['final'] * 100:.0f} / 100**")]
    st.markdown("**Scores**" + ("" if s.get("hook") is not None else " (no AI judge in rule-based mode)"))
    st.markdown("| Measure | Score |\n|---|---|\n" + "\n".join(f"| {a} | {b} |" for a, b in rows))
    if promo["flags"]:
        st.markdown("**Repairs / flags**")
        for f in promo["flags"]:
            st.caption(f"• {f}")

# full-width review form: the narrow right column squashed it at smaller widths
st.divider()
st.subheader("Editor review")
feedback_path = run_file.parent / "feedback.jsonl"
previous = []
if feedback_path.exists():
    for line_ in feedback_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line_)
        except json.JSONDecodeError:
            continue
        if rec.get("promo_id") == promo["promo_id"]:
            previous.append(rec)
if previous:
    last = previous[-1]
    st.caption(f"Last review of this promo: **{last['verdict']}** ({last['at']})" + (f" - {last['note']}" if last.get("note") else ""))

with st.form(f"review-{promo['promo_id']}"):
    verdict = st.radio("Verdict", ["approve", "reject", "adjust"], horizontal=True)
    nudges = []
    for i, seg in enumerate(promo["segments"]):
        a, b, _ = st.columns([1, 1, 2])
        nudges.append({
            "segment": i,
            "start_nudge_s": a.number_input(f"Segment {i + 1} start (±s)", -10.0, 10.0, 0.0, 0.1),
            "end_nudge_s": b.number_input(f"Segment {i + 1} end (±s)", -10.0, 10.0, 0.0, 0.1),
        })
    note = st.text_input("Note (why?)")
    if st.form_submit_button("Save review", type="primary"):
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "promo_id": promo["promo_id"], "rank": promo["rank"], "verdict": verdict,
            "segments": promo["segments"], "nudges": nudges, "note": note,
        }
        with feedback_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        st.success("Saved to feedback.jsonl")
