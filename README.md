# Promo Finder

Reads a folder of timestamped microdrama screenplays and returns a **ranked list of 20–60 s promo clips**. Each clip has its segment timestamps, the hook (first 3 s), the cliffhanger it cuts on, one line on why it works, and a reason for its posting order.

Built for Kuku's AI Generalist assignment, Problem 1. The full design is in [DESIGN.md](DESIGN.md).

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add GEMINI_API_KEY (free: https://aistudio.google.com/apikey)

# Find promos (LLM mode if a key is set, otherwise the rule-based baseline)
python -m promo_finder run --input input_pack --out outputs

# Review UI
streamlit run app.py
```

Input can be a folder (searched recursively), a `.zip` or a single `.txt`.

## Commands

| Command | What it does |
|---|---|
| `run --input X [--mode auto\|llm\|heuristic] [--episodes 1-10] [--generator spec] [--judge spec]` | full pipeline → `outputs/<show>/` |
| `check --input X` | parse only; per-episode stats and warnings |
| `eval --promos outputs/<show>/promos.json --gold eval/gold.json` | precision@k, gold recall and boundary error against hand-picked promos |
| `stability --a run1/promos.json --b run2/promos.json` | how far two runs agree on the top k |

## Swapping models

Set models as `provider:model` in `config.yaml`, with an env var, or on the command line. No code changes are needed.

```bash
python -m promo_finder run --input input_pack --generator groq:llama-3.3-70b-versatile
PROMO_GENERATOR=openrouter:<model> PROMO_JUDGE=gemini:gemini-2.5-flash python -m promo_finder run ...
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
- **The judge only sees the clip.** It gets no story context, so its `standalone` score and `stranger_view` act as the "makes sense to a new viewer" test.
- **Every LLM step has a fallback.** If a call fails, that step falls back to the rule-based method and the failure is logged in `run_log.json`, never hidden. `--mode heuristic` runs fully offline.

## Outputs (`outputs/<show>/`)

| File | Contents |
|---|---|
| `promos.json` | ranked promos with all details (segments, hook, cliffhanger, scores, flags, post reason) |
| `promos.csv` | an editor-friendly table that opens in Excel |
| `report.md` | posting order, why the top 5 come first, notes on quality |
| `candidates_all.json` | every candidate, including rejected ones and the reason |
| `run_log.json` | models, prompt versions, token usage, timings, errors, the story bible |
| `feedback.jsonl` | editor approve/reject/adjust decisions from the UI |

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
