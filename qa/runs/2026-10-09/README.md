# Descriptions-only runs, 9 October

Same method as `../2026-10-08/` (qa/describe.py, Gemma 4 31B, images on, 3 runs per commit). Folders `<paper>/<commit>-<n>`.

- e65d8a1: equations keep their printed numbers (R23-01), the caption-number check (9a37d37), and D16 inside
  the code bullet, which cut table descriptions short (DeLM Tables 2, 3, 8).
- d3e4e44: e65d8a1 with D16's rule in a bullet of its own.

Compare with `python qa/qa.py facts qa/runs/2026-10-09/<paper>/d3e4e44-* --baseline qa/runs/2026-10-08/<paper>/9a70f65-*`
(D17: a regression is a fact right 3/3 before and at most 1/3 after).
