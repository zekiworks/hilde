#!/usr/bin/env python3
"""Hilde QA tool. Read AGENTS.md first.

  python qa/qa.py validate                       check every QA file for schema and broken references
  python qa/qa.py check TARGET [--paper ID]      generic checks + golden assertions on one book
  python qa/qa.py report [--bug B02]             markdown summary (paste into the human checklist)
  python qa/qa.py next                           highest-priority unfixed bug class with its open findings
  python qa/qa.py facts RUN... [--baseline RUN...]   score fact sets (facts/<paper>.yaml) over runs; describe.py makes runs

TARGET is a book folder (Audiobooks/<slug>--<hash12>/), its narration.json, or a plain text file.
For a book, the checked text is exactly what every voice reads: the non-empty passage texts joined by
blank lines. Passage types and sources then enable typed checks (G06-G07 typed, G09, G10, G11, G12, G13), and the
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

    fact_ids = {}
    for path in sorted((ROOT / "facts").glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        if spec.get("paper") not in paper_ids:
            errors.append(f"facts/{path.name}: unknown paper '{spec.get('paper')}'")
        for visual in spec.get("visuals") or []:
            where = f"facts/{path.name} {visual.get('name')}"
            if not (visual.get("caption") or visual.get("find") or visual.get("scope") == "book"
                    or str(visual.get("name", "")).startswith("Equation")):
                errors.append(f"{where}: needs a caption, find, scope: book, or an Equation name")
            for key in ("holders", "regions"):
                for pat in (visual.get(key) or {}).values():
                    try:
                        re.compile(pat)
                    except re.error as e:
                        errors.append(f"{where}: bad {key} regex {pat!r} ({e})")
            for fact in visual.get("facts") or []:
                fid = fact.get("id")
                if fid in fact_ids:
                    errors.append(f"{where}: duplicate fact id {fid} (also in {fact_ids[fid]})")
                fact_ids[fid] = path.name
                for key in ("id", "what", "severity", "source", "verified"):
                    if not fact.get(key):
                        errors.append(f"{where} {fid}: missing '{key}'")
                unknown = set(fact) - FACT_KEYS
                if unknown:
                    errors.append(f"{where} {fid}: unknown keys {sorted(map(str, unknown))} (an unquoted ': ' in YAML?)")
                if fact.get("severity") not in FACT_SEVERITY:
                    errors.append(f"{where} {fid}: severity must be one of {sorted(FACT_SEVERITY)}")
                if "value" in fact and not fact.get("holder"):
                    errors.append(f"{where} {fid}: a value needs a holder")
                for holder in as_list(fact.get("holder")):
                    if holder not in (visual.get("holders") or {}):
                        errors.append(f"{where} {fid}: unknown holder '{holder}'")
                if fact.get("region") and fact["region"] not in (visual.get("regions") or {}):
                    errors.append(f"{where} {fid}: unknown region '{fact['region']}'")
                if not any(key in fact for key in ("value", "says", "not")):
                    errors.append(f"{where} {fid}: needs value, says or not")
                for pat in as_list(fact.get("value")) + as_list(fact.get("says")) + as_list(fact.get("not")):
                    try:
                        re.compile(str(pat))
                    except re.error as e:
                        errors.append(f"{where} {fid}: bad regex {pat!r} ({e})")

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
          f"{len(golden_ids)} golden assertions, {len(fact_ids)} facts.")
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
    regex_check("G04", "B23", "Markup leaked into text", r"<sup>|</sup>|<sub>|</sub>|<br>|\*\*", 0)
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

    # G12: an equation passage names an equation number other than one printed beside
    # its equations, "(2)" at the end of the picture text, or any number when none is.
    hits = []
    words = {w: str(n) for n, w in enumerate("one two three four five six seven eight nine ten".split(), 1)}
    number = r"(?:\d+|" + "|".join(words) + r")\b"
    for p in t.passages:
        if p.get("type") != "equation":
            continue
        printed = set()
        for s in p.get("sources", []):
            for block in re.findall(r"<!-- Start of picture text -->(.*?)<!-- End of picture text -->",
                                    s.get("text", ""), re.S):
                m = re.search(r"\((\d+)[a-z]?\)\s*$", re.sub(r"<!--.*?-->", "", block).strip())
                if m:
                    printed.add(m.group(1))
        for m in re.finditer(rf"\b(?:Equations?|Eq\.)\s*\(?({number}(?:\)?\s*(?:,|and|&|to|–|-)\s*\(?{number})*)",
                             p.get("text", "")):
            said = {words.get(n.lower(), n) for n in re.findall(number, m.group(1), re.I)}
            if not said or not said <= printed:
                want = f"Equation {', '.join(sorted(printed))}" if printed else "an unnumbered equation"
                hits.append((f"passage {p.get('id')} (p. {p.get('page')})", f"says {m.group(0)!r}, source prints {want}"))
    results.append(("G12", "B03", "Equation passage names a number other than the one printed beside it", hits))

    # G13: a passage Hilde kept and marked: a check found it stating what its source
    # does not (an unprinted number or name, a dropped or added "not" or "all"), and
    # asked once more, the model said it again. Start the review here.
    hits = [
        (f"passage {p.get('id')} (p. {p.get('page')})", "; ".join(p["flags"]))
        for p in t.passages if p.get("flags")
    ]
    results.append(("G13", "B02", "Passage kept and marked after asking the model again", hits))

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


# ---------------------------------------------------------------- facts
# Fact sets (facts/<paper>.yaml; facts/README.md): what each figure, table, and equation
# description must state, scored right, wrong, or missing, over one run or several.
FACT_SEVERITY = {"critical", "important", "minor"}
FACT_KEYS = {"id", "what", "severity", "source", "verified", "seen", "value", "holder", "says", "not",
             "region", "open"}
DESCRIBED = {"figure", "table", "equation"}
NAME_AT_START = re.compile(r"^\s*(?:(The) (equation|figure|table)s?\b|(Figure|Table|Equation)s? (\d+))", re.I)
MARKERS = {"description", "table", "figure", "equation"}
NUMBER = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)*(?:\s?[kK]\b)?")
AFTER_HOLDER = r"^\s*(?:(?!followed\b|while\b|and\b)[A-Za-z%-]+\s+){0,2}?(?:for|by|of|in|on)\s+(?:the\s+)?"


def load_facts(paper):
    path = ROOT / "facts" / f"{paper}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None


def _marker(p):
    """The reader label a copied paragraph ends with ("Description", "Table"), or None."""
    last = p.strip().splitlines()[-1].strip().lower() if p.strip() else ""
    return last if last in MARKERS else None


def _text_description(visual, paras, caption_at, captions):
    """A visual's description in follow-along text, as (paragraphs, labelled)."""
    name = visual["name"]
    if visual.get("find"):
        for i, p in enumerate(paras):
            if _marker(p) != "description":
                continue
            block = []
            for q in paras[i + 1:]:
                if _marker(q) == "description" or any(c.search(q) for c in captions):
                    break
                if q.strip().lower() not in MARKERS:
                    block.append(q)
            if re.search(visual["find"], "\n\n".join(block), re.I):
                return block, True
        return [], False
    if name.startswith("Equation"):
        return [p for p in paras if re.match(rf"{re.escape(name)}\b", p)], False
    if visual.get("scope") == "book":
        return paras, False
    c = caption_at.get(name)
    if c is None:
        return [], False
    others = {i for n, i in caption_at.items() if n != name and i is not None}
    i = c - 1
    while i >= 0 and c - i <= 15:
        if _marker(paras[i]) == "description":
            return [p for p in paras[i + 1:c] if p.strip().lower() not in MARKERS], True
        if i in others:
            break
        i -= 1
    j = c - 1
    while j >= 0 and paras[j].strip().lower() in MARKERS:
        j -= 1
    return ([paras[j]] if j >= 0 and j not in others else []), False


