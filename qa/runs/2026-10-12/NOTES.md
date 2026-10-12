R24 (2026-10-12): Gemma 4 31B (lm-studio), images on, temperature 0.2. Three runs per folder group.

- `<paper>/<commit>-<n>/`: descriptions-only runs (qa/describe.py). `merton/full-<commit>-<n>/`: full adaptation.
- 26abdb1 is 7591bb9 (the 12 Oct book's code) plus per-call records; it is the baseline.
- `clm/efc7009-1,2` and `clm/6d733e7-3` ran efc7009's code; describe.py reads the commit when a run ends, after
  6d733e7 had been committed. CLM's extraction and prompt are the same at both.
- `clm/e6417bb-*` ran 852753d's code (e6417bb changed only qa/). These are the D17 confirmation runs.
- `experiments/exp*.jsonl`: per-block model calls with one factor varied (request side, prompt side, images),
  3 or 6 repeats each; `case` names the block, `response` is the raw answer. exp1: 9a70f65 vs 7591bb9 requests
  and prompts; exp2/exp5: the fixed requests; exp3/exp4: two wordings of the conditions rule on Table 4;
  exp6: Equation 9's prose batch with 7591bb9's and efc7009's extraction.
