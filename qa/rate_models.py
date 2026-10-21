#!/usr/bin/env python3
"""Rate the models Hilde can use, job by job, on Hilde's own checks, and write the ratings file the model pickers'
"Suggested for <job>" sections read (ARCHITECTURE.md, "Suggested models"). Read AGENTS.md first.

Do not run it without Can's go: it sends the test papers to every model and narrates on the GPUs.

  python qa/rate_models.py --dry-run [--catalog FILE]          # what it would run; no model or server is asked
  python qa/rate_models.py [--hilde-url URL | --catalog FILE] [--stages prepare_text,figures,check,narration]
      [--papers bert,clm] [--pdf-dir DIR] [--runs 3] [--models SELECTOR,...] [--check-text-model SELECTOR]
      [--narration-model PATH_OR_ID ...] [--voice NAME] [--device cuda:0] [--stt-url URL] [--stt-model NAME]
      [--work DIR] [--out FILE] [--machine LABEL]

Jobs (the ratings file's stages):
- prepare_text: each text model adapts every test paper (a full adaptation, as a book's text); the paper's fact set
  (facts/<paper>.yaml, scored by qa.py's rules) counts the facts dropped (missing) or changed (wrong).
- figures: each model that reads images describes the paper's figures, tables and equations from their pictures
  (descriptions only, as describe.py --interpret); only the facts of figures ("Figure N") are counted right.
- check: one text model (--check-text-model) writes the descriptions and each model checks and corrects them (as
  describe.py --verify). Hilde keeps no record of each error a check finds, so errors are counted by fact: a fact
  wrong in most of the text model's own unchecked runs and right after checking was found; a fact wrong after
  checking was missed. The score is the share of facts right after checking.
- ask: left out. Hilde has no question set for answers (questions.yaml holds open questions for humans).
- narration: each narration model (--narration-model) reads the fixed passages (narration_passages.yaml) with one
  saved voice (--voice, copied first), a speech-to-text model that already runs (--stt-url, an OpenAI-compatible
  /v1/audio/transcriptions) writes them back, and the wrong, missing or extra words are counted. Without a
  speech-to-text server the script stops before anything runs: ask Can which one to use.
- voice_design: no automatic score. Can or Ümit add voice-design models they rated by ear to the ratings file by
  hand; a run keeps those entries, and every stage it did not rate.

Each model's score is the mean of its runs (share of facts right, 0-100; narration: words right), `noise` the standard
deviation of the runs' scores, `errors` the mean count per run over all papers, `speed` as measured on this machine:
pages of the test papers per minute of work (text jobs), seconds of audio per second of work (narration).

The models are those the running Hilde server lists (GET <--hilde-url>/api/paper/models), or a saved answer of that
route (--catalog). A dry run without --catalog reads only Hilde's remembered local models (~/.hilde/local-models.json
and providers.yaml), so cloud models are not listed. Before it starts and before every run the script reads
GET <--hilde-url>/api/jobs and stops while any job is queued or running (the GPUs are shared with narration). It only
reads from the Hilde server and changes nothing on it or on any model server. Runs are kept in --work, so a stopped
rating continues where it stopped; the ratings file is written once, atomically, at the end."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date
from pathlib import Path

QA = Path(__file__).resolve().parent
REPO = QA.parent
sys.path.insert(0, str(QA))
sys.path.insert(1, str(REPO))
import yaml  # noqa: E402

import audiobook_tts_web as web  # noqa: E402
import describe  # noqa: E402
import qa  # noqa: E402

STAGES = ("prepare_text", "figures", "check", "narration")
DEFAULT_STAGES = STAGES
ACTIVE_JOB_STATUSES = {"preparing", "queued", "running"}
PASSAGES = QA / "narration_passages.yaml"
DEFAULT_WORK = Path.home() / ".cache" / "hilde-ratings"
LEFT_OUT = {
    "ask": "left out: Hilde has no question set to score answers by (qa/questions.yaml holds open questions "
           "for humans, not test questions)",
    "voice_design": "no automatic score: only voice-design models Can or Ümit rated by ear, added to the ratings "
                    "file by hand; this run keeps those entries",
}
NO_STT = ("Narration needs a speech-to-text model that already runs, to write the audio back as text, and none is "
          "configured (--stt-url or HILDE_STT_URL). Ask Can which one to use, or leave narration out with --stages.")


class Refused(SystemExit):
    """The script stops without rating: a job runs, or something it needs is missing."""


# --- the Hilde server: models and jobs ------------------------------------------------------------------------
def fetch_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def busy_jobs(payload):
    """What a /api/jobs answer says runs or waits, in a few words, or "" when nothing does."""
    busy = [f"{run.get('title') or run.get('job_id') or 'a job'} ({run.get('stage') or run.get('status') or 'running'})"
            for run in payload.get("runs") or ()]
    busy += [f"{item.get('title') or item.get('id') or 'a job'} ({item.get('status')})"
             for item in payload.get("queue") or () if item.get("status") in ACTIVE_JOB_STATUSES]
    return ", ".join(busy)


def ensure_idle(hilde_url):
    """Stop (Refused) while the Hilde server has a job queued or running, or can't say."""
    try:
        payload = fetch_json(f"{hilde_url.rstrip('/')}/api/jobs")
    except (OSError, ValueError) as exc:
        raise Refused(f"Can't tell whether a narration job runs ({hilde_url}/api/jobs: {exc}); not starting.")
    busy = busy_jobs(payload)
    if busy:
        raise Refused(f"A job runs or waits on the Hilde server: {busy}. The GPUs are shared with narration; "
                      "run this again when it is done.")


