# Run report: real runs on the input pack

What happened when Promo Finder ran against Gemini's free tier on the 50-episode input pack (10 Nov 2026), how good the output was, what slowed it down, and what changed as a result.

> This file is committed, so it quotes no script lines. Clips are referred to by episode and timestamp only. The full outputs, including hook and cliffhanger text, are in the git-ignored `outputs/` folder.

## 1. Runs

| Run | Episodes | Model that actually answered | LLM calls | Result |
|---|---|---|---|---|
| Heuristic baseline | 1–50 | none (rules only) | 0 | 103 candidates, all valid; top 20 ranked |
| LLM #1 | 1–10 | — | 0 | Every call rejected ("invalid API key": the `AQ.` prefix was missing when the run started) |
| LLM #2 | 1–10 | `gemini-3.1-flash-lite` (the primary was overloaded) | 6 | Worked, but exposed two bugs (§4.3, §4.4) |
| LLM #3 | 1–10 | `gemini-3.1-flash-lite` | 5 + 1 from cache | Both bugs fixed; 37 candidates → 20 ranked |
| LLM full, attempt 1 | 1–50 | `gemini-3.5-flash` + fallback | stopped | Two batches (12 episodes) lost to a schema error (§4.5); stopped to save quota |
| LLM full, attempt 2 | 1–50 | 3.5-flash: 4 calls; Flash-Lite fallback: 6 | 10 + 4 from cache | Completed: no errors, all 50 episodes covered, 189 → 20 ranked (lenient parser). Kept as `runs/full_lenient_mixed/` |
| Schema run (old project) | 1–50 | 3.5-flash-lite → 3.1-flash-lite | stopped | Gemini rejected 6-episode schemas with a generic 400, and the client didn't recover (§4.6) |
| **LLM full, attempt 3 (current `outputs/`)** | **1–50** | **3.5-flash-lite (main) → 3.1-flash-lite** | **6 + 8 from cache** | **Completed: no errors, all 50 episodes covered, no re-asks. 159 → 155 valid → 20 ranked. Circuit breaker tripped once (§4.7)** |
| Schema check | 6–11 | 3.5-flash-lite: 3, 3.1-flash-lite: 1 | 4 | The 6-episode batch that had failed: **0 schema downgrades**, no errors |
| **LLM full, attempt 4: lean schemas (current `outputs/`)** | **1–50** | **3.5-flash-lite only, no fallback needed** | **8 + 6 from cache, then 2 for re-judging** | **No errors, 0 schema downgrades, decoder-enforced coverage, 212 candidates → 20 ranked. See §3.2** |

## 2. Output quality, episodes 1–10 (LLM run #3)

I checked the top picks by hand against the screenplay:

| # | Clip | Verdict |
|---|---|---|
| 1 | E01 00:46.80–01:10.40 (24 s) | **Strong.** The accusation at the altar lands in the first second, and it cuts on an unanswered question. |
| 2 | E03 00:58.20–01:25.50 (27 s) | **Strong hook** (an order to drag the bride out). It ends *on* the stranger's punch, which resolves the moment. Cutting about 3 s earlier, on his warning, would leave more unresolved. |
| 3 | E01 01:47.40–02:18.00 (31 s) | **Strong.** Public humiliation, ending on the music sting before her reaction. |
| 4 | E01 00:25.60–01:19.00 (53 s) | **Weak open.** The first 3 s are her walking up the aisle; the real hook comes about 8 s in. The rule-based score flagged it (0.40), but the judge still gave it 4/5. |
| 5 | E02 00:00.00–00:32.40 (32 s) | **Strong.** A rejection in the first line, ending on her unanswered appeal to her father. |
| 6 | E02 + E03 montage (41 s) | Works. Rejection, then a cut to the next episode's turn. Trimmed by code from 183 s. |

**Mix:** 14 of the top 20 came from the rule-based method and 6 from the LLM. Flash-Lite returned only 11 clips for 10 episodes (3 per episode were requested), and 7 of them ran past 60 s and had to be trimmed by code.

**Judge:** the "stranger view" summaries were accurate, e.g. "A bride walks down the aisle to find her fiancé marrying her sister instead." The `standalone` score dropped to 2 or below on a few clips that rely on earlier context, and those were flagged.

## 3. Full run, episodes 1–50

**Result:** completed with no errors in about 15 minutes, nearly all of it waiting on the LLM (generation 793 s, judging 67 s; parsing and validation took under 1 s).
- **Calls:** 14 in total: 10 made and 4 served from the cache. 10 generation batches of 4–6 episodes, no episode re-asked.
- **Answered by:** `gemini-3.5-flash` 4 calls, `gemini-3.1-flash-lite` 6 (3.5-flash returned 503 on the others).
- **Candidates:** 189 (AI 81, rule-based 103, montages 5), of which 188 passed validation. 40 were judged and the top 20 ranked.
- **Mix in the top 20:** 6 from the AI, 14 rule-based. **The top 10 covers 9 different episodes**, led by E01 and E03, which is the early-episode bonus working.

