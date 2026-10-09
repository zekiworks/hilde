# Fact sets: what each description must get right

A fact set lists, for one test paper, the facts its figure, table and equation descriptions must state correctly. It replaces reading whole books: each description is scored against fixed facts (see `hilde-plan-2026-10-08.md`).

Reviewers (Claude, Astra) write the fact sets. `python qa/qa.py facts RUN... [--baseline RUN...]` scores them, and `python qa/describe.py` makes the runs (descriptions only, 3 per paper). `qa.py validate` checks every fact set. In a flow mapping (`- {id: …, what: …}`), quote a value holding a comma: `what: 'Circle packing, CLM'`, or YAML splits it into another key.

## Rules

- **Values, not wording.** A fact is right when its value sits with the right method, symbol or unit, in any phrasing. It is never a ban on an old sentence.
- **Both directions.** A fact set holds the errors found and the facts that were already right, so a regression fails too.
- **Missing is not right.** A description that drops a fact to avoid an error scores `missing`, not `right`.
- **Verified.** Every fact names its place in the source (`source`) and how it was checked (`verified`: `pdf`, `figure`, or a reviewer's name).

## Format

```yaml
paper: clm
visuals:
  - name: Figure 6                 # "Figure N", "Table N" or "Equation N"
    caption: 'outperform Codex-style summary harness'   # words found only in its caption, case-insensitive
    scope: book                    # optional: search the whole book instead of the description
    holders:                       # who a number can belong to: name -> regex
      CLM: '\bCLMs?\b'
      CLM-SA: 'subagents?'
    regions:                       # optional: parts of one description, by the text that opens each
      qwen: '(?i)\bfor Qwen'
    facts:
      - id: CLM-F43
        what: 'EdgeBench-10, Qwen3.6-27B: CLM final score'
        value: '44\.6'             # regex for the number
        holder: CLM                # or a list, for a shared value
        region: qwen               # optional
        severity: critical         # critical | important | minor
        source: §5.1.2; Fig. 6a
        verified: pdf
        seen: {2026-10-07: wrong, 2026-10-08: wrong}   # status per run date
      - id: CLM-F56
        what: Package versions are versions, not speedups
        says: ['…']                # optional: any match counts as right
        not: ['(?i)speedup[^.;]{0,40}\b\d+\.\d+\.\d+']  # any match is wrong
```

## Where a description is found

- **Figure or table, in narration.json:** the first passage whose sources hold the caption, when it is a figure or table passage. When it is not (a caption not recognized, as in CLM on 84e7f88 and 9a70f65), take the last passage before it that carries a visual: a figure, table, or equation passage, or a body passage with a figure, table, or equation among its sources (a figure joined into prose), without passing another visual's caption.
- **Figure or table, in follow-along text:** the paragraphs after the nearest "Description" before the caption. If there is none, the one paragraph just before the caption.
- **Equation:** every paragraph that opens with "Equation N", also inside a passage of several paragraphs.
- **`find`, in narration.json:** the first figure, table, or equation passage matching it; when none does and the visual has a number ("Figure 3"), the figure or table passage opening with that name, which code writes from the caption.
- **Descriptions-only runs** (`qa/describe.py`, `descriptions_only` in book.json) keep the author's prose, so facts with `scope: book` are not scored there.

`python qa/qa.py facts` implements these rules; on the 8 October books it gives each fact's `seen` status for 380 of 382 facts. The other two, BERT-F42 and BERT-F45, are about the reader's layout, which narration.json does not hold.
- **Opening name:** a description that opens "Figure 8 shows…" or "The equation shows…" for Figure 6 is scored as a wrong label. Labels are written by code, so they are counted apart from the facts. An equation found by `find` is checked the same way: "The equation says…" for Equation 8 is a wrong label (R23-01).

## Scoring one fact

1. A `not` match anywhere in the description: **wrong**.
2. A `value` fact: each occurrence of the number, inside its `region`, is given an owner:
   - a holder right after it, joined by "for", "by" or "of" ("47.3 for CLM", "7.8 percent reused by SCR");
   - else the last holder before it in the same sentence that rule 1 has not already taken;
   - none for the A of "from A to B", whose owner a sentence does not say; such facts also get a `says` pair (`'28\.8[^;]{0,30}42\.5'`).

   An occurrence owned by another holder makes the fact **wrong**. One owned by its holder, or by no one, makes it **right**.
3. Else a `says` match: **right**.
4. Else **missing**. A fact with only `not` patterns is right when none match.

## The gate (from the plan)

A change is accepted when:
- no fact regresses (D17): a fact right in every baseline run must be right in more than a third of the new runs; one missed in fewer is wavering, listed but not counted, and a regression is confirmed by rerunning its paper;
- the facts it targets are right in 3 of 3 descriptions-only runs;
- the same holds on one paper Hilde has never seen, with its own fact set.

Report per run: right, wrong and missing counts by severity, the list of changes since the baseline, and the opening names.