def remembered_catalog():
    """The local models Hilde remembered, read from its files only (no server is asked): a catalog-like
    {"models", "endpoints", "source"} whose models are not known to be reachable now."""
    endpoints, _, _ = web.read_providers(web.providers_path())
    remembered = web.read_remembered_models()
    models = []
    for entry in endpoints:
        saved = remembered.get(entry["server"])
        if not saved or saved["provider"] != entry["provider"]:
            continue
        for name in saved["models"]:
            images, _, _ = web.local_model_images(entry, name, remembered)
            models.append({"selector": f"{entry['provider']}/{name}", "provider": entry["provider"], "kind": "local",
                           "server": entry["server"], "images": images})
    return {"models": models, "endpoints": endpoints,
            "source": f"Hilde's remembered local models ({web.local_models_path()}), not checked reachable; "
                      "cloud models are listed only by the live catalog (--hilde-url or --catalog)"}


def load_catalog(args):
    if args.catalog:
        catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
        catalog["source"] = f"the saved catalog {args.catalog}"
    elif args.dry_run:
        catalog = remembered_catalog()
    else:
        catalog = fetch_json(f"{args.hilde_url.rstrip('/')}/api/paper/models", timeout=120)
        catalog["source"] = f"the live catalog of {args.hilde_url}"
    if args.models:
        wanted = set(args.models)
        catalog["models"] = [model for model in catalog["models"] if model["selector"] in wanted]
    return catalog


def local_servers(catalog):
    """The catalog's local servers for PaperRun, each with what the catalog knows of its models' images (so the
    runs send figures to exactly the models the pickers list as reading images)."""
    servers = []
    for entry in catalog.get("endpoints") or ():
        known = {model["selector"].partition("/")[2]: model["images"] for model in catalog["models"]
                 if model.get("server") == entry["server"] and isinstance(model.get("images"), bool)}
        servers.append({"name": entry.get("name") or entry["server"], "server": entry["server"],
                        "provider": entry["provider"], "reads_images": known})
    return servers


# --- papers -----------------------------------------------------------------------------------------------------
def fact_papers():
    return sorted(path.stem for path in (QA / "facts").glob("*.yaml"))


def find_pdfs(papers, folder):
    """{paper: PDF path} for each paper whose PDF (by its sha256 in papers.yaml) is in `folder`; read only."""
    hashes = {entry["id"]: [str(value) for value in qa.as_list(entry.get("sha256"))]
              for entry in qa.load_yaml("papers.yaml")["papers"]}
    found = {}
    for path in sorted(Path(folder).glob("*.pdf")) if Path(folder).is_dir() else ():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for paper in papers:
            if paper not in found and any(digest.startswith(value) for value in hashes.get(paper, ())):
                found[paper] = path
    return found


def test_copy(pdf, work, paper):
    """The paper's test copy in the work folder; Hilde's own document is never opened for writing."""
    copy = work / "papers" / f"{paper}.pdf"
    if not copy.is_file():
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, copy)
    return copy