def _json_description(visual, passages, captions):
    """A visual's description in narration.json, as (texts, labelled): the figure or table
    passage that first holds its caption in its sources, else the last passage carrying a
    visual before the one that does;
    every passage opening "Equation N"; or the first described passage matching `find`."""
    name = visual["name"]
    described = [p for p in passages if p.get("type") in DESCRIBED and p.get("text")]
    if visual.get("find"):
        hit = next((p for p in described if re.search(visual["find"], p["text"], re.I)), None)
        if hit is None and not visual.get("unnumbered") and re.fullmatch(r"(Figure|Table) \d+", name):
            # Reworded past `find`: the passage code named after this visual's caption.
            hit = next((p for p in described if p.get("type") in {"figure", "table"}
                        and re.match(rf"{re.escape(name)}\b", p["text"])), None)
        return ([hit["text"]], True) if hit else ([], False)
    if name.startswith("Equation"):
        # One passage may read several paragraphs; any of them may open with the name.
        return [
            paragraph for p in passages for paragraph in re.split(r"\n\s*\n", p.get("text") or "")
            if re.match(rf"{re.escape(name)}\b", paragraph)
        ], False
    if visual.get("scope") == "book":
        return [p["text"] for p in passages if p.get("text")], False

    def holds(p, pattern):
        return any(pattern.search(s.get("text") or "") for s in p.get("sources") or ())

    def carries(p):
        # A figure joined into a prose batch leaves a body passage holding the figure.
        return bool(p.get("text")) and (
            p.get("type") in DESCRIBED or any(s.get("type") in DESCRIBED for s in p.get("sources") or ())
        )

    own = re.compile(re.escape(visual["caption"]), re.I)
    first = next((i for i, p in enumerate(passages) if holds(p, own)), None)
    if first is None:
        return [], False
    if passages[first].get("type") in {"figure", "table"}:
        text = passages[first].get("text") or ""
        return ([text], True) if text else ([], False)
    # A caption extraction missed sits in a body passage; the description is the last
    # passage before it that carries a visual, of any type, since a figure missing its
    # caption may be typed an equation, or joined into prose.
    others = [c for c in captions if c.pattern != own.pattern]
    for i in range(first - 1, -1, -1):
        p = passages[i]
        if carries(p):
            return [p["text"]], True
        if any(holds(p, c) for c in others):
            break
    return [], False


