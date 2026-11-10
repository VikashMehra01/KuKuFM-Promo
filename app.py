"""Streamlit review UI: run the finder, inspect each promo's script, approve/reject.

    streamlit run app.py
"""

from __future__ import annotations

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
from promo_finder.llm import ConfigError, missing_credentials
from promo_finder.models import Beat, BeatKind, Segment
from promo_finder.parse import load_show
from promo_finder.pipeline import run_pipeline
from promo_finder.timecode import format_timecode

load_dotenv()
st.set_page_config(page_title="Promo Finder", layout="wide")
OUT_ROOT = Path("outputs")


@st.cache_data(show_spinner=False)
def _load_beats(input_path: str) -> dict:
    show = load_show(input_path)
    return {ep: [b.model_dump() for b in bl] for ep, bl in build_show_beats(show, build_alias_map(show)).items()}


def _existing_runs() -> list[Path]:
    return sorted(OUT_ROOT.glob("*/promos.json"), key=lambda p: p.stat().st_mtime, reverse=True)


# ------------------------------------------------------------------ sidebar: run
st.sidebar.header("Run")
cfg = load_config("config.yaml")
upload = st.sidebar.file_uploader("Upload screenplays (.zip)", type=["zip"])
input_path = st.sidebar.text_input("…or local folder", value="input_pack")
mode = st.sidebar.selectbox("Mode", ["auto", "llm", "heuristic"], help="auto = LLM if an API key is set")
cfg.llm.generator = st.sidebar.text_input("Generator model", value=cfg.llm.generator)
cfg.llm.judge = st.sidebar.text_input("Judge model", value=cfg.llm.judge)
episodes_text = st.sidebar.text_input("Episodes (blank = all)", placeholder="1-10")
if missing := missing_credentials(cfg.llm.generator):
    st.sidebar.caption(f"⚠️ {missing} not set → auto mode will use heuristics.")

if st.sidebar.button("Find promos", type="primary"):
    if upload is not None:
        tmp = Path(tempfile.mkdtemp(prefix="promo_upload_")) / upload.name
        tmp.write_bytes(upload.getvalue())
        input_path = str(tmp)
    bar = st.progress(0.0, text="Starting…")
    try:
        result = run_pipeline(
            input_path, OUT_ROOT, cfg, mode=mode, episodes=parse_episode_range(episodes_text),
            progress=lambda step, frac: bar.progress(min(frac, 1.0), text=step),
        )
        st.session_state["run_file"] = str(result.paths["promos_json"])
        st.success(f"Done: {len(result.ranked)} promos ranked ({result.run_log['mode']} mode).")
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        st.error(str(exc))

st.sidebar.header("Open a run")
runs = _existing_runs()
if runs:
    labels = [str(p.parent.name) for p in runs]
    default = labels.index(Path(st.session_state["run_file"]).parent.name) if "run_file" in st.session_state else 0
    choice = st.sidebar.selectbox("Saved runs", labels, index=default)
    st.session_state["run_file"] = str(runs[labels.index(choice)])

# ------------------------------------------------------------------ main
st.title("Promo Finder")
if "run_file" not in st.session_state:
    st.info("Run the finder from the sidebar, or open a saved run.")
    st.stop()

run_file = Path(st.session_state["run_file"])
data = json.loads(run_file.read_text(encoding="utf-8"))
run_log = json.loads((run_file.parent / "run_log.json").read_text(encoding="utf-8"))
promos = data["promos"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Show", data["show"][:28])
c2.metric("Mode", run_log["mode"])
c3.metric("Candidates", run_log["counts"]["candidates"])
c4.metric("Ranked", len(promos))
if run_log.get("errors"):
    st.warning(f"{len(run_log['errors'])} LLM step(s) fell back to heuristics — see run_log.json.")

table = pd.DataFrame([
    {
        "#": p["rank"],
        "segments": " + ".join(f"E{s['episode']:02d} {s['start_tc']}–{s['end_tc']}" for s in p["segments"]),
        "len (s)": round(p["duration_s"]),
        "hook": p["hook"]["text"],
        "cliffhanger": p["cliffhanger"]["text"],
        "score": round(p["scores"]["final"] * 100),
        "source": p["source"],
    }
    for p in promos
])
st.dataframe(table, hide_index=True, width="stretch")

# ------------------------------------------------------------------ detail + review
pick = st.selectbox("Inspect promo", [f"#{p['rank']} · {p['promo_id']} · {p['title']}" for p in promos])
promo = promos[int(pick.split(" ")[0][1:]) - 1]

left, right = st.columns([3, 2])
with left:
    st.subheader("Script")
    try:
        beats_by_ep = _load_beats(run_log["input"])
    except FileNotFoundError:
        beats_by_ep = {}
        st.caption("Original input not found; script excerpt unavailable.")
    for i, seg in enumerate(promo["segments"]):
        if i:
            st.markdown("**— CUT —**")
        st.markdown(f"**E{seg['episode']:02d} {seg['start_tc']} → {seg['end_tc']}** ({seg['duration_s']}s)")
        beats = [Beat.model_validate(b) for b in beats_by_ep.get(seg["episode"], [])]
        lines = []
        for b in beats_in(Segment.model_validate(seg), beats):
            marker = "🎣 " if b.id == promo["hook"]["beat_id"] else "⏸️ " if b.id == promo["cliffhanger"]["beat_id"] else ""
            who = f"**{b.speaker}**: " if b.kind is BeatKind.DIALOGUE else f"*{b.kind.value.lower()}*: "
            lines.append(f"`{format_timecode(b.start)}` {marker}{who}{b.text}")
        st.markdown("  \n".join(lines) or "_no beats_")

with right:
    st.subheader("Why")
    st.write(promo["why_it_works"] or "—")
    st.caption(f"Post order reason: {promo['post_reason']}")
    if promo.get("stranger_view"):
        st.caption(f"Stranger test: {promo['stranger_view']}")
    s = promo["scores"]
    st.write({k: s[k] for k in ("hook", "cliffhanger", "standalone", "intensity", "trope_fit") if s[k] is not None}
             | {"heuristic": round(s["heuristic"], 2), "final": round(s["final"], 2)})
    if promo["flags"]:
        st.subheader("Flags")
        for f in promo["flags"]:
            st.caption(f"• {f}")

    st.subheader("Editor review")
    with st.form(f"review-{promo['promo_id']}"):
        verdict = st.radio("Verdict", ["approve", "reject", "adjust"], horizontal=True)
        nudges = []
        for i, seg in enumerate(promo["segments"]):
            a, b = st.columns(2)
            nudges.append({
                "segment": i,
                "start_nudge_s": a.number_input(f"Seg {i + 1} start ±s", -10.0, 10.0, 0.0, 0.1),
                "end_nudge_s": b.number_input(f"Seg {i + 1} end ±s", -10.0, 10.0, 0.0, 0.1),
            })
        note = st.text_input("Note (why?)")
        if st.form_submit_button("Save review"):
            record = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "promo_id": promo["promo_id"], "rank": promo["rank"], "verdict": verdict,
                "segments": promo["segments"], "nudges": nudges, "note": note,
            }
            with (run_file.parent / "feedback.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            st.success("Saved to feedback.jsonl")