# --- text jobs --------------------------------------------------------------------------------------------------
def slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")


def run_paper(pdf, folder, model, servers, descriptions_only, interpret=False, verify_model=""):
    """One run of Hilde's pipeline on one paper into `folder` (as describe.py's): narration.json and book.json for
    qa.py, and how long it took and how many pages the paper has."""
    scratch, prepared = folder / "scratch", folder / "prepared.txt"
    folder.mkdir(parents=True, exist_ok=True)
    options = {"interpret_figures": True, "figure_model": model} if interpret else {}
    if verify_model:
        options.update(verify=True, verify_model=verify_model)
    run = web.PaperRun(pdf, prepared, "utf-8", model, servers, in_flight=3,
                       descriptions_only=descriptions_only, scratch_path=scratch, **options)
    with (folder / "log.txt").open("a", encoding="utf-8") as log:
        run.publish = lambda event, data: event == "log" and (log.write(str(data)), log.flush())
        started = time.monotonic()
        run.prepare_document(scratch)
        seconds = time.monotonic() - started
    data = web.narration_bytes(describe.narration_record(web, scratch, prepared))
    (folder / "narration.json").write_bytes(data)
    (folder / "book.json").write_text(json.dumps({
        "descriptions_only": descriptions_only, "source_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "model": model, "verification": {"model": verify_model, **run.verification} if verify_model else None,
        "figure_model": model if interpret else None, "git_commit": describe.commit_of(REPO),
    }, indent=2) + "\n", encoding="utf-8")
    pages = len((web.read_json_file(scratch / "document-pages.json") or {}).get("pages") or ())
    return seconds, pages


def fact_statuses(folder, paper, figures_only=False):
    """{fact id: right | wrong | missing} of one run, by qa.py's rules (open and book-wide-skipped facts left out)."""
    spec = qa.load_facts(paper)
    facts, _ = qa.score_facts(qa.Target(folder), spec)
    return {fact["id"]: fact["status"] for fact in facts
            if not fact["open"] and fact["status"] != "skipped"
            and (not figures_only or str(fact["in"]).startswith("Figure"))}


def text_run(stage, model, paper, number, pdf, work, servers, check_text_model=""):
    """One rated run, done once and kept (result.json): {"statuses", "seconds", "pages"}."""
    owner = check_text_model if stage == "check-baseline" else model
    folder = work / stage / slug(owner) / paper / f"run-{number}"
    result = folder / "result.json"
    if result.is_file():
        return json.loads(result.read_text(encoding="utf-8"))
    if folder.exists():
        shutil.rmtree(folder)
    if stage == "prepare_text":
        seconds, pages = run_paper(pdf, folder, model, servers, descriptions_only=False)
    elif stage == "figures":
        seconds, pages = run_paper(pdf, folder, model, servers, descriptions_only=True, interpret=True)
    elif stage == "check-baseline":
        seconds, pages = run_paper(pdf, folder, check_text_model, servers, descriptions_only=True)
    else:
        seconds, pages = run_paper(pdf, folder, check_text_model, servers, descriptions_only=True, verify_model=model)
    outcome = {"statuses": fact_statuses(folder, paper, figures_only=stage == "figures"),
               "seconds": seconds, "pages": pages}
    result.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    return outcome


def baseline_wrong(baseline_runs):
    """Facts wrong in most of the text model's own unchecked runs of one paper."""
    counts = {}
    for outcome in baseline_runs:
        for fact, status in outcome["statuses"].items():
            counts.setdefault(fact, []).append(status == "wrong")
    return {fact for fact, wrong in counts.items() if sum(wrong) * 2 > len(wrong)}


def run_totals(outcomes, wrong_before=None):
    """One run's totals over every paper ({paper: outcome}): right, counted, errors, seconds, pages, and for the
    check, errors found ({paper: facts wrong before checking})."""
    totals = {"right": 0, "counted": 0, "errors": 0, "found": 0, "seconds": 0.0, "pages": 0}
    for paper, outcome in outcomes.items():
        statuses = outcome["statuses"].values()
        totals["right"] += sum(status == "right" for status in statuses)
        totals["counted"] += len(outcome["statuses"])
        totals["seconds"] += outcome["seconds"]
        totals["pages"] += outcome["pages"]
        if wrong_before is None:
            totals["errors"] += sum(status != "right" for status in statuses)
        else:
            totals["errors"] += sum(status == "wrong" for status in statuses)
            totals["found"] += sum(outcome["statuses"].get(fact) == "right" for fact in wrong_before[paper])
    return totals


