import argparse
import base64
import datetime
import hashlib
import io
import http.client
import http.cookiejar
import json
import os
import re
import shlex
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml
import audiobook_tts as cli
import audiobook_tts_web as web

from audiobook_tts import read_voice, save_voice, speech_endpoint
from audiobook_tts_web import GIB, Handler, PaperRun, local_model_names, normalize
from audiobook_tts_web import parse_paper_response

# Tests never reach the Claude Code installed on the machine running them.
web.CLAUDE_CODE_CANDIDATES = ()


class FakeWordAligner:
    def _align(self, frame_count, text, block, start_sample, word_index):
        words = web.reader_word_matches(text)
        if not words:
            return []
        step = max(1, frame_count // (len(words) * 2 + 1))
        return [
            {
                "block": block,
                "index": word_index + index,
                "text": match.group(0),
                "start_sample": start_sample + (2 * index + 1) * step,
                "end_sample": start_sample + (2 * index + 2) * step,
            }
            for index, match in enumerate(words)
        ]

    def align_file(
        self,
        path,
        text,
        block,
        start_sample,
        word_index=0,
    ):
        return self._align(
            sf.info(path).frames,
            text,
            block,
            start_sample,
            word_index,
        )

    def align_samples(
        self,
        samples,
        sample_rate,
        text,
        block,
        start_sample,
        word_index=0,
    ):
        return self._align(
            len(samples),
            text,
            block,
            start_sample,
            word_index,
        )

    def close(self):
        pass


def mp4_box_payload(data, *path):
    """Return the payload of the box reached through nested box types."""
    start, end = 0, len(data)
    for kind in path:
        position = start
        while position < end:
            size, found = struct.unpack_from(">I4s", data, position)
            if found == kind:
                start, end = position + 8, position + size
                break
            position += size
        else:
            raise AssertionError(f"MP4 box {kind!r} is missing")
    return data[start:end]


def fake_jwt(claims):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


# A paragraph below the figure; extraction leaves the line above it out.
FIGURE_PROSE = "Recurrent models are slow to train because each step waits for the one before it."


def write_figure_pdf(path, prose=None):
    """Write a one-page PDF with a sentence and a raster figure, and `prose` below it."""
    import pymupdf

    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "A paragraph before the figure.")
        figure = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
        figure.clear_with(200)
        page.insert_image(pymupdf.Rect(72, 120, 372, 420), pixmap=figure)
        if prose:
            page.insert_text((72, 460), prose)
        pdf.save(path)


ONE_BLOCK_TIMINGS = {
    "schema": 3, "sample_rate": 24000, "duration_samples": 2400, "block_count": 1,
    "paragraphs": [0], "cues": [{"block": 0, "start_sample": 0, "end_sample": 2400}],
    "word_timing": "unavailable", "word_cues": [],
}


def speech_wav(text, seconds_per_char=0.07):
    """WAV bytes as long as `text` takes to say, the way a speech model answers."""
    buffer = io.BytesIO()
    samples = max(1, int(len(text) * seconds_per_char * 24000))
    sf.write(buffer, np.linspace(-0.2, 0.2, samples, dtype=np.float32), 24000,
             format="WAV", subtype="PCM_16")
    return buffer.getvalue()



def store_book(storage, title="A Paper", source_sha256="a" * 64, voice="Narrator", *,
               markdown="<!-- audiobook-tts:block=0 -->\n\nBody text.",
               timings=ONE_BLOCK_TIMINGS, narration=None, audio=None, voice_fields=None, source=None):
    """Publish a book with one voice the way a finished job does; `source`,
    a PDF, is kept as the book's own copy."""
    staged = storage.in_progress / f"staged-{voice}.mp3"
    staged.parent.mkdir(parents=True, exist_ok=True)
    if audio is None:
        sf.write(staged, np.zeros(2400, dtype=np.float32), 24000,
                 format="MP3", subtype="MPEG_LAYER_III")
    else:
        staged.write_bytes(audio)
    now = web.utc_timestamp()
    record = {
        "schema": web.BOOK_SCHEMA, "source_sha256": source_sha256, "title": title,
        "source_filenames": [f"{title}.pdf"], "source_file": "source.pdf" if source else None,
        "created_at": now, "updated_at": now, "chunk_max_chars": 500,
    }
    entry = {"name": voice, "created_at": now, "duration": 0.1, **(voice_fields or {})}
    return web.commit_book(
        storage, record, narration or {"schema": 1, "original_view": False, "passages": []},
        markdown, source, entry, staged, dict(timings),
    )



class PaperWorkflowTests(unittest.TestCase):
    def test_audiobook_is_the_default_tab(self):
        self.assertEqual(normalize({})["tab"], "audiobook")

    def test_listen_and_progress_are_persisted_tabs(self):
        # A tab the server does not keep would bounce the page back to Create.
        for tab in ("player", "progress"):
            self.assertEqual(normalize({"tab": tab})["tab"], tab)

    def test_create_step_and_open_book_survive_refresh_when_valid(self):
        restored = normalize({
            "audiobook": {"step": "voice"},
            "player": {"book": "Kept Book-Martin.mp3"},
        })
        self.assertEqual(restored["audiobook"]["step"], "voice")
        self.assertEqual(restored["player"]["book"], "Kept Book-Martin.mp3")
        self.assertEqual(
            normalize({"audiobook": {"step": "publish"}})["audiobook"]["step"], "book"
        )

    def test_error_correction_is_off_unless_ticked(self):
        self.assertIs(normalize({})["audiobook"]["verify"], False)
        self.assertEqual(normalize({})["audiobook"]["verify_model"], "")
        unticked = normalize({"audiobook": {"verify": False, "verify_model": "lm-studio/checker"}})
        self.assertIs(unticked["audiobook"]["verify"], False)
        self.assertEqual(unticked["audiobook"]["verify_model"], "lm-studio/checker")

    def test_figures_are_interpreted_only_when_ticked(self):
        self.assertIs(normalize({})["audiobook"]["interpret_figures"], False)
        self.assertEqual(normalize({})["audiobook"]["figure_model"], "")
        for stored, kept in ((True, True), ("yes", False), (None, False)):
            self.assertIs(
                normalize({"audiobook": {"interpret_figures": stored}})["audiobook"]["interpret_figures"], kept
            )
        self.assertEqual(
            normalize({"audiobook": {"figure_model": "openai-codex/gpt-a"}})["audiobook"]["figure_model"],
            "openai-codex/gpt-a",
        )

    def test_a_check_reports_only_problems_and_a_correction_needs_a_fix_and_nothing_new(self):
        # Gemma lists statements it found right too, with "Problem: none".
        self.assertEqual(web.verification_problems(
            "<ISSUES>\n"
            '- Narration: "about 60 percent" | Image: CLM at 60 | Problem: none\n'
            '- Narration: "a decode cost of 10" | Image: 10 is an axis tick | Problem: 10 is no cost.\n'
            "</ISSUES>"
        ), ['Narration: "a decode cost of 10" | Image: 10 is an axis tick | Problem: 10 is no cost.'])
        self.assertEqual(web.verification_problems("<ISSUES>none</ISSUES>"), [])
        self.assertTrue(web.correction_holds("<RESULT>\n1: FIXED | ok\n2: NOT FIXED | no\nNEW: none\n</RESULT>"))
        self.assertFalse(web.correction_holds("<RESULT>\n1: NOT FIXED | still 10\nNEW: none\n</RESULT>"))
        self.assertFalse(web.correction_holds(
            '<RESULT>\n1: FIXED | ok\nNEW: Revision: "Summary leads" | Image: CLM leads | Problem: wrong leader.\n</RESULT>'
        ))

    def test_html_shell_disables_browser_caching(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/"
                ) as response:
                    self.assertEqual(
                        response.headers["Cache-Control"],
                        "no-store",
                    )
            finally:
                server.shutdown()
                thread.join()

    def test_runtime_has_no_browser_controlled_device(self):
        self.assertNotIn("device", normalize({})["runtime"])

    def test_automatic_device_prefers_cuda_when_available(self):
        devices = [
            {"value": "cpu", "label": "CPU"},
            {"value": "cuda:0", "label": "CUDA 0"},
            {"value": "cuda:1", "label": "CUDA 1"},
        ]

        self.assertEqual(web.resolve_device("auto", devices), "cuda:0")

    def test_local_audiobook_pool_uses_every_cuda_device(self):
        consumers = web.audiobook_consumers(
            {"source": "local"},
            [
                {"value": "cpu", "label": "CPU"},
                {"value": "cuda:0", "label": "CUDA 0 — First"},
                {"value": "cuda:1", "label": "CUDA 1 — Second"},
            ],
        )

        self.assertEqual(
            [consumer["device"] for consumer in consumers],
            ["cuda:0", "cuda:1"],
        )

    def test_gpu_the_cuda_runtime_cannot_open_leaves_the_others_usable(self):
        # NVML counts four GPUs before CUDA starts; the runtime then opens
        # three, and those are the indices narration children will see.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        package = Path(temporary.name) / "torch"
        package.mkdir()
        (package / "__init__.py").write_text(
            "from types import SimpleNamespace\n"
            "state = {'initialized': False}\n"
            "def properties(index):\n"
            "    state['initialized'] = True\n"
            "    if not 0 <= index < 3:\n"
            "        raise AssertionError('Invalid device id')\n"
            "    return SimpleNamespace(\n"
            "        name=f'Model {index}', total_memory=(index + 1) << 30\n"
            "    )\n"
            "cuda = SimpleNamespace(\n"
            "    is_available=lambda: True,\n"
            "    init=lambda: state.update(initialized=True),\n"
            "    device_count=lambda: 3 if state['initialized'] else 4,\n"
            "    get_device_properties=properties,\n"
            "    get_device_name=lambda index: properties(index).name,\n"
            ")\n"
            "backends = SimpleNamespace()\n",
            encoding="utf-8",
        )
        previous = web._DEVICE_OPTIONS
        web._DEVICE_OPTIONS = None
        self.addCleanup(setattr, web, "_DEVICE_OPTIONS", previous)

        with mock.patch.dict(os.environ, {"PYTHONPATH": temporary.name}):
            devices = web.available_devices()

        self.assertEqual(
            [device["value"] for device in devices],
            ["cpu", "cuda:0", "cuda:1", "cuda:2"],
        )

    def test_browser_sees_worker_devices_and_models_but_no_hosts_or_paths(self):
        clone = {
            "source": "local",
            "model": "/srv/private/models/Base",
            "allow_downloads": False,
        }
        nodes = [{
            "host": "narrator@spark-one",
            "python": "/srv/private/venv/bin/python",
            "model": "/srv/private/models/Base",
            "devices": ["cuda:0", "cuda:2"],
        }]
        devices = [
            {"value": "cpu", "label": "CPU"},
            {
                "value": "cuda:0",
                "label": "CUDA 0 — Accelerator",
                "name": "Accelerator",
                "memory": 96 * GIB,
            },
            {
                "value": "cuda:1",
                "label": "CUDA 1 — Accelerator",
                "name": "Accelerator",
                "memory": 96 * GIB,
            },
        ]
        jobs = web.JobQueue(web.audiobook_consumers(clone, devices, nodes))
        self.addCleanup(jobs.shutdown)
        public = {
            "consumers": jobs.public_consumers_snapshot(),
            "configuration": web.public_configuration(
                {
                    "design": {
                        "source": "server",
                        "server": "http://10.1.2.3:9000/v1",
                        "server_model": "gpt-4o-mini-tts",
                    },
                    "clone": clone,
                },
                devices,
            ),
        }
        local_design = web.public_configuration(
            {
                "design": {
                    "source": "local",
                    "model": "Qwen/VoiceDesign",
                    "allow_downloads": True,
                },
                "clone": {"source": "missing"},
            },
            devices,
        )

        # A node is kind "node" and its number only; the GPUs here are "local".
        self.assertEqual(
            [
                (worker["label"], worker["detail"], worker["kind"], worker["status"])
                for worker in public["consumers"]
            ],
            [
                ("GPU 0", "Accelerator · 96 GiB", "local", "idle"),
                ("GPU 1", "Accelerator · 96 GiB", "local", "idle"),
                ("Node 1 · GPU 0", "", "node", "idle"),
                ("Node 1 · GPU 2", "", "node", "idle"),
            ],
        )
        # The one worker of a speech server or of no model is this server's own.
        for clone_model in (
            {"source": "server", "server": "http://10.1.2.3:9000/v1", "server_model": "tts-1"},
            {"source": "missing"},
        ):
            self.assertEqual(
                [worker["kind"] for worker in web.JobQueue(
                    web.audiobook_consumers(clone_model, devices, nodes)
                ).public_consumers_snapshot()],
                ["local"],
            )
        self.assertEqual(public["configuration"], {
            "design": {"source": "server", "model": "gpt-4o-mini-tts"},
            "clone": {"source": "local", "model": "Base"},
        })
        self.assertEqual(local_design["design"], {
            "source": "local",
            "model": "Qwen/VoiceDesign",
            "device": "the GPU with the most free memory",
        })
        serialized = json.dumps(public)
        for private in ("spark-one", "narrator", "/srv/private", "10.1.2.3"):
            self.assertNotIn(private, serialized)

    def test_values_resolve_automatic_device_before_starting_a_run(self):
        previous = web._DEVICE_OPTIONS
        web._DEVICE_OPTIONS = [
            {"value": "cpu", "label": "CPU"},
            {"value": "cuda:0", "label": "CUDA 0"},
        ]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        try:
            values = web.values_of(
                normalize({}), web.unconfigured_tts_models(), storage
            )
        finally:
            web._DEVICE_OPTIONS = previous

        self.assertEqual(values["device"], "cuda:0")

    def test_automatic_device_falls_back_to_cpu(self):
        devices = [{"value": "cpu", "label": "CPU"}]

        self.assertEqual(web.resolve_device("auto", devices), "cpu")

    def test_voice_design_takes_the_gpu_with_the_most_free_memory(self):
        devices = [
            {"value": "cpu", "label": "CPU"},
            {"value": "cuda:0", "label": "CUDA 0", "uuid": "aaaa"},
            {"value": "cuda:1", "label": "CUDA 1", "uuid": "bbbb"},
            {"value": "cuda:2", "label": "CUDA 2", "uuid": "cccc"},
        ]
        # nvidia-smi numbers GPUs its own way; only the UUIDs line up.
        smi = web.subprocess.CompletedProcess(
            [], 0, stdout="GPU-cccc, 50000\nGPU-aaaa, 261\nGPU-bbbb, 96387\n", stderr=""
        )
        with mock.patch.object(web.subprocess, "run", return_value=smi):
            chosen = web.roomiest_cuda_device(devices)
        with mock.patch.object(web.subprocess, "run", side_effect=FileNotFoundError):
            without_tool = web.roomiest_cuda_device(devices)

        self.assertEqual(chosen, "cuda:1")
        self.assertIsNone(without_tool)

    def test_browser_device_selection_is_ignored(self):
        state = normalize({
            "schema": web.STATE_SCHEMA_VERSION,
            "runtime": {"device": "cuda:1"},
        })

        self.assertNotIn("device", state["runtime"])

    def test_terminal_bibliography_is_omitted_from_narration(self):
        kept, omitted = web.narrated_source_paragraphs(
            "Conclusion cites Smith (2020) in the body.\n\n"
            "References\n\n"
            "Smith, A. Important work. 2020.\n\n"
            "Jones, B. Another work. 2021."
        )

        self.assertEqual(kept, ["Conclusion cites Smith (2020) in the body."])
        self.assertEqual(omitted, 3)

    def test_appendix_after_markdown_references_is_preserved(self):
        kept, omitted = web.narrated_source_paragraphs(
            "# Main text\n\n"
            "The substantive discussion remains intact.\n\n"
            "## Bibliography\n\n"
            "Smith, A. Important work. 2020.\n\n"
            "## Appendix A\n\n"
            "Appendix evidence remains part of the narration."
        )

        self.assertEqual(kept, [
            "# Main text",
            "The substantive discussion remains intact.",
            "## Appendix A",
            "Appendix evidence remains part of the narration.",
        ])
        self.assertEqual(omitted, 2)

    def test_a_references_title_run_into_the_text_still_starts_the_bibliography(self):
        # As PDF extraction wrote the Attention paper: the acknowledgements,
        # the references title, and the first entry in one paragraph, and an
        # appendix whose title has no "Appendix" in it.
        kept, omitted = web.narrated_source_paragraphs(
            # Bold titles in running prose are not headings.
            "Earlier work matters. **References** to it appear in the method, "
            "and the **Bibliography** Tool lists them.\n\n"
            "**Acknowledgements** We are grateful to Nal Kalchbrenner for his comments. "
            "**References** [1] Jimmy Lei Ba and Geoffrey Hinton. Layer normalization. 2016.\n\n"
            "- [2] Dzmitry Bahdanau and Yoshua Bengio. Neural machine translation. 2014.\n\n"
            "# **Attention Visualizations**\n\n"
            "Figure 3: The attention mechanism following long-distance dependencies."
        )

        self.assertEqual(kept, [
            "Earlier work matters. **References** to it appear in the method, "
            "and the **Bibliography** Tool lists them.",
            "**Acknowledgements** We are grateful to Nal Kalchbrenner for his comments.",
            "# **Attention Visualizations**",
            "Figure 3: The attention mechanism following long-distance dependencies.",
        ])
        self.assertEqual(omitted, 3)

    def test_tables_of_contents_are_never_narrated(self):
        # As PDF extraction writes them: a contents table whose wrapped rows
        # lose their page number, a format-control paragraph from the page
        # break, and a list of figures with dot leaders.
        kept, omitted = web.narrated_source_paragraphs(
            "# On the Measure of Intelligence\n\n"
            "# **Contents**\n\n"
            "|**I**<br>**Context**|**and history**|**3**|\n"
            "|---|---|---|\n"
            "|I.1|Need for an actionable definition . . . . . .|3|\n"
            "|I.3.3|Measuring broad abilities: the psychometrics||\n"
            "|I.3.4|Integrating AI evaluation and psychometrics . . .|11|\n\n"
            "\u200b\n\n"
            "|II<br>A new|perspective|18|\n"
            "|---|---|---|\n"
            "|II.1|Critical assessment . . . . . . . .|18|\n\n"
            "## List of Figures\n\n"
            "1 Hierarchical model of cognitive abilities . . . . . . . 12\n\n"
            "# Chapter 1\n\n"
            "The promise of the field of AI is to develop machines."
        )

        self.assertEqual(kept, [
            "# On the Measure of Intelligence",
            "# Chapter 1",
            "The promise of the field of AI is to develop machines.",
        ])
        self.assertEqual(omitted, 5)
        # Over prose instead of entries, "Contents" names a real section.
        section = ["## Contents", "The archive holds one folder per speaker."]
        self.assertEqual(
            web.narrated_source_paragraphs("\n\n".join(section)), (section, 0)
        )

    def test_the_first_page_title_is_read_past_margin_stamps_and_small_capitals(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)

        def overview(name, metadata=None, title=True):
            path = root / name
            with pymupdf.open() as pdf:
                page = pdf.new_page()
                # arXiv stamps run up the margin in the largest type.
                page.insert_text(
                    (30, 600), "arXiv:2609.22682v1 [cs.AI] 19 Sep 2026",
                    fontsize=20, rotate=90,
                )
                if title:
                    # Small capitals: each word's first letter set larger.
                    writer, x = pymupdf.TextWriter(page.rect), 110
                    for text, size in (
                        ("S", 17.2), ("ELF", 13.8), ("-O", 17.2), ("RGANIZING ", 13.8),
                        ("A", 17.2), ("GENT ", 13.8), ("T", 17.2), ("EAMS", 13.8),
                    ):
                        x = writer.append((x, 100), text, fontsize=size)[1].x
                    writer.write_text(page)
                    page.insert_text((110, 125), "LEARN TO REASON TOGETHER", fontsize=17.2)
                for line in range(20):
                    page.insert_text(
                        (110, 160 + 14 * line),
                        "Collective intelligence depends on how a team organizes its work.",
                        fontsize=10,
                    )
                if metadata:
                    pdf.set_metadata({"title": metadata})
                pdf.save(path)
            return json.loads(subprocess.run(
                [sys.executable, "-c", web._PDF_OVERVIEW, str(path)],
                capture_output=True, text=True, check=True,
            ).stdout)

        # A metadata title with the same words spells the title properly;
        # one that names something else is not trusted.
        paper = overview("paper.pdf", "Self-Organizing Agent Teams Learn to Reason Together")
        self.assertEqual(
            (paper["pages"], paper["title"]),
            (1, "Self-Organizing Agent Teams Learn to Reason Together"),
        )
        self.assertEqual(
            overview("draft.pdf", "Microsoft Word - draft.docx")["title"],
            "SELF-ORGANIZING AGENT TEAMS LEARN TO REASON TOGETHER",
        )
        # Without text that stands out, a page has no title.
        self.assertEqual(overview("chapter.pdf", title=False)["title"], "")

    def test_a_title_the_layout_dropped_heads_the_first_page(self):
        title = "Self-Organizing Agent Teams Learn to Reason Together"
        page = (
            "**Aneesh Pappu**<sup>1</sup> **James Zou**<sup>1</sup>\n\n"
            "# ABSTRACT\n\n"
            "We introduce Self-Organizing Agent Teams, or SAT.\n\n"
            # Figures are named after the PDF, often named after its title.
            "![](images/SELF-ORGANIZING_AGENT_TEAMS_LEARN_TO_REASON_TOGETHER.pdf-0001-05.png)"
        )

        self.assertEqual(
            web.split_paper_paragraphs(web.with_title_heading(page, title))[:2],
            [f"# {title}", "**Aneesh Pappu**<sup>1</sup> **James Zou**<sup>1</sup>"],
        )
        # A title printed as a paragraph of its own becomes the heading.
        self.assertEqual(
            web.split_paper_paragraphs(web.with_title_heading(
                "SELF-ORGANIZING AGENT TEAMS LEARN TO REASON TOGETHER\n\n" + page, title
            ))[:2],
            [f"# {title}", "**Aneesh Pappu**<sup>1</sup> **James Zou**<sup>1</sup>"],
        )
        # One already a heading, or split over two lines, is never read twice.
        for kept in (
            "# **Self-Organizing Agent Teams Learn to Reason Together**<sup>∗</sup>\n\n" + page,
            "SELF-ORGANIZING AGENT TEAMS\n\nLEARN TO REASON TOGETHER\n\n" + page,
        ):
            self.assertEqual(web.with_title_heading(kept, title), kept)

    def test_each_narrated_paragraph_knows_the_pdf_page_it_starts_on(self):
        markdown, starts, rejoined, _, _ = web.join_pdf_pages([
            "# Introduction\n\n"
            "Attention maps a query to an output. The output is a weighted",
            "sum of the values.\n\nA second paragraph starts here.",
            "**Acknowledgements** We thank our colleagues. "
            "**References** [1] A. Author. A paper. 2016.\n\n"
            "# Attention Visualizations\n\n"
            "Figure 3: Attention heads.",
        ])

        self.assertEqual(rejoined, 1)
        kept, _ = web.narrated_source_paragraphs(markdown)
        # A sentence a page break split starts on the earlier page, and a
        # paragraph split off another keeps that one's page.
        self.assertEqual(list(zip(kept, web.narrated_source_pages(markdown, starts))), [
            ("# Introduction", 1),
            ("Attention maps a query to an output. The output is a weighted "
             "sum of the values.", 1),
            ("A second paragraph starts here.", 2),
            ("**Acknowledgements** We thank our colleagues.", 3),
            ("# Attention Visualizations", 3),
            ("Figure 3: Attention heads.", 3),
        ])
        # Pages recorded for other text are not trusted.
        self.assertIsNone(web.narrated_source_pages(markdown + "\n\nMore.", starts))

    def test_page_breaks_do_not_split_sentences(self):
        def joined(*pages):
            return web.split_paper_paragraphs(web.join_pdf_pages(list(pages))[0])

        # Page breaks as PDF extraction writes them for real papers. Attention
        # Is All You Need, pages 3 and 4: the figure's panel titles come out as
        # headings between the halves of the sentence.
        figure = [
            "# Scaled Dot-Product Attention",
            "![](images/p4-1.png)",
            "<!-- Start of picture text -->MatMul<br>SoftMax<!-- End of picture text -->",
            "# Multi-Head Attention",
            "![](images/p4-3.png)",
            "**Figure 2:** (left) Scaled Dot-Product Attention.",
        ]
        self.assertEqual(joined(
            "Attention maps a query to an output. The output is computed as a weighted sum",
            "\n\n".join([*figure, "of the values, where each weight comes from a key."]),
        ), [
            "Attention maps a query to an output. The output is computed as a "
            "weighted sum of the values, where each weight comes from a key.",
            *figure,
        ])
        # On the Measure of Intelligence, page 3: a footnote closes the page
        # between the halves of a hyphenated word.
        footnote = "> 1Turing's imitation game was meant as an argumentative device."
        self.assertEqual(joined(
            f"This is a mistake, as the absence of widely-accepted ex-\n\n{footnote}",
            "plicit definitions has been substituted with implicit ones.",
        ), [
            "This is a mistake, as the absence of widely-accepted explicit "
            "definitions has been substituted with implicit ones.",
            footnote,
        ])
        # Attention, page 10: a caption broken above its table.
        table = "|Parser|WSJ 23 F1|\n|---|---|\n|Transformer (4 layers)|91.3|"
        self.assertEqual(joined(
            "During inference, we",
            "Table 4: The Transformer generalizes well (Results are on Section 23"
            f"\n\nof WSJ)\n\n{table}\n\nincreased the maximum output length.",
        ), [
            "During inference, we increased the maximum output length.",
            "Table 4: The Transformer generalizes well (Results are on Section 23 of WSJ)",
            table,
        ])
        # On the Measure of Intelligence, page 39: an italic first line read
        # as a heading.
        self.assertEqual(joined(
            "Intelligence is measured with respect to priors, experience, and",
            "# _generalization difficulty.”_\n\nWe consider an intelligent system.",
        ), [
            "Intelligence is measured with respect to priors, experience, and "
            "_generalization difficulty.”_",
            "We consider an intelligent system.",
        ])
        # A compound the paper spells hyphenated keeps its hyphen.
        self.assertEqual(joined(
            "Self-attention relates positions. We call it self-",
            "attention, as every position attends to all others.",
        ), [
            "Self-attention relates positions. We call it self-attention, as "
            "every position attends to all others.",
        ])
        # A new section, or a capitalized paragraph after a line that could
        # end a sentence, stays apart.
        for page in (
            "# 4 Why Self-Attention\n\nthis section compares layer types.",
            "Deep Learning is a connectionist framework.",
        ):
            with self.subTest(page=page):
                self.assertEqual(
                    joined("The model is trained on large corpora", page),
                    ["The model is trained on large corpora", *web.split_paper_paragraphs(page)],
                )
        # Procedural Graphs, pages 19 and 20: a line that stops on a word no
        # sentence ends on goes on across a table, even into a capital.
        table = ["Table 5 | Dataset statistics.", "|Benchmark|Train|\n|---|---|\n|HotpotQA|1,000|"]
        self.assertEqual(joined(
            "The validation set never overlaps the test set; its size is 1,000 for",
            "\n\n".join([*table, "HotpotQA and 100 for MultiChallenge."]),
        ), [
            "The validation set never overlaps the test set; its size is 1,000 for "
            "HotpotQA and 100 for MultiChallenge.",
            *table,
        ])

    def test_figures_follow_the_sentence_they_cut_and_the_text_that_introduces_them(self):
        def joined(*pages):
            markdown, _, _, moved, _ = web.join_pdf_pages(list(pages))
            return web.split_paper_paragraphs(markdown), moved

        labels = "<!-- Start of picture text -->Search<br>Read<!-- End of picture text -->"
        # Procedural Graphs, pages 1 and 2: a caption written "Figure 1 | …"
        # sat between the halves of a sentence, and the figure was described
        # in the middle of it.
        figure = [
            "![](images/p2-1.png)",
            labels,
            "Figure 1 | **From knowledge to procedure.** A Knowledge Graph organizes facts.",
        ]
        self.assertEqual(joined(
            "We argue that an agent needs procedural knowledge that is responsive "
            "to its current progress,",
            "\n\n".join([*figure, "and able to improve from experience. The design "
                         "mirrors a familiar structure (Figure 1)."]),
        ), ([
            "We argue that an agent needs procedural knowledge that is responsive to "
            "its current progress, and able to improve from experience. The design "
            "mirrors a familiar structure (Figure 1).",
            *figure,
        ], 0))
        # Hermes, page 2: a figure cuts a sentence within a column.
        figure = ["![](images/p2-9.png)", labels, "Figure 1: Policy deployment."]
        self.assertEqual(joined("\n\n".join([
            "Figure 1 illustrates the stages. It begins with an objective, such as “reduce",
            *figure,
            "network energy consumption by 2%”. The intent is then translated.",
        ])), ([
            "Figure 1 illustrates the stages. It begins with an objective, such as "
            "“reduce network energy consumption by 2%”. The intent is then translated.",
            *figure,
        ], 0))
        # Self-Organizing Agent Teams, pages 5 and 6: a panel's title comes out
        # as a heading between the figure's labels and its caption.
        figure = [
            "![](images/p6-1.png)",
            labels,
            "# **(b) From failure diagnosis to a specialized agent role**",
            "Figure 3: **Team organization is learned offline.**",
        ]
        self.assertEqual(joined(
            "These probes measure whether the mutation transfers; their",
            "\n\n".join([*figure, "outcomes are written back to the archive."]),
        ), ([
            "These probes measure whether the mutation transfers; their outcomes "
            "are written back to the archive.",
            *figure,
        ], 0))
        # Word2vec, page 3: an equation printed as a picture belongs to its
        # sentence, on the page and across a page break alike.
        equation = ["![](images/p3-4.png)", "<!-- Start of picture text -->Q = N × D<!-- End of picture text -->"]
        for pages in (
            ["\n\n".join(["The complexity per training example is", *equation,
                          "where the dominating term is H × V."])],
            ["The complexity per training example is",
             "\n\n".join([*equation, "where the dominating term is H × V."])],
        ):
            with self.subTest(pages=len(pages)):
                self.assertEqual(joined(*pages), ([
                    "The complexity per training example is", *equation,
                    "where the dominating term is H × V.",
                ], 0))
        # Attention, page 3: the figure is printed before the text introduces
        # it, so its description would come first. Tables 1 and 2 follow the
        # passage naming them both, and Figure 5 the one naming Figures 4–6; a
        # figure already after its first mention, or mentioned only pages
        # away, stays where it is.
        figure = ["![](images/p3-0.png)", "Figure 1: The Transformer - model architecture."]
        tables = [
            "Table 1: Path lengths.", "|Layer|Length|\n|---|---|\n|Self-Attention|O(1)|",
            "Table 2: Scores.", "|Model|BLEU|\n|---|---|\n|Transformer|28.4|",
        ]
        introduced = (
            "The Transformer follows this overall architecture, shown in the left "
            "and right halves of Figure 1, respectively."
        )
        compared = "Tables 1 and 2 compare layer types and results."
        early = ["Figure 2 shows attention.", "Attention is a weighted sum.",
                 "![](images/p3-2.png)", "Figure 2: Scaled dot-product attention."]
        distant = ["![](images/p3-3.png)", "Figure 3: Attention visualizations."]
        heads = ["![](images/p3-5.png)", "Figure 5: Heads in later layers."]
        ranged = "Figures 4–6 show heads in later layers."
        self.assertEqual(joined(
            "\n\n".join([*figure, introduced, *tables, compared, *early, *distant, *heads, ranged]),
            "Page four says nothing of figures.",
            "Page five says Figure 3 shows attention heads.",
        ), ([
            introduced, *figure, compared, *tables, *early, *distant, ranged, *heads,
            "Page four says nothing of figures.",
            "Page five says Figure 3 shows attention heads.",
        ], 4))

    def test_footnotes_follow_and_go_to_the_model_with_the_paragraph_citing_them(self):
        # Attention Is All You Need, pages 1 and 4, as extracted: affiliation
        # notes cited from the author lines, and note 4 cited mid-paragraph,
        # all printed at the bottom of their page.
        authors = "**Aidan N. Gomez**<sup>_∗†_</sup> **Łukasz Kaiser**<sup>_∗_</sup> Google Research"
        last_author = "**Illia Polosukhin**<sup>_∗‡_</sup> illia@example.com"
        abstract = "The dominant sequence transduction models are based on recurrent networks."
        equal, brain, research = (
            "> _∗_ Equal contribution. Listing order is random.",
            "> _†_ Work performed while at Google Brain.",
            "> _‡_ Work performed while at Google Research.",
        )
        cited = ("The dot products grow large, pushing the softmax into regions with "
                 "extremely small gradients<sup>4</sup>. We scale them to counteract this.")
        squared = "The cost grows with n<sup>2</sup> in the sequence length."
        heads = "Multi-head attention attends to several representation subspaces at once."
        note = "> 4To illustrate why the dot products get large, assume independent components."
        # Cited only on a page two pages back, or by nothing: stays put.
        stray = "> 2This note names no nearby passage."
        markdown, _, _, _, notes = web.join_pdf_pages([
            "\n\n".join([authors, last_author, abstract, equal, brain, research]),
            "\n\n".join([squared, "Recurrent layers are slow."]),
            "Attention layers are fast.",
            "\n\n".join([cited, heads, note, stray]),
        ])
        paragraphs = web.split_paper_paragraphs(markdown)

        # A note about one paragraph alone comes right after it, before a note
        # it shares with the paragraphs above, as the last author's
        # affiliation comes before the equal-contribution note.
        self.assertEqual(paragraphs, [
            authors, brain, last_author, research, equal, abstract,
            squared, "Recurrent layers are slow.", "Attention layers are fast.",
            cited, note, heads, stray,
        ])
        self.assertEqual(notes, 4)
        # The model gets each footnote with its paragraph, so it can say whom
        # a note is about and read it after the sentence citing it.
        self.assertEqual(web.paper_batches(paragraphs, 1), [
            (1, 2), (3, 5), (6, 6), (7, 7), (8, 8), (9, 9), (10, 11), (12, 12), (13, 13),
        ])

    def test_authors_are_paired_with_the_affiliation_printed_under_each_name(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name) / "paper.pdf"
        # Attention Is All You Need, page 1: each name in bold over its
        # affiliation and email, in columns, the last author alone below.
        columns = (
            (110, "Llion Jones*", "Google Research", "llion@google.com"),
            (250, "Aidan N. Gomez*", "University of Toronto", "aidan@cs.toronto.edu"),
            (400, "Lukasz Kaiser*", "Google Brain", "lukasz@google.com"),
        )
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((180, 150), "Attention Is All You Need", fontsize=17, fontname="hebo")
            for x, name, place, email in columns:
                page.insert_text((x, 234), name, fontsize=10, fontname="hebo")
                page.insert_text((x, 246), place, fontsize=9)
                page.insert_text((x, 257), email, fontsize=9, fontname="cour")
            page.insert_text((250, 284), "Illia Polosukhin*", fontsize=10, fontname="hebo")
            page.insert_text((240, 295), "illia@gmail.com", fontsize=9, fontname="cour")
            for line in range(20):
                page.insert_text((72, 430 + 14 * line), "The dominant models are recurrent.", fontsize=10)
            pdf.save(source)
        overview = json.loads(subprocess.run(
            [sys.executable, "-c", web._PDF_OVERVIEW, str(source)],
            capture_output=True, text=True, check=True,
        ).stdout)
        authors = web.pair_authors(overview["lines"])
        self.assertEqual(dict(authors), {
            "Llion Jones": "Google Research",
            "Aidan N. Gomez": "University of Toronto",
            "Lukasz Kaiser": "Google Brain",
            "Illia Polosukhin": "",
        })

        # Extraction runs a row together, names first, so a model must guess
        # who works where; the row arrives paired, marks kept on each name.
        row = (
            "**Llion Jones**<sup>_∗_</sup> **Aidan N. Gomez**<sup>_∗†_</sup> "
            "**Lukasz Kaiser**<sup>_∗_</sup> Google Research University of Toronto "
            "Google Brain `llion@google.com aidan@cs.toronto.edu lukasz@google.com`"
        )
        last = "**Illia Polosukhin**<sup>_∗‡_</sup> `illia@gmail.com`"
        page, paired = web.with_author_affiliations(
            f"# Attention Is All You Need\n\n{row}\n\n{last}", authors
        )
        self.assertEqual(paired, 3)
        self.assertEqual(web.split_paper_paragraphs(page), [
            "# Attention Is All You Need",
            "**Llion Jones**<sup>_∗_</sup>, Google Research; **Aidan N. Gomez**<sup>_∗†_</sup>, "
            "University of Toronto; **Lukasz Kaiser**<sup>_∗_</sup>, Google Brain.",
            last,
        ])
        # Names on lines of their own, their affiliations printed apart, are
        # left as printed rather than given their affiliation a second time.
        apart = "**Llion Jones**\n\n**Aidan N. Gomez**\n\nGoogle Research and University of Toronto"
        self.assertEqual(web.with_author_affiliations(apart, authors), (apart, 0))

        def line(text, left, right, top, bold=False):
            return {"text": text, "bbox": [left, top, right, top + 10], "bold": bold}

        # Numbered affiliations are matched to names by their numbers, not
        # by place, and one line centered under a row belongs to no one name.
        self.assertEqual(web.pair_authors([
            line("Aneesh Pappu1,∗", 114, 180, 120, True),
            line("Mirac Suzgun1", 200, 260, 120, True),
            line("1 Stanford University", 114, 200, 133),
            line("2 Together AI", 205, 260, 133),
        ]), [])
        self.assertEqual(web.pair_authors([
            line("Jacob Devlin", 100, 160, 120, True),
            line("Ming-Wei Chang", 180, 250, 120, True),
            line("Kenton Lee", 270, 320, 120, True),
            line("Kristina Toutanova", 340, 420, 120, True),
            line("Google AI Language", 210, 300, 133),
        ]), [])

    def test_pdf_tables_show_as_printed_while_the_model_gets_their_cells(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "table.pdf"
        rows = [("Model", "BLEU EN-DE", "Training Cost (FLOPs)"), ("ByteNet", "23.75", ""),
                ("GNMT + RL", "24.6", "2.3 · 10^19"), ("Transformer (big)", "28.4", "2.3 · 10^19"),
                ("READY_FOR_NEXT_OP", "29.1", "")]
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            # Attention Is All You Need prints Table 3's caption on three lines,
            # ending in a caveat on how its perplexities were measured.
            caption = (
                "Table 2: The Transformer achieves better BLEU scores than previous models on the",
                "English-to-German and English-to-French newstest2014 tests at a fraction of the cost.",
                "Listed perplexities are per-wordpiece and should not be compared to per-word ones.",
            )
            for line, text in enumerate(caption):
                page.insert_text((72, 62 + 9 * line), text, fontsize=8)
            edges = [100, 250, 360, 510]
            for row, cells in enumerate(rows):
                for column, cell in enumerate(cells):
                    page.insert_text((edges[column] + 5, 109 + row * 20), cell, fontsize=10)
            for row in range(len(rows) + 1):
                page.draw_line((edges[0], 95 + row * 20), (edges[-1], 95 + row * 20))
            for edge in edges:
                page.draw_line((edge, 95), (edge, 95 + len(rows) * 20))
            for line in range(12):
                page.insert_text((72, 220 + 14 * line),
                                 "The Transformer outperforms the best reported models.", fontsize=10)
            pdf.save(source)
        stage = root / "stage"
        stage.mkdir()
        output = stage / "page.md"
        subprocess.run(
            [sys.executable, "-c", web._PDF_CONVERTER, str(source), str(output),
             str(stage / "images"), "0"],
            check=True, capture_output=True,
        )
        paragraphs = web.split_paper_paragraphs(output.read_text(encoding="utf-8"))
        kinds = web._layout_kinds(paragraphs)

        # The table as printed, then its cells as the text behind it: never
        # a Markdown table rebuilt from the layout, which the reader would show.
        self.assertEqual(kinds[:3], ["caption", "image", "labels"])
        self.assertNotIn("table", kinds)
        # Screen readers announce the table by its whole caption.
        image = re.fullmatch(r"!\[(.+)\]\((images/[^)]+\.png)\)", paragraphs[1])
        self.assertIsNotNone(image, paragraphs[1])
        self.assertEqual(image.group(1), " ".join(caption))
        with pymupdf.open(stage / image.group(2)) as picture:
            self.assertGreater(picture[0].rect.width, 300)
        self.assertIn("|ByteNet|23.75|", paragraphs[2].replace(" ", ""))
        # An underscore inside a name is the name's own, not emphasis.
        self.assertIn("READY_FOR_NEXT_OP", paragraphs[2])
        # The caption, picture, and cells reach the model in one request.
        self.assertEqual(web.paper_batches(paragraphs, 1)[0], (1, 3))

    def test_a_listing_reaches_the_model_whole_with_its_words_apart(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "listing.pdf"
        # Harness-Zero, Skill 1: a skill file set in a typewriter font, with a
        # sentence wrapped over two lines, a heading, a list item, and a file
        # tree whose comments sit to the right of each name.
        listing = [
            ("name: student-harness-evolve", None),
            ("description: evolve a shared harness, use a swarm (default 10", None),
            ("  coder subagents) to analyze rollouts in parallel.", None),
            ("## Campaign parameters (set at each instantiation)", None),
            ("- tools/ -- prebuilt tools loaded into the agent;", None),
            ("|-- registry.py", "# single entry point; API contract below"),
            ("|-- manifest.json", "# enablement manifest"),
        ]
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            for line in range(4):
                page.insert_text((72, 72 + 14 * line),
                                 "The skill below guides the evolution agent.", fontsize=10)
            for line, (text, comment) in enumerate(listing):
                page.insert_text((80, 150 + 10 * line), text, fontsize=8, fontname="cour")
                if comment:
                    page.insert_text((200, 150 + 10 * line), comment, fontsize=8, fontname="cour")
            # Harness-Zero draws some rows a second time as scattered glyphs
            # over the first; that copy must not reach the text.
            heading = listing[3][0]
            for column in range(3, len(heading), 7):
                page.insert_text((80 + 4.8 * column, 180), heading[column], fontsize=8, fontname="cour")
            for line in range(4):
                page.insert_text((72, 250 + 14 * line),
                                 "Evaluation uses the student with the harness attached.", fontsize=10)
            pdf.save(source)
        stage = root / "stage"
        stage.mkdir()
        output = stage / "page.md"
        subprocess.run(
            [sys.executable, "-c", web._PDF_CONVERTER, str(source), str(output),
             str(stage / "images"), "0"],
            check=True, capture_output=True,
        )
        paragraphs = web.split_paper_paragraphs(output.read_text(encoding="utf-8"))
        listings = [paragraph for paragraph in paragraphs if web._is_listing(paragraph)]

        # One fenced block, so the wrapped sentence reaches the model whole,
        # with every line in order and no words run together.
        self.assertEqual(len(listings), 1, paragraphs)
        self.assertEqual(
            [" ".join(line.split()) for line in listings[0].splitlines()[1:-1]],
            [" ".join(" ".join(filter(None, row)).split()) for row in listing],
        )
        self.assertTrue(any("guides the evolution agent" in p for p in paragraphs))
        self.assertFalse(any("guides the evolution agent" in p for p in listings))

    @staticmethod
    def converted_page(root, draw):
        import pymupdf

        source = root / "page.pdf"
        with pymupdf.open() as pdf:
            draw(pdf.new_page())
            pdf.save(source)
        stage = root / "stage"
        stage.mkdir()
        output = stage / "page.md"
        subprocess.run(
            [sys.executable, "-c", web._PDF_CONVERTER, str(source), str(output),
             str(stage / "images"), "0"],
            check=True, capture_output=True,
        )
        return web.split_paper_paragraphs(output.read_text(encoding="utf-8"))

    def test_a_listing_going_on_in_the_next_column_keeps_its_order(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        prompt = [f"Step {number}: check the student's work before it runs." for number in range(1, 9)]

        def draw(page):
            # A two-column paper: the prompt starts at the foot of the left
            # column and goes on at the head of the right one.
            for line in range(36):
                page.insert_text((52, 72 + 14 * line), "Body text of the left column.", fontsize=10)
            for line, text in enumerate(prompt[:5]):
                page.insert_text((52, 600 + 10 * line), text, fontsize=7, fontname="cour")
            for line, text in enumerate(prompt[5:]):
                page.insert_text((312, 72 + 10 * line), text, fontsize=7, fontname="cour")
            for line in range(36):
                page.insert_text((312, 130 + 14 * line), "Body text of the right column.", fontsize=10)

        paragraphs = self.converted_page(Path(temporary.name), draw)
        listings = [paragraph for paragraph in paragraphs if web._is_listing(paragraph)]
        self.assertEqual(len(listings), 1, paragraphs)
        # In reading order, and the right column is not pushed far right.
        lines = listings[0].splitlines()[1:-1]
        self.assertEqual([line.strip() for line in lines], prompt)
        self.assertTrue(all(len(line) - len(line.lstrip()) < 4 for line in lines), lines)

    def test_scripts_are_marked_where_they_hang_from_their_symbol(self):
        import pymupdf

        # The converter's own functions, run on a page drawn the way Attention
        # prints Equation 3 and "d_k" in prose.
        source = web._PDF_CONVERTER
        start = source.index("# Sub- and superscripts as printed")
        functions = {"collections": __import__("collections"), "re": re}
        exec(source[start:source.index("# Replacements are made from the end of the page")], functions)
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            x = 72
            for text, size, raise_by in (
                ("lrate = d", 10, 0), ("model", 7, -3), ("-0.5", 7, 4), (" * step_num * warmup_steps", 10, 0),
                ("-1.5", 7, 4),
            ):
                # A subscript and the superscript above it start at the same place.
                left = x - (pymupdf.get_text_length("model", fontsize=7) if text == "-0.5" else 0)
                page.insert_text((left, 100 - raise_by), text, fontsize=size)
                x = left + pymupdf.get_text_length(text, fontsize=size)
            page.insert_text((72, 200), "keys of dimension d", fontsize=10)
            page.insert_text((72 + pymupdf.get_text_length("keys of dimension d", fontsize=10), 202), "k", fontsize=7)
            formula = functions["formula_text"](page, pymupdf.Rect(60, 80, 400, 110))
            prose = functions["with_subscripts"]("keys of dimension _dk_ .", page, pymupdf.Rect(60, 185, 400, 210))
        # Only warmup_steps is raised to −1.5, and d_model to −0.5.
        self.assertEqual(formula, "lrate = d<sub>model</sub><sup>-0.5</sup> * step_num * warmup_steps<sup>-1.5</sup>")
        self.assertEqual(prose, "keys of dimension _d<sub>k</sub>_ .")
        # A numerator as long as its line is still a numerator (RRSI's Equation 6);
        # a second line further down is a line.
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 100), "dS = s1 - s0, dC =", fontsize=10)
            page.insert_text((180, 93), "cost1 - cost0", fontsize=10)
            page.insert_text((190, 107), "cost0", fontsize=10)
            page.insert_text((260, 100), ".", fontsize=10)
            page.insert_text((380, 100), "(6)", fontsize=10)
            page.insert_text((72, 117), "y = x + 1", fontsize=10)
            formula = functions["formula_text"](page, pymupdf.Rect(60, 80, 400, 122))
        # The printed number ends the formula's text, where Hilde reads it to name the equation.
        self.assertEqual(formula, "dS = s1 - s0, dC = (cost1 - cost0)/(cost0) . y = x + 1 (6)")
        # Cases: a brace and "=" beside two rows are no fraction, and the number
        # printed on the middle line still ends the text (Procedural Graphs' Equation 5).
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 107), "Gk = (", fontsize=10)
            page.insert_text((110, 100), "cand, if better", fontsize=10)
            page.insert_text((110, 114), "old, otherwise", fontsize=10)
            page.insert_text((400, 107), "(5)", fontsize=10)
            cases = functions["formula_text"](page, pymupdf.Rect(60, 88, 430, 120))
        self.assertTrue(cases.endswith("(5)"), cases)
        self.assertNotIn(")/(", cases)
        self.assertEqual(web.printed_equation_numbers([f"<!-- Start of picture text -->\n{cases}\n<!-- End of picture text -->"]), ["5"])
        # A stacked one-half, set small, inside a bracket after a subscripted
        # symbol (CLM's Equation 9): a fraction each time, and never a script
        # of the symbol before the bracket, as "C_attn₂ … +₂¹" was.
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            x = 72
            for text, size, y, step in (
                ("F = C", 10, 100, 0), ("attn", 7, 102, 1), ("[", 10, 100, 0.5),
                ("1", 7, 96, None), ("2", 7, 103.5, 0.5), ("(P + G) +", 10, 100, 0.5),
                ("1", 7, 96, None), ("2", 7, 103.5, 0.5), ("G]", 10, 100, 0),
            ):
                page.insert_text((x, y), text, fontsize=size)
                if step is not None:
                    x += pymupdf.get_text_length(text, fontsize=size) + step
            page.insert_text((300, 100), "(9)", fontsize=10)
            half = functions["formula_text"](page, pymupdf.Rect(60, 85, 330, 112))
        self.assertEqual(half, "F = C<sub>attn</sub> [(1)/(2)(P + G) +(1)/(2)G] (9)")

    def test_a_table_is_told_the_paragraphs_that_discuss_it(self):
        paragraphs = [
            "Training used label smoothing.",
            "In Table 3 rows (B), we observe that reducing the attention key size dk hurts model quality.",
            "Unrelated prose.",
            "Table 3: Variations on the Transformer architecture.",
            "![](images/table.png)",
            "<!-- Start of picture text -->|N|dk|BLEU|<!-- End of picture text -->",
            "More unrelated prose.",
            "where the learning rate is the one in Equation 3.",
        ]
        kinds = ["prose", "prose", "prose", "caption", "image", "labels", "prose", "prose"]
        self.assertEqual(web.visual_context(paragraphs, kinds, 4, 6, "Table 3"), (paragraphs[1],))
        # An equation is told the sentence around it and what names it.
        self.assertEqual(web.visual_context(paragraphs, kinds, 4, 6, None, ["3"]),
                         (paragraphs[2], paragraphs[6], paragraphs[7]))
        # A run-in heading is a section's name, not what a symbol means: with it,
        # RRSI's cost became "complexity" (Equation 6).
        lead_in = ["**Ridge/** _L_ 2 **-Style Complexity-Aware Acceptance.** For a candidate H′, let",
                   "![](images/eq.png)", "<!-- Start of picture text -->\nΔC = (C(H′) − C(H))/(C(H)) . (6)\n<!-- End of picture text -->"]
        self.assertEqual(web.visual_context(lead_in, ["prose", "image", "labels"], 2, 3, None, ["6"]),
                         ("For a candidate H′, let",))
        # A figure whose caption extraction missed gets no neighbors: beside
        # CLM's Figure 6, half a sentence of another section swapped two scores.
        figure = ["Previous section, half a sentence which", "![](images/fig.png)",
                  "<!-- Start of picture text -->\n44.6 | 179 PF\n<!-- End of picture text -->"]
        self.assertEqual(web.visual_context(figure, ["prose", "image", "labels"], 2, 3), ())

    def test_captions_printed_below_their_tables_name_their_own_table(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        captions = ("Table 1: GLUE results for every model.", "Table 2: SQuAD results for every model.")

        def draw(page):
            # BERT prints each caption below its table, so Table 1's caption
            # sits right above Table 2 too, a little nearer to it than to its own.
            edges = [100, 250, 360, 510]
            for number, top in enumerate((90, 214)):
                rows = [("System", "Score", "Average")] + [(f"Model {n}", f"{80 + n}.{number}", f"{70 + n}.5") for n in range(4)]
                for row, cells in enumerate(rows):
                    for column, cell in enumerate(cells):
                        page.insert_text((edges[column] + 5, top + 14 + row * 20), cell, fontsize=10)
                for row in range(len(rows) + 1):
                    page.draw_line((edges[0], top + row * 20), (edges[-1], top + row * 20))
                for edge in edges:
                    page.draw_line((edge, top), (edge, top + len(rows) * 20))
                page.insert_text((150, top + len(rows) * 20 + 16), captions[number], fontsize=9)
            for line in range(12):
                page.insert_text((72, 600 + 14 * line), "Fine-tuning improves every task.", fontsize=10)

        paragraphs = self.converted_page(Path(temporary.name), draw)
        self.assertEqual(
            [match.group(1) for p in paragraphs for match in [re.fullmatch(r"!\[(.+)\]\(images/[^)]+\)", p)] if match],
            list(captions),
        )

    def test_a_figure_caption_above_a_table_is_not_the_tables(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        caption = "Table 1: GLUE results for every model."

        def draw(page):
            # BERT, page 6: Figure 1's caption, printed below the figure,
            # sits right above Table 1, whose caption is printed below it.
            figure = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
            figure.set_rect(figure.irect, (40, 90, 160))
            page.insert_image(pymupdf.Rect(150, 60, 450, 200), pixmap=figure)
            page.insert_text((120, 216), "Figure 1: Overall pre-training and fine-tuning procedures.", fontsize=9)
            edges, top = [100, 250, 360, 510], 232
            rows = [("System", "Score", "Average")] + [(f"Model {n}", f"{80 + n}.1", f"{70 + n}.5") for n in range(4)]
            for row, cells in enumerate(rows):
                for column, cell in enumerate(cells):
                    page.insert_text((edges[column] + 5, top + 14 + row * 20), cell, fontsize=10)
            for row in range(len(rows) + 1):
                page.draw_line((edges[0], top + row * 20), (edges[-1], top + row * 20))
            for edge in edges:
                page.draw_line((edge, top), (edge, top + len(rows) * 20))
            page.insert_text((150, top + len(rows) * 20 + 16), caption, fontsize=9)
            for line in range(12):
                page.insert_text((72, 420 + 14 * line), "Fine-tuning improves every task.", fontsize=10)

        paragraphs = self.converted_page(Path(temporary.name), draw)
        self.assertEqual(
            [match.group(1) for p in paragraphs
             for match in [re.fullmatch(r"!\[(.*)\]\(images/[^)]*-table-\d+\.png\)", p)] if match],
            [caption],
        )

    def test_an_icon_in_a_line_of_text_is_no_figure(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        def draw(page):
            # CLM's first page sets a 5 pt logo in its link line; alone in a
            # request, it had the model invent a figure.
            icon = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 16, 16), False)
            icon.set_rect(icon.irect, (30, 90, 200))
            figure = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
            figure.set_rect(figure.irect, (200, 90, 40))
            for line in range(8):
                page.insert_text((72, 100 + 14 * line), "Body text of the paper about context and models.", fontsize=10)
            page.insert_text((72, 230), "Code: github.com/example/clm", fontsize=10)
            page.insert_image(pymupdf.Rect(232, 222, 238, 229), pixmap=icon)
            page.insert_image(pymupdf.Rect(150, 260, 450, 420), pixmap=figure)
            page.insert_text((150, 436), "Figure 1: The method.", fontsize=9)
            for line in range(10):
                page.insert_text((72, 470 + 14 * line), "More body text follows the figure here.", fontsize=10)

        paragraphs = self.converted_page(Path(temporary.name), draw)
        self.assertEqual(len([p for p in paragraphs if p.startswith("![")]), 1, paragraphs)

    def test_a_caption_labelled_in_bold_without_punctuation_names_its_table(self):
        # The converter's own caption functions, on CLM's page 19: the layout
        # files Table 3's caption, "**Table 3** The four …", as body text
        # under the table, and Figure 15's as a caption under the next picture.
        source = web._PDF_CONVERTER
        start = source.index("# A caption opens with its number and punctuation")
        markdown = (
            "|Task|Metric|\n|---|---|\n|Needle|Recall|"
            "**Table 3** The four ContextBench tasks.\n\n"
            "**Figure 15** Context pressure of every level."
        )
        table_end = markdown.index("**Table 3**")
        caption_end = markdown.index("**Figure 15**")
        functions = {"re": re, "markdown": markdown, "boxes": [
            {"class": "table", "pos": (0, table_end), "bbox": (95, 68, 517, 157)},
            {"class": "text", "pos": (table_end, caption_end), "bbox": (70, 166, 542, 184)},
            {"class": "caption", "pos": (caption_end, len(markdown)), "bbox": (70, 365, 542, 397)},
        ]}
        exec(source[start:source.index("def monospace_share")], functions)
        self.assertEqual(functions["table_captions"]([0]), {0: "Table 3 The four ContextBench tasks."})

    def test_a_bold_caption_without_punctuation_is_its_visuals(self):
        paragraphs = [
            "The plot compares four runs.",
            "![](images/a.png)",
            "<!-- Start of picture text -->Accuracy<!-- End of picture text -->",
            "**Figure 6** CLMs outperform the summary harness.",
            "Figure 6 shows the gap.",
            "**Figure 16 Operations as the agent receives them.** Shortened where marked.",
            "![Table 4 Excerpts of each skill.](images/page-0019-table-1.png)",
            "<!-- Start of picture text -->|Method|Excerpt|<!-- End of picture text -->",
            "**Table 4** Excerpts of each skill.",
            "The skills differ in mechanism.",
        ]
        kinds = web._layout_kinds(paragraphs)
        # A bold label opens a caption; a sentence about a figure does not.
        self.assertEqual([kinds[3], kinds[4], kinds[5], kinds[8]], ["caption", "prose", "caption", "caption"])
        # Between two captions, a table takes the one its picture is named
        # after, not Figure 16's printed above it (CLM, page 19).
        batches = web.paper_batches(paragraphs, 1)
        self.assertEqual(batches, [(1, 1), (2, 4), (5, 5), (6, 6), (7, 9), (10, 10)])
        self.assertEqual(web.visual_label(paragraphs[6:9], kinds[6:9]), "Table 4")


    def test_a_listing_cut_by_a_page_or_a_table_stays_whole_and_takes_no_sentence(self):
        def joined(*pages):
            return web.split_paper_paragraphs(web.join_pdf_pages(list(pages))[0])

        table = "Table 7: Components of the reference harness.\n\n![Table 7](images/t.png)"
        # Harness-Zero, Code 3: a recipe runs over a page break, with Table 7
        # printed at the top of the next page above its second half.
        self.assertEqual(joined(
            "Code 3 shows the recipe.\n\n```\ncat > tool.py <<'PY'\nimport sys\n```",
            f"{table}\n\n```\nprint('PASS')\nPY\n```\n\nThe recipe then runs the check.",
        ), [
            "Code 3 shows the recipe.",
            "```\ncat > tool.py <<'PY'\nimport sys\nprint('PASS')\nPY\n```",
            "Table 7: Components of the reference harness.",
            "![Table 7](images/t.png)",
            "The recipe then runs the check.",
        ])
        # A listing ending a page, or a sentence left open before one, is
        # never joined with what follows, even a listing whose language tag
        # starts in lowercase.
        self.assertEqual(joined(
            "The recipe prints:\n\n```\nPASS when nothing\n```",
            "matters for every command.",
        ), [
            "The recipe prints:",
            "```\nPASS when nothing\n```",
            "matters for every command.",
        ])
        self.assertEqual(joined(
            "Each call starts a fresh shell, so the\n\nTable 2: Tools.\n\n|a|b|\n|---|---|\n|1|2|"
            "\n\n```bash\nls -la\n```",
        ), [
            "Each call starts a fresh shell, so the",
            "Table 2: Tools.",
            "|a|b|\n|---|---|\n|1|2|",
            "```bash\nls -la\n```",
        ])
        # Harness-Zero, Section 4.1: Table 1 cuts a sentence whose second
        # half opens in code.
        self.assertEqual(joined(
            "We merge the official train split and hold out the 168"
            "\n\nTable 1: Results.\n\n|a|b|\n|---|---|\n|1|2|\n\n"
            "`test_normal` tasks, grouped into scenarios.",
        )[0], "We merge the official train split and hold out the 168 "
              "`test_normal` tasks, grouped into scenarios.")

    def test_figures_reach_the_model_whole(self):
        paragraphs = [
            "The output is a weighted sum of the values.",
            "# Scaled Dot-Product Attention",
            "![](images/a.png)",
            "<!-- Start of picture text -->MatMul<br>SoftMax<!-- End of picture text -->",
            "# Multi-Head Attention",
            "![](images/b.png)",
            "Figure 2: (left) Scaled Dot-Product Attention.",
            "## 3.2.2 Multi-Head Attention",
            "Table 1: Maximum path lengths.",
            "|Layer|Complexity|\n|---|---|\n|Self-Attention|O(n2 d)|",
            "Instead of one attention function, we use several.",
        ]
        # Each figure or table is one request, with its panel titles, labels,
        # and caption, whether the caption sits below or above it.
        self.assertEqual(
            web.paper_batches(paragraphs, 1),
            [(1, 1), (2, 7), (8, 8), (9, 10), (11, 11)],
        )
        # Larger batches still never split one, and each figure or table is a
        # batch of its own, so its narration is its description alone, which
        # can be pinned; the prose around it still shares batches.
        self.assertEqual(
            web.paper_batches(paragraphs, 4), [(1, 1), (2, 7), (8, 8), (9, 10), (11, 11)]
        )
        self.assertEqual(web.paper_batches([
            "Self-attention relates positions.",
            "It connects them in a constant number of steps.",
            "Table 1: Maximum path lengths.",
            "|Layer|Complexity|\n|---|---|\n|Self-Attention|O(n2 d)|",
            "Recurrent layers take n steps.",
            "Convolutions sit between.",
        ], 4), [(1, 2), (3, 4), (5, 6)])
        # A numbered section heading above a figure is not one of its titles,
        # and labelled equations without a caption stay apart.
        self.assertEqual(web.paper_batches([
            "## 3 Model Architecture",
            "![](images/c.png)",
            "Figure 1: The Transformer.",
            "# Intelligence of a system (sufficient case):",
            "![](images/sufficient.png)",
            "# Intelligence of a system (optimal case):",
            "![](images/optimal.png)",
        ], 1), [(1, 1), (2, 3), (4, 5), (6, 7)])
        # Procedural Graphs, page 3: an equation printed inside a sentence
        # goes to the model with both halves, so the sentence is read through.
        self.assertEqual(web.paper_batches([
            "The design mirrors a familiar structure.",
            "Formally, a Procedural Graph is a directed, attributed graph",
            "![](images/g.png)",
            "<!-- Start of picture text -->G = (V, R, E)<!-- End of picture text -->",
            "where V is the set of abstract nodes and each edge is a triplet",
            "![](images/e.png)",
            "stating that one node may follow another.",
            "Each node abstracts a tool action.",
        ], 1), [(1, 1), (2, 7), (8, 8)])

    def test_a_lone_title_over_a_figure_its_caption_does_not_name_is_a_section(self):
        # Attention Is All You Need, page 13: the appendix heading sits right
        # above Figure 3, whose caption says nothing of it.
        kinds = web._layout_kinds([
            "# **Attention Visualizations**",
            "![](images/p13.png)",
            "Figure 3: An example of the attention mechanism following dependencies.",
            "# Scaled Dot-Product Attention",
            "![](images/p4.png)",
            "Figure 2: Scaled Dot-Product Attention.",
            "# Scaled Dot-Product Attention",
            "![](images/p4-1.png)",
            "# Multi-Head Attention",
            "![](images/p4-2.png)",
            "Figure 2: Two attention mechanisms side by side.",
        ])
        # A title the caption repeats, and the titles of a figure with
        # several panels, stay panel titles.
        self.assertEqual(kinds, [
            "heading", "image", "caption",
            "panel", "image", "caption",
            "panel", "image", "panel", "image", "caption",
        ])

    def test_each_request_lists_the_acronyms_the_author_defined_earlier(self):
        paragraphs = [
            "Recurrent neural networks, RNNs, run on GPUs.",
            "We trained on the **Wall Street Journal** (WSJ) portion of the Penn Treebank.",
            "Words use byte-pair encoding (BPE), as in prior work (Sennrich, 2016).",
            # Parentheses whose words do not spell the letters define nothing.
            "We report scores on the test split (FT) and in a table (Table 4).",
        ]
        self.assertEqual(web.defined_acronyms(paragraphs), [
            "WSJ (Wall Street Journal)", "BPE (byte pair encoding)",
        ])

        # Each request lists those defined before it, whichever batch runs first.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text("\n\n".join(paragraphs), encoding="utf-8")
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        requests = {}

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                requests[request_path.stem] = request_path.read_text(encoding="utf-8")
                return "<NARRATION>Narrated.</NARRATION><SUMMARY>Summary.</SUMMARY>"

        run = StubPaperRun(source, root / "prepared.txt", "utf-8", in_flight=4, prompt_path=prompt)
        run.pump()

        self.assertEqual(run.code, 0)
        self.assertNotIn("never expand", requests["paragraphs-2-2"])
        self.assertIn("never expand them again: WSJ (Wall Street Journal).", requests["paragraphs-3-3"])
        self.assertIn(
            "WSJ (Wall Street Journal), BPE (byte pair encoding).", requests["paragraphs-4-4"]
        )

    def test_browser_cookie_state_isolated_between_clients(self):
        web._DEVICE_OPTIONS = [{"value": "cpu", "label": "CPU"}]
        storage_temp = tempfile.TemporaryDirectory()
        self.addCleanup(storage_temp.cleanup)
        storage = web.SharedStorage(Path(storage_temp.name))
        storage.ensure()
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.jobs = web.JobQueue()
            server.tts_models = web.unconfigured_tts_models()
            server.verbose = False
            server.storage = storage
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            clients = [
                urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
                )
                for _ in range(2)
            ]

            def sync(client, language, description=""):
                state = normalize({"tab": "audiobook", "runtime": {"language": language}})
                state["voice"]["instruct"] = description
                request = urllib.request.Request(
                    f"{origin}/api/sync",
                    data=json.dumps({"state": state}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with client.open(request) as response:
                    return json.load(response)["state"]

            def load(client):
                with client.open(f"{origin}/api/state") as response:
                    return json.load(response)["state"]

            try:
                with clients[0].open(f"{origin}/api/state") as response:
                    public_state = json.load(response)
                # This test's browser is on the server's machine.
                self.assertEqual(
                    public_state["capabilities"]["manage_workers"], True
                )
                long_description = "".join(
                    hashlib.sha256(str(index).encode()).hexdigest()
                    for index in range(400)
                )
                sync(clients[0], "French", long_description)
                first = load(clients[0])
                self.assertEqual(first["runtime"]["language"], "French")
                self.assertEqual(first["voice"]["instruct"], long_description)
                self.assertEqual(load(clients[1])["runtime"]["language"], "Auto")
                sync(clients[1], "German")
                self.assertEqual(load(clients[0])["runtime"]["language"], "French")
                self.assertEqual(load(clients[1])["runtime"]["language"], "German")
                sync(clients[0], "French")
                self.assertEqual(load(clients[0])["voice"]["instruct"], "")
            finally:
                server.shutdown()
                thread.join()


class EventStreamTests(unittest.TestCase):
    def test_reconnect_resumes_after_last_received_event(self):
        class ManualRun(web.Run):
            def start(self):
                pass

        jobs = web.JobQueue()
        run = ManualRun([], "audiobook", "unused.mp3")
        record, created = jobs.reserve_audiobook(
            "document-version",
            "voice-version",
            "paper.txt",
            "Narrator",
            "paper-Narrator.mp3",
        )
        self.assertTrue(created)
        jobs.commit(record, run)
        run.publish("log", "first event\n")
        run.publish("progress", {"done": 1, "total": 2})
        run.close(130)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.jobs = jobs
            server.tts_models = web.unconfigured_tts_models()
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = urllib.request.Request(
                (
                    f"http://127.0.0.1:{server.server_port}/api/events"
                    f"?job={record['id']}"
                ),
                headers={"Last-Event-ID": "1"},
            )
            try:
                with urllib.request.urlopen(request) as response:
                    payload = response.read().decode("utf-8")
            finally:
                server.shutdown()
                thread.join()

        self.assertNotIn("first event", payload)
        self.assertIn("id: 2\nevent: progress", payload)
        self.assertIn("id: 3\nevent: done", payload)
        progress_block = next(
            block for block in payload.split("\n\n")
            if "\nevent: progress\n" in f"\n{block}\n"
        )
        progress = json.loads(next(
            line.removeprefix("data: ")
            for line in progress_block.splitlines()
            if line.startswith("data: ")
        ))
        self.assertGreaterEqual(progress["elapsed"], 0)
        self.assertGreaterEqual(progress["stream_elapsed"], progress["elapsed"])

    def test_joining_the_parts_reports_its_progress_to_the_page(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        count = 450
        tone = np.zeros(240, dtype=np.float32)
        for index in range(1, count + 1):
            sf.write(cli._checkpoint_path(root, index), tone, 24000, subtype="FLOAT")
        printed = io.StringIO()
        with redirect_stdout(printed):
            cli.assemble_checkpoints(root, count, root / "book.wav", "WAV", "PCM_16", None, sf)

        # Each line the narrator prints while joining reaches the page as
        # progress of its own step, from 0 to every part.
        run = web.Run([], "audiobook", "book.wav")
        for line in printed.getvalue().splitlines():
            run.inspect(line)
        steps = [
            (data["done"], data["total"]) for event, data in run.history
            if event == "progress" and data.get("unit") == "join"
        ]
        self.assertEqual(steps[0], (0, count))
        self.assertEqual(steps[-1], (count, count))
        # Often enough to show movement, not once per part.
        self.assertLessEqual(len(steps), 205)
        self.assertGreaterEqual(len(steps), 100)


class WebTtsConfigurationTests(unittest.TestCase):
    def test_browser_cannot_replace_server_owned_voice_model(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        design_model = root / "design-model"
        design_model.mkdir()
        state = normalize({
            "tab": "voice",
            "voice": {
                "model": {
                    "source": "hub",
                    "hub_id": "browser/injected-model",
                },
                "parent": str(root),
                "name": "narrator",
                "instruct": "A calm narrator.",
            },
        })

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.jobs = web.JobQueue()
            server.tts_models = {
                "design": {
                    "source": "local",
                    "model": str(design_model),
                    "allow_downloads": False,
                },
                "clone": {"source": "missing"},
            }
            server.storage = web.SharedStorage(root / "library")
            server.storage.ensure()
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/sync",
                data=json.dumps({"state": state}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request) as response:
                    payload = json.load(response)
            finally:
                server.shutdown()
                thread.join()

        self.assertNotIn("model", payload["state"]["voice"])
        self.assertNotIn("command", payload["derived"])
        self.assertIsNone(payload["derived"]["problem"])
        self.assertNotIn("browser/injected-model", json.dumps(payload))




class VoicePersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.voice = self.root / "narrator"
        self.waveform = np.linspace(-0.9, 0.9, 2400, dtype=np.float32)

    def test_reference_round_trip_preserves_audio_and_exact_transcript(self):
        transcript = "  Café beside the river.\r\nA second line.\n  "
        save_voice(self.voice, self.waveform, 24000, transcript, "FLOAT")

        moved = self.root / "moved-voice"
        self.voice.rename(moved)
        waveform, sample_rate, restored_text = read_voice(moved)

        self.assertEqual(sample_rate, 24000)
        self.assertEqual(restored_text, transcript)
        self.assertEqual((moved / "transcript.txt").read_bytes(), transcript.encode("utf-8"))
        np.testing.assert_array_equal(waveform, self.waveform)

    def test_existing_voice_requires_explicit_overwrite(self):
        self.voice.mkdir()

        with self.assertRaises(FileExistsError):
            save_voice(self.voice, self.waveform, 24000, "A reference.", "FLOAT")

        self.assertEqual(list(self.voice.iterdir()), [])

    def test_overwrite_replaces_saved_audio_and_transcript(self):
        save_voice(self.voice, self.waveform, 24000, "Original passage.", "FLOAT")
        notes = self.voice / "notes.txt"
        notes.write_text("Keep this user-owned file.", encoding="utf-8")
        replacement = np.linspace(0.8, -0.8, 1600, dtype=np.float32)

        save_voice(
            self.voice,
            replacement,
            22050,
            "Replacement passage.",
            "FLOAT",
            overwrite=True,
        )

        waveform, sample_rate, transcript = read_voice(self.voice)
        self.assertEqual(sample_rate, 22050)
        self.assertEqual(transcript, "Replacement passage.")
        np.testing.assert_array_equal(waveform, replacement)
        self.assertEqual(notes.read_text(encoding="utf-8"), "Keep this user-owned file.")

    def test_replacement_drops_description_and_preview_of_the_old_audio(self):
        save_voice(
            self.voice, self.waveform, 24000, "Original passage.", "FLOAT",
            description="Warm narrator.",
        )
        (self.voice / "preview.wav").write_bytes(b"preview of the original voice")

        save_voice(
            self.voice, self.waveform, 24000, "Second passage.", "FLOAT",
            overwrite=True, description="Bright narrator.",
        )
        self.assertEqual(
            (self.voice / "description.txt").read_text(encoding="utf-8"),
            "Bright narrator.",
        )
        self.assertFalse((self.voice / "preview.wav").exists())

        save_voice(
            self.voice, self.waveform, 24000, "Third passage.", "FLOAT", overwrite=True,
        )
        self.assertFalse((self.voice / "description.txt").exists())
        # Each replacement kept the voice it replaced, prompt and all.
        kept = self.voice / ".versions"
        self.assertEqual(sorted(path.name for path in kept.iterdir()), ["v1", "v2"])
        self.assertEqual(read_voice(kept / "v1")[2], "Original passage.")
        self.assertEqual(
            (kept / "v1" / "description.txt").read_text(encoding="utf-8"), "Warm narrator."
        )
        self.assertEqual(read_voice(kept / "v2")[2], "Second passage.")
        self.assertEqual(read_voice(self.voice)[2], "Third passage.")
        self.assertFalse((kept / "v1" / "preview.wav").exists())


class SharedLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = web.SharedStorage(self.root / "library")
        self.storage.ensure()

    def test_shared_assets_have_fixed_locations(self):
        self.assertEqual(self.storage.voices.name, "Voices")
        self.assertEqual(self.storage.audiobooks.name, "Audiobooks")
        self.assertEqual(self.storage.documents.name, "Documents")
        self.assertEqual(self.storage.in_progress.name, "in_progress")
        self.assertTrue(all(
            directory.is_dir()
            for directory in (
                self.storage.voices,
                self.storage.audiobooks,
                self.storage.documents,
                self.storage.in_progress,
            )
        ))

    def test_the_same_document_under_another_name_is_the_book_already_made(self):
        document = self.storage.documents / "Attention.txt"
        document.write_text("The shared source.", encoding="utf-8")
        renamed = self.storage.documents / "Attention New.txt"
        renamed.write_bytes(document.read_bytes())
        voice = self.storage.voices / "Narrator"
        save_voice(
            voice,
            np.linspace(-0.5, 0.5, 1200, dtype=np.float32),
            24000,
            "Reference passage.",
            "FLOAT",
        )
        model = self.root / "clone-model"
        model.mkdir()
        models = {
            "design": {"source": "missing"},
            "clone": {
                "source": "local",
                "model": str(model),
                "allow_downloads": False,
            },
        }
        book = store_book(
            self.storage, "Attention", web.file_version(document), "Narrator",
            narration={"schema": 1, "original_view": False, "passages": [
                {"id": 1, "type": "body", "page": 1, "text": "The shared source.",
                 "original_text": "", "unchanged": True, "paragraphs": [0, 0], "sources": []},
            ]},
            voice_fields={"voice_version": web.saved_voice_version(voice)},
        )
        state = normalize({
            "audiobook": {
                "document": renamed.name,
                "voice": voice.name,
                "adapt": True,
            },
        })

        # The name differs, the content is the book: no model, no new folder.
        facts = web.derived(state, models, self.storage)
        self.assertEqual(facts["existing_book"], {
            "id": book, "title": "Attention", "voices": ["Narrator"], "has_text": True,
        })
        self.assertIsNone(facts["problem"])
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.jobs = web.JobQueue()
            server.tts_models = models
            server.storage = self.storage
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/run",
                data=json.dumps({"state": state}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                payload = json.loads(caught.exception.read())
                # Confirmed, it is a voice of that book, never a new book.
                queued = []
                with mock.patch.object(
                    server.jobs, "commit",
                    lambda record, run: queued.append(run) or server.jobs._public(record),
                ):
                    confirmed = urllib.request.Request(
                        request.full_url,
                        data=json.dumps({"state": state, "confirmed": True}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    with urllib.request.urlopen(confirmed) as response:
                        job = json.load(response)["job"]
            finally:
                server.shutdown()
                thread.join()

        # The book already has this voice, so making it again is confirmed.
        self.assertEqual(caught.exception.code, 409)
        self.assertTrue(payload["confirmation_required"])
        self.assertEqual((job["book"], job["mode"]), (book, "voice"))
        self.assertEqual(queued[0].values["input"], str(self.storage.audiobooks / book / "narration.json"))
        self.assertEqual([path.name for path in self.storage.audiobooks.iterdir()], [book])

        renamed.write_text("A new source version.", encoding="utf-8")
        self.assertIsNone(web.derived(state, models, self.storage)["existing_book"])

    def test_two_requests_at_once_for_one_document_and_voice_make_one_job(self):
        document = self.storage.documents / "paper.txt"
        document.write_text("One short paper.", encoding="utf-8")
        voice = self.storage.voices / "Narrator"
        save_voice(
            voice, np.linspace(-0.5, 0.5, 1200, dtype=np.float32), 24000,
            "Reference passage.", "FLOAT",
        )
        model = self.root / "clone-model"
        model.mkdir()
        models = {
            "design": {"source": "missing"},
            "clone": {"source": "local", "model": str(model), "allow_downloads": False},
        }
        state = normalize({"audiobook": {"document": document.name, "voice": voice.name, "adapt": False}})
        real_prepare = web.AudiobookRun.prepare

        def slow_prepare(run):
            # The second request arrives while the first is still preparing.
            time.sleep(0.3)
            return real_prepare(run)

        answers = []
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, \
                mock.patch.object(web.AudiobookRun, "prepare", slow_prepare), \
                mock.patch.object(web.AudiobookRun, "start", lambda run: None):
            server.daemon_threads = True
            server.jobs = web.JobQueue()
            self.addCleanup(server.jobs.shutdown)
            server.tts_models = models
            server.storage = self.storage
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            together = threading.Barrier(2)

            def post():
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/run",
                    data=json.dumps({"state": state}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                together.wait()
                with urllib.request.urlopen(request) as response:
                    answers.append(json.load(response))

            try:
                posts = [threading.Thread(target=post) for _ in range(2)]
                for item in posts:
                    item.start()
                for item in posts:
                    item.join(10)
                queue = server.jobs.snapshot()
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual(len(answers), 2)
        self.assertEqual(len({answer["job"]["id"] for answer in answers}), 1)
        self.assertEqual(sorted(answer["duplicate"] for answer in answers), [False, True])
        self.assertEqual([job["id"] for job in queue], [answers[0]["job"]["id"]])

    def test_no_half_made_book_is_ever_visible(self):
        book = store_book(self.storage, voice="Martin")
        before = (self.storage.audiobooks / book / "book.json").read_bytes()
        real_write = web.write_json_atomic

        def crash_on_record(path, value):
            if Path(path).name == "book.json":
                raise OSError("the machine went down")
            return real_write(path, value)

        with mock.patch.object(web, "write_json_atomic", crash_on_record), \
                self.assertRaises(OSError):
            store_book(self.storage, voice="Sarah")
        with mock.patch.object(web, "write_json_atomic", crash_on_record), \
                self.assertRaises(OSError):
            store_book(self.storage, "Another", "b" * 64, "Sarah")
        self.assertEqual([path.name for path in self.storage.audiobooks.iterdir()], [book])
        self.assertEqual((self.storage.audiobooks / book / "book.json").read_bytes(), before)

        # A crash between a swap's two renames leaves the old folder aside, and
        # a build that never finished; the next start puts the old one back.
        aside = self.storage.audiobooks / f".replaced-{book}-0a1b2c3d"
        (self.storage.audiobooks / book).rename(aside)
        (self.storage.audiobooks / ".build-0123456789ab").mkdir()
        (self.storage.audiobooks / ".build-0123456789ab" / "book.json").write_text("{}")
        web.prepare_library(self.storage)
        self.assertEqual([path.name for path in self.storage.audiobooks.iterdir()], [book])
        self.assertEqual(web.library_catalog(self.storage)[0]["id"], book)

    def test_books_kept_in_the_earlier_layout_move_into_book_folders(self):
        audiobooks = self.storage.audiobooks
        (audiobooks / ".versions").mkdir()
        (audiobooks / ".readers").mkdir()
        mp3 = audiobooks / "Attention New-Eir.mp3"
        sf.write(mp3, np.zeros(4800, dtype=np.float32), 24000,
                 format="MP3", subtype="MPEG_LAYER_III")
        audio = mp3.read_bytes()
        source = "c" * 64
        markdown = (
            "<!-- audiobook-tts:block=0 -->\n\nFirst sentence.\n\n"
            "<!-- audiobook-tts:block=1 -->\n\nSecond sentence."
        )
        reader = "Attention New-Eir.mp3.0123456789abcdef"
        (audiobooks / ".readers" / f"{reader}.md").write_text(markdown)
        web.write_json_atomic(audiobooks / ".readers" / f"{reader}.json", {
            "schema": 3, "sample_rate": 24000, "duration_samples": 4800, "block_count": 2,
            "paragraphs": [0, 0],
            "cues": [{"block": 0, "start_sample": 0, "end_sample": 2400},
                     {"block": 1, "start_sample": 2400, "end_sample": 4800}],
            "word_timing": "unavailable", "word_cues": [],
            "originals": [{"paragraphs": [0, 0], "page": 1, "description": False,
                           "unchanged": False, "markdown": "First sentence, then the second."}],
        })
        web.write_json_atomic(audiobooks / ".versions" / f"{mp3.name}.json", {
            "schema": 3, "input_version": source, "voice_version": "v" * 64,
            "document": "Attention New.pdf", "voice": "Eir",
            "reader": {"markdown": f"{reader}.md", "sync": f"{reader}.json",
                       "audio_sha256": "d" * 64},
            "adaptation": {"model": "openai-codex/gpt-a", "prose": None},
        })
        # The prepared text the job stored is the book's text.
        (self.storage.documents / "Attention New-narration.txt").write_text(
            "First sentence. Second sentence.", encoding="utf-8"
        )
        # A very old MP3 with no record still plays.
        (audiobooks / "old-tale.mp3").write_bytes(audio)

        web.prepare_library(self.storage)
        layout = sorted(path.name for path in audiobooks.iterdir())
        # Starting again changes nothing.
        web.prepare_library(self.storage)
        self.assertEqual(sorted(path.name for path in audiobooks.iterdir()), layout)

        book = web.book_for_source(self.storage, source)
        self.assertEqual(book, "attention-new--cccccccccccc")
        books = {entry["id"]: entry for entry in web.library_catalog(self.storage)}
        self.assertEqual(len(books), 2)
        self.assertNotIn(".versions", layout)
        self.assertNotIn(".readers", layout)
        record, voice, played = web.book_audio(self.storage, book)
        self.assertEqual((voice, played.read_bytes()), ("Eir", audio))
        self.assertEqual(record["legacy_names"], ["Attention New-Eir.mp3"])
        self.assertEqual(record["chunk_max_chars"], 500)
        payload = web.audiobook_reader_payload(self.storage, book)
        self.assertEqual(len(payload["blocks"]), 2)
        self.assertIn("First sentence, then the second.", payload["originals"][0]["html"])
        # It can take a new voice: its text is the one its reader shows.
        self.assertTrue(payload["has_text"])
        narration, _ = web.read_narration(audiobooks / book)
        self.assertEqual(web.narration_text(narration), "First sentence. Second sentence.")
        [old] = [entry for entry in books.values() if entry["id"] != book]
        self.assertEqual(old["legacy_names"], ["old-tale.mp3"])
        self.assertEqual(web.book_audio(self.storage, old["id"])[2].read_bytes(), audio)

        # The earlier files stay aside until the book has played once.
        self.assertTrue((audiobooks / ".backup" / book / mp3.name).is_file())
        web.release_migration_backup(self.storage, book)
        self.assertFalse((audiobooks / ".backup" / book).exists())

    def test_job_identity_uses_only_document_and_voice_versions(self):
        first_document = self.storage.documents / "first-name.txt"
        second_document = self.storage.documents / "renamed.txt"
        first_document.write_text("Identical document.", encoding="utf-8")
        second_document.write_bytes(first_document.read_bytes())
        first_voice = self.storage.voices / "First Voice"
        second_voice = self.storage.voices / "Renamed Voice"
        save_voice(
            first_voice,
            np.linspace(-0.5, 0.5, 1200, dtype=np.float32),
            24000,
            "Reference passage.",
            "FLOAT",
        )
        second_voice.mkdir()
        for name in web.VOICE_FILES:
            (second_voice / name).write_bytes((first_voice / name).read_bytes())

        first_id = web.audiobook_job_id(
            web.file_version(first_document),
            web.saved_voice_version(first_voice),
        )
        renamed_id = web.audiobook_job_id(
            web.file_version(second_document),
            web.saved_voice_version(second_voice),
        )

        self.assertEqual(first_id, renamed_id)
        second_document.write_text("Changed document.", encoding="utf-8")
        self.assertNotEqual(
            first_id,
            web.audiobook_job_id(
                web.file_version(second_document),
                web.saved_voice_version(second_voice),
            ),
        )


class JobQueueTests(unittest.TestCase):
    class ControlledRun(web.Run):
        def __init__(self, name):
            super().__init__([], "audiobook", name)
            self.started = threading.Event()
            self.assigned_workers = []
            self.assigned_device = None

        def assign_workers(self, workers):
            self.assigned_workers = [dict(worker) for worker in workers]
            self.assigned_device = next(
                (
                    worker.get("device")
                    for worker in workers
                    if worker.get("kind") == "local"
                ),
                None,
            )

        def start(self):
            self.started.set()

        def stop(self):
            if not self.finished.is_set():
                self.close(130)

    def reserve(
        self,
        jobs,
        document_version,
        requested_device="auto",
        resolved_device=None,
    ):
        return jobs.reserve_audiobook(
            document_version,
            "voice-version",
            f"{document_version}.txt",
            "Narrator",
            f"{document_version}-Narrator.mp3",
            requested_device,
            resolved_device,
        )

    def test_queue_runs_fifo_deduplicates_and_cancels_pending_jobs(self):
        jobs = web.JobQueue()
        self.addCleanup(jobs.shutdown)
        first_record, created = self.reserve(jobs, "first")
        self.assertTrue(created)
        first = self.ControlledRun("first.mp3")
        self.assertEqual(jobs.commit(first_record, first)["status"], "running")
        self.assertTrue(first.started.wait(1))

        second_record, created = self.reserve(jobs, "second")
        self.assertTrue(created)
        second = self.ControlledRun("second.mp3")
        self.assertEqual(jobs.commit(second_record, second)["status"], "queued")
        duplicate, created = self.reserve(jobs, "second")
        self.assertFalse(created)
        self.assertIs(duplicate, second_record)

        third_record, created = self.reserve(jobs, "third")
        self.assertTrue(created)
        third = self.ControlledRun("third.mp3")
        jobs.commit(third_record, third)
        canceled = jobs.cancel(third_record["id"])
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(
            [job["id"] for job in jobs.snapshot()],
            [first_record["id"], second_record["id"]],
        )

        first.close(0)
        self.assertTrue(second.started.wait(1))
        self.assertIs(jobs.current_run(), second)
        second.close(0)

    def test_recent_lists_finished_jobs_newest_first_with_their_last_stage_and_count(self):
        jobs = web.JobQueue()
        self.addCleanup(jobs.shutdown)

        def finish(document_version, steps, code, result=None):
            record, _ = self.reserve(jobs, document_version)
            run = self.ControlledRun(f"{document_version}.mp3")
            jobs.commit(record, run)
            self.assertTrue(run.started.wait(1))
            steps(run)
            run.result = result or {}
            run.close(code)
            deadline = time.monotonic() + 2
            while record["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
            return record

        def failed_in_narration(run):
            run.set_phase("extraction", "Extraction")
            run.publish("progress", {"done": 12, "total": 12, "unit": "page"})
            run.publish("progress", {"done": 840, "total": 840, "unit": "paragraph"})
            run.set_phase("narration", "Narration")
            run.publish("progress", {"done": 5, "total": 20})

        def stopped_after_reusing_its_text(run):
            run.set_phase("extraction", "Extraction")
            run.publish("progress", {"done": 3, "total": 3, "unit": "page"})
            # "Using the text prepared earlier" is no count of what was done.
            run.publish("progress", {"done": 1, "total": 1, "unit": "document"})

        def aligned(run):
            run.set_phase("alignment", "Word alignment")
            run.publish("progress", {"done": 40, "total": 40, "unit": "sentence", "failed": 0})

        failed = finish("failed", failed_in_narration, 1)
        stopped = finish("stopped", stopped_after_reusing_its_text, 130)
        done = finish("done", aligned, 0, {"book": "made--0123456789ab", "voice": "Narrator"})
        # Voice design, a queued job canceled, and a running job are not listed.
        voice = self.ControlledRun("voice.wav")
        self.assertTrue(jobs.start_voice(voice))
        voice.close(0)
        deadline = time.monotonic() + 2
        while jobs.exclusive is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        running_record, _ = self.reserve(jobs, "running")
        jobs.commit(running_record, self.ControlledRun("running.mp3"))
        canceled_record, _ = self.reserve(jobs, "canceled")
        jobs.commit(canceled_record, self.ControlledRun("canceled.mp3"))
        jobs.cancel(canceled_record["id"])

        recent = jobs.recent_snapshot()
        for job in recent:
            self.assertRegex(job.pop("finished_at"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(recent, [
            {"id": done["id"], "document": "done.txt", "voice": "Narrator",
             "book": "made--0123456789ab", "mode": "create", "status": "done",
             "stage": "alignment", "progress": {"done": 40, "total": 40, "unit": "sentence"}},
            {"id": stopped["id"], "document": "stopped.txt", "voice": "Narrator", "book": "",
             "mode": "create", "status": "stopped",
             "stage": "extraction", "progress": {"done": 3, "total": 3, "unit": "page"}},
            {"id": failed["id"], "document": "failed.txt", "voice": "Narrator", "book": "",
             "mode": "create", "status": "failed",
             "stage": "narration", "progress": {"done": 5, "total": 20, "unit": None}},
        ])

    def test_auto_job_claims_all_gpu_workers_before_next_job(self):
        consumers = (
            {"id": "cuda:0", "device": "cuda:0", "label": "CUDA 0"},
            {"id": "cuda:1", "device": "cuda:1", "label": "CUDA 1"},
        )
        jobs = web.JobQueue(consumers)
        self.addCleanup(jobs.shutdown)

        first_record, _ = self.reserve(jobs, "first")
        first = self.ControlledRun("first.mp3")
        self.assertEqual(jobs.commit(first_record, first)["status"], "running")
        self.assertTrue(first.started.wait(1))
        self.assertEqual(
            [worker["device"] for worker in first.assigned_workers],
            ["cuda:0", "cuda:1"],
        )
        self.assertEqual(len(jobs.active_snapshots()), 1)
        self.assertEqual(
            {consumer["job_id"] for consumer in jobs.consumers_snapshot()},
            {first_record["id"]},
        )

        second_record, _ = self.reserve(jobs, "second")
        second = self.ControlledRun("second.mp3")
        self.assertEqual(jobs.commit(second_record, second)["status"], "queued")
        first.close(0)
        self.assertTrue(second.started.wait(1))
        self.assertEqual(
            [worker["device"] for worker in second.assigned_workers],
            ["cuda:0", "cuda:1"],
        )
        second.close(0)

    def test_auto_job_claims_local_and_ssh_workers(self):
        consumers = (
            {"id": "cuda:0", "device": "cuda:0", "label": "CUDA 0"},
            {
                "id": "ssh:spark-one",
                "kind": "ssh",
                "device": None,
                "label": "SSH spark-one — cuda:0",
                "worker": {
                    "kind": "ssh",
                    "target": "spark-one",
                    "device": "cuda:0",
                    "label": "SSH spark-one — cuda:0",
                },
            },
            {
                "id": "ssh:spark-two",
                "kind": "ssh",
                "device": None,
                "label": "SSH spark-two — cuda:0",
                "worker": {
                    "kind": "ssh",
                    "target": "spark-two",
                    "device": "cuda:0",
                    "label": "SSH spark-two — cuda:0",
                },
            },
        )
        jobs = web.JobQueue(consumers)
        self.addCleanup(jobs.shutdown)
        record, _ = self.reserve(jobs, "distributed")
        run = self.ControlledRun("distributed.mp3")

        self.assertEqual(jobs.commit(record, run)["status"], "running")
        self.assertTrue(run.started.wait(1))
        self.assertEqual(
            [
                worker.get("target")
                for worker in run.assigned_workers
                if worker["kind"] == "ssh"
            ],
            ["spark-one", "spark-two"],
        )
        self.assertEqual(
            {consumer["job_id"] for consumer in jobs.consumers_snapshot()},
            {record["id"]},
        )
        run.close(0)

    def test_pinned_job_waits_for_its_gpu_without_blocking_other_gpu(self):
        consumers = (
            {"id": "cuda:0", "device": "cuda:0", "label": "CUDA 0"},
            {"id": "cuda:1", "device": "cuda:1", "label": "CUDA 1"},
        )
        jobs = web.JobQueue(consumers)
        self.addCleanup(jobs.shutdown)

        first_record, _ = self.reserve(
            jobs, "first", requested_device="cuda:0", resolved_device="cuda:0"
        )
        first = self.ControlledRun("first.mp3")
        jobs.commit(first_record, first)
        second_record, _ = self.reserve(
            jobs, "second", requested_device="cuda:0", resolved_device="cuda:0"
        )
        second = self.ControlledRun("second.mp3")
        self.assertEqual(jobs.commit(second_record, second)["status"], "queued")

        third_record, _ = self.reserve(jobs, "third")
        third = self.ControlledRun("third.mp3")
        self.assertEqual(jobs.commit(third_record, third)["status"], "running")
        self.assertTrue(third.started.wait(1))
        self.assertEqual(third.assigned_device, "cuda:1")
        self.assertFalse(second.started.is_set())

        first.close(0)
        self.assertTrue(second.started.wait(1))
        self.assertEqual(second.assigned_device, "cuda:0")
        second.close(0)
        third.close(0)


class NarrationResumeTests(unittest.TestCase):
    def test_remote_narration_reuses_completed_audio_chunks(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "book.txt"
        source.write_text("First paragraph.\n\nSecond paragraph.", encoding="utf-8")
        buffer = io.BytesIO()
        sf.write(
            buffer,
            np.linspace(-0.2, 0.2, 800, dtype=np.float32),
            24000,
            format="WAV",
            subtype="PCM_16",
        )
        payload = buffer.getvalue()
        requests = {"count": 0}

        class SpeechHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests["count"] += 1
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), SpeechHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            args = argparse.Namespace(
                output=root / "book.wav",
                wav_subtype=None,
                mp3_compression_level=None,
                input=source,
                overwrite=True,
                resume_dir=root / "resume",
                chunk_max_chars=500,
                server=f"http://127.0.0.1:{server.server_port}/v1",
                server_model="tts-1",
                server_voice="alloy",
                language="Auto",
                api_key=None,
                server_timeout=5.0,
            )
            parser = argparse.ArgumentParser()
            try:
                cli.narrate_server(args, parser, source.read_text())
                first_request_count = requests["count"]
                cli.narrate_server(args, parser, source.read_text())
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual(first_request_count, 2)
        self.assertEqual(requests["count"], first_request_count)
        self.assertGreater(sf.info(args.output).frames, 0)


HEADING = "Pattern classification"
SENTENCE = (
    "Around the mid 1950s, it seemed that progress on connectionism had started to slow and "
    "would have perhaps tapered off had psychologist Frank Rosenblatt not made a striking discovery."
)


class ClipLengthTests(unittest.TestCase):
    """A narration clip that runs on (the model missing its end) is made again alone."""

    def test_a_clip_from_the_speech_server_that_runs_on_is_asked_for_again(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        inputs = []

        class SpeechHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                text = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["input"]
                inputs.append(text)
                # The heading's first clip runs on for 170 s.
                speech = speech_wav(text, 170 / len(text) if inputs.count(text) == 1 and text.startswith("Pattern") else 0.07)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(speech)))
                self.end_headers()
                self.wfile.write(speech)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SpeechHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        args = argparse.Namespace(
            output=root / "book.wav", wav_subtype=None, mp3_compression_level=None,
            input=None, overwrite=True, resume_dir=root / "resume", chunk_max_chars=500, sentence_chunks=True,
            server=f"http://127.0.0.1:{server.server_port}/v1", server_model="tts-1",
            server_voice="alloy", language="Auto", api_key=None, server_timeout=5.0,
        )
        text = f"{HEADING}\n\n{SENTENCE}"
        with mock.patch("sys.stdout", io.StringIO()) as log:
            cli.narrate_server(args, argparse.ArgumentParser(), text)
        # The heading went to the model with a period, and once more alone.
        self.assertEqual(inputs, [f"{HEADING}.", f"{HEADING}.", SENTENCE])
        self.assertIn("Chunk 1 ran on: 170.0 s for 22 characters", log.getvalue())
        self.assertAlmostEqual(sf.info(cli._checkpoint_path(args.resume_dir, 1)).duration, 1.61, places=1)

        # A run on from before this check, left in a resumed stage, is made again.
        sf.write(cli._checkpoint_path(args.resume_dir, 2), np.zeros(24000 * 300, dtype=np.float32), 24000)
        inputs.clear()
        with mock.patch("sys.stdout", io.StringIO()):
            cli.narrate_server(args, argparse.ArgumentParser(), text)
        self.assertEqual(inputs, [SENTENCE])
        self.assertLess(sf.info(args.output).duration, 20)

    def test_a_worker_clip_that_runs_on_is_made_again_alone_with_its_own_cap(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        record = root / "requests.jsonl"
        (root / "worker.py").write_text(f"""\
import base64, io, json, sys
import numpy, soundfile
protocol, record = sys.argv[1:3]
print(protocol + json.dumps({{"type": "ready"}}), flush=True)
seen = set()
for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "stop":
        break
    with open(record, "a") as stream:
        stream.write(json.dumps([request["texts"], request["max_new_tokens"]]) + "\\n")
    waves = []
    for text in request["texts"]:
        seconds = 170 if text == {HEADING!r} and text not in seen else len(text) * 0.07
        seen.add(text)
        buffer = io.BytesIO()
        soundfile.write(buffer, numpy.zeros(int(seconds * 8000), dtype="int16"), 8000, format="WAV", subtype="PCM_16")
        waves.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
    print(protocol + json.dumps({{"type": "result", "indexes": request["indexes"], "waves": waves}}), flush=True)
""", encoding="utf-8")
        checkpoints = root / "checkpoints"
        checkpoints.mkdir()
        command = [sys.executable, "-u", str(root / "worker.py"), cli.WORKER_PROTOCOL, str(record)]

        class Parser:
            @staticmethod
            def error(message):
                raise AssertionError(message)

        completed = set()
        with (
            mock.patch.object(cli, "_narration_worker_specifications", return_value=[("Node", command, None, None)]),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertTrue(cli._narrate_distributed(
                argparse.Namespace(batch_size=2), Parser, [HEADING, SENTENCE], root, checkpoints,
                completed, [1, 2], sf,
            ))
        requests = [json.loads(line) for line in record.read_text().splitlines()]
        # The batch was capped for its longest text; the heading, alone, for its own.
        # "1950s" is counted as spoken, each digit as about five characters.
        self.assertEqual(requests, [[[HEADING, SENTENCE], 519], [[HEADING], 79]])
        self.assertEqual(completed, {1, 2})
        self.assertAlmostEqual(sf.info(cli._checkpoint_path(checkpoints, 1)).duration, 1.54, places=2)

    def test_a_clip_that_keeps_running_on_keeps_the_try_nearest_its_length(self):
        lengths = iter([200.0, 9.0])
        made = []

        def remake(text):
            made.append(text)
            return np.zeros(int(next(lengths) * 100)), 100

        with mock.patch("sys.stdout", io.StringIO()) as log:
            waveform, rate = cli.checked_clip(7, HEADING, np.zeros(100 * 655), 100, remake)
        self.assertEqual(made, [HEADING, HEADING])
        self.assertEqual(len(waveform) / rate, 9.0)
        self.assertIn("Chunk 7 ran on: 9.0 s for 22 characters, about 1.5 s expected after 2 more tries; "
                      "keeping the one nearest its length (9.0 s)", log.getvalue())
        # A clip of the right length, or a short sentence, is not made again.
        self.assertIsNone(cli.clip_problem(SENTENCE, 12.0))
        self.assertIsNone(cli.clip_problem("Yes.", 0.1))
        self.assertIn("ended early", cli.clip_problem(SENTENCE, 1.1))

    def test_a_sentence_dense_with_numbers_gets_room_and_a_capped_try_is_kept_last(self):
        numbers = "The costs were 3.3 times 10 to the 18, 2.3 times 10 to the 19, and 1.8 times 10 to the 20."
        # Counted as spoken, so it is not cut at its last number.
        self.assertEqual(cli.spoken_length(numbers), len(numbers) + 4 * 18)
        self.assertIsNone(cli.clip_problem(numbers, 12.0))
        cap = cli.clip_token_limit([numbers]) / cli.CODEC_TOKENS_PER_SECOND
        lengths = iter([cap, 0.5])
        with mock.patch("sys.stdout", io.StringIO()):
            waveform, rate = cli.checked_clip(
                3, numbers, np.zeros(int(cap * 100)), 100, lambda text: (np.zeros(int(next(lengths) * 100)), 100)
            )
        # Every try failed: the one that ended on its own (too short) beats the
        # two that stopped at the cap, though those are nearer its length.
        self.assertEqual(len(waveform) / rate, 0.5)

class UnifiedWorkflowTests(unittest.TestCase):
    def test_pdf_extraction_flows_directly_into_shared_audiobook(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        storage = web.SharedStorage(root / "library")
        storage.ensure()
        document = storage.documents / "paper.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text(
                (72, 72),
                "A Short Paper\n\nThis paragraph should become spoken audio. "
                "Its second sentence needs its own cue.",
            )
            pdf.save(document)

        requests = {"count": 0}

        class SpeechHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests["count"] += 1
                length = int(self.headers.get("Content-Length") or 0)
                speech = speech_wav(json.loads(self.rfile.read(length))["input"])
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(speech)))
                self.end_headers()
                self.wfile.write(speech)

            def log_message(self, format, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), SpeechHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            models = {
                "design": {"source": "missing"},
                "clone": {
                    "source": "server",
                    "server": f"http://127.0.0.1:{server.server_port}/v1",
                    "server_model": "tts-1",
                },
            }
            state = normalize({
                "audiobook": {
                    "document": document.name,
                    "server_voice": "alloy",
                    "adapt": False,
                },
            })
            values = web.values_of(state, models, storage)
            input_version, voice_version = web.audiobook_versions(values)
            run = web.AudiobookRun(
                values,
                storage,
                input_version,
                voice_version,
                web.audiobook_job_id(input_version, voice_version),
            )
            try:
                with mock.patch.object(web, "ForcedWordAligner", FakeWordAligner):
                    run.pump()
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual(run.code, 0)
        book = run.result["book"]
        output = storage.audiobooks / book / "voices" / "alloy" / "audio.mp3"
        self.assertEqual(run.artifact, str(output))
        self.assertTrue(output.is_file())
        self.assertGreater(sf.info(output).frames, 0)
        # The book keeps its own text; Documents gets no narration file.
        self.assertEqual([path.name for path in storage.documents.iterdir()], ["paper.pdf"])
        self.assertIn("spoken audio", web.narration_text(json.loads(
            (storage.audiobooks / book / "narration.json").read_text(encoding="utf-8")
        )))
        self.assertGreater(requests["count"], 0)
        self.assertFalse(run.stage.exists())
        reader = web.audiobook_reader_payload(storage, book)
        self.assertEqual((reader["book"], reader["voice"]), (book, "alloy"))
        self.assertEqual(len(reader["cues"]), requests["count"])
        self.assertEqual(
            len({cue["block"] for cue in reader["cues"]}),
            requests["count"],
        )
        self.assertEqual(reader["word_timing"], "aligned")
        self.assertGreater(len(reader["word_cues"]), requests["count"])
        self.assertIn(
            "spoken audio",
            " ".join(block["html"] for block in reader["blocks"]),
        )
        # Both cued sentences of the body paragraph render in one paragraph.
        paragraph_of = {
            text: next(
                block["paragraph"]
                for block in reader["blocks"]
                if text in block["html"]
            )
            for text in ("spoken audio", "its own cue")
        }
        self.assertEqual(
            paragraph_of["spoken audio"], paragraph_of["its own cue"]
        )
        self.assertEqual(
            [
                data["phase"]
                for event, data in run.history
                if event == "phase"
            ],
            ["extraction", "narration", "alignment"],
        )
        narration = [
            (data.get("unit"), data["done"])
            for event, data in run.history
            if event == "progress" and data["phase"] == "narration"
        ]
        # Committed chunks drive the narration's progress; joining them into
        # one file afterwards is a step of its own.
        self.assertEqual(
            [done for unit, done in narration if unit != "join"],
            list(range(1, requests["count"] + 1)),
        )
        self.assertEqual(narration[-1], ("join", requests["count"]))
        # The end of the run reports how long it took, as the log does.
        done = [data for event, data in run.history if event == "done"][-1]
        self.assertRegex(
            done["total_time"],
            r"^\d+s \(\d+\.\d s\): reading \d+s(, adapting \d+s)?, narrating \d+s, aligning \d+s$",
        )
        self.assertIn(
            f"Total time: {done['total_time']}.\n",
            [data for event, data in run.history if event == "log"],
        )

    def test_total_time_rounds_to_whole_seconds_and_lists_only_timed_stages(self):
        self.assertEqual(
            web.total_time_summary({"total": 3723.4, "narrating": 3600.0, "reading": 59.6}),
            "1h 02m 03s (3723.4 s): reading 1m 00s, narrating 1h 00m 00s",
        )
        self.assertEqual(web.total_time_summary({"total": 4.0}), "4s (4.0 s)")

    def test_each_book_records_its_model_prose_kept_and_times_across_a_resume(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        storage = web.SharedStorage(root / "library")
        storage.ensure()
        document = storage.documents / "paper.md"
        document.write_text(
            "Attention maps a query together with a set of key and value pairs onto an output vector.\n\n"
            "The output is computed as a weighted sum of the values, where each "
            "weight comes from comparing the query with its key.",
            encoding="utf-8",
        )
        buffer = io.BytesIO()
        sf.write(buffer, np.linspace(-0.2, 0.2, 1200, dtype=np.float32), 24000,
                 format="WAV", subtype="PCM_16")
        speech = buffer.getvalue()
        speech_fails = threading.Event()
        speech_fails.set()

        class SpeechHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if speech_fails.is_set():
                    self.send_error(500, "speech server is down")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(speech)))
                self.end_headers()
                self.wfile.write(speech)

            def log_message(self, format, *args):
                pass

        def echo(self, request_path, system_prompt, attachments=()):
            # A faithful model: it narrates each source paragraph as written.
            source = re.findall(
                r"<SOURCE_PARAGRAPH[^>]*>\n(.*?)\n</SOURCE_PARAGRAPH>",
                request_path.read_text(encoding="utf-8"), flags=re.DOTALL,
            )
            return f"<NARRATION>{chr(10).join(source)}</NARRATION><SUMMARY>Summary.</SUMMARY>"

        runs = []
        with ThreadingHTTPServer(("127.0.0.1", 0), SpeechHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            models = {
                "design": {"source": "missing"},
                "clone": {
                    "source": "server",
                    "server": f"http://127.0.0.1:{server.server_port}/v1",
                    "server_model": "tts-1",
                },
            }
            values = web.values_of(normalize({"audiobook": {
                "document": document.name, "server_voice": "alloy", "adapt": True,
                "model": "lm-studio/faithful",
                "local_servers": [{"server": "127.0.0.1:9", "provider": "lm-studio"}],
            }}), models, storage)
            input_version, voice_version = web.audiobook_versions(values)
            try:
                with mock.patch.object(web, "ForcedWordAligner", FakeWordAligner), \
                        mock.patch.object(web, "local_model_names", return_value=("faithful",)), \
                        mock.patch.object(web.PaperRun, "model_response", echo):
                    # The first attempt adapts the book, then narration fails;
                    # Continue reuses the adaptation.
                    for _ in range(2):
                        run = web.AudiobookRun(
                            values, storage, input_version, voice_version,
                            web.audiobook_job_id(input_version, voice_version),
                        )
                        run.pump()
                        runs.append(run)
                        speech_fails.clear()
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual([run.code for run in runs], [1, 0])
        book = runs[-1].result["book"]
        self.assertRegex(book, rf"^paper--{input_version[:12]}$")
        record = json.loads((storage.audiobooks / book / "book.json").read_text(encoding="utf-8"))
        # What made the text, so a later Hilde knows which books it would change.
        self.assertEqual(record["source_sha256"], input_version)
        self.assertEqual(record["source_filenames"], ["paper.md"])
        self.assertEqual(record["hilde_version"], web.HILDE_VERSION)
        self.assertEqual(record["schema_version"], web.EXTRACTION_SCHEMA)
        self.assertRegex(record["prompt_hash"], r"^[0-9a-f]{64}$")
        self.assertIn("git_commit", record)
        self.assertEqual(record["model"], "lm-studio/faithful")
        self.assertEqual(record["local_vision"], {"lm-studio/faithful": False})
        self.assertEqual(record["chunk_max_chars"], 500)
        _, narration_sha256 = web.read_narration(storage.audiobooks / book)
        self.assertEqual(record["narration_sha256"], narration_sha256)
        self.assertTrue((storage.audiobooks / book / "source.md").is_file())
        # Measured from the saved adaptation, though this run reused it.
        self.assertEqual(
            (record["prose"]["prose_passages"], record["prose"]["kept_95"]),
            (2, 2),
        )
        # The times are this run's: it adapted nothing, then made the voice.
        self.assertEqual(record["seconds"], {})
        [voice] = record["voices"]
        self.assertEqual((voice["name"], voice["status"]), ("alloy", "ready"))
        self.assertEqual(voice["narration_sha256"], narration_sha256)
        self.assertEqual(set(voice["seconds"]), {"narrating", "aligning"})
        self.assertGreater(voice["duration"], 0)

    def test_a_new_voice_reads_the_books_own_text_and_a_remake_leaves_old_voices_stale(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name) / "library")
        storage.ensure()
        document = storage.documents / "paper.md"
        document.write_text(
            "# A Short Paper\n\nAttention maps a query to an output. It weighs the values.\n\n"
            "Recurrent models read tokens one at a time.",
            encoding="utf-8",
        )
        speech = {}
        for voice, frames in (("alloy", 1200), ("echo", 2000)):
            buffer = io.BytesIO()
            sf.write(buffer, np.linspace(-0.2, 0.2, frames, dtype=np.float32), 24000,
                     format="WAV", subtype="PCM_16")
            speech[voice] = buffer.getvalue()
        heard = []

        class SpeechHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                heard.append((body["voice"], body["input"]))
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(speech[body["voice"]])))
                self.end_headers()
                self.wfile.write(speech[body["voice"]])

            def log_message(self, format, *args):
                pass

        model_calls = []
        wording = {"suffix": ""}

        def faithful(self, request_path, system_prompt, attachments=()):
            model_calls.append(request_path.stem)
            source = re.findall(
                r"<SOURCE_PARAGRAPH[^>]*>\n(.*?)\n</SOURCE_PARAGRAPH>",
                request_path.read_text(encoding="utf-8"), flags=re.DOTALL,
            )
            text = "\n".join(source).replace("time.", f"time{wording['suffix']}.")
            return f"<NARRATION>{text}</NARRATION><SUMMARY>Summary.</SUMMARY>"

        with ThreadingHTTPServer(("127.0.0.1", 0), SpeechHandler) as speech_server:
            threading.Thread(target=speech_server.serve_forever, daemon=True).start()
            models = {
                "design": {"source": "missing"},
                "clone": {
                    "source": "server",
                    "server": f"http://127.0.0.1:{speech_server.server_port}/v1",
                    "server_model": "tts-1",
                },
            }
            state = normalize({"tab": "audiobook", "audiobook": {
                "document": document.name, "server_voice": "alloy", "adapt": True,
                "model": "lm-studio/faithful",
                "local_servers": [{"server": "127.0.0.1:9", "provider": "lm-studio"}],
            }})
            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, \
                    mock.patch.object(web, "ForcedWordAligner", FakeWordAligner), \
                    mock.patch.object(web, "local_model_names", return_value=("faithful",)), \
                    mock.patch.object(web.PaperRun, "model_response", faithful):
                server.daemon_threads = True
                server.jobs = web.JobQueue()
                server.tts_models = models
                server.storage = storage
                server.verbose = False
                threading.Thread(target=server.serve_forever, daemon=True).start()

                def run(**extra):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/run",
                        data=json.dumps({"state": state, **extra}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    with urllib.request.urlopen(request) as response:
                        job = json.load(response)["job"]
                    finished = server.jobs.run_for(job["id"])
                    self.assertTrue(finished.finished.wait(120))
                    self.assertEqual(finished.code, 0, "".join(
                        str(data) for event, data in finished.history if event == "log"
                    ))
                    return finished

                try:
                    book = run().result["book"]
                    path = storage.audiobooks / book
                    narration = (path / "narration.json").read_bytes()
                    alloy_audio = (path / "voices" / "alloy" / "audio.mp3").read_bytes()
                    calls = len(model_calls)
                    text = web.narration_text(json.loads(narration))

                    voiced = run(book=book, mode="voice", voice="echo")
                    self.assertEqual(voiced.result["book"], book)
                    # The new voice read the book's text; no model was asked.
                    self.assertEqual(len(model_calls), calls)
                    self.assertEqual((path / "narration.json").read_bytes(), narration)
                    self.assertEqual(
                        " ".join(chunk for voice, chunk in heard if voice == "echo"),
                        " ".join(chunk for voice, chunk in heard if voice == "alloy"),
                    )
                    self.assertIn("Recurrent models", text)
                    # Each voice keeps its own audio and its own timings.
                    self.assertEqual((path / "voices" / "alloy" / "audio.mp3").read_bytes(), alloy_audio)
                    timings = {
                        voice: json.loads((path / "voices" / voice / "timings.json").read_text())
                        for voice in ("alloy", "echo")
                    }
                    self.assertEqual(timings["alloy"]["paragraphs"], timings["echo"]["paragraphs"])
                    self.assertNotEqual(
                        timings["alloy"]["duration_samples"], timings["echo"]["duration_samples"]
                    )
                    for voice in ("alloy", "echo"):
                        payload = web.audiobook_reader_payload(storage, book, voice)
                        self.assertEqual(
                            payload["cues"][-1]["end_sample"], timings[voice]["duration_samples"]
                        )

                    # Made again with the latest Hilde, read by alloy: the text
                    # changed, so echo read the earlier one.
                    wording["suffix"] = ", as before"
                    run(book=book, mode="recreate", voice="alloy")
                    self.assertGreater(len(model_calls), calls)
                    record = json.loads((path / "book.json").read_text())
                    _, narration_sha256 = web.read_narration(path)
                    self.assertNotEqual((path / "narration.json").read_bytes(), narration)
                    self.assertEqual(record["narration_sha256"], narration_sha256)
                    self.assertEqual(
                        {voice["name"]: voice["status"] for voice in record["voices"]},
                        {"alloy": "ready", "echo": "stale"},
                    )
                    with self.assertRaises(web.StaleVoiceError):
                        web.book_audio(storage, book, "echo")
                    self.assertEqual([item.name for item in storage.audiobooks.iterdir()], [book])
                finally:
                    server.shutdown()
            speech_server.shutdown()

    def test_pdf_figures_reach_reader_and_model_with_the_library_under_the_project(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 72), "A paragraph before the figure.")
            figure = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
            figure.clear_with(200)
            page.insert_image(pymupdf.Rect(72, 120, 372, 420), pixmap=figure)
            pdf.save(source)
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        stage = root / "User" / "in_progress" / "job" / "extraction"
        attached = []

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                if request_path.stem.endswith("-kind"):
                    return "OTHER"
                attached.extend(attachments)
                return "<NARRATION>Narrated.</NARRATION><SUMMARY>Summary.</SUMMARY>"

        # The server runs converters from the project folder, which holds User/.
        with mock.patch.object(web, "ROOT", root):
            run = StubPaperRun(
                source, stage / "prepared.txt", "utf-8", in_flight=1,
                prompt_path=prompt, scratch_path=stage,
            )
            run.pump()

        self.assertEqual(run.code, 0)
        images = sorted((stage / "images").iterdir())
        self.assertEqual(len(images), 1)
        self.assertEqual(attached, images)
        markdown = (stage / "document.md").read_text(encoding="utf-8")
        self.assertIn(
            "](data:image/png;base64,", web.embed_reader_images(markdown, (stage,))
        )

    def test_split_sentence_and_its_figure_reach_the_model_whole(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        # Laid out like Attention Is All You Need, pages 3 and 4: a sentence
        # breaks off at the foot of a page, and the next opens with a figure.
        source = root / "paper.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 72), "Attention maps a query and key-value pairs to an output.")
            page.insert_text((72, 700), "The output is computed as a weighted sum")
            page = pdf.new_page()
            figure = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
            figure.clear_with(200)
            page.insert_image(pymupdf.Rect(72, 72, 372, 372), pixmap=figure)
            page.insert_text((72, 400), "Figure 2: Scaled Dot-Product Attention.")
            page.insert_text((72, 460), "of the values, where each weight comes from a key.")
            pdf.save(source)
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        stage = root / "extraction"
        requests = []

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                if request_path.stem.endswith("-kind"):
                    return "OTHER"
                requests.append((request_path.read_text(encoding="utf-8"), attachments))
                return "<NARRATION>Narrated.</NARRATION><SUMMARY>Summary.</SUMMARY>"

        run = StubPaperRun(
            source, stage / "prepared.txt", "utf-8", in_flight=1,
            prompt_path=prompt, scratch_path=stage,
        )
        run.pump()

        self.assertEqual(run.code, 0)
        # What a request asks the model to narrate: its source, not the
        # author's text about a figure that comes with the figure.
        sources = [
            "".join(re.findall(r"<SOURCE_PARAGRAPH>(.*?)</SOURCE_PARAGRAPH>", text, re.S))
            for text, _ in requests
        ]
        self.assertEqual(sum(
            "The output is computed as a weighted sum of the values, where each "
            "weight comes from a key." in source
            # A second request for the same batch (prose narrated short, or a
            # dropped "each" the check found) is not another narration of it.
            for source, (text, _) in zip(sources, requests)
            if "reworded these sentences" not in text and "A check of your narration" not in text
        ), 1)
        # The request that sends the figure also holds its caption, and no
        # other request does.
        [figure_request] = [text for text, attached in requests if attached]
        self.assertIn("Figure 2: Scaled Dot-Product Attention.", figure_request)
        self.assertEqual(sum("Figure 2:" in source for source in sources), 1)
        # No paragraph mentions Figure 2, so it is told none of the prose
        # beside it, which may be anything, such as Acknowledgements.
        self.assertNotIn("<AUTHOR_CONTEXT>", figure_request)

    def test_error_correction_keeps_a_correction_only_when_its_check_holds(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text(
            "Caching makes serving cheaper.\n\n"
            "Table 4: The check each skill runs.\n\n"
            "|Method|Check|\n|---|---|\n"
            "|CLM|No note, or a token count that did not drop, means the batch is still in your context.|\n\n"
            "The skills differ in mechanism.",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        # CLM's Table 4: a failure condition turned into one for success.
        wrong = "Table 4 shows that success is confirmed if a note appears."
        right = "Table 4 shows that no note, or a token count that did not drop, means the batch is still in context."

        def made(name, verdict, verify=True):
            calls = []

            class StubPaperRun(PaperRun):
                def model_response(self, request_path, system_prompt, attachments=(), model=None):
                    text = request_path.read_text(encoding="utf-8")
                    calls.append((request_path.stem, model))
                    if request_path.stem.endswith("-check"):
                        return (
                            '<ISSUES>\n- Narration: "success is confirmed if a note appears" | Source: "No note" '
                            "| Problem: a failure condition became one for success.\n</ISSUES>"
                            if wrong in text else "<ISSUES>none</ISSUES>"
                        )
                    if request_path.stem.endswith("-recheck"):
                        return verdict
                    rewrite = "A check of your narration of this batch against its source found" in text
                    narration = right if rewrite else wrong if "Table 4:" in text else "Narrated prose."
                    return f"<NARRATION>{narration}</NARRATION><SUMMARY>Summary.</SUMMARY>"

            stage = root / name
            run = StubPaperRun(
                source, stage / "prepared.txt", "utf-8", "lm-studio/writer",
                [{"server": "127.0.0.1:9", "provider": "lm-studio"}], in_flight=1,
                prompt_path=prompt, scratch_path=stage, verify=verify, verify_model="lm-studio/checker",
            )
            with mock.patch.object(web, "local_model_names", return_value=("checker", "writer")):
                run.pump()
            self.assertEqual(run.code, 0)
            return (stage / "prepared.txt").read_text(encoding="utf-8"), calls, run.verification

        # The second check finds the problem fixed and nothing new: the correction is read.
        text, calls, counts = made("held", "<RESULT>\n1: FIXED | the condition is restored.\nNEW: none\n</RESULT>")
        self.assertIn(right, text)
        self.assertNotIn(wrong, text)
        self.assertEqual(counts, {"checked": 1, "flagged": 1, "corrected": 1})
        # Only the table is checked, and only by the checking model; the writer
        # writes the correction.
        self.assertEqual(
            [model for stem, model in calls if stem.endswith(("-check", "-recheck"))],
            ["lm-studio/checker", "lm-studio/checker"],
        )
        self.assertTrue(all(model is None for stem, model in calls if not stem.endswith(("-check", "-recheck"))))
        # The correction brings a new problem: the first description stays.
        text, _, counts = made("rejected", (
            '<RESULT>\n1: FIXED | restored.\nNEW: Revision: "batch is still in context" '
            '| Source: "your context" | Problem: invented.\n</RESULT>'
        ))
        self.assertIn(wrong, text)
        self.assertEqual(counts["corrected"], 0)
        # Unticked, nothing is checked.
        text, calls, _ = made("off", "", verify=False)
        self.assertIn(wrong, text)
        self.assertFalse([stem for stem, _ in calls if stem.endswith("-check")])

    def test_panel_titles_reach_the_model_as_titles_not_sections(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text(
            "The output is a weighted sum of the values.\n\n"
            "# Scaled Dot-Product Attention\n\n![](images/a.png)\n\n"
            "Figure 2: Scaled Dot-Product Attention.\n\n"
            "# 3.2.2 Multi-Head Attention\n\n"
            "Instead of one attention function, we use several.",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        requests = {}

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                requests[request_path.stem] = request_path.read_text(encoding="utf-8")
                return "<NARRATION>Narrated.</NARRATION><SUMMARY>Summary.</SUMMARY>"

        run = StubPaperRun(
            source, root / "prepared.txt", "utf-8", in_flight=1, prompt_path=prompt
        )
        run.pump()

        self.assertEqual(run.code, 0)
        # Extraction writes a panel title as a heading; the model must not
        # read it out as a section of its own.
        figure = requests["paragraphs-2-4"]
        self.assertIn("Panel title: Scaled Dot-Product Attention", figure)
        self.assertNotIn("# Scaled Dot-Product Attention", figure)
        # The numbered heading after it is read as printed, without the model.
        self.assertNotIn("paragraphs-5-5", requests)

    def test_an_equation_inside_a_sentence_reaches_the_model_as_part_of_it(self):
        equation = [
            "![](images/e.png)",
            "<!-- Start of picture text -->\nU = E (w<sup>γ</sup>/γ) (2)\n<!-- End of picture text -->",
        ]
        # Merton's "the investor's total expected utility is [Equation 2] where…":
        # described on its own, it was spliced in as "…is\n\nEquation 2 says…".
        inside = ["The investor's total expected utility is", *equation, "where γ < 1 sets the risk aversion."]
        standing = ["The utility is defined as follows:", *equation, "The parameter β trades off the two."]
        for paragraphs, marked in ((inside, True), (standing, False)):
            shown = web.model_paragraphs(paragraphs, web._layout_kinds(paragraphs))
            self.assertEqual(shown[2].startswith(web.INLINE_EQUATION_NOTE), marked, shown)
            self.assertEqual(shown[:2] + shown[3:], paragraphs[:2] + paragraphs[3:])

    def test_a_numbered_heading_is_read_as_printed_without_the_model(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        # RRSI's own headings, which a model rendered three ways in one book.
        source.write_text(
            "# **3.1. A Regularization View**\n\n"
            "Harness evolution is a search.\n\n"
            "# **B. Baseline Methods**\n\n"
            "We compare against three baselines.\n\n"
            "# **Limitations**\n\n"
            "# A Short Paper",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        requests = []

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                requests.append(request_path.stem)
                return "<NARRATION>Narrated.</NARRATION><SUMMARY>Summary.</SUMMARY>"

        run = StubPaperRun(
            source, root / "prepared.txt", "utf-8", in_flight=1,
            paragraphs_per_worker=4, prompt_path=prompt,
        )
        run.pump()

        self.assertEqual(run.code, 0)
        # Each numbered heading is a batch of its own, read as printed; an
        # unnumbered one, or a title opening with "A", goes to the model.
        self.assertEqual(requests, ["paragraphs-2-2", "paragraphs-4-6"])
        self.assertEqual(
            web.split_paper_paragraphs((root / "prepared.txt").read_text(encoding="utf-8")),
            ["3.1. A Regularization View", "Narrated.", "B. Baseline Methods", "Narrated."],
        )

    def test_a_description_that_does_not_say_what_it_describes_is_logged(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text(
            "The model has an encoder and a decoder.\n\n"
            "![](images/architecture.png)\n\nFigure 1: The Transformer.\n\n"
            "Attention weighs the values.\n\n"
            "![](images/attention.png)\n\n"
            "The paper ends with its logo.\n\n"
            "![](images/logo.png)",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        answers = {
            "paragraphs-1-1": "The model has an encoder and a decoder.",
            "paragraphs-2-3": "Figure 1 shows the Transformer, an encoder beside a decoder.",
            "paragraphs-4-4": "Attention weighs the values.",
            # An equation printed as an image, described without saying so.
            "paragraphs-5-5": "Attention is the softmax of the scaled scores times the values.",
            "paragraphs-6-6": "The paper ends with its logo.",
            # A figure the model leaves out adds nothing to check.
            "paragraphs-7-7": "",
        }

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                if request_path.stem.endswith("-kind"):
                    return "OTHER"
                narration = answers[request_path.stem]
                return f"<NARRATION>{narration}</NARRATION><SUMMARY>Summary.</SUMMARY>"

        run = StubPaperRun(
            source, root / "prepared.txt", "utf-8", in_flight=1, prompt_path=prompt
        )
        run.pump()

        self.assertEqual(run.code, 0)
        flagged = [
            str(data) for event, data in run.history
            if event == "log" and "does not open by naming" in str(data)
        ]
        self.assertEqual(len(flagged), 1)
        self.assertIn("Paragraph 5/7", flagged[0])

    def test_prose_that_lost_its_wording_is_logged_and_summarized(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        paragraphs = [
            "Recurrent models compute hidden states one position after another, "
            "which prevents parallel training within long training examples.",
            "Attention mechanisms have become an integral part of sequence models. "
            "They allow modeling dependencies without regard to their distance "
            "in the input or output sequences.",
            "The Transformer relies entirely on attention [12] to draw global "
            "dependencies<sup>3</sup> between input and output, as https://arxiv.org/abs/1706.03762 shows.",
            "See Figure 1.",
            "Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey Hinton. Layer normalization. "
            "arXiv preprint, July 2016.",
        ]
        source.write_text("\n\n".join(paragraphs), encoding="utf-8")
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        answers = {
            "paragraphs-1-1": paragraphs[0],
            # The model dropped the second sentence.
            "paragraphs-2-2": "Attention mechanisms have become an integral part of sequence models.",
            # Citation marks, a superscript, and a link are not the author's words.
            "paragraphs-3-3": "The Transformer relies entirely on attention to draw global "
                              "dependencies between input and output, as the paper shows.",
            "paragraphs-4-4": "The figure shows the model.",
            # A stray reference entry the model rightly leaves out whole.
            "paragraphs-5-5": "",
        }

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                return f"<NARRATION>{answers[request_path.stem]}</NARRATION><SUMMARY>S.</SUMMARY>"

        run = StubPaperRun(source, root / "prepared.txt", "utf-8", in_flight=1, prompt_path=prompt)
        run.pump()

        self.assertEqual(run.code, 0)
        logs = [str(data) for event, data in run.history if event == "log"]
        low = [line for line in logs if "of the author's words; missing" in line]
        self.assertEqual(len(low), 1)
        self.assertIn("Paragraph 2/5", low[0])
        self.assertIn("dependencies", low[0])
        # The short paragraph is too short to judge, and the reference entry
        # left out whole is counted apart rather than as a loss.
        self.assertEqual(
            {key: run.fidelity[key] for key in ("prose_passages", "kept_95", "below_80", "lowest_paragraph", "left_out")},
            {"prose_passages": 3, "kept_95": 2, "below_80": 1, "lowest_paragraph": 2, "left_out": 1},
        )
        self.assertTrue(any("2 of 3 narrated prose paragraphs kept" in line for line in logs))

    def test_pdf_figures_are_not_overprinted_with_ocr_text(self):
        import pymupdf
        import pymupdf4llm

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        # A diagram whose labels are pixels rather than text, so OCR reads them.
        with pymupdf.open() as sketch:
            page = sketch.new_page(width=300, height=160)
            for x, label in ((20, "Encoder"), (170, "Decoder")):
                page.draw_rect(
                    pymupdf.Rect(x, 50, x + 110, 100),
                    color=(0, 0, 0), fill=(0.85, 0.9, 1), width=1.5,
                )
                page.insert_text((x + 18, 81), label, fontsize=16)
            diagram = page.get_pixmap(dpi=150)
        source = root / "paper.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_textbox(
                pymupdf.Rect(72, 72, 540, 160),
                "The model has two parts, shown in the figure below.",
                fontsize=12,
            )
            page.insert_image(pymupdf.Rect(150, 200, 450, 360), pixmap=diagram)
            pdf.save(source)

        stage = root / "extraction"
        run = PaperRun(source, stage / "prepared.txt", "utf-8", adapt=False)
        markdown_path, images = run.convert_pdf(stage, source)
        # With OCR off, the figure is rendered exactly as the PDF draws it.
        plain = root / "plain"
        with mock.patch("sys.stdout", io.StringIO()):
            pymupdf4llm.to_markdown(
                str(source), pages=[0], write_images=True, image_path=str(plain),
                image_format="png", use_ocr=False, show_progress=False,
            )
        references = sorted(plain.iterdir())

        self.assertEqual((len(images), len(references)), (1, 1))
        self.assertEqual(
            hashlib.sha256(pymupdf.Pixmap(str(images[0])).samples).hexdigest(),
            hashlib.sha256(pymupdf.Pixmap(str(references[0])).samples).hexdigest(),
            "OCR text is drawn over the figure",
        )
        self.assertIn("Encoder", markdown_path.read_text(encoding="utf-8"))




class ReaderArtifactTests(unittest.TestCase):
    def test_reader_keeps_markdown_tables_and_embeds_visuals(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        image = root / "chart.png"
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
            "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        source = (
            "| Metric | Value |\n"
            "| --- | ---: |\n"
            "| Accuracy | 86% |\n\n"
            f"![Trend chart]({image})"
        )
        narration = (
            "The table reports an accuracy of eighty-six percent.\n\n"
            "The chart shows the trend over time."
        )
        adaptation = root / "paragraph-checkpoints"
        adaptation.mkdir()
        web.write_json_atomic(
            adaptation / "000001-000001.json",
            {
                "end": 1,
                "narration": "The table reports an accuracy of eighty-six percent.",
                "summary": "Table summary.",
            },
        )
        web.write_json_atomic(
            adaptation / "000002-000002.json",
            {
                "end": 2,
                "narration": "The chart shows the trend over time.",
                "summary": "Chart summary.",
            },
        )
        for index, frames in enumerate((1200, 2400), 1):
            sf.write(
                root / f"chunk-{index:06d}.wav",
                np.linspace(-0.1, 0.1, frames, dtype=np.float32),
                24000,
                subtype="FLOAT",
            )

        markdown, synchronization, _ = web.build_reader_artifacts(
            narration,
            source,
            500,
            root,
            adaptation_checkpoints=adaptation,
            image_roots=(root,),
        )
        blocks = web.render_reader_blocks(markdown)

        self.assertIn("<table>", blocks[0]["html"])
        self.assertIn('src="data:image/png;base64,', blocks[1]["html"])
        self.assertNotIn(str(image), markdown)
        numbered_fragment = web.render_reader_blocks(
            "<!-- audiobook-tts:block=0 -->\n\n1."
        )[0]["html"]
        self.assertIn("<p>1.</p>", numbered_fragment)
        self.assertEqual(synchronization["sample_rate"], 24000)
        self.assertEqual(
            synchronization["cues"],
            [
                {"block": 0, "start_sample": 0, "end_sample": 1200},
                {"block": 1, "start_sample": 1200, "end_sample": 3600},
            ],
        )
        self.assertEqual(synchronization["paragraphs"], [0, 1])

    def test_reader_uses_exact_sentence_boundaries(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        narration = "First sentence. Second sentence!"
        for index, frames in enumerate((1200, 1800), 1):
            sf.write(
                root / f"chunk-{index:06d}.wav",
                np.linspace(-0.1, 0.1, frames, dtype=np.float32),
                24000,
                subtype="FLOAT",
            )

        markdown, synchronization, _ = web.build_reader_artifacts(
            narration,
            narration,
            500,
            root,
            word_aligner=FakeWordAligner(),
        )
        blocks = web.render_reader_blocks(markdown)

        self.assertEqual(synchronization["schema"], 3)
        self.assertEqual(len(blocks), 2)
        self.assertIn("First sentence.", blocks[0]["html"])
        self.assertIn("Second sentence!", blocks[1]["html"])
        self.assertEqual(
            synchronization["cues"],
            [
                {"block": 0, "start_sample": 0, "end_sample": 1200},
                {"block": 1, "start_sample": 1200, "end_sample": 3000},
            ],
        )
        self.assertEqual(synchronization["paragraphs"], [0, 0])
        self.assertEqual(synchronization["word_timing"], "aligned")
        self.assertEqual(
            [
                (cue["block"], cue["index"], cue["text"])
                for cue in synchronization["word_cues"]
            ],
            [
                (0, 0, "First"),
                (0, 1, "sentence"),
                (1, 0, "Second"),
                (1, 1, "sentence"),
            ],
        )

    def test_legacy_paragraph_reader_gets_estimated_sentence_cues(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        book = store_book(
            storage,
            markdown="<!-- audiobook-tts:block=0 -->\n\nFirst sentence. Second sentence.",
            timings={
                "schema": 1,
                "sample_rate": 24000,
                "duration_samples": 3100,
                "block_count": 1,
                "cues": [
                    {"block": 0, "start_sample": 0, "end_sample": 3100},
                ],
            },
        )

        payload = web.audiobook_reader_payload(storage, book)

        self.assertEqual(payload["timing_precision"], "estimated")
        self.assertEqual(payload["word_timing"], "unavailable")
        self.assertEqual(len(payload["blocks"]), 2)
        self.assertEqual(
            [cue["block"] for cue in payload["cues"]],
            [0, 1],
        )
        self.assertEqual(payload["cues"][0]["start_sample"], 0)
        self.assertEqual(
            payload["cues"][0]["end_sample"],
            payload["cues"][1]["start_sample"],
        )
        self.assertEqual(payload["cues"][1]["end_sample"], 3100)
        self.assertEqual(
            [block["paragraph"] for block in payload["blocks"]],
            [0, 0],
        )

    def test_visual_blocks_exclude_unspoken_text_and_absorb_invisible_audio(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        sources = [
            "Spoken table summary.\n\n"
            "| Raw table value | Count |\n"
            "| --- | ---: |\n"
            "| Not narrated | 42 |",
            "&#8203;",
            "Spoken figure description.\n\n"
            "![Chart](data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
            "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=)",
        ]

        def words(block, start, texts):
            return [
                {"block": block, "index": index, "text": text,
                 "start_sample": start + 500 * index, "end_sample": start + 500 * index + 400}
                for index, text in enumerate(texts)
            ]

        book = store_book(
            storage,
            markdown="\n\n".join(
                f"<!-- audiobook-tts:block={index} -->\n\n{source}"
                for index, source in enumerate(sources)
            ),
            timings={
                "schema": 3,
                "sample_rate": 24000,
                "duration_samples": 9000,
                "block_count": 3,
                "paragraphs": [0, 1, 2],
                "cues": [
                    {"block": 0, "start_sample": 0, "end_sample": 3000},
                    {"block": 1, "start_sample": 3000, "end_sample": 5000},
                    {"block": 2, "start_sample": 5000, "end_sample": 9000},
                ],
                "word_timing": "aligned",
                "word_cues": words(0, 0, ["Spoken", "table", "summary"])
                + words(2, 5000, ["Spoken", "figure", "description"]),
            },
        )
        payload = web.audiobook_reader_payload(storage, book)

        self.assertEqual(len(payload["blocks"]), 2)
        self.assertIn("<table>", payload["blocks"][0]["html"])
        self.assertIn("Spoken figure description.", payload["blocks"][1]["html"])
        self.assertIn("<img", payload["blocks"][1]["html"])
        self.assertEqual(
            [
                (cue["block"], cue["start_sample"], cue["end_sample"])
                for cue in payload["cues"]
            ],
            [(0, 0, 3000), (1, 3000, 9000)],
        )
        self.assertNotEqual(
            payload["blocks"][0]["paragraph"],
            payload["blocks"][1]["paragraph"],
        )
        self.assertEqual(
            [cue["text"] for cue in payload["word_cues"]],
            ["Spoken", "table", "summary", "Spoken", "figure", "description"],
        )
        self.assertEqual(
            [cue["block"] for cue in payload["word_cues"]],
            [0, 0, 0, 1, 1, 1],
        )

    def test_reader_shows_each_batch_original_text_page_and_whether_the_model_wrote_it(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        root = Path(temporary.name) / "stage"
        (root / "images").mkdir(parents=True)
        (root / "images" / "figure-1.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
            "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        source = [
            "Provided proper attribution is given, Google grants permission.",
            "## **Abstract**",
            "The Transformer<sup>1</sup> relies on <b>attention</b> [5].",
            "# Scaled Dot-Product Attention",
            "![](images/figure-1.png)",
            "<!-- Start of picture text -->Q<br>K<!-- End of picture text -->",
            "Figure 1: Scaled Dot-Product Attention.",
            "Recurrent models are slow. They read tokens in order.",
            "Attention runs in parallel.",
        ]
        batches = {
            (1, 1): "",  # boilerplate the model left out
            (2, 2): "## Abstract",  # read word for word
            (3, 3): "The Transformer relies on attention.",
            (4, 7): "Figure 1 shows the model. It stacks attention layers.",
            (8, 9): "Recurrent models are slow. They read tokens one by one.\n\n"
                    "Attention runs in parallel.",
        }
        adaptation = root / "paragraph-checkpoints"
        for (start, end), narration in batches.items():
            web.write_json_atomic(
                adaptation / f"{start:06d}-{end:06d}.json",
                {"end": end, "narration": narration, "summary": "S.",
                 **({"tags": ["attention", "parallelism"]} if start == 3 else {})},
            )
        narration = "\n\n".join(text for text in batches.values() if text)
        chunks = web.split_text(narration, 500, sentence_chunks=True)
        for index in range(1, len(chunks) + 1):
            sf.write(
                root / f"chunk-{index:06d}.wav",
                np.linspace(-0.1, 0.1, 1200, dtype=np.float32),
                24000,
                subtype="FLOAT",
            )

        markdown, synchronization, narration_record = web.build_reader_artifacts(
            narration,
            "\n\n".join(source),
            500,
            root,
            adaptation_checkpoints=adaptation,
            source_pages=[1, 1, 2, 3, 3, 3, 3, 4, 5],
            image_roots=(root,),
        )

        self.assertEqual(synchronization["paragraphs"], [0, 1, 2, 2, 3, 3, 4])
        passages = narration_record["passages"]
        # What a new voice reads is exactly what this one read.
        self.assertEqual(web.narration_text(narration_record), narration)
        # Each passage keeps its batch's summary and tags, for Chat with Hilde;
        # a checkpoint made before tags has none.
        self.assertEqual(narration_record["schema"], 2)
        self.assertEqual(
            [(passage["summary"], passage["tags"]) for passage in passages[1:3]],
            [("S.", []), ("S.", ["attention", "parallelism"])],
        )
        # Each passage is typed, and so is each author's paragraph it came
        # from, so a caption read inside a figure's passage stays one.
        self.assertEqual(
            [passage["type"] for passage in passages],
            ["body", "heading", "body", "figure", "body"],
        )
        self.assertEqual(
            [source_part["type"] for source_part in passages[3]["sources"]],
            ["figure", "figure", "figure", "caption"],
        )
        self.assertEqual(
            [source_part["page"] for source_part in passages[3]["sources"]], [3, 3, 3, 3]
        )
        # The figure's title, image, and labels already show beside its
        # description, and text read word for word shows as the narration.
        self.assertEqual(web.narration_originals(narration_record), [
            {"paragraphs": None, "page": 1, "description": False,
             "unchanged": False, "markdown": source[0]},
            {"paragraphs": [0, 0], "page": 1, "description": False,
             "unchanged": True, "markdown": ""},
            {"paragraphs": [1, 1], "page": 2, "description": False,
             "unchanged": False, "markdown": source[2]},
            {"paragraphs": [2, 2], "page": 3, "description": True,
             "unchanged": False, "markdown": "Figure 1: Scaled Dot-Product Attention."},
            {"paragraphs": [3, 4], "page": 4, "description": False,
             "unchanged": False, "markdown": f"{source[7]}\n\n{source[8]}"},
        ])

        book = store_book(
            storage, markdown=markdown, timings=synchronization, narration=narration_record
        )
        payload = web.audiobook_reader_payload(storage, book)

        self.assertEqual(
            [(item["paragraphs"], item["page"], item["description"], item["unchanged"])
             for item in payload["originals"]],
            [(None, 1, False, False), ([0, 0], 1, False, True), ([1, 1], 2, False, False),
             ([2, 2], 3, True, False), ([3, 4], 4, False, False)],
        )
        # Extraction's superscript tags render as superscripts; any other
        # markup in the source stays text.
        self.assertIn("Transformer<sup>1</sup>", payload["originals"][2]["html"])
        self.assertIn("&lt;b&gt;attention&lt;/b&gt;", payload["originals"][2]["html"])

        # A batch placed out of order is not trusted.
        passages[4]["paragraphs"] = [2, 4]
        tangled = store_book(
            storage, source_sha256="b" * 64, markdown=markdown, timings=synchronization,
            narration=narration_record,
        )
        with self.assertRaises(ValueError):
            web.audiobook_reader_payload(storage, tangled)


class BatchingTests(unittest.TestCase):
    class Model:
        """A fake model that runs out of GPU memory above a batch capacity."""

        def __init__(self, capacity):
            self.capacity = capacity
            self.calls = []

        def generate_voice_clone(self, text, language, voice_clone_prompt, max_new_tokens=None):
            import torch

            self.calls.append((list(text), max_new_tokens))
            if len(text) > self.capacity:
                raise torch.cuda.OutOfMemoryError("CUDA out of memory")
            return [np.zeros(len(item), dtype=np.float32) for item in text], 24000

    def generate(self, capacity, texts):
        model = self.Model(capacity)
        with mock.patch("sys.stdout", io.StringIO()):
            waveforms, rate = cli.generate_clone_batch(model, texts, "English", None)
        return model.calls, [len(waveform) for waveform in waveforms], rate

    def test_only_a_batch_that_runs_out_of_memory_is_retried_one_chunk_at_a_time(self):
        long = "x" * 100
        calls, lengths, _ = self.generate(3, ["a", "bb", long])
        # A batch's cap fits its longest text; a short text is spoken with a period.
        self.assertEqual(calls, [(["a.", "bb.", long], 274)])
        self.assertEqual(lengths, [2, 3, 100])

        calls, lengths, rate = self.generate(1, ["a", "bb", long])
        # Alone, each chunk gets its own cap.
        self.assertEqual(calls, [(["a.", "bb.", long], 274), (["a."], 29), (["bb."], 31), ([long], 274)])
        self.assertEqual((lengths, rate), ([2, 3, 100], 24000))

    def test_a_single_chunk_that_runs_out_of_memory_still_fails(self):
        import torch

        with self.assertRaises(torch.cuda.OutOfMemoryError):
            self.generate(0, ["a"])

    def test_browsers_saved_with_the_old_default_batch_size_move_to_the_new_one(self):
        def batch_size(schema, stored):
            state = normalize({"schema": schema, "audiobook": {"batch_size": stored}})
            return state["audiobook"]["batch_size"]

        self.assertEqual(batch_size(3, "1"), "2")
        self.assertEqual(batch_size(3, "4"), "4")
        self.assertEqual(batch_size(web.STATE_SCHEMA_VERSION, "1"), "1")


class DistributedNarrationTests(unittest.TestCase):
    # Speaks the worker protocol without a model. When given a marker path, the
    # worker's first chunk runs out of memory and leaves the marker behind.
    WORKER = """\
import base64, io, json, sys, time
from pathlib import Path

import numpy
import soundfile

protocol, device, record, marker = sys.argv[1:5]


def note(line):
    with open(record, "a", encoding="utf-8") as stream:
        stream.write(line + "\\n")


def emit(payload):
    print(protocol + json.dumps(payload), flush=True)


note(f"start {device}")
emit({"type": "ready"})
for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "stop":
        break
    indexes = request["indexes"]
    if marker and not Path(marker).exists():
        note(f"oom {device} {indexes}")
        Path(marker).touch()
        emit({"type": "error", "message": "AcceleratorError: CUDA error: out of memory"})
        sys.exit(1)
    time.sleep(0.1)
    waves = []
    for _ in indexes:
        buffer = io.BytesIO()
        soundfile.write(buffer, numpy.zeros(240, dtype="float32"), 24000, format="WAV", subtype="FLOAT")
        waves.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
    emit({"type": "result", "indexes": indexes, "waves": waves})
"""

    def test_full_gpu_joins_later_and_out_of_memory_chunks_return_to_the_queue(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "worker.py").write_text(self.WORKER, encoding="utf-8")
        record, marker = root / "record.txt", root / "out-of-memory"
        checkpoints = root / "checkpoints"
        checkpoints.mkdir()

        def worker(device, oom_marker):
            command = [
                sys.executable, "-u", str(root / "worker.py"),
                cli.WORKER_PROTOCOL, device, str(record), oom_marker,
            ]
            return (f"Local {device}", command, None, device)

        class Memory:
            # Another program fills cuda:1 until cuda:0 has run out of memory.
            def __init__(self, devices):
                pass

            def free_mib(self):
                return {"cuda:0": 90000, "cuda:1": 90000 if marker.exists() else 1000}

        class Parser:
            @staticmethod
            def error(message):
                raise AssertionError(message)

        chunks = [f"Chunk {index}." for index in range(1, 7)]
        completed, outcome = set(), {}

        def narrate():
            try:
                outcome["value"] = cli._narrate_distributed(
                    argparse.Namespace(batch_size=1), Parser, chunks, root, checkpoints,
                    completed, list(range(1, len(chunks) + 1)), sf,
                )
            except BaseException as exc:
                outcome["error"] = exc

        specifications = [worker("cuda:0", str(marker)), worker("cuda:1", "")]
        with (
            mock.patch.object(cli, "_narration_worker_specifications", return_value=specifications),
            mock.patch.object(cli, "GpuMemory", Memory),
            mock.patch.object(cli, "GPU_RECHECK_SECONDS", 0.2),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            # A thread, so a coordinator that never rechecks fails instead of hanging.
            thread = threading.Thread(target=narrate, daemon=True)
            thread.start()
            thread.join(30)

        self.assertFalse(thread.is_alive(), "narration never finished")
        self.assertEqual(outcome, {"value": True})
        self.assertEqual(completed, set(range(1, 7)))
        for index in completed:
            self.assertGreater(sf.info(cli._checkpoint_path(checkpoints, index)).frames, 0)
        events = record.read_text(encoding="utf-8").splitlines()
        # The full GPU started only once it had room; the GPU that ran out came back.
        self.assertEqual(events[:2], ["start cuda:0", "oom cuda:0 [1]"])
        self.assertIn("start cuda:1", events)
        self.assertEqual(events.count("start cuda:0"), 2)


class SpeechEndpointTests(unittest.TestCase):
    def test_bare_authority_gets_the_openai_prefix_but_an_explicit_path_wins(self):
        self.assertEqual(speech_endpoint("127.0.0.1:8880"), "http://127.0.0.1:8880/v1")
        self.assertEqual(speech_endpoint("box.local"), "http://box.local/v1")
        self.assertEqual(speech_endpoint(" http://10.0.0.5:9000/v1 "), "http://10.0.0.5:9000/v1")
        self.assertEqual(
            speech_endpoint("https://tts.example.com/openai/"), "https://tts.example.com/openai"
        )

    def test_unusable_endpoints_are_rejected(self):
        for value in ("", "127.0.0.1:notaport", "ftp://host:1/", "user:secret@host:8880"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    speech_endpoint(value)


def fake_local_server(test, models, requests):
    """An OpenAI-compatible server offering `models` that narrates every
    batch as FIGURE_PROSE and keeps each request's body in `requests`.
    Return its host:port."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"data": [{"id": model} for model in models]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = {"content": f"<NARRATION>{FIGURE_PROSE}</NARRATION><SUMMARY>S.</SUMMARY>"}
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n".encode())
            self.wfile.write(b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f"127.0.0.1:{server.server_port}"


class LocalPaperProviderTests(unittest.TestCase):
    # SGLang answers Ollama's /api/tags too, but only Ollama answers /api/version.
    SGLANG_ROUTES = {
        "/api/tags": {"models": [{"name": "deepseek-v4.1-flash"}]},
        "/v1/models": {"object": "list", "data": [
            {"id": "deepseek-v4.1-flash", "owned_by": "sglang"},
        ]},
    }

    def list_models(self, routes, provider):
        """List models of the chosen type on a fake server answering only these JSON routes."""
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in routes:
                    self.send_error(404)
                    return
                body = json.dumps(routes[self.path]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                return origin, local_model_names(origin, provider)
            finally:
                server.shutdown()
                thread.join()

    def test_openai_compatible_server_is_listed(self):
        _, names = self.list_models({"/v1/models": {"object": "list", "data": [
            {"id": "vision-model", "owned_by": "vllm"},
            {"id": "text-model", "owned_by": "vllm"},
        ]}}, "lm-studio")

        self.assertEqual(names, ("text-model", "vision-model"))

    def test_choosing_ollama_for_a_server_that_only_imitates_it_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.list_models(self.SGLANG_ROUTES, "ollama")

        self.assertEqual(
            self.list_models(self.SGLANG_ROUTES, "lm-studio")[1], ("deepseek-v4.1-flash",)
        )

    def test_ollama_is_listed(self):
        _, names = self.list_models({
            "/api/version": {"version": "0.32.8"},
            "/api/tags": {"models": [{"name": "qwen3.8:27b"}, {"name": "deepseek-v4-flash:latest"}]},
        }, "ollama")

        self.assertEqual(names, ("deepseek-v4-flash:latest", "qwen3.8:27b"))

    def test_an_added_local_server_is_the_default_and_never_gives_way_to_the_cloud(self):
        with mock.patch.object(web, "read_openai_credentials", return_value={}), \
                mock.patch.object(web, "openai_model_names", return_value=["gpt-a"]), \
                mock.patch.object(web, "read_anthropic_key", return_value=None), \
                mock.patch.object(web, "read_openai_api_key", return_value=None), \
                mock.patch.object(web, "read_gemini_key", return_value=None), \
                mock.patch.object(web, "claude_code_status", return_value=(False, "")):
            added = [{"server": "http://127.0.0.1:8010", "provider": "lm-studio", "vision": False}]
            with mock.patch.object(web, "local_model_names", return_value=["deepseek"]):
                catalog = web.paper_model_catalog(added)
            self.assertEqual(catalog["default_model"], "lm-studio/deepseek")
            self.assertEqual(
                [model["selector"] for model in catalog["models"]],
                ["lm-studio/deepseek", "openai-codex/gpt-a"],
            )
            # Each model says where it runs, as Chat's picker shows it before sending.
            self.assertEqual(
                [model["badge"] for model in catalog["models"]], ["Local", "Cloud: OpenAI"],
            )
            # While it does not answer, a job gets no default rather than
            # sending the document to OpenAI.
            with mock.patch.object(
                web, "local_model_names", side_effect=RuntimeError("Cannot reach it")
            ):
                catalog = web.paper_model_catalog(added)
            self.assertEqual(catalog["default_model"], "")
            self.assertEqual(catalog["local_error"], "127.0.0.1:8010: Cannot reach it")
            # Without a local server, OpenAI is the default once signed in.
            self.assertEqual(web.paper_model_catalog()["default_model"], "openai-codex/gpt-a")

    def test_adaptation_accepts_only_models_this_server_can_reach(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        storage = web.SharedStorage(root / "library")
        storage.ensure()
        prompt = root / "prompt.md"
        prompt.write_text("Adapt the text.", encoding="utf-8")
        home = root / ".hilde"

        def problem(model, local_server="127.0.0.1:8010", **settings):
            state = normalize({"tab": "audiobook", "audiobook": {
                "adapt": True, "model": model, **settings,
                "local_servers": [{"server": local_server, "provider": "lm-studio"}] if local_server else [],
            }})
            with mock.patch.object(web, "_DEVICE_OPTIONS", [{"value": "cpu", "label": "CPU"}]):
                values = web.values_of(state, web.unconfigured_tts_models(), storage)
            with mock.patch.object(web, "PAPER_PROMPT_PATH", prompt), \
                    mock.patch.object(web, "HILDE_HOME", home):
                return web._adaptation_problem(values)

        self.assertIsNotNone(problem("ollama/deepseek-v4.1-flash"))
        self.assertIsNone(problem("lm-studio/deepseek-v4.1-flash"))
        # The checking and figure models need a server of their type too, once used.
        self.assertEqual(
            problem("lm-studio/a", verify=True, verify_model="ollama/b"),
            "Add a local server of this model's type under Add local.",
        )
        self.assertIsNone(problem("lm-studio/a", verify_model="ollama/b"))
        self.assertIsNotNone(problem("lm-studio/a", interpret_figures=True, figure_model="ollama/b"))
        self.assertIsNotNone(problem("lm-studio/a", local_server=""))
        # A model no backend of this server serves is refused before the job starts.
        self.assertIsNotNone(problem("mistral/large"))
        for model in ("", "openai-codex/gpt-a", "anthropic/claude-a", "openai/gpt-a", "gemini/gemini-a"):
            self.assertIsNotNone(problem(model, local_server=""))
        with mock.patch.object(web, "HILDE_HOME", home):
            web._write_private_json(web.anthropic_key_path(), {"api_key": "sk-ant-key"})
        self.assertIsNone(problem("anthropic/claude-a", local_server=""))
        self.assertIsNone(problem("", local_server=""))
        self.assertIsNotNone(problem("openai-codex/gpt-a", local_server=""))
        self.assertIsNotNone(problem("openai/gpt-a", local_server=""))
        with mock.patch.object(web, "HILDE_HOME", home):
            web.anthropic_key_path().unlink()
            web.save_openai_credentials({
                "access_token": "access", "refresh_token": "refresh",
                "id_token": fake_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct"}}),
            })
        self.assertIsNone(problem("openai-codex/gpt-a", local_server=""))
        self.assertIsNone(problem("", local_server=""))
        self.assertIsNotNone(problem("anthropic/claude-a", local_server=""))
        # The OpenAI API key is its own provider, apart from the ChatGPT sign-in.
        self.assertIsNotNone(problem("openai/gpt-a", local_server=""))
        with mock.patch.object(web, "HILDE_HOME", home):
            web.openai_credentials_path().unlink()
            web._write_private_json(web.openai_api_key_path(), {"api_key": "sk-key"})
        self.assertIsNone(problem("openai/gpt-a", local_server=""))
        self.assertIsNone(problem("", local_server=""))
        self.assertIsNotNone(problem("openai-codex/gpt-a", local_server=""))
        self.assertIsNotNone(problem("gemini/gemini-a", local_server=""))
        with mock.patch.object(web, "HILDE_HOME", home):
            web.openai_api_key_path().unlink()
            web._write_private_json(web.gemini_key_path(), {"api_key": "AIza-key"})
        self.assertIsNone(problem("gemini/gemini-a", local_server=""))
        self.assertIsNone(problem("", local_server=""))
        self.assertIsNotNone(problem("openai/gpt-a", local_server=""))
        self.assertEqual(
            normalize({"audiobook": {"local_servers": [{"server": "127.0.0.1:1", "provider": "vllm"}]}})
            ["audiobook"]["local_servers"], []
        )

    def test_local_server_gets_figures_as_images_only_when_it_sees_them(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        requests = []
        sees_images = threading.Event()

        class ModelHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body))
                # Like a text-only model, refuse image input.
                if not sees_images.is_set() and any(
                    isinstance(message["content"], list) for message in body["messages"]
                ):
                    self.send_error(400, "image input is not supported")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta in (
                    # Thinking models often draft the tagged answer first.
                    {"reasoning_content": "Draft: <NARRATION>Wrong.</NARRATION><SUMMARY>x</SUMMARY>"},
                    {"content": "<NARRATION>Spoken "},
                    {"content": "text.</NARRATION><SUMMARY>Short.</SUMMARY>"},
                ):
                    self.wfile.write(
                        f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n".encode()
                    )
                self.wfile.write(
                    b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
                    b"data: [DONE]\n\n"
                )

            def log_message(self, format, *args):
                pass

        source = root / "paper.pdf"
        write_figure_pdf(source)
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        stage = root / "extraction"
        runs = []
        with ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for local_vision, server_sees_images in ((False, False), (True, False), (True, True)):
                    if server_sees_images:
                        sees_images.set()
                    requests.clear()
                    run = PaperRun(
                        source, stage / "prepared.txt", "utf-8", model="lm-studio/local-model",
                        local_servers=[{
                            "server": f"127.0.0.1:{server.server_port}", "provider": "lm-studio",
                            "vision": local_vision,
                        }],
                        in_flight=1, prompt_path=prompt, scratch_path=stage,
                    )
                    with mock.patch.object(web, "local_model_names", return_value=("local-model",)):
                        run.pump()
                    runs.append((run, list(requests)))
            finally:
                server.shutdown()
                thread.join()

        # By default the figure reaches the model as its Markdown text only.
        (text_only, text_requests), (refused, _), (seen, image_requests) = runs
        self.assertEqual(text_only.code, 0)
        self.assertTrue(all(
            path == "/v1/chat/completions" and body["model"] == "local-model"
            and isinstance(body["messages"][1]["content"], str)
            for path, body in text_requests
        ))
        self.assertTrue(any("images/" in body["messages"][1]["content"] for _, body in text_requests))
        # Turning images on redoes the adaptation; a text-only model refuses
        # them, and the job says how to fix that.
        self.assertEqual(refused.code, 1)
        self.assertTrue(any(
            event == "log" and "This model sees images" in str(data)
            for event, data in refused.history
        ))
        # A model that sees images gets the figure as an image.
        self.assertEqual(seen.code, 0)
        [figure] = [
            body["messages"][1]["content"] for _, body in image_requests
            if isinstance(body["messages"][1]["content"], list)
            and not body["messages"][0]["content"].startswith("You sort figures")
        ]
        image = next(part for part in figure if part["type"] == "image_url")
        self.assertTrue(image["image_url"]["url"].startswith("data:image/png;base64,"))
        prepared = (stage / "prepared.txt").read_text(encoding="utf-8")
        self.assertIn("Spoken text.", prepared)
        self.assertNotIn("Wrong", prepared)
        # The setting survives only as a real yes.
        for stored, kept in ((True, True), ("yes", False), (None, False)):
            [entry] = normalize({"audiobook": {"local_servers": [
                {"server": "127.0.0.1:1", "provider": "lm-studio", "vision": stored},
            ]}})["audiobook"]["local_servers"]
            self.assertIs(entry["vision"], kept)

    def test_an_old_single_server_state_becomes_the_first_of_a_list_of_unique_servers(self):
        audiobook = normalize({"audiobook": {
            "local_server": "127.0.0.1:8894", "local_provider": "lm-studio", "local_vision": True,
        }})["audiobook"]
        self.assertEqual(
            audiobook["local_servers"],
            [{"server": "http://127.0.0.1:8894", "provider": "lm-studio", "vision": True}],
        )
        self.assertFalse({"local_server", "local_provider", "local_vision"} & set(audiobook))
        self.assertEqual(normalize({"audiobook": {}})["audiobook"]["local_servers"], [])
        # One entry a server, the first kept; invalid ones dropped; at most LOCAL_SERVERS_MAX.
        entries = [{"server": f"127.0.0.1:{8000 + number}", "provider": "lm-studio"} for number in range(12)]
        servers = normalize({"audiobook": {"local_servers": [
            {"server": "127.0.0.1:8003", "provider": "ollama", "vision": True},
            {"server": "127.0.0.1:1/v1", "provider": "lm-studio"},
            "127.0.0.1:2",
            *entries,
        ]}})["audiobook"]["local_servers"]
        self.assertEqual(len(servers), web.LOCAL_SERVERS_MAX)
        self.assertEqual(servers[0], {"server": "http://127.0.0.1:8003", "provider": "ollama", "vision": True})
        self.assertEqual(
            [entry["server"] for entry in servers[1:]],
            [f"http://127.0.0.1:{port}" for port in (8000, 8001, 8002, 8004, 8005, 8006, 8007)],
        )

    def test_a_local_server_registered_on_the_server_is_parsed_and_checked(self):
        option = web.local_server_option
        self.assertEqual(option("lm-studio=http://127.0.0.1:8000,vision"),
                         {"server": "http://127.0.0.1:8000", "provider": "lm-studio", "vision": True})
        self.assertEqual(option("ollama=127.0.0.1:11434"),
                         {"server": "http://127.0.0.1:11434", "provider": "ollama", "vision": False})
        for wrong in ("vllm=127.0.0.1:8000", "127.0.0.1:8000", "lm-studio=", "lm-studio=127.0.0.1:8000,fast",
                      "lm-studio=http://127.0.0.1:8000/v1", "lm-studio=ftp://127.0.0.1"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=wrong):
                option(wrong)

    def test_registered_local_servers_come_before_the_browsers_own_and_the_catalog_says_which(self):
        shared = [{"server": "http://127.0.0.1:9", "provider": "lm-studio", "vision": True}]
        gemma = fake_local_server(self, ["gemma"], [])
        added = normalize({"audiobook": {"local_servers": [
            {"server": "127.0.0.1:9", "provider": "lm-studio"}, {"server": gemma, "provider": "lm-studio"},
        ]}})["audiobook"]["local_servers"]
        merged = web.local_servers_of(added, shared)
        # The registered one first, marked; the browser's copy of it dropped; its own after.
        self.assertEqual([(entry["server"], entry.get("on_server", False), entry["vision"]) for entry in merged],
                         [("http://127.0.0.1:9", True, True), (f"http://{gemma}", False, False)])
        with mock.patch.object(web, "SERVER_LOCAL_SERVERS", shared):
            self.assertEqual(web.local_servers_of([]), [{**shared[0], "on_server": True}])
        with mock.patch.object(web, "read_openai_credentials", return_value=None), \
                mock.patch.object(web, "read_anthropic_key", return_value=None), \
                mock.patch.object(web, "read_openai_api_key", return_value=None), \
                mock.patch.object(web, "read_gemini_key", return_value=None), \
                mock.patch.object(web, "claude_code_status", return_value=(False, "")):
            catalog = web.paper_model_catalog(merged)
        # A registered server that does not answer is listed as not running; the browser's models stay.
        self.assertEqual(
            [(entry["label"], entry["on_server"], entry["running"]) for entry in catalog["local_servers"]],
            [("127.0.0.1:9", True, False), (gemma, False, True)],
        )
        self.assertEqual([model["selector"] for model in catalog["models"]], ["lm-studio/gemma"])
        self.assertNotIn("on_server", catalog["models"][0])

    def test_models_of_several_local_servers_are_listed_together_and_each_asked_on_its_own(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        gemma_requests, qwen_requests = [], []
        gemma = fake_local_server(self, ["gemma"], gemma_requests)
        qwen = fake_local_server(self, ["qwen"], qwen_requests)
        added = normalize({"audiobook": {"local_servers": [
            {"server": gemma, "provider": "lm-studio"}, {"server": qwen, "provider": "lm-studio"},
        ]}})["audiobook"]["local_servers"]
        with mock.patch.object(web, "read_openai_credentials", return_value={}), \
                mock.patch.object(web, "openai_model_names", return_value=["gpt-a"]), \
                mock.patch.object(web, "read_anthropic_key", return_value=None), \
                mock.patch.object(web, "read_openai_api_key", return_value=None), \
                mock.patch.object(web, "read_gemini_key", return_value=None), \
                mock.patch.object(web, "claude_code_status", return_value=(False, "")):
            catalog = web.paper_model_catalog(added)
            # A server that does not answer is named; the others' models stay.
            unreachable = web.paper_model_catalog([
                {"server": "http://127.0.0.1:9", "provider": "lm-studio", "vision": False}, *added,
            ])
        # Local models first, server by server, each with its server; then the cloud's.
        self.assertEqual(
            [(model["selector"], model.get("server"), model.get("label")) for model in catalog["models"]],
            [
                ("lm-studio/gemma", f"http://{gemma}", f"gemma · {gemma}"),
                ("lm-studio/qwen", f"http://{qwen}", f"qwen · {qwen}"),
                ("openai-codex/gpt-a", None, None),
            ],
        )
        self.assertEqual(catalog["default_model"], "lm-studio/gemma")
        self.assertEqual(catalog["local_error"], "")
        self.assertEqual(unreachable["default_model"], "lm-studio/gemma")
        self.assertTrue(unreachable["local_error"].startswith("127.0.0.1:9: "))

        source = root / "paper.pdf"
        write_figure_pdf(source, prose=FIGURE_PROSE)
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        for qwen_sees_images in (True, False):
            gemma_requests.clear()
            qwen_requests.clear()
            stage = root / f"stage-{qwen_sees_images}"
            # The text model is on the first server, the figure model on the second.
            run = PaperRun(
                source, stage / "prepared.txt", "utf-8", "lm-studio/gemma",
                [
                    {"server": gemma, "provider": "lm-studio", "vision": True},
                    {"server": qwen, "provider": "lm-studio", "vision": qwen_sees_images},
                ],
                in_flight=1, paragraphs_per_worker=1, prompt_path=prompt, scratch_path=stage,
                interpret_figures=True, figure_model="lm-studio/qwen",
            )
            run.pump()
            self.assertEqual(run.code, 0)
            # The prose goes to the text model's server, never with an image.
            self.assertEqual({body["model"] for body in gemma_requests}, {"gemma"})
            self.assertTrue(any(
                FIGURE_PROSE in str(body["messages"][1]["content"])
                for body in gemma_requests
            ))
            self.assertFalse(any(isinstance(body["messages"][1]["content"], list) for body in gemma_requests))
            # The figure goes to the figure model's server, as an image only
            # when that server's models see images.
            self.assertEqual({body["model"] for body in qwen_requests}, {"qwen"})
            self.assertEqual(
                any(isinstance(body["messages"][1]["content"], list) for body in qwen_requests),
                qwen_sees_images,
            )
            # Which server each model ran on is part of the extraction's identity.
            manifest = json.loads((stage / "extraction.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["local"], {
                "lm-studio/gemma": {"server": f"http://{gemma}", "vision": True},
                "lm-studio/qwen": {"server": f"http://{qwen}", "vision": qwen_sees_images},
            })
            self.assertEqual(run.local_vision(), {"lm-studio/gemma": True, "lm-studio/qwen": qwen_sees_images})

    def test_a_local_model_no_added_server_offers_stops_the_job(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        requests = []
        server = fake_local_server(self, ["gemma"], requests)
        added = [{"server": server, "provider": "lm-studio", "vision": False}]
        for selector in ("lm-studio/qwen", "ollama/gemma"):
            with self.assertRaises(RuntimeError) as caught:
                web.local_route(selector, added)
            self.assertEqual(str(caught.exception), f"No local server you added offers {selector.split('/', 1)[1]}.")
        source = root / "paper.md"
        source.write_text("Recurrent models are slow to train.", encoding="utf-8")
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        run = PaperRun(
            source, root / "prepared.txt", "utf-8", "lm-studio/qwen", added,
            in_flight=1, prompt_path=prompt, scratch_path=root / "stage",
        )
        run.pump()
        self.assertEqual(run.code, 1)
        self.assertTrue(any("No local server you added offers qwen." in str(data) for _, data in run.history))
        self.assertEqual(requests, [])

    def test_a_model_still_writing_at_its_limit_is_asked_again_and_never_hangs(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        bodies = []
        answers = []

        class ModelHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                content, reason = answers.pop(0)
                for event in ({"delta": {"content": content}}, {"delta": {}, "finish_reason": reason}):
                    self.wfile.write(f"data: {json.dumps({'choices': [event]})}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, format, *args):
                pass

        source = root / "paper.md"
        source.write_text("Recurrent models are slow to train.", encoding="utf-8")
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        looping = ("loop " * 50, "length")
        whole = ("<NARRATION>Recurrent models are slow to train.</NARRATION><SUMMARY>s</SUMMARY>", "stop")
        with ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                runs = []
                for script in ([looping, whole], [looping] * web.PAPER_RESPONSE_ATTEMPTS):
                    answers[:] = script
                    stage = root / f"stage-{len(runs)}"
                    run = PaperRun(
                        source, stage / "prepared.txt", "utf-8", model="lm-studio/local-model",
                        local_servers=[{"server": f"127.0.0.1:{server.server_port}", "provider": "lm-studio"}],
                        in_flight=1, prompt_path=prompt, scratch_path=stage,
                    )
                    with mock.patch.object(web, "local_model_names", return_value=("local-model",)):
                        run.pump()
                    runs.append(run)
            finally:
                server.shutdown()
                thread.join()

        # Every request caps the model's output.
        self.assertTrue(all(body["max_tokens"] == web.LOCAL_MAX_TOKENS for body in bodies))
        asked_again, gave_up = runs
        self.assertEqual(asked_again.code, 0)
        self.assertIn(
            "Recurrent models are slow to train.",
            (root / "stage-0" / "prepared.txt").read_text(encoding="utf-8"),
        )
        # Still writing at the limit every time, the job ends instead of hanging.
        self.assertEqual(gave_up.code, 1)
        self.assertTrue(any("repeating itself" in str(data) for _, data in gave_up.history))


class OpenAISignInTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.approved = threading.Event()
        self.pending = threading.Semaphore(0)
        self.token_requests = []
        self.responses = []
        test = self

        class OpenAIHandler(BaseHTTPRequestHandler):
            def reply(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def signed_in(self):
                return (
                    self.headers.get("Authorization") == "Bearer access-2"
                    and self.headers.get("ChatGPT-Account-Id") == "acct-1"
                )

            def do_GET(self):
                if not self.path.startswith("/backend-api/codex/models?"):
                    return self.reply(404, {})
                if not self.signed_in():
                    return self.reply(401, {"detail": "expired"})
                self.reply(200, {"models": [
                    {"slug": "gpt-b", "priority": 2},
                    {"slug": "gpt-a", "priority": 1},
                    {"slug": "gpt-hidden", "priority": 0, "visibility": "hide"},
                ]})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/api/accounts/deviceauth/usercode":
                    return self.reply(200, {
                        "device_auth_id": "device-1", "user_code": "ABCD-EFGH", "interval": "0",
                    })
                if self.path == "/api/accounts/deviceauth/token":
                    if not test.approved.is_set():
                        test.pending.release()
                        return self.reply(403, {"error": "authorization_pending"})
                    return self.reply(200, {
                        "authorization_code": "code-1", "code_verifier": "verifier-1",
                    })
                if self.path == "/oauth/token":
                    form = dict(urllib.parse.parse_qsl(raw.decode()))
                    test.token_requests.append(form)
                    if form.get("grant_type") == "authorization_code" and (
                        form.get("code"), form.get("code_verifier")
                    ) == ("code-1", "verifier-1"):
                        # Expires at once, so its first use renews it.
                        return self.reply(200, {
                            "access_token": "access-1", "refresh_token": "refresh-1",
                            "id_token": fake_jwt(
                                {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"}}
                            ),
                            "expires_in": 1,
                        })
                    if form.get("grant_type") == "refresh_token" and (
                        form.get("refresh_token") == "refresh-1"
                    ):
                        return self.reply(200, {
                            "access_token": "access-2", "refresh_token": "refresh-2",
                            "expires_in": 3600,
                        })
                    return self.reply(400, {"error": "invalid_grant"})
                if self.path == "/backend-api/codex/responses":
                    if not self.signed_in():
                        return self.reply(401, {"detail": "expired"})
                    test.responses.append(json.loads(raw))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for event in (
                        {"type": "response.created"},
                        {"type": "response.output_text.delta", "delta": "<NARRATION>Figure one is "},
                        {"type": "response.output_text.delta", "delta": "described.</NARRATION>"},
                        {"type": "response.output_text.delta", "delta": "<SUMMARY>Figure.</SUMMARY>"},
                        {"type": "response.completed", "response": {"usage": {}}},
                    ):
                        self.wfile.write(
                            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                        )
                    return None
                return self.reply(404, {})

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop_server)
        origin = f"http://127.0.0.1:{server.server_port}"
        for name, value in (
            ("OPENAI_AUTH_URL", origin),
            ("CHATGPT_CODEX_URL", f"{origin}/backend-api/codex"),
            ("HILDE_HOME", self.root / ".hilde"),
            ("OPENAI_DEVICE_POLL_FLOOR", 0.01),
        ):
            patcher = mock.patch.object(web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def wait_for(login, status):
        deadline = time.monotonic() + 10
        while login.snapshot()["status"] != status and time.monotonic() < deadline:
            time.sleep(0.01)
        return login.snapshot()

    def test_device_sign_in_is_kept_owner_only_and_renewed_for_adaptation(self):
        login = web.OpenAIOAuthLogin()
        login.start()
        waiting = self.wait_for(login, "waiting")
        self.assertEqual(
            (waiting["url"], waiting["code"]), (web.OPENAI_DEVICE_PAGE, "ABCD-EFGH")
        )
        # OpenAI answers "not yet" until the user enters the code.
        self.assertTrue(self.pending.acquire(timeout=5))
        self.approved.set()
        self.assertEqual(self.wait_for(login, "connected")["status"], "connected")
        home = self.root / ".hilde"
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((home / "openai.json").stat().st_mode), 0o600)

        # The sign-in has already expired, so listing the models renews it.
        with mock.patch.object(web, "claude_code_status", return_value=(False, "")):
            catalog = web.paper_model_catalog()
        self.assertEqual(
            [model["selector"] for model in catalog["models"]],
            ["openai-codex/gpt-a", "openai-codex/gpt-b"],
        )
        self.assertEqual(catalog["default_model"], "openai-codex/gpt-a")

        source = self.root / "paper.pdf"
        write_figure_pdf(source)
        prompt = self.root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        stage = self.root / "extraction"
        run = PaperRun(
            source, stage / "prepared.txt", "utf-8", model="openai-codex/gpt-a",
            in_flight=1, prompt_path=prompt, scratch_path=stage,
        )
        run.pump()

        self.assertEqual(run.code, 0)
        self.assertIn(
            "Figure one is described.", (stage / "prepared.txt").read_text(encoding="utf-8")
        )
        request = next(
            body for body in self.responses
            if any(part["type"] == "input_image" for part in body["input"][0]["content"])
            and not body["instructions"].startswith("You sort figures")
        )
        self.assertEqual((request["model"], request["stream"], request["store"]), ("gpt-a", True, False))
        self.assertIn("Adapt every paragraph.", request["instructions"])
        image = next(
            part for part in request["input"][0]["content"] if part["type"] == "input_image"
        )
        self.assertTrue(image["image_url"].startswith("data:image/png;base64,"))
        # Renewed once, and the rotated refresh token was kept.
        self.assertEqual(
            [form["grant_type"] for form in self.token_requests],
            ["authorization_code", "refresh_token"],
        )
        self.assertEqual(
            json.loads((home / "openai.json").read_text(encoding="utf-8"))["refresh_token"],
            "refresh-2",
        )


class OpenAIRetryTests(unittest.TestCase):
    SUCCESS = (200, [
        {"type": "response.output_text.delta", "delta": "<NARRATION>Narrated.</NARRATION>"},
        {"type": "response.output_text.delta", "delta": "<SUMMARY>Summary.</SUMMARY>"},
        {"type": "response.completed", "response": {}},
    ])

    def start(self, answers):
        """Start one paragraph's adaptation against a Codex backend answering in turn."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        requests = []

        class CodexHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                status, events = answers[min(len(requests), len(answers) - 1)]
                requests.append(self.path)
                self.send_response(status)
                if status != 200:
                    body = json.dumps({"error": {"message": "upstream connect error"}}).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", str(events))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), CodexHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop_server)
        for name, value in (
            ("CHATGPT_CODEX_URL", f"http://127.0.0.1:{server.server_port}/backend-api/codex"),
            ("HILDE_HOME", root / ".hilde"),
        ):
            patcher = mock.patch.object(web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        web.save_openai_credentials({
            "access_token": "access", "refresh_token": "refresh",
            "id_token": fake_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct"}}),
        })
        source = root / "paper.md"
        source.write_text("One paragraph to adapt.", encoding="utf-8")
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        run = PaperRun(
            source, root / "prepared.txt", "utf-8", model="openai-codex/gpt-a",
            in_flight=1, prompt_path=prompt,
        )
        worker = threading.Thread(target=run.pump)
        worker.start()
        self.addCleanup(worker.join, 10)
        return run, worker, requests

    def test_busy_backend_is_asked_again_until_it_answers(self):
        # Failures OpenAI's backend gave during real adaptation runs, ordered
        # so the waits between attempts stay short.
        run, worker, requests = self.start([
            (200, [{"type": "error", "message": "Unable to verify model access right now. Please retry."}]),
            (200, [{"type": "response.failed", "response": {"error": {
                "code": "server_error", "message": "An error occurred while processing your request.",
            }}}]),
            (503, 0),
            self.SUCCESS,
        ])
        worker.join(30)

        self.assertEqual(run.code, 0)
        self.assertEqual(len(requests), 4)

    def test_a_refused_request_is_not_repeated(self):
        run, worker, requests = self.start([
            (200, [{"type": "response.failed", "response": {"error": {
                "code": "invalid_prompt", "message": "Your prompt was flagged.",
            }}}]),
            self.SUCCESS,
        ])
        worker.join(30)

        self.assertEqual(run.code, 1)
        self.assertEqual(len(requests), 1)

    def test_stop_cuts_the_wait_before_asking_again(self):
        run, worker, requests = self.start([(503, 30)])
        deadline = time.monotonic() + 10
        while not requests and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        run.stop()
        worker.join(10)

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(run.code, 130)
        self.assertEqual(len(requests), 1)



class AnthropicProviderTests(unittest.TestCase):
    def test_api_key_is_checked_kept_owner_only_and_used_with_figures(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        messages = []

        class AnthropicHandler(BaseHTTPRequestHandler):
            def reply(self, status, payload, headers=()):
                body = json.dumps(payload).encode()
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                if (
                    self.headers.get("x-api-key") == "sk-ant-good"
                    and self.headers.get("anthropic-version") == "2023-06-01"
                ):
                    return True
                self.reply(401, {"type": "error", "error": {
                    "type": "authentication_error", "message": "invalid x-api-key",
                }})
                return False

            def do_GET(self):
                if self.path != "/v1/models?limit=1000":
                    return self.reply(404, {})
                if self.authorized():
                    self.reply(200, {"data": [
                        {"id": "claude-new", "type": "model"},
                        {"id": "claude-old", "type": "model"},
                    ], "has_more": False})
                return None

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/v1/messages":
                    return self.reply(404, {})
                if not self.authorized():
                    return None
                # Anthropic requires max_tokens within the model's output limit;
                # 64,000 is the lowest limit among the models it still serves.
                limit = body.get("max_tokens")
                if not isinstance(limit, int) or not 1 <= limit <= 64_000:
                    return self.reply(400, {"type": "error", "error": {
                        "type": "invalid_request_error", "message": f"max_tokens: {limit}",
                    }})
                messages.append(body)
                # The first request meets a rate limit, as a busy account does.
                if len(messages) == 1:
                    return self.reply(429, {"type": "error", "error": {
                        "type": "rate_limit_error", "message": "Too many requests.",
                    }}, (("retry-after", "0"),))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                # Recent Claude models always think before they answer.
                for event in (
                    {"type": "message_start", "message": {"id": "msg", "content": []}},
                    {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "thinking", "thinking": ""}},
                    # and often draft the tagged answer while they do.
                    {"type": "content_block_delta", "index": 0, "delta": {
                        "type": "thinking_delta",
                        "thinking": "Draft: <NARRATION>Wrong.</NARRATION><SUMMARY>x</SUMMARY>",
                    }},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "signature_delta", "signature": "c2lnbmF0dXJl"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "content_block_start", "index": 1,
                     "content_block": {"type": "text", "text": ""}},
                    {"type": "ping"},
                    {"type": "content_block_delta", "index": 1,
                     "delta": {"type": "text_delta", "text": "<NARRATION>Figure one is "}},
                    {"type": "content_block_delta", "index": 1, "delta": {
                        "type": "text_delta",
                        "text": "described.</NARRATION><SUMMARY>Figure.</SUMMARY>",
                    }},
                    {"type": "content_block_stop", "index": 1},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                    {"type": "message_stop"},
                ):
                    self.wfile.write(
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                    )
                return None

            def log_message(self, format, *args):
                pass

        source = root / "paper.pdf"
        write_figure_pdf(source)
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        stage = root / "extraction"
        home = root / ".hilde"
        with ThreadingHTTPServer(("127.0.0.1", 0), AnthropicHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(
                    web, "ANTHROPIC_API_URL", f"http://127.0.0.1:{server.server_port}"
                ), mock.patch.object(web, "HILDE_HOME", home), \
                        mock.patch.object(web, "claude_code_status", return_value=(False, "")):
                    with self.assertRaises(RuntimeError):
                        web.connect_anthropic("sk-ant-wrong")
                    self.assertFalse(web.anthropic_key_path().exists())
                    web.connect_anthropic(" sk-ant-good\n")
                    catalog = web.paper_model_catalog()
                    run = PaperRun(
                        source, stage / "prepared.txt", "utf-8", model="anthropic/claude-new",
                        in_flight=1, prompt_path=prompt, scratch_path=stage,
                    )
                    run.pump()
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((home / "anthropic.json").stat().st_mode), 0o600)
        self.assertEqual(
            [model["selector"] for model in catalog["models"]],
            ["anthropic/claude-new", "anthropic/claude-old"],
        )
        self.assertEqual(catalog["default_model"], "anthropic/claude-new")
        self.assertEqual(run.code, 0)
        self.assertIn(
            "Figure one is described.", (stage / "prepared.txt").read_text(encoding="utf-8")
        )
        # The rate-limited request was sent again unchanged.
        self.assertEqual(messages[0], messages[1])
        request = next(
            body for body in messages
            if any(part["type"] == "image" for part in body["messages"][0]["content"])
            and not str(body["system"]).startswith("You sort figures")
        )
        self.assertEqual((request["model"], request["stream"]), ("claude-new", True))
        self.assertIn("Adapt every paragraph.", request["system"])
        self.assertNotIn("Wrong.", (stage / "prepared.txt").read_text(encoding="utf-8"))
        image = next(
            part for part in request["messages"][0]["content"] if part["type"] == "image"
        )
        self.assertEqual(
            (image["source"]["type"], image["source"]["media_type"]), ("base64", "image/png")
        )
        base64.b64decode(image["source"]["data"], validate=True)


class FakeProviderHandler(BaseHTTPRequestHandler):
    """A cloud provider's HTTP API: JSON replies and server-sent events."""

    def reply(self, status, payload, headers=()):
        body = json.dumps(payload).encode()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())

    def body(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def log_message(self, format, *args):
        pass


def serve_fake_provider(test, handler):
    """Start a fake provider for one test; return its origin."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}"


def patch_for_test(test, *patches):
    for patch in patches:
        patch.start()
        test.addCleanup(patch.stop)


class OpenAIAPIProviderTests(unittest.TestCase):
    def test_api_key_is_checked_kept_owner_only_and_used_with_figures(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        requests = []

        class OpenAIHandler(FakeProviderHandler):
            def authorized(self):
                if self.headers.get("Authorization") == "Bearer sk-good":
                    return True
                self.reply(401, {"error": {
                    "message": "Incorrect API key provided.", "code": "invalid_api_key",
                }})
                return False

            def do_GET(self):
                if self.path != "/v1/models":
                    return self.reply(404, {})
                if self.authorized():
                    self.reply(200, {"object": "list", "data": [
                        {"id": name, "object": "model", "created": created}
                        for name, created in (
                            ("gpt-old", 100), ("gpt-new", 300), ("o4-mini", 200),
                            ("chatgpt-4o-latest", 250), ("gpt-4o-audio-preview", 400),
                            ("gpt-4o-realtime-preview", 401), ("gpt-4o-transcribe", 402),
                            ("gpt-4o-mini-tts", 403), ("gpt-image-1", 404),
                            ("gpt-4o-search-preview", 405), ("text-embedding-3-large", 406),
                            ("omni-moderation-latest", 407), ("gpt-3.5-turbo-instruct", 408),
                            ("dall-e-3", 409), ("whisper-1", 410),
                        )
                    ]})
                return None

            def do_POST(self):
                body = self.body()
                if self.path != "/v1/responses":
                    return self.reply(404, {})
                if not self.authorized():
                    return None
                requests.append(body)
                if len(requests) == 1:
                    return self.reply(429, {"error": {
                        "message": "Rate limit reached.", "code": "rate_limit_exceeded",
                    }}, (("retry-after", "0"),))
                return self.stream((
                    {"type": "response.created", "response": {"id": "resp"}},
                    # A reasoning model's summary is never the answer.
                    {"type": "response.reasoning_summary_text.delta",
                     "delta": "Draft: <NARRATION>Wrong.</NARRATION><SUMMARY>x</SUMMARY>"},
                    {"type": "response.output_item.done", "item": {"type": "reasoning", "summary": []}},
                    {"type": "response.output_text.delta", "delta": "<NARRATION>Figure one is "},
                    {"type": "response.output_text.delta",
                     "delta": "described.</NARRATION><SUMMARY>Figure.</SUMMARY>"},
                    {"type": "response.completed", "response": {"id": "resp"}},
                ))

        origin = serve_fake_provider(self, OpenAIHandler)
        source = root / "paper.pdf"
        write_figure_pdf(source)
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        stage = root / "extraction"
        home = root / ".hilde"
        with mock.patch.object(web, "OPENAI_API_URL", f"{origin}/v1"), \
                mock.patch.object(web, "HILDE_HOME", home), \
                mock.patch.object(web, "claude_code_status", return_value=(False, "")):
            with self.assertRaisesRegex(RuntimeError, "OpenAI rejected the API key"):
                web.connect_openai_api("sk-wrong")
            self.assertFalse(web.openai_api_key_path().exists())
            with self.assertRaises(ValueError):
                web.connect_openai_api("sk with spaces")
            web.connect_openai_api(" sk-good\n")
            catalog = web.paper_model_catalog()
            run = PaperRun(
                source, stage / "prepared.txt", "utf-8", model="openai/gpt-new",
                in_flight=1, prompt_path=prompt, scratch_path=stage,
            )
            run.pump()

        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((home / "openai-api.json").stat().st_mode), 0o600)
        # Text models only, newest first, apart from the ChatGPT sign-in's.
        self.assertEqual(
            [model["selector"] for model in catalog["models"]],
            ["openai/gpt-new", "openai/chatgpt-4o-latest", "openai/o4-mini", "openai/gpt-old"],
        )
        self.assertEqual(catalog["default_model"], "openai/gpt-new")
        self.assertEqual((catalog["openai_api_connected"], catalog["openai_connected"]), (True, False))
        self.assertEqual(run.code, 0)
        prepared = (stage / "prepared.txt").read_text(encoding="utf-8")
        self.assertIn("Figure one is described.", prepared)
        self.assertNotIn("Wrong.", prepared)
        # The rate-limited request was sent again unchanged.
        self.assertEqual(requests[0], requests[1])
        request = next(
            body for body in requests
            if any(part["type"] == "input_image" for part in body["input"][0]["content"])
            and not str(body["instructions"]).startswith("You sort figures")
        )
        self.assertEqual(
            (request["model"], request["stream"], request["store"]), ("gpt-new", True, False)
        )
        self.assertIn("Adapt every paragraph.", request["instructions"])
        image = next(part for part in request["input"][0]["content"] if part["type"] == "input_image")
        self.assertTrue(image["image_url"].startswith("data:image/png;base64,"))
        base64.b64decode(image["image_url"].partition(",")[2], validate=True)


class GeminiProviderTests(unittest.TestCase):
    def test_api_key_is_checked_kept_owner_only_and_used_with_figures(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        requests = []
        generate = ["generateContent", "countTokens"]
        pages = {
            "": ([
                {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": generate},
                {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": generate},
                {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": generate},
            ], "page-2"),
            "page-2": ([
                {"name": "models/gemini-3.8-flash-image", "supportedGenerationMethods": generate},
                {"name": "models/gemini-live-2.5-flash", "supportedGenerationMethods": ["bidiGenerateContent"]},
                {"name": "models/gemini-2.5-flash-native-audio-latest", "supportedGenerationMethods": generate},
                {"name": "models/aqa", "supportedGenerationMethods": ["generateAnswer"]},
                {"name": "models/gemma-3-27b-it", "supportedGenerationMethods": generate},
                {"name": "models/gemini-10-pro", "supportedGenerationMethods": generate},
                {"name": "models/imagen-4.0-generate-001", "supportedGenerationMethods": ["predict"]},
            ], None),
        }

        class GeminiHandler(FakeProviderHandler):
            def authorized(self):
                if self.headers.get("x-goog-api-key") == "AIza-good":
                    return True
                # Google answers a wrong key with 400, not 401.
                self.reply(400, {"error": {
                    "code": 400, "message": "API key not valid. Please pass a valid API key.",
                    "status": "INVALID_ARGUMENT",
                }})
                return False

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                if parts.path != "/v1beta/models" or query.get("pageSize") != ["1000"]:
                    return self.reply(404, {})
                if self.authorized():
                    models, following = pages[query.get("pageToken", [""])[0]]
                    payload = {"models": models}
                    if following:
                        payload["nextPageToken"] = following
                    self.reply(200, payload)
                return None

            def do_POST(self):
                body = self.body()
                if self.path != "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse":
                    return self.reply(404, {})
                if not self.authorized():
                    return None
                requests.append(body)
                if len(requests) == 1:
                    return self.reply(429, {"error": {
                        "code": 429, "message": "Resource exhausted.", "status": "RESOURCE_EXHAUSTED",
                    }}, (("retry-after", "0"),))

                def chunk(parts, **candidate):
                    return {"candidates": [{"content": {"role": "model", "parts": parts}, **candidate}]}

                return self.stream((
                    # Thoughts come first when a model shows them, and are never text.
                    chunk([{"text": "Draft: <NARRATION>Wrong.</NARRATION><SUMMARY>x</SUMMARY>",
                            "thought": True}]),
                    chunk([{"text": "<NARRATION>Figure one is "}]),
                    chunk([{"text": "described.</NARRATION><SUMMARY>Figure.</SUMMARY>",
                            "thoughtSignature": "c2lnbmF0dXJl"}], finishReason="STOP"),
                    {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}},
                ))

        origin = serve_fake_provider(self, GeminiHandler)
        source = root / "paper.pdf"
        write_figure_pdf(source)
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        stage = root / "extraction"
        home = root / ".hilde"
        with mock.patch.object(web, "GEMINI_API_URL", f"{origin}/v1beta"), \
                mock.patch.object(web, "HILDE_HOME", home), \
                mock.patch.object(web, "claude_code_status", return_value=(False, "")):
            with self.assertRaisesRegex(RuntimeError, "API key not valid"):
                web.connect_gemini("AIza-wrong")
            self.assertFalse(web.gemini_key_path().exists())
            web.connect_gemini(" AIza-good\n")
            catalog = web.paper_model_catalog()
            run = PaperRun(
                source, stage / "prepared.txt", "utf-8", model="gemini/gemini-3.8-flash",
                in_flight=1, prompt_path=prompt, scratch_path=stage,
            )
            run.pump()

        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((home / "gemini.json").stat().st_mode), 0o600)
        # Text models from every page, newest version first.
        self.assertEqual(
            [model["selector"] for model in catalog["models"]],
            ["gemini/gemini-10-pro", "gemini/gemini-3.8-flash", "gemini/gemini-2.5-pro"],
        )
        self.assertEqual(catalog["default_model"], "gemini/gemini-10-pro")
        self.assertTrue(catalog["gemini_connected"])
        self.assertEqual(run.code, 0)
        prepared = (stage / "prepared.txt").read_text(encoding="utf-8")
        self.assertIn("Figure one is described.", prepared)
        self.assertNotIn("Wrong.", prepared)
        # The rate-limited request was sent again unchanged.
        self.assertEqual(requests[0], requests[1])
        request = next(
            body for body in requests
            if any("inlineData" in part for part in body["contents"][0]["parts"])
            and not body["systemInstruction"]["parts"][0]["text"].startswith("You sort figures")
        )
        self.assertIn("Adapt every paragraph.", request["systemInstruction"]["parts"][0]["text"])
        # The model's own output limit applies.
        self.assertNotIn("generationConfig", request)
        self.assertEqual(request["contents"][0]["role"], "user")
        image = next(part for part in request["contents"][0]["parts"] if "inlineData" in part)
        self.assertEqual(image["inlineData"]["mimeType"], "image/png")
        base64.b64decode(image["inlineData"]["data"], validate=True)

    def test_a_stopped_or_cut_off_answer_is_reported(self):
        def read(*events):
            return web._gemini_text(io.BytesIO(b"".join(
                f"data: {json.dumps(event)}\n\n".encode() for event in events
            )))

        def finished(reason):
            return {"candidates": [{"content": {"parts": [{"text": "Half"}]}, "finishReason": reason}]}

        with self.assertRaisesRegex(RuntimeError, "output limit; lower Paragraphs per worker"):
            read(finished("MAX_TOKENS"))
        with self.assertRaisesRegex(RuntimeError, "SAFETY"):
            read(finished("SAFETY"))
        with self.assertRaisesRegex(RuntimeError, "PROHIBITED_CONTENT"):
            read({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
        with self.assertRaises(web.ModelBusy):
            read({"error": {"code": 503, "message": "Overloaded.", "status": "UNAVAILABLE"}})
        with self.assertRaises(web.ModelBusy):
            read({"candidates": [{"content": {"parts": [{"text": "Half"}]}}]})
        with self.assertRaises(RuntimeError) as refused:
            read({"error": {"code": 400, "message": "Bad request.", "status": "INVALID_ARGUMENT"}})
        self.assertNotIsInstance(refused.exception, web.ModelBusy)
        self.assertEqual(read(finished("STOP")), "Half")



FAKE_CLAUDE_CODE = """\
#!{python}
import json, os, sys
from pathlib import Path

state = Path({state!r})
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": (state / "signed-in").exists(), "subscriptionType": "max"}}))
    sys.exit(0)
message = json.loads(sys.stdin.readline())
with (state / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "message": message}}) + "\\n")
model = sys.argv[sys.argv.index("--model") + 1]
print(json.dumps({{"type": "system", "subtype": "init", "model": "claude-" + model + "-9-1"}}))
if (state / "limit").exists():
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                      "result": "You've hit your limit · resets 5pm"}}))
    sys.exit(1)
images = [part for part in message["message"]["content"] if part["type"] == "image"]
answer = ("<NARRATION>Figure one is described.</NARRATION><SUMMARY>Figure.</SUMMARY>" if images
          else "<NARRATION>Prose stays.</NARRATION><SUMMARY>Prose.</SUMMARY>")
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": answer}}]}}}}))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": answer}}))
"""


class ClaudeCodeProviderTests(unittest.TestCase):
    def test_own_signed_in_claude_code_adapts_with_figures_and_reports_its_limits(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        state = root / "claude-state"
        state.mkdir()
        claude = root / "bin" / "claude"
        claude.parent.mkdir()
        claude.write_text(FAKE_CLAUDE_CODE.format(python=sys.executable, state=str(state)))
        claude.chmod(0o755)
        source = root / "paper.pdf"
        write_figure_pdf(source)
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        home = root / ".hilde"

        def run(stage):
            job = PaperRun(
                source, stage / "prepared.txt", "utf-8", model="claude-code/sonnet",
                in_flight=1, prompt_path=prompt, scratch_path=stage,
            )
            job.pump()
            return job

        with mock.patch.object(web, "CLAUDE_CODE_CANDIDATES", (str(claude),)), \
                mock.patch.object(web, "HILDE_HOME", home), \
                mock.patch.object(web, "read_openai_credentials", return_value=None), \
                mock.patch.object(web, "read_anthropic_key", return_value=None), \
                mock.patch.object(web, "read_openai_api_key", return_value=None), \
                mock.patch.object(web, "read_gemini_key", return_value=None):
            # Installed but signed out: no models, and the job is refused.
            catalog = web.paper_model_catalog()
            self.assertEqual((catalog["models"], catalog["claude_code_connected"]), ([], False))
            self.assertIn("not signed in", catalog["claude_code_status"])
            values = {"adapt": True, "model": "claude-code/sonnet", "local_servers": [],
                      "verify": False, "verify_model": "", "interpret_figures": False, "figure_model": "",
                      "in_flight": 1, "paragraphs_per_worker": 1}
            self.assertIsNotNone(web._adaptation_problem(values))

            (state / "signed-in").touch()
            # Each nickname is shown by the model it runs, asked of Claude Code
            # once and remembered until Check again.
            web._CLAUDE_CODE_RESOLVED.update(at=0.0, models={})
            catalog = web.paper_model_catalog()
            asked = len((state / "calls.jsonl").read_text().splitlines())
            web.paper_model_catalog()
            self.assertEqual(len((state / "calls.jsonl").read_text().splitlines()), asked)
            web.paper_model_catalog(refresh=True)
            self.assertEqual(len((state / "calls.jsonl").read_text().splitlines()), asked + 3)
            self.assertIsNone(web._adaptation_problem(values))
            finished = run(root / "extraction")
            (state / "limit").touch()
            limited = run(root / "limited")

        self.assertEqual(catalog["default_model"], "claude-code/sonnet")
        self.assertEqual(
            [model["selector"] for model in catalog["models"]],
            ["claude-code/sonnet", "claude-code/opus", "claude-code/haiku"],
        )
        self.assertEqual(
            [model["label"] for model in catalog["models"]],
            ["Claude Sonnet 9.1 (sonnet)", "Claude Opus 9.1 (opus)", "Claude Haiku 9.1 (haiku)"],
        )
        self.assertEqual(finished.code, 0)
        self.assertIn(
            "Figure one is described.",
            (root / "extraction" / "prepared.txt").read_text(encoding="utf-8"),
        )
        calls = [json.loads(line) for line in (state / "calls.jsonl").read_text().splitlines()]
        call = next(
            call for call in calls
            if any(part["type"] == "image" for part in call["message"]["message"]["content"])
            and not call["argv"][call["argv"].index("--system-prompt") + 1].startswith("You sort figures")
        )
        argv = call["argv"]
        # One answer and no actions: Hilde's instructions, every tool off,
        # and never --bare, which would skip the subscription sign-in.
        self.assertEqual(argv[:3], ["-p", "--model", "sonnet"])
        self.assertIn("Adapt every paragraph.", argv[argv.index("--system-prompt") + 1])
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("--bare", argv)
        # Run outside the project, so no instructions file joins the prompt.
        self.assertEqual(Path(call["cwd"]).resolve(), home.resolve())
        image = next(
            part for part in call["message"]["message"]["content"] if part["type"] == "image"
        )
        self.assertEqual(image["source"]["media_type"], "image/png")
        base64.b64decode(image["source"]["data"], validate=True)
        # A plan's usage limit ends the job with Claude Code's own words,
        # not as a reply that fails to parse.
        self.assertNotEqual(limited.code, 0)
        self.assertTrue(any(
            "Claude Code: You've hit your limit" in str(data)
            for event, data in limited.history if event == "log"
        ))


class DocumentDownloadTests(unittest.TestCase):
    def test_extensionless_pdf_url_is_saved_with_inferred_suffix(self):
        source = b"%PDF-1.7\nextensionless PDF fixture\n"

        class SourceHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/pdf/2508.21433":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(source)))
                self.end_headers()
                self.wfile.write(source)

            def log_message(self, format, *args):
                pass

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        storage = web.SharedStorage(root / "library")
        storage.ensure()
        voice = storage.voices / "Martin"
        save_voice(
            voice,
            np.linspace(-0.5, 0.5, 1200, dtype=np.float32),
            24000,
            "Reference passage.",
            "FLOAT",
        )
        model = root / "clone-model"
        model.mkdir()
        models = {
            "design": {"source": "missing"},
            "clone": {
                "source": "local",
                "model": str(model),
                "allow_downloads": False,
            },
        }

        with (
            ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler) as source_server,
            ThreadingHTTPServer(("127.0.0.1", 0), Handler) as web_server,
        ):
            source_server.daemon_threads = True
            web_server.daemon_threads = True
            web_server.jobs = web.JobQueue()
            web_server.tts_models = models
            web_server.storage = storage
            web_server.verbose = False
            source_thread = threading.Thread(
                target=source_server.serve_forever, daemon=True
            )
            web_thread = threading.Thread(
                target=web_server.serve_forever, daemon=True
            )
            source_thread.start()
            web_thread.start()
            source_url = (
                f"http://127.0.0.1:{source_server.server_port}/pdf/2508.21433"
            )
            state = normalize({
                "audiobook": {
                    "source_url": source_url,
                    "download_name": "2508.21433",
                    "voice": voice.name,
                    "adapt": False,
                },
            })
            self.assertIsNone(web.derived(state, models, storage)["problem"])
            request = urllib.request.Request(
                (
                    f"http://127.0.0.1:{web_server.server_port}"
                    "/api/documents/download"
                ),
                data=json.dumps({
                    "url": source_url,
                    "name": "2508.21433",
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request) as response:
                    payload = json.load(response)
            finally:
                web_server.shutdown()
                source_server.shutdown()
                web_thread.join()
                source_thread.join()

        self.assertEqual(payload["name"], "2508.21433.pdf")
        self.assertIn(payload["name"], payload["assets"]["documents"])
        self.assertEqual(
            (storage.documents / payload["name"]).read_bytes(),
            source,
        )


class RetainedAudiobookDownloadTests(unittest.TestCase):
    def test_named_mp3_download_is_an_attachment_and_rejects_traversal(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        payload = b"retained audiobook bytes"
        book = store_book(storage, title="Kept Book", voice="Eir", audio=payload)
        # A title with a path separator, a colon, quotes, and a letter beyond ASCII.
        titled = store_book(
            storage, title='Rivers/Deltas: Å "short" study', source_sha256="b" * 64, voice="Eir",
        )

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.storage = storage
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(
                    f"{origin}/api/download?book={book}&voice=Eir"
                ) as response:
                    downloaded = response.read()
                    disposition = response.headers["Content-Disposition"]
                with urllib.request.urlopen(
                    f"{origin}/api/download?book={titled}&voice=Eir"
                ) as response:
                    titled_disposition = response.headers["Content-Disposition"]
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(
                        f"{origin}/api/download?book=..%2F{book}&voice=Eir"
                    )
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual(downloaded, payload)
        self.assertIn('filename="Kept Book - Eir.mp3"', disposition)
        self.assertIn("""filename="Rivers-Deltas - ? 'short' study - Eir.mp3";""", titled_disposition)
        encoded = titled_disposition.split("filename*=UTF-8''", 1)[1]
        self.assertEqual(
            urllib.parse.unquote(encoded), "Rivers-Deltas - Å 'short' study - Eir.mp3"
        )
        self.assertNotIn("/", encoded)
        self.assertEqual(refused.exception.code, 400)

    def test_a_long_title_is_cut_at_a_word_and_a_book_without_one_uses_its_document(self):
        title = "Sediment " + "transport and storage in large river deltas " * 5
        name = web.book_download_name({"title": title}, "book--id", "Eir")
        stem = name.removesuffix(" - Eir.mp3")
        self.assertLessEqual(len(stem), web.BOOK_DOWNLOAD_TITLE_CHARS)
        self.assertGreater(len(stem), web.BOOK_DOWNLOAD_TITLE_CHARS // 2)
        self.assertTrue(title.startswith(stem + " "), stem)
        self.assertEqual(
            web.book_download_name({"source_filenames": ["deltas\x07 2024.pdf"]}, "book--id", "Eir"),
            "deltas 2024 - Eir.mp3",
        )
        self.assertEqual(web.book_download_name({}, "book--id", "Eir"), "book--id - Eir.mp3")

    def test_a_books_document_downloads_from_listen_only_when_kept(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        payload = b"%PDF-1.7\nthe paper\n"
        # A book without its own copy finds its document in Documents by content.
        document = storage.documents / "renamed copy.pdf"
        document.write_bytes(payload)
        book = store_book(storage, title="Kept Book", source_sha256=web.cached_file_version(document), voice="Eir")
        self.assertIs(web.library_catalog(storage)[0]["has_source"], True)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.storage = storage
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(f"{origin}/api/source?book={book}") as response:
                    downloaded = response.read()
                    disposition = response.headers["Content-Disposition"]
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(f"{origin}/api/source?book=..%2F{book}")
                document.unlink()
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(f"{origin}/api/source?book={book}")
            finally:
                server.shutdown()
                thread.join()

        # Under the name it was added with, not the copy's.
        self.assertEqual(downloaded, payload)
        self.assertIn('filename="Kept Book.pdf"', disposition)
        self.assertEqual(refused.exception.code, 400)
        self.assertEqual(missing.exception.code, 404)
        self.assertIs(web.library_catalog(storage)[0]["has_source"], False)


class HttpsTests(unittest.TestCase):
    def test_hildes_certificate_authority_vouches_for_this_machine_only_and_lasts(self):
        from cryptography import x509

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name) / "tls"
        names = (["hilde-box", "hilde-box.local", "localhost"], ["127.0.0.1", "192.168.1.20"])
        cert, key = web.ensure_tls_files(home, names)
        authority = x509.load_pem_x509_certificate((home / "ca.pem").read_bytes())
        server = x509.load_pem_x509_certificate(cert.read_bytes())
        # The certificate is the authority's and names this machine's names and addresses.
        server.verify_directly_issued_by(authority)
        alternative = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(sorted(alternative.get_values_for_type(x509.DNSName)), names[0])
        self.assertEqual(sorted(str(ip) for ip in alternative.get_values_for_type(x509.IPAddress)), names[1])
        # The authority may vouch only for private networks and these names.
        constraints = authority.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertTrue(constraints.critical)
        permitted = {str(name.value) for name in constraints.value.permitted_subtrees}
        self.assertIn("192.168.0.0/16", permitted)
        self.assertNotIn("0.0.0.0/0", permitted)
        self.assertEqual((home / "ca-key.pem").stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        # Started again, nothing changes; a new address or a near expiry makes a
        # new certificate under the same authority, so devices keep trusting it.
        before = cert.read_bytes()
        web.ensure_tls_files(home, names)
        self.assertEqual(cert.read_bytes(), before)
        moved = (names[0], ["127.0.0.1", "192.168.1.31"])
        web.ensure_tls_files(home, moved)
        self.assertNotEqual(cert.read_bytes(), before)
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=web.TLS_SERVER_DAYS - 5)
        renewed_before = cert.read_bytes()
        web.ensure_tls_files(home, moved, now=later)
        renewed = x509.load_pem_x509_certificate(cert.read_bytes())
        self.assertNotEqual(cert.read_bytes(), renewed_before)
        renewed.verify_directly_issued_by(authority)
        self.assertEqual(x509.load_pem_x509_certificate((home / "ca.pem").read_bytes()), authority)

    def test_https_serves_the_page_redirects_plain_http_and_offers_the_authority(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        storage = web.SharedStorage(root / "library")
        storage.ensure()
        cert, key = web.ensure_tls_files(root / "tls", (["localhost"], ["127.0.0.1"]))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server = web.HildeServer(("127.0.0.1", 0), Handler)
        server.tls, server.tls_authority = context, root / "tls" / "ca.pem"
        server.daemon_threads = True
        server.storage = storage
        server.jobs = web.JobQueue()
        server.tts_models = web.unconfigured_tts_models()
        server.verbose = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(), server.server_close()))
        port = server.server_port
        trusting = ssl.create_default_context(cafile=str(root / "tls" / "ca.pem"))

        # A browser that trusts Hilde's authority gets the page, and its
        # settings cookies are sent over HTTPS only.
        with urllib.request.urlopen(f"https://localhost:{port}/api/state", context=trusting) as response:
            self.assertIs(json.load(response)["capabilities"]["certificate"], True)
        request = urllib.request.Request(
            f"https://localhost:{port}/api/sync", data=json.dumps({"state": {}}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, context=trusting) as response:
            cookies = response.headers.get_all("Set-Cookie")
        self.assertTrue(cookies and all("; Secure" in cookie for cookie in cookies))
        with urllib.request.urlopen(f"https://localhost:{port}/api/certificate", context=trusting) as response:
            self.assertEqual(response.read(), (root / "tls" / "ca.pem").read_bytes())
            self.assertEqual(response.headers["Content-Type"], "application/x-x509-ca-cert")
        # One that does not trust it is refused by its own check, never served.
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(f"https://localhost:{port}/api/state", context=ssl.create_default_context())
        # An old plain-HTTP bookmark is sent to the same page over HTTPS.
        connection = http.client.HTTPConnection("localhost", port, timeout=10)
        connection.request("GET", "/?tab=player")
        answer = connection.getresponse()
        # Temporary and uncached, so turning HTTPS off later strands no browser.
        self.assertEqual(answer.status, 307)
        self.assertEqual(answer.getheader("Cache-Control"), "no-store")
        self.assertEqual(answer.getheader("Location"), f"https://localhost:{port}/?tab=player")
        connection.close()


class VoiceAndLibraryCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = web.SharedStorage(Path(temporary.name))
        self.storage.ensure()

    def add_voice(self, name, transcript, description=""):
        waveform = np.linspace(-0.5, 0.5, 2400, dtype=np.float32)
        save_voice(
            self.storage.voices / name, waveform, 24000, transcript, "FLOAT",
            description=description,
        )
        return self.storage.voices / name

    def serve(self, tts_models=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.storage = self.storage
        server.jobs = self.jobs = web.JobQueue()
        server.tts_models = tts_models or web.unconfigured_tts_models()
        server.verbose = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join()
            server.server_close()

        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_port}"

    def post(self, origin, path, body, headers=None):
        request = urllib.request.Request(
            f"{origin}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def delete(self, origin, kind, name, headers=None):
        return self.post(origin, f"/api/{kind}/delete", {"name": name}, headers)

    def test_previews_are_comparable_only_when_they_read_the_fixed_passage(self):
        fixed = self.add_voice(
            "zoe", web.VOICE_REFERENCE_TEXT, "Warm  british\nfemale narrator."
        )
        self.add_voice("Adam", "Older reference words.")
        rendered = self.add_voice("mark", "Older reference words.")
        (rendered / "preview.wav").write_bytes(b"fixed passage in Mark's voice")
        (self.storage.voices / "unfinished").mkdir()
        origin = self.serve()

        with urllib.request.urlopen(f"{origin}/api/voices") as response:
            voices = json.load(response)["voices"]
        with urllib.request.urlopen(f"{origin}/api/voices/preview?name=mark") as response:
            mark_preview = response.read()
        with urllib.request.urlopen(f"{origin}/api/voices/preview?name=zoe") as response:
            zoe_preview = response.read()
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{origin}/api/voices/preview?name=..%2FVoices")

        self.assertEqual(
            [(voice["name"], voice["comparable"]) for voice in voices],
            [("Adam", False), ("mark", True), ("zoe", True)],
        )
        self.assertEqual(voices[2]["description"], "Warm british female narrator.")
        self.assertEqual(mark_preview, b"fixed passage in Mark's voice")
        self.assertEqual(zoe_preview, (fixed / "reference.wav").read_bytes())
        self.assertEqual(refused.exception.code, 400)
        # A voice changed when its sample, transcript, or prompt last did; a
        # preview rendered later is not a change.
        os.utime(rendered / "reference.wav", (1_700_000_000, 1_700_000_000))
        os.utime(rendered / "transcript.txt", (1_700_000_100, 1_700_000_100))
        os.utime(rendered / "preview.wav", (1_800_000_000, 1_800_000_000))
        self.assertEqual(
            next(voice for voice in web.voice_catalog(self.storage) if voice["name"] == "mark")["modified"],
            1_700_000_100,
        )

    def test_every_new_voice_reads_the_fixed_passage(self):
        state = normalize({
            "tab": "voice",
            "voice": {
                "name": "Rebecca",
                "instruct": "Warm narrator.",
                "reference_text": "Words chosen in the browser.",
            },
        })
        models = {
            "design": {"source": "local", "model": "/models/design", "allow_downloads": False},
            "clone": {"source": "missing"},
        }
        with mock.patch.object(web, "_DEVICE_OPTIONS", [{"value": "cpu", "label": "CPU"}]):
            command = web.create_voice_command(
                web.values_of(state, models, self.storage), self.storage.drafts / "draft"
            )

        self.assertEqual(command[command.index("--text") + 1], web.VOICE_REFERENCE_TEXT)
        self.assertEqual(command[command.index("--instruct") + 1], "Warm narrator.")
        self.assertNotIn("Words chosen in the browser.", command)

    def test_rendering_skips_comparable_voices_and_never_publishes_a_failed_clip(self):
        self.add_voice("Fixed", web.VOICE_REFERENCE_TEXT)
        legacy = self.add_voice("Legacy", "Older reference words.")
        broken = self.add_voice("Broken", "Older reference words.")
        rendered = self.add_voice("Rendered", "Older reference words.")
        (rendered / "preview.wav").write_bytes(b"kept preview")
        fake = Path(self.storage.root) / "fake_narrate.py"
        fake.write_text(
            "import argparse, pathlib, sys\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('command')\n"
            "parser.add_argument('--voice-dir', type=pathlib.Path)\n"
            "parser.add_argument('--text')\n"
            "parser.add_argument('--output', type=pathlib.Path)\n"
            "args, _ = parser.parse_known_args()\n"
            "args.output.write_text(f'{args.voice_dir.name}|{args.text}', encoding='utf-8')\n"
            "sys.exit(3 if args.voice_dir.name == 'Broken' else 0)\n",
            encoding="utf-8",
        )
        clone = {"source": "local", "model": "/models/base", "allow_downloads": False}

        with mock.patch.object(web, "SCRIPT", fake), mock.patch("sys.stdout", io.StringIO()):
            failed = web.render_voice_previews(self.storage, clone, "cpu")

        self.assertEqual(failed, ["Broken"])
        self.assertEqual(
            (legacy / "preview.wav").read_text(encoding="utf-8"),
            f"Legacy|{web.VOICE_REFERENCE_TEXT}",
        )
        self.assertEqual((rendered / "preview.wav").read_bytes(), b"kept preview")
        self.assertFalse((self.storage.voices / "Fixed" / "preview.wav").exists())
        self.assertFalse((broken / "preview.wav").exists())
        self.assertEqual(list(self.storage.voices.glob("*/.preview-*")), [])

    def test_library_lists_books_with_their_voices_newest_first(self):
        paper = store_book(self.storage, "Attention Is All You Need", "a" * 64, "Martin")
        novel = store_book(self.storage, "Great Expectations", "b" * 64, "Sarah")
        (self.storage.audiobooks / "notes.txt").write_text("not a book", encoding="utf-8")
        (self.storage.audiobooks / ".build-unfinished").mkdir()

        def made(book, voice, seconds):
            audio = self.storage.audiobooks / book / "voices" / voice / "audio.mp3"
            os.utime(audio, (seconds, seconds))

        made(paper, "Martin", 1_700_000_000)
        made(novel, "Sarah", 1_800_000_000)
        origin = self.serve()

        with urllib.request.urlopen(f"{origin}/api/library") as response:
            books = json.load(response)["books"]

        self.assertEqual(
            [(book["id"], book["title"], book["source"], book["voice"]) for book in books],
            [
                (novel, "Great Expectations", "Great Expectations.pdf", "Sarah"),
                (paper, "Attention Is All You Need", "Attention Is All You Need.pdf", "Martin"),
            ],
        )
        self.assertEqual(paper, "attention-is-all-you-need--aaaaaaaaaaaa")
        # A book changes when one of its voices is made.
        self.assertEqual(books[0]["modified"], 1_800_000_000)

    def test_a_title_is_a_books_first_heading_unless_that_opens_a_section(self):
        # A figure before the title shows after its heading, in its block.
        self.assertEqual(web.audiobook_title(
            "<!-- audiobook-tts:block=0 -->\n\n# Attention Is *All* You Need\n\n"
            "![](data:image/png;base64,iVBORw0KGgo=)\n\n"
            "<!-- audiobook-tts:block=1 -->\n\nBody text.",
            "paper",
        ), "Attention Is All You Need")
        # A paper whose title was never a heading opens with its abstract.
        self.assertEqual(web.audiobook_title(
            "<!-- audiobook-tts:block=0 -->\n\nAneesh Pappu and James Zou.\n\n"
            "<!-- audiobook-tts:block=1 -->\n\n# 1 ABSTRACT\n\n"
            "<!-- audiobook-tts:block=2 -->\n\nBody text.",
            "agent teams",
        ), "agent teams")
        self.assertEqual(
            web.book_id("Ünïcode — Attention, Is All You Need?! " * 3, "f" * 64),
            "unicode-attention-is-all-you-need-unicode-attention-is-all--ffffffffffff",
        )

    def test_a_book_made_again_keeps_voices_of_the_same_text_and_marks_the_rest_stale(self):
        def narration(text):
            return {"schema": 1, "original_view": False, "passages": [
                {"id": 1, "type": "body", "page": 1, "text": text, "original_text": "",
                 "unchanged": True, "paragraphs": [0, 0], "sources": []},
            ]}

        book = store_book(self.storage, voice="Martin", narration=narration("Old text."))
        self.assertEqual(
            store_book(self.storage, voice="Sarah", narration=narration("Old text.")), book
        )
        sarah = self.storage.audiobooks / book / "voices" / "Sarah" / "audio.mp3"
        sarah_audio = sarah.read_bytes()
        record = json.loads((self.storage.audiobooks / book / "book.json").read_text())
        self.assertEqual(
            [(voice["name"], voice["status"]) for voice in record["voices"]],
            [("Sarah", "ready"), ("Martin", "ready")],
        )

        # Made again with new text, read by Martin: Sarah read the old text.
        store_book(self.storage, voice="Martin", narration=narration("New text."))
        record = json.loads((self.storage.audiobooks / book / "book.json").read_text())
        _, narration_sha256 = web.read_narration(self.storage.audiobooks / book)
        self.assertEqual(record["narration_sha256"], narration_sha256)
        self.assertEqual(
            {voice["name"]: voice["status"] for voice in record["voices"]},
            {"Martin": "ready", "Sarah": "stale"},
        )
        self.assertEqual(sarah.read_bytes(), sarah_audio)
        self.assertEqual(web.default_voice(record), "Martin")
        with self.assertRaises(web.StaleVoiceError):
            web.audiobook_reader_payload(self.storage, book, "Sarah")
        with self.assertRaises(web.StaleVoiceError):
            web.book_audio(self.storage, book, "Sarah")
        self.assertEqual(len([path for path in self.storage.audiobooks.iterdir()]), 1)

    def test_preview_refuses_a_voice_linked_from_outside_the_library(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        outside = Path(elsewhere.name) / "Private"
        save_voice(
            outside, np.linspace(-0.5, 0.5, 2400, dtype=np.float32), 24000,
            web.VOICE_REFERENCE_TEXT, "FLOAT",
        )
        (self.storage.voices / "Linked").symlink_to(outside, target_is_directory=True)
        origin = self.serve()

        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{origin}/api/voices/preview?name=Linked")

        self.assertEqual(refused.exception.code, 404)

    def test_deleting_a_voice_removes_it_from_the_library(self):
        self.add_voice("Keep", web.VOICE_REFERENCE_TEXT)
        gone = self.add_voice("Gone", "Older reference words.")
        (gone / "preview.wav").write_bytes(b"preview of the deleted voice")
        origin = self.serve()

        status, payload = self.delete(origin, "voices", "Gone")
        with urllib.request.urlopen(f"{origin}/api/voices") as response:
            listed = [voice["name"] for voice in json.load(response)["voices"]]

        self.assertEqual(status, 200)
        self.assertEqual(payload["assets"]["voices"], ["Keep"])
        self.assertEqual(listed, ["Keep"])
        self.assertEqual([path.name for path in self.storage.voices.iterdir()], ["Keep"])

    def test_renaming_a_voice_keeps_its_version_and_renames_it_in_the_books_it_read(self):
        voice = self.add_voice("Martin", web.VOICE_REFERENCE_TEXT, description="Calm.")
        first = web.saved_voice_version(voice)
        save_voice(
            voice, np.linspace(0.5, -0.5, 2400, dtype=np.float32), 24000,
            web.VOICE_REFERENCE_TEXT, "FLOAT", overwrite=True, description="Calmer.",
        )
        current = web.saved_voice_version(voice)
        # One book read each version; a third was read by an unrelated Martin.
        old = store_book(self.storage, "Old", "a" * 64, "Martin", voice_fields={"voice_version": first})
        new = store_book(self.storage, "New", "b" * 64, "Martin", voice_fields={"voice_version": current})
        other = store_book(self.storage, "Other", "c" * 64, "Martin", voice_fields={"voice_version": "f" * 64})
        origin = self.serve()

        status, payload = self.post(
            origin, "/api/voices/rename", {"name": "Martin", "new_name": " Marten "}
        )
        self.assertEqual((status, payload["name"], payload["assets"]["voices"]),
                         (200, "Marten", ["Marten"]))
        renamed = self.storage.voices / "Marten"
        self.assertEqual(web.saved_voice_version(renamed), current)
        self.assertEqual((renamed / "description.txt").read_text(encoding="utf-8"), "Calmer.")
        for book, name in ((old, "Marten"), (new, "Marten"), (other, "Martin")):
            path, record = web.read_book(self.storage, book)
            self.assertEqual([entry["name"] for entry in record["voices"]], [name], book)
            self.assertTrue((path / "voices" / name / "audio.mp3").is_file(), book)

        self.add_voice("Sarah", web.VOICE_REFERENCE_TEXT)
        refused = {
            "Sarah": 409,                    # another voice's name
            "../Sarah": 400, ".hidden": 400,  # not one plain name
        }
        for new_name, expected in refused.items():
            with self.subTest(new_name):
                status, _ = self.post(
                    origin, "/api/voices/rename", {"name": "Marten", "new_name": new_name}
                )
                self.assertEqual(status, expected)
        self.assertEqual(
            self.post(origin, "/api/voices/rename", {"name": "Nobody", "new_name": "X"})[0], 404
        )
        # A book that already has a voice of the new name stops the rename.
        store_book(self.storage, "Old", "a" * 64, "Mia", voice_fields={"voice_version": "e" * 64})
        status, payload = self.post(
            origin, "/api/voices/rename", {"name": "Marten", "new_name": "Mia"}
        )
        self.assertEqual(status, 409, payload)
        self.assertTrue(renamed.is_dir())
        # So does a job still reading with the voice.
        self.jobs.reserve_audiobook(
            "document-version", current, "Old.pdf", "Marten", "Old-Marten.mp3", "auto", None
        )
        status, payload = self.post(
            origin, "/api/voices/rename", {"name": "Marten", "new_name": "Max"}
        )
        self.assertEqual(status, 409, payload)
        self.assertTrue(renamed.is_dir())

    def test_deleting_a_linked_voice_removes_only_the_link(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        outside = Path(elsewhere.name) / "Private"
        save_voice(
            outside, np.linspace(-0.5, 0.5, 2400, dtype=np.float32), 24000,
            web.VOICE_REFERENCE_TEXT, "FLOAT",
        )
        link = self.storage.voices / "Linked"
        link.symlink_to(outside, target_is_directory=True)
        origin = self.serve()

        status, _ = self.delete(origin, "voices", "Linked")

        self.assertEqual(status, 200)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(web.is_saved_voice(outside))

    def test_deleting_an_audiobook_removes_the_book_with_every_voice(self):
        tale = store_book(self.storage, "Tale", "a" * 64, "Martin")
        store_book(self.storage, "Tale", "a" * 64, "Sarah")
        other = store_book(self.storage, "Other", "b" * 64, "Sarah")
        origin = self.serve()

        status, _ = self.delete(origin, "audiobooks", tale)
        with urllib.request.urlopen(f"{origin}/api/library") as response:
            listed = [entry["id"] for entry in json.load(response)["books"]]

        self.assertEqual(status, 200)
        self.assertEqual(listed, [other])
        self.assertEqual([path.name for path in self.storage.audiobooks.iterdir()], [other])

    def test_deleting_a_document_keeps_audiobooks_made_from_it(self):
        (self.storage.documents / "paper.pdf").write_bytes(b"%PDF-1.7\n")
        (self.storage.documents / "paper-narration.txt").write_text("Prepared.", encoding="utf-8")
        audiobook = self.storage.audiobooks / "paper-Martin.mp3"
        audiobook.write_bytes(b"narrated from paper.pdf")
        origin = self.serve()

        status, payload = self.delete(origin, "documents", "paper.pdf")

        self.assertEqual(status, 200)
        self.assertEqual(payload["assets"]["documents"], ["paper-narration.txt"])
        self.assertTrue(audiobook.is_file())

    def test_delete_refuses_traversal_missing_assets_and_other_origins(self):
        keep = self.add_voice("Keep", web.VOICE_REFERENCE_TEXT)
        (self.storage.documents / "folder").mkdir()
        origin = self.serve()

        traversal = self.delete(origin, "voices", "../Voices/Keep")
        missing = self.delete(origin, "audiobooks", "missing--000000000000")
        directory = self.delete(origin, "documents", "folder")
        foreign = self.delete(origin, "voices", "Keep", {"Origin": "http://attacker.example"})

        self.assertEqual(traversal[0], 400)
        self.assertEqual(missing, (404, {"error": "That audiobook no longer exists."}))
        self.assertEqual(directory, (404, {"error": "That book no longer exists."}))
        self.assertEqual(foreign[0], 403)
        self.assertTrue(web.is_saved_voice(keep))

    def test_a_library_left_in_the_project_folder_stops_a_new_empty_one(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        old, new = root / "project" / "User", root / "home" / ".hilde" / "library"
        (old / "Audiobooks" / ".backup").mkdir(parents=True)
        with mock.patch.object(web, "PROJECT_STORAGE_ROOT", old), mock.patch.object(web, "DEFAULT_STORAGE_ROOT", new):
            # Only hidden leftovers: nothing to move, the new library starts.
            self.assertIsNone(web.library_left_behind(new))
            (old / "Audiobooks" / "a-book--0123456789ab").mkdir()
            # Starting a new library here would show no books: the server stops and says where they are.
            self.assertEqual(web.library_left_behind(new), old)
            # Another root, or a default that exists already, is the user's choice.
            self.assertIsNone(web.library_left_behind(old))
            new.mkdir(parents=True)
            self.assertIsNone(web.library_left_behind(new))

    def test_stock_voices_seed_only_a_new_library(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        stock = Path(temporary.name) / "stock"
        stock.mkdir()
        for name in ("Astrid", "Birger"):
            save_voice(
                stock / name, np.linspace(-0.5, 0.5, 2400, dtype=np.float32), 24000,
                web.VOICE_REFERENCE_TEXT, "FLOAT", description=f"{name} narrator.",
            )
        (stock / "Birger" / ".DS_Store").write_bytes(b"Finder metadata")
        fresh = web.SharedStorage(Path(temporary.name) / "fresh")
        existing = web.SharedStorage(Path(temporary.name) / "existing")
        existing.voices.mkdir(parents=True)

        with mock.patch.object(web, "STOCK_VOICES_PATH", stock):
            web.prepare_library(fresh)
            seeded = sorted(path.name for path in fresh.voices.iterdir())
            web.delete_voice(fresh, "Astrid")
            web.prepare_library(fresh)
            web.prepare_library(existing)

        self.assertEqual(seeded, ["Astrid", "Birger"])
        self.assertEqual([path.name for path in fresh.voices.iterdir()], ["Birger"])
        self.assertEqual(
            sorted(path.name for path in (fresh.voices / "Birger").iterdir()),
            ["description.txt", "reference.wav", "transcript.txt"],
        )
        self.assertEqual(
            (fresh.voices / "Birger" / "reference.wav").read_bytes(),
            (stock / "Birger" / "reference.wav").read_bytes(),
        )
        self.assertEqual(list(existing.voices.iterdir()), [])

    def test_stock_voices_are_valid_and_preview_the_fixed_passage(self):
        voices = sorted(
            path for path in web.STOCK_VOICES_PATH.iterdir() if not path.name.startswith(".")
        )
        self.assertTrue(voices)
        for voice in voices:
            with self.subTest(voice=voice.name):
                read_voice(voice)
                self.assertEqual(web.voice_preview(voice), (voice / "reference.wav", True))
                self.assertTrue((voice / "description.txt").read_text(encoding="utf-8").strip())

    def test_listen_makes_a_draft_and_save_keeps_exactly_that_clip(self):
        freyja = self.add_voice("Freyja", web.VOICE_REFERENCE_TEXT, "Old prompt.")
        (freyja / "preview.wav").write_bytes(b"preview of the old voice")
        before = (freyja / "reference.wav").read_bytes()
        fake = Path(self.storage.root) / "fake_design.py"
        fake.write_text(
            "import argparse, pathlib, numpy, soundfile\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('command')\n"
            "parser.add_argument('--voice-dir', type=pathlib.Path)\n"
            "parser.add_argument('--instruct')\n"
            "parser.add_argument('--text')\n"
            "args, _ = parser.parse_known_args()\n"
            "args.voice_dir.mkdir(parents=True)\n"
            "soundfile.write(args.voice_dir / 'reference.wav',\n"
            "                numpy.full(2400, 0.25, 'float32'), 24000, subtype='FLOAT')\n"
            "(args.voice_dir / 'transcript.txt').write_text(args.text, encoding='utf-8')\n"
            "(args.voice_dir / 'description.txt').write_text(args.instruct, encoding='utf-8')\n",
            encoding="utf-8",
        )
        models = {
            "design": {"source": "local", "model": "/models/design", "allow_downloads": False},
            "clone": {"source": "missing"},
        }
        state = normalize({"tab": "voice", "voice": {"name": "Freyja", "instruct": "New prompt."}})
        with mock.patch.object(web, "SCRIPT", fake), \
                mock.patch.object(web, "_DEVICE_OPTIONS", [{"value": "cpu", "label": "CPU"}]):
            origin = self.serve(models)
            status, _ = self.post(origin, "/api/run", {"state": state})
            deadline = time.monotonic() + 30
            while self.jobs.exclusive is not None and time.monotonic() < deadline:
                time.sleep(0.05)
        [draft] = list(self.storage.drafts.iterdir())
        with urllib.request.urlopen(f"{origin}/api/voices/draft?id={draft.name}") as response:
            heard, _ = sf.read(io.BytesIO(response.read()), dtype="float32")
        untouched = (
            (freyja / "reference.wav").read_bytes() == before
            and (freyja / "description.txt").read_text(encoding="utf-8") == "Old prompt."
        )
        saved = self.post(origin, "/api/voices/save", {"draft": draft.name, "name": "Freyja"})

        self.assertEqual(status, 200)
        self.assertTrue(untouched)
        self.assertEqual(saved[0], 200)
        np.testing.assert_array_equal(sf.read(freyja / "reference.wav", dtype="float32")[0], heard)
        self.assertEqual((freyja / "description.txt").read_text(encoding="utf-8"), "New prompt.")
        self.assertEqual(
            (freyja / "transcript.txt").read_text(encoding="utf-8"), web.VOICE_REFERENCE_TEXT
        )
        self.assertFalse((freyja / "preview.wav").exists())
        self.assertFalse(draft.exists())

    def test_save_refuses_missing_drafts_bad_names_and_linked_voices(self):
        draft = self.storage.drafts / "0123456789abcdef"
        save_voice(
            draft, np.linspace(-0.5, 0.5, 2400, dtype=np.float32), 24000,
            web.VOICE_REFERENCE_TEXT, "FLOAT", description="Prompt.",
        )
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        (self.storage.voices / "Linked").symlink_to(elsewhere.name, target_is_directory=True)
        origin = self.serve()

        missing = self.post(origin, "/api/voices/save", {"draft": "feedfacefeedface", "name": "Nova"})
        bad_name = self.post(origin, "/api/voices/save", {"draft": draft.name, "name": "../Nova"})
        linked = self.post(origin, "/api/voices/save", {"draft": draft.name, "name": "Linked"})
        with self.assertRaises(urllib.error.HTTPError) as traversal:
            urllib.request.urlopen(f"{origin}/api/voices/draft?id=..%2FVoices")

        self.assertEqual(missing, (404, {"error": "That draft no longer exists. Listen again."}))
        self.assertEqual(bad_name, (400, {"error": "Enter a voice name without a slash."}))
        self.assertEqual(linked, (400, {"error": "An existing voice must be a directory."}))
        self.assertEqual(traversal.exception.code, 400)
        self.assertTrue(web.is_saved_voice(draft))
        self.assertEqual(os.listdir(elsewhere.name), [])

    def test_listen_keeps_only_the_newest_drafts(self):
        for index in range(web.VOICE_DRAFT_LIMIT + 2):
            old = self.storage.drafts / f"draft{index:02d}"
            old.mkdir()
            os.utime(old, ns=(index * 10**9,) * 2)

        fresh = web.new_voice_draft(self.storage)

        self.assertEqual(
            sorted(path.name for path in self.storage.drafts.iterdir()),
            [f"draft{index:02d}" for index in range(3, web.VOICE_DRAFT_LIMIT + 2)],
        )
        self.assertEqual(fresh.parent, self.storage.drafts)
        self.assertFalse(fresh.exists())


class DocumentUploadTests(unittest.TestCase):
    def test_a_refused_upload_leaves_its_connection_usable(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.storage = storage
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            try:
                # A file Hilde does not read is refused by its name; its body
                # must not become the start of the next request.
                connection.request("POST", "/api/documents/upload?name=notes.docx", body=b"PK\x03\x04 not a document",
                                   headers={"Content-Type": "application/octet-stream"})
                refused = connection.getresponse()
                refused_answer = (refused.status, json.loads(refused.read())["error"])
                connection.request("POST", "/api/documents/upload?name=notes.txt", body=b"Plain text.",
                                   headers={"Content-Type": "application/octet-stream"})
                accepted = connection.getresponse()
                accepted_answer = (accepted.status, json.loads(accepted.read())["name"])
            finally:
                connection.close()
                server.shutdown()
                thread.join()
        self.assertEqual(refused_answer, (400, "Document filename must end in PDF, text, or Markdown."))
        self.assertEqual(accepted_answer, (201, "notes.txt"))
        self.assertEqual((storage.documents / "notes.txt").read_text(), "Plain text.")


class ReaderAudioIndexTests(unittest.TestCase):
    def test_reader_mp4_indexes_every_mp3_frame_on_the_decoded_timeline(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        rate = 24000
        waveform = 0.3 * np.random.default_rng(3).standard_normal(3 * rate)
        # Alternating noise and silence gives the default VBR encoding varied
        # frame sizes, the layout browsers cannot seek exactly as plain MP3.
        waveform[np.arange(waveform.size) // (rate // 2) % 2 == 1] = 0
        staged = storage.root / "indexed-book.mp3"
        sf.write(staged, waveform, rate, format="MP3", subtype="MPEG_LAYER_III")
        mp3 = staged.read_bytes()
        book_id = store_book(storage, voice="Eir", audio=mp3)
        book = storage.audiobooks / book_id / "voices" / "Eir" / "audio.mp3"
        mp3_path = f"/api/audio?book={book_id}&voice=Eir"
        mp4_path = f"{mp3_path}&container=mp4"

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.storage = storage
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            try:
                connection.request("GET", mp4_path)
                response = connection.getresponse()
                kind = response.headers["Content-Type"]
                mp4 = response.read()
                table = mp4_box_payload(
                    mp4, b"moov", b"trak", b"mdia", b"minf", b"stbl"
                )
                offset = struct.unpack_from(">I", mp4_box_payload(table, b"stco"), 8)[0]
                # Both ranges share one keep-alive connection, so an overlong
                # body would corrupt the following response.
                ranges = {}
                for path, first, last in (
                    (mp3_path, 10, 19),
                    (mp4_path, offset - 5, offset + 4),
                ):
                    connection.request(
                        "GET", path, headers={"Range": f"bytes={first}-{last}"}
                    )
                    response = connection.getresponse()
                    ranges[path] = (response.status, response.read())
            finally:
                connection.close()
                server.shutdown()
                thread.join()

        self.assertEqual(kind, "audio/mp4")
        # Safari plays MP3 in MP4 only from QuickTime's `.mp3` sample entry;
        # given `mp4a`, it falls back to the plain MP3 and seeks it seconds off.
        self.assertEqual(mp4_box_payload(table, b"stsd")[12:16], b".mp3")
        sample_sizes = mp4_box_payload(table, b"stsz")
        count = struct.unpack_from(">I", sample_sizes, 8)[0]
        sizes = struct.unpack_from(f">{count}I", sample_sizes, 12)
        payload = mp4[offset:]
        self.assertGreater(len(set(sizes)), 1)
        self.assertEqual(sum(sizes), len(payload))
        # Every audio frame after the MP3's Xing tag frame, byte for byte.
        self.assertTrue(mp3.endswith(payload))
        self.assertIn(b"Xing", mp3[:len(mp3) - len(payload)])
        self.assertEqual(
            struct.unpack_from(">III", mp4_box_payload(table, b"stts"), 4),
            (1, count, 576),
        )
        media = mp4_box_payload(mp4, b"moov", b"trak", b"mdia", b"mdhd")
        self.assertEqual(struct.unpack_from(">I", media, 12)[0], rate)
        entries, duration, media_time = struct.unpack_from(
            ">IIi", mp4_box_payload(mp4, b"moov", b"trak", b"edts", b"elst"), 4
        )
        # Media time zero is the first decoded sample, the origin of reader cues.
        self.assertEqual(entries, 1)
        self.assertEqual(duration, sf.info(book).frames)
        self.assertLessEqual(media_time + duration, count * 576)
        self.assertEqual(ranges[mp3_path], (206, mp3[10:20]))
        self.assertEqual(ranges[mp4_path], (206, mp4[offset - 5:offset + 5]))


class PaperUrlTests(unittest.TestCase):
    def test_direct_markdown_url_is_downloaded_and_processed(self):
        source = (
            b"Remote source paragraph with an inline attribution.\n\n"
            b"References\n\n"
            b"Smith, A. Bibliographic entry that must not reach a worker."
        )

        class SourceHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/download?id=paper":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(source)))
                self.end_headers()
                self.wfile.write(source)

            def log_message(self, format, *args):
                pass

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        output = root / "remote-audiobook.txt"
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                self.assert_source = request_path.read_text(encoding="utf-8")
                return (
                    "<NARRATION>Remote narration.</NARRATION>"
                    "<SUMMARY>Remote summary.</SUMMARY>"
                )

        with ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                source_url = (
                    f"http://127.0.0.1:{server.server_port}/download?id=paper"
                )
                run = StubPaperRun(
                    source_url,
                    output,
                    "utf-8",
                    in_flight=1,
                    prompt_path=prompt,
                )
                run.pump()
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual(run.code, 0)
        self.assertEqual(output.read_text(encoding="utf-8"), "Remote narration.")
        self.assertIn("Remote source paragraph with an inline attribution.", run.assert_source)
        self.assertNotIn("References", run.assert_source)
        self.assertNotIn("Bibliographic entry", run.assert_source)
        self.assertTrue(any(
            event == "log" and "Downloaded" in data
            for event, data in run.history
        ))


class PaperResponseTests(unittest.TestCase):
    def test_preparation_drops_invisible_control_paragraphs(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source.txt"
        output = root / "prepared.txt"
        source.write_text(
            "First paragraph.\n\n\u200b\n\n&#8203;\n\n\u2060\n\nSecond paragraph.",
            encoding="utf-8",
        )
        run = PaperRun(source, output, "utf-8", adapt=False)

        run.prepare_document(root / "scratch")

        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "First paragraph.\n\nSecond paragraph.",
        )

    def test_parser_accepts_one_tagged_payload_with_surrounding_commentary(self):
        self.assertEqual(
            parse_paper_response(
                "Here is the requested transport payload:\n"
                "<NARRATION>Spoken paragraph.</NARRATION>\n"
                "<SUMMARY>Short context.</SUMMARY>\n"
                "End of payload."
            ),
            ("Spoken paragraph.", "Short context.", []),
        )
        # Tags are trimmed and kept once each, in order.
        self.assertEqual(
            parse_paper_response(
                "<NARRATION>Spoken.</NARRATION><SUMMARY>Context.</SUMMARY>"
                "<TAGS>\n- Sediment transport, sampling bias,\nsediment TRANSPORT\n</TAGS>"
            )[2],
            ["Sediment transport", "sampling bias"],
        )
        # A model that ends before closing TAGS still gave its tags.
        self.assertEqual(
            parse_paper_response(
                "<NARRATION>Spoken.</NARRATION><SUMMARY>Context.</SUMMARY><TAGS>\nstorm events, silt"
            )[2],
            ["storm events", "silt"],
        )

    def test_paper_run_retries_a_malformed_response_without_losing_progress(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        output = root / "paper-audiobook.txt"
        prompt = root / "prompt.md"
        source.write_text("First source.\n\nSecond source.", encoding="utf-8")
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")

        class StubPaperRun(PaperRun):
            responses = iter((
                "<NARRATION>First narration.</NARRATION>"
                "<SUMMARY>First summary.</SUMMARY>",
                "Second narration without the required transport wrapper.",
                "<NARRATION>Second narration.</NARRATION>"
                "<SUMMARY>Second summary.</SUMMARY>",
            ))

            def model_response(self, request_path, system_prompt, attachments=()):
                return next(self.responses)

        completed_outputs = []
        run = StubPaperRun(
            source,
            output,
            "utf-8",
            in_flight=1,
            prompt_path=prompt,
            on_success=completed_outputs.append,
        )
        run.pump()

        self.assertEqual(run.code, 0)
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "First narration.\n\nSecond narration.",
        )
        self.assertEqual(completed_outputs, [str(output)])
        self.assertTrue(any(
            event == "log" and "retrying" in data
            for event, data in run.history
        ))

    def test_left_out_batches_add_no_text_and_figures_keep_their_place(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        stage = root / "stage"
        output = stage / "prepared.txt"
        prompt = root / "prompt.md"
        paragraphs = [
            "First body paragraph.",
            "![](images/figure-1.png)",
            "[25] Marcus, M. Building the Penn Treebank. 1993.",
            "![Figure 2](images/figure-2.png)\nFigure 2: The model.",
        ]
        source.write_text("\n\n".join(paragraphs), encoding="utf-8")
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        # The model leaves out the reference entry, as the prompt tells it to,
        # and the bare image, which has no text to read.
        narrations = {1: "First narration.", 2: "", 3: "", 4: "Figure two shows the model."}
        requested = []

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                if request_path.stem.endswith("-kind"):
                    return "OTHER"
                start = int(request_path.stem.split("-")[1])
                requested.append(start)
                return (
                    f"<NARRATION>\n{narrations[start]}\n</NARRATION>"
                    f"<SUMMARY>Paragraph {start}.</SUMMARY>"
                )

        run = StubPaperRun(
            source, output, "utf-8", in_flight=1, prompt_path=prompt, scratch_path=stage
        )
        run.pump()

        self.assertEqual(run.code, 0)
        self.assertEqual(sorted(requested), [1, 2, 3, 4])
        prepared = output.read_text(encoding="utf-8")
        self.assertEqual(prepared, "First narration.\n\nFigure two shows the model.")
        blocks, *_ = web._reader_blocks(
            prepared, "\n\n".join(paragraphs), 500, stage / "paragraph-checkpoints"
        )
        self.assertEqual(blocks[0], "First narration.\n\n![](images/figure-1.png)")
        self.assertIn("![Figure 2](images/figure-2.png)", blocks[1])

    def test_changed_harness_instructions_redo_the_adaptation(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        stage = root / "stage"
        prompt = root / "prompt.md"
        source.write_text("First source.\n\nSecond source.", encoding="utf-8")
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        requested = []

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                requested.append(request_path.stem)
                return "<NARRATION>Narration.</NARRATION><SUMMARY>Summary.</SUMMARY>"

        def adapt():
            run = StubPaperRun(
                source, stage / "prepared.txt", "utf-8", in_flight=1,
                prompt_path=prompt, scratch_path=stage,
            )
            run.pump()
            self.assertEqual(run.code, 0)

        adapt()
        adapt()
        self.assertEqual(len(requested), 2)
        changed = web.PAPER_LOOP_INSTRUCTION + "\nChanged instruction."
        with mock.patch.object(web, "PAPER_LOOP_INSTRUCTION", changed):
            adapt()
        self.assertEqual(len(requested), 4)


    def test_interrupted_adaptation_resumes_from_committed_batch(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        stage = root / "stage"
        output = stage / "prepared.txt"
        prompt = root / "prompt.md"
        source.write_text("First source.\n\nSecond source.", encoding="utf-8")
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")

        class InterruptedPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                start = int(request_path.stem.split("-")[1])
                if start == 2:
                    raise RuntimeError("simulated interruption")
                return (
                    "<NARRATION>First narration.</NARRATION>"
                    "<SUMMARY>First summary.</SUMMARY>"
                )

        first = InterruptedPaperRun(
            source,
            output,
            "utf-8",
            in_flight=1,
            prompt_path=prompt,
            scratch_path=stage,
        )
        first.pump()
        self.assertEqual(first.code, 1)
        self.assertEqual(
            output.read_text(encoding="utf-8"), "First narration."
        )

        requested = []

        class ResumedPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                start = int(request_path.stem.split("-")[1])
                requested.append(start)
                return (
                    "<NARRATION>Second narration.</NARRATION>"
                    "<SUMMARY>Second summary.</SUMMARY>"
                )

        resumed = ResumedPaperRun(
            source,
            output,
            "utf-8",
            in_flight=1,
            prompt_path=prompt,
            scratch_path=stage,
        )
        resumed.pump()

        self.assertEqual(resumed.code, 0)
        self.assertEqual(requested, [2])
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "First narration.\n\nSecond narration.",
        )


class PaperConcurrencyTests(unittest.TestCase):
    def test_rolling_pool_refills_and_commits_in_source_order(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        output = root / "paper-audiobook.txt"
        prompt = root / "prompt.md"
        source.write_text(
            "\n\n".join(f"Source {index}." for index in range(1, 7)),
            encoding="utf-8",
        )
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        started = {index: threading.Event() for index in range(1, 7)}
        release = {index: threading.Event() for index in range(1, 7)}
        activity = {"current": 0, "maximum": 0}
        activity_lock = threading.Lock()

        class RollingPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                index = int(request_path.stem.rsplit("-", 1)[1])
                with activity_lock:
                    activity["current"] += 1
                    activity["maximum"] = max(
                        activity["maximum"], activity["current"]
                    )
                started[index].set()
                try:
                    if not release[index].wait(5):
                        raise RuntimeError(f"paragraph {index} was not released")
                finally:
                    with activity_lock:
                        activity["current"] -= 1
                return (
                    f"<NARRATION>Narration {index}.</NARRATION>"
                    f"<SUMMARY>Summary {index}.</SUMMARY>"
                )

        run = RollingPaperRun(
            source, output, "utf-8", in_flight=4, prompt_path=prompt
        )
        worker = threading.Thread(target=run.pump)
        worker.start()
        try:
            for index in range(1, 5):
                self.assertTrue(started[index].wait(2))
            release[4].set()
            self.assertTrue(started[5].wait(2))
            release[2].set()
            self.assertTrue(started[6].wait(2))
        finally:
            for signal in release.values():
                signal.set()
            worker.join(10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(run.code, 0)
        self.assertEqual(activity["maximum"], 4)
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "\n\n".join(f"Narration {index}." for index in range(1, 7)),
        )
        self.assertEqual(
            [
                data["done"]
                for event, data in run.history
                if event == "progress"
            ],
            list(range(1, 7)),
        )
        activity_events = [
            data for event, data in run.history if event == "activity"
        ]
        self.assertTrue(any(
            data["waiting_for"] == 1 and data["completed"] > data["committed"]
            for data in activity_events
        ))
        self.assertEqual(
            activity_events[-1],
            {
                "completed": 6,
                "committed": 6,
                "total": 6,
                "waiting_for": None,
            },
        )

    def test_configured_worker_batches_roll_and_commit_in_source_order(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        output = root / "paper-audiobook.txt"
        prompt = root / "prompt.md"
        source.write_text(
            "\n\n".join(f"Source {index}." for index in range(1, 8)),
            encoding="utf-8",
        )
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        started = {index: threading.Event() for index in (1, 4, 7)}
        release = {index: threading.Event() for index in (1, 4, 7)}
        requests = {}
        activity = {"current": 0, "maximum": 0}
        activity_lock = threading.Lock()

        class BatchingPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                _, start_text, end_text = request_path.stem.split("-")
                start, end = int(start_text), int(end_text)
                requests[start] = request_path.read_text(encoding="utf-8")
                with activity_lock:
                    activity["current"] += 1
                    activity["maximum"] = max(
                        activity["maximum"], activity["current"]
                    )
                started[start].set()
                try:
                    if not release[start].wait(5):
                        raise RuntimeError(f"batch {start}-{end} was not released")
                finally:
                    with activity_lock:
                        activity["current"] -= 1
                return (
                    f"<NARRATION>Narration {start}-{end}.</NARRATION>"
                    f"<SUMMARY>Summary {start}-{end}.</SUMMARY>"
                )

        run = BatchingPaperRun(
            source,
            output,
            "utf-8",
            in_flight=2,
            paragraphs_per_worker=3,
            prompt_path=prompt,
        )
        worker = threading.Thread(target=run.pump)
        worker.start()
        try:
            self.assertTrue(started[1].wait(2))
            self.assertTrue(started[4].wait(2))
            release[4].set()
            self.assertTrue(started[7].wait(2))
        finally:
            for signal in release.values():
                signal.set()
            worker.join(10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(run.code, 0)
        self.assertEqual(activity["maximum"], 2)
        self.assertEqual(set(requests), {1, 4, 7})
        self.assertIn("Source 1.", requests[1])
        self.assertIn("Source 3.", requests[1])
        self.assertNotIn("Source 4.", requests[1])
        # The batch's place in the book is not shown: a model borrows it as a label.
        self.assertNotIn("number=", requests[1])
        self.assertIn("Current source, 3 paragraphs:", requests[4])
        self.assertNotIn("of 7", requests[4])
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "Narration 1-3.\n\nNarration 4-6.\n\nNarration 7-7.",
        )
        self.assertEqual(
            [
                data["done"]
                for event, data in run.history
                if event == "progress"
            ],
            [3, 6, 7],
        )

    def test_summary_context_stays_bounded_for_long_papers(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        output = root / "paper-audiobook.txt"
        prompt = root / "prompt.md"
        source.write_text(
            "\n\n".join(f"Source {index}." for index in range(1, 13)),
            encoding="utf-8",
        )
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        requests = {}

        class BoundedContextPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                _, start_text, _ = request_path.stem.split("-")
                index = int(start_text)
                requests[index] = request_path.read_text(encoding="utf-8")
                return (
                    f"<NARRATION>Narration {index}.</NARRATION>"
                    f"<SUMMARY>Summary {index} {'x' * 180}.</SUMMARY>"
                )

        run = BoundedContextPaperRun(
            source,
            output,
            "utf-8",
            in_flight=1,
            summary_context_chars=512,
            prompt_path=prompt,
        )
        run.pump()

        self.assertEqual(run.code, 0)
        context = requests[12].split(
            "Compacted summaries from earlier source batches completed "
            "before dispatch:\n",
            1,
        )[1].split("\nSome immediately preceding batches", 1)[0]
        self.assertLessEqual(len(context), 512)
        # The first summary and the latest stay; no batch's place in the book is shown.
        self.assertTrue(context.startswith("- Summary 1 "), context[:40])
        self.assertIn("\n- Summary 11 ", context)
        self.assertNotIn("Paragraph", context)
        self.assertIn("older source-batch summaries omitted", context)


    def test_stop_cuts_off_every_inflight_model_request(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        output = root / "paper-audiobook.txt"
        prompt = root / "prompt.md"
        source.write_text(
            "\n\n".join(f"Source {index}." for index in range(1, 5)),
            encoding="utf-8",
        )
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        arrived = threading.Semaphore(0)
        release = threading.Event()
        self.addCleanup(release.set)

        class BusyHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
                arrived.release()
                # A busy server: the answer never starts.
                release.wait(60)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), BusyHandler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        patcher = mock.patch.object(web, "local_model_names", return_value=("local-model",))
        patcher.start()
        self.addCleanup(patcher.stop)
        run = PaperRun(
            source, output, "utf-8", model="lm-studio/local-model",
            local_servers=[{"server": f"127.0.0.1:{server.server_port}", "provider": "lm-studio"}],
            in_flight=4, prompt_path=prompt,
        )
        worker = threading.Thread(target=run.pump)
        worker.start()
        try:
            for _ in range(4):
                self.assertTrue(arrived.acquire(timeout=5))
        finally:
            run.stop()
            worker.join(10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(run.code, 130)
        with run.process_lock:
            self.assertEqual(run.streams, set())


class GroundingTests(unittest.TestCase):
    REFERENCES = [
        "**Acknowledgements** We thank our colleagues. **References** [1] Jimmy Lei Ba, "
        "Jamie Ryan Kiros, and Geoffrey E Hinton. Layer normalization. arXiv, 2016.",
        "- [2] Dzmitry Bahdanau, Kyunghyun Cho, and Yoshua Bengio. Neural machine "
        "translation. CoRR, abs/1409.0473, 2014.",
        "- [3] Francois Chollet. Xception. arXiv preprint arXiv:1610.02357, 2016.",
        "- [4] Quoc V. Le & Tomas Mikolov. Distributed representations. In ICML, 2014.",
    ]

    def test_a_needed_citation_names_its_authors_and_a_parenthetical_one_goes(self):
        entries = web.reference_entries(self.REFERENCES)
        self.assertEqual(entries, {
            1: ["Ba", "Kiros", "Hinton"], 2: ["Bahdanau", "Cho", "Bengio"],
            3: ["Chollet"], 4: ["Le", "Mikolov"],
        })
        self.assertEqual(
            web.resolve_citations(
                "Normalization [1] helps. We follow [3], as in [4] and in [2–3]. "
                "[1] showed it, unlike [9].", entries,
            ),
            "Normalization helps. We follow Chollet, as in Le and Mikolov and in "
            "Bahdanau and Chollet. Ba and colleagues showed it, unlike earlier work.",
        )
        # Citations joined by "and" or a comma are one chain, named together:
        # the chain after a named one never leaves "and" hanging.
        self.assertEqual(
            web.resolve_citations("models such as [3, 9] and [2]. Networks [4], [1] work.", entries),
            "models such as Chollet and Bahdanau. Networks work.",
        )
        self.assertEqual(
            web.resolve_citations("as in [1, 2, 3].", entries), "as in Ba, Bahdanau, and Chollet.",
        )
        # A citation that is the subject of the next clause is a chain of its own.
        self.assertEqual(
            web.resolve_citations("as shown in [3], and [4] later extended it.", entries),
            "as shown in Chollet, and Le and Mikolov later extended it.",
        )
        self.assertEqual(
            web.resolve_citations("[3] and [4] showed it.", entries), "Chollet and Le showed it.",
        )
        # Two bracketed numbers are no reference list, and nothing changes.
        self.assertEqual(web.reference_entries(self.REFERENCES[1:3]), {})
        self.assertEqual(web.resolve_citations("Networks [13] work.", {}), "Networks [13] work.")

    def test_a_figure_goes_to_the_model_alone_and_prose_with_the_summaries(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text(
            "The encoder maps the input to a sequence of representations.\n\n"
            "![](images/a.png)\n\n"
            "Figure 1: The Transformer architecture.\n\n"
            "The decoder is shown in the right half of Figure 1, similar to [3].",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        requests = {}

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                requests[request_path.stem] = request_path.read_text(encoding="utf-8")
                return "<NARRATION>Narrated.</NARRATION><SUMMARY>Summary.</SUMMARY>"

        run = StubPaperRun(source, root / "prepared.txt", "utf-8", in_flight=1, prompt_path=prompt)
        run.pump()

        self.assertEqual(run.code, 0)
        figure = requests["paragraphs-2-3"]
        self.assertNotIn("Compacted summaries", figure)
        self.assertNotIn("The encoder maps", figure)
        self.assertIn("Compacted summaries", requests["paragraphs-4-4"])

    def test_a_note_beside_one_author_carries_that_authors_name(self):
        paragraphs = [
            "**Mara Ellison**<sup>_∗_</sup>, Northfield Institute; "
            "**Tomas Reyes**<sup>_∗†_</sup>, Harbor University",
            "> _†_ Now at Acme Labs.",
            "**Priya Anand**<sup>_∗_</sup>, Harbor University",
            "> _∗_ Equal contribution.",
            "## Abstract",
            "We scale the dot products.<sup>4</sup>",
            "> 4To illustrate why the dot products get large, assume independence.",
        ]
        named = web.name_author_notes(paragraphs, web._layout_kinds(paragraphs))
        self.assertEqual(named[1], "> _†_ Tomas Reyes: Now at Acme Labs.")
        # A note every author carries, and a note in the body, stay as printed.
        self.assertEqual(named[3:], paragraphs[3:])

    def test_prose_narrated_with_sentences_missing_is_asked_again_and_keeps_the_fuller_answer(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        first = "Recurrent models factor computation along the symbol positions of the sequences."
        second = "This inherently sequential nature precludes parallelization within training examples."
        source.write_text(f"{first} {second}", encoding="utf-8")
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")

        def adapt(answer_again):
            requests = []

            class StubPaperRun(PaperRun):
                def model_response(self, request_path, system_prompt, attachments=()):
                    text = request_path.read_text(encoding="utf-8")
                    requests.append(text)
                    narration = answer_again if "reworded these sentences" in text else first
                    return f"<NARRATION>{narration}</NARRATION><SUMMARY>Summary.</SUMMARY>"

            output = root / f"prepared-{len(requests)}-{len(answer_again)}.txt"
            run = StubPaperRun(source, output, "utf-8", in_flight=1, prompt_path=prompt)
            run.pump()
            self.assertEqual(run.code, 0)
            # The second request names the sentence the first answer dropped.
            self.assertEqual(len(requests), 2)
            self.assertIn(f"- {second}", requests[1])
            return output.read_text(encoding="utf-8")

        self.assertIn(second, adapt(f"{first} {second}"))
        # A second answer that keeps less loses to the first.
        self.assertIn(first, adapt("Models are sequential."))
        # Email addresses, which the prompt leaves out, are never asked back.
        paragraph = ("Correspondence about this sentence goes to the three authors listed above, "
                     "jonathanho@berkeley.edu, ajayj@berkeley.edu, and pabbeel@cs.berkeley.edu.")
        narration = "Correspondence about this sentence goes to the three authors listed above."
        self.assertEqual(web.prose_kept(paragraph, narration)[0], 1)
        self.assertEqual(web.missing_sentences([paragraph], narration), [])

    def test_text_left_out_whole_is_asked_again_and_author_lines_then_read_as_printed(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text(
            "We grant permission to reproduce the figures.\n\n"
            "**Mara Ellison**<sup>_∗†_</sup>, Northfield Institute; "
            "**Tomas Reyes**<sup>_∗_</sup>, Harbor University `mara@example.org`\n\n"
            "> _†_ Now at Acme Labs.\n\n"
            "## Abstract\n\n"
            "We study how rivers carry silt.\n\n"
            "- [7] Ofir Press and Lior Wolf. Using the output embedding. 2016.",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")

        def adapt(second_answer):
            requests = []

            class StubPaperRun(PaperRun):
                def model_response(self, request_path, system_prompt, attachments=()):
                    text = request_path.read_text(encoding="utf-8")
                    requests.append(text)
                    narration = second_answer if "title block" in text else ""
                    return f"<NARRATION>{narration}</NARRATION><SUMMARY>Left out.</SUMMARY>"

            output = root / f"prepared-{len(second_answer)}.txt"
            run = StubPaperRun(source, output, "utf-8", in_flight=1, prompt_path=prompt)
            run.pump()
            self.assertEqual(run.code, 0)
            # Only the batch with author lines is asked again as a title block.
            self.assertEqual(sum("title block" in text for text in requests), 1)
            # Prose left out whole is asked again; a stray reference entry is not.
            again = [text for text in requests if web.LEFT_OUT_NOTES["prose"] in text]
            self.assertTrue(any("rivers" in text for text in again))
            self.assertFalse(any("Ofir Press" in text for text in again))
            return output.read_text(encoding="utf-8")

        self.assertIn("Mara Ellison, now at Acme Labs", adapt("Mara Ellison, now at Acme Labs."))
        printed = adapt("")
        self.assertIn("Mara Ellison, Northfield Institute; Tomas Reyes, Harbor University", printed)
        self.assertIn("Now at Acme Labs.", printed)
        for left_out in ("mara@example.org", "†", "permission", "rivers"):
            self.assertNotIn(left_out, printed)

    def test_code_names_an_equation_as_the_paper_prints_it(self):
        numbered = (
            "![](images/e.png)",
            "<!-- Start of picture text -->\nFFN(x) = max(0, xW1 + b1)W2 + b2 (2)\n"
            "<!-- End of picture text -->",
        )
        unnumbered = (
            "![](images/p.png)",
            "<!-- Start of picture text -->\nPE(pos,2i) = sin(pos/10000)\n"
            "<!-- End of picture text -->",
        )
        printed = {"1", "2", "3"}
        for narration, source, expected in (
            ("Equation 5 says two layers apply in turn.", numbered,
             "Equation 2 says two layers apply in turn."),
            ("Equation 2 says…", numbered, "Equation 2 says…"),
            # A picture of an equation is no figure, whatever the model calls it.
            ("Figure 4 shows two layers.", numbered, "Equation 2 shows two layers."),
            ("The figure shows two layers.", unnumbered, "The equation shows two layers."),
            # A number the paper prints nowhere is invented, with its article.
            ("This is what Equation 6 says.", unnumbered, "This is what the equation says."),
            ("This Equation 6 then projects them.", numbered, "Equation 2 then projects them."),
            ("Equations 4 and 5 encode position.", unnumbered, "The equations encode position."),
            ("Equation six adds a sine. Eq. 6 adds a cosine.", unnumbered,
             "The equation adds a sine. The equation adds a cosine."),
            # Any other name may be a reference or plain wording, and stays.
            ("Equation 2 is like Equation 1, as in Figure 2. Table 1 lists the costs "
             "shown in the figure.", numbered,
             "Equation 2 is like Equation 1, as in Figure 2. Table 1 lists the costs "
             "shown in the figure."),
            # A picture without math is no equation, whatever extraction calls it.
            ("Figure one shows the logo.", ("![](images/l.png)",), "Figure one shows the logo."),
        ):
            with self.subTest(narration):
                self.assertEqual(web.label_equation(narration, source, printed), expected)
        # A name left as written that is not this equation's is logged.
        self.assertEqual(
            web.equation_label_problems("Equation 2 is like Equation 1. Table 1 lists it.", numbered),
            ["the description says Equation 1, though it describes Equation 2",
             "the description says Table 1, though it describes Equation 2"],
        )
        self.assertEqual(
            web.equation_label_problems("The equation adds a sine.", unnumbered), [],
        )
        # A batch of two equations may name either one.
        pair = numbered + (
            "![](images/f.png)",
            "<!-- Start of picture text -->\nq = p(x) (3)\n<!-- End of picture text -->",
        )
        self.assertEqual(
            web.equation_label_problems("Equations 2 and 3 agree. Equation 3 sums. Equation 5 differs.", pair),
            ["the description says Equation 5, though it describes Equations 2 and 3"],
        )

    def test_the_log_names_what_a_narration_states_that_its_source_does_not(self):
        source = (
            "![](images/t.png)",
            "<!-- Start of picture text -->\nBLEU 27.3 41.8 params 65M steps 100K\n"
            "<!-- End of picture text -->",
            "Table 2: The Transformer, similar to Press and Wolf.",
        )
        self.assertEqual(web.grounding_problems(
            "Table 2 shows BLEU of 27.3, about 42 for the big model, with 65 million "
            "parameters after 100,000 steps, as Press and Wolf found.", source, describes=True,
        ), [])
        self.assertEqual(web.grounding_problems(
            "Table 2 shows BLEU of 28.4, as Vaswani and others found.", source, describes=True,
        ), [
            "the narration names Vaswani, whom its source never names",
            "the description says 28.4, which its source does not print",
        ])
        # A printed decimal never vouches for its whole part.
        self.assertEqual(
            web.grounding_problems("Table 2 gives 41 BLEU.", source, describes=True),
            ["the description says 41, which its source does not print"],
        )
        # Only the passage's own request counts, never the rest of the paper:
        # Vaswani heads the author block, and §3.4 still may not name him,
        # however the narration puts it. Common words that pair up do not count.
        section = ("We share the weight matrix, similar to Press and Wolf, as the encoder does.",)
        for narration in (
            "We share it, similar to Vaswani and others.",
            "We share it, as Vaswani et al. did.",
            "We share it, as used by Vaswani.",
        ):
            with self.subTest(narration):
                self.assertEqual(
                    web.grounding_problems(narration, section, known_names={"Vaswani", "Press"}),
                    ["the narration names Vaswani, whom its source never names"],
                )
        self.assertEqual(web.grounding_problems(
            "The Encoder and Decoder share it, similar to Press and Wolf.", section,
            known_names={"Vaswani", "Press"},
        ), [])
        # Prose numbers are checked too: Halley's total of 34,000 read as 3,400
        # went unnoticed when only descriptions were.
        self.assertEqual(web.grounding_problems("We reach 28.4.", ("We reach 28.3.",)),
                         ["the narration says 28.4, which its source does not print"])
        self.assertEqual(
            web.grounding_problems("Halley counted 3,400 people.", ("Halley counted a total of 34,000 people.",)),
            ["the narration says 3400, which its source does not print"],
        )
        # Only a number run into letters, a power extraction ran into its base, opens to a prefix.
        self.assertEqual(web.grounding_problems("Equation 26 holds.", ("See Equation 2615.",)),
                         ["the narration says 26, which its source does not print"])
        self.assertEqual(web.grounding_problems("The base is 10000.", ("The term 100002i grows.",)), [])
        self.assertEqual(web._numbers("from 16.0K to 30.9K, and 100K steps"), ["16000", "30900", "100000"])
        # OCR spaces a separator on a chart's axis; a list keeps its numbers apart.
        self.assertEqual(web._numbers("600 800 1 , 000"), ["600", "800", "1000"])
        self.assertEqual(web._numbers("batches of 16, 32, 128"), ["16", "32", "128"])

    def test_a_footnote_mark_reaches_the_model_at_the_end_of_its_sentence(self):
        paragraphs = [
            "Runs stalled on subagents cut off by the CLI’s idle limit<sup>1</sup> or by extra review "
            "rounds. By comparison, DELM wins.",
            "> 1Claude Code defaults to a 600-second idle limit.",
        ]
        moved = web.marks_after_sentences(paragraphs, web._layout_kinds(paragraphs))
        # Read at the mark, the note split the sentence (R22-04); at its end it cannot.
        self.assertEqual(moved[0], "Runs stalled on subagents cut off by the CLI’s idle limit or by extra review "
                                   "rounds.<sup>1</sup> By comparison, DELM wins.")
        self.assertEqual(moved[1], paragraphs[1])

    def test_a_tables_lettered_note_moves_with_the_table(self):
        page = (
            "Table 5: Comparison on SWE-bench Verified.\n\n"
            "![Table 5: Comparison on SWE-bench Verified.](images/t.png)\n\n"
            "<!-- Start of picture text -->\n|Method|Cost|\n|---|---|\n|Claude Code|$1.00 ^a|\n"
            "<!-- End of picture text -->\n\n"
            "> a The Claude Code CLI sends cache_control blocks.\n\n"
            "# 3.3 SWE-bench Verified\n\n"
            "Table 5 shows that DELM is cheapest, at a cost of 10^2 cents."
        )
        joined = web.split_paper_paragraphs(web.join_pdf_pages([page])[0])
        # The table moves after the text that introduces it, and its note, read
        # by page position before the heading in DeLM (R22-05), goes with it.
        self.assertEqual([paragraph.split("\n")[0][:20] for paragraph in joined], [
            "# 3.3 SWE-bench Veri", "Table 5 shows that D", "Table 5: Comparison ", "![Table 5: Compariso",
            "<!-- Start of pictur", "> a The Claude Code ",
        ])

    def test_a_footnote_is_grounded_by_the_paragraph_its_mark_sits_in(self):
        paragraphs = [
            "**Aidan N. Gomez**<sup>_∗†_</sup>, University of Toronto; "
            "**Llion Jones**<sup>_∗_</sup>, Google Research",
            "> _†_ Work performed while at Google Brain.",
            "We suspect the dot products grow large.<sup>4</sup>",
            "> 4To illustrate why the dot products get large, assume independence.",
        ]
        # "†" sits beside Gomez, so his name grounds its note; the "4" note
        # explains the third paragraph, which never names him.
        self.assertEqual(
            web.footnote_citers(paragraphs, web._layout_kinds(paragraphs)), {1: 0, 3: 2},
        )

    def test_a_dropped_model_connection_is_asked_again_and_a_silent_one_is_not(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        run = PaperRun(Path(temporary.name) / "paper.md", Path(temporary.name) / "out.txt", "utf-8")
        logs = []
        run.publish = lambda event, data: logs.append(data)
        answers = iter([
            web.ModelConnectionError("Model request failed: [Errno 104] Connection reset by peer"),
            web.ModelConnectionError("Model request failed: [Errno 111] Connection refused"),
            "<NARRATION>Read.</NARRATION><SUMMARY>S.</SUMMARY>",
        ])

        def answer(*_):
            value = next(answers)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(run, "request_model", side_effect=answer), \
                mock.patch.object(run.stop_requested, "wait", return_value=False) as waited:
            self.assertEqual(run.model_response(Path("request.txt"), "prompt"),
                             "<NARRATION>Read.</NARRATION><SUMMARY>S.</SUMMARY>")
        self.assertEqual([call.args[0] for call in waited.call_args_list], [1, 2])
        self.assertEqual(len(logs), 2)
        self.assertIn("Connection reset by peer; asking again in 1 s (1 of 4)", logs[0])

        # One that keeps dropping fails after the retries; Stop ends the wait.
        with mock.patch.object(run, "request_model",
                               side_effect=web.ModelConnectionError("Model request failed")), \
                mock.patch.object(run.stop_requested, "wait", return_value=False), \
                self.assertRaises(web.ModelConnectionError):
            run.model_response(Path("request.txt"), "prompt")
        with mock.patch.object(run, "request_model",
                               side_effect=web.ModelConnectionError("Model request failed")), \
                mock.patch.object(run.stop_requested, "wait", return_value=True), \
                self.assertRaises(InterruptedError):
            run.model_response(Path("request.txt"), "prompt")
        # A reset is a dropped connection; a stream that went silent is not.
        for failure, retried in ((ConnectionResetError(104, "reset"), True),
                                 (TimeoutError("timed out"), False)):
            with self.subTest(failure), \
                    mock.patch.object(web.ModelStream, "open", side_effect=failure), \
                    self.assertRaises(RuntimeError) as raised:
                with run.model_stream("http://127.0.0.1:9/v1", {}, b"{}"):
                    pass
            self.assertEqual(isinstance(raised.exception, web.ModelConnectionError), retried)

    def test_code_names_a_figure_by_its_caption_and_leaves_real_references(self):
        paper = ["**Figure 3:** One.", "**Figure 6:** Accuracy against compute.", "As Tables 1–4 show."]
        names = web.paper_visual_names(paper)
        self.assertLessEqual({"figure 3", "figure 6", "table 1", "table 4"}, names)
        # The opening name is the caption's; a name the paper never has goes too.
        self.assertEqual(web.label_visual("Figure 81 and 82 compares accuracy.", "Figure 6", names),
                         "Figure 6 compares accuracy.")
        self.assertEqual(web.label_visual("The equation shows accuracy rising.", "Figure 6", names),
                         "Figure 6 shows accuracy rising.")
        self.assertEqual(web.label_visual("It shows the trend of Figure 213.", "Figure 6", names),
                         "It shows the trend of Figure 6.")
        # An equation extracted as text is not in the paper's names, and still not this figure.
        self.assertEqual(web.label_visual("It plots the loss defined in Equation 2.", "Figure 6", names),
                         "It plots the loss defined in Equation 2.")
        # A name the paper has may be a real reference: kept, and logged.
        kept = web.label_visual("Figure 6 shows the trend of Figure 3.", "Figure 6", names)
        self.assertEqual(kept, "Figure 6 shows the trend of Figure 3.")
        self.assertEqual(web.visual_label_problems(kept, "Figure 6"),
                         ["the description says Figure 3, though it describes Figure 6"])

    def test_a_descriptions_only_run_sends_only_figures_tables_and_equations(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text(
            "The method does not improve accuracy.\n\n"
            "![](images/plot.png)\n\n**Figure 1:** Accuracy against compute.\n\n"
            "We conclude with a summary.",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        asked = []

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                if request_path.stem.endswith("-kind"):
                    return "OTHER"
                asked.append(request_path.read_text(encoding="utf-8"))
                return "<NARRATION>Figure 1 shows accuracy against compute.</NARRATION><SUMMARY>S.</SUMMARY>"

        run = StubPaperRun(source, root / "out.txt", "utf-8", in_flight=1, paragraphs_per_worker=1,
                           prompt_path=prompt, scratch_path=root / "stage", descriptions_only=True)
        run.publish = lambda event, data: None
        run.pump()
        self.assertEqual(run.code, 0)
        # Only the figure reached the model; the prose is the author's, as printed.
        self.assertEqual(len(asked), 1)
        self.assertIn("Figure 1:", asked[0])
        self.assertEqual(
            (root / "out.txt").read_text(encoding="utf-8"),
            "The method does not improve accuracy.\n\nFigure 1 shows accuracy against compute.\n\n"
            "We conclude with a summary.",
        )

    def test_a_chart_is_described_from_its_caption_and_the_authors_text(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text(
            "With more compute, CLM reaches 73.7% (Figure 3).\n\n"
            "![](images/plot.png)\n\n<!-- Start of picture text -->\n0 20 40 60 80<br>Accuracy<br>PFLOPs<br>"
            "Needle Retention<br>Log Triage\n<!-- End of picture text -->\n\n"
            # Extraction's emphasis around a thousands separator, as CLM's Figure 14 caption.
            "**Figure 3:** Accuracy against compute, at 20 _,_ 000 tokens.\n\n"
            "![](images/flow.png)\n\n**Figure 4:** The pipeline, in 60 steps.\n\n"
            "We conclude with a summary.",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        asked = {}

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                text = request_path.read_text(encoding="utf-8")
                if request_path.stem.endswith("-kind"):
                    return "CHART a line plot." if "plot.png" in text else "OTHER a flowchart."
                asked.setdefault(request_path.stem, []).append(text)
                if "Figure 3:" in text:
                    # Values read off the plot: its ticks, said as results.
                    # The second answer names the tasks the chart's labels print.
                    narration = (
                        "Figure 3 plots accuracy against compute at 20,000 tokens, on Needle Retention "
                        "and Log Triage. Accuracy reaches 73.7 percent. One point sits at 80 percent."
                        if "A check of your narration" in text else
                        "Figure 3 plots accuracy against compute. It rises from 40 to 60 percent."
                    )
                elif "Figure 4:" in text:
                    narration = "Figure 4 shows the pipeline, in 60 steps."
                else:
                    narration = text.split("<SOURCE_PARAGRAPH>\n")[1].split("\n</SOURCE_PARAGRAPH>")[0]
                return f"<NARRATION>{narration}</NARRATION><SUMMARY>S.</SUMMARY>"

        run = StubPaperRun(source, root / "out.txt", "utf-8", in_flight=1, paragraphs_per_worker=1,
                           prompt_path=prompt, scratch_path=root / "stage")
        run.publish = lambda event, data: None
        run.pump()
        self.assertEqual(run.code, 0)
        [chart] = [requests for requests in asked.values() if "Figure 3:" in requests[0]]
        [diagram] = [requests for requests in asked.values() if "Figure 4:" in requests[0]]
        # The chart is asked from text, with the author's result; the diagram as before.
        self.assertIn("This batch is a chart", chart[0])
        self.assertIn("CLM reaches 73.7%", chart[0])
        self.assertNotIn("This batch is a chart", diagram[0])
        # Numbers printed only inside the chart are not grounds: asked again,
        # the answer with fewer is kept, and a sentence still saying one goes.
        self.assertEqual(len(chart), 2)
        self.assertEqual(len(diagram), 1)
        text = (root / "out.txt").read_text(encoding="utf-8")
        self.assertIn(
            "Figure 3 plots accuracy against compute at 20,000 tokens, on Needle Retention and Log Triage. "
            "Accuracy reaches 73.7 percent.", text,
        )
        self.assertNotIn("80 percent", text)
        self.assertIn("Figure 4 shows the pipeline, in 60 steps.", text)
        checkpoints = {
            json.loads(path.read_text())["narration"].split()[1]: json.loads(path.read_text())
            for path in (root / "stage" / "paragraph-checkpoints").glob("*.json")
            if json.loads(path.read_text())["narration"].startswith("Figure")
        }
        self.assertEqual((checkpoints["3"]["chart"], checkpoints["4"]["chart"]), (True, False))
        self.assertEqual(checkpoints["3"]["flags"], [])
        # The opening sentence names the figure, so it stays even with such a number.
        self.assertEqual(
            web.without_unprinted_sentences("Figure 5 shows 99 runs. It peaks at 80.", ["Figure 5: Runs."]),
            ("Figure 5 shows 99 runs.", ["It peaks at 80."]),
        )

    def test_interpreted_figures_go_to_the_figure_model_from_their_pictures(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text(
            "With more compute, CLM reaches 73.7% (Figure 3).\n\n"
            "![](images/plot.png)\n\n<!-- Start of picture text -->\n0 20 40 60 80<br>Accuracy\n"
            "<!-- End of picture text -->\n\n**Figure 3:** Accuracy against compute.\n\n"
            "We conclude with a summary.",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        calls = []

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=(), model=None):
                text = request_path.read_text(encoding="utf-8")
                calls.append((request_path.stem, model, text))
                narration = (
                    "Figure 3 plots accuracy against compute; it rises from 40 to 60 percent."
                    if "Figure 3:" in text else "Narrated prose."
                )
                return f"<NARRATION>{narration}</NARRATION><SUMMARY>S.</SUMMARY>"

        run = StubPaperRun(
            source, root / "out.txt", "utf-8", "lm-studio/writer",
            [{"server": "127.0.0.1:9", "provider": "lm-studio"}], in_flight=1, paragraphs_per_worker=1,
            prompt_path=prompt, scratch_path=root / "stage",
            interpret_figures=True, figure_model="openai-codex/figure-reader",
        )
        run.publish = lambda event, data: None
        with mock.patch.object(web, "local_model_names", return_value=("writer",)):
            run.pump()
        self.assertEqual(run.code, 0)
        figure = [(model, text) for stem, model, text in calls if "Figure 3:" in text]
        prose = [model for stem, model, text in calls if "Figure 3:" not in text]
        # The figure model reads the figure, never asked whether it is a chart
        # and never told to describe it from text; prose stays with the text model.
        self.assertEqual({model for model, _ in figure}, {"openai-codex/figure-reader"})
        self.assertFalse(any("This batch is a chart" in text for _, text in figure))
        self.assertFalse([stem for stem, _, _ in calls if stem.endswith("-kind")])
        self.assertEqual(set(prose), {None})
        # Values read off the plot are its to give: the description stays.
        self.assertIn("it rises from 40 to 60 percent", (root / "out.txt").read_text(encoding="utf-8"))

    def test_a_narration_stating_what_its_source_does_not_is_asked_about_once_then_marked(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "paper.md"
        source.write_text("Halley counted a total of 34,000 people.\n\nThe method does not improve accuracy.",
                          encoding="utf-8")
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        asked = []

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                text = request_path.read_text(encoding="utf-8")
                again = "A check of your narration" in text
                asked.append(again)
                if "Halley" in text:
                    # Wrong once, then right when told what the check found.
                    narration = "Halley counted a total of 34,000 people." if again else "Halley counted 3,400 people."
                else:
                    # Wrong both times: kept and marked.
                    narration = "The method does improve accuracy."
                return f"<NARRATION>{narration}</NARRATION><SUMMARY>S.</SUMMARY>"

        run = StubPaperRun(source, root / "out.txt", "utf-8", in_flight=1, paragraphs_per_worker=1,
                           prompt_path=prompt, scratch_path=root / "stage")
        logs = []
        run.publish = lambda event, data: logs.append(data) if event == "log" else None
        run.pump()
        self.assertEqual(run.code, 0)
        self.assertEqual(asked.count(True), 2)
        checkpoints = root / "stage" / "paragraph-checkpoints"
        first = json.loads((checkpoints / "000001-000001.json").read_text())
        second = json.loads((checkpoints / "000002-000002.json").read_text())
        self.assertEqual((first["narration"], first["flags"]), ("Halley counted a total of 34,000 people.", []))
        self.assertEqual((second["narration"], second["flags"]), ("The method does improve accuracy.", ["dropped “not”"]))
        self.assertIn("Paragraph 2/2 kept and marked: dropped “not”.", "".join(logs))
        # The mark goes with the passage into the book's narration.
        groups = web._adapted_reader_groups(
            (root / "out.txt").read_text(encoding="utf-8"), ["a", "b"], checkpoints,
        )
        self.assertEqual([group[3].get("flags") for group in groups], [None, ["dropped “not”"]])

    def test_a_number_the_caption_prints_must_reach_the_description(self):
        caption = ("Figure 3: Execution timelines with Claude Opus 5.5 and four agents (§3). The main agent spends "
                   "73% of its time waiting; DeLM finishes 5.5× faster (Mao et al., 2026), see Figure 2.<sup>4</sup>")
        lost = web.caption_number_problems(
            "Figure 3 shows the timelines with Claude Opus 5.5 and four agents. The main agent spends a large "
            "majority of its time waiting, and DeLM finishes 5.5 times faster.", [caption])
        # The 73% was lost (R22, F75); the model name, §3, the year, Figure 2, and
        # the footnote mark are not the caption's numbers, and "four" says 4.
        self.assertEqual(lost, ["the description leaves out 73, which its caption prints"])
        self.assertEqual(len(web.hard_flags(lost)), 1)
        self.assertEqual(web.caption_number_problems("The main agent waits 73 percent of the time; DeLM is 5.5 times faster.",
                                                     [caption]), [])

    def test_math_said_in_an_unclear_order_and_an_unstated_magnitude_are_hard_flags(self):
        problems = web.math_and_magnitude_problems(
            "The rate is the product of the step number and warmup steps raised to the power of negative 1.5. "
            "Its cost is orders of magnitude lower.",
            ["| step_num · warmup_steps<sup>−1.5</sup> | 3.3 · 10<sup>18</sup> |"],
        )
        self.assertEqual(len(web.hard_flags(problems)), 2)
        # Said as steps, or a magnitude the source states, is fine.
        self.assertEqual(web.math_and_magnitude_problems(
            "Raise warmup steps to the power of negative 1.5, then multiply the step number by the result. "
            "It is orders of magnitude faster.",
            ["It runs orders of magnitude faster."],
        ), [])

    def test_a_changed_word_symbol_or_not_is_named_where_the_kept_share_sees_nothing(self):
        source = "On the hand, the risk of a predictor _Ŷ_ is the loss. Readers familiar with it know."
        narration = "On the other hand, the risk of a predictor Y is the loss. Listeners familiar with it know."
        # Kept above the bar the share logs at, so only the word comparison sees it.
        self.assertGreater(web.prose_kept(source, narration)[0], web.PROSE_KEPT_LOW)
        self.assertEqual(web.prose_changes(source, narration), ["ŷ → y", "readers → listeners"])
        self.assertEqual(web.prose_changes("This does not improve accuracy.", "This does improve accuracy."),
                         ["dropped “not”"])
        # Math read aloud is not a change.
        for source, narration in (
            ("where _f_(_X_) ≠ _Y_ across the population", "where f of X does not equal Y across the population"),
            ("the set of _x_ with _x_ > 0", "the set of all x such that x is greater than zero"),
            ("the weights w<sub>t</sub> at step 1", "the weights w t at step one"),
            ("the loss ℓ of each", "the loss l of each"),
            # A less-than sign is math, not the start of a tag that ends at "<sub>".
            ("width _k < n_ does not connect all pairs, or _log<sub>k</sub>_",
             "width k less than n does not connect all pairs, or the logarithm base k"),
        ):
            with self.subTest(narration):
                self.assertEqual(web.prose_changes(source, narration), [])

    def test_a_picture_is_an_equation_only_when_its_text_is_math(self):
        chart = ["![](images/chart.png)", "<!-- Start of picture text -->0 10 20 accuracy<!-- End of picture text -->"]
        bound = ["![](images/eq.png)", "<!-- Start of picture text -->e ≤ y + z<!-- End of picture text -->"]
        self.assertEqual(web._visual_type(chart, ["image", "labels"]), "figure")
        self.assertEqual(web._visual_type(bound, ["image", "labels"]), "equation")
        # An inequality printed without a number is "the equation", not a made-up one.
        self.assertEqual(web.label_equation("Equation 999 bounds e.", bound), "The equation bounds e.")

    def test_names_are_fixed_in_new_and_saved_batches_and_an_equation_inside_prose(self):
        import pymupdf

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "images").mkdir()
        for name in ("chart", "eq"):
            pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False).save(str(root / "images" / f"{name}.png"))
        source = root / "paper.md"
        source.write_text(
            "Compute grows with the model.\n\n![](images/chart.png)\n\n"
            "<!-- Start of picture text -->accuracy 44.6 179 PF<!-- End of picture text -->\n\n"
            "Figure 6: Accuracy against compute.\n\nWe bound the error\n\n![](images/eq.png)\n\n"
            "<!-- Start of picture text -->e ≤ y + z<!-- End of picture text -->\n\nwhere e is the error.",
            encoding="utf-8",
        )
        prompt = root / "prompt.md"
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        answers = {
            "Figure 6:": "Figure 81 and 82 compares accuracy and compute.",
            "We bound": "We bound the error. Equation 673 says e is at most y plus z, where e is the error.",
        }

        class StubPaperRun(PaperRun):
            def model_response(self, request_path, system_prompt, attachments=()):
                text = request_path.read_text(encoding="utf-8")
                narration = next((answer for key, answer in answers.items() if key in text),
                                 "Compute always grows with the model.")
                return f"<NARRATION>{narration}</NARRATION><SUMMARY>S.</SUMMARY>"

        def adapt():
            run = StubPaperRun(source, root / "out.txt", "utf-8", in_flight=1,
                               paragraphs_per_worker=4, prompt_path=prompt, scratch_path=root / "stage")
            logs = []
            run.publish = lambda event, data: logs.append(data) if event == "log" else None
            run.pump()
            self.assertEqual(run.code, 0)
            checkpoints = root / "stage" / "paragraph-checkpoints"
            return {path.stem: json.loads(path.read_text())["narration"]
                    for path in sorted(checkpoints.glob("*.json"))}, "".join(logs)

        narrations, log = adapt()
        # A word the author never wrote, which the kept-words share cannot see, is named.
        self.assertIn("Paragraph 1/8 changed the author's words: added “always”.", log)
        self.assertEqual(narrations["000002-000004"], "Figure 6 compares accuracy and compute.")
        self.assertEqual(narrations["000005-000008"],
                         "We bound the error. The equation says e is at most y plus z, where e is the error.")

        # A batch saved before these fixes gets them, and the checks, when reused.
        checkpoints = root / "stage" / "paragraph-checkpoints"
        saved = {
            "000002-000004": "Figure 81 and 82 compares accuracy of 99.9 and compute.",
            "000005-000008": answers["We bound"],
        }
        for stem, wrong in saved.items():
            path = checkpoints / f"{stem}.json"
            path.write_text(json.dumps({**json.loads(path.read_text()), "narration": wrong}))
        (root / "out.txt").unlink()
        answers.clear()  # the model is not asked again
        narrations, log = adapt()
        self.assertEqual(narrations["000002-000004"], "Figure 6 compares accuracy of 99.9 and compute.")
        self.assertIn("The equation says", narrations["000005-000008"])
        self.assertIn("Paragraphs 2-4/8 (saved): the description says 99.9, which its source does not print.",
                      log)


def chat_narration(*summaries):
    """A narration whose passages kept their batches' summaries, one per text."""
    return {"schema": 2, "original_view": False, "passages": [
        {"type": "body", "text": f"Paragraph {number} text.", "paragraphs": [number - 1],
         "summary": summary, "tags": ["tag"]}
        for number, summary in enumerate(summaries, 1)
    ]}


class FakeChatModel(BaseHTTPRequestHandler):
    """An OpenAI-compatible server that streams the next scripted reply and
    keeps every request body. A reply that is a (status, message) pair is a
    refusal; /tokenize answers `tokenize` as the count, or 404 when None."""

    replies = []
    requests = []
    window = 100_000
    tokenize = None

    def log_message(self, *args):
        pass

    def answer(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.answer(200, {"data": [{"id": "fake", "max_model_len": type(self).window}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/tokenize":
            if type(self).tokenize is None:
                return self.answer(404, {"detail": "Not Found"})
            return self.answer(200, {"count": type(self).tokenize, "max_model_len": type(self).window})
        type(self).requests.append(body)
        reply = type(self).replies.pop(0)
        if isinstance(reply, tuple):
            return self.answer(reply[0], {"error": {"message": reply[1], "type": "BadRequestError"}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in reply:
            event = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def local_servers_at(server):
    """A browser's added local servers: one, the fake model server."""
    return [{"server": f"http://127.0.0.1:{server.server_port}", "provider": "lm-studio", "vision": False}]


class ChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = web.SharedStorage(Path(temporary.name))
        self.storage.ensure()

    def book(self, narration):
        book = store_book(self.storage, narration=narration)
        return book, web.read_book(self.storage, book)[0]

    def test_a_book_without_passage_summaries_cannot_chat(self):
        book, _ = self.book({"schema": 1, "original_view": False, "passages": [
            {"type": "body", "text": "Body text.", "paragraphs": [0]},
        ]})
        with self.assertRaisesRegex(web.ChatUnavailable, "older version of Hilde"):
            web.chat_book(self.storage, book)
        self.assertEqual(
            web.chat_payload(self.storage, web.ChatRegistry(), book)["problem"], web.CHAT_OLD_BOOK
        )

    def test_files_are_markdown_in_the_books_files_folder_only(self):
        _, path = self.book(chat_narration("S."))
        self.assertEqual(web.chat_file_path(path, "notes"), path / "files" / "notes.md")
        self.assertEqual(web.chat_file_path(path, "Notes.MD"), path / "files" / "Notes.md")
        for name in ("notes.txt", "../book.json", "../escape.md", "sub/notes.md", ".hidden.md", "", None):
            with self.subTest(name), self.assertRaises(ValueError):
                web.chat_file_path(path, name)

    def test_write_append_list_and_delete_a_file(self):
        book, path = self.book(chat_narration("S."))
        passages = web.chat_book(self.storage, book)[2]

        def call(tool, **arguments):
            return web.chat_tool(path, passages, {"name": tool, "arguments": arguments})

        self.assertEqual(call("write_file", name="notes", mode="append", content="x")[2], False)
        self.assertEqual(call("write_file", name="notes", mode="create", content="# A\n")[2], True)
        # Creating over a file would lose it; the model is told to append.
        content, _, changed = call("write_file", name="notes.md", mode="create", content="# B\n")
        self.assertIn("already exists", content)
        self.assertFalse(changed)
        self.assertEqual(call("write_file", name="notes.md", mode="append", content="more\n")[2], True)
        self.assertEqual((path / "files" / "notes.md").read_text(), "# A\nmore\n")
        self.assertIn("notes.md (9 bytes)", call("list_files")[0])
        # The book's own files are out of reach.
        self.assertEqual(call("delete_file", name="../book.json")[2], False)
        self.assertTrue((path / "book.json").is_file())
        self.assertEqual(call("delete_file", name="notes.md")[2], True)
        self.assertEqual(web.chat_files(path), [])
        self.assertEqual(call("delete_file", name="notes.md")[2], False)

    def test_reads_stop_at_the_cap_and_say_where_to_read_on(self):
        passages = [{"text": "w" * 15_000}, {"text": "x" * 15_000}, {"text": ""}]
        content, label = web.chat_read(passages, 1, 3)
        self.assertEqual(label, "Read ¶1")
        self.assertIn("Stopped before ¶2", content)
        content, label = web.chat_read(passages, 3, 99)
        self.assertEqual(label, "Read ¶3")
        self.assertIn("Not narrated", content)
        self.assertEqual(web.chat_read(passages, 4)[1], "Read nothing")

    def test_past_80_percent_the_oldest_go_first_with_their_tool_results(self):
        entries = [
            {"role": "user", "text": "a" * 300},
            {"role": "assistant", "text": "", "calls": [{"id": "1", "name": "read_paragraphs", "arguments": {}}]},
            {"role": "tool", "id": "1", "name": "read_paragraphs", "content": "r" * 300},
            {"role": "notice", "text": "Stopped."},
            {"role": "assistant", "text": "b" * 300},
            {"role": "user", "text": "latest"},
        ]
        # Under 80% of the window, everything stays.
        self.assertEqual(web.chat_context(entries, 0, 1_000), (
            [entry for entry in entries if entry["role"] != "notice"], 0
        ))
        kept, dropped = web.chat_context(entries, 0, 300)
        # The call goes with its result, and the oldest go only until the
        # context is under 60%; it still opens with the listener.
        self.assertEqual(dropped, 3)
        self.assertEqual([entry["role"] for entry in kept], ["user", "assistant", "user"])
        self.assertIn("removed", kept[0]["text"])
        self.assertEqual(kept[2]["text"], "latest")
        # The latest message stays even when it alone is too long.
        self.assertEqual(web.chat_context(entries, 10_000, 300)[0][-1]["text"], "latest")

    def test_recreating_a_book_keeps_its_files_and_drops_its_conversation(self):
        book, path = self.book(chat_narration("S."))
        (path / "files").mkdir()
        (path / "files" / "notes.md").write_text("# Notes\n")
        web.write_json_atomic(path / "chat.json", {"schema": 1, "entries": [{"role": "user", "text": "hi"}]})
        self.assertEqual(store_book(self.storage, narration=chat_narration("New.")), book)
        self.assertEqual((path / "files" / "notes.md").read_text(), "# Notes\n")
        self.assertEqual(web.read_chat(path), [])

    def test_a_turn_runs_the_models_tool_calls_until_it_answers(self):
        book, path = self.book(chat_narration("Storms.", "Dams."))
        FakeChatModel.requests = []
        FakeChatModel.replies = [
            [({"content": "Reading."}, None),
             ({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read_paragraphs", "arguments": '{"start": 2'}}]}, None),
             ({"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}, None),
             ({"tool_calls": [{"index": 1, "id": "b", "function": {
                 "name": "write_file",
                 "arguments": json.dumps({"name": "dams", "mode": "create", "content": "# Dams\n"})}}]},
              "tool_calls")],
            [({"content": "Dams are in ¶2."}, "stop")],
        ]
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatModel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        chats = web.ChatRegistry()
        turn = web.ChatTurn(self.storage, book, "Where are dams?", "lm-studio/fake",
                            local_servers_at(server))
        self.assertTrue(chats.start(turn))
        events, index = [], 0
        while not (events and events[-1]["type"] == "done"):
            new, _ = turn.events_from(index, 10)
            self.assertTrue(new, "the turn stalled")
            events += new
            index += len(new)

        # The model saw its paragraph lines, then the text it asked to read.
        first, second = FakeChatModel.requests
        self.assertIn("¶2 [body] Dams. Tags: tag.", first["messages"][0]["content"])
        self.assertEqual({tool["function"]["name"] for tool in first["tools"]},
                         {"read_paragraphs", "search_book", "write_file", "list_files", "delete_file"})
        results = [message for message in second["messages"] if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in results], ["a", "b"])
        self.assertEqual(results[0]["content"], "¶2\nParagraph 2 text.")
        self.assertEqual((path / "files" / "dams.md").read_text(), "# Dams\n")
        # The page heard each step, and the conversation survives a reload.
        # The question named no part of the book, so the note has no section.
        self.assertIn({"type": "files", "files": [
            {**item, "section": None} for item in web.chat_files(path)
        ]}, events)
        payload = web.chat_payload(self.storage, chats, book)
        self.assertFalse(payload["running"])
        self.assertEqual(
            [(entry["role"], entry["text"]) for entry in payload["conversation"]],
            [("user", "Where are dams?"), ("assistant", "Reading."),
             ("tool", "Read ¶2"), ("tool", "Wrote dams.md"), ("assistant", "Dams are in ¶2.")],
        )
        self.assertIn('<a href="#" class="chat-cite" data-passage="2">¶2</a>',
                      payload["conversation"][-1]["html"])
        self.assertEqual(payload["passages"], {"1": 0, "2": 1})

    def test_a_files_section_is_the_question_whose_turn_last_wrote_it(self):
        book, path = self.book(chat_narration("S."))
        passages = web.chat_book(self.storage, book)[2]
        entries = []

        def ask(section, *writes):
            entries.append({"role": "user", "text": "Note this.", "at": "2026-10-15T10:00:00Z",
                            **({"context": {"section": section}} if section else {})})
            for number, (name, mode) in enumerate(writes):
                call = {"id": str(number), "name": "write_file",
                        "arguments": {"name": name, "mode": mode, "content": "x\n"}}
                entries.append({"role": "assistant", "text": "", "calls": [call]})
                content, label, _ = web.chat_tool(path, passages, call)
                entries.append({"role": "tool", "id": call["id"], "name": "write_file",
                                "content": content, "label": label})
            entries.append({"role": "assistant", "text": "Done."})

        ask("2 Methods", ("notes", "create"), ("summary", "create"))
        # Appending moves notes.md to this question; creating summary.md again is refused.
        ask("3 Results", ("notes", "append"), ("summary", "create"))
        # The latest question wrote nothing, and one that named no section wrote plain.md.
        ask(None, ("plain", "create"))
        ask("4 Discussion")
        # A file no recorded turn wrote has no section.
        (path / "files" / "by hand.md").write_text("# Mine\n")
        web.write_json_atomic(path / "chat.json", {"schema": 1, "book": book, "entries": entries})

        files = web.chat_payload(self.storage, web.ChatRegistry(), book)["files"]
        self.assertEqual(
            {item["name"]: item["section"] for item in files},
            {"notes.md": "3 Results", "summary.md": "2 Methods", "plain.md": None, "by hand.md": None},
        )

    def test_the_chat_says_whether_the_server_searches_the_web(self):
        book, _ = self.book(chat_narration("S."))
        answers = {}
        for search_server in ("", "http://127.0.0.1:8890"):
            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                server.daemon_threads = True
                server.storage = self.storage
                server.chats = web.ChatRegistry()
                server.chat_speaker = None
                server.search_server = search_server
                server.verbose = False
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/api/chat?book={book}"
                    ) as response:
                        answers[search_server] = json.load(response)["web_search"]
                finally:
                    server.shutdown()
                    thread.join()
        self.assertEqual(answers, {"": False, "http://127.0.0.1:8890": True})

    def run_turn(self, turn):
        """Start a turn and wait for it; return its events."""
        self.assertTrue(web.ChatRegistry().start(turn))
        events, index = [], 0
        while not (events and events[-1]["type"] == "done"):
            new, _ = turn.events_from(index, 10)
            self.assertTrue(new, "the turn stalled")
            events += new
            index += len(new)
        return events

    def test_gemini_gets_its_answer_back_as_received_with_its_thought_signatures(self):
        book, path = self.book(chat_narration("Storms.", "Dams."))
        requests = []
        call_parts = [
            {"text": "Which paragraph holds dams?", "thought": True},
            {"functionCall": {"id": "fc-1", "name": "read_paragraphs", "args": {"start": 2}},
             "thoughtSignature": "c2lnLWNhbGw="},
            # Streaming may bring a signature later, on a part with no text.
            {"text": "", "thoughtSignature": "c2lnLWVuZA=="},
        ]

        class GeminiHandler(FakeProviderHandler):
            def do_POST(self):
                if (
                    self.path != "/v1beta/models/gemini-test:streamGenerateContent?alt=sse"
                    or self.headers.get("x-goog-api-key") != "AIza-good"
                ):
                    return self.reply(404, {})
                requests.append(self.body())
                if len(requests) == 1:
                    return self.stream((
                        {"candidates": [{"content": {"role": "model", "parts": call_parts[:1]}}]},
                        {"candidates": [{"content": {"role": "model", "parts": call_parts[1:2]}}]},
                        {"candidates": [{"content": {"role": "model", "parts": call_parts[2:]},
                                         "finishReason": "STOP"}]},
                    ))
                return self.stream((
                    {"candidates": [{"content": {"role": "model", "parts": [
                        {"text": "Dams are in ¶2.", "thoughtSignature": "c2lnLWFuc3dlcg=="},
                    ]}, "finishReason": "STOP"}]},
                ))

        origin = serve_fake_provider(self, GeminiHandler)
        home = Path(self.storage.root) / ".hilde"
        patch_for_test(
            self, mock.patch.object(web, "GEMINI_API_URL", f"{origin}/v1beta"),
            mock.patch.object(web, "HILDE_HOME", home),
        )
        web._write_private_json(web.gemini_key_path(), {"api_key": "AIza-good"})
        self.run_turn(web.ChatTurn(self.storage, book, "Where are dams?", "gemini/gemini-test", []))

        first, second = requests
        self.assertIn("¶2 [body] Dams. Tags: tag.", first["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(first["contents"], [{"role": "user", "parts": [{"text": "Where are dams?"}]}])
        declarations = first["tools"][0]["functionDeclarations"]
        self.assertEqual({tool["name"] for tool in declarations},
                         {"read_paragraphs", "search_book", "write_file", "list_files", "delete_file"})
        self.assertTrue(all("parametersJsonSchema" in tool and "parameters" not in tool for tool in declarations))
        self.assertEqual(first["toolConfig"], {"functionCallingConfig": {"mode": "AUTO"}})
        # The model turn goes back exactly as it arrived, every signature in
        # its part, followed by the tool's result for that call.
        self.assertEqual(second["contents"][1], {"role": "model", "parts": call_parts})
        self.assertEqual(second["contents"][2], {"role": "user", "parts": [{"functionResponse": {
            "name": "read_paragraphs", "id": "fc-1", "response": {"content": "¶2\nParagraph 2 text."},
        }}]})
        entries = web.read_chat(path)
        self.assertEqual(entries[1]["gemini_parts"], call_parts)
        self.assertEqual(entries[1]["calls"], [{"id": "fc-1", "name": "read_paragraphs", "arguments": {"start": 2}}])
        self.assertEqual(entries[-1]["text"], "Dams are in ¶2.")
        # A thought is never part of the answer.
        self.assertNotIn("Which paragraph", json.dumps([entry.get("text") for entry in entries]))
        # A call Gemini did not write, such as another model's or a reading an
        # earlier Hilde added for the listener, is marked so Gemini 3 takes it
        # without a signature of its own.
        contents = web._chat_gemini_contents([
            {"role": "user", "text": "What is this?"},
            {"role": "assistant", "text": "", "calls": [
                {"id": "listener-1", "name": "read_paragraphs", "arguments": {"start": 1, "end": 1}},
                {"id": "gemini-call-1", "name": "search_book", "arguments": {"query": "dams"}},
            ]},
            {"role": "tool", "id": "listener-1", "name": "read_paragraphs", "content": "¶1"},
            {"role": "tool", "id": "gemini-call-1", "name": "search_book", "content": "none"},
        ])
        self.assertEqual(contents[1:], [
            {"role": "model", "parts": [
                {"functionCall": {"name": "read_paragraphs", "args": {"start": 1, "end": 1}, "id": "listener-1"},
                 "thoughtSignature": web.GEMINI_SKIP_SIGNATURE},
                {"functionCall": {"name": "search_book", "args": {"query": "dams"}}},
            ]},
            {"role": "user", "parts": [
                {"functionResponse": {"name": "read_paragraphs", "response": {"content": "¶1"}, "id": "listener-1"}},
                {"functionResponse": {"name": "search_book", "response": {"content": "none"}}},
            ]},
        ])

    def test_an_openai_api_key_chats_through_responses(self):
        book, path = self.book(chat_narration("Storms.", "Dams."))
        requests = []

        class OpenAIHandler(FakeProviderHandler):
            def do_POST(self):
                if self.path != "/v1/responses" or self.headers.get("Authorization") != "Bearer sk-good":
                    return self.reply(404, {})
                requests.append(self.body())
                if len(requests) == 1:
                    return self.stream((
                        {"type": "response.output_item.done", "item": {"type": "reasoning", "summary": []}},
                        {"type": "response.output_item.done", "item": {
                            "type": "function_call", "id": "fc_1", "call_id": "call_1",
                            "name": "read_paragraphs", "arguments": '{"start": 2}',
                        }},
                        {"type": "response.completed", "response": {"id": "resp"}},
                    ))
                return self.stream((
                    {"type": "response.output_text.delta", "delta": "Dams are in ¶2."},
                    {"type": "response.completed", "response": {"id": "resp"}},
                ))

        origin = serve_fake_provider(self, OpenAIHandler)
        home = Path(self.storage.root) / ".hilde"
        patch_for_test(
            self, mock.patch.object(web, "OPENAI_API_URL", f"{origin}/v1"),
            mock.patch.object(web, "HILDE_HOME", home),
        )
        web._write_private_json(web.openai_api_key_path(), {"api_key": "sk-good"})
        self.run_turn(web.ChatTurn(self.storage, book, "Where are dams?", "openai/gpt-test", []))

        first, second = requests
        self.assertEqual((first["model"], first["store"], first["stream"]), ("gpt-test", False, True))
        self.assertEqual(first["tool_choice"], "auto")
        self.assertTrue(all(tool["type"] == "function" and tool["strict"] is False for tool in first["tools"]))
        self.assertEqual(second["input"][1:], [
            {"type": "function_call", "call_id": "call_1", "name": "read_paragraphs",
             "arguments": json.dumps({"start": 2})},
            {"type": "function_call_output", "call_id": "call_1", "output": "¶2\nParagraph 2 text."},
        ])
        self.assertEqual(web.read_chat(path)[-1]["text"], "Dams are in ¶2.")

    def test_a_question_about_paragraphs_comes_with_their_narration_in_the_message(self):
        book, _ = self.book(chat_narration("Storms.", "Dams.", "Silt."))
        FakeChatModel.requests = []
        FakeChatModel.replies = [[({"content": "Dams hold coarse sediment back."}, "stop")]]
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatModel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        passages = web.chat_book(self.storage, book)[2]
        about = web.chat_about({"kind": "passages", "start": 2, "end": 3}, passages)
        attachment = web.chat_attachment(
            self.storage, book, passages, "lm-studio/fake", about, local_servers_at(server),
        )
        turn = web.ChatTurn(self.storage, book, 'What does it mean by "trapped"?', "lm-studio/fake",
                            local_servers_at(server), attachment)
        self.assertTrue(web.ChatRegistry().start(turn))
        index = 0
        while not turn.events_from(index, 10)[1]:
            index = len(turn.events)

        # The model is asked once, the labelled paragraphs in her message,
        # without a read made up for it.
        (request,) = FakeChatModel.requests
        (message,) = request["messages"][1:]
        self.assertEqual(message["content"], (
            "The listener attached this from the book:\n\n"
            "[Hilde's narration (¶2)]\nParagraph 2 text.\n\n"
            "[Hilde's narration (¶3)]\nParagraph 3 text.\n\n"
            'What does it mean by "trapped"?'
        ))
        conversation = web.chat_payload(self.storage, web.ChatRegistry(), book)["conversation"]
        self.assertEqual([entry["text"] for entry in conversation],
                         ['What does it mean by "trapped"?', "Dams hold coarse sediment back."])
        # The page shows the chip of a past question from its manifest.
        self.assertEqual(conversation[0]["context"], attachment[0])
        self.assertEqual(attachment[0]["preview"], "Paragraph 2 text.")
        self.assertEqual(attachment[0]["badge"], "Local")
        with self.assertRaisesRegex(ValueError, "at most 6"):
            web.chat_about({"kind": "passages", "start": 1, "end": 9}, passages)
        with self.assertRaisesRegex(ValueError, "figure or table"):
            web.chat_about({"kind": "figure", "passage": 2}, passages)

    def test_a_local_model_is_asked_on_the_server_that_offers_it(self):
        book, _ = self.book(chat_narration("Storms.", "Dams."))

        class OtherModel(FakeChatModel):
            requests, replies = [], []

            def do_GET(self):
                self.answer(200, {"data": [{"id": "other", "max_model_len": 100_000}]})

        FakeChatModel.requests = []
        FakeChatModel.replies = [[({"content": "Dams are in ¶2."}, "stop")]]
        servers = []
        for handler in (OtherModel, FakeChatModel):
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            servers += local_servers_at(server)
        # The first server added offers another model; the second offers this one.
        turn = web.ChatTurn(self.storage, book, "Where are dams?", "lm-studio/fake", servers)
        self.assertTrue(web.ChatRegistry().start(turn))
        index = 0
        while not turn.events_from(index, 10)[1]:
            index = len(turn.events)
        self.assertEqual([request["model"] for request in FakeChatModel.requests], ["fake"])
        self.assertEqual(OtherModel.requests, [])
        self.assertEqual(
            web.chat_payload(self.storage, web.ChatRegistry(), book)["conversation"][-1]["text"],
            "Dams are in ¶2.",
        )

    def long_book(self, count=3000):
        """A book of `count` passages: a heading, then 29 paragraphs, over and over."""
        passages = []
        for number in range(1, count + 1):
            heading = number % 30 == 1
            passages.append({
                "type": "heading" if heading else "body",
                "text": f"Section {number // 30}" if heading else f"Paragraph {number} text about topic {number}.",
                "paragraphs": [number - 1],
                "summary": f"Paragraph {number} explains topic {number} with several more words of summary here.",
                "tags": ["topic", f"item {number}"],
            })
        passages[1554]["text"] = "If we denote the optimal threshold value as eta, we can rewrite the rule."
        return self.book({"schema": 2, "original_view": False, "passages": passages})[0]

    def ask(self, book, question="Explain this part."):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatModel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        turn = web.ChatTurn(self.storage, book, question, "lm-studio/fake", local_servers_at(server))
        self.assertTrue(web.ChatRegistry().start(turn))
        index = 0
        while not turn.events_from(index, 10)[1]:
            index = len(turn.events)
        return [entry["text"] for entry in web.chat_payload(self.storage, web.ChatRegistry(), book)["conversation"]]

    def reset_fake(self, window, replies, tokenize=None):
        previous = (FakeChatModel.window, FakeChatModel.tokenize)
        self.addCleanup(lambda: (setattr(FakeChatModel, "window", previous[0]),
                                 setattr(FakeChatModel, "tokenize", previous[1])))
        FakeChatModel.window, FakeChatModel.tokenize = window, tokenize
        FakeChatModel.requests, FakeChatModel.replies = [], replies

    def test_a_long_book_sends_a_section_outline_that_fits_a_32k_model(self):
        book = self.long_book()
        self.reset_fake(32_768, [[({"content": "It is a threshold."}, "stop")]])
        conversation = self.ask(book)
        self.assertEqual(conversation[-1], "It is a threshold.")
        (request,) = FakeChatModel.requests
        system = request["messages"][0]["content"]
        self.assertIn("each section is one line", system)
        self.assertIn("¶1531–1560 Section 51:", system)
        counted = (len(json.dumps(request["messages"])) + len(json.dumps(request["tools"]))) // 3
        self.assertLessEqual(counted + request["max_tokens"], 32_768)

    def test_a_request_the_server_refuses_as_too_long_is_sent_once_more_with_the_shortest_outline(self):
        book, _ = self.book(chat_narration("Storms.", "Dams."))
        refusal = (400, "This model's maximum context length is 32768 tokens. However, you requested "
                        "8000 output tokens and your prompt contains at least 24769 input tokens.")
        self.reset_fake(100_000, [refusal, [({"content": "Dams trap silt."}, "stop")]])
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(self.ask(book)[-1], "Dams trap silt.")
        first, second = FakeChatModel.requests
        self.assertIn("¶2 [body] Dams. Tags: tag.", first["messages"][0]["content"])
        self.assertIn("each section is one line", second["messages"][0]["content"])

        # Refused again: the listener reads why in plain words, once.
        self.reset_fake(100_000, [refusal, refusal])
        with mock.patch("sys.stdout", io.StringIO()) as log:
            self.assertEqual(self.ask(book)[-1], web.CHAT_TOO_LONG)
        self.assertEqual(len(FakeChatModel.requests), 2)
        self.assertIn("maximum context length is 32768", log.getvalue())

    def test_the_servers_own_count_sets_how_long_the_answer_may_be(self):
        book, _ = self.book(chat_narration("Storms.", "Dams."))
        self.reset_fake(32_768, [[({"content": "Short."}, "stop")]], tokenize=31_000)
        self.ask(book)
        (request,) = FakeChatModel.requests
        self.assertEqual(request["max_tokens"], 32_768 - 31_000 - web.CHAT_TOKEN_MARGIN)

    def test_tool_schemas_count_as_input(self):
        without = web.chat_input_tokens("anthropic/x", "", "System.", [], ())
        with_tools = web.chat_input_tokens("anthropic/x", "", "System.", [], web.CHAT_TOOLS)
        self.assertAlmostEqual(with_tools - without, (len(json.dumps(web.CHAT_TOOLS)) - 2) / 3, delta=1)

    def test_search_book_puts_paragraphs_holding_the_words_together_first(self):
        passages = [
            {"type": "body", "text": "The threshold moves.", "summary": "Optimal choices.", "tags": []},
            {"type": "body", "text": "If we denote the optimal threshold value, the rule is simple.",
             "summary": "Bayes rule.", "tags": []},
            {"type": "body", "text": "Nothing here.", "summary": "Unrelated.", "tags": []},
        ]
        content, label = web.search_book(passages, "optimal threshold")
        self.assertEqual(label, 'Searched the book for "optimal threshold"')
        self.assertEqual([line.split()[0] for line in content.splitlines()], ["¶2", "¶1"])
        self.assertIn("denote the optimal threshold value", content)


# A figure of two panels side by side, at points the crop dpi cuts on whole pixels.
FIGURE_PANELS = ((72, 120, 216, 264), (312, 96, 504, 288))


def store_figure_book(storage):
    """A one-page PDF book: a heading, prose, Figure 1 (two panels side by
    side, cut as PDF extraction cuts them), and prose naming other figures.
    Return the book and the crops."""
    import pymupdf

    pdf_path = Path(storage.root) / "two-panels.pdf"
    rng = np.random.default_rng(7)
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "1 Model")
        for rect in FIGURE_PANELS:
            pixels = rng.integers(0, 256, (48, 48, 3), dtype=np.uint8)
            page.insert_image(pymupdf.Rect(rect), pixmap=pymupdf.Pixmap(pymupdf.csRGB, 48, 48, pixels.tobytes(), False))
        pdf.save(pdf_path)
        crops = [
            pdf[0].get_pixmap(dpi=web.FIGURE_CROP_DPI, clip=pymupdf.Rect(rect)).tobytes("png")
            for rect in FIGURE_PANELS
        ]

    def passage(number, kind, text, source, unchanged=True, sources=None):
        return {
            "id": number, "type": kind, "page": 1, "text": text, "original_text": source,
            "unchanged": unchanged, "paragraphs": [number - 1, number - 1],
            "sources": sources or [{"type": kind, "page": 1, "text": source}],
            "summary": f"Summary {number}.", "tags": [],
        }

    caption = "Figure 1: Encoder (left) and decoder (right)."
    narration = {"schema": 2, "original_view": False, "passages": [
        passage(1, "heading", "1 Model", "# **1 Model**"),
        passage(2, "body", "The encoder reads the input, as Figure 1 shows.",
                "The encoder reads the input (Figure 1).", unchanged=False),
        # Printed right above the figure, it never names it.
        passage(3, "body", "The decoder writes the output.", "The decoder writes the output."),
        passage(4, "figure", "Figure 1 shows the encoder on the left and the decoder on the right.", caption,
                unchanged=False, sources=[
                    {"type": "figure", "page": 1, "text": "![](images/left.png)"},
                    {"type": "figure", "page": 1, "text": "![](images/right.png)"},
                    {"type": "caption", "page": 1, "text": caption},
                ]),
        passage(5, "body", "Unlike Figure 10, Figures 1 and 2 hold no numbers.",
                "Unlike Figure 10, Figures 1 and 2 hold no numbers."),
        passage(6, "body", "Figure 10 is in the appendix.", "Figure 10 is in the appendix."),
    ]}
    pictures = "\n\n".join(f"![](data:image/png;base64,{base64.b64encode(crop).decode()})" for crop in crops)
    blocks = [
        "# 1 Model", "The encoder reads the input, as Figure 1 shows.", "The decoder writes the output.",
        f"Figure 1 shows the encoder on the left and the decoder on the right.\n\n{pictures}",
        "Unlike Figure 10, Figures 1 and 2 hold no numbers.", "Figure 10 is in the appendix.",
    ]
    timings = {
        "schema": 3, "sample_rate": 24000, "duration_samples": 2400, "block_count": len(blocks),
        "paragraphs": list(range(len(blocks))),
        "cues": [{"block": block, "start_sample": 400 * block, "end_sample": 400 * (block + 1)}
                 for block in range(len(blocks))],
        "word_timing": "unavailable", "word_cues": [],
    }
    book = store_book(
        storage, source_sha256="f" * 64, narration=narration, timings=timings, source=pdf_path,
        markdown="\n\n".join(f"<!-- audiobook-tts:block={index} -->\n\n{block}" for index, block in enumerate(blocks)),
    )
    return book, crops


class FigureAndContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = web.SharedStorage(Path(temporary.name))
        self.storage.ensure()
        self.book, self.crops = store_figure_book(self.storage)
        self.passages = web.chat_book(self.storage, self.book)[2]

    def figure(self, model, local_servers=()):
        about = web.chat_about({"kind": "figure", "passage": 4}, self.passages)
        return web.chat_attachment(self.storage, self.book, self.passages, model, about, list(local_servers))

    def local_server(self):
        FakeChatModel.requests, FakeChatModel.replies = [], []
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatModel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_the_reader_places_blocks_passages_and_figure_panels(self):
        payload = web.audiobook_reader_payload(self.storage, self.book)
        self.assertEqual(payload["block_passages"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(payload["passages"][4], {
            "type": "figure", "page": 1, "section": "1 Model", "label": "Figure 1",
            "caption": "Figure 1: Encoder (left) and decoder (right).",
            "sources": self.passages[3]["sources"],
        })
        self.assertEqual(
            [(detail["section"], detail["label"]) for detail in payload["passages"].values()],
            [("1 Model", "")] * 3 + [("1 Model", "Figure 1")] + [("1 Model", "")] * 2,
        )
        link = f"/api/figure?book={self.book}&passage=4"
        self.assertEqual(payload["figures"], {4: {
            "label": "Figure 1", "page": 1, "composite": link,
            "panels": [{"block": 3, "image": 0, "position": "left", "picture": f"{link}&panel=0"},
                       {"block": 3, "image": 1, "position": "right", "picture": f"{link}&panel=1"}],
        }})

    def test_panel_positions_follow_the_boxes_as_printed(self):
        grid = [(0, 0, 10, 10), (12, 0, 22, 10), (0, 12, 10, 22), (12, 12, 22, 22)]
        self.assertEqual(web.figure_panel_positions(grid), ["top left", "top right", "bottom left", "bottom right"])
        self.assertEqual(web.figure_panel_positions([(0, 0, 10, 4), (0, 6, 10, 20)]), ["top", "bottom"])
        self.assertEqual(web.figure_panel_positions([(0, 0, 10, 10)]), ["whole"])

    def get(self, server, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}") as response:
                return response.status, response.headers["Content-Type"], response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers["Content-Type"], exc.read()

    def test_the_figure_route_renders_the_panels_side_by_side_from_the_pdf(self):
        import cv2

        bare = store_book(self.storage, title="No PDF", source_sha256="b" * 64)
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.storage = self.storage
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, kind, body = self.get(server, f"/api/figure?book={self.book}&passage=4")
                not_figure = self.get(server, f"/api/figure?book={self.book}&passage=3")[0]
                page = self.get(server, f"/api/source-page?book={self.book}&page=1")
                missing = [
                    self.get(server, f"/api/source-page?book={self.book}&page={number}")[0]
                    for number in ("0", "2", "x")
                ] + [self.get(server, f"/api/source-page?book={bare}&page=1")[0],
                     self.get(server, f"/api/figure?book={bare}&passage=1")[0],
                     self.get(server, f"/api/figure?book={self.book}&passage=4&panel=2")[0]]
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual((status, kind), (200, "image/png"))
        composite = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_GRAYSCALE)
        # One picture: the panels' region with its margin, at the render dpi.
        scale = web.FIGURE_RENDER_DPI / 72
        margin = web.FIGURE_REGION_MARGIN
        self.assertAlmostEqual(composite.shape[1], (504 - 72 + 2 * margin) * scale, delta=2)
        self.assertAlmostEqual(composite.shape[0], (288 - 96 + 2 * margin) * scale, delta=2)
        # Each panel sits where the PDF prints it: side by side, left then right.
        found = []
        for crop in self.crops:
            template = cv2.imdecode(np.frombuffer(crop, np.uint8), cv2.IMREAD_GRAYSCALE)
            template = cv2.resize(template, None, fx=web.FIGURE_RENDER_DPI / web.FIGURE_CROP_DPI,
                                  fy=web.FIGURE_RENDER_DPI / web.FIGURE_CROP_DPI)
            _, score, _, at = cv2.minMaxLoc(cv2.matchTemplate(composite, template, cv2.TM_CCOEFF_NORMED))
            self.assertGreater(score, 0.8)
            found.append(at)
        self.assertAlmostEqual(found[0][0], margin * scale, delta=3)
        self.assertAlmostEqual(found[1][0], (312 - 72 + margin) * scale, delta=3)
        self.assertAlmostEqual(found[0][1], (120 - 96 + margin) * scale, delta=3)
        self.assertEqual(not_figure, 404)
        self.assertEqual((page[0], page[1]), (200, "image/png"))
        self.assertEqual(missing, [404] * 6)

    def test_two_figures_side_by_side_keep_apart_with_their_titles_and_without_their_captions(self):
        import pymupdf

        # Two pictures 9 pt apart, each titled just above and captioned just
        # below, over the paper's running text, which spans both.
        left, right = (72, 100, 290, 250), (299, 100, 480, 250)
        path = Path(self.storage.root) / "two-figures.pdf"
        rng = np.random.default_rng(3)
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            for rect in (left, right):
                pixels = rng.integers(0, 256, (40, 40, 3), dtype=np.uint8)
                page.insert_image(pymupdf.Rect(rect), pixmap=pymupdf.Pixmap(pymupdf.csRGB, 40, 40, pixels.tobytes(), False))
            page.insert_text((120, 94), "Encoder stack", fontsize=9)
            page.insert_text((360, 94), "Decoder stack", fontsize=9)
            page.insert_text((72, 266), "Figure 1: The encoder.", fontsize=9)
            page.insert_text((299, 266), "Figure 2: The decoder.", fontsize=9)
            for line in range(5):
                page.insert_text((72, 300 + 13 * line), "The running text of the paper goes on here, line by line, "
                                 "across the whole width of its column.", fontsize=10)
            titles = [page.search_for(text)[0] for text in ("Encoder stack", "Decoder stack")]
            captions = [page.search_for(text)[0] for text in ("Figure 1:", "Figure 2:")]
            pdf.save(path)
        first, first_panels = web.figure_regions(path, 1, [left], [right])
        second, second_panels = web.figure_regions(path, 1, [right], [left])
        for region, title, caption in ((first, titles[0], captions[0]), (second, titles[1], captions[1])):
            self.assertLessEqual(region[1], title.y0)
            self.assertLessEqual(region[3], caption.y0)
            self.assertTrue(region[0] <= title.x0 and title.x1 <= region[2])
        self.assertLessEqual(first[2], second[0])
        self.assertLess(first[2], right[0])
        self.assertGreater(second[0], left[2])
        self.assertEqual((first_panels, second_panels), ((first,), (second,)))

    def test_a_figure_brings_its_caption_the_passages_naming_it_and_its_description(self):
        manifest, images = self.figure("anthropic/claude-test")
        self.assertEqual(
            [(item["kind"], item["label"], item["passage"]) for item in manifest["items"]],
            [("caption", "From the paper · caption", 4),
             ("source", "From the paper · 1 Model, PDF p. 1", 2),
             ("source", "From the paper · 1 Model, PDF p. 1", 5),
             ("description", "Hilde's description", 4)],
        )
        self.assertEqual(manifest["items"][1]["text"], "The encoder reads the input (Figure 1).")
        # The paragraph printed beside it, and one naming Figure 10, are not about it.
        sent = json.dumps(manifest)
        self.assertNotIn("The decoder writes the output", sent)
        self.assertNotIn("appendix", sent)
        self.assertEqual(manifest["section"], "1 Model")
        self.assertEqual(manifest["preview"], "Figure 1: Encoder (left) and decoder (right).")
        self.assertEqual(manifest["badge"], "Cloud: Anthropic")
        # The picture as printed goes to a cloud model, never into the manifest.
        self.assertEqual(manifest["image"], {"sent": True, "count": 1})
        self.assertEqual(images[0]["mime"], "image/png")
        self.assertNotIn(images[0]["data"], sent)
        self.assertEqual(self.figure("claude-code/opus")[0]["image"]["sent"], False)

    def test_every_provider_sends_exactly_the_manifest_and_its_picture(self):
        question = "What is on the left?"

        def entry(attachment):
            manifest, images = attachment
            return {"role": "user", "text": question, "context": manifest, "images": images}

        attachment = self.figure("gemini/gemini-test")
        manifest, images = attachment
        text = web.chat_attachment_text(manifest)
        for item in manifest["items"]:
            self.assertIn(f"[{item['label']} (¶{item['passage']})]\n{item['text']}", text)
        self.assertEqual(text.count("\n\n["), len(manifest["items"]) + 1)  # the items and the picture's note
        data, url = images[0]["data"], f"data:image/png;base64,{images[0]['data']}"
        self.assertEqual(web._chat_anthropic_messages([entry(attachment)]), [{"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
            {"type": "text", "text": question},
        ]}])
        self.assertEqual(web._chat_openai_input([entry(attachment)]), [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": text},
            {"type": "input_image", "image_url": url},
            {"type": "input_text", "text": question},
        ]}])
        self.assertEqual(web._chat_gemini_contents([entry(attachment)]), [{"role": "user", "parts": [
            {"text": text}, {"inlineData": {"mimeType": "image/png", "data": data}}, {"text": question},
        ]}])
        # A local model gets the picture only when its server is marked as seeing images.
        server = f"http://127.0.0.1:{self.local_server().server_port}"
        seeing = self.figure("lm-studio/fake", [{"server": server, "provider": "lm-studio", "vision": True}])
        self.assertEqual(seeing[0]["image"], {"sent": True, "count": 1})
        self.assertEqual(seeing[0]["badge"], "Local")
        self.assertEqual(web._chat_openai_messages("S", [entry(seeing)])[1], {"role": "user", "content": [
            {"type": "text", "text": web.chat_attachment_text(seeing[0])},
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": question},
        ]})
        blind = self.figure("lm-studio/fake", [{"server": server, "provider": "lm-studio", "vision": False}])
        self.assertEqual(blind[0]["image"], {
            "sent": False, "count": 1, "why": "This local model's server is not marked as seeing images.",
        })
        self.assertEqual(blind[1], [])
        self.assertEqual(web._chat_openai_messages("S", [entry(blind)])[1], {
            "role": "user", "content": f"{web.chat_attachment_text(blind[0])}\n\n{question}",
        })
        self.assertNotIn("as printed in the paper", web.chat_attachment_text(blind[0]))

    def test_the_picture_goes_with_the_turn_that_attached_it_only(self):
        server = self.local_server()
        servers = [{"server": f"http://127.0.0.1:{server.server_port}", "provider": "lm-studio", "vision": True}]
        FakeChatModel.replies = [[({"content": "The encoder."}, "stop")], [({"content": "Yes."}, "stop")]]
        attachment = self.figure("lm-studio/fake", servers)
        for text, attached in (("What is on the left?", attachment), ("Sure?", None)):
            turn = web.ChatTurn(self.storage, self.book, text, "lm-studio/fake", servers, attached)
            self.assertTrue(web.ChatRegistry().start(turn))
            index = 0
            while not turn.events_from(index, 10)[1]:
                index = len(turn.events)
        first, second = FakeChatModel.requests
        self.assertIn("image_url", json.dumps(first["messages"][-1]))
        self.assertNotIn("image_url", json.dumps(second))
        self.assertIn("[Hilde's description (¶4)]", second["messages"][1]["content"])
        path = web.read_book(self.storage, self.book)[0]
        stored = web.read_chat(path)[0]
        self.assertEqual(stored["context"], attachment[0])
        self.assertNotIn("images", stored)
        self.assertNotIn(attachment[1][0]["data"], (path / "chat.json").read_text())

    def test_the_context_route_answers_the_manifest_send_uses(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.storage = self.storage
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def post(about):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/chat/context",
                        data=json.dumps({"book": self.book, "model": "openai/gpt-test", "about": about}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    try:
                        with urllib.request.urlopen(request) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as exc:
                        return exc.code, json.load(exc)

                figure = post({"kind": "figure", "passage": 4})
                selection = post({"kind": "selection", "start": 2, "end": 3, "quote": "reads  the\ninput"})
                wrong = post({"kind": "figure", "passage": 3})
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(figure, (200, self.figure("openai/gpt-test")[0]))
        self.assertEqual(selection[0], 200)
        self.assertEqual(
            [(item["kind"], item.get("passage")) for item in selection[1]["items"]],
            [("selection", None), ("narration", 2), ("source", 2), ("narration", 3)],
        )
        self.assertEqual(selection[1]["items"][0], {"kind": "selection", "label": "Your selection", "text": "reads the input"})
        self.assertEqual(selection[1]["image"], {"sent": False, "count": 0, "why": "Only text is attached."})
        self.assertEqual(wrong[0], 400)


class FakeWeb(BaseHTTPRequestHandler):
    """Pages by path: (status, headers, body); every path asked is kept."""

    pages = {}
    asked = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).asked.append(self.path)
        status, headers, body = type(self).pages.get(self.path.split("?")[0], (404, {}, b""))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WebToolTests(unittest.TestCase):
    def serve(self):
        FakeWeb.pages, FakeWeb.asked = {}, []
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWeb)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", server.server_port

    def allowing(self, port):
        """Connect to the test server's port as if it were public; anything
        else gets the real check."""
        def connect(address, *args, **kwargs):
            if address[1] == port:
                return socket.create_connection(address, *args, **kwargs)
            return web._public_connection(address, *args, **kwargs)
        return connect

    def test_pages_on_this_machine_or_the_local_network_are_never_fetched(self):
        origin, _ = self.serve()
        FakeWeb.pages["/"] = (200, {"Content-Type": "text/plain"}, b"secret")
        for url in (f"{origin}/", origin.replace("127.0.0.1", "localhost") + "/"):
            content, label = web.read_web_page(url)
            self.assertIn("is not a public address", content)
            self.assertTrue(label.startswith("Could not read"))
        self.assertEqual(FakeWeb.asked, [])
        self.assertIn("HTTP or HTTPS", web.read_web_page("file:///etc/passwd")[0])

    def test_a_redirect_to_a_local_address_is_refused(self):
        public, public_port = self.serve()
        local = ThreadingHTTPServer(("127.0.0.1", 0), FakeWeb)
        threading.Thread(target=local.serve_forever, daemon=True).start()
        self.addCleanup(local.server_close)
        self.addCleanup(local.shutdown)
        FakeWeb.pages["/moved"] = (302, {"Location": f"http://127.0.0.1:{local.server_port}/admin"}, b"")
        FakeWeb.pages["/admin"] = (200, {"Content-Type": "text/plain"}, b"secret")
        content, _ = web.read_web_page(f"{public}/moved", connect=self.allowing(public_port))
        self.assertIn("127.0.0.1 is not a public address", content)
        self.assertEqual(FakeWeb.asked, ["/moved"])

    def test_a_page_is_read_as_its_content_in_pieces(self):
        origin, port = self.serve()
        article = "".join(f"<p>Sentence {number} about sediment.</p>" for number in range(1500))
        FakeWeb.pages["/a"] = (301, {"Location": "/article"}, b"")
        FakeWeb.pages["/article"] = (200, {"Content-Type": "text/html; charset=utf-8"}, (
            "<html><head><title>Ebro &amp; silt</title><style>p{}</style></head><body>"
            "<nav><a href='/'>Home</a> Menu</nav><script>track()</script>"
            f"<main><h1>Delta</h1>{article}</main><footer>Cookie notice</footer></body></html>"
        ).encode())
        first, label = web.read_web_page(f"{origin}/a", connect=self.allowing(port))
        self.assertEqual(label, "Read 127.0.0.1/article")
        self.assertTrue(first.startswith(f"Ebro & silt\n{origin}/article\n\nDelta\nSentence 0 about sediment."))
        for chrome in ("Menu", "track()", "Cookie notice", "p{}"):
            self.assertNotIn(chrome, first)
        resume = int(re.search(r"read on with start=(\d+)\.\)$", first).group(1))
        rest, _ = web.read_web_page(f"{origin}/article", resume, connect=self.allowing(port))
        self.assertTrue(rest.endswith("Sentence 1499 about sediment."))
        self.assertNotIn("read on", rest)

    def test_search_lists_searxng_results_with_their_links(self):
        origin, _ = self.serve()
        results = [{"title": f"Result  {number}", "url": f"https://example.org/{number}",
                    "content": "A  snippet."} for number in range(12)]
        FakeWeb.pages["/search"] = (200, {"Content-Type": "application/json"},
                                    json.dumps({"results": results}).encode())
        content, label = web.web_search(origin, "  storm   sediment ")
        self.assertEqual(label, 'Searched the web for "storm sediment"')
        self.assertEqual(FakeWeb.asked, ["/search?q=storm+sediment&format=json"])
        self.assertTrue(content.startswith("1. Result 0\nhttps://example.org/0\nA snippet."))
        self.assertIn("8. Result 7", content)
        self.assertNotIn("9. Result 8", content)

    def test_without_a_search_server_the_web_tools_do_not_exist(self):
        call = {"name": "web_search", "arguments": {"query": "sediment"}}
        self.assertEqual(web.chat_tool(Path("."), [], call)[0], "There is no tool named web_search.")



class ChatSpeechTests(unittest.TestCase):
    def test_an_answer_is_read_as_its_words_block_by_block(self):
        self.assertEqual(
            web.chat_speech_blocks(
                "## Storms\n\nStorms carry **71%** of the load (¶4), see ¶12–14 and "
                "[the survey](https://example.org/a) or https://example.org/b.\n\n"
                "```python\nprint('no')\n```\n\n| Station | Load |\n| --- | --- |\n| Ebro | — |\n\n- One `item`"
            ),
            # A cell without words is not read but keeps its number, as on the page.
            [(0, "Storms"),
             (1, "Storms carry 71% of the load (paragraph 4), see paragraphs 12 to 14 and the survey or."),
             (2, "Station"), (3, "Load"), (4, "Ebro"), (6, "One item")],
        )
        self.assertEqual(web.chat_speech_blocks("```\ncode only\n```"), [])

    def test_no_clip_crosses_a_block_and_the_first_is_short(self):
        opening = ("The base model uses eight attention heads, each of 64 dimensions, so the total "
                   "computational cost is similar to that of single-head attention with full "
                   "dimensionality across all layers. It works.")
        clips = web.chat_speech_chunks([(0, opening), (2, "Second block.")])
        self.assertEqual(clips, [
            (0, "The base model uses eight attention heads, each of 64 dimensions,"),
            (0, "so the total computational cost is similar to that of single-head attention with "
                "full dimensionality across all layers. It works."),
            (2, "Second block."),
        ])

    def test_a_reading_is_made_only_as_far_ahead_as_it_is_played(self):
        class Speaker:
            def __init__(self):
                self.spoken = []

            def speak(self, voice_dir, text):
                self.spoken.append(text)
                if text == "broken":
                    raise RuntimeError("out of memory")
                return text.encode()

        speaker = Speaker()
        reading = web.ChatReading(speaker, "k", Path("."), [f"part {number}" for number in range(1, 9)])
        self.assertEqual(reading.clip(1, 5), b"part 1")
        # The maker stops once it is CHAT_SPEECH_AHEAD past the last clip asked for.
        deadline = time.monotonic() + 5
        while len(speaker.spoken) < 1 + web.CHAT_SPEECH_AHEAD and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        self.assertEqual(speaker.spoken, ["part 1", "part 2", "part 3"])
        self.assertEqual(reading.clip(5, 5), b"part 5")
        time.sleep(0.2)
        self.assertEqual(len(speaker.spoken), 7)

        failing = web.ChatReading(speaker, "f", Path("."), ["broken"])
        with self.assertRaisesRegex(RuntimeError, "couldn't read this aloud: out of memory"):
            failing.clip(1, 5)

    def test_reading_aloud_is_off_unless_a_browser_chose_it(self):
        self.assertIs(normalize({})["player"]["chat_speak"], False)
        self.assertIs(normalize({"player": {"chat_speak": "yes"}})["player"]["chat_speak"], False)
        self.assertIs(normalize({"player": {"chat_speak": True}})["player"]["chat_speak"], True)

    def test_answers_are_read_by_hilde_or_else_the_first_voice_by_name(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        self.assertIsNone(web.chat_speech_voice(storage))
        for name in ("eir", "Balder", "Hilde", ".staged"):
            save_voice(storage.voices / name, np.zeros(2400, dtype=np.float32), 24000, "A reference.", "FLOAT")
        (storage.voices / "Aaron").mkdir()  # not a saved voice: no sample
        self.assertEqual(web.chat_speech_voice(storage).name, "Hilde")
        (storage.voices / "Hilde").rename(storage.voices / ".Hilde-deleted")
        # A hidden folder is a voice being staged or deleted, never a choice.
        self.assertEqual(web.chat_speech_voice(storage).name, "Balder")


class WorkerNodeTests(unittest.TestCase):
    NODE = {
        "host": "narrator@10.0.0.5",
        "python": "/opt/qwen/bin/python",
        "model": "/models/Base",
        "devices": ["cuda:1", "cuda:3"],
    }
    CLONE = {"source": "local", "model": "/local/Base", "allow_downloads": False}

    def serve(self, nodes=()):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.jobs = web.JobQueue(web.audiobook_consumers(
            self.CLONE, [{"value": "cuda:0", "label": "CUDA 0"}], list(nodes)
        ))
        server.tts_models = {"design": {"source": "missing"}, "clone": dict(self.CLONE)}
        server.verbose = False
        server.storage = storage
        server.worker_nodes = list(nodes)
        server.workers_path = storage.root / web.WORKERS_FILE
        server.workers_lock = threading.Lock()
        server.worker_setup = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.jobs.shutdown)
        return server, f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def request(origin, path, body=None):
        request = urllib.request.Request(
            origin + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_workers_yaml_keeps_each_node_and_refuses_unsafe_or_unclear_ones(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "workers.yaml"
        self.assertEqual(web.read_workers(path), ([], []))
        mac = {**self.NODE, "host": "studio-mac", "devices": ["mps"]}
        web.write_workers(path, [self.NODE, mac], {"cuda:2", "cuda:0"})
        self.assertEqual(web.read_workers(path), ([self.NODE, mac], ["cuda:0", "cuda:2"]))
        web.write_workers(path, [mac], set())
        self.assertEqual(web.read_workers(path), ([mac], []))

        refused = {
            "an option as host": {**self.NODE, "host": "-oProxyCommand=sh"},
            "an unknown device": {**self.NODE, "devices": ["gpu0"]},
            "a device twice": {**self.NODE, "devices": ["cuda:1", "cuda:1"]},
            "no device": {**self.NODE, "devices": []},
            "a comma in python": {**self.NODE, "python": "/opt/a,b/python"},
            "an option as model": {**self.NODE, "model": "--help"},
            "an unknown setting": {**self.NODE, "port": 22},
            "a host twice": [self.NODE, self.NODE],
        }
        for reason, nodes in refused.items():
            with self.subTest(reason):
                path.write_text(
                    yaml.safe_dump({"nodes": nodes if isinstance(nodes, list) else [nodes]})
                )
                with self.assertRaises(ValueError):
                    web.read_workers(path)
        for reason, local_off in {
            "not a device": ["gpu0"], "a device twice": ["cuda:0", "cuda:0"], "not a list": "cuda:0",
        }.items():
            with self.subTest(reason):
                path.write_text(yaml.safe_dump({"local_off": local_off, "nodes": []}))
                with self.assertRaises(ValueError):
                    web.read_workers(path)
        path.write_text("nodes: [unclosed")
        with self.assertRaises(ValueError):
            web.read_workers(path)

    def test_each_node_device_narrates_with_that_nodes_python_model_and_device(self):
        workers = [
            {"kind": "local", "device": "cuda:0"},
            *(consumer["worker"] for consumer in web.remote_consumers([self.NODE])),
        ]
        command = web.narrate_command({
            "clone": self.CLONE, "input": "book.txt", "encoding": "utf-8",
            "output": "book.mp3", "chunk_max_chars": "500", "resume_dir": "work",
            "mp3_level": "", "voice_dir": "voices/Eir", "batch_size": "2",
            "workers": workers, "device": "cuda:0", "dtype": "auto",
            "attn": "sdpa", "language": "", "seed": "",
        })
        parser = cli.build_parser()
        args = parser.parse_args(command[3:])
        cli.check_distributed_mode(args, parser)

        self.assertEqual(args.ssh_worker, [
            {"target": "narrator@10.0.0.5", "device": device,
             "python": "/opt/qwen/bin/python", "model": "/models/Base"}
            for device in ("cuda:1", "cuda:3")
        ])
        with mock.patch.object(cli.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
                mock.patch.object(cli, "_run_transport"):
            label, staged, _ = cli._stage_ssh_worker(args, args.ssh_worker[1], Path("voices/Eir"))
        remote = shlex.split(staged[-1])
        self.assertEqual(staged[-2], "narrator@10.0.0.5")
        self.assertEqual(remote[0], "/opt/qwen/bin/python")
        self.assertEqual(remote[remote.index("--device") + 1], "cuda:3")
        self.assertEqual(remote[remote.index("--clone-model-path") + 1], "/models/Base")

    def test_a_bare_ssh_target_takes_the_shared_settings_and_workers_stay_distinct(self):
        parser = cli.build_parser()
        base = [
            "narrate", "--text", "Hello.", "--output", "out.wav", "--voice-dir", "voice",
            "--clone-model-path", "/models/Base", "--device", "cuda:0",
            "--attn-implementation", "sdpa", "--resume-dir", "work",
        ]
        args = parser.parse_args(base + [
            "--ssh-worker", "spark", "--ssh-worker", "spark,device=cuda:1",
            "--ssh-device", "cuda:2", "--ssh-python", "/venv/bin/python",
        ])
        cli.check_distributed_mode(args, parser)
        self.assertEqual(
            [(worker["device"], worker["python"], worker["model"]) for worker in args.ssh_worker],
            [("cuda:2", "/venv/bin/python", "/models/Base"),
             ("cuda:1", "/venv/bin/python", "/models/Base")],
        )
        refused = (
            ["--ssh-worker", "spark,device=cuda:1", "--ssh-worker", "spark,device=cuda:1"],
            ["--ssh-worker", "spark,gpu=1"],
            ["--ssh-worker", "spark,python=-c"],
        )
        for extra in refused:
            with self.subTest(extra), mock.patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit):
                cli.check_distributed_mode(parser.parse_args(base + extra), parser)

    def test_only_a_browser_on_the_servers_machine_sees_or_changes_workers(self):
        for address, local in (
            ("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True),
            ("192.168.1.20", False), ("::ffff:10.0.0.2", False), ("unknown", False),
        ):
            self.assertEqual(web.is_loopback_address(address), local, address)
        server, origin = self.serve([self.NODE])
        with mock.patch.object(web, "is_loopback_address", return_value=False), \
                mock.patch.object(web, "probe_worker_node") as probe:
            self.assertEqual(self.request(origin, "/api/workers")[0], 403)
            for path, body in (
                ("/api/workers/probe", {"host": "spark"}),
                ("/api/workers/setup", {"host": "spark"}),
                ("/api/workers/setup/stop", {}),
                ("/api/workers/add", {**self.NODE, "host": "spark"}),
                ("/api/workers/remove", {"host": self.NODE["host"]}),
            ):
                self.assertEqual(self.request(origin, path, body)[0], 403, path)
            state = self.request(origin, "/api/state")[1]
        probe.assert_not_called()
        self.assertFalse(state["capabilities"]["manage_workers"])
        self.assertEqual(server.worker_nodes, [self.NODE])
        self.assertFalse(server.workers_path.exists())
        serialized = json.dumps(state)
        for private in ("10.0.0.5", "narrator", "/opt/qwen", "/models/Base"):
            self.assertNotIn(private, serialized)

    @staticmethod
    def probed(python="", model="", problem=""):
        return {
            "host": "narrator@10.0.0.5", "python": python, "model": model,
            "devices": [{"device": "mps", "name": "Apple MPS"}] if python else [],
            "problem": problem,
        }

    def run_setup(self, model, probes):
        scripts = []
        with mock.patch.object(web, "probe_worker_node", side_effect=probes) as probe, \
                mock.patch.object(web.WorkerSetup, "remote", autospec=True,
                                  side_effect=lambda setup, script: scripts.append(script)):
            setup = web.WorkerSetup("narrator@10.0.0.5", model).start()
            setup.thread.join(5)
        return setup.snapshot(), scripts, probe

    def test_setup_installs_only_what_the_node_lacks_and_ends_with_its_report(self):
        venv = "/Users/narrator/hilde/.venv/bin/python"
        ready = self.probed(venv, "/Users/narrator/hilde/models/Qwen3-TTS-12Hz-1.7B-Base")
        snapshot, scripts, probe = self.run_setup(
            "/local/models/Qwen3-TTS-12Hz-1.7B-Base",
            [self.probed(problem="No Python."), self.probed(venv, problem="No model."), ready],
        )
        self.assertEqual((snapshot["status"], snapshot["found"]), ("done", ready))
        self.assertEqual(scripts[0], web.INSTALL_SCRIPT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(scripts), 2)
        self.assertIn(f"exec {venv} - Qwen/Qwen3-TTS-12Hz-1.7B-Base <<", scripts[1])
        self.assertEqual(probe.call_args_list[-1].args[1], venv)

        # A node with Python but no model only downloads it, by the server's
        # Hugging Face ID; a node with both installs nothing.
        snapshot, scripts, _ = self.run_setup(
            "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
            [self.probed("/opt/qwen/bin/python", problem="No model."), ready],
        )
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(len(scripts), 1)
        self.assertIn("/opt/qwen/bin/python - Qwen/Qwen3-TTS-12Hz-0.6B-Base <<", scripts[0])
        snapshot, scripts, _ = self.run_setup("/local/Base", [ready])
        self.assertEqual((snapshot["status"], scripts), ("done", []))

    def test_setup_fails_with_the_reason_when_the_install_does_not_take(self):
        snapshot, scripts, _ = self.run_setup(
            "/local/Base",
            [self.probed(problem="No Python."),
             self.probed(problem="No Python with PyTorch and Qwen TTS was found.")],
        )
        self.assertEqual(len(scripts), 1)
        self.assertEqual(
            (snapshot["status"], snapshot["error"], snapshot["found"]),
            ("failed", "No Python with PyTorch and Qwen TTS was found.", None),
        )

    def test_one_setup_runs_at_a_time_and_stop_ends_it(self):
        server, origin = self.serve()
        installing, stopped = threading.Event(), threading.Event()

        def remote(setup, script):
            installing.set()
            stopped.wait(5)
            raise RuntimeError("ssh ended")

        def stop(setup):
            setup.stopped = True
            stopped.set()

        with mock.patch.object(web, "probe_worker_node",
                               return_value=self.probed(problem="No Python.")), \
                mock.patch.object(web.WorkerSetup, "remote", autospec=True, side_effect=remote), \
                mock.patch.object(web.WorkerSetup, "stop", autospec=True, side_effect=stop):
            status, answer = self.request(origin, "/api/workers/setup", {"host": "narrator@10.0.0.5"})
            self.assertEqual((status, answer["setup"]["status"]), (200, "running"))
            self.assertTrue(installing.wait(5))
            self.assertEqual(self.request(origin, "/api/workers/setup", {"host": "other"})[0], 409)
            self.assertEqual(self.request(origin, "/api/workers/setup/stop", {})[0], 200)
            server.worker_setup.thread.join(5)
        setup = self.request(origin, "/api/workers")[1]["setup"]
        self.assertEqual(
            (setup["host"], setup["status"], setup["error"]),
            ("narrator@10.0.0.5", "stopped", "Setup stopped."),
        )

    def test_a_node_added_while_a_book_waits_narrates_it_and_stays_until_the_book_ends(self):
        server, origin = self.serve()
        jobs = server.jobs
        first, _ = JobQueueTests.reserve(self, jobs, "first")
        first_run = JobQueueTests.ControlledRun("first.mp3")
        self.assertEqual(jobs.commit(first, first_run)["status"], "running")
        second, _ = JobQueueTests.reserve(self, jobs, "second")
        second_run = JobQueueTests.ControlledRun("second.mp3")
        self.assertEqual(jobs.commit(second, second_run)["status"], "queued")

        status, answer = self.request(origin, "/api/workers/add", self.NODE)
        self.assertEqual(status, 200, answer)
        self.assertEqual(web.read_workers(server.workers_path), ([self.NODE], []))
        first_run.close(0)
        self.assertTrue(second_run.started.wait(2))
        self.assertEqual(
            [(worker["target"], worker["device"])
             for worker in second_run.assigned_workers if worker["kind"] == "ssh"],
            [("narrator@10.0.0.5", "cuda:1"), ("narrator@10.0.0.5", "cuda:3")],
        )

        status, _ = self.request(origin, "/api/workers/remove", {"host": self.NODE["host"]})
        self.assertEqual(status, 409)
        self.assertEqual(web.read_workers(server.workers_path), ([self.NODE], []))
        second_run.close(0)
        self.idle(jobs)
        status, answer = self.request(origin, "/api/workers/remove", {"host": self.NODE["host"]})
        self.assertEqual((status, answer["nodes"]), (200, []))
        self.assertEqual(web.read_workers(server.workers_path), ([], []))
        self.assertEqual([item["label"] for item in answer["consumers"]], ["GPU 0"])

    def idle(self, jobs):
        deadline = time.monotonic() + 2
        while jobs.busy_consumer_ids() and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_a_gpu_turned_off_takes_no_book_and_the_pool_never_empties(self):
        server, origin = self.serve()
        jobs = server.jobs
        off = {"device": "cuda:0", "narrates": False}
        # This machine's only GPU stays on until another machine can narrate.
        self.assertEqual(self.request(origin, "/api/workers/local", off)[0], 409)
        self.assertEqual(self.request(origin, "/api/workers/add", self.NODE)[0], 200)
        status, answer = self.request(origin, "/api/workers/local", off)
        self.assertEqual(status, 200, answer)
        self.assertEqual([item["narrates"] for item in answer["local"]], [False])
        self.assertEqual([item["status"] for item in answer["consumers"]], ["off", "idle", "idle"])
        self.assertEqual(web.read_workers(server.workers_path), ([self.NODE], ["cuda:0"]))

        record, _ = JobQueueTests.reserve(self, jobs, "nodes only")
        run = JobQueueTests.ControlledRun("nodes-only.mp3")
        self.assertEqual(jobs.commit(record, run)["status"], "running")
        self.assertTrue(run.started.wait(2))
        self.assertEqual({worker["kind"] for worker in run.assigned_workers}, {"ssh"})
        run.close(0)
        self.idle(jobs)
        # Nor can the last machine go while every GPU here is off.
        status, _ = self.request(origin, "/api/workers/remove", {"host": self.NODE["host"]})
        self.assertEqual(status, 409)
        self.assertEqual(web.read_workers(server.workers_path), ([self.NODE], ["cuda:0"]))

        # Back on, the GPU narrates the next book and cannot leave it midway.
        status, answer = self.request(
            origin, "/api/workers/local", {"device": "cuda:0", "narrates": True}
        )
        self.assertEqual((status, answer["consumers"][0]["status"]), (200, "idle"))
        record, _ = JobQueueTests.reserve(self, jobs, "everywhere")
        run = JobQueueTests.ControlledRun("everywhere.mp3")
        self.assertEqual(jobs.commit(record, run)["status"], "running")
        self.assertTrue(run.started.wait(2))
        self.assertIn("cuda:0", [worker["device"] for worker in run.assigned_workers])
        self.assertEqual(self.request(origin, "/api/workers/local", off)[0], 409)
        self.assertEqual(web.read_workers(server.workers_path), ([self.NODE], []))
        run.close(0)
        self.idle(jobs)

    def test_a_book_on_nodes_alone_starts_no_worker_on_this_machine(self):
        workers = [consumer["worker"] for consumer in web.remote_consumers([self.NODE])]
        command = web.narrate_command({
            "clone": self.CLONE, "input": "book.txt", "encoding": "utf-8",
            "output": "book.mp3", "chunk_max_chars": "500", "resume_dir": "work",
            "mp3_level": "", "voice_dir": "voices/Eir", "batch_size": "2",
            "workers": workers, "device": "cuda:0", "dtype": "auto",
            "attn": "sdpa", "language": "", "seed": "",
        })
        parser = cli.build_parser()
        args = parser.parse_args(command[3:])
        cli.check_distributed_mode(args, parser)
        with mock.patch.object(cli.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
                mock.patch.object(cli, "_run_transport"):
            specifications = cli._narration_worker_specifications(args, Path("voices/Eir"))
        self.assertEqual(
            [label for label, *_ in specifications],
            ["SSH narrator@10.0.0.5 cuda:1", "SSH narrator@10.0.0.5 cuda:3"],
        )
        self.assertEqual(cli._worker_topology(args)["local_devices"], [])

        base = command[3:command.index("--no-local-worker")]
        for extra in (["--no-local-worker"], ["--no-local-worker", "--worker-device", "cuda:1",
                                               "--ssh-worker", "spark"]):
            with self.subTest(extra), mock.patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit):
                cli.check_distributed_mode(parser.parse_args(base + extra), parser)


if __name__ == "__main__":
    unittest.main()
