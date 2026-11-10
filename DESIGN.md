# Promo Finder — System Design (Kuku AI Generalist, Problem 1)

> Status: draft for review, before any code is written.
> Input pack: *I Married a Broken Soldier. He was a Duke* — 50 episodes, extracted to `input_pack/`.

---

## 1. What we are building

A re-runnable tool that takes **a folder of timestamped microdrama screenplays** and outputs a **ranked list of promo clips** (20–60 s each), where every promo has:

- one or more **segments**, in order: `episode, start, end`
- the **hook** (what happens in the first 3 s)
- the **cliffhanger** (the unresolved moment it cuts on)
- **one line on why it works**
- a **rank** + reason for posting order
- **quality flags** (where the tool is unsure / likely wrong)

The interviewers will run it on a **new show** and may plug in **stronger models** → the two non-negotiables are *re-runnable on any folder* and *model is a config switch*.

---

## 2. What the input actually looks like (findings from the pack)

| Fact | Implication for design |
|---|---|
| Each file = header (`EPISODE NUMBER`, `EPISODE LOGLINE`, `CONTINUITY NOTES`) + `SCENE` blocks | Loglines + continuity notes are a free, cheap "story bible" source |
| Every block is `**TYPE (Emotion):**` + `Start: (mm:ss.xx) | End: (mm:ss.xx)` + text | Fully deterministic parsing — no LLM needed |
| Block types: CAMERA (~1750), ACTION (~370), dialogue (~1500), SOUND FX (~115) | CAMERA is 50% of tokens but mostly redundant → condense before LLM |
| Episodes are 1:10 – 3:20 long, 1–8 scenes | A 20–60 s promo is ~25–50 % of an episode; most promos fit inside one episode |
| Casing varies (`Scene start:` vs `SCENE START:`) | Parser must be case-insensitive and tolerant |
| Speaker aliases (a character is first credited by description, e.g. "LADY IN …", then by name; `LADY X` vs `X`; `(V.O.)`, `(O.S.)`) | Need alias resolution → matters for the "makes sense on its own" check |
| Dialogue tags carry emotion (`Gloating, Smug`, `Outraged`) and SFX hints like "cliffhanger peak" | Free intensity signals for heuristics |
| Timestamps are episode-local; blocks overlap (camera vs dialogue) | Global key = `(episode, seconds)`; cut points snap to block edges |

---

## 3. Architecture

```
 input folder (*.txt)                                   config.yaml / .env
        │                                              (provider, model, weights)
        ▼                                                        │
┌─────────────────┐   ┌──────────────────┐   ┌─────────────────────────────┐
│ 1. PARSER       │──▶│ 2. BEAT BUILDER  │──▶│ 3. STORY BIBLE (LLM, 1 call)│
│ (code)          │   │ (code)           │   │ characters, aliases, arcs   │
│ txt → Episode/  │   │ blocks → beats   │   └──────────────┬──────────────┘
│ Scene/Block     │   │ with IDs + secs  │                  │
└─────────────────┘   └────────┬─────────┘                  │
                               ▼                            ▼
              ┌───────────────────────────────────────────────────┐
              │ 4. CANDIDATE GENERATOR (LLM, 1 call per episode)  │
              │  in: condensed beats + bible + prev/next logline  │
              │  out: hooks, cliffhangers, 2–4 promo candidates   │
              │       as BEAT-ID RANGES (never raw timestamps)    │
              ├───────────────────────────────────────────────────┤
              │ 4b. CROSS-EPISODE PASS (LLM, 1 call per show)     │
              │  in: index of all hooks/cliffhangers              │
              │  out: multi-segment / multi-episode candidates    │
              └───────────────────────┬───────────────────────────┘
                                      ▼
              ┌───────────────────────────────────────────────────┐
              │ 5. VALIDATOR + SNAPPER (code, hard rules)         │
              │  map beat IDs → timestamps, snap to block edges,  │
              │  20–60 s, hook in first 3 s, ends on cliff beat,  │
              │  ordered, non-overlapping, auto-repair or reject  │
              └───────────────────────┬───────────────────────────┘
                                      ▼
              ┌───────────────────────────────────────────────────┐
              │ 6. JUDGE / SCORER (LLM rubric + code heuristics)  │
              │  separate prompt (ideally separate model)         │
              └───────────────────────┬───────────────────────────┘
                                      ▼
              ┌───────────────────────────────────────────────────┐
              │ 7. RANKER (code): score + dedupe + diversity +    │
              │    funnel position → posting order                │
              └───────────────────────┬───────────────────────────┘
                                      ▼
        outputs/<show>/promos.json · promos.csv · report.md · run_log.json
                                      ▼
              ┌───────────────────────────────────────────────────┐
              │ 8. REVIEW UI (Streamlit): editor approves/rejects,│
              │    nudges boundaries, sees script excerpt + flags │
              └───────────────────────────────────────────────────┘
```