def _holder_spans(text, holders):
    spans = []
    for name, pattern in (holders or {}).items():
        for m in re.finditer(pattern, text):
            spans.append((m.start(), m.end(), name))
    # Longer, later matches win where two overlap ("CLM with subagents").
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept = []
    for s in spans:
        if kept and s[0] < kept[-1][1]:
            if s[1] - s[0] > kept[-1][1] - kept[-1][0]:
                kept[-1] = s
            continue
        kept.append(s)
    return kept


def _sentence_start(text, pos):
    cut = 0
    for m in re.finditer(r"[.;!?](?=\s)|\n", text[:pos]):
        cut = m.end()
    return cut


def _attributions(text, holders):
    """Each number's span -> the holder it is stated for, or None (README, rule 2)."""
    spans = _holder_spans(text, holders)
    claimed, explicit = set(), {}
    numbers = list(NUMBER.finditer(text))
    for m in numbers:  # "47.3 for CLM", "7.8 percent reused by SCR"
        for s in spans:
            if s[0] >= m.end() and s[0] - m.end() <= 45:
                between = text[m.end():s[0]]
                if re.fullmatch(AFTER_HOLDER, between, re.I) and not NUMBER.search(between):
                    explicit[m.span()] = s[2]
                    claimed.add(s)
                break
    result = {}
    for m in numbers:
        if m.span() in explicit:
            result[m.span()] = explicit[m.span()]
        elif re.search(r"\bfrom\s+$", text[max(0, m.start() - 6):m.start()], re.I):
            result[m.span()] = None  # "from A to B": A's owner is not said
        else:
            start = _sentence_start(text, m.start())
            prior = [s for s in spans if start <= s[0] and s[1] <= m.start() and s not in claimed]
            result[m.span()] = prior[-1][2] if prior else None
    return result


def _region_of(text, pos, regions):
    best, where = None, -1
    for name, pattern in (regions or {}).items():
        for m in re.finditer(pattern, text[:pos]):
            if m.start() > where:
                best, where = name, m.start()
    return best


