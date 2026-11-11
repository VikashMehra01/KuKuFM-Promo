"""Streamlit review UI: run the finder, inspect each promo's script, approve/reject.

    streamlit run app.py
"""

from __future__ import annotations

import argparse
import html
import json
import tempfile
from datetime import datetime
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
from promo_finder.review import (
    CLEAR,
    append_review,
    approved_posting_order,
    clip_key,
    gold_from_reviews,
    load_reviews,
    review_summary,
)
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
      table.promo-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; table-layout: fixed; }
      table.promo-table th { text-align: left; font-weight: 600; padding: 0.45rem 0.5rem;
                             border-bottom: 2px solid rgba(128,128,128,0.35); }
      table.promo-table td { vertical-align: top; padding: 0.4rem 0.5rem;
                             border-bottom: 1px solid rgba(128,128,128,0.18); }
      table.promo-table td:nth-child(1) { text-align: right; }
      /* 5 columns: # | clip | hook | cuts on | info. Short ones are fixed; the two texts share the rest */
      table.promo-table th:nth-child(1) { width: 2.4rem; }
      table.promo-table th:nth-child(2) { width: 8.6rem; }
      table.promo-table th:nth-child(5) { width: 6.8rem; }
      table.promo-table td .sub { color: rgba(128,128,128,0.95); font-size: 0.8rem; }
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
    # nowrap keeps "E01 00:46.80" together; a line may still break after the dash if space is short
    sp, dash = (" ", "⁠–") if nowrap else (" ", "–")
    return " + ".join(f"E{s['episode']:02d}{sp}{s['start_tc']}{dash}{s['end_tc']}" for s in promo["segments"])


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

# ------------------------------------------------------------------ reviews (matched by clip, not by ID)
feedback_path = run_file.parent / "feedback.jsonl"
reviews = load_reviews(feedback_path)
STATUS = {"approve": "✅ approved", "reject": "❌ rejected", "adjust": "✏️ adjusted"}


def _status(p: dict) -> str:
    rev = reviews.get(clip_key(p["segments"]))
    return STATUS[rev["verdict"]] if rev else "—"


summary = review_summary(promos, reviews)

# ------------------------------------------------------------------ ranking table (collapsible)
with st.expander(f"Posting order · {len(promos)} promos", expanded=True):
    t1, t2, _ = st.columns([1, 1, 2])
    show_all = t1.toggle(f"Show all {len(promos)}", value=len(promos) <= 10)
    hide_rejected = t2.toggle("Hide rejected", value=False)
    shown = [p for p in promos if not (hide_rejected and _status(p).startswith("❌"))]
    if not show_all:
        shown = shown[:10]
    if shown:
        # Hand-built HTML table: wraps long lines, shows every row, and stacks the short facts so the
        # hook and cliffhanger keep most of the width even on narrow screens. Every value is escaped.
        rows_html = []
        for p in shown:
            e = html.escape
            clip = f"{e(_segments_text(p, nowrap=True))}<div class='sub'>{p['duration_s']:.0f}s</div>"
            info = (f"<b>{round(p['scores']['final'] * 100)}</b> {e(_status(p).split(' ')[0])}"
                    f"<div class='sub'>{e(SOURCE_LABELS.get(p['source'], p['source']))}</div>")
            rows_html.append(
                f"<tr><td>{p['rank']}</td><td>{clip}</td><td>{e(p['hook']['text'])}</td>"
                f"<td>{e(p['cliffhanger']['text'])}</td><td>{info}</td></tr>"
            )
        st.markdown(
            "<table class='promo-table'><thead><tr><th>#</th><th>Clip</th><th>Hook (first 3 s)</th>"
            "<th>Cuts on (cliffhanger)</th><th>Score · review</th></tr></thead><tbody>"
            + "".join(rows_html) + "</tbody></table>",
            unsafe_allow_html=True,
        )
    else:
        st.caption("No promos to show with these filters.")
    if not show_all and len(promos) > 10:
        st.caption(f"Showing the top 10 of {len(promos)} - switch on *Show all* for the rest.")

