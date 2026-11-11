# Promo Finder

Reads a folder of timestamped microdrama screenplays and returns a **ranked list of 20–60 s promo clips**. Each clip has its segment timestamps, the hook (first 3 s), the cliffhanger it cuts on, one line on why it works, and a reason for its posting order.

Built for Kuku's AI Generalist assignment, Problem 1. The full design is in [DESIGN.md](DESIGN.md). How candidates are found, scored and ranked (with worked numbers) is in [docs/HOW_RANKING_WORKS.md](docs/HOW_RANKING_WORKS.md), and what happened on real runs is in [docs/RUN_REPORT.md](docs/RUN_REPORT.md).

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add GEMINI_API_KEY (free: https://aistudio.google.com/apikey)

# Find promos with the LLM (needs the API key; stops with a clear error if it is missing)
python -m promo_finder run --input input_pack --out outputs
# Rule-based only (no LLM, no key) - chosen explicitly, never as a silent fallback
python -m promo_finder run --input input_pack --mode heuristic --out runs/rules

# Review UI
streamlit run app.py
```

Input can be a folder (searched recursively), a `.zip` or a single `.txt`.

**Running from the UI:**
1. Start it with `streamlit run app.py` and open http://localhost:8501.
2. In the sidebar, pick a folder or upload a `.zip` and choose the **Mode** (LLM, or Rule-based only). In LLM mode also choose the models. Then set **Promos to rank (top N)** and an optional episode range. In LLM mode a missing API key is reported in the sidebar and the run button is disabled.
3. Click **Find promos**. Keep the tab open while it runs.

Each run replaces `outputs/<show>/`. Use the CLI with `--out` to keep runs side by side.

## Commands

| Command | What it does |
|---|---|
| `run --input X [--mode llm\|heuristic] [--episodes 1-10] [--generator spec] [--judge spec] [--fallback spec]` | full pipeline → `outputs/<show>/` |
| `check --input X` | parse only; per-episode stats and warnings |
| `eval --promos outputs/<show>/promos.json --gold eval/gold.json` | precision@k, gold recall and boundary error against hand-picked promos |
| `stability --a run1/promos.json --b run2/promos.json` | how far two runs agree on the top k |

## Swapping models

Set models as `provider:model` in `config.yaml`, with an env var, or on the command line. No code changes are needed.

```bash
python -m promo_finder run --input input_pack --generator groq:llama-3.3-70b-versatile
PROMO_GENERATOR=openrouter:<model> PROMO_JUDGE=gemini:gemini-3.5-flash-lite python -m promo_finder run ...
# Prefer a stronger main model but stay safe when it is overloaded (circuit breaker -> fallback):
python -m promo_finder run --input input_pack --generator gemini:gemini-3.8-flash --fallback gemini:gemini-3.5-flash-lite
# Any OpenAI-compatible endpoint (e.g. one provided in an interview):
LLM_BASE_URL=https://host/v1 LLM_API_KEY=... python -m promo_finder run --input X --generator custom:<model>
```

Providers: `gemini`, `groq`, `openrouter`, `openai`, `ollama` (local, no key), `custom`. Model names change often, so check your provider's current list. Responses are cached in `.cache/llm/`, so a re-run with the same prompts costs nothing.

## How it works

```
parse (code) → beats (code) → story bible (1 LLM call) → candidates per episode (1 LLM call each)
→ cross-episode montages (1 call) → validate + snap cuts (code) → judge (LLM, batched) + heuristics
→ rank: dedupe, early-episode bonus, variety → promos.json / .csv / report.md
```

- **The LLM never writes timestamps.** It refers to beat IDs (`E03-B017`), and code maps those to exact times. This removes a whole class of made-up timings.
- **Code enforces the rules in the brief.** Clips must be 20–60 s, have the hook within 3 s, end on the cliffhanger, never cut mid-line, and cut on camera-shot edges. Each automatic repair is recorded in `flags`.
- **Camera lines are part of the script the LLM reads.** Rules attach each `CAMERA` shot to the lines it overlaps (or make it a `VISUAL` beat), and both the generator and the judge see the full shot descriptions, each shot listed once. What this changed, measured against judge noise, is in [docs/CAMERA_TEXT_EXPERIMENT.md](docs/CAMERA_TEXT_EXPERIMENT.md).
- **The judge only sees the clip.** It gets no story context, so its `standalone` score and `stranger_view` act as the "makes sense to a new viewer" test.
- **LLM mode needs the API key and never silently switches to rules.** A missing key stops the run before any work, and a rejected key stops it on the first call. The rule-based mode only runs when chosen explicitly.
- **A failed call mid-run degrades only that step.** For example, an episode batch that fails after retries and the fallback model gets rule-based candidates. The failure is logged in `run_log.json` and shown in the UI, never hidden.

## Key concepts

### Caching: how a re-run avoids LLM calls

Every LLM call goes through `CachedLLM` (`promo_finder/llm/cache.py`):

1. Before calling the API, it builds a **cache key**: a SHA-256 hash of the model name, system prompt, user prompt, temperature and JSON schema.
2. If `.cache/llm/<key>.json` exists, the saved answer is returned and **no API call is made**. The run log counts it under `served_from_cache`.
3. If it doesn't exist, the API is called and the answer is saved under that key. The file is written to a temporary name first and then renamed, so a crash never leaves a half-written entry.

What this means in practice:

- **The same input gives the same request, so it's free.** Re-running a show with unchanged settings makes 0 calls. A run that stopped half-way resumes from the last finished call, because finished calls are cached.
- **Anything that changes the request is a cache miss:** a different model (including the fallback model, which is part of the name), an edited prompt, a different schema, different episodes, or a changed story bible.
- **The API key is *not* part of the key,** so switching to a new key still reuses cached answers.
- **Failed calls are never cached,** because nothing is saved unless the API answered. A corrupt cache file is ignored and fetched again.
- **The raw answer is cached before it's parsed.** So if the parser is fixed later, the next run reuses the old answer with the new parser for free. To force fresh answers, delete `.cache/llm/` (or run with a different model).
- The cache contains script text, so it's git-ignored. It never expires on its own.

### Modes: `llm` and `heuristic`, both chosen explicitly

| Mode | What runs | Key needed |
|---|---|---|
| `llm` (default) | The LLM builds the story bible, proposes clips and montages, and judges them. Rule-based candidates are added to the same pool and compete in the ranking. | **Yes.** |
| `heuristic` (UI: *Rule-based only*) | No LLM at all. Clips come from keyword and score rules, the same validator checks them, and the ranking uses the rule-based score alone. Runs in about 1 second with 0 API calls. | No |

CLI: `--mode llm` (default) or `--mode heuristic`. UI: the **Mode** radio button.

**There is no automatic switching between them.** An earlier `auto` mode quietly ran rules when the key was missing, which produced results that looked real but weren't LLM-quality. It was removed. Now:
- **Missing key in LLM mode:** the CLI exits (code 2) with `error: No API key for the generator model '…': environment variable GEMINI_API_KEY is not set. Add it to .env … or choose the rule-based mode explicitly.` Nothing is written. The UI shows the same message and disables *Find promos*.
- **Rejected key:** the run stops on the first call with an authentication error.

**Why keep the rule-based mode:**
- It's a **baseline** for showing what the LLM adds ("how do you know it's good?").
- It's a free offline demo.
- It's a reproducible reference that doesn't depend on model availability.

**What stays rule-based inside an LLM run:** the validator, the rule-based scores, the rule-based candidates that compete in the ranking, and the fallback for a single step whose LLM call fails mid-run (logged, and shown in the UI).

### `rule-based pick` vs. a title: where each promo comes from

Each promo has a `source` (the table's **source** column):

| Source | ID | Title |
|---|---|---|
| `llm` | `L-E05-1` | Written by the model, e.g. "Forced Out of the Church" |
| `llm-cross` | `X-03` | Written by the model, for a 2–3 segment montage |
| `heuristic` | `H-E05-1` | **Generated by code: `E05 rule-based pick 1`** |

Rules can find a strong hook and an unresolved ending, but they can't write a creative title, so code labels those clips with their episode and pick number. The label marks *where the clip came from*, not how good it is. Every clip, from either source, goes through the same validator and the same blind LLM judge, and the two compete on equal terms in the ranking. In real runs, judged rule-based and LLM clips scored about the same (3.47 vs 3.43). The judge shortlist alternates sources so neither can crowd out the other.

### Editor review: what approving or rejecting does

In the UI, the **Editor review** box sits at the top right of *Inspect a promo*:
- **✅ Approve** and **❌ Reject** save with one click and jump to the next promo. An optional note is saved with the verdict.
- **✏️ Adjust timing** lets you nudge each segment's start and end by up to ±10 s, then save the verdict "adjust".

Each review is appended to `outputs/<show>/feedback.jsonl` (`promo_finder/review.py`). The latest verdict for a clip wins, and reviews are used like this:

| Where | What happens |
|---|---|
| Posting order table | A **review** icon per row (✅ / ✏️ / ❌), plus a **Hide rejected** toggle. The summary line shows reviewed, approved and rejected counts and the acceptance rate. |
| **⬇ Approved** | A CSV of approved and adjusted promos in rank order, **with the timing nudges applied**. This is the hand-off list for the editor. |
| **Save as eval set** | Writes `gold_from_reviews.json` (the `eval/gold.json` format) from your approvals, so a *future* run (another model, prompt or setting) can be scored with `promo_finder eval --promos <run>/promos.json --gold <that file>`. Scoring the same run against its own approvals only gives the acceptance rate, not recall. |

**Reviews are matched to the clip itself** (episode plus start and end of every segment), **not to the promo ID.** IDs like `L-E05-1` are reassigned on every run, so after a re-run an ID-based review could attach to a different clip. With the clip match, a review carries over when the same clip is found again and is simply ignored when it isn't. Reviews don't change the tool's own ranking or `promos.json`; using them to tune the ranking weights is future work and needs many more reviews.

The **Posting order** list is collapsible and shows the top 10 by default; *Show all* lists the rest.

### How many promos are ranked (top N)

- Default **20** (`ranking.top_n` in `config.yaml`). In the UI, change it with **Promos to rank (top N)** in the sidebar before clicking *Find promos*.
- In LLM mode, at most `ranking.judge_top_k` clips (default 40) reach the judge, minus duplicates. So top N is capped there, and the list can come out a little shorter than N. Rule-based mode has no judge shortlist, so the UI allows up to 100.
- Changing top N doesn't change any LLM request: a re-run with the same input is served from the cache and only re-ranks.

## Outputs (`outputs/<show>/`)

| File | Contents |
|---|---|
| `promos.json` | ranked promos with all details (segments, hook, cliffhanger, scores, flags, post reason) |
| `promos.csv` | an editor-friendly table that opens in Excel |
| `report.md` | posting order, why the top 5 come first, notes on quality |
| `candidates_all.json` | every candidate, including rejected ones and the reason |
| `run_log.json` | models, prompt versions, token usage, timings, errors, the story bible |
| `feedback.jsonl` | editor approve/reject/adjust decisions from the UI (matched to clips by content) |
| `gold_from_reviews.json` | approvals saved as an evaluation set (written by *Save as eval set*) |

## Tests

```bash
python -m pytest
```

The tests use a synthetic screenplay, so they don't need the confidential pack. They cover the parser, the validator rules, retries and caching in the LLM client (with a mock HTTP transport), the full pipeline in both modes with a fake model, and the fallback when a provider fails. One extra test checks the real pack when it is present.

## Confidentiality

The input pack belongs to Kuku and is **git-ignored** (`input_pack/`, `*.zip`). `outputs/` is also ignored because it quotes short script lines, so share raw outputs privately (for example on Drive). If you deploy the UI, upload the zip at run time; never bundle the pack with the app.

## Known limitations

- **Text only.** Visual impact is inferred from ACTION and CAMERA descriptions, so a hook that is purely visual can be underrated.
- **Screenplay timing may differ from the final cut.** Expect the editor to nudge cut points by ±1–2 s.
- **The rule-based baseline is keyword-driven.** It finds the loud moments but sometimes opens on a weak line; LLM mode is the main path.
- **Free-tier quotas are small** (possibly ~20 requests/day on Gemini Flash; check your AI Studio dashboard). Episodes are therefore batched: a full 50-episode run makes **about 14 calls** (planned and actual counts are in `run_log.json`). `--episodes-per-call 1` gives one call per episode (~54 calls) when quota allows.

### Does batching hurt quality? Check it with a key

```bash
python -m promo_finder run --input input_pack --episodes 1-10 --out runs/batched
python -m promo_finder run --input input_pack --episodes 1-10 --out runs/single --episodes-per-call 1
python -m promo_finder stability --a runs/batched/<show>/promos.json --b runs/single/<show>/promos.json
```
