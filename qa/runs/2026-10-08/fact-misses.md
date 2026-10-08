# Facts that reject correct descriptions (descriptions-only runs, 8 October)

- **From:** Can's agent (fixer), for Claude and Astra.
- **Runs:** `qa/runs/2026-10-08/<paper>/<commit>-<n>/` on this branch:
  - 9a70f65: the baseline, plus only the descriptions-only mode;
  - 5872402: d9f4e2e's fixes;
  - f0c29d9: those fixes, plus the regression fixes;
  - n = 1–3.
- **Scorer:** `python qa/qa.py facts <runs> [--baseline <runs>]` at c39098c or later.
- **How these were found:** for each fact that was not right in every run, I read the sentence of the description the scorer used (`qa._json_description()`).
- **A fact is listed as a miss** only when that sentence states the fact correctly.
- **Run IDs** are the ones where the fact scored wrong or missing although the description has it.

My 20:45 estimate ("5–8 per paper") was too broad. Below are the misses I can show, and the cases I first called misses that are real.

## BERT

| Fact | Runs | What the description says | Why the pattern misses it | Suggested fix |
|---|---|---|---|---|
| F01 | all 6 at 5872402, f0c29d9 | "processed as a sequence starting with a special classification token, [CLS], and separated by a special separator token, [SEP]" | needs "[CLS] … in front of every input" | also accept "starting with … [CLS]" |
| F02 | 5872402-3, f0c29d9-1..3 | "First are the token embeddings, which are unique to each token." then the segment and the position embeddings, each in its own sentence | the three names must sit within 120/160 characters | allow them across sentences of the description |
| F04 | 5872402-1, f0c29d9-1, f0c29d9-3 | "BERT LARGE achieves … the highest average of 82.1" | 82.1 is also OpenAI GPT's MNLI-m, stated later; that occurrence makes it wrong | a region for the average, or exclude GPT's 82.1 |
| F07 | f0c29d9-1, f0c29d9-2 | "the Pre-OpenAI state of the art at 74.0" | holder `pre-OpenAI` is case-sensitive | `(?i)` |
| F12 | 5872402-2, 5872402-3, f0c29d9-1, f0c29d9-3 | "the BERT large ensemble reaches a development EM of 85.8 and F1 of 91.8"; "the BERT large ensemble without TriviaQA at 85.8 and 91.8" | 91.8 is also the single model + TriviaQA test F1; "without TriviaQA" matches the `tqa` holder | a dev/test region; holder `tqa` not after "without" |
| F30, F31 | all 3 at 5872402 | "the smallest configuration … has a perplexity of 5.84 and accuracies of 77.9 percent on MNLI-m, 79.8 percent on MRPC, and 88.4 percent on SST-2" | 5.84 and 88.4 are more than 60 characters apart | allow the whole sentence |

**Not misses (real omissions):**
- F30 and F31 at f0c29d9: the description gives the perplexities (5.84 to 3.23) but not the SST-2 accuracies.
- F12 in 5872402-1 and f0c29d9-2: 91.8 is not stated.

## RRSI

| Fact | Runs | What the description says | Why | Suggested fix |
|---|---|---|---|---|
| F02, F05, F08 | every run of every commit | "H 0 scores 82.0, the prior average is 82.3, and RRSI is 83.8" (39.4 and 17.9 the same way) | holder `prior` (`prior methods\|four baseline\|average of the`) does not match "prior average", so 82.3 goes to H0 | add `prior average` |
| F01–F09 | f0c29d9-3 | "Panels b, c, and d show the out-of-distribution held-out scores … In Coding, measured on SWE-bench Verified, H zero scores 82.0 and the prior average is 82.3, while RRSI improves to 83.8 …" | regions open on "panel b", "panel c", "panel d" | also open regions on the domain names (Coding, Agentic workspace, Frontier-Eng) |
| F33 | all 3 at 5872402, all 3 at f0c29d9 | "the score on Terminal-Bench 2.1 rises from 74.2 with the initial harness to 80.2 with RRSI" | 74.2 and 80.2 are more than 30 characters apart | allow up to about 80 |

F02, F05 and F08 lower every commit equally, so they do not change the comparison. F33 and F01–F09 do.

## Procedural Graphs

| Fact | Runs | What the description says | Why | Suggested fix |
|---|---|---|---|---|
| F02 | 5872402-1, 5872402-2, f0c29d9-1 | "extracts its 2-hop subgraph G t, falling back to the full graph if matching fails" | the pattern needs "falls back" | `fall(?:s\|ing)? back` |
| F38, F40 | 5872402-1..3 | "the highest full-horizon survival at 58.0 percent"; "34.0 percent" | the value regex rejects a trailing ".0" | `58(?:\.0)?`, `34(?:\.0)?` |

**Not misses** (I wrongly called them misses at 20:45):
- F19 is real. Figure 3's description leaves out Gemini 3.5 Flash. The "no configuration achieves full-horizon survival" I had quoted is from Table 8.
- F01 in 5872402-3, f0c29d9-2 and f0c29d9-3: "2-hop" is not stated.
- F44 ($38.20M raised) is a real omission.

## CLM

| Fact | Runs | What the description says | Why | Suggested fix |
|---|---|---|---|---|
| F46, F47 | 5872402-2 | "44.6 with 179 PF for CLM, 44.2 with 181 PF for CLMs subagents, and 42.3 with 437 PF for Summary" (correct) | "CLMs subagents" matches the CLM holder before CLM-SA's; in "42.3 with 437 PF for Summary" another number sits between the value and its holder | CLM holder `\bCLMs?\b(?!\s*\(?subagents)`; let rule 1 skip a "with N PF" pair |
| F55 | 5872402-3 | "the CLMs agent swarm achieves … 1.044, while the summary agent swarm achieves 1.026" | holder `\bSummary\b` is case-sensitive | `(?i)` |
| F72 | 5872402-3 | "7.14 PFLOPs, compared to 10.98 PFLOPs for standard SGLang" | holder `Standard( SGLang)?` is case-sensitive | `(?i)` |
| F98 | 5872402-2 | "For Needle Retention, the setting is chunks, with values of 4, 24, 60, 192, and 224." | the next sentence gives Sudoku's 224 too, owned by Sudoku | a region per task |

**Real errors in these runs, not misses:**
- Figure 6's scores credited to the wrong model:
  - "the CLMs subagents configuration reaches the highest score of 44.6" (f0c29d9-1, f0c29d9-2);
  - "CLMs agent swarm reaches 44.6" (9a70f65).
- "speedups of 2.34.2" (F56, f0c29d9-1).
- "Standard SGLang uses … 7.14 PFLOPs for decode" (F73, f0c29d9-3).

**On Hilde's side, not the fact set:** F13, F16 and F19–F21 score "no description found" in some runs. CLM's equation descriptions there open "The equation…", not "Equation N" (R20-09, caption and number recognition), so the lookup cannot find them.

## DeLM

F03 is fixed with the pattern you sent (c39098c / 9a37d37). I have not sorted DeLM's other facts. Its 4 regressions are F75 (real: the 73% became "a large majority") and F21, F31 and F35, each missed in one run of three.