Top 10, checked by hand against the screenplay:

| # | Clip | Verdict |
|---|---|---|
| 1 | E01 00:39.00–01:19.00 (40 s) | Strong. The rival reveals the wedding swap and ends on her admission it was planned. |
| 2 | E03 00:58.20–01:28.10 (30 s) | Strong. Better than in the 10-episode run: it now cuts on a henchman's taunt **before** the fight resolves. |
| 3 | E45 01:08.80–01:48.80 (40 s) | Strong reveal hook; ends on the marriage being declared invalid. |
| 4 | E01 01:30.80–02:18.00 (47 s) | Good. Family cruelty, ending on the sting. |
| 5 | E18 00:45.00–01:30.00 (45 s) | Good. A purely audio/visual hook (a collapsing fire) found from the SFX line. |
| 6 | E28 01:06.80–01:51.00 (44 s) | Good. An accusation, ending on an identity taunt. |
| 7 | E15 00:44.80–01:26.00 (41 s) | Strong physical hook; cuts just before the brawl. |
| 8 | E36 02:43.00–03:20.75 (38 s) | OK. The judge rated standalone 3/5; it needs some context about the hero. |
| 9 | E16 00:40.80–01:23.19 (42 s) | Good. Interrogation hook; ends on a sting. |
| 10 | E43 00:16.20–01:16.00 (60 s) | OK, but right at the 60 s limit. |

**Weaker than it should be:**
- Most batches were answered by Flash-Lite, which returns fewer and longer clips. 18 AI clips had to be trimmed for length, and the rule-based method supplies most of the ranking.
- Of the AI's 81 candidates, 68 were "rejected", but almost all of those (101 across all sources) were cut at the judge shortlist (`judge_top_k: 40`), not for quality.
- The montages the model returned were all duplicates of or too similar to single-episode clips, so none ranked this time.

That run used the lenient parser; structured output (§4.5) came afterwards.

### 3.1 Attempt 3: schema-enforced, stable Lite main model (current `outputs/`)

- **Result:** completed with **no errors**, all 50 episodes covered, no episode re-asked, in about 8 minutes (generation 434 s, judging 30 s).
- **Calls:** 6 made, plus 8 served from the cache (batches from the stopped schema run with identical requests).
- **Answered by:** `gemini-3.5-flash-lite` 1, `gemini-3.1-flash-lite` 5. The circuit breaker tripped after the main model failed twice in a row (503, then a timeout).
- **Schema:** 5 of the 6 fresh calls had their schema rejected and were retried in JSON mode. The cause was identified and fixed afterwards (§4.6); the 6-episode re-check had 0 downgrades.
- **Candidates:** 159, of which 155 passed validation. Ranked: 6 from the AI, 14 rule-based. The Lite models still over-run: 36 AI clips were flagged "too long" and trimmed by code.

**Stability across runs.** Attempt 3 (all Lite) and attempt 2 (3.5-flash + Lite) share **5 of their top 10 and 12 of their top 20** (`promo_finder stability`). The clips both runs chose are the most robust picks, and they include the E03 guards scene, the E01 altar confrontation and the E45 marriage-invalidated reveal. The rest of the ranking varies with the model, which is a reason to keep a human editor approving the final list.

### 3.2 Attempt 4: lean schemas plus a fair judge shortlist (current `outputs/`)

**What changed (§4.8):**
- One lean schema per call type, asking only for what the model must decide.
- No hand-written JSON templates in the prompts.
- 2–3 clips per episode.
- Fixed episode count back in the batch schema.

