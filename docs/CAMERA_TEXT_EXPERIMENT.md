# Experiment: sending the camera lines to the LLM (11 Nov 2026)

**Question:** the screenplays describe every camera shot (`**CAMERA:**` blocks). These often carry story: a reveal, a reaction, or a slap shown only on screen. Were the LLM steps seeing them, and do the promos improve if they see them in full?

> This file is committed, so it quotes no script lines. Clips are referred to by episode and timestamp only.

## 1. What the LLM saw before

Camera blocks are handled entirely by rules ([beats.py](../promo_finder/beats.py)), with no LLM involved:
- **Overlapping a line:** a camera block whose time range overlaps a dialogue, action or sound line is attached to that line as its `visual` text.
- **Overlapping nothing:** a camera block that overlaps no line becomes its own `VISUAL` beat.

That part was fine. Two gaps were not:

| Gap | Size on the input pack |
|---|---|
| **The generator saw only the first 110 characters** of a line's camera text. The cut often fell on the action at the end of the shot description. | 681 of 1,830 lines with camera text were truncated, losing **17%** of all camera text |
| **The judge never saw camera text attached to a line.** It got dialogue, action, sound and only the standalone `VISUAL` beats. A slap or a reveal written only as a shot during dialogue was invisible to the step that scores hook, cliffhanger and standalone. | every judged clip |

A third finding came out of measuring: one shot usually spans several lines, so attaching it to each line repeated it. **25% of the attached camera text was a repeat** of the previous line's shot.

## 2. The change

| | Before | After (new defaults) | Config |
|---|---|---|---|
| Generator camera text | first 110 characters | full text | `promo.camera_text_chars: null` (before: `110`) |
| Repeated shots | on every line they span | listed once, on the first line they cover | `promo.camera_dedupe: true` (before: `false`) |
| Judge transcript | no attached camera text | attached camera text as `\| cam: …`, deduplicated the same way | `ranking.judge_sees_camera: true` (before: `false`) |

**Prompt size:** the generator's beat text went from about **103.8K to 98.6K tokens** for 50 episodes (**−5%**), despite carrying the full camera text, because the repeats were removed. It's still 10 generation batches.

**The old behaviour is reproducible exactly.** With the three settings set back to their "before" values, a full run was served **14 of 14 calls from the cache** (0 misses, with a stub client that fails on any API call) and produced an **identical top 20**. So the old prompts are rebuilt byte for byte. Tests: [tests/test_camera.py](../tests/test_camera.py).

## 3. Runs

| Run | Folder | Calls | Result |
|---|---|---|---|
| Baseline (camera text truncated, judge without it) | `outputs/` | cached | 212 candidates, 40 judged, top 20 |
| **Camera text** | `runs/camera/` | **13 + 1 cached**, all answered by `gemini-3.5-flash-lite`, 0 errors | 211 candidates, 40 judged, top 20, in **2.5 min** |
| Judge noise check (same 40 clips, uncached, main model only) | `runs/camera/judge_noise.json` | 4 (+1 retried 503) | see §4 |

## 4. Effect on the judge, separated from noise

The judge runs at the model's default temperature, so its scores vary from run to run. Comparing two runs naively mixes the camera effect with that noise, so I re-judged the **same 40 clips** twice:
- **Noise:** the same camera prompts as the camera run, judged again.
- **Effect:** the same clips **in identical batches**, judged with and without camera text.

Mean change in the 1–5 score, with the number of clips that went up / stayed the same / went down:

| Rubric | Noise (same prompts again) | **Effect of camera text** | Verdict |
|---|---|---|---|
| hook | +0.05 (11 / 20 / 9) | +0.03 (10 / 21 / 9) | **No effect.** The change is within noise |
| cliffhanger | +0.15 (9 / 28 / 3) | **−0.30 (1 / 26 / 13)** | **Real: stricter** |
| standalone | −0.05 (6 / 26 / 8) | **+0.25 (12 / 26 / 2)** | **Real: higher** |
| intensity | 0.00 (7 / 26 / 7) | −0.15 (2 / 30 / 8) | Small; slightly lower |
| trope_fit | −0.15 (1 / 32 / 7) | −0.07 (4 / 29 / 7) | Within noise |

The first naive comparison between the two full runs, on 21 clips judged in both, pointed the same way for cliffhanger (−0.33) and standalone (+0.10). But it also showed hook +0.29, and the controlled test shows that was noise.

