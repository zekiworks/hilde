# Hilde QA: protocol for agents

This folder is the single source of truth for Hilde's audiobook QA. Reviewers (Claude, Astra), the fixer (Can's coding agent) and humans (Ümit, Can) all read and write the same files. The "Hilde QA Checklist" doc on claude.ai is a human view of these files; when they disagree, the files win.

Write in English. Keep every record self-contained: another agent must be able to act on it without this conversation.

## Files

| File | Holds | Who writes |
| --- | --- | --- |
| `bugs.yaml` | Bug classes: one per kind of failure, with priority, status, layer, fix and test | Reviewers propose; fixer updates status |
| `findings.jsonl` | One JSON object per line: a single observed instance of a bug in one run | Reviewers append; fixer updates status |
| `runs.yaml` | Every reviewed run: paper, date, Hilde commit, model, output file | Whoever produced the run |
| `papers.yaml` | Source papers: title, URL, template quirks | Reviewers |
| `golden/<paper>.yaml` | Executable assertions for one paper; the regression suite | Reviewers add; fixer must keep green |
| `decisions.yaml` | Settled choices (D01…); do not reopen without a new decision | Humans; agents may draft |
| `questions.yaml` | Open questions for humans (Q01…) | Anyone asks; humans answer |
| `voices.yaml` | Narrator recipes and their status | Can |
| `qa.py` | `validate`, `check`, `report`, `next` | Fixer |
| `outputs/` | Legacy: copied follow-along text from runs before book folders | Whoever produced the run |

## IDs

- Bug class: `B01`, `B02`, … (never reuse a number)
- Finding: `<run>-<nn>`, e.g. `R6-09`
- Run: `R1`, `R1b`, `R2`, … in the order reviewed
- Golden assertion: `<PAPER>-<nn>`, e.g. `CLM-08`
- Decision `D01…`, question `Q01…`

Cite IDs everywhere: commit messages ("Fix B05: detect 'Table N |' captions"), PR titles, chat handoffs.

## Workflow

1. **Run.** Generate the audiobook. Its book folder (`Audiobooks/<slug>--<hash12>/`) is the output: `narration.json` is exactly what every voice reads, and `book.json` records the source hash, Hilde version, commit and model. Add the run to `runs.yaml` with those values; add the PDF's hash to `papers.yaml` if it is new.
2. **Check.** `python qa/qa.py check Audiobooks/<slug>--<hash12>/ --json`. The paper is inferred from the source hash. Generic checks run on every paper (typed ones use passage types and sources; G10 checks the folder's hashes and voices); golden assertions run when `golden/<paper>.yaml` exists. Plain text files still work for older runs.
3. **Review.** Compare the output with the source PDF. Append findings to `findings.jsonl` with status `reported`. Link each to an existing bug class; if none fits, add a class with status `proposed`.
4. **Cross-check.** A second reviewer, ideally a different model, re-checks against the PDF and sets `verified` plus its name in `by`. If it disagrees, set `disputed` and explain in `notes`; do not delete the record.
5. **Fix.** The fixer runs `python qa/qa.py next`, takes the top open class, fixes it, adds or updates golden assertions, and sets `fixed` with `fixed_in` (commit) on each finding it addressed. A class becomes `fixed` only when all its findings are fixed and the golden assertions tagged with that class (`bug:` in the golden files) pass on the latest runs. Another class's red assertions do not hold it open.
6. **Re-test.** Regenerate the control set (attention, procedural-graphs, bert) plus one paper Hilde has never seen. Check known findings first, then look for new breakage. Set `verified-fixed` or `regressed`.
7. **Listen.** A human listening pass closes a run. Sync problems (B01) are found only this way.

## Rules

- **Evidence or it didn't happen.** Every finding names where it is in the output and in the source (page, section, equation, table). Quote the source in 15 words or fewer; for numbers, show the arithmetic.
- **Say how you verified.** `verified` is one of `pdf` (checked against the paper), `image` (checked against the figure), `computed` (recomputed from the paper's numbers), `internal` (contradicts the output itself or an approved earlier output), `partial`, `unverified`.
- **Never delete records.** Change `status` and add `notes`. Only status, notes, by, verified and fixed_in change after a record is written.
- **Source quirks are not bugs.** When the paper itself is inconsistent (Attention 41.0 vs 41.8 BLEU), record it with status `keep` and a golden `must_contain`, so nobody "fixes" it.
- **Respect decisions.** If a finding conflicts with a decision (e.g. D01: tables are shown as printed), it is `wontfix` unless a new decision supersedes the old one.
- **A book's text stays as it was made (D12).** A new voice of a book reads its narration.json and must not regenerate anything (B14). Only "Recreate with the latest Hilde" writes a book's text again; compare a recreated book with its earlier text before approving it.
- **One class per root cause.** Prefer adding a finding to an existing class over creating a near-duplicate class.

## Status values

- Finding: `reported` → `verified` → `fixed` → `verified-fixed`; also `regressed`, `disputed`, `keep`, `not-a-bug`, `wontfix`
- Bug class: `proposed` → `open` → `fixed`; also `regressed`, `wontfix`
- Priority: `P0` blocker, `P1` meaning is wrong, `P2` structure, `P3` wording

## Handoff message

When you pass work to another agent, in chat or in a PR description, start with this block so the receiver can parse it:

```
QA-HANDOFF
from: claude (reviewer)
to: fixer
run: R6
findings: R6-01..R6-19
bugs: B03, B02, B08
ask: Fix B03 (write figure and table numbers from the caption in code). Keep CLM-* and generic G07 green.
blocked_on: Q01
```

Keep the ask to one or two sentences. Everything else lives in the files.

## Finding format

One line per finding in `findings.jsonl`:

```json
{"id": "R6-09", "run": "R6", "paper": "clm", "bug": "B02", "status": "verified",
 "where": "Appendix C, Equation 9 description", "source": "Appendix C, Eq. 9; Fig. 14",
 "observed": "Calls the prompt length 'prefilled'.",
 "expected": "P is the prompt length; prefilled tokens are P minus R.",
 "evidence": "Fig. 14's 1.41, 5.74 and 10.81 x 10^14 FLOPs only reproduce with P = prompt length.",
 "by": ["claude"], "verified": "computed"}
```

Required: `id`, `run`, `paper`, `bug` (null only for `keep`), `status`, `where`, `observed`, `expected`, `by`, `verified`. Optional: `source`, `evidence`, `notes`, `fixed_in`, `assert` (golden IDs that cover it).
