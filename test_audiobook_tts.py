import argparse
import base64
import hashlib
import io
import http.client
import http.cookiejar
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundfile as sf
import audiobook_tts as cli
import audiobook_tts_web as web

from audiobook_tts import read_voice, save_voice, speech_endpoint
from audiobook_tts_web import GIB, Handler, PaperRun, local_model_names, normalize, paper_omp_environment
from audiobook_tts_web import parse_paper_response


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


class PaperWorkflowTests(unittest.TestCase):
    def test_audiobook_is_the_default_tab(self):
        self.assertEqual(normalize({})["tab"], "audiobook")

    def test_player_is_a_persisted_tab(self):
        self.assertEqual(normalize({"tab": "player"})["tab"], "player")

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
            "ssh_workers": ("narrator@spark-one",),
            "ssh_python": "python3",
            "ssh_model": "/srv/private/models/Base",
            "ssh_device": "cuda:0",
        }
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
        jobs = web.JobQueue(web.audiobook_consumers(clone, devices))
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

        self.assertEqual(
            [
                (worker["label"], worker["detail"], worker["status"])
                for worker in public["consumers"]
            ],
            [
                ("GPU 0", "Accelerator · 96 GiB", "idle"),
                ("GPU 1", "Accelerator · 96 GiB", "idle"),
                ("SSH worker 1", "cuda:0", "idle"),
            ],
        )
        self.assertEqual(public["configuration"], {
            "design": {"source": "server", "model": "gpt-4o-mini-tts"},
            "clone": {"source": "local", "model": "Base"},
        })
        self.assertEqual(local_design["design"], {
            "source": "local",
            "model": "Qwen/VoiceDesign",
            "device": "GPU 0",
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

    def test_browser_device_selection_is_ignored(self):
        state = normalize({
            "schema": web.STATE_SCHEMA_VERSION,
            "runtime": {"device": "cuda:1"},
        })

        self.assertNotIn("device", state["runtime"])

    def test_terminal_bibliography_is_omitted_from_narration(self):
        paragraphs = web.split_paper_paragraphs(
            "Conclusion cites Smith (2020) in the body.\n\n"
            "References\n\n"
            "Smith, A. Important work. 2020.\n\n"
            "Jones, B. Another work. 2021."
        )

        kept, omitted = web.omit_reference_sections(paragraphs)

        self.assertEqual(kept, ["Conclusion cites Smith (2020) in the body."])
        self.assertEqual(omitted, 3)

    def test_appendix_after_markdown_references_is_preserved(self):
        paragraphs = web.split_paper_paragraphs(
            "# Main text\n\n"
            "The substantive discussion remains intact.\n\n"
            "## Bibliography\n\n"
            "Smith, A. Important work. 2020.\n\n"
            "## Appendix A\n\n"
            "Appendix evidence remains part of the narration."
        )

        kept, omitted = web.omit_reference_sections(paragraphs)

        self.assertEqual(kept, [
            "# Main text",
            "The substantive discussion remains intact.",
            "## Appendix A",
            "Appendix evidence remains part of the narration.",
        ])
        self.assertEqual(omitted, 2)

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
                self.assertEqual(
                    set(public_state["capabilities"]),
                    {"airdrop"},
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


class SharedLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = web.SharedStorage(self.root / "library")
        self.storage.ensure()

    def test_shared_assets_have_fixed_locations_and_derived_audiobook_name(self):
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

        state = normalize({
            "audiobook": {
                "document": "Research Paper.pdf",
                "voice": "Calm Voice.v2",
                "adapt": False,
            },
        })
        values = web.values_of(
            state, web.unconfigured_tts_models(), self.storage
        )

        self.assertEqual(
            Path(values["output"]),
            self.storage.audiobooks / "Research Paper-Calm Voice.v2.mp3",
        )

    def test_unchanged_document_and_voice_require_overwrite_confirmation(self):
        document = self.storage.documents / "paper.txt"
        document.write_text("The shared source.", encoding="utf-8")
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
        state = normalize({
            "audiobook": {
                "document": document.name,
                "voice": voice.name,
                "adapt": False,
            },
        })
        values = web.values_of(state, models, self.storage)
        input_version, voice_version = web.audiobook_versions(values)
        output = Path(values["output"])
        output.write_bytes(b"existing audiobook")
        web.write_json_atomic(
            web.audiobook_version_path(self.storage, output),
            {
                "schema": 1,
                "input_version": input_version,
                "voice_version": voice_version,
            },
        )

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
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual(caught.exception.code, 409)
        self.assertTrue(payload["confirmation_required"])
        self.assertIsNone(server.jobs.current_run())

        document.write_text("A new source version.", encoding="utf-8")
        changed_input_version, unchanged_voice_version = web.audiobook_versions(
            values
        )
        self.assertFalse(web.output_versions_match(
            self.storage,
            output,
            changed_input_version,
            unchanged_voice_version,
        ))

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

        buffer = io.BytesIO()
        sf.write(
            buffer,
            np.linspace(-0.2, 0.2, 1200, dtype=np.float32),
            24000,
            format="WAV",
            subtype="PCM_16",
        )
        speech = buffer.getvalue()
        requests = {"count": 0}

        class SpeechHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests["count"] += 1
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
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

        output = storage.audiobooks / "paper-alloy.mp3"
        prepared = storage.documents / "paper-narration.txt"
        self.assertEqual(run.code, 0)
        self.assertEqual(run.artifact, str(output))
        self.assertTrue(output.is_file())
        self.assertGreater(sf.info(output).frames, 0)
        self.assertIn("spoken audio", prepared.read_text(encoding="utf-8"))
        self.assertGreater(requests["count"], 0)
        self.assertFalse(run.stage.exists())
        reader = web.audiobook_reader_payload(storage, output.name)
        self.assertEqual(reader["name"], output.name)
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
        self.assertEqual(
            [
                data["done"]
                for event, data in run.history
                if event == "progress" and data["phase"] == "narration"
            ],
            list(range(1, requests["count"] + 1)),
        )




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

        markdown, synchronization = web.build_reader_artifacts(
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

        markdown, synchronization = web.build_reader_artifacts(
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
        output = storage.audiobooks / "legacy.mp3"
        sf.write(
            output,
            np.linspace(-0.1, 0.1, 3100, dtype=np.float32),
            24000,
            format="WAV",
            subtype="FLOAT",
        )
        markdown_name = "legacy.md"
        sync_name = "legacy.json"
        (storage.readers / markdown_name).write_text(
            "<!-- audiobook-tts:block=0 -->\n\n"
            "First sentence. Second sentence.",
            encoding="utf-8",
        )
        web.write_json_atomic(
            storage.readers / sync_name,
            {
                "schema": 1,
                "sample_rate": 24000,
                "duration_samples": 3100,
                "block_count": 1,
                "cues": [
                    {"block": 0, "start_sample": 0, "end_sample": 3100},
                ],
            },
        )
        web.write_json_atomic(
            web.audiobook_version_path(storage, output),
            {
                "schema": 3,
                "document": "legacy.txt",
                "voice": "Narrator",
                "reader": {
                    "markdown": markdown_name,
                    "sync": sync_name,
                },
            },
        )

        payload = web.audiobook_reader_payload(storage, output.name)

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

        web.align_existing_reader_words(
            storage,
            output.name,
            FakeWordAligner(),
        )
        aligned = web.audiobook_reader_payload(storage, output.name)
        self.assertEqual(aligned["word_timing"], "aligned")
        self.assertEqual(
            [cue["block"] for cue in aligned["word_cues"]],
            [0, 0, 1, 1],
        )
        second_checkpoint = next(
            cue for cue in aligned["cues"] if cue["block"] == 1
        )
        second_sentence_word = next(
            cue for cue in aligned["word_cues"] if cue["block"] == 1
        )
        self.assertEqual(
            second_checkpoint["start_sample"],
            second_sentence_word["start_sample"],
        )

    def test_visual_blocks_exclude_unspoken_text_and_absorb_invisible_audio(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        output = storage.audiobooks / "visual.mp3"
        sf.write(
            output,
            np.linspace(-0.1, 0.1, 9000, dtype=np.float32),
            24000,
            format="WAV",
            subtype="FLOAT",
        )
        markdown_name = "visual.md"
        sync_name = "visual.json"
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
        (storage.readers / markdown_name).write_text(
            "\n\n".join(
                f"<!-- audiobook-tts:block={index} -->\n\n{source}"
                for index, source in enumerate(sources)
            ),
            encoding="utf-8",
        )
        web.write_json_atomic(
            storage.readers / sync_name,
            {
                "schema": 1,
                "sample_rate": 24000,
                "duration_samples": 9000,
                "block_count": 3,
                "cues": [
                    {"block": 0, "start_sample": 0, "end_sample": 3000},
                    {"block": 1, "start_sample": 3000, "end_sample": 5000},
                    {"block": 2, "start_sample": 5000, "end_sample": 9000},
                ],
            },
        )
        web.write_json_atomic(
            web.audiobook_version_path(storage, output),
            {
                "schema": 3,
                "document": "visual.pdf",
                "voice": "Narrator",
                "reader": {
                    "markdown": markdown_name,
                    "sync": sync_name,
                },
            },
        )

        web.align_existing_reader_words(
            storage,
            output.name,
            FakeWordAligner(),
        )
        payload = web.audiobook_reader_payload(storage, output.name)

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

class BatchingTests(unittest.TestCase):
    class Model:
        """A fake model that runs out of GPU memory above a batch capacity."""

        def __init__(self, capacity):
            self.capacity = capacity
            self.calls = []

        def generate_voice_clone(self, text, language, voice_clone_prompt):
            import torch

            self.calls.append(list(text))
            if len(text) > self.capacity:
                raise torch.cuda.OutOfMemoryError("CUDA out of memory")
            return [np.zeros(len(item), dtype=np.float32) for item in text], 24000

    def generate(self, capacity, texts):
        model = self.Model(capacity)
        with mock.patch("sys.stdout", io.StringIO()):
            waveforms, rate = cli.generate_clone_batch(model, texts, "English", None)
        return model.calls, [len(waveform) for waveform in waveforms], rate

    def test_only_a_batch_that_runs_out_of_memory_is_retried_one_chunk_at_a_time(self):
        calls, lengths, _ = self.generate(3, ["a", "bb", "ccc"])
        self.assertEqual((calls, lengths), ([["a", "bb", "ccc"]], [1, 2, 3]))

        calls, lengths, rate = self.generate(1, ["a", "bb", "ccc"])
        self.assertEqual(calls, [["a", "bb", "ccc"], ["a"], ["bb"], ["ccc"]])
        self.assertEqual((lengths, rate), ([1, 2, 3], 24000))

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


class LocalPaperProviderTests(unittest.TestCase):
    # SGLang answers Ollama's /api/tags, but OMP's Ollama client fails against it.
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

    def test_openai_compatible_server_is_listed_and_routed(self):
        origin, names = self.list_models({"/v1/models": {"object": "list", "data": [
            {"id": "vision-model", "owned_by": "vllm"},
            {"id": "text-model", "owned_by": "vllm"},
        ]}}, "lm-studio")

        self.assertEqual(names, ("text-model", "vision-model"))
        environment = paper_omp_environment(origin, "lm-studio")
        self.assertEqual(environment["LM_STUDIO_BASE_URL"], f"{origin}/v1")

    def test_choosing_ollama_for_a_server_that_only_imitates_it_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.list_models(self.SGLANG_ROUTES, "ollama")

        self.assertEqual(
            self.list_models(self.SGLANG_ROUTES, "lm-studio")[1], ("deepseek-v4.1-flash",)
        )

    def test_ollama_is_listed_and_routed(self):
        origin, names = self.list_models({
            "/api/version": {"version": "0.32.8"},
            "/api/tags": {"models": [{"name": "qwen3.8:27b"}, {"name": "deepseek-v4-flash:latest"}]},
        }, "ollama")

        self.assertEqual(names, ("deepseek-v4-flash:latest", "qwen3.8:27b"))
        self.assertEqual(paper_omp_environment(origin, "ollama")["OLLAMA_BASE_URL"], origin)

    def test_adaptation_refuses_a_model_of_another_type_than_the_local_server(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = web.SharedStorage(Path(temporary.name))
        storage.ensure()
        prompt = Path(temporary.name) / "prompt.md"
        prompt.write_text("Adapt the text.", encoding="utf-8")

        def problem(model):
            state = normalize({"tab": "audiobook", "audiobook": {
                "adapt": True, "model": model,
                "local_server": "127.0.0.1:8010", "local_provider": "lm-studio",
            }})
            with mock.patch.object(web, "_DEVICE_OPTIONS", [{"value": "cpu", "label": "CPU"}]):
                values = web.values_of(state, web.unconfigured_tts_models(), storage)
            with mock.patch.object(web, "PAPER_PROMPT_PATH", prompt), \
                    mock.patch.object(web.shutil, "which", return_value="/usr/bin/omp"):
                return web._adaptation_problem(values)

        self.assertIsNotNone(problem("ollama/deepseek-v4.1-flash"))
        self.assertIsNone(problem("lm-studio/deepseek-v4.1-flash"))
        self.assertIsNone(problem("anthropic/claude-opus-5-5"))
        self.assertEqual(
            normalize({"audiobook": {"local_provider": "vllm"}})["audiobook"]["local_provider"], ""
        )


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
        (storage.audiobooks / "kept-book.mp3").write_bytes(payload)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            server.storage = storage
            server.verbose = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(
                    f"{origin}/api/download?asset=kept-book.mp3"
                ) as response:
                    downloaded = response.read()
                    disposition = response.headers["Content-Disposition"]
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(
                        f"{origin}/api/download?asset=..%2Fkept-book.mp3"
                    )
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual(downloaded, payload)
        self.assertIn('filename="kept-book.mp3"', disposition)
        self.assertEqual(refused.exception.code, 400)


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

    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.storage = self.storage
        server.verbose = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join()
            server.server_close()

        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_port}"

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
            command = web.create_voice_command(web.values_of(state, models, self.storage))

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

    def test_library_lists_titles_durations_and_sources_newest_first(self):
        paper = self.storage.audiobooks / "paper-Martin.mp3"
        novel = self.storage.audiobooks / "great_expectations-Sarah.mp3"
        for output in (paper, novel):
            sf.write(
                output, np.zeros(24000, dtype=np.float32), 24000,
                format="MP3", subtype="MPEG_LAYER_III",
            )
        (self.storage.audiobooks / "notes.txt").write_text("not a book", encoding="utf-8")
        (self.storage.readers / "paper.md").write_text(
            "<!-- audiobook-tts:block=0 -->\n\n# Attention Is *All* You Need\n\n"
            "<!-- audiobook-tts:block=1 -->\n\nBody text.",
            encoding="utf-8",
        )
        web.write_json_atomic(
            web.audiobook_version_path(self.storage, paper),
            {"document": "paper.pdf", "voice": "Martin", "reader": {"markdown": "paper.md"}},
        )
        web.write_json_atomic(
            web.audiobook_version_path(self.storage, novel),
            {"document": "great_expectations.txt", "voice": "Sarah"},
        )
        os.utime(paper, ns=(1_700_000_000_000_000_000,) * 2)
        os.utime(novel, ns=(1_800_000_000_000_000_000,) * 2)
        origin = self.serve()

        with urllib.request.urlopen(f"{origin}/api/library") as response:
            books = json.load(response)["books"]

        self.assertEqual(
            [(book["name"], book["title"], book["source"], book["voice"]) for book in books],
            [
                ("great_expectations-Sarah.mp3", "great expectations",
                 "great_expectations.txt", "Sarah"),
                ("paper-Martin.mp3", "Attention Is All You Need", "paper.pdf", "Martin"),
            ],
        )
        for book in books:
            self.assertAlmostEqual(book["duration"], 1.0, delta=0.1)

    def test_library_describes_a_book_narrated_again_afresh(self):
        book = self.storage.audiobooks / "tale-Martin.mp3"
        sf.write(
            book, np.zeros(24000, dtype=np.float32), 24000,
            format="MP3", subtype="MPEG_LAYER_III",
        )
        record = web.audiobook_version_path(self.storage, book)
        web.write_json_atomic(record, {"document": "old_tale.txt", "voice": "Martin"})
        os.utime(record, ns=(1_700_000_000_000_000_000,) * 2)
        self.assertEqual(web.library_catalog(self.storage)[0]["title"], "old tale")

        web.write_json_atomic(record, {"document": "new_tale.txt", "voice": "Sarah"})
        os.utime(record, ns=(1_800_000_000_000_000_000,) * 2)
        again = web.library_catalog(self.storage)[0]

        self.assertEqual((again["title"], again["voice"]), ("new tale", "Sarah"))

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
        book = storage.audiobooks / "indexed-book.mp3"
        sf.write(book, waveform, rate, format="MP3", subtype="MPEG_LAYER_III")
        mp3 = book.read_bytes()
        mp3_path = "/api/audio?name=indexed-book.mp3"
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
            event == "log" and "Omitted 2 paragraphs" in data
            for event, data in run.history
        ))
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
            ("Spoken paragraph.", "Short context."),
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
        self.assertIn('<SOURCE_PARAGRAPH number="1">', requests[1])
        self.assertIn('<SOURCE_PARAGRAPH number="3">', requests[1])
        self.assertNotIn('<SOURCE_PARAGRAPH number="4">', requests[1])
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
        self.assertIn("Paragraph 1: Summary 1", context)
        self.assertIn("Paragraph 11: Summary 11", context)
        self.assertIn("older source-batch summaries omitted", context)


    def test_stop_terminates_every_inflight_child(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        run_directory = root / "run"
        marker_directory = root / "markers"
        run_directory.mkdir()
        marker_directory.mkdir()
        source = root / "paper.md"
        output = run_directory / "paper-audiobook.txt"
        prompt = root / "prompt.md"
        executable = root / "omp"
        source.write_text(
            "\n\n".join(f"Source {index}." for index in range(1, 5)),
            encoding="utf-8",
        )
        prompt.write_text("Adapt every paragraph.", encoding="utf-8")
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            "request = next(arg[1:] for arg in sys.argv[1:] "
            "if arg.startswith('@') and 'paragraphs-' in arg)\n"
            f"(Path({str(marker_directory)!r}) / Path(request).stem).touch()\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        run = PaperRun(
            source,
            output,
            "utf-8",
            in_flight=4,
            executable=str(executable),
            prompt_path=prompt,
        )
        worker = threading.Thread(target=run.pump)
        worker.start()
        deadline = time.monotonic() + 5
        while len(tuple(marker_directory.iterdir())) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            self.assertEqual(len(tuple(marker_directory.iterdir())), 4)
        finally:
            run.stop()
            worker.join(10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(run.code, 130)
        with run.process_lock:
            self.assertEqual(run.processes, set())



if __name__ == "__main__":
    unittest.main()