# ------------------------------------------------------------------ exports
approved_rows = approved_posting_order(promos, reviews)
st.caption(
    f"Reviewed {summary['reviewed']} of {len(promos)} · ✅ approved / ✏️ adjusted: {summary['approved']} · "
    f"❌ rejected: {summary['rejected']}"
    + (f" · acceptance rate {summary['acceptance_rate']:.0%}" if summary["acceptance_rate"] is not None else "")
)
e1, e2, e3 = st.columns(3)
e1.download_button(
    "⬇ All promos", (run_file.parent / "promos.csv").read_bytes(),
    file_name=f"{run_file.parent.name}_promos.csv", mime="text/csv", width="stretch",
    help="CSV of the full posting order, as ranked by the tool.",
)
e2.download_button(
    f"⬇ Approved ({len(approved_rows)})",
    pd.DataFrame(approved_rows).to_csv(index=False).encode("utf-8-sig") if approved_rows else b"",
    file_name=f"{run_file.parent.name}_approved.csv", mime="text/csv", width="stretch",
    disabled=not approved_rows,
    help="CSV of approved and adjusted promos in rank order, with timing nudges applied - the hand-off list.",
)
if e3.button("Save as eval set", width="stretch", disabled=not approved_rows,
             help="Writes gold_from_reviews.json from your approvals: future runs (other models/prompts) "
                  "can be scored against it with `promo_finder eval`."):
    gold_path = run_file.parent / "gold_from_reviews.json"
    gold_path.write_text(json.dumps(gold_from_reviews(data["show"], promos, reviews), indent=2), encoding="utf-8")
    st.success(
        f"Saved {gold_path}. Score a future run against it with:  \n"
        f"`python -m promo_finder eval --promos <run>/promos.json --gold {gold_path.as_posix()}`"
    )

# ------------------------------------------------------------------ detail + review
st.divider()
st.subheader("Inspect a promo")
if "_next_promo" in st.session_state:  # set by a review button: move on to the next promo
    st.session_state["promo_idx"] = st.session_state.pop("_next_promo")
if st.session_state.get("promo_idx", 0) >= len(promos):
    st.session_state["promo_idx"] = 0
idx = st.selectbox(
    "Promo", range(len(promos)), key="promo_idx", label_visibility="collapsed",
    format_func=lambda i: (f"#{promos[i]['rank']} · {_segments_text(promos[i])} · "
                           f"{SOURCE_LABELS.get(promos[i]['source'], promos[i]['source'])} · "
                           f"{_status(promos[i])} · {promos[i]['hook']['text'][:45]}"),
)
promo = promos[idx]
key = clip_key(promo["segments"])
source_label = SOURCE_LABELS.get(promo["source"], promo["source"])
title = promo["title"] if promo["source"] != "heuristic" else f"{source_label} pick"
st.markdown(f"#### #{promo['rank']} · {title}  \n:gray-background[{source_label}] :gray-background[{promo['promo_id']}] "
            f":gray-background[{promo['duration_s']:.1f}s] :gray-background[hook type: {promo['hook_type']}]")
if flash := st.session_state.pop("_review_flash", None):
    st.success(flash)

left, right = st.columns([5, 4], gap="large")
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


def _save(verdict: str, with_nudges: bool = False) -> None:
    """Button callback: record the review, then move on to the next promo."""
    nudges = [
        {"segment": i,
         "start_nudge_s": float(st.session_state.get(f"ns-{key}-{i}", 0.0)),
         "end_nudge_s": float(st.session_state.get(f"ne-{key}-{i}", 0.0))}
        for i in range(len(promo["segments"]))
    ] if with_nudges else []
    append_review(feedback_path, promo, verdict, nudges, st.session_state.get(f"note-{key}", ""))
    st.session_state["_review_flash"] = f"Saved #{promo['rank']} as {STATUS[verdict]}. Showing the next promo."
    st.session_state["_next_promo"] = (idx + 1) % len(promos)


def _clear() -> None:
    """Button callback: undo a review mark. Stays on this promo so it can be re-marked."""
    append_review(feedback_path, promo, CLEAR)
    st.session_state["_review_flash"] = f"Cleared the review of #{promo['rank']}: it is unreviewed again."


with right:
    # Review first, so approving/rejecting never needs scrolling.
    with st.container(border=True):
        st.markdown("**Editor review**")
        rev = reviews.get(key)
        if rev:
            c1, c2 = st.columns([3, 2], vertical_alignment="center")
            c1.caption(f"Current: **{STATUS[rev['verdict']]}** ({rev['at'][:16].replace('T', ' ')} UTC)"
                       + (f" - {rev['note']}" if rev.get("note") else ""))
            c2.button("↩ Clear mark", width="stretch", on_click=_clear,
                      help="Undo the approve/reject/adjust mark; the clip becomes unreviewed again")
        else:
            st.caption("Current: not reviewed")
        st.text_input("Note (optional)", key=f"note-{key}", placeholder="why?")
        b1, b2 = st.columns(2)
        b1.button("✅ Approve", type="primary", width="stretch", on_click=_save, args=("approve",))
        b2.button("❌ Reject", width="stretch", on_click=_save, args=("reject",))
        with st.expander("✏️ Adjust timing"):
            for i, _seg in enumerate(promo["segments"]):
                label = f"Segment {i + 1}" if len(promo["segments"]) > 1 else "Clip"
                st.number_input(f"{label} start (± seconds)", -10.0, 10.0, 0.0, 0.1, key=f"ns-{key}-{i}")
                st.number_input(f"{label} end (± seconds)", -10.0, 10.0, 0.0, 0.1, key=f"ne-{key}-{i}")
            st.button("Save adjustment", width="stretch", on_click=_save, args=("adjust", True))

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