def rating_entry(provider, model_id, scores, errors, speeds, extra=None):
    """A ratings-file entry from per-run scores, errors and speeds (None where not measured)."""
    speeds = [speed for speed in speeds if speed]
    entry = {"model_id": model_id, "provider": provider, "score": round(statistics.fmean(scores), 2),
             "errors": round(statistics.fmean(errors), 2),
             "speed": round(statistics.fmean(speeds), 3) if speeds else None, "runs": len(scores)}
    if len(scores) > 1:
        entry["noise"] = round(statistics.stdev(scores), 2)
    return {**entry, **(extra or {})}


def text_entry(stage, model, totals_by_run):
    """A text model's entry from its runs' totals (run_totals())."""
    scores = [100 * totals["right"] / totals["counted"] if totals["counted"] else 0 for totals in totals_by_run]
    speeds = [totals["pages"] / (totals["seconds"] / 60) if totals["seconds"] and totals["pages"] else None
              for totals in totals_by_run]
    extra = {"found": round(statistics.fmean(t["found"] for t in totals_by_run), 2)} if stage == "check" else None
    provider, _, model_id = model["selector"].partition("/")
    return rating_entry(provider, model_id, scores, [t["errors"] for t in totals_by_run], speeds, extra)


# --- narration --------------------------------------------------------------------------------------------------
ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
SCALES = ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"), (100, "hundred"))


def integer_words(number):
    if number < 20:
        return ONES[number]
    if number < 100:
        return TENS[number // 10] + ("" if number % 10 == 0 else f" {ONES[number % 10]}")
    for size, word in SCALES:
        if number >= size:
            head, rest = divmod(number, size)
            return f"{integer_words(head)} {word}" + ("" if rest == 0 else f" {integer_words(rest)}")
    raise ValueError(number)


def number_words(token):
    """A number as words: "3.5" three point five, "128,000", and a year from 1900 to 2099 as it is said
    ("twenty fourteen", "two thousand nine", "nineteen hundred")."""
    plain = token.replace(",", "")
    whole, _, fraction = plain.partition(".")
    if not fraction and "," not in token and len(whole) == 4 and 1900 <= int(whole) <= 2099:
        year = int(whole)
        if 2000 <= year <= 2009:
            return integer_words(year)
        head, tail = divmod(year, 100)
        return f"{integer_words(head)} {'hundred' if tail == 0 else integer_words(tail)}"
    words = integer_words(int(whole))
    return words + ("" if not fraction else " point " + " ".join(ONES[int(digit)] for digit in fraction))


def spoken_words(text):
    """Words as they are heard, so a passage and its transcript compare alike: lower case, numbers as words,
    "%" as percent, hyphens and slashes as spaces, other punctuation dropped."""
    text = text.lower().replace("%", " percent ")
    text = re.sub(r"\d[\d,]*(?:\.\d+)?", lambda match: f" {number_words(match.group(0).rstrip(','))} ", text)
    text = re.sub(r"[-/–—]", " ", text)
    return re.sub(r"[^a-z0-9' ]+", " ", text).replace("'", "").split()


def word_errors(reference, heard):
    """Wrong, missing and extra words: the word-level edit distance."""
    previous = list(range(len(heard) + 1))
    for index, word in enumerate(reference, 1):
        current = [index]
        for at, other in enumerate(heard, 1):
            current.append(min(previous[at] + 1, current[at - 1] + 1, previous[at - 1] + (word != other)))
        previous = current
    return previous[-1]


def load_passages(path=PASSAGES):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))["passages"]


def stt_url(args):
    return (args.stt_url or os.environ.get("HILDE_STT_URL") or "").rstrip("/")


def ensure_stt(url):
    """Stop (Refused) unless a speech-to-text server is configured and answers."""
    if not url:
        raise Refused(NO_STT)
    try:
        fetch_json(f"{url}/v1/models", timeout=10)
    except (OSError, ValueError) as exc:
        raise Refused(f"The speech-to-text server {url} does not answer ({exc}). Ask Can which speech-to-text "
                      "model runs, and pass its address with --stt-url.")