### Design principle: **AI judges taste, code enforces facts**

| Code / rules (deterministic, free, testable) | LLM (judgement) | Human (editor) |
|---|---|---|
| Parsing, timestamps, durations | What is a hook / cliffhanger | Final approve / reject |
| Snapping cuts to line boundaries | Does it stand alone for a stranger | Boundary nudges (±1–2 s) |
| 20–60 s, ordering, overlap, dedupe | Why it works (one line) | Feedback → gold set for eval |
| Heuristic intensity features | Rubric scores | |
| Ranking formula + diversity | | |

The LLM **never emits a timestamp**. It references beat IDs (`E01-B014`) that code maps to real times. This removes the biggest hallucination risk (made-up or off-by-seconds timestamps).

---

## 4. Components in detail

### 4.1 Parser (code)
- Regex, case-insensitive; tolerant of missing `Scene end`.
- Output model (pydantic):
  - `Episode{num, logline, continuity, scenes[], duration_s}`
  - `Scene{id, heading, start_s, end_s, blocks[]}`
  - `Block{id, type: CAMERA|ACTION|DIALOGUE|SFX, speaker, emotion, start_s, end_s, text}`
- Speaker normalisation: strip `(V.O.)`, `(O.S.)`, `LADY ` prefix → canonical name; alias map completed by the Story Bible step.
- Unit-tested on all 50 files (assert: every episode parses, monotonic-ish times, no negative durations).

### 4.2 Beat builder (code)
- A **beat** = one dialogue line or ACTION/SFX, with overlapping CAMERA text attached as visual context (truncated). Typical beat = 1–4 s.
- Condensed line format sent to the LLM (~50–60 % fewer tokens than raw):
  `E01-B023 [00:39.1–00:41.2] RIVAL (gloating): "<line of dialogue>" | cam: close-up, cruel triumph`
- Precomputed features per beat: is_dialogue, emotion words, physical-action keywords (slap, kiss, grab, blood, falls, kneels, reveal…), SFX intensity cues, exclamation / question marks, characters on screen.

### 4.3 Story bible (LLM, 1 call per show)
- Input: all 50 loglines + continuity notes (~6K tokens).
- Output: characters + aliases + relationships + main arcs + known reveals.
- Used for: alias resolution, the "stands alone?" check (does this clip need knowledge the clip does not give?), and the "why it works" line.

### 4.4 Candidate generator (LLM, per episode)
- Input: condensed beats of episode N + bible + loglines of N-1 and N+1.
- Asked for, as strict JSON (schema-validated, 1 retry on failure):
  - `hooks[]`: beat_id, type (`slap|reveal|shock_line|threat|humiliation|romance|danger`), strength 1–5
  - `cliffhangers[]`: beat_id, what is unresolved, strength 1–5
  - `candidates[]`: segments as `[start_beat, end_beat]`, hook beat, cliff beat, why
