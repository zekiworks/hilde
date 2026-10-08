# Descriptions-only runs, 8 October

Made by `qa/describe.py` (Gemma 4 31B, images on, 3 runs per commit) on Can's machine. Each folder,
`<paper>/<commit>-<n>`, holds the run's `narration.json` and its `book.json` stamp.

- 9a70f65: the baseline, plus only the descriptions-only mode (book.json says 9a70f65…-dirty)
- 5872402: d9f4e2e's fixes (not for DeLM)
- f0c29d9: those fixes plus the regression fixes

Score with `python qa/qa.py facts qa/runs/2026-10-08/<paper>/f0c29d9-* --baseline qa/runs/2026-10-08/<paper>/9a70f65-*`
from a checkout at c39098c or later. `fact-misses.md` lists the facts that reject correct descriptions.