def transcribe(url, model, wav):
    """The text an OpenAI-compatible speech-to-text server hears in `wav`."""
    boundary = uuid.uuid4().hex
    parts = [(b'Content-Disposition: form-data; name="model"\r\n\r\n', model.encode()),
             (b'Content-Disposition: form-data; name="response_format"\r\n\r\n', b"json"),
             (b'Content-Disposition: form-data; name="file"; filename="passages.wav"\r\n'
              b"Content-Type: audio/wav\r\n\r\n", Path(wav).read_bytes())]
    body = b"".join(b"--" + boundary.encode() + b"\r\n" + head + value + b"\r\n" for head, value in parts)
    body += b"--" + boundary.encode() + b"--\r\n"
    request = urllib.request.Request(f"{url}/v1/audio/transcriptions", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)["text"]


def narration_model_id(model):
    """A narration model as Settings names it (/api/jobs "configuration"): a local folder by its name."""
    path = Path(model)
    return path.name if path.is_absolute() or path.exists() else model


def narration_run(args, model, number, voice, work):
    """One narration run, done once and kept (result.json): {"errors", "words", "audio", "seconds"}."""
    folder = work / "narration" / slug(narration_model_id(model)) / f"run-{number}"
    result = folder / "result.json"
    if result.is_file():
        return json.loads(result.read_text(encoding="utf-8"))
    folder.mkdir(parents=True, exist_ok=True)
    passages = load_passages()
    script = "\n\n".join(" ".join(passage["text"].split()) for passage in passages)
    (folder / "passages.txt").write_text(script + "\n", encoding="utf-8")
    output = folder / "passages.wav"
    command = [sys.executable, str(REPO / "audiobook_tts.py"), "narrate", "--input", str(folder / "passages.txt"),
               "--clone-model-path", model, "--voice-dir", str(voice), "--device", args.device,
               "--attn-implementation", "sdpa", "--sentence-chunks", "--output", str(output), "--overwrite"]
    started = time.monotonic()
    with (folder / "log.txt").open("w", encoding="utf-8") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    seconds = time.monotonic() - started
    import soundfile
    audio = soundfile.info(str(output)).duration
    heard = transcribe(stt_url(args), args.stt_model, output)
    (folder / "heard.txt").write_text(heard + "\n", encoding="utf-8")
    reference = spoken_words(script)
    outcome = {"errors": word_errors(reference, spoken_words(heard)), "words": len(reference),
               "audio": audio, "seconds": seconds}
    result.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    return outcome


def narration_entry(model, outcomes):
    scores = [max(0.0, 100 * (1 - run["errors"] / run["words"])) for run in outcomes]
    return rating_entry("hilde", narration_model_id(model), scores, [run["errors"] for run in outcomes],
                        [run["audio"] / run["seconds"] if run["seconds"] else None for run in outcomes])


def voice_copy(args, work):
    """The reference voice's test copy (reference.wav and transcript.txt), from Hilde's Voices folder, read only."""
    source = Path(args.voices) / args.voice
    copy = work / "voice" / args.voice
    if not copy.is_dir():
        if not (source / "reference.wav").is_file():
            raise Refused(f"No saved voice {args.voice!r} in {args.voices}.")
        copy.mkdir(parents=True)
        for name in ("reference.wav", "transcript.txt"):
            shutil.copyfile(source / name, copy / name)
    return copy


# --- the plan ---------------------------------------------------------------------------------------------------
def count_of(number, noun):
    return f"{number} {noun}{'' if number == 1 else 's'}"


def stage_models(stage, catalog):
    """The catalog models a text job rates, and those it leaves out with why."""
    models = [model for model in catalog["models"] if model.get("kind") in (None, "local", "cloud")]
    if stage != "figures":
        return models, []
    return ([model for model in models if model.get("images") is True],
            [(model, "does not read images" if model.get("images") is False else "not known to read images")
             for model in models if model.get("images") is not True])