**Result:**
- **No errors and 0 schema downgrades.** Every call was answered by the main model, `gemini-3.5-flash-lite`.
- All 50 episodes covered. Generation took 76 s, against 434 s for attempt 3.
- No wrong aliases this time (the bible prompt no longer uses this show's speaker label as its example).
- **The AI proposed twice as many clips:** 103, against 51 in attempt 3.

**A second flaw this exposed: the judge shortlist was biased.**

| | AI clips | Rule-based clips |
|---|---|---|
| Cut by the pre-judge shortlist (plain top 40 by heuristic score) | **62** | 48 |
| Median heuristic score | 0.44 | 0.58 |
| Mean judge score once judged | **3.25** | **3.26** |

The rule-based clips were *selected* to maximise the heuristic score, so ranking the shortlist by that score let them crowd out AI clips the judge rated just as highly.

**Fix:** `stratified_shortlist` alternates the best AI and best rule-based clips for the 40 judge places. Re-judging cost 2 calls (generation was cached):

| | Before the fix | After |
|---|---|---|
| AI clips judged | 12 | 20 |
| AI clips in the top 20 | 5 | **8** |
| Mean judge score, AI vs rule-based | 3.25 / 3.26 | 3.43 / 3.47 |

The top 10 now covers 9 different episodes. Six of them are in E01–E16, which is the early-episode bonus working; the rest run up to E28.

### 3.3 Camera text (11 Nov)

A follow-up run sent the full camera-shot descriptions to the generator and the judge (`runs/camera/`, 13 calls, 0 errors). It shares 13 of 20 with the run above. The judge's standalone scores rose and its cliffhanger scores fell, both beyond run-to-run noise. The ranking was not clearly better. Details: [CAMERA_TEXT_EXPERIMENT.md](CAMERA_TEXT_EXPERIMENT.md).

## 4. Bottlenecks, in order of impact

### 4.1 Free-tier quota: 20 requests per day, per model, and failed attempts count

This is the official number, taken from the API's own 429 response rather than from blogs:

```
quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier   quotaValue: 20
```

- The limit is **per model**. Each model has its own 20 a day, which is why a fallback to a second model extends the budget.
- **Failed attempts count.** Retrying an overloaded `gemini-3.8-flash` (503 "high demand") used up its whole day without a single successful answer. It reset about 20 hours later.
- Google's docs no longer publish these numbers; they point to the AI Studio dashboard.

**Changes:**
- Batched episodes, cutting a full run from ~62 calls to ~14.
- Shortlist before judging, with 20 clips per judge call.
- Disk cache, so re-runs are free.
- When a fallback exists, the primary gets only **1 retry** before the call moves over.

### 4.2 Model availability (HTTP 503 "high demand")

The newest Flash models (3.5 and 3.8) returned 503 errors often. The first fallback design waited out the overload: 4 retries × 300 s timeout, so one call could hang for about 20 minutes.

**Changes:**
- A per-call `FallbackLLM` (`gemini-3.1-flash-lite`) with fast failover.
- `run_log.json` records which model answered each call (`calls_served_by`), so quality differences can be explained.

### 4.3 AI mistake: the story bible merged different people

Flash-Lite's bible said the heroine's rival sister was the heroine herself (an unnamed "LADY IN …" speaker), and merged the stepmother into the father. Because aliases rename speakers, the judge then saw the heroine saying her rival's lines.

**Change:** code checks every AI alias against the script: **two names that speak in the same scene are different people**. Both wrong aliases are now rejected with a logged reason, and all the correct ones are kept (the bearded stranger is Alexander, the former groom is Gerald). The filter itself needed one fix: it first compared against the canonical name only, but the heroine is credited as `LADY …` in that scene, so it now compares against every name the title-stripping rule already ties to the character.

### 4.4 AI mistake: montage segments the length of whole episodes

Asked for 2–3 short moments (5–20 s each), the model returned segments of 90–115 s. Models are poor at adding up durations. All 6 montages were rejected in run #2.

**Change:** code does the arithmetic. Each segment is trimmed to an equal share of the time budget, keeping the hook at the start and the cliffhanger at the end. The montages became 41–44 s clips.

### 4.5 Strict output schema: one missing field lost 6 episodes

`gemini-3.5-flash` sometimes left out `cliffhanger_beat`. That field is redundant, because the prompt defines it as the end beat. The schema required it, so a single omission failed validation for the **whole 6-episode batch**, and two batches (12 episodes) dropped to heuristics.

**Change:**
- Redundant fields now default (`hook_beat` to the start beat, `cliffhanger_beat` to the end beat).
- List items are validated one at a time, so one bad item is dropped and logged instead of the whole batch failing.

The failed responses had been cached, so the fixed parser reused them for free.

**Root cause, and the deterministic fix:** we asked only for JSON mode (`json_object`) and described the schema in the prompt, so nothing *forced* the shape. Every call now sends the JSON Schema itself as structured output (`response_format: json_schema`, in `promo_finder/schemas.py`), so the provider constrains decoding to it:
- Required fields can't be left out, and enums (hook type, episode label, clip ID) can't be violated.
- **Coverage is enforced by the schema.** A batch must return exactly one entry per requested episode (`minItems = maxItems = number of episodes`, labels limited to that batch) with at least one clip each. The judge must score exactly the clips it was sent.
- If a provider rejects `json_schema`, the client drops back to `json_object` once and remembers it. The lenient parser stays as a second safety net.

### 4.6 Gemini rejects large structured-output schemas with a generic 400

With structured output on, every 5–6-episode batch came back `400 INVALID_ARGUMENT: Request contains an invalid argument.`, while the judge, montage and 4-episode schemas were accepted. I isolated it with one call per schema variant against `gemini-3.5-flash-lite`:

| Schema variant (6 episodes unless noted) | Result |
|---|---|
| Fixed-size array, `minItems = maxItems = 6` | 400 |
| Same, 4 episodes | OK |
| No fixed size on the outer array | **OK** |
| Object with one required key per episode | 400 |

The limit is total schema complexity, not any single keyword, and the error message doesn't say so.

**Changes:**
- Batch schemas keep every field-level constraint (required fields, enums including the batch's episode labels, at least 1 clip per episode) but no fixed outer size. Per-episode coverage is enforced in code (re-ask once, logged).
- If a schema is still rejected, that one call is retried in JSON mode; the client keeps sending schemas to later calls. Downgrades are counted in `run_log.json` (`usage.*.schema_downgrades`).

### 4.7 Unstable main model: circuit breaker

The full Flash models (3.5, 3.6, 3.8) returned 503 on almost every call during testing, even with fresh quota on a new project. Waiting each call out costs time and quota.

**Changes:**
- The main model is now the most capable model that is reliably *available*: `gemini-3.5-flash-lite`, which answers in 2–40 s.
- **Circuit breaker:** after 2 consecutive failures, the main model is skipped for the rest of the run, and `run_log` records `primary_tripped`.
- The fallback has its own short timeout (120 s). One stalled fallback call had cost 5 minutes before this.

### 4.8 The schema itself was over-built (review of how it was passed)

The transport was correct: `response_format: json_schema` on the OpenAI-compatible endpoint, the documented route. Schema-enforced responses had 0 missing fields, against 32 across 5 JSON-mode responses. The content of the request was wrong in four ways:

1. **Two format specs.** The prompts still contained a hand-written JSON template, which wasn't even valid JSON (e.g. `"strength": 1-5`, `"a|b|c"` enums) and conflicted with the schema. Google's docs pass the schema only in `response_format`.
2. **Over-constraint.** Fixing the episode count on the heavy schema went over Gemini's complexity limit. `minItems: 1` turned the minimum into the target (about 1 clip per episode instead of about 2). `strict: true` is an OpenAI-only flag.
3. **Redundant asks.** `cliffhanger_beat` equalled `end_beat` in 25 of 25 responses. The separate `hooks[]` and `cliffhangers[]` lists only fed the montage index, yet they made up most of the nesting.
4. **Two definitions** (the schema and the parser) that had already drifted apart.

**Fix:**
- One **lean** schema per call type (`schemas.py`), asking only for what the model must decide. Derived fields are filled in by code.
- No templates in the prompts. If a provider rejects a schema, the client moves *the same schema* into the prompt.
- 2–3 clips per episode.
- A test (`tests/test_schemas.py`) that fails if the schema and the parser disagree.

The lean 6-episode schema is accepted **with** the fixed episode count, so coverage is enforced by the decoder again (`schema_fixed_episode_count: true`).

### 4.9 Smaller issues found along the way

- An invalid key used to let the run carry on as "llm" with every step silently falling back. It now stops the run immediately (`LLMAuthError`).
- The "retry without JSON mode" path fired on any 400 error. It now fires only on errors about JSON mode.
- `gemini-2.5-flash`, the original default, is closed to new keys and returns 404. The default is now `gemini-3.5-flash`.
- Gemini 3.x docs say not to set temperature (values below 1.0 can cause looping). Temperature is now optional and not sent by default.

## 5. Where the tool still picks badly

1. **Hook timing within the first 3 s.** The validator checks that the *declared* hook beat is within 3 s. If the model declares a slow opening beat as the hook (pick #4 above), the clip passes. Possible fix: require a minimum rule-based hook score on the first beat, or move the start forward to the strongest beat in the first ~8 s.
2. **Cutting on the resolution.** Some clips end on the payoff (the punch lands) rather than just before it. Possible fix: a rule that prefers cutting on the beat *before* a physical-impact sound.
3. **Context-dependent lines.** Lines that shock only if you know the backstory are sometimes overrated; the judge's `standalone` score partly catches these.
4. **Weaker fallback model.** When Flash-Lite answers, there are fewer and longer candidates, so more of the ranking comes from rule-based picks.
5. **Text only.** Visual hooks are inferred from camera and action descriptions, and screenplay timing may differ from the final cut by ±1–2 s.

## 6. Next steps

- **Re-run the full show with the schema fix once quota resets**, ideally with a full Flash model as the main model when its capacity recovers (`--generator gemini:gemini-3.8-flash`; the circuit breaker makes this safe). The quota is per project per model, and a new key only helps if it is in a new project.
- Hand-pick ~10 gold promos (`eval/gold.json`) and report precision@10 and boundary error.
- Once the 3.8-flash quota resets, run E1–E10 batched vs. one episode per call, and compare the two with `promo_finder stability`.
- Try the two §5 fixes (hook-strength floor, cut before impact) and re-measure.