- Rule given to the model: a hook must be *understandable in 3 s with zero context* (a line or a visible action, not a slow camera move).

### 4.5 Cross-episode pass (LLM, 1 call per show)
- Input: compact index of all hooks + cliffhangers across 50 episodes (~8–10K tokens).
- Produces multi-segment promos, e.g. "hook from E01 (abandoned at altar) → setup from E03 (stranger proposes) → cliff from E04". This is how ReelShort/DramaBox-style "story so far" trailers are built.
- Kept to ≤ 3 segments per promo (more cuts = harder to follow).

### 4.6 Validator + snapper (code)
Hard rules, each with an auto-repair before rejecting:

| Rule | Repair |
|---|---|
| Total duration 20–60 s | extend/trim by whole beats toward nearest valid length |
| Hook beat starts within first 3 s of segment 1 | move start to hook beat (or ≤ 1 beat before it) |
| Promo ends on the cliffhanger beat (+ ≤ 1.5 s tail for reaction shot) | trim end to cliff beat end |
| Cuts never land mid-dialogue | snap to nearest block boundary |
| Segments ordered, non-overlapping | reorder / merge |
| Duplicates (time-overlap IoU > 0.5) | keep higher score |

Every repair is recorded in `flags` so the editor sees what was changed.

### 4.7 Judge / scorer
- **LLM rubric** (separate prompt; if a second free model is available, use it to reduce self-grading bias), 1–5 each:
  hook strength · cliffhanger strength · stands alone · emotional intensity · genre-trope fit (betrayal, revenge, secret identity, humiliation→payback) · clarity of cuts.
- **Heuristic features** (code): hook in first 3 s is dialogue/action (not camera), # named characters needed (fewer = better), # segments (fewer = better), emotion intensity in first/last beats, duration closeness to ~30–45 s sweet spot.
- `final_score = 0.7 · LLM_rubric + 0.3 · heuristics` (weights in config; tuned against the gold set).

### 4.8 Ranker → posting order
Score alone isn't enough. Posting order also considers:
1. **Funnel position**: promos from early episodes (≈ E1–E10) rank up, because a new viewer who taps through lands at E1 and the story they saw is right there. Late-episode promos may confuse or spoil.
2. **Diversity**: no two promos in a row from the same storyline/scene; mix hook types (betrayal, action, romance, mystery).
3. **Title promise**: promos that deliver the title's promise ("broken soldier… was a Duke") rank up.

Output includes a one-line **"why post this first"** for the top 5.

### 4.9 Review UI (Streamlit, optional but strong for the demo)
- Upload/select folder → run → table of ranked promos.
- Click a promo → shows the exact script excerpt for each segment, hook and cliff highlighted, flags, scores.
- Approve / reject / nudge boundaries → saved to `feedback.jsonl` (becomes the gold set + future few-shot examples).

---

## 5. Model-swappability

```
llm/
  base.py        # LLMClient.complete(messages, json_schema) -> dict
  providers.py   # gemini, groq, openrouter, ollama, any OpenAI-compatible base_url
config.yaml      # generator_model, judge_model, temperature, weights
```
- One interface, provider chosen by config/env var (`PROMO_LLM=gemini:gemini-flash`, `PROMO_JUDGE=groq:llama-…`).
- An OpenAI-compatible adapter covers most "APIs we provide" in the interview with zero code change.
- **Disk cache** keyed by hash(prompt + model) → re-runs are free and reproducible; prompt versions are stored in `prompts/*.md`, not in code.
- Default free option: Gemini Flash free tier (large context, JSON mode). Fallback: Groq free tier or local Ollama.

---

## 6. Cost & scale (per show of 50 episodes, rough estimates)

Measured on the input pack: about 2.1K tokens of condensed script per episode (104K for the show) and about 300 tokens per clip sent to the judge.