def score_fact(fact, visual, texts):
    """(status, evidence) for one fact over a description's texts (README, Scoring one fact)."""
    nots, says = as_list(fact.get("not")), as_list(fact.get("says"))
    for t in texts:
        for pattern in nots:
            m = re.search(pattern, t)
            if m:
                return "wrong", m.group(0)
    if "value" in fact:
        want = as_list(fact["holder"])
        value = re.compile(rf"(?<![\d.,])(?:{fact['value']})(?![\d]|[.,]\d)")
        right = wrong = None
        for t in texts:
            owners = _attributions(t, visual.get("holders"))
            for m in value.finditer(t):
                if fact.get("region") and _region_of(t, m.start(), visual.get("regions")) != fact["region"]:
                    continue
                span = next((s for s in owners if s[0] <= m.start() < s[1]), None)
                owner = owners.get(span) if span else None
                snippet = t[max(0, m.start() - 70):m.end() + 10].replace("\n", " ")
                if owner is None or owner in want:
                    right = right or snippet
                else:
                    wrong = wrong or f"{snippet}  [{owner}]"
        if wrong:
            return "wrong", wrong
        if right:
            return "right", right
        if not says:
            return "missing", ""
    for t in texts:
        for pattern in says:
            m = re.search(pattern, t)
            if m:
                return "right", m.group(0)
    return ("missing", "") if says else ("right", "")


def score_facts(t: Target, spec):
    """Every fact of a fact set scored on one target, and each labelled description's opening name.
    A descriptions-only run (book.json) has no adapted prose, so book-wide facts are skipped."""
    descriptions_only = bool(t.book and t.book.get("descriptions_only"))
    captions = [re.compile(re.escape(v["caption"]), re.I) for v in spec["visuals"] if v.get("caption")]
    if t.passages is None:
        paras = [p.strip() for p in re.split(r"\n\s*\n", t.text) if p.strip()]
        caption_at = {
            v["name"]: next((i for i, p in enumerate(paras) if re.search(re.escape(v["caption"]), p, re.I)), None)
            for v in spec["visuals"] if v.get("caption")
        }
    facts, labels = [], []
    for visual in spec["visuals"]:
        skipped = descriptions_only and visual.get("scope") == "book"
        if t.passages is None:
            texts, labelled = _text_description(visual, paras, caption_at, captions)
            texts = ["\n\n".join(texts)] if texts else []
        else:
            texts, labelled = _json_description(visual, t.passages, captions)
        # An equation found by its content (`find`) is checked too: one Hilde names
        # "The equation…" though the paper numbers it lost its number (R23-01).
        if labelled and texts:
            m = NAME_AT_START.match(texts[0])
            said = (f"The {m.group(2).lower()}" if m.group(1) else f"{m.group(3).capitalize()} {m.group(4)}") if m else None
            if visual.get("unnumbered"):  # the paper gives it no number; any "Figure N" is invented
                labels.append((visual["name"], "wrong" if said and said[-1].isdigit() else "right", said or "(no name)"))
            elif said:
                labels.append((visual["name"], "right" if said == visual["name"] else "wrong", said))
        for fact in visual.get("facts") or []:
            if skipped:
                status, evidence = "skipped", "descriptions-only run"
            elif texts:
                status, evidence = score_fact(fact, visual, texts)
            else:
                status, evidence = "missing", "no description found"
            facts.append({"id": fact["id"], "in": visual["name"], "what": fact["what"],
                          "severity": fact.get("severity"), "open": fact.get("open") or visual.get("open"),
                          "status": status, "evidence": evidence})
    return facts, labels


