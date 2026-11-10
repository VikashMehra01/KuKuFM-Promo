# How candidates are found, scored and ranked

How the rule-based engine finds promo windows inside an episode, how every candidate (from the AI or the rules) is checked and scored, and how a full run gets from 212 candidates to the top 20. Code references are to `promo_finder/`.

> Example numbers are from the real 50-episode run of 10–11 Nov 2026. Lines are described by their tags, not quoted, because the screenplays are confidential.

## 1. Score every beat (`heuristics.score_beat`)

A **beat** is one line of dialogue, one action or one sound effect, or a camera shot with nothing else happening. Every beat gets three scores from 0 to 1, computed from keyword lists and the screenplay's own emotion tags.

| Signal | How it's detected |
|---|---|
| physical | Action or camera text contains e.g. *slap, strike, grab, shove, collapse, blood, dagger, lunges, slams* |
| reveal | Dialogue contains e.g. *truth, secret, knew, duke, pregnant, betray, married, "you are the"* |
| threat | Dialogue contains e.g. *kill, destroy, ruin, guards, punish* |
| humiliation | Dialogue contains e.g. *worthless, plain, disgrace, beg, low value* |
| exclaim / question / trailing | The line contains `!`, ends with `?`, or ends with `…` or `—` |
| short line | Dialogue of 12 words or fewer |
| sfx_cliff | A sound-effect line mentions e.g. *cliffhanger, sting, swell, boom, drone, peak* |
| cut_to_black | Camera text says *cut to black, freeze, fade to black* |
| intensity | Each high-emotion tag (e.g. *Outraged, Gloating, Desperate, Horrified*) adds 0.45, capped at 1. A physical beat gets at least 0.6. |

**The three scores (each capped at 1):**
- **Hook** = 0.40·physical + 0.35·reveal + 0.25·threat + 0.20·humiliation + 0.15·exclaim + 0.15·short line + 0.25·intensity
- **Cliffhanger** = 0.30·question + 0.25·trailing + 0.35·sfx_cliff + 0.35·cut_to_black + 0.30·reveal + 0.25·threat + 0.20·physical + 0.15·intensity
- **Intensity** = as in the table.

## 2. Find the windows in an episode (`heuristics.generate_heuristic_candidates`)

1. **Possible starts:** every beat with a hook score of at least **0.35**. E01 has 53 beats, and 15 pass.
2. **Possible ends:** for each start, every later beat that makes the clip **20–60 s** long.
3. **Each window gets a value** = hook(start) + **1.2** × cliffhanger(end) + 0.3 × length fit. The cliffhanger counts most, because cutting on an unresolved moment is the hardest part. Length fit is 1.0 for 30–45 s and falls to 0.5 at 20 s or 60 s.
4. **It keeps the top 3 windows that don't overlap.** These become `H-E01-1`, `H-E01-2` and so on, titled "E01 rule-based pick N", because rules can't write a creative title.

The three E01 picks, as computed by the code:

| Pick | Window | Hook beat | Cliffhanger beat | Value |
|---|---|---|---|---|
| H-E01-1 | 46.8 → 70.4 s (23.6 s) | "Outraged, Accusing" line: reveal + exclaim, intensity 0.9 → **hook 0.875** | "Horrified" question with a reveal → **cliff 0.667** | 0.875 + 1.2·0.667 + 0.3·0.68 = **1.88** |
| H-E01-3 | 107.5 → 137.5 s (30 s) | reveal + humiliation → hook 0.70 | sound sting + cut to black → cliff 0.70 | **1.84** |
| H-E01-2 | 75.0 → 107.3 s (32 s) | reveal → hook 0.61 | question → cliff 0.30 | **1.27** |

## 3. Validate every candidate (`validate.validate_promo`)

AI clips, AI montages and rule-based windows all go through the same checks. The AI never returns timestamps: it returns beat IDs (e.g. `E03-B017`), and code looks up their real times, so the times are correct by construction. The validator checks and repairs the brief's rules:
- the beat IDs must exist;
- 20–60 s in total;
- hook within the first 3 s;
- ends on the cliffhanger beat;
- no cut in the middle of a line;
- cuts snapped to camera-shot edges;
- at most 3 segments, which don't overlap.

Each repair is recorded in `flags`. Anything that can't be repaired is rejected, with the reason.

## 4. Score the whole clip (`heuristics.heuristic_score`, 0–1)

**0.30·hook (strongest beat in the first 3 s) + 0.25·cliffhanger (last 2 beats) + 0.15·average intensity + 0.15·standalone + 0.10·length fit + 0.05·clarity − 0.08 per extra segment**

- **Standalone:** 1.0 with 3 or fewer speakers, then −0.15 for each extra speaker.
- **Clarity:** 0.4 + the share of dialogue beats. Clips that are mostly visual are harder to judge from text.

How this score is used:
- **Rule-based mode:** it is the final score.
- **LLM mode:** final = **0.7 × the AI judge's rubric** (hook, cliffhanger, standalone, intensity, trope fit; 1–5 each, weighted) **+ 0.3 × this score**. Each source's list for the judge shortlist is also ordered by this score.

## 5. Final ordering (`rank.py`, both modes)

1. **Duplicates removed:** if two clips overlap by 50% or more in time, only the better one stays.
2. **Judge shortlist (LLM mode):** 40 places, **alternating the best AI clip and the best rule-based clip**. Rule-based clips are built to maximise the rule-based score, so a plain top-40 let them crowd out AI clips the judge rated equally (see RUN_REPORT §3.2).
3. **Early-episode bonus:** up to +0.08 for E01, falling to 0 at E10, because new viewers start at episode 1.
4. **Variety:** −0.05 if a clip has the same hook type as the previous pick, and −0.05 if it shares an episode with the last two picks.
5. **Keep the top N** (default 20; the UI's *Promos to rank* setting).

## 6. How the full run reached 212 candidates

| Stage | Count | What happened |
|---|---|---|
| AI clips | 103 | 50 episodes × 2–3 clips (the batch schema requires at least 2, at most 3) |
| Rule-based clips | 103 | Up to 3 non-overlapping windows per episode, fewer when an episode has too few strong hooks |
| AI montages | 6 | One cross-episode call |
| **Total candidates** | **212** | That AI and rules both gave 103 is a coincidence. |
| Rejected by validation | −13 | 10 too short, 2 too long to trim, 1 unknown beat ID (all AI clips) |
| Valid | **199** | |
| Duplicates | −45 | Mostly rule-based and AI clips covering the same moment |
| Distinct | 154 | |
| Judge shortlist | **40** | 20 AI + 20 rule-based; the other 114 are "below shortlist cut" |
| Ranked | **20** | top N |

Every candidate and its rejection reason is in `outputs/<show>/candidates_all.json`.

## 7. Where the rules are weak

- **They match keywords; they don't understand the story.** A line with "knew" or "secret" scores as a reveal even when it isn't one, and a shocking line that uses none of the keywords scores low.
- **The weights are hand-set, not tuned on data.** A hand-picked reference set (`eval/gold.json`) would let them be fitted.
- **There's a pull toward episode endings.** The screenplays mark the final sting as a cliffhanger in their sound-effect lines, so those beats often score well.
- **The keyword lists are English-only, and tuned for this genre.**

That's why, in LLM mode, the rule-based clips compete but the AI judge decides 70% of the final score.