def plan(args, catalog, pdfs):
    """What a run would do: lines for --dry-run, and the problems that stop it before anything runs."""
    lines, stops = [], []
    runs = args.runs
    papers = [paper for paper in args.papers if paper in pdfs]
    lines.append(f"Models: {catalog['source']}.")
    lines.append(f"Papers ({len(papers)} of {len(args.papers)} with a fact set and a PDF in {args.pdf_dir}):")
    for paper in args.papers:
        lines.append(f"  {paper}: " + (str(pdfs[paper]) if paper in pdfs else "no PDF with its papers.yaml hash: left out"))
    if not papers and any(stage != "narration" for stage in args.stages):
        stops.append(f"No test paper's PDF is in {args.pdf_dir}: pass the folder with --pdf-dir.")
    for stage in args.stages:
        if stage == "narration":
            continue
        models, left = stage_models(stage, catalog)
        if stage == "check" and not args.check_text_model:
            lines.append("check: left out: no --check-text-model (the model that writes the descriptions checked)")
            continue
        count = len(models) * len(papers) * runs
        extra = (f" plus {len(papers) * runs} unchecked runs of {args.check_text_model}" if stage == "check" else "")
        lines.append(f"{stage}: {count_of(len(models), 'model')} × {count_of(len(papers), 'paper')} × "
                     f"{count_of(runs, 'run')} = {count_of(count, 'run')}{extra}")
        for model in models:
            lines.append(f"  {model['selector']} ({model.get('kind', 'local')}"
                         f"{', reads images' if model.get('images') is True else ''})")
        for model, why in left:
            lines.append(f"  left out: {model['selector']} ({why})")
    for stage in ("ask", "voice_design"):
        lines.append(f"{stage}: {LEFT_OUT[stage]}")
    if "narration" in args.stages:
        url = stt_url(args)
        passages = load_passages()
        if not args.narration_model:
            lines.append("narration: left out: no --narration-model (the narration model's folder or ID)")
        else:
            lines.append(f"narration: {count_of(len(args.narration_model), 'model')} × {count_of(runs, 'run')}, "
                         f"{count_of(len(passages), 'passage')}, "
                         f"voice {args.voice or '(none given)'} on {args.device}")
            for model in args.narration_model:
                lines.append(f"  {narration_model_id(model)} ({model})")
            if not args.voice:
                stops.append("Narration needs a saved reference voice: pass its name with --voice.")
        if not url:
            stops.append(NO_STT)
        else:
            lines.append(f"  speech to text: {url} (model {args.stt_model})")
    lines.append(f"Jobs: {args.hilde_url}/api/jobs is read before the start and before every run; any job queued or "
                 "running stops the rating.")
    lines.append(f"Ratings file: {args.out} (written once at the end; stages not rated are kept)")
    return lines, stops


# --- the ratings file -------------------------------------------------------------------------------------------
def merged_ratings(path, rated, machine, papers):
    """The new file: today's date, this machine, the papers used, the stages rated now, and every other stage of
    the file there (voice design's hand entries above all). Checked against Hilde's own schema."""
    try:
        before = json.loads(Path(path).read_text(encoding="utf-8"))
        kept = before.get("stages") if isinstance(before, dict) and isinstance(before.get("stages"), dict) else {}
    except (OSError, ValueError):
        kept = {}
    data = {"generated": date.today().isoformat(), "machine": machine, "papers": papers,
            "stages": {**{stage: entries for stage, entries in kept.items() if stage not in rated}, **rated}}
    web.parse_ratings(data)
    return data