def cmd_facts(args):
    targets = [Target(path) for path in args.outputs]
    baseline = [Target(path) for path in args.baseline or ()]
    papers = {args.paper or t.paper() for t in targets + baseline}
    if len(papers) != 1 or None in papers:
        raise SystemExit(f"one paper per call, found {sorted(map(str, papers))}; name it with --paper")
    paper = papers.pop()
    spec = load_facts(paper)
    if spec is None:
        raise SystemExit(f"no fact set for {paper} (qa/facts/{paper}.yaml)")

    def score_all(group):
        return [(t, *score_facts(t, spec)) for t in group]

    runs, base = score_all(targets), score_all(baseline)
    counted = lambda facts: [f for f in facts if not f["open"] and f["status"] != "skipped"]

    def statuses(scored):
        table = {}
        for _, facts, _ in scored:
            for f in counted(facts):
                table.setdefault(f["id"], []).append(f["status"])
        return table

    now, before = statuses(runs), statuses(base)
    stable = {i for i, s in now.items() if all(x == "right" for x in s)}
    was_stable = {i for i, s in before.items() if all(x == "right" for x in s)}
    regressions = sorted(was_stable - stable) if base else []
    gains = sorted(stable - was_stable) if base else []
    about = {f["id"]: f for _, facts, _ in runs for f in facts}

    if args.json:
        print(json.dumps({
            "paper": paper,
            "runs": [{"target": str(t.path), "commit": (t.book or {}).get("git_commit"),
                      "facts": facts, "labels": [dict(zip(("name", "status", "said"), l)) for l in labels]}
                     for t, facts, labels in runs],
            "stable": sorted(stable), "unstable": sorted(set(now) - stable),
            "baseline": [str(t.path) for t, _, _ in base], "regressions": regressions, "gains": gains,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"Fact set {paper}: {len(now)} facts scored over {len(runs)} run{'s' if len(runs) != 1 else ''}")
        for t, facts, labels in runs:
            c = counted(facts)
            totals = {s: sum(f["status"] == s for f in c) for s in ("right", "wrong", "missing")}
            commit = str((t.book or {}).get("git_commit") or "")[:12]
            print(f"  {t.path}" + (f" @ {commit}" if commit else "")
                  + f": right {totals['right']}  wrong {totals['wrong']}  missing {totals['missing']}"
                  + f"  labels {sum(l[1] == 'right' for l in labels)}/{len(labels)}")
            for name, status, said in labels:
                if status != "right":
                    print(f"      LABEL {name} says {said}")
        by = {s: sum(1 for i in stable if about[i]["severity"] == s) for s in ("critical", "important", "minor")}
        print(f"Stable (right in every run): {len(stable)}/{len(now)}"
              f"  (critical {by['critical']}, important {by['important']}, minor {by['minor']})")
        letter = {"right": "R", "wrong": "W", "missing": "M"}
        for i in sorted(set(now) - stable, key=lambda i: (about[i]["severity"] != "critical", i)):
            f = about[i]
            print(f"  {' '.join(letter[s] for s in now[i])}  {i:9} [{f['severity']}] {f['in']}: {f['what'][:80]}")
            evidence = next((r[1] for r in [(None, x["evidence"]) for _, facts, _ in runs for x in facts if x["id"] == i]
                             if r[1]), "")
            if evidence:
                print(f"        ↳ {evidence[:150]}")
        if base:
            print(f"\nAgainst the baseline ({len(base)} run{'s' if len(base) != 1 else ''}): "
                  f"{len(gains)} gained, {len(regressions)} regressed")
            for i in regressions:
                print(f"  REGRESSED {i:9} [{about[i]['severity']}] {about[i]['in']}: {about[i]['what'][:80]}")
            for i in gains:
                print(f"  GAINED    {i:9} [{about[i]['severity']}] {about[i]['in']}: {about[i]['what'][:80]}")
        skipped = sum(f["status"] == "skipped" for _, facts, _ in runs[:1] for f in facts)
        waiting = sum(1 for _, facts, _ in runs[:1] for f in facts if f["open"])
        if skipped or waiting:
            print(f"\nNot scored: {skipped} book-wide facts (descriptions-only runs), {waiting} waiting on an open question.")
    return 1 if regressions or set(now) - stable else 0


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
    f = sub.add_parser("facts")
    f.add_argument("outputs", nargs="+", help="runs to score: book folders, narration.json, or text files")
    f.add_argument("--baseline", nargs="+", help="runs of the code before the change")
    f.add_argument("--paper")
    f.add_argument("--json", action="store_true")
    args = p.parse_args()
    return {"validate": cmd_validate, "check": cmd_check, "report": cmd_report, "next": cmd_next,
            "facts": cmd_facts}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
