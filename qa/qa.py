#!/usr/bin/env python3
"""Hilde QA tool. Read AGENTS.md first.

  python qa/qa.py validate                       check every QA file for schema and broken references
  python qa/qa.py check OUTPUT [--paper ID]      generic checks + golden assertions on one output text
  python qa/qa.py report [--bug B02]             markdown summary (paste into the human checklist)
  python qa/qa.py next                           highest-priority unfixed bug class with its open findings

Add --json to check, report or next for machine-readable output. `check` exits 1 when anything fails.
Requires PyYAML (pip install pyyaml).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("qa.py needs PyYAML: pip install pyyaml")

ROOT = Path(__file__).resolve().parent
PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
BUG_STATUS = {"proposed", "open", "regressed", "fixed", "wontfix"}
FINDING_STATUS = {"reported", "verified", "fixed", "verified-fixed", "regressed",
                  "disputed", "keep", "not-a-bug", "wontfix"}
VERIFIED = {"pdf", "image", "computed", "internal", "partial", "unverified"}
REQUIRED = ["id", "run", "paper", "bug", "status", "where", "observed", "expected", "by", "verified"]
UNFIXED = {"reported", "verified", "regressed", "disputed"}


# ---------------------------------------------------------------- loading
def load_yaml(name):
    with open(ROOT / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_findings():
    out = []
    with open(ROOT / "findings.jsonl", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"findings.jsonl line {n}: invalid JSON ({e})")
            rec["_line"] = n
            out.append(rec)
    return out


def load_golden(paper):
    path = ROOT / "golden" / f"{paper}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


# ---------------------------------------------------------------- validate
def cmd_validate(_args):
    errors = []
    bugs = load_yaml("bugs.yaml")["bugs"]
    runs = load_yaml("runs.yaml")["runs"]
    papers = load_yaml("papers.yaml")["papers"]
    decisions = load_yaml("decisions.yaml")["decisions"]
    questions = load_yaml("questions.yaml")["questions"]
    findings = load_findings()

    def unique(items, label):
        seen = set()
        for it in items:
            if it["id"] in seen:
                errors.append(f"{label}: duplicate id {it['id']}")
            seen.add(it["id"])
        return seen

    bug_ids = unique(bugs, "bugs.yaml")
    run_ids = unique(runs, "runs.yaml")
    paper_ids = unique(papers, "papers.yaml")
    unique(decisions, "decisions.yaml")
    unique(questions, "questions.yaml")

    for b in bugs:
        if not re.fullmatch(r"B\d{2,}", b["id"]):
            errors.append(f"bugs.yaml {b['id']}: id must look like B01")
        if b.get("priority") not in PRIORITY:
            errors.append(f"bugs.yaml {b['id']}: priority must be P0..P3")
        if b.get("status") not in BUG_STATUS:
            errors.append(f"bugs.yaml {b['id']}: status '{b.get('status')}' not in {sorted(BUG_STATUS)}")
    for r in runs:
        for p in as_list(r.get("papers")):
            if p not in paper_ids:
                errors.append(f"runs.yaml {r['id']}: unknown paper '{p}'")

    golden_ids = {}
    for path in sorted((ROOT / "golden").glob("*.yaml")):
        g = yaml.safe_load(path.read_text(encoding="utf-8"))
        if g.get("paper") not in paper_ids:
            errors.append(f"{path.name}: unknown paper '{g.get('paper')}'")
        for a in g.get("asserts", []):
            if a["id"] in golden_ids:
                errors.append(f"{path.name}: duplicate assert id {a['id']}")
            golden_ids[a["id"]] = path.name
            if a.get("bug") is not None and a["bug"] not in bug_ids:
                errors.append(f"{path.name} {a['id']}: unknown bug {a['bug']}")
            kinds = [k for k in ("must_contain", "must_not_contain", "order") if k in a]
            if not kinds:
                errors.append(f"{path.name} {a['id']}: needs must_contain, must_not_contain or order")
            for k in kinds:
                for pat in as_list(a[k]):
                    try:
                        re.compile(pat)
                    except re.error as e:
                        errors.append(f"{path.name} {a['id']}: bad regex {pat!r} ({e})")

    seen = set()
    for f in findings:
        where = f"findings.jsonl line {f['_line']} ({f.get('id', '?')})"
        for key in REQUIRED:
            if key not in f:
                errors.append(f"{where}: missing '{key}'")
        if f.get("id") in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(f.get("id"))
        if f.get("run") not in run_ids:
            errors.append(f"{where}: unknown run '{f.get('run')}'")
        elif f.get("id") and not f["id"].startswith(f["run"] + "-"):
            errors.append(f"{where}: id should start with its run, e.g. {f['run']}-01")
        if f.get("paper") not in paper_ids:
            errors.append(f"{where}: unknown paper '{f.get('paper')}'")
        if f.get("bug") is None:
            if f.get("status") not in {"keep", "not-a-bug", "wontfix"}:
                errors.append(f"{where}: bug may be null only for keep, not-a-bug or wontfix")
        elif f["bug"] not in bug_ids:
            errors.append(f"{where}: unknown bug '{f['bug']}'")
        if f.get("status") not in FINDING_STATUS:
            errors.append(f"{where}: status '{f.get('status')}' not in {sorted(FINDING_STATUS)}")
        if f.get("verified") not in VERIFIED:
            errors.append(f"{where}: verified '{f.get('verified')}' not in {sorted(VERIFIED)}")
        if not isinstance(f.get("by"), list) or not f.get("by"):
            errors.append(f"{where}: 'by' must be a non-empty list")
        for aid in as_list(f.get("assert")):
            if aid not in golden_ids:
                errors.append(f"{where}: unknown golden assert '{aid}'")

    if errors:
        print("\n".join(errors))
        print(f"\n{len(errors)} problem(s).")
        return 1
    print(f"OK: {len(bugs)} bug classes, {len(findings)} findings, {len(runs)} runs, "
          f"{len(golden_ids)} golden assertions.")
    return 0


# ---------------------------------------------------------------- generic checks
LABEL = re.compile(r"^(Figure|Table)\s+(\d+)\b")
DESC = re.compile(r"^\s*description\s*$", re.I)
TERMINAL = tuple('.!?:;)"”’\'»]')


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def generic_checks(text):
    """Paper-independent structural checks. Each returns (id, bug, about, hits[(line, snippet)])."""
    lines = text.splitlines()
    results = []

    def regex_check(cid, bug, about, pattern, flags=re.M):
        hits = [(line_of(text, m.start()), m.group(0).strip()[:80]) for m in re.finditer(pattern, text, flags)]
        results.append((cid, bug, about, hits))

    regex_check("G01", "B04", 'Bare "Table" or "Figure" label with no number or caption', r"^(Table|Figure)\s*$")
    regex_check("G02", "B05", 'Caption in "Table N |" format leaked as text', r"^(Table|Figure) \d+ \|.*$")
    regex_check("G03", "B17", 'Heading with a "Part" prefix',
                r"^Part (One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|[A-H])\b.*$")
    regex_check("G04", "B23", "Markup leaked into text", r"<sup>|</sup>|<br>|\*\*", 0)
    regex_check("G05", "B20", "Ligature character (needs NFKC)", "[ﬀ-ﬆĲĳ]", 0)

    # G06: body paragraph (12+ words) that ends without final punctuation
    hits = []
    nonempty = [(n, l.strip()) for n, l in enumerate(lines, 1) if l.strip()]
    for i, (n, s) in enumerate(nonempty):
        if "\t" in s or " · " in s:  # raw table rows (G04 covers them) and the app's header line
            continue
        if re.match(r"(Part|Appendix)\b", s):  # headings; G03 covers the "Part" prefix
            continue
        following = [t for _, t in nonempty[i + 1:i + 3]]
        if following and DESC.match(following[0]):
            following = following[1:]
        if following and following[0].startswith("Equation"):  # sentence that leads into an equation
            continue
        if len(s.split()) >= 12 and not s.endswith(TERMINAL):
            hits.append((n, "…" + s[-60:]))
    results.append(("G06", "B06", "Paragraph ends mid-sentence (split, cut caption or lost text)", hits))

    # G07: description label vs the next caption label inside the same description block
    hits = []
    blocks, current = [], None
    for n, line in enumerate(lines, 1):
        if DESC.match(line):
            current = []
            blocks.append(current)
        elif current is not None and line.strip():
            current.append((n, line.strip()))
    for block in blocks:
        labeled = [(n, LABEL.match(s)) for n, s in block]
        labeled = [(n, m) for n, m in labeled if m]
        if not block or not labeled or labeled[0][0] != block[0][0]:
            continue  # description has no number of its own
        first = labeled[0][1]
        for n, m in labeled[1:]:
            if (m.group(1), m.group(2)) != (first.group(1), first.group(2)):
                hits.append((block[0][0], f"description says {first.group(0)}, caption at line {n} says {m.group(0)}"))
            break
    results.append(("G07", "B03", "Description labeled with a different number than its caption", hits))
    return results


# ---------------------------------------------------------------- golden
def golden_checks(text, golden):
    results = []
    for a in golden.get("asserts", []):
        problems = []
        for pat in as_list(a.get("must_contain")):
            if not re.search(pat, text):
                problems.append(f"missing: {pat}")
        for pat in as_list(a.get("must_not_contain")):
            m = re.search(pat, text)
            if m:
                problems.append(f"found at line {line_of(text, m.start())}: {m.group(0).strip()[:80]!r}")
        if "order" in a:
            positions = []
            for pat in a["order"]:
                m = re.search(pat, text)
                positions.append(m.start() if m else None)
            if None in positions:
                missing = [p for p, pos in zip(a["order"], positions) if pos is None]
                problems.append(f"order: not found: {missing}")
            elif positions != sorted(positions):
                problems.append("order: found out of order at lines "
                                + ", ".join(str(line_of(text, p)) for p in positions))
        results.append((a["id"], a.get("bug"), a.get("about", ""), problems))
    return results


def cmd_check(args):
    path = Path(args.output)
    text = path.read_text(encoding="utf-8")
    paper = args.paper
    if paper is None:  # infer from runs.yaml output_file
        for r in load_yaml("runs.yaml")["runs"]:
            of = r.get("output_file")
            if of and (ROOT / of).resolve() == path.resolve():
                paper = as_list(r["papers"])[0]
    golden = load_golden(paper) if paper else None

    generic = generic_checks(text)
    gold = golden_checks(text, golden) if golden else []
    fails = sum(1 for g in generic if g[3]) + sum(1 for g in gold if g[3])

    if args.json:
        print(json.dumps({
            "output": str(path), "paper": paper,
            "generic": [{"id": i, "bug": b, "about": a, "pass": not h,
                         "hits": [{"line": n, "text": s} for n, s in h[:10]], "count": len(h)}
                        for i, b, a, h in generic],
            "golden": [{"id": i, "bug": b, "about": a, "pass": not p, "problems": p} for i, b, a, p in gold],
            "failed": fails,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"{path.name}  paper={paper or '?'}" + ("" if golden else "  (no golden file)"))
        print("\nGeneric checks")
        for i, b, a, h in generic:
            print(f"  {'PASS' if not h else 'FAIL'} {i} [{b}] {a}" + (f"  ({len(h)})" if h else ""))
            for n, s in h[:5]:
                print(f"       line {n}: {s}")
        if gold:
            print("\nGolden assertions")
            for i, b, a, p in gold:
                print(f"  {'PASS' if not p else 'FAIL'} {i} [{b or 'keep'}] {a}")
                for msg in p:
                    print(f"       {msg}")
            for m in golden.get("manual", []):
                print(f"  TODO manual [{m.get('bug')}] {m['check']}")
        total = len(generic) + len(gold)
        print(f"\n{total - fails}/{total} passed.")
    return 1 if fails else 0


# ---------------------------------------------------------------- report / next
def summary():
    bugs = load_yaml("bugs.yaml")["bugs"]
    findings = load_findings()
    rows = []
    for b in bugs:
        mine = [f for f in findings if f.get("bug") == b["id"]]
        rows.append({
            "id": b["id"], "title": b["title"], "priority": b["priority"], "status": b["status"],
            "layer": b.get("layer"), "fix": b.get("fix", "").strip(), "test": b.get("test", ""),
            "papers": sorted({f["paper"] for f in mine}),
            "runs": sorted({f["run"] for f in mine}),
            "open": [f for f in mine if f["status"] in UNFIXED],
        })
    rows.sort(key=lambda r: (r["status"] in {"fixed", "wontfix"}, PRIORITY[r["priority"]], r["id"]))
    return rows


def strip(f):
    return {k: v for k, v in f.items() if not k.startswith("_")}


def cmd_report(args):
    rows = summary()
    if args.bug:
        rows = [r for r in rows if r["id"] == args.bug]
    if args.json:
        print(json.dumps([{**r, "open": [strip(f) for f in r["open"]]} for r in rows], ensure_ascii=False, indent=2))
        return 0
    print("| Bug | Title | Priority | Status | Seen in | Open findings |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['id']} | {r['title']} | {r['priority']} | {r['status']} | "
              f"{', '.join(r['papers']) or '-'} | {len(r['open'])} |")
    if args.bug:
        for r in rows:
            print(f"\nFix: {r['fix']}\nTest: {r['test']}")
            for f in r["open"]:
                print(f"- {f['id']} ({f['paper']}, {f['where']}): {f['observed']} → {f['expected']}")
    return 0


def cmd_next(args):
    rows = [r for r in summary() if r["status"] in {"open", "regressed", "proposed"} and r["open"]]
    if not rows:
        print("Nothing open.")
        return 0
    r = rows[0]
    if args.json:
        print(json.dumps({**r, "open": [strip(f) for f in r["open"]]}, ensure_ascii=False, indent=2))
        return 0
    print(f"{r['id']} [{r['priority']}, {r['status']}, layer {r['layer']}] {r['title']}")
    print(f"Fix:  {r['fix']}\nTest: {r['test']}\nOpen findings ({len(r['open'])}):")
    for f in r["open"]:
        print(f"  {f['id']} {f['paper']} | {f['where']}\n     observed: {f['observed']}\n     expected: {f['expected']}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    c = sub.add_parser("check")
    c.add_argument("output")
    c.add_argument("--paper")
    c.add_argument("--json", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--bug")
    r.add_argument("--json", action="store_true")
    n = sub.add_parser("next")
    n.add_argument("--json", action="store_true")
    args = p.parse_args()
    return {"validate": cmd_validate, "check": cmd_check, "report": cmd_report, "next": cmd_next}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