def write_ratings(path, data):
    """Write the ratings file atomically: a private temporary file beside it, then a rename."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as out:
            out.write(json.dumps(data, indent=2) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# --- running ----------------------------------------------------------------------------------------------------
def rate(args, catalog, pdfs):
    """Every run of the plan, then the ratings file. Returns the stages rated."""
    work = Path(args.work)
    papers = [paper for paper in args.papers if paper in pdfs]
    copies = {paper: test_copy(pdfs[paper], work, paper) for paper in papers}
    servers = local_servers(catalog)
    rated = {}
    for stage in args.stages:
        if stage == "narration" or (stage == "check" and not args.check_text_model):
            continue
        models, _ = stage_models(stage, catalog)
        wrong_before = None
        if stage == "check":
            baseline = {paper: [] for paper in papers}
            for number in range(1, args.runs + 1):
                for paper in papers:
                    ensure_idle(args.hilde_url)
                    baseline[paper].append(text_run("check-baseline", "", paper, number, copies[paper], work,
                                                    servers, args.check_text_model))
            wrong_before = {paper: baseline_wrong(outcomes) for paper, outcomes in baseline.items()}
        entries = []
        for model in models:
            totals_by_run = []
            for number in range(1, args.runs + 1):
                outcomes = {}
                for paper in papers:
                    ensure_idle(args.hilde_url)
                    print(f"{stage}: {model['selector']}, {paper}, run {number}", flush=True)
                    outcomes[paper] = text_run(stage, model["selector"], paper, number, copies[paper], work, servers,
                                               args.check_text_model)
                totals_by_run.append(run_totals(outcomes, wrong_before))
            entries.append(text_entry(stage, model, totals_by_run))
        rated[stage] = entries
    if "narration" in args.stages and args.narration_model:
        voice = voice_copy(args, work)
        entries = []
        for model in args.narration_model:
            outcomes = []
            for number in range(1, args.runs + 1):
                ensure_idle(args.hilde_url)
                print(f"narration: {narration_model_id(model)}, run {number}", flush=True)
                outcomes.append(narration_run(args, model, number, voice, work))
            entries.append(narration_entry(model, outcomes))
        rated["narration"] = entries
    write_ratings(args.out, merged_ratings(args.out, rated, args.machine, len(papers)))
    return rated


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    listed = lambda text: [part.strip() for part in text.split(",") if part.strip()]
    p.add_argument("--dry-run", action="store_true", help="list what would run; ask no model and no server")
    p.add_argument("--hilde-url", default="http://127.0.0.1:8800", help="the running Hilde server (read only)")
    p.add_argument("--catalog", help="a saved /api/paper/models answer, instead of asking the Hilde server")
    p.add_argument("--stages", type=listed, default=list(DEFAULT_STAGES), help=",".join(STAGES))
    p.add_argument("--papers", type=listed, default=None, help="test papers (default: every fact set)")
    p.add_argument("--pdf-dir", default=str(web.DEFAULT_STORAGE_ROOT / "Documents"),
                   help="where the test papers' PDFs are, found by hash (read only; each is copied first)")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--models", type=listed, default=None, help="only these catalog selectors")
    p.add_argument("--check-text-model", default="", help="the text model whose descriptions the check stage checks")
    p.add_argument("--narration-model", action="append", default=[], help="a narration model folder or ID; repeat")
    p.add_argument("--voice", default="", help="the saved voice narration reads with (copied first)")
    p.add_argument("--voices", default=str(web.DEFAULT_STORAGE_ROOT / "Voices"), help="Hilde's Voices folder")
    p.add_argument("--device", default="cuda:0", help="the device narration runs on")
    p.add_argument("--stt-url", default="", help="a running OpenAI-compatible speech-to-text server (HILDE_STT_URL)")
    p.add_argument("--stt-model", default=os.environ.get("HILDE_STT_MODEL", "whisper-1"))
    p.add_argument("--work", default=str(DEFAULT_WORK), help="where runs are kept, so a rating can continue")
    p.add_argument("--out", default=str(web.ratings_path()), help="the ratings file (default: Hilde's)")
    p.add_argument("--machine", default=web.machine_name(), help="this machine's label in the file")
    args = p.parse_args(argv)
    unknown = [stage for stage in args.stages if stage not in STAGES]
    if unknown:
        p.error(f"unknown stage {', '.join(unknown)}; choose from {', '.join(STAGES)} "
                f"(ask: {LEFT_OUT['ask']}; voice_design: {LEFT_OUT['voice_design']})")
    args.papers = args.papers or fact_papers()
    return args


def main(argv=None):
    args = parse_args(argv)
    catalog = load_catalog(args)
    pdfs = find_pdfs(args.papers, args.pdf_dir)
    lines, stops = plan(args, catalog, pdfs)
    if args.dry_run:
        print(f"Dry run on {args.machine}: nothing is sent to any model or server.")
        print("\n".join(lines))
        for stop in stops:
            print(f"A real run would stop here: {stop}")
        return 0
    if stops:
        raise Refused(stops[0])
    ensure_idle(args.hilde_url)
    if "narration" in args.stages:
        ensure_stt(stt_url(args))
    rated = rate(args, catalog, pdfs)
    print(f"Wrote {args.out}: " + ", ".join(f"{stage} {len(entries)} models" for stage, entries in rated.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