| Step | Calls (v1 default) | Input tokens per call |
|---|---|---|
| Story bible | 1 | ~6K |
| Candidate generation, packed into ~12K-token batches of at most 6 episodes | 10 (4–6 episodes each) | ~13K |
| Follow-up for any episode a batch skipped | 0–2 | ~3–6K |
| Cross-episode montages | 1 | ~10K |
| Judge: top 40 clips, 20 per call, shuffled | 2 | ~6K |
| **Total** | **~14 (plus retries)** | **~0.2M tokens** |

**Why batching and not one call per episode:** Free Gemini Flash may allow as few as ~20 requests/day (Google no longer publishes the numbers; check AI Studio per project). One call per episode is ~54 calls and would not finish a show in a day.

**Quality guards on batching:**
- Batches stay small (~12K tokens) and keep episodes consecutive, so neighbouring episodes give each other context.
- Each episode's candidates are assigned by beat-ID prefix, so a mislabelled entry can't corrupt another episode.
- Skipped episodes are re-asked once; after that they fall back to heuristics, and the fallback is logged.
- The judge's input is shuffled with a fixed seed to avoid position bias.

`max_episodes_per_call: 1` restores one call per episode for strong paid models. To check that batching doesn't hurt, run E1–E10 both ways and compare with `promo_finder stability`.

- Fits comfortably in free tiers for a single show (rate limits are the constraint, not cost → add simple throttling + retry).
- On a paid flash-class model this is on the order of cents per show; even with a frontier model it stays small vs. an editor's hours. Exact price depends on the model chosen — we'll quote it from the provider's price sheet in the solution doc.
- Runtime: minutes per show, episodes processed in parallel (bounded concurrency).
- "Every show every week": batch CLI over a folder of shows; outputs per show; editors only review the top ~10.

---

## 7. Quality checking (how we know it's good)

1. **Hard-rule pass rate** (automatic): % of candidates valid before/after repair.
2. **Gold set** (manual, ~1 hour): we hand-pick ~10–15 promos in E1–E10 like an editor would. Metrics:
   - precision@10 of tool picks vs gold (overlap ≥ 50 %)
   - boundary error (seconds) on matched promos
3. **Stability**: run twice (or with 2 models) → overlap of the top 10. Low overlap = taste is noisy → flag.
4. **Blind "stranger" test**: give only the clip's script excerpt to a fresh LLM call (no bible) and ask "what is happening, who are these people, what do you want to know next?" If it can't answer → fails "stands alone".
5. **Failure catalogue** (the brief explicitly asks "where does it pick badly?"). Expected failure modes we'll check and report honestly:
   - Visual-only hooks (a slap described only in CAMERA text) get underrated
   - Lines that only shock if you know the backstory get overrated
   - Long emotional monologues score high on intensity but are slow
   - Multi-episode promos with jarring jumps in location/costume
   - Screenplay timestamps ≠ final edited video timing
   - The LLM tends to pick the episode's final beat as cliffhanger every time (position bias)

---

## 8. Assumptions (to list in the solution doc)

1. Screenplay timestamps match the final cut closely enough; editor adjusts ±1–2 s.
2. Timestamps are per-episode; a promo's length is the sum of its segments.
3. "Hook in first 3 s" is measured from the start of segment 1.
4. A promo may start mid-scene and may combine up to 3 segments, across episodes.
5. We work only from text (no video/audio), so visual impact is inferred from ACTION/CAMERA text.
6. English, US audience; ReelShort/DramaBox-style tropes are the target "taste".
7. Spoilers are acceptable in promos (the title itself spoils the Duke reveal), but early-episode promos are preferred for funnel reasons.

---

## 9. Outputs (raw outputs folder for submission)

```
outputs/<show_slug>/
  promos.json        # canonical, full detail incl. scores + flags
  promos.csv         # editor-friendly: rank, episode(s), start, end, hook, cliff, why
  report.md          # top 10 with reasoning + posting order + known failures
  candidates_all.json# everything generated, including rejected (for honesty/debug)
  run_log.json       # models, prompt versions, token counts, timings
```