**How to read it:**
- **Standalone went up, and the hand check agrees.** The camera text says who is who: the mother grabbing his coat in E36, the groom and the rival at the altar in E01. That is exactly the "makes sense to a stranger" test the brief asks for.
- **Cliffhanger went down, and the hand check doesn't clearly justify it.** I read the three biggest drops: E11 00:28.9–01:24.0, E28 00:20.4–01:04.8 and E28 01:53.0–02:14.2. All three still cut on an unresolved threat or provocation, and the extra camera lines describe reactions, not a resolution. So this looks like the judge getting harsher with more text, not more accurate. It's a cost of the change.
- **My main worry did not happen.** I expected the adjective-heavy shot descriptions to inflate intensity; it went slightly down instead.
- **The hook gain I expected did not show up on average.** Clips with a purely visual opening, such as E45 ~01:26 (doors bursting open), do benefit, but they are few among the 40.

## 5. Effect on the final ranking

| | Baseline | Camera text |
|---|---|---|
| Shared with the other run | | **5 of the top 10, 13 of the top 20** |
| Top 20 by source | 8 AI, 12 rule-based | 6 AI, 14 rule-based |
| Episodes in the top 10 | E01, 02, 03, 05, 11, 13, 16, 27, 28 | E01, 03, 05, 11, 16, 36, 45, 47 |
| #1 | E01 00:46.8–01:10.4 | the same clip (also the only editor-approved clip so far) |

Hand check of what moved in or out of the top 10:

| Change | Clip | Verdict |
|---|---|---|
| In at #4 | E45 01:08.8–01:48.8 | **Strong.** Reveal hook; ends on the marriage being declared invalid |
| In at #7 | E36 02:43.0–03:20.8 | **Good.** Ends on cut to black plus a sting; needs some context |
| In at #9 | E11 01:28.0–01:52.8 | **Good.** Ends on a held insert shot of the evidence |
| In at #8 | E45 01:26.5–02:01.8 | Good visual hook, but it **overlaps #4 by 22 s**, just under the dedupe threshold (IoU 0.42 < 0.5). Two near-duplicates in the top 10 |
| In at #6 | E47 01:03.5–01:44.8 | **Weak.** Ends on a complaint, not a cliffhanger; a late episode |
| In at #10, replacing #6 | E01 01:30.8–02:18.0 replaced E01 01:47.4–02:18.0 | **Worse.** The same ending, but a longer clip with a weaker opening line |
| In at #2, replacing #7 | E03 01:02.4–01:25.5 replaced E03 00:58.2–01:25.5 | **About equal.** The same scene; it starts 4 s later and still ends on the punch landing |
| Out (was #2) | E02 00:00.0–00:30.6 | **A loss.** A strong first-line rejection from episode 2, good for the funnel |
| Out (was #3, #5) | E28 01:53.0–02:14.2, E27 02:04.6–02:24.9 | **A loss.** Both end on strong unresolved threats; pushed down by the stricter cliffhanger scores |

**Verdict: the ranking did not get clearly better.** It gained two strong late-episode reveals and lost three good early or mid-show clips, mostly through the stricter cliffhanger scores. Also, 13 of 20 shared is within the range two runs can differ anyway (the run report saw 12 of 20 between two models). One run cannot show a ranking-level improvement either way.

## 6. Decision

**Kept on as the default:**
- The generator now sees what the viewer sees, in full, with 5% fewer tokens.
- The judge's standalone scores measurably improve, and standalone is one of the brief's three rules.
- Turning it off is a three-line config change that reproduces the old prompts exactly.

**Known cost:** the judge rates cliffhangers about 0.3 lower with camera text, without a clear reason. Watch it in the editor reviews.

**What would settle it:** editor reviews. With about 30 approve/reject decisions on the baseline, **Save as eval set** produces a gold file, and `promo_finder eval` can score both runs against it. So far there is one review, which both runs agree on.

**Follow-ups found along the way, not changed:**
- **Unaliased speaker label.** The former groom speaks under a descriptive label in E01–E02, but the camera text names him. The story bible didn't link the two, so the judge sees two names for one person. Camera text makes this mismatch more visible.
- **Dedupe threshold.** It lets two overlapping E45 clips (IoU 0.42) both reach the top 10. Raising the threshold slightly, or measuring overlap by shared time, would catch it.
