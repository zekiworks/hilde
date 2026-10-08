#!/usr/bin/env python3
"""Descriptions-only runs of one paper, for fact-set scoring. Read AGENTS.md first.

  python qa/describe.py PDF --model lm-studio/gemma-4-31B-it --local-server 127.0.0.1:8010 \
      [--local-vision] [--runs 3] [--in-flight 3] [--hilde DIR] --out DIR

Each run adapts only the paper's figure, table, and equation batches with Hilde's own pipeline
(PaperRun with descriptions_only): the same extraction, requests, naming, checks, and second
requests a book gets, while prose keeps the author's text and no audio is made. Every run starts
from no adaptation at all, so the score measures the pipeline; the PDF is extracted once and that
extraction is shared, since it does not depend on the model.

Run N is written to OUT/run-N/: narration.json, as a book's, and book.json with the source hash,
commit, and model, so `python qa/qa.py facts OUT/run-*` finds the paper and scores it. --hilde runs
another checkout of Hilde (a baseline worktree) that has the descriptions_only option.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Reader chunks of the default size (audiobook_tts.py --chunk-max-chars); passages do not depend on it.
CHUNK_MAX_CHARS = 500


def commit_of(folder):
    def git(*args):
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True).stdout.strip()
    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    return commit + ("-dirty" if dirty else "")


def narration_record(web, scratch, prepared):
    """narration.json for one run, built as a book's is (build_reader_artifacts())."""
    document = scratch / "document.md"
    source = document.read_text(encoding="utf-8")
    paragraphs, _ = web.narrated_source_paragraphs(source)
    recorded = web.read_json_file(scratch / "document-pages.json")
    pages = web.narrated_source_pages(source, recorded.get("pages")) if recorded is not None else None
    *_, passages, original_view = web._reader_blocks(
        prepared.read_text(encoding="utf-8"), "\n\n".join(paragraphs), CHUNK_MAX_CHARS,
        scratch / "paragraph-checkpoints", pages,
    )
    return {"schema": 2, "original_view": original_view, "passages": passages}


def run_once(web, args, pdf, folder, extraction):
    """One descriptions-only run into `folder`; reuse `extraction` (run 1's scratch) when given."""
    scratch = folder / "scratch"
    if extraction is not None:
        shutil.copytree(extraction, scratch, ignore=shutil.ignore_patterns("paragraph-checkpoints", "paragraphs-*.txt"))
        manifest = json.loads((scratch / "extraction.json").read_text(encoding="utf-8"))
        (scratch / "extraction.json").write_text(json.dumps({**manifest, "complete": False}), encoding="utf-8")
    log = (folder / "log.txt").open("a", encoding="utf-8")
    prepared = folder / "prepared.txt"
    run = web.PaperRun(
        pdf, prepared, "utf-8", args.model, args.local_server, in_flight=args.in_flight,
        local_vision=args.local_vision, descriptions_only=True, scratch_path=scratch,
    )
    run.publish = lambda event, data: event == "log" and (log.write(str(data)), log.flush())
    started = time.monotonic()
    run.prepare_document(scratch)
    seconds = round(time.monotonic() - started)
    log.write(f"Done in {seconds} s.\n")
    log.close()
    data = web.narration_bytes(narration_record(web, scratch, prepared))
    (folder / "narration.json").write_bytes(data)
    (folder / "book.json").write_text(json.dumps({
        "descriptions_only": True,
        "source_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "source_file": pdf.name,
        "git_commit": commit_of(Path(web.__file__).parent),
        "model": args.model,
        "local_vision": args.local_vision,
        "prompt_hash": run.prompt_version,
        "narration_sha256": hashlib.sha256(data).hexdigest(),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": seconds,
    }, indent=2) + "\n", encoding="utf-8")
    return scratch


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pdf", type=Path)
    p.add_argument("--model", required=True)
    p.add_argument("--local-server", default="")
    p.add_argument("--local-vision", action="store_true", help="the local model sees images")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--in-flight", type=int, default=3)
    p.add_argument("--hilde", type=Path, default=ROOT.parent, help="Hilde checkout to run (default: this one)")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    sys.path.insert(0, str(args.hilde.resolve()))
    import audiobook_tts_web as web
    if "descriptions_only" not in web.PaperRun.__init__.__code__.co_varnames:
        sys.exit(f"{args.hilde}: this Hilde has no descriptions-only mode")
    pdf = args.pdf.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    extraction = None
    for number in range(1, args.runs + 1):
        folder = args.out / f"run-{number}"
        if (folder / "narration.json").is_file():
            print(f"{folder}: done already", flush=True)
            extraction = extraction or folder / "scratch"
            continue
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        scratch = run_once(web, args, pdf, folder, extraction)
        extraction = extraction or scratch
        print(f"{folder}: done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