Example `promos.json` item:
```json
{
  "rank": 1,
  "promo_id": "E01-a",
  "segments": [{"episode": 1, "start": "00:33.70", "end": "01:12.40"}],
  "duration_s": 38.7,
  "hook": {"episode": 1, "t": "00:33.70", "text": "<the shocking line or action, quoted from the script>"},
  "cliffhanger": {"episode": 1, "t": "01:12.10", "text": "<the unresolved line the clip cuts on>"},
  "why_it_works": "Instant betrayal setup, zero context needed, ends before the reaction.",
  "post_first_because": "Episode 1 → viewer lands right where the clip leaves off.",
  "scores": {"hook": 5, "cliff": 4, "standalone": 5, "intensity": 5, "final": 4.6},
  "flags": ["end trimmed by 1 beat to land on cliff"]
}
```

---

## 10. Repo layout & tech

```
KukuFM/
  promo_finder/
    parse.py  beats.py  bible.py  generate.py  validate.py  judge.py  rank.py  export.py
    llm/ base.py providers.py cache.py
    models.py          # pydantic schemas
  prompts/             # versioned prompt files
  app.py               # Streamlit review UI
  cli.py               # python cli.py run --input <folder> --out outputs/
  config.yaml  .env.example  requirements.txt  README.md
  tests/test_parse.py  tests/test_validate.py
  eval/gold.json  eval/evaluate.py
```
Stack: Python 3.11, pydantic, httpx/official SDKs, Streamlit, pytest.

⚠ **Confidentiality**: the input pack must not be published. The GitHub repo/Streamlit app ships **without** `input_pack/` (gitignored); the app takes an uploaded folder/zip. Raw outputs go only into the submission folder.

---

## 11. Build plan (~2–3 h of focused work)

| # | Step | Time |
|---|---|---|
| 1 | Parser + beats + tests on all 50 eps | 25 min |
| 2 | LLM adapter + cache + config | 20 min |
| 3 | Bible + per-episode generator prompts | 30 min |
| 4 | Validator/snapper | 25 min |
| 5 | Judge + ranker + exports | 25 min |
| 6 | Full run → raw outputs; gold set + eval; failure notes | 30 min |
| 7 | Streamlit UI (thin) + README | 20 min |
| 8 | 2-page solution doc + optional 3-min video | after |

---

## 12. Decisions taken (v1)

1. **Default LLM provider**: Gemini Flash through its OpenAI-compatible endpoint. One HTTP client covers Gemini, Groq, OpenRouter, OpenAI, Ollama and any `custom` endpoint.
2. **UI**: Streamlit review app (`app.py`) plus the CLI, CSV and Markdown outputs.
3. **Multi-episode promos**: included as one cross-episode LLM call with up to 3 segments.
4. **Gold set**: you hand-pick it into `eval/gold.json` (template in `eval/`); `promo_finder eval` scores runs against it.

### Changes from the original plan, found while building

- **Rule-based candidates in the pipeline.** They compete with the LLM's own candidates, so the judge has more to choose from, and they are the fallback for a step whose LLM call fails mid-run. There are two explicit modes, `--mode llm` (default) and `--mode heuristic` (rule-based only). The old `auto` mode, which silently switched to rules when the key was missing, was removed: a missing key in LLM mode is now reported as an error.
- **Shortlist before judging.** The judge sees only the top `judge_top_k` (60) distinct candidates by heuristic score. This keeps a full run at about 60–70 calls, which fits free-tier daily limits.
- **`physical` hook type instead of `slap`.** The keyword list was tightened after "burning outrage" and "fire" in camera notes were wrongly tagged as violence.
- **Camera-only shots become `VISUAL` beats**, so purely visual moments can still be picked.
