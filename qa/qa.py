#!/usr/bin/env python3
"""Hilde QA tool. Read AGENTS.md first.

  python qa/qa.py validate                       check every QA file for schema and broken references
  python qa/qa.py check TARGET [--paper ID]      generic checks + golden assertions on one book
  python qa/qa.py report [--bug B02]             markdown summary (paste into the human checklist)
  python qa/qa.py next                           highest-priority unfixed bug class with its open findings

TARGET is a book folder (Audiobooks/<slug>--<hash12>/), its narration.json, or a plain text file.
For a book, the checked text is exactly what every voice reads: the non-empty passage texts joined by
blank lines. Passage types and sources then enable typed checks (G06-G07 typed, G09, G10, G11), and the
paper is inferred from book.json's source_sha256 (papers.yaml `sha256`).

Add --json to check, report or next for machine-readable output. `check` exits 1 when anything fails.
Requires PyYAML (pip install pyyaml).
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
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


class Target:
    """What `check` looks at: a book folder, a narration.json, or a plain text file."""

    def __init__(self, path):
        self.path = Path(path)
        self.folder = None
        self.book = None
        self.passages = None
        self.narration_bytes = None
        narration = None
        if self.path.is_dir():
            self.folder = self.path
            narration = self.path / "narration.json"
            if not narration.exists():
                raise SystemExit(f"{self.path}: no narration.json in this folder")
        elif self.path.suffix == ".json":
            narration = self.path
            if self.path.name == "narration.json":
                self.folder = self.path.parent
        if narration is None:
            self.text = self.path.read_text(encoding="utf-8")
            self.starts = None
            return
        self.narration_bytes = narration.read_bytes()
        data = json.loads(self.narration_bytes)
        self.passages = data["passages"] if isinstance(data, dict) else data
        if self.folder and (self.folder / "book.json").exists():
            self.book = json.loads((self.folder / "book.json").read_text(encoding="utf-8"))
        # Exactly what every voice reads (Hilde's narration_text).
        spoken = [p for p in self.passages if p.get("text")]
        self.text, self.starts, self.spoken, pos = "", [], spoken, 0
        parts = []
        for p in spoken:
            self.starts.append(pos)
            parts.append(p["text"])
            pos += len(p["text"]) + 2
        self.text = "\n\n".join(parts)

    def where(self, pos):
        """Human location of a character offset: a line, or a passage with its page."""
        if self.starts is None:
            return f"line {self.text.count(chr(10), 0, pos) + 1}"
        p = self.spoken[bisect.bisect_right(self.starts, pos) - 1]
        return f"passage {p.get('id')} (p. {p.get('page')})"

    def paper(self):
        hashes = []
        if self.book and self.book.get("source_sha256"):
            hashes.append(self.book["source_sha256"])
        if self.folder:
            m = re.search(r"--([0-9a-f]{12})$", self.folder.name)
            if m:
                hashes.append(m.group(1))
        for h in hashes:
            for paper in load_yaml("papers.yaml")["papers"]:
                for s in as_list(paper.get("sha256")):
                    if h.startswith(str(s)) or str(s).startswith(h):
                        return paper["id"]
        for r in load_yaml("runs.yaml")["runs"]:  # legacy: text files listed in runs.yaml
            of = r.get("output_file")
            if of and (ROOT / of).resolve() == self.path.resolve():
                return as_list(r["papers"])[0]
        return None


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
    for p in papers:
        for s in as_list(p.get("sha256")):
            if not re.fullmatch(r"[0-9a-f]{12,64}", str(s)):
                errors.append(f"papers.yaml {p['id']}: sha256 '{s}' must be 12 to 64 lowercase hex characters")
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
CAPTION_LABEL = re.compile(r"^\W*(Figure|Table)\s+(\d+)\b")
DESC = re.compile(r"^\s*description\s*$", re.I)
TERMINAL = tuple('.!?:;)"”’\'»]')
STOP = set("""about above after again against also among because been before being below between both
could does doing down during each from further have having here into itself just more most must once only
other ours over same should some such than that their them then there these they this those through under
until very were what when where which while with within would your""".split())


def content_words(s):
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)|<[^>]+>|[_*>#`]", " ", s)
    return {w for w in re.findall(r"[a-z][a-z\-]{3,}", s.lower()) if w not in STOP}


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def generic_checks(t: Target):
    """Paper-independent checks. Each returns (id, bug, about, hits[(where, snippet)])."""
    text = t.text
    results = []

    def regex_check(cid, bug, about, pattern, flags=re.M):
        hits = [(t.where(m.start()), m.group(0).strip()[:80]) for m in re.finditer(pattern, text, flags)]
        results.append((cid, bug, about, hits))

    regex_check("G01", "B04", 'Bare "Table" or "Figure" label with no number or caption', r"^(Table|Figure)\s*$")
    regex_check("G02", "B05", 'Caption in "Table N |" format leaked as text', r"^(Table|Figure) \d+ \|.*$")
    regex_check("G03", "B17", 'Heading with a "Part" prefix',
                r"^Part (One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|[A-H])\b.*$")
    regex_check("G04", "B23", "Markup leaked into text", r"<sup>|</sup>|<br>|\*\*", 0)
    regex_check("G05", "B20", "Ligature character (needs NFKC)", "[ﬀ-ﬆĲĳ]", 0)

    # G06: passage or paragraph (12+ words) that ends without final punctuation
    hits = []
    if t.passages is not None:
        spoken = [p for p in t.passages if p.get("text")]
        for i, p in enumerate(spoken):
            s = p["text"].strip()
            if p.get("type") == "heading":
                continue
            nxt = spoken[i + 1].get("type") if i + 1 < len(spoken) else None
            if nxt == "equation":  # sentence that leads into an equation
                continue
            if len(s.split()) >= 12 and not s.endswith(TERMINAL):
                hits.append((f"passage {p.get('id')} (p. {p.get('page')})", "…" + s[-60:]))
    else:
        nonempty = [(n, l.strip()) for n, l in enumerate(text.splitlines(), 1) if l.strip()]
        for i, (n, s) in enumerate(nonempty):
            if "\t" in s or " · " in s:  # raw table rows (G04 covers them) and the app's header line
                continue
            if re.match(r"(Part|Appendix)\b", s):  # headings; G03 covers the "Part" prefix
                continue
            following = [x for _, x in nonempty[i + 1:i + 3]]
            if following and DESC.match(following[0]):
                following = following[1:]
            if following and following[0].startswith("Equation"):
                continue
            if len(s.split()) >= 12 and not s.endswith(TERMINAL):
                hits.append((f"line {n}", "…" + s[-60:]))
    results.append(("G06", "B06", "Paragraph ends mid-sentence (split, cut caption or lost text)", hits))

    # G07: a description's figure or table number differs from its caption's
    hits = []
    if t.passages is not None:
        for p in t.passages:
            if p.get("type") not in ("figure", "table"):
                continue
            caps = [CAPTION_LABEL.match(s.get("text", "")) for s in p.get("sources", []) if s.get("type") == "caption"]
            caps = [m for m in caps if m]
            if not caps:
                continue
            said = LABEL.match(p.get("text", "").strip())
            want = (caps[0].group(1), caps[0].group(2))
            if not said:
                hits.append((f"passage {p.get('id')} (p. {p.get('page')})",
                             f"description does not name {want[0]} {want[1]}"))
            elif (said.group(1), said.group(2)) != want:
                hits.append((f"passage {p.get('id')} (p. {p.get('page')})",
                             f"description says {said.group(0)}, caption says {want[0]} {want[1]}"))
    else:
        lines = text.splitlines()
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
                    hits.append((f"line {block[0][0]}",
                                 f"description says {first.group(0)}, caption at line {n} says {m.group(0)}"))
                break
    results.append(("G07", "B03", "Description labeled with a different number than its caption", hits))

    if t.passages is None:
        return results

    # G09: a footnote attached to a passage leaves little or no trace in the passage's text (heuristic)
    hits = []
    for p in t.passages:
        spoken = content_words(p.get("text", ""))
        for s in p.get("sources", []):
            if s.get("type") != "footnote":
                continue
            words = content_words(s.get("text", ""))
            if len(words) < 3:
                continue
            kept = len(words & spoken) / len(words)
            if kept < 0.6:
                note = re.sub(r"\s+", " ", re.sub(r"[_*>]", "", s["text"])).strip()
                hits.append((f"passage {p.get('id')} (p. {p.get('page')})",
                             f"{kept:.0%} of the footnote's words kept: {note[:60]}"))
    results.append(("G09", "B13", "Footnote dropped from the passage that carries it (word-overlap heuristic)", hits))

    # G11: an equation passage opens with a figure or table number. Hilde names an
    # equation from the number printed beside it; "Figure 4 shows…" is the model's.
    hits = []
    for p in t.passages:
        if p.get("type") != "equation":
            continue
        m = re.match(r"(?:Figures?|Figs?\.|Tables?)\s+\(?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
                     p.get("text", "").strip())
        if m:
            hits.append((f"passage {p.get('id')} (p. {p.get('page')})", f"equation passage opens with {m.group(0)!r}"))
    results.append(("G11", "B03", "Equation passage opens with a figure or table number", hits))

    # G10: book folder integrity (only for book folders)
    if t.book is not None:
        hits = []
        b = t.book
        actual = sha256_hex(t.narration_bytes)
        if b.get("narration_sha256") != actual:
            hits.append(("book.json", f"narration_sha256 {str(b.get('narration_sha256'))[:12]}… != narration.json {actual[:12]}…"))
        # Books moved from the layout before book folders never recorded a version stamp;
        # Hilde writes null there rather than guess (migrated_from names the source).
        stamp = () if b.get("migrated_from") else ("hilde_version", "prompt_hash")
        for key in ("source_sha256", "created_at", "model", *stamp):
            if not b.get(key):
                hits.append(("book.json", f"missing {key}"))
        m = re.search(r"--([0-9a-f]{12})$", t.folder.name) if t.folder else None
        if m and b.get("source_sha256") and not b["source_sha256"].startswith(m.group(1)):
            hits.append(("folder", f"name hash {m.group(1)} does not match source_sha256"))
        for v in b.get("voices", []):
            if v.get("status") == "ready" and v.get("narration_sha256") != b.get("narration_sha256"):
                hits.append((f"voice {v.get('name')}", "status ready but made from different narration (should be stale)"))
            vdir = t.folder / "voices" / str(v.get("name")) if t.folder else None
            if vdir and (t.folder / "voices").exists() and v.get("status") == "ready":
                for f in ("audio.mp3", "timings.json"):
                    if not (vdir / f).exists():
                        hits.append((f"voice {v.get('name')}", f"missing {f}"))
                audio = vdir / "audio.mp3"
                if audio.exists() and v.get("audio_sha256") and sha256_hex(audio.read_bytes()) != v["audio_sha256"]:
                    hits.append((f"voice {v.get('name')}", "audio.mp3 does not match audio_sha256"))
        results.append(("G10", None, "Book folder integrity (hashes, version stamp, voices)", hits))
    return results


# ---------------------------------------------------------------- golden
def golden_checks(t: Target, golden):
    text = t.text
    results = []
    for a in golden.get("asserts", []):
        problems = []
        for pat in as_list(a.get("must_contain")):
            if not re.search(pat, text):
                problems.append(f"missing: {pat}")
        for pat in as_list(a.get("must_not_contain")):
            m = re.search(pat, text)
            if m:
                problems.append(f"found at {t.where(m.start())}: {m.group(0).strip()[:80]!r}")
        if "order" in a:
            positions = []
            for pat in a["order"]:
                m = re.search(pat, text)
                positions.append(m.start() if m else None)
            if None in positions:
                missing = [p for p, pos in zip(a["order"], positions) if pos is None]
                problems.append(f"order: not found: {missing}")
            elif positions != sorted(positions):
                problems.append("order: found out of order at " + ", ".join(t.where(p) for p in positions))
        results.append((a["id"], a.get("bug"), a.get("about", ""), problems))
    return results


def cmd_check(args):
    t = Target(args.output)
    paper = args.paper or t.paper()
    golden = load_golden(paper) if paper else None

    generic = generic_checks(t)
    gold = golden_checks(t, golden) if golden else []
    fails = sum(1 for g in generic if g[3]) + sum(1 for g in gold if g[3])
    kind = "book" if t.book is not None else ("narration" if t.passages is not None else "text")

    if args.json:
        print(json.dumps({
            "target": str(t.path), "kind": kind, "paper": paper,
            "book": {k: t.book.get(k) for k in ("source_sha256", "hilde_version", "git_commit", "model",
                                                 "prompt_hash", "narration_sha256")} if t.book else None,
            "generic": [{"id": i, "bug": b, "about": a, "pass": not h,
                         "hits": [{"at": w, "text": s} for w, s in h[:10]], "count": len(h)}
                        for i, b, a, h in generic],
            "golden": [{"id": i, "bug": b, "about": a, "pass": not p, "problems": p} for i, b, a, p in gold],
            "manual": golden.get("manual", []) if golden else [],
            "failed": fails,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"{t.path.name}  ({kind})  paper={paper or '?'}" + ("" if golden else "  (no golden file)"))
        if t.book:
            print(f"Hilde {t.book.get('hilde_version')} @ {str(t.book.get('git_commit'))[:7]}, model {t.book.get('model')}")
        print("\nGeneric checks")
        for i, b, a, h in generic:
            print(f"  {'PASS' if not h else 'FAIL'} {i} [{b or '-'}] {a}" + (f"  ({len(h)})" if h else ""))
            for w, s in h[:5]:
                print(f"       {w}: {s}")
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
    c.add_argument("output", help="book folder, narration.json, or plain text file")
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
