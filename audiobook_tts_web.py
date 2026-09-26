"""Web front end for audiobook_tts.py.

A dependency-light HTTP server that serves one page, drives the TTS CLI and OMP
as child processes, and converts uploaded PDFs to Markdown in a short-lived
converter child. Logs and progress stream over server-sent events; successful
artifacts are exposed for playback, download, or AirDrop as applicable.

Run it on the machine that holds the models and configure each TTS backend once
for the whole server process:

    python audiobook_tts_web.py \\
        --voice-design-model models/Qwen3-TTS-12Hz-1.7B-VoiceDesign \\
        --voice-clone-model models/Qwen3-TTS-12Hz-1.7B-Base --port 8800

Documents, saved voices, completed audiobooks, and resumable checkpoints live
under one server-owned shared storage root. The interface selects named shared
assets and never accepts arbitrary filesystem paths. The server binds to
0.0.0.0 by default, so anyone who can reach the port can read or replace those
shared assets as the user running it. Use --host 127.0.0.1 to limit access.

Window state is stored in per-browser cookies, but TTS model configuration is
owned by the server process and is never accepted from a browser.
"""

import argparse
import array
import base64
import concurrent.futures
import json
import hashlib
import html
import mimetypes
import mmap
import os
import platform
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import webbrowser
import zlib
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from markdown_it import MarkdownIt

from audiobook_tts import (
    VOICE_DESCRIPTION_FILE,
    VOICE_PREVIEW_FILE,
    speech_endpoint,
    split_sentences,
    split_text,
    ssh_target,
)


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "audiobook_tts.py"
BRAND_IMAGE_PATH = ROOT / "assets" / "zeki.jpg"
APP_ICON_PATH = ROOT / "assets" / "hilde-dark.png"
PAPER_PROMPT_PATH = ROOT / "prompts" / "PAPER-AUDIO-BOOK.md"
STOCK_VOICES_PATH = ROOT / "voices"
BOOK_UPLOAD_LIMIT = 64 * 1024 * 1024
DEFAULT_STORAGE_ROOT = ROOT / "User"
STATE_COOKIE_PREFIX = "audiobook_tts_state"
STATE_COOKIE_COUNT = f"{STATE_COOKIE_PREFIX}_chunks"
STATE_COOKIE_CHUNK_BYTES = 3_800
STATE_COOKIE_MAX_CHUNKS = 10
STATE_COOKIE_MAX_JSON_BYTES = 96 * 1024
STATE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
STATE_SCHEMA_VERSION = 4
GIB = 2**30
VOICE_FILES = ("reference.wav", "transcript.txt")
# Every voice created here speaks this passage in its reference clip, so voice
# previews compare speed, tone, and naturalness on the same words. Older voices
# get it rendered into preview.wav by --render-voice-previews.
VOICE_REFERENCE_TEXT = (
    "Every story begins with a single voice. I will read each chapter to you "
    "at an easy, steady pace, clear enough to follow and warm enough to enjoy. "
    "Shall we turn the page and begin?"
)
PAPER_LOOP_INSTRUCTION = (
    "You will process the included narration material in batches of one or more "
    "consecutive paragraphs. Standalone bibliographies and reference lists have "
    "already been removed; never recreate them. Preserve every remaining paragraph "
    "in full and in source order. After each batch, also provide a compact summary "
    "which will be kept in the context window instead of the source text."
)

ROLE_LABELS = {"design": "VoiceDesign model", "clone": "Base model"}
SERVER_MODEL_DEFAULTS = {"design": "gpt-4o-mini-tts", "clone": "tts-1"}
NO_INSTRUCTIONS = ("tts-1", "tts-1-hd")
PAPER_SUFFIXES = {".pdf", ".txt", ".text", ".md", ".markdown"}
OLLAMA_DEFAULT_SERVER = "http://127.0.0.1:11434"
OPENAI_OAUTH_PROVIDER = "openai-codex-device"
OPENAI_MODEL_PROVIDER = "openai-codex"
MODEL_CATALOG_TIMEOUT = 30
LOCAL_SERVER_TIMEOUT = 5
PAPER_DOWNLOAD_TIMEOUT = 60
LOCAL_MODEL_PROVIDERS = ("ollama", "lm-studio")
PAPER_RESPONSE_ATTEMPTS = 3
PAPER_DEFAULT_IN_FLIGHT = 4
PAPER_MAX_IN_FLIGHT = 32
PAPER_DEFAULT_PARAGRAPHS_PER_WORKER = 1
PAPER_MAX_PARAGRAPHS_PER_WORKER = 32
PAPER_MAX_SUMMARY_CHARS = 2_000
PAPER_MIN_SUMMARY_CONTEXT_CHARS = 4_000
PAPER_MAX_SUMMARY_CONTEXT_CHARS = 24_000
PAPER_TOTAL_SUMMARY_CONTEXT_CHARS = 96_000
PAPER_RESPONSE_PATTERN = re.compile(
    r"<NARRATION>\s*(.*?)\s*</NARRATION>\s*"
    r"<SUMMARY>\s*(.*?)\s*</SUMMARY>",
    flags=re.DOTALL | re.IGNORECASE,
)
REFERENCE_SECTION_TITLES = frozenset({
    "bibliography",
    "literature cited",
    "notes and references",
    "reference list",
    "references",
    "references and notes",
    "selected bibliography",
    "works cited",
})
POST_REFERENCE_SECTION_TITLES = frozenset({
    "acknowledgments",
    "acknowledgements",
    "author contributions",
    "conflict of interest",
    "conflicts of interest",
    "data availability",
    "ethics statement",
    "funding",
})
POST_REFERENCE_SECTION_PREFIXES = (
    "appendix",
    "appendices",
    "supplementary material",
    "supplemental material",
)
MARKDOWN_HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
SECTION_NUMBER_PATTERN = re.compile(
    r"^(?:(?:chapter|section)[ \t]+)?"
    r"(?:\d+(?:\.\d+)*|[ivxlcdm]+)[.)]?[ \t]+",
    flags=re.IGNORECASE,
)

BATCH_LINE = re.compile(r"^Generating batch \d+ \(chunks \d+-(\d+)/(\d+)\)")
CHUNK_LINE = re.compile(r"^Requesting chunk (\d+)/(\d+)")
CHECKPOINT_LINE = re.compile(r"^Checkpointed chunk (\d+)/(\d+)")
WROTE_LINE = re.compile(r"^Wrote (.+) \([\d.]+s, \d+ Hz\)$")
SAVED_LINE = re.compile(r"^Saved voice: (.+) \([\d.]+s, \d+ Hz\)$")
READER_BLOCK_PATTERN = re.compile(
    r"^<!-- audiobook-tts:block=(\d+) -->[ \t]*$",
    flags=re.MULTILINE,
)
MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+([\"'])(.*?)\3)?\)"
)
MARKDOWN_TABLE_DIVIDER = re.compile(r"[ \t]*:?-{3,}:?[ \t]*")
READER_IMAGE_DATA = re.compile(
    r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=\r\n]+",
    flags=re.IGNORECASE,
)
READER_WORD_PATTERN = re.compile(
    r"[^\W_]+(?:['’][^\W_]+)*",
    flags=re.UNICODE,
)
READER_MARKDOWN = MarkdownIt(
    "commonmark",
    {"html": False, "linkify": False, "typographer": False},
).enable("table")
WORD_ALIGNMENT_LOCK = threading.Lock()
# Layer III bitrate tables in kbps, indexed by [MPEG-1][bitrate index].
MP3_LAYER3_KBPS = (
    (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
)
MP3_SAMPLE_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}
# Samples every Layer III decoder emits before the encoder's first sample.
MP3_DECODER_DELAY = 529
MP4_LAYOUT_CACHE_SIZE = 4

_DEVICE_PROBE = """\
import json
import torch

devices = [{"value": "cpu", "label": "CPU"}]
if torch.cuda.is_available():
    # Before initialization the count comes from NVML and includes GPUs the
    # CUDA runtime cannot open; narration children see only the runtime's.
    torch.cuda.init()
    for index in range(torch.cuda.device_count()):
        properties = torch.cuda.get_device_properties(index)
        devices.append({
            "value": f"cuda:{index}",
            "label": f"CUDA {index} — {properties.name}",
            "name": properties.name,
            "memory": properties.total_memory,
        })
mps = getattr(torch.backends, "mps", None)
if mps is not None and mps.is_available():
    devices.append({"value": "mps", "label": "Apple MPS"})
print(json.dumps(devices))
"""

_PDF_PAGE_COUNT = """\
import sys
import pymupdf

with pymupdf.open(sys.argv[1]) as document:
    print(document.page_count)
"""

_PDF_CONVERTER = """\
import sys
from pathlib import Path

import pymupdf4llm

source, output, images = (Path(value) for value in sys.argv[1:4])
page = int(sys.argv[4])
images.mkdir(parents=True, exist_ok=True)
markdown = pymupdf4llm.to_markdown(
    str(source),
    pages=[page],
    header=False,
    footer=False,
    write_images=True,
    image_path=str(images),
    image_format="png",
    force_text=True,
    use_ocr=True,
    show_progress=False,
)
if not isinstance(markdown, str):
    raise RuntimeError(f"PDF page {page + 1} returned invalid Markdown")
output.write_text(markdown, encoding="utf-8")
print(f"Extracted PDF page {page + 1} ({len(markdown)} Markdown characters).")
"""
_DEVICE_OPTIONS = None
_DEVICE_OPTIONS_LOCK = threading.Lock()
_MP4_LAYOUTS = {}
_MP4_LAYOUT_LOCK = threading.Lock()


def available_devices():
    """Probe devices in a child so importing torch does not burden the web process."""
    global _DEVICE_OPTIONS
    with _DEVICE_OPTIONS_LOCK:
        if _DEVICE_OPTIONS is not None:
            return _DEVICE_OPTIONS
        try:
            result = subprocess.run(
                [sys.executable, "-c", _DEVICE_PROBE],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            choices = json.loads(result.stdout.strip().splitlines()[-1])
            if not isinstance(choices, list) or not choices:
                raise ValueError("device probe returned no choices")
            _DEVICE_OPTIONS = [
                {
                    "value": item["value"],
                    "label": item["label"],
                    **{
                        key: item[key]
                        for key, kind in (("name", str), ("memory", int))
                        if isinstance(item.get(key), kind)
                    },
                }
                for item in choices
                if isinstance(item, dict)
                and isinstance(item.get("value"), str)
                and isinstance(item.get("label"), str)
            ]
            if not _DEVICE_OPTIONS:
                raise ValueError("device probe returned no valid choices")
        except (OSError, subprocess.SubprocessError, ValueError):
            _DEVICE_OPTIONS = [{"value": "cpu", "label": "CPU"}]
        return _DEVICE_OPTIONS


def resolve_device(requested, devices=None):
    """Resolve the automatic runtime choice without breaking CPU-only hosts."""
    requested = (requested or "auto").strip()
    if requested != "auto":
        return requested
    choices = devices if devices is not None else available_devices()
    values = [choice.get("value", "") for choice in choices]
    for prefix in ("cuda:", "mps"):
        match = next((value for value in values if value.startswith(prefix)), None)
        if match:
            return match
    return "cpu"


def public_device_label(value):
    """Name a local torch device the way the pool shows it to browsers."""
    if value.startswith("cuda:"):
        return f"GPU {value[5:]}"
    return {"cpu": "CPU", "mps": "Apple MPS"}.get(value, value)


def public_device(choice):
    """Describe one local device for browsers; hosts and paths never appear."""
    detail = [choice["name"]] if choice.get("name") else []
    if choice.get("memory"):
        detail.append(f"{choice['memory'] / GIB:.0f} GiB")
    return {
        "label": public_device_label(choice["value"]),
        "detail": " · ".join(detail),
    }


def audiobook_consumers(clone_model, devices=None):
    """Return local GPU and configured SSH narration workers."""
    source = clone_model.get("source")
    if source == "server":
        return ({
            "id": "remote",
            "kind": "server",
            "device": None,
            "label": "Remote speech server",
            "public": {
                "label": "Speech server",
                "detail": clone_model["server_model"],
            },
            "worker": None,
        },)
    if source != "local":
        return ({
            "id": "default",
            "kind": "generic",
            "device": None,
            "label": "Worker",
            "public": {"label": "Worker", "detail": ""},
            "worker": None,
        },)
    choices = list(devices if devices is not None else available_devices())
    selected = [
        choice for choice in choices
        if str(choice.get("value", "")).startswith("cuda:")
    ]
    if not selected:
        resolved = resolve_device("auto", choices)
        selected = [
            next(
                (
                    choice for choice in choices
                    if choice.get("value") == resolved
                ),
                {"value": resolved, "label": resolved},
            )
        ]
    consumers = [
        {
            "id": choice["value"],
            "kind": "local",
            "device": choice["value"],
            "label": choice["label"],
            "public": public_device(choice),
            "worker": {
                "kind": "local",
                "device": choice["value"],
                "label": choice["label"],
            },
        }
        for choice in selected
    ]
    consumers.extend(
        {
            "id": f"ssh:{target}",
            "kind": "ssh",
            "device": None,
            "label": f"SSH {target} — {clone_model['ssh_device']}",
            "public": {
                "label": f"SSH worker {index}",
                "detail": clone_model["ssh_device"],
            },
            "worker": {
                "kind": "ssh",
                "target": target,
                "device": clone_model["ssh_device"],
                "label": f"SSH {target} — {clone_model['ssh_device']}",
            },
        }
        for index, target in enumerate(clone_model.get("ssh_workers", ()), 1)
    )
    return tuple(consumers)


# --- pure helpers -------------------------------------------------------------


class SharedStorage:
    """Server-owned flat asset library plus durable unfinished-job storage."""

    def __init__(self, root=DEFAULT_STORAGE_ROOT):
        self.root = Path(root).expanduser().resolve()
        self.voices = self.root / "Voices"
        self.audiobooks = self.root / "Audiobooks"
        self.documents = self.root / "Documents"
        self.in_progress = self.root / "in_progress"
        self.versions = self.audiobooks / ".versions"
        self.readers = self.audiobooks / ".readers"

    def ensure(self):
        for directory in (
            self.voices,
            self.audiobooks,
            self.documents,
            self.in_progress,
            self.versions,
            self.readers,
        ):
            directory.mkdir(mode=0o750, parents=True, exist_ok=True)

    def contains(self, path):
        try:
            Path(path).expanduser().resolve().relative_to(self.root)
        except (OSError, ValueError):
            return False
        return True


def safe_asset_name(value, fallback=""):
    """Return one filesystem component with control characters removed."""
    name = Path(str(value).replace("\\", "/")).name.replace("\x00", "").strip()
    name = re.sub(r"[\x00-\x1f\x7f]+", "_", name).strip(" .")
    return fallback if name in ("", ".", "..") else name


def resolve_asset(directory, name):
    """Resolve a browser-supplied flat asset name without permitting traversal."""
    clean = safe_asset_name(name)
    if not clean or clean != str(name).strip():
        raise ValueError("Select a valid asset.")
    return Path(directory) / clean


def file_version(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def saved_voice_version(voice_dir):
    digest = hashlib.sha256()
    for name in VOICE_FILES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        with (Path(voice_dir) / name).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def remote_voice_version(model, name):
    payload = json.dumps(
        {
            "server": model.get("server"),
            "model": model.get("server_model"),
            "voice": name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_json_atomic(path, value):
    target = Path(path)
    target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(target)


def asset_catalog(storage):
    return {
        "voices": sorted(
            path.name for path in storage.voices.iterdir()
            if path.is_dir() and is_saved_voice(path)
        ),
        "documents": sorted(
            path.name for path in storage.documents.iterdir() if path.is_file()
        ),
    }


def voice_preview(voice_dir):
    """Return a voice's preview clip and whether it speaks the fixed passage."""
    directory = Path(voice_dir)
    try:
        transcript = (directory / "transcript.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        transcript = ""
    if transcript == VOICE_REFERENCE_TEXT:
        return directory / "reference.wav", True
    preview = directory / VOICE_PREVIEW_FILE
    if preview.is_file():
        return preview, True
    return directory / "reference.wav", False


def voice_catalog(storage):
    """List saved voices with descriptions and preview versions for searching."""
    voices = []
    for path in sorted(storage.voices.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_dir() or not is_saved_voice(path):
            continue
        try:
            description = (path / VOICE_DESCRIPTION_FILE).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            description = ""
        preview, comparable = voice_preview(path)
        try:
            version = f"{preview.stat().st_mtime_ns:x}"
        except OSError:
            continue
        voices.append({
            "name": path.name,
            "description": " ".join(description.split()),
            "comparable": comparable,
            # Changes whenever the clip is replaced, so browsers refetch it.
            "preview": version,
        })
    return voices


def voice_preview_command(clone_model, voice_dir, output, device):
    """Narrate the fixed passage with one saved voice into a WAV file."""
    # Previews narrate with the runtime a fresh browser starts with.
    runtime = normalize({})["runtime"]
    return [
        sys.executable,
        "-u",
        str(SCRIPT),
        "narrate",
        "--voice-dir",
        str(voice_dir),
        "--text",
        VOICE_REFERENCE_TEXT,
        "--output",
        str(output),
    ] + (
        model_arguments(clone_model, "--clone-model-path")
        + shared_arguments({**runtime, "device": device})
    )


def render_voice_previews(storage, clone_model, device):
    """Render preview.wav for saved voices whose clip reads other words.

    Returns the names of voices that could not be rendered.
    """
    failed = []
    for voice_dir in sorted(storage.voices.iterdir(), key=lambda item: item.name.casefold()):
        if not voice_dir.is_dir() or not is_saved_voice(voice_dir):
            continue
        if voice_preview(voice_dir)[1]:
            continue
        print(f"Rendering the preview passage with {voice_dir.name}", flush=True)
        # Stage the clip so an interrupted render never publishes part of one.
        with tempfile.TemporaryDirectory(prefix=".preview-", dir=voice_dir) as temporary:
            staged = Path(temporary) / VOICE_PREVIEW_FILE
            command = voice_preview_command(clone_model, voice_dir, staged, device)
            if subprocess.run(command).returncode == 0 and staged.is_file():
                staged.replace(voice_dir / VOICE_PREVIEW_FILE)
            else:
                failed.append(voice_dir.name)
    return failed


def safe_output_stem(input_path):
    value = str(input_path)
    parsed = urllib.parse.urlsplit(value)
    source = urllib.parse.unquote(parsed.path) if parsed.scheme in ("http", "https") else value
    stem = Path(source).stem or parsed.hostname or "audiobook"
    return re.sub(r"[\x00-\x1f\x7f/\\]+", "_", stem).strip(" .") or "audiobook"


def safe_output_component(value, fallback):
    """Sanitize one output-name component without treating dots as suffixes."""
    component = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", str(value)).strip(" .")
    return component or fallback


def narration_output_name(input_name, voice_name):
    """Derive the one shared MP3 name from its document and narrator."""
    return (
        f"{safe_output_stem(input_name)}-"
        f"{safe_output_component(voice_name, 'voice')}.mp3"
    )


def prepared_document_name(input_name):
    """Derive the retained narration-ready text asset name."""
    return f"{safe_output_stem(input_name)}-narration.txt"


def audiobook_version_path(storage, output_path):
    return storage.versions / f"{Path(output_path).name}.json"


def read_json_file(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def audiobook_reader_paths(storage, output_path, audio_version):
    """Return content-addressed Markdown and timing sidecars for one audiobook."""
    prefix = f"{Path(output_path).name}.{audio_version[:16]}"
    return (
        storage.readers / f"{prefix}.md",
        storage.readers / f"{prefix}.json",
    )


_LIBRARY_ENTRIES = {}
_LIBRARY_LOCK = threading.Lock()


def audiobook_title(markdown, fallback):
    """Name a book by its first top-level Markdown heading near the start."""
    for source in _reader_markdown_sources(markdown)[:8]:
        heading = MARKDOWN_HEADING_PATTERN.fullmatch(source.strip())
        if heading is not None and len(heading.group(1)) == 1:
            title = re.sub(r"[*_`]+", "", heading.group(2)).strip()
            if title:
                return title
    return fallback


def library_entry(storage, output):
    """Describe one retained MP3 by title, duration, source, and narrator."""
    import soundfile as sf

    metadata = read_json_file(audiobook_version_path(storage, output)) or {}
    document, voice, reader = (
        metadata.get(key) for key in ("document", "voice", "reader")
    )
    document = document if isinstance(document, str) else ""
    stem = Path(document or output.name).stem
    title = re.sub(r"[-_]+", " ", stem).strip() or stem
    if isinstance(reader, dict):
        try:
            markdown = resolve_asset(
                storage.readers, str(reader.get("markdown", ""))
            ).read_text(encoding="utf-8")
            title = audiobook_title(markdown, title)
        except (OSError, UnicodeError, ValueError):
            pass
    try:
        duration = sf.info(str(output)).duration
    except (OSError, RuntimeError):
        duration = None
    return {
        "name": output.name,
        "title": title,
        "duration": duration,
        "source": document,
        "voice": voice if isinstance(voice, str) else "",
    }


def library_catalog(storage):
    """List retained audiobooks, newest first, reusing unchanged entries."""
    books = []
    for output in storage.audiobooks.iterdir():
        if not output.is_file() or output.suffix.lower() != ".mp3":
            continue
        try:
            status = output.stat()
        except OSError:
            continue
        try:
            record = audiobook_version_path(storage, output).stat().st_mtime_ns
        except OSError:
            record = None
        # Titles come from reader Markdown of up to a megabyte per book.
        signature = (status.st_size, status.st_mtime_ns, record)
        key = str(output)
        with _LIBRARY_LOCK:
            cached = _LIBRARY_ENTRIES.get(key)
        if cached is None or cached[0] != signature:
            cached = (signature, library_entry(storage, output))
            with _LIBRARY_LOCK:
                _LIBRARY_ENTRIES[key] = cached
        books.append((status.st_mtime_ns, cached[1]))
    books.sort(key=lambda item: item[0], reverse=True)
    return [book for _, book in books]


def delete_voice(storage, name):
    """Delete one saved voice; a linked voice loses only its link."""
    voice_dir = resolve_asset(storage.voices, name)
    if not is_saved_voice(voice_dir):
        raise FileNotFoundError("no such voice")
    if voice_dir.is_symlink():
        voice_dir.unlink()
        return
    # One rename takes the whole voice out of the library, so no catalog or job
    # snapshot sees part of it while its files are removed.
    trash = Path(tempfile.mkdtemp(prefix=".deleting-", dir=storage.voices))
    try:
        voice_dir.rename(trash / voice_dir.name)
    finally:
        shutil.rmtree(trash)


def delete_document(storage, name):
    """Delete one shared document; a linked document loses only its link."""
    document = resolve_asset(storage.documents, name)
    if not document.is_file():
        raise FileNotFoundError("no such document")
    document.unlink()


def delete_audiobook(storage, name):
    """Delete one retained MP3 with its version record and reader files."""
    output = resolve_asset(storage.audiobooks, name)
    if output.suffix.lower() != ".mp3" or not output.is_file():
        raise FileNotFoundError("no such audiobook")
    record = audiobook_version_path(storage, output)
    reader = (read_json_file(record) or {}).get("reader")
    named = (
        {reader.get("markdown"), reader.get("sync")}
        if isinstance(reader, dict) else set()
    )
    output.unlink()
    record.unlink(missing_ok=True)
    # Reader files are named for their book and audio version, so earlier
    # narrations of the same book can have left some behind as well.
    earlier = re.compile(re.escape(output.name) + r"\.[0-9a-f]{16}\.(?:md|json)")
    for sidecar in storage.readers.iterdir():
        if sidecar.name in named or earlier.fullmatch(sidecar.name):
            sidecar.unlink(missing_ok=True)
    with _LIBRARY_LOCK:
        _LIBRARY_ENTRIES.pop(str(output), None)


def prepare_library(storage):
    """Create the shared folders; a new library starts with the stock voices."""
    new = not storage.voices.exists()
    storage.ensure()
    if not new:
        return
    # Each voice is copied whole before it appears, so none is ever half there.
    staging = Path(tempfile.mkdtemp(prefix=".stock-", dir=storage.voices))
    try:
        for voice in sorted(STOCK_VOICES_PATH.glob("*")):
            if is_saved_voice(voice):
                shutil.copytree(
                    voice, staging / voice.name, ignore=shutil.ignore_patterns(".*")
                )
                (staging / voice.name).rename(storage.voices / voice.name)
    finally:
        shutil.rmtree(staging)


def is_markdown_table(paragraph):
    """Return whether a paragraph is a structurally valid pipe table."""
    rows = [line.strip() for line in paragraph.splitlines() if line.strip()]
    if len(rows) < 2:
        return False
    cells = rows[1].strip("|").split("|")
    return len(cells) >= 2 and all(
        MARKDOWN_TABLE_DIVIDER.fullmatch(cell) for cell in cells
    )


def reader_visual_markdown(paragraph):
    """Keep source tables and image elements beside adapted narration."""
    if is_markdown_table(paragraph):
        return paragraph.strip()
    return "\n\n".join(
        match.group(0) for match in MARKDOWN_IMAGE_PATTERN.finditer(paragraph)
    )


def _reader_source_is_visible(markdown):
    """Keep real text and visuals while rejecting format-control artifacts."""
    if reader_visual_markdown(markdown):
        return True
    return any(
        not char.isspace() and not unicodedata.category(char).startswith("C")
        for char in html.unescape(markdown)
    )


def _without_invisible_paragraphs(text):
    return "\n\n".join(
        paragraph
        for paragraph in split_paper_paragraphs(text)
        if _reader_source_is_visible(paragraph)
    )


def _reader_image_path(source, roots):
    value = urllib.parse.unquote(source.strip("<>"))
    path = Path(value)
    candidates = (path,) if path.is_absolute() else tuple(
        Path(root) / path for root in roots
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        for root in roots:
            try:
                resolved.relative_to(Path(root).resolve())
            except (OSError, ValueError):
                continue
            return resolved
    return None


def embed_reader_images(markdown, roots):
    """Inline validated local raster images without exposing server paths."""
    roots = tuple(Path(root) for root in roots)

    def replace(match):
        alt, source = match.group(1), match.group(2)
        if READER_IMAGE_DATA.fullmatch(source):
            return match.group(0)
        path = _reader_image_path(source, roots)
        if path is None:
            return alt or "Visual unavailable"
        mime = mimetypes.guess_type(path.name)[0]
        if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            return alt or "Visual unavailable"
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"![{alt}](data:{mime};base64,{payload})"

    return MARKDOWN_IMAGE_PATTERN.sub(replace, markdown)


def _adapted_reader_groups(narration, source_paragraphs, checkpoint_dir):
    checkpoints = sorted(Path(checkpoint_dir).glob("*.json"))
    if not checkpoints:
        return None
    groups = []
    for checkpoint in checkpoints:
        try:
            start = int(checkpoint.stem.split("-", 1)[0])
        except ValueError:
            return None
        data = read_json_file(checkpoint)
        if data is None:
            return None
        end = data.get("end")
        adapted = data.get("narration")
        if (
            not isinstance(end, int)
            or not isinstance(adapted, str)
            or not adapted.strip()
            or start < 1
            or end < start
            or end > len(source_paragraphs)
        ):
            return None
        groups.append((adapted.strip(), source_paragraphs[start - 1:end]))
    if "\n\n".join(group[0] for group in groups) != narration.strip():
        return None
    return groups


def reader_word_matches(text):
    """Return display words in stable spoken order."""
    return list(READER_WORD_PATTERN.finditer(text))


class ForcedWordAligner:
    """CPU MMS aligner mapping known transcript words to source samples."""

    def __init__(self):
        import torch
        import torchaudio
        import uroman

        self.torch = torch
        self.torchaudio = torchaudio
        self.bundle = torchaudio.pipelines.MMS_FA
        self.model = self.bundle.get_model().eval()
        self.tokenizer = self.bundle.get_tokenizer()
        self.aligner = self.bundle.get_aligner()
        self.romanizer = uroman.Uroman()

    def _normalize(self, word):
        romanized = self.romanizer.romanize_string(word)
        normalized = re.sub(
            r"[^a-z']",
            "",
            romanized.lower().replace("’", "'"),
        )
        return normalized or "*"

    def align_samples(
        self,
        samples,
        sample_rate,
        text,
        block,
        start_sample,
        word_index=0,
    ):
        words = reader_word_matches(text)
        if not words:
            return []
        torch = self.torch
        waveform = torch.as_tensor(samples, dtype=torch.float32)
        if waveform.ndim == 2:
            waveform = waveform.mean(dim=1)
        if waveform.ndim != 1 or not waveform.numel():
            raise ValueError("word alignment audio is empty")
        source_frames = waveform.numel()
        waveform = waveform.unsqueeze(0)
        if sample_rate != self.bundle.sample_rate:
            waveform = self.torchaudio.functional.resample(
                waveform,
                sample_rate,
                self.bundle.sample_rate,
            )
        transcript = [self._normalize(match.group(0)) for match in words]
        with torch.inference_mode():
            emission, _ = self.model(waveform)
        spans = self.aligner(
            emission[0],
            self.tokenizer(transcript),
        )
        if len(spans) != len(words) or any(not span for span in spans):
            raise ValueError("forced aligner returned incomplete word spans")
        ratio = source_frames / emission.shape[1]
        cues = []
        for offset, (match, span) in enumerate(zip(words, spans, strict=True)):
            local_start = round(span[0].start * ratio)
            local_end = round(span[-1].end * ratio)
            local_start = max(0, min(local_start, source_frames - 1))
            local_end = max(local_start + 1, min(local_end, source_frames))
            cues.append({
                "block": block,
                "index": word_index + offset,
                "text": match.group(0),
                "start_sample": start_sample + local_start,
                "end_sample": start_sample + local_end,
            })
        return cues

    def align_file(
        self,
        path,
        text,
        block,
        start_sample,
        word_index=0,
    ):
        import soundfile as sf

        samples, sample_rate = sf.read(
            path,
            dtype="float32",
            always_2d=True,
        )
        return self.align_samples(
            samples,
            sample_rate,
            text,
            block,
            start_sample,
            word_index,
        )

    def close(self):
        import gc

        self.model = None
        gc.collect()


def _reader_sentence_markdown(paragraph):
    """Split prose for highlighting while keeping visual Markdown attached."""
    prose = []
    visuals = []
    for part in split_paper_paragraphs(paragraph):
        visual = reader_visual_markdown(part)
        visual_only = bool(visual) and (
            is_markdown_table(part)
            or not MARKDOWN_IMAGE_PATTERN.sub("", part).strip()
        )
        (visuals if visual_only else prose).append(part)
    sentences = [
        sentence
        for part in prose
        for sentence in split_sentences(part)
    ]
    if not sentences:
        return ["\n\n".join(visuals)] if visuals else []
    if visuals:
        sentences[-1] = f"{sentences[-1]}\n\n" + "\n\n".join(visuals)
    return sentences


def _reader_blocks(narration, source, max_chars, adaptation_checkpoints=None):
    source_paragraphs = split_paper_paragraphs(source)
    groups = (
        _adapted_reader_groups(
            narration, source_paragraphs, adaptation_checkpoints
        )
        if adaptation_checkpoints is not None
        else None
    )
    if groups is None:
        narration_paragraphs = split_paper_paragraphs(narration)
        groups = [
            (
                paragraph,
                source_paragraphs[index:index + 1]
                if index < len(source_paragraphs)
                else (),
            )
            for index, paragraph in enumerate(narration_paragraphs)
        ]

    blocks = []
    paragraphs = []
    chunk_blocks = []
    flattened_chunks = []
    for adapted, source_group in groups:
        adapted_paragraphs = split_paper_paragraphs(adapted)
        paired = len(adapted_paragraphs) == len(source_group)
        group_blocks = []
        for index, paragraph in enumerate(adapted_paragraphs):
            paragraph_index = paragraphs[-1] + 1 if paragraphs else 0
            source_paragraph = source_group[index] if paired else ""
            unchanged = source_paragraph.strip() == paragraph.strip()
            sentences = split_sentences(paragraph) or [paragraph.strip()]
            display_sentences = (
                _reader_sentence_markdown(source_paragraph)
                if unchanged
                else list(sentences)
            )
            if len(display_sentences) != len(sentences):
                display_sentences = list(sentences)
            if not unchanged and source_paragraph:
                heading = MARKDOWN_HEADING_PATTERN.fullmatch(
                    source_paragraph.strip()
                )
                if heading is not None:
                    display_sentences[0] = (
                        f"{heading.group(1)} "
                        f"{display_sentences[0].lstrip('# ')}"
                    )
                visual = reader_visual_markdown(source_paragraph)
                if visual:
                    display_sentences[-1] = (
                        f"{display_sentences[-1]}\n\n{visual}"
                    )
            for sentence, display_sentence in zip(
                sentences, display_sentences, strict=True
            ):
                block_index = len(blocks)
                blocks.append(display_sentence)
                paragraphs.append(paragraph_index)
                group_blocks.append(block_index)
                chunks = split_text(
                    sentence, max_chars, sentence_chunks=True
                )
                flattened_chunks.extend(chunks)
                chunk_blocks.extend([block_index] * len(chunks))
        if not paired and group_blocks:
            visuals = "\n\n".join(
                visual
                for visual in map(reader_visual_markdown, source_group)
                if visual
            )
            if visuals:
                blocks[group_blocks[-1]] = (
                    f"{blocks[group_blocks[-1]]}\n\n{visuals}"
                )

    expected_chunks = split_text(
        narration, max_chars, sentence_chunks=True
    )
    if flattened_chunks != expected_chunks:
        blocks = []
        paragraphs = []
        chunk_blocks = []
        flattened_chunks = []
        for paragraph_index, paragraph in enumerate(
            split_paper_paragraphs(narration)
        ):
            for sentence in split_sentences(paragraph) or [paragraph]:
                block_index = len(blocks)
                blocks.append(sentence)
                paragraphs.append(paragraph_index)
                chunks = split_text(
                    sentence, max_chars, sentence_chunks=True
                )
                flattened_chunks.extend(chunks)
                chunk_blocks.extend([block_index] * len(chunks))
    if flattened_chunks != expected_chunks:
        raise ValueError("reader text does not match narration chunks")
    return blocks, paragraphs, expected_chunks, chunk_blocks


def build_reader_artifacts(
    narration,
    source,
    max_chars,
    audio_checkpoints,
    *,
    adaptation_checkpoints=None,
    image_roots=(),
    word_aligner=None,
    alignment_progress=None,
):
    """Build embedded Markdown and exact sentence cues from completed WAVs."""
    import soundfile as sf

    blocks, paragraphs, chunks, chunk_blocks = _reader_blocks(
        narration, source, max_chars, adaptation_checkpoints
    )
    if not chunks:
        raise ValueError("reader narration is empty")
    cues = []
    word_cues = []
    block_word_counts = {}
    alignment_failures = 0
    aligned_chunks = 0
    sample_rate = None
    current_sample = 0
    for index, block_index in enumerate(chunk_blocks, 1):
        checkpoint = Path(audio_checkpoints) / f"chunk-{index:06d}.wav"
        info = sf.info(checkpoint)
        if info.frames <= 0 or info.samplerate <= 0:
            raise ValueError(f"reader audio chunk {index} is empty")
        if sample_rate is None:
            sample_rate = info.samplerate
        elif info.samplerate != sample_rate:
            raise ValueError(
                f"reader audio chunk {index} has a different sample rate"
            )
        end_sample = current_sample + info.frames
        cues.append({
            "block": block_index,
            "start_sample": current_sample,
            "end_sample": end_sample,
        })
        if word_aligner is not None:
            chunk = chunks[index - 1]
            word_index = block_word_counts.get(block_index, 0)
            word_count = len(reader_word_matches(chunk))
            try:
                aligned = word_aligner.align_file(
                    checkpoint,
                    chunk,
                    block_index,
                    current_sample,
                    word_index,
                )
                if len(aligned) != word_count:
                    raise ValueError(
                        f"word aligner returned {len(aligned)} of "
                        f"{word_count} words"
                    )
            except Exception:
                alignment_failures += 1
            else:
                word_cues.extend(aligned)
                aligned_chunks += 1
            block_word_counts[block_index] = word_index + word_count
            if alignment_progress is not None:
                alignment_progress(
                    index,
                    len(chunk_blocks),
                    alignment_failures,
                )
        current_sample = end_sample
    markdown = "\n\n".join(
        f"<!-- audiobook-tts:block={index} -->\n\n"
        f"{embed_reader_images(block, image_roots)}"
        for index, block in enumerate(blocks)
    )
    word_timing = (
        "unavailable"
        if word_aligner is None or not word_cues
        else "partial"
        if alignment_failures
        else "aligned"
    )
    return markdown, {
        "schema": 3,
        "sample_rate": sample_rate,
        "duration_samples": current_sample,
        "block_count": len(blocks),
        # Narration paragraph of each block, so the reader can flow sentences.
        "paragraphs": paragraphs,
        "cues": cues,
        "word_timing": word_timing,
        "word_cues": word_cues,
        "aligned_chunks": aligned_chunks,
        "alignment_failures": alignment_failures,
    }


def _reader_markdown_sources(markdown):
    matches = list(READER_BLOCK_PATTERN.finditer(markdown))
    if not matches or markdown[:matches[0].start()].strip():
        raise ValueError("reader Markdown has no valid block markers")
    sources = []
    for index, match in enumerate(matches):
        block_id = int(match.group(1))
        if block_id != index:
            raise ValueError("reader Markdown block sequence is invalid")
        end = matches[index + 1].start() if index + 1 < len(matches) else None
        sources.append(markdown[match.end():end].strip())
    return sources


def _render_reader_sources(sources):
    return [
        {
            "id": index,
            "html": READER_MARKDOWN.render(
                re.sub(r"^(\d+)\.$", r"\1\\.", source.strip())
            ),
        }
        for index, source in enumerate(sources)
    ]


def render_reader_blocks(markdown):
    """Render generated marked blocks with raw HTML disabled."""
    return _render_reader_sources(_reader_markdown_sources(markdown))


def _upgrade_legacy_reader(sources, cues):
    """Give paragraph-era readers estimated sentence-level highlighting."""
    upgraded_sources = []
    paragraphs = []
    upgraded_cues = []
    for old_block, source in enumerate(sources):
        sentences = _reader_sentence_markdown(source) or [source]
        first_block = len(upgraded_sources)
        upgraded_sources.extend(sentences)
        paragraphs.extend([old_block] * len(sentences))
        block_cues = [cue for cue in cues if cue["block"] == old_block]
        if not block_cues:
            raise ValueError("reader block has no cue")
        if len(sentences) == 1:
            upgraded_cues.extend({
                **cue,
                "block": first_block,
            } for cue in block_cues)
            continue
        start = block_cues[0]["start_sample"]
        end = block_cues[-1]["end_sample"]
        duration = end - start
        if duration < len(sentences):
            raise ValueError("reader cue is too short for its sentences")
        weights = [
            max(
                1,
                len(
                    re.sub(
                        r"\s+",
                        " ",
                        MARKDOWN_IMAGE_PATTERN.sub("", sentence),
                    ).strip()
                ),
            )
            for sentence in sentences
        ]
        total_weight = sum(weights)
        cumulative_weight = 0
        previous = start
        for offset, weight in enumerate(weights):
            cumulative_weight += weight
            remaining = len(weights) - offset - 1
            boundary = (
                end
                if not remaining
                else start + round(duration * cumulative_weight / total_weight)
            )
            boundary = max(previous + 1, min(boundary, end - remaining))
            upgraded_cues.append({
                "block": first_block + offset,
                "start_sample": previous,
                "end_sample": boundary,
            })
            previous = boundary
    return upgraded_sources, paragraphs, upgraded_cues


def _collapse_invisible_reader_blocks(sources, paragraphs, cues, word_cues):
    """Assign artifact-only audio to an adjacent block the reader can show."""
    visible = [_reader_source_is_visible(source) for source in sources]
    if all(visible):
        return sources, paragraphs, cues, word_cues, 0, set()
    if not any(visible):
        raise ValueError("reader contains no visible blocks")

    visible_to_new = {
        old: new for new, old in enumerate(
            index for index, is_visible in enumerate(visible) if is_visible
        )
    }
    targets = [None] * len(sources)
    next_visible = None
    for index in range(len(sources) - 1, -1, -1):
        if visible[index]:
            next_visible = index
        targets[index] = next_visible
    previous_visible = None
    for index, is_visible in enumerate(visible):
        if targets[index] is None:
            targets[index] = previous_visible
        if is_visible:
            previous_visible = index

    block_map = {
        old: visible_to_new[target] for old, target in enumerate(targets)
    }
    remapped_cues = []
    for cue in cues:
        remapped = {**cue, "block": block_map[cue["block"]]}
        if (
            remapped_cues
            and remapped_cues[-1]["block"] == remapped["block"]
            and remapped_cues[-1]["end_sample"] == remapped["start_sample"]
        ):
            remapped_cues[-1]["end_sample"] = remapped["end_sample"]
        else:
            remapped_cues.append(remapped)
    remapped_words = [
        {**cue, "block": block_map[cue["block"]]}
        for cue in word_cues
        if visible[cue["block"]]
    ]
    absorbed_blocks = {
        block_map[index] for index, is_visible in enumerate(visible)
        if not is_visible
    }
    return (
        [source for source, is_visible in zip(sources, visible) if is_visible],
        [
            paragraph
            for paragraph, is_visible in zip(paragraphs, visible)
            if is_visible
        ],
        remapped_cues,
        remapped_words,
        len(word_cues) - len(remapped_words),
        absorbed_blocks,
    )


def _reader_word_checkpoint_cues(
    block_count,
    cues,
    word_cues,
    duration_samples,
    held_blocks,
):
    """Use aligned sentence onsets instead of legacy length estimates."""
    if not word_cues:
        return cues
    original_starts = {}
    for cue in cues:
        original_starts.setdefault(cue["block"], cue["start_sample"])
    if set(original_starts) != set(range(block_count)):
        return cues
    first_words = {}
    for cue in word_cues:
        first_words.setdefault(cue["block"], cue)

    starts = []
    for block in range(block_count):
        if block == 0:
            start = 0
        elif block in first_words and block not in held_blocks:
            start = first_words[block]["start_sample"]
        else:
            start = original_starts[block]
        minimum = starts[-1] + 1 if starts else 0
        maximum = duration_samples - (block_count - block)
        starts.append(max(minimum, min(start, maximum)))
    return [
        {
            "block": block,
            "start_sample": start,
            "end_sample": (
                starts[block + 1]
                if block + 1 < block_count
                else duration_samples
            ),
        }
        for block, start in enumerate(starts)
    ]


def _reader_spoken_text(markdown):
    """Extract spoken prose while excluding attached tables and images."""
    words = []
    for paragraph in split_paper_paragraphs(markdown):
        if is_markdown_table(paragraph):
            continue
        text = MARKDOWN_IMAGE_PATTERN.sub("", paragraph)
        text = re.sub(r"\[([^\]\n]+)\]\([^)]+\)", r"\1", text)
        text = html.unescape(text)
        words.extend(match.group(0) for match in reader_word_matches(text))
    return " ".join(words)


def align_existing_reader_words(
    storage,
    name,
    word_aligner,
    *,
    progress=None,
    force=False,
):
    """Persist forced word cues for a retained sentence- or paragraph-era reader."""
    import soundfile as sf

    output = resolve_asset(storage.audiobooks, name)
    metadata = read_json_file(audiobook_version_path(storage, output))
    reader = metadata.get("reader") if metadata else None
    if not isinstance(reader, dict):
        raise FileNotFoundError("synchronized reader is unavailable")
    markdown_path = resolve_asset(storage.readers, reader.get("markdown", ""))
    sync_path = resolve_asset(storage.readers, reader.get("sync", ""))
    sources = _reader_markdown_sources(
        markdown_path.read_text(encoding="utf-8")
    )
    synchronization = read_json_file(sync_path)
    if synchronization is None:
        raise ValueError("reader synchronization is invalid")
    if synchronization.get("word_cues") and not force:
        return synchronization
    schema = synchronization.get("schema")
    cues = synchronization.get("cues")
    sample_rate = synchronization.get("sample_rate")
    if (
        schema not in (1, 2, 3)
        or not isinstance(cues, list)
        or not isinstance(sample_rate, int)
        or sample_rate <= 0
    ):
        raise ValueError("reader synchronization is invalid")

    plans = []
    next_block = 0
    for old_block, source in enumerate(sources):
        parts = (
            _reader_sentence_markdown(source) or [source]
            if schema == 1
            else [source]
        )
        blocks = list(range(next_block, next_block + len(parts)))
        next_block += len(parts)
        plans.append((
            old_block,
            blocks,
            [_reader_spoken_text(part) for part in parts],
        ))

    word_cues = []
    failures = 0
    aligned_blocks = 0
    with sf.SoundFile(output) as audio:
        if audio.samplerate != sample_rate:
            raise ValueError("reader audio sample rate does not match its cues")
        for position, (old_block, blocks, texts) in enumerate(plans, 1):
            block_cues = [cue for cue in cues if cue.get("block") == old_block]
            if not block_cues:
                failures += 1
                continue
            start = block_cues[0].get("start_sample")
            end = block_cues[-1].get("end_sample")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= audio.frames
            ):
                failures += 1
                continue
            transcript = " ".join(text for text in texts if text)
            counts = [len(reader_word_matches(text)) for text in texts]
            if not transcript or not sum(counts):
                continue
            try:
                audio.seek(start)
                samples = audio.read(
                    end - start,
                    dtype="float32",
                    always_2d=True,
                )
                aligned = word_aligner.align_samples(
                    samples,
                    sample_rate,
                    transcript,
                    blocks[0],
                    start,
                )
                if len(aligned) != sum(counts):
                    raise ValueError("forced aligner returned an incomplete block")
            except Exception:
                failures += 1
            else:
                cursor = 0
                for block, count in zip(blocks, counts, strict=True):
                    for index, cue in enumerate(aligned[cursor:cursor + count]):
                        cue["block"] = block
                        cue["index"] = index
                    cursor += count
                word_cues.extend(aligned)
                aligned_blocks += 1
            if progress is not None:
                progress(position, len(plans), failures)

    synchronization["word_timing"] = (
        "unavailable"
        if not word_cues
        else "partial"
        if failures
        else "aligned"
    )
    synchronization["word_cues"] = word_cues
    synchronization["aligned_chunks"] = aligned_blocks
    synchronization["alignment_failures"] = failures
    write_json_atomic(sync_path, synchronization)
    return synchronization


def audiobook_reader_payload(storage, name):
    """Return one validated, rendered reader without accepting server paths."""
    output = resolve_asset(storage.audiobooks, name)
    if output.suffix.lower() != ".mp3" or not output.is_file():
        raise FileNotFoundError("audiobook is unavailable")
    metadata = read_json_file(audiobook_version_path(storage, output))
    reader = metadata.get("reader") if metadata else None
    if not isinstance(reader, dict):
        raise FileNotFoundError("synchronized reader is unavailable")
    markdown_name = reader.get("markdown")
    sync_name = reader.get("sync")
    if not isinstance(markdown_name, str) or not isinstance(sync_name, str):
        raise ValueError("reader metadata is invalid")
    markdown_path = resolve_asset(storage.readers, markdown_name)
    sync_path = resolve_asset(storage.readers, sync_name)
    markdown = markdown_path.read_text(encoding="utf-8")
    synchronization = read_json_file(sync_path)
    if (
        synchronization is None
        or synchronization.get("schema") not in (1, 2, 3)
    ):
        raise ValueError("reader synchronization is invalid")
    sources = _reader_markdown_sources(markdown)
    sample_rate = synchronization.get("sample_rate")
    duration_samples = synchronization.get("duration_samples")
    block_count = synchronization.get("block_count")
    cues = synchronization.get("cues")
    if (
        not isinstance(sample_rate, int)
        or isinstance(sample_rate, bool)
        or sample_rate <= 0
        or not isinstance(duration_samples, int)
        or isinstance(duration_samples, bool)
        or duration_samples <= 0
        or block_count != len(sources)
        or not isinstance(cues, list)
    ):
        raise ValueError("reader synchronization is invalid")
    previous_end = 0
    for cue in cues:
        if not isinstance(cue, dict):
            raise ValueError("reader cue is invalid")
        block = cue.get("block")
        start = cue.get("start_sample")
        end = cue.get("end_sample")
        if (
            not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (block, start, end)
            )
            or not 0 <= block < len(sources)
            or start != previous_end
            or end <= start
        ):
            raise ValueError("reader cue is invalid")
        previous_end = end
    if previous_end != duration_samples:
        raise ValueError("reader duration is invalid")

    timing_precision = "exact"
    if synchronization["schema"] == 1:
        sources, paragraphs, cues = _upgrade_legacy_reader(sources, cues)
        timing_precision = "estimated"
    else:
        # Sidecars written before paragraph grouping show one sentence each.
        paragraphs = synchronization.get(
            "paragraphs", list(range(len(sources)))
        )
        if (
            not isinstance(paragraphs, list)
            or len(paragraphs) != len(sources)
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in paragraphs
            )
            or paragraphs[0] != 0
            or any(
                following - current not in (0, 1)
                for current, following in zip(paragraphs, paragraphs[1:])
            )
        ):
            raise ValueError("reader paragraphs are invalid")

    word_timing = synchronization.get("word_timing", "unavailable")
    word_cues = synchronization.get("word_cues", [])
    if (
        word_timing not in ("aligned", "partial", "unavailable")
        or not isinstance(word_cues, list)
        or (word_timing == "unavailable" and word_cues)
    ):
        raise ValueError("reader word synchronization is invalid")
    previous_word_end = 0
    last_word_indexes = {}
    for cue in word_cues:
        if not isinstance(cue, dict):
            raise ValueError("reader word cue is invalid")
        block = cue.get("block")
        index = cue.get("index")
        text = cue.get("text")
        start = cue.get("start_sample")
        end = cue.get("end_sample")
        if (
            not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (block, index, start, end)
            )
            or not isinstance(text, str)
            or not text
            or not 0 <= block < len(sources)
            or index <= last_word_indexes.get(block, -1)
            or start < previous_word_end
            or end <= start
            or end > duration_samples
        ):
            raise ValueError("reader word cue is invalid")
        last_word_indexes[block] = index
        previous_word_end = end
    if word_timing == "aligned" and not word_cues:
        raise ValueError("reader word synchronization is empty")
    sources, paragraphs, cues, word_cues, dropped_word_cues, held_blocks = (
        _collapse_invisible_reader_blocks(sources, paragraphs, cues, word_cues)
    )
    if not word_cues:
        word_timing = "unavailable"
    elif dropped_word_cues and word_timing == "aligned":
        word_timing = "partial"
    if timing_precision == "estimated":
        cues = _reader_word_checkpoint_cues(
            len(sources),
            cues,
            word_cues,
            duration_samples,
            held_blocks,
        )
    return {
        "name": output.name,
        "document": metadata.get("document"),
        "voice": metadata.get("voice"),
        "sample_rate": sample_rate,
        "timing_precision": timing_precision,
        "word_timing": word_timing,
        "word_cues": word_cues,
        "cues": cues,
        "blocks": [
            {**block, "paragraph": paragraph}
            for block, paragraph in zip(
                _render_reader_sources(sources), paragraphs, strict=True
            )
        ],
    }


def _mp3_layer3_frame(header):
    """Return one Layer III frame's byte size and stream layout, or None."""
    version = header >> 19 & 3
    bitrate = header >> 12 & 15
    rate = header >> 10 & 3
    if (
        header >> 21 != 0x7FF
        or version == 1
        or header >> 17 & 3 != 1
        or bitrate in (0, 15)
        or rate == 3
    ):
        return None
    mpeg1 = version == 3
    sample_rate = MP3_SAMPLE_RATES[version][rate]
    frame_samples = 1152 if mpeg1 else 576
    size = (
        frame_samples // 8 * MP3_LAYER3_KBPS[mpeg1][bitrate] * 1000
        // sample_rate
        + (header >> 9 & 1)
    )
    channels = 1 if header >> 6 & 3 == 3 else 2
    return size, (version, sample_rate, frame_samples, channels)


def mp3_frame_table(data):
    """Index a Layer III stream's audio frames and its LAME gapless trim."""
    end = len(data)
    position = 0
    if end >= 10 and data[:3] == b"ID3":
        position = 10 + (data[6] << 21 | data[7] << 14 | data[8] << 7 | data[9])
        if data[5] & 0x10:
            position += 10
    first = (
        _mp3_layer3_frame(int.from_bytes(data[position:position + 4], "big"))
        if position + 4 <= end
        else None
    )
    if first is None:
        raise ValueError("audio is not an MPEG Layer III stream")
    size, layout = first
    version, sample_rate, frame_samples, channels = layout
    side_info = (
        (32 if channels == 2 else 17)
        if version == 3
        else (17 if channels == 2 else 9)
    )
    cursor = position + 4 + side_info
    declared = delay = padding = None
    if data[cursor:cursor + 4] in (b"Xing", b"Info"):
        flags = int.from_bytes(data[cursor + 4:cursor + 8], "big")
        cursor += 8
        if flags & 1:
            declared = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += sum(
            length
            for bit, length in ((1, 4), (2, 4), (4, 100), (8, 4))
            if flags & bit
        )
        if data[cursor:cursor + 4] in (b"LAME", b"Lavf", b"Lavc"):
            trim = int.from_bytes(data[cursor + 21:cursor + 24], "big")
            delay, padding = trim >> 12, trim & 0xFFF
        # The tag frame decodes as silence and is not part of the audio.
        position += size
    payload_start = position
    sizes = array.array("I")
    read_header = struct.Struct(">I").unpack_from
    while position + 4 <= end:
        frame = _mp3_layer3_frame(read_header(data, position)[0])
        if frame is None or frame[1] != layout or position + frame[0] > end:
            break
        sizes.append(frame[0])
        position += frame[0]
    if position < end and not data[position:position + 8].startswith(
        (b"TAG", b"APETAGEX", b"LYRICS")
    ):
        raise ValueError("MPEG stream contains unindexed data")
    if not sizes or declared not in (None, len(sizes)):
        raise ValueError("MPEG frame count does not match its header")
    return {
        "payload_start": payload_start,
        "payload_end": position,
        "sizes": sizes,
        "version": version,
        "sample_rate": sample_rate,
        "frame_samples": frame_samples,
        "channels": channels,
        "delay": delay,
        "padding": padding,
    }


def _mp4_box(kind, *parts):
    payload = b"".join(parts)
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _mp4_full_box(kind, version, flags, *parts):
    return _mp4_box(kind, struct.pack(">I", version << 24 | flags), *parts)


def _mp4_descriptor(tag, payload):
    size = len(payload)
    return bytes((
        tag,
        0x80 | size >> 21 & 0x7F,
        0x80 | size >> 14 & 0x7F,
        0x80 | size >> 7 & 0x7F,
        size & 0x7F,
    )) + payload


def mp3_mp4_header(table):
    """Describe indexed MP3 frames as one MP4 track preceding its raw payload.

    Browsers seek VBR MP3 through a coarse 100-point table, then report the
    requested time while decoding audio from elsewhere. An MP4 sample table
    maps every frame exactly. The edit list applies the LAME gapless trim, so
    media time zero is the first narrated sample, the origin of reader cues.
    """
    sizes = table["sizes"]
    rate = table["sample_rate"]
    frame_samples = table["frame_samples"]
    count = len(sizes)
    media_duration = count * frame_samples
    skip = 0
    duration = media_duration
    if table["delay"] is not None:
        skip = table["delay"] + MP3_DECODER_DELAY
        duration = min(
            media_duration - table["delay"] - table["padding"],
            media_duration - skip,
        )
    if duration <= 0:
        raise ValueError("MPEG stream has no playable samples")
    wide = media_duration > 0xFFFFFFFF
    version = int(wide)
    times = struct.Struct(">QQ" if wide else ">II").pack
    length = struct.Struct(">Q" if wide else ">I").pack
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    payload_size = table["payload_end"] - table["payload_start"]
    largest = max(sizes)
    esds = _mp4_full_box(b"esds", 0, 0, _mp4_descriptor(
        3,
        struct.pack(">HB", 1, 0)
        + _mp4_descriptor(
            4,
            # MPEG-1 or MPEG-2 audio object type; stream type 5 is audio.
            bytes((0x6B if table["version"] == 3 else 0x69, 0x15))
            + largest.to_bytes(3, "big")
            + struct.pack(
                ">II",
                largest * 8 * rate // frame_samples,
                payload_size * 8 * rate // media_duration,
            ),
        )
        + _mp4_descriptor(6, b"\x02"),
    ))
    sample_table = (
        _mp4_full_box(
            b"stsd",
            0,
            0,
            struct.pack(">I", 1),
            _mp4_box(
                b"mp4a",
                bytes(6),
                struct.pack(
                    ">H8xHHHHI", 1, table["channels"], 16, 0, 0, rate << 16
                ),
                esds,
            ),
        ),
        _mp4_full_box(b"stts", 0, 0, struct.pack(">III", 1, count, frame_samples)),
        _mp4_full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, count, 1)),
        _mp4_full_box(
            b"stsz",
            0,
            0,
            struct.pack(">II", 0, count),
            struct.pack(f">{count}I", *sizes),
        ),
    )
    edit = (
        _mp4_box(b"edts", _mp4_full_box(
            b"elst",
            version,
            0,
            struct.pack(">I", 1),
            length(duration),
            struct.pack(">q" if wide else ">i", skip),
            struct.pack(">hh", 1, 0),
        ))
        if (skip, duration) != (0, media_duration)
        else b""
    )
    track_header = (
        _mp4_full_box(
            b"tkhd",
            version,
            3,
            times(0, 0),
            struct.pack(">II", 1, 0),
            length(duration),
            struct.pack(">8xhhhH", 0, 0, 0x100, 0),
            matrix,
            struct.pack(">II", 0, 0),
        ),
        edit,
    )
    media_header = (
        _mp4_full_box(
            b"mdhd",
            version,
            0,
            times(0, 0),
            struct.pack(">I", rate),
            length(media_duration),
            struct.pack(">HH", 0x55C4, 0),
        ),
        _mp4_full_box(
            b"hdlr", 0, 0, struct.pack(">I4s12x", 0, b"soun"), b"SoundHandler\0"
        ),
    )
    media_information = (
        _mp4_full_box(b"smhd", 0, 0, bytes(4)),
        _mp4_box(b"dinf", _mp4_full_box(
            b"dref", 0, 0, struct.pack(">I", 1), _mp4_full_box(b"url ", 0, 1)
        )),
    )

    def movie(chunk_offset):
        return _mp4_box(
            b"moov",
            _mp4_full_box(
                b"mvhd",
                version,
                0,
                times(0, 0),
                struct.pack(">I", rate),
                length(duration),
                struct.pack(">IH10x", 0x10000, 0x100),
                matrix,
                bytes(24),
                struct.pack(">I", 2),
            ),
            _mp4_box(b"trak", *track_header, _mp4_box(
                b"mdia",
                *media_header,
                _mp4_box(b"minf", *media_information, _mp4_box(
                    b"stbl",
                    *sample_table,
                    _mp4_full_box(
                        b"stco", 0, 0, struct.pack(">II", 1, chunk_offset)
                    ),
                )),
            )),
        )

    file_type = _mp4_box(
        b"ftyp", b"isom", struct.pack(">I", 0x200), b"isom", b"iso2", b"mp41"
    )
    media_data = (
        struct.pack(">I4s", 8 + payload_size, b"mdat")
        if 8 + payload_size <= 0xFFFFFFFF
        else struct.pack(">I4sQ", 1, b"mdat", 16 + payload_size)
    )
    chunk_offset = len(file_type) + len(movie(0)) + len(media_data)
    return file_type + movie(chunk_offset) + media_data


def mp3_mp4_layout(handle):
    """Return an MP4 prefix and the MP3 byte span it indexes for one open file."""
    status = os.fstat(handle.fileno())
    key = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
    with _MP4_LAYOUT_LOCK:
        layout = _MP4_LAYOUTS.pop(key, None)
        if layout is None:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                table = mp3_frame_table(data)
            layout = (
                mp3_mp4_header(table),
                table["payload_start"],
                table["payload_end"],
            )
        _MP4_LAYOUTS[key] = layout
        while len(_MP4_LAYOUTS) > MP4_LAYOUT_CACHE_SIZE:
            del _MP4_LAYOUTS[next(iter(_MP4_LAYOUTS))]
    return layout


def output_versions_match(storage, output_path, input_version, voice_version):
    if not Path(output_path).is_file():
        return False
    metadata = read_json_file(audiobook_version_path(storage, output_path))
    return bool(
        metadata
        and metadata.get("input_version") == input_version
        and metadata.get("voice_version") == voice_version
    )

def normalize_paper_url(value):
    """Return a safe HTTP(S) document URL, or an empty string for a local path."""
    value = str(value or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Document URL must use HTTP or HTTPS and include a host.")
    if parsed.username or parsed.password:
        raise ValueError("Document URL cannot contain credentials.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Document URL port is invalid.") from exc
    return urllib.parse.urlunsplit((
        parsed.scheme.lower(),
        parsed.netloc,
        parsed.path or "/",
        parsed.query,
        "",
    ))


def normalize_local_server(value):
    """Return one safe local-model origin from a URL or host:port."""
    value = str(value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Local server must be an HTTP URL or host:port.")
    if parsed.username or parsed.password:
        raise ValueError("Local server URL cannot contain credentials.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Local server URL must contain only a host and port.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Local server port is invalid.") from exc
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, "", "", "")
    ).rstrip("/")


def paper_omp_environment(local_server, provider):
    environment = os.environ.copy()
    server = normalize_local_server(local_server)
    if provider == "ollama":
        environment["OLLAMA_BASE_URL"] = server
    elif provider == "lm-studio":
        environment["LM_STUDIO_BASE_URL"] = f"{server}/v1"
    else:
        raise ValueError(f"Unsupported local model provider: {provider}")
    return environment


def run_omp_json(arguments, environment=None, timeout=MODEL_CATALOG_TIMEOUT):
    executable = shutil.which("omp")
    if executable is None:
        raise RuntimeError("The omp command is not available to the web server.")
    try:
        result = subprocess.run(
            [executable, *arguments],
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("OMP model discovery timed out.") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot start omp: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        if len(details) > 2000:
            details = details[-2000:]
        raise RuntimeError(
            f"omp exited with {result.returncode}"
            + (f": {details}" if details else "")
        )
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("OMP returned invalid model metadata.") from exc


def local_server_json(local_server, path, label):
    server = normalize_local_server(local_server)
    request = urllib.request.Request(
        f"{server}{path}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=LOCAL_SERVER_TIMEOUT) as response:
            body = response.read(8 * 1024 * 1024 + 1)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot reach {label} endpoint at {server}{path}: {exc}"
        ) from exc
    if len(body) > 8 * 1024 * 1024:
        raise RuntimeError(f"{label} model list at {server} is too large.")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise RuntimeError(
            f"{server}{path} did not return JSON model metadata."
        ) from exc


def valid_local_model_names(rows, keys):
    names = {
        str(next((row.get(key) for key in keys if row.get(key)), "")).strip()
        for row in rows
        if isinstance(row, dict)
    }
    return tuple(sorted(
        (
            name for name in names
            if name and len(name) <= 512
            and not any(ord(char) < 32 for char in name)
        ),
        key=str.lower,
    ))


def ollama_model_names(local_server):
    server = normalize_local_server(local_server)
    # SGLang answers /api/tags too, but OMP's Ollama client fails against it;
    # only Ollama itself answers /api/version.
    try:
        local_server_json(server, "/api/version", "Ollama")
    except RuntimeError as exc:
        if isinstance(exc.__cause__, urllib.error.HTTPError):
            raise RuntimeError(
                f"{server} is not an Ollama server. Choose OpenAI-compatible for "
                "SGLang, vLLM, or LM Studio."
            ) from exc
        raise
    payload = local_server_json(server, "/api/tags", "Ollama")
    try:
        names = valid_local_model_names(payload["models"], ("name", "model"))
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"{server} did not return an Ollama model list.") from exc
    if not names:
        raise RuntimeError(f"Ollama at {server} has no installed models.")
    return names


def openai_compatible_model_names(local_server):
    server = normalize_local_server(local_server)
    payload = local_server_json(server, "/v1/models", "OpenAI-compatible")
    try:
        names = valid_local_model_names(payload["data"], ("id",))
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"{server} did not return an OpenAI-compatible model list."
        ) from exc
    if not names:
        raise RuntimeError(
            f"OpenAI-compatible server at {server} has no available models."
        )
    return names


def local_model_names(local_server, provider):
    """List the models on a local server of the type the user chose."""
    if provider == "ollama":
        return ollama_model_names(local_server)
    if provider == "lm-studio":
        return openai_compatible_model_names(local_server)
    raise RuntimeError("Choose the local server's type under Add local.")


def paper_model_catalog(local_server="", local_provider=""):
    local_server = normalize_local_server(local_server)
    local_names = None
    local_error = ""
    environment = None
    if local_server:
        try:
            local_names = local_model_names(local_server, local_provider)
            environment = paper_omp_environment(local_server, local_provider)
        except RuntimeError as exc:
            local_error = str(exc)
    payload = run_omp_json(["models", "--json"], environment)
    rows = payload.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("OMP returned no model list.")
    if local_names is not None:
        discovered = {
            row.get("id"): row
            for row in rows
            if isinstance(row, dict) and row.get("provider") == local_provider
        }
        rows = [
            row for row in rows
            if not isinstance(row, dict) or row.get("provider") != local_provider
        ]
        rows.extend(
            discovered.get(name, {
                "provider": local_provider,
                "id": name,
                "selector": f"{local_provider}/{name}",
                "name": name,
            })
            for name in local_names
        )
    models = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        selector = row.get("selector")
        name = row.get("name") or row.get("id")
        if not all(isinstance(value, str) and value for value in (
            provider, selector, name
        )):
            continue
        if selector in seen:
            continue
        seen.add(selector)
        models.append({
            "provider": provider,
            "selector": selector,
            "name": name,
        })

    def provider_priority(provider):
        if provider == OPENAI_MODEL_PROVIDER:
            return 0
        if local_provider and provider == local_provider:
            return 1
        if provider == "ollama":
            return 2
        return 3

    models.sort(key=lambda model: (
        provider_priority(model["provider"]),
        model["provider"].lower(),
        model["name"].lower(),
    ))
    default_model = ""
    try:
        roles = run_omp_json(["config", "get", "modelRoles", "--json"])
        values = roles.get("value")
        if isinstance(values, dict) and isinstance(values.get("default"), str):
            default_model = values["default"]
    except RuntimeError:
        pass
    return {
        "models": models,
        "default_model": default_model,
        "local_server": local_server,
        "local_provider": local_provider,
        "local_error": local_error,
        "openai_connected": any(
            model["provider"] == OPENAI_MODEL_PROVIDER for model in models
        ),
    }


def split_paper_paragraphs(text):
    """Return nonempty blank-line-delimited blocks in source order."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [part.strip() for part in re.split(r"\n[ \t]*\n+", normalized) if part.strip()]


def _paper_heading_title(paragraph):
    """Return a normalized standalone section heading, or None for body text."""
    text = paragraph.strip()
    if not text or "\n" in text:
        return None
    match = MARKDOWN_HEADING_PATTERN.fullmatch(text)
    if match is not None:
        text = match.group(2)
    text = re.sub(r"[ \t]*\{#[^}]+\}[ \t]*$", "", text)
    text = text.strip().strip("*_").strip()
    text = SECTION_NUMBER_PATTERN.sub("", text)
    return text.rstrip(":").strip().casefold()


def omit_reference_sections(paragraphs):
    """Remove standalone bibliography sections while retaining later appendices."""
    kept = []
    omitted = 0
    in_references = False
    for paragraph in paragraphs:
        heading = _paper_heading_title(paragraph)
        if heading in REFERENCE_SECTION_TITLES:
            in_references = True
            omitted += 1
            continue
        if in_references and heading is not None and (
            heading in POST_REFERENCE_SECTION_TITLES
            or heading.startswith(POST_REFERENCE_SECTION_PREFIXES)
        ):
            in_references = False
        if in_references:
            omitted += 1
        else:
            kept.append(paragraph)
    return kept, omitted


def paper_system_prompt(task):
    return f"""{task.rstrip()}

{PAPER_LOOP_INSTRUCTION}

Mandatory harness transport protocol:
The XML wrapper below is required for every intermediate model response. The
harness removes it before writing the final narration, so the wrapper is not
part of the narration and does not violate the task's final-output rules. This
protocol overrides any conflicting response-format instruction in the task for
intermediate responses only.

Return exactly these two elements, in this order, without fences or commentary:
<NARRATION>
the complete TTS-adapted version of every current included source paragraph,
preserving their order and paragraph boundaries
</NARRATION>
<SUMMARY>
a compact summary of the current source batch, using at most two short sentences
per source paragraph
</SUMMARY>

NARRATION is appended to the final file and must remain complete for all included
source material. SUMMARY is internal compacted context; it must not shorten or
replace any narration or recreate an omitted bibliography.
Earlier source batches and narration are intentionally absent from later calls:
use their summaries only for continuity."""


def compact_paper_summary(summary):
    text = " ".join(summary.split())
    if len(text) <= PAPER_MAX_SUMMARY_CHARS:
        return text
    return text[:PAPER_MAX_SUMMARY_CHARS - 1].rstrip() + "…"


def paper_summary_context_limit(in_flight):
    return max(
        PAPER_MIN_SUMMARY_CONTEXT_CHARS,
        min(
            PAPER_MAX_SUMMARY_CONTEXT_CHARS,
            PAPER_TOTAL_SUMMARY_CONTEXT_CHARS // max(1, int(in_flight)),
        ),
    )


def paper_summary_context(summaries, max_chars):
    if not summaries:
        return "(none available when this batch was dispatched)", 0
    max_chars = max(256, int(max_chars))
    entries = [
        f"Paragraph{'s' if first != last else ''} "
        f"{first}{f'-{last}' if first != last else ''}: "
        f"{compact_paper_summary(summary)}"
        for first, last, summary in summaries
    ]
    complete = "\n".join(entries)
    if len(complete) <= max_chars:
        return complete, 0

    marker_reserve = 96
    first = entries[0][:max(1, max_chars - marker_reserve)].rstrip()
    recent = []
    used = len(first) + marker_reserve
    for entry in reversed(entries[1:]):
        cost = len(entry) + 1
        if used + cost > max_chars:
            break
        recent.append(entry)
        used += cost
    omitted = len(entries) - len(recent) - 1
    marker = (
        f"({omitted} older source-batch summar"
        f"{'y' if omitted == 1 else 'ies'} omitted to bound model context)"
    )
    return "\n".join((first, marker, *reversed(recent))), omitted


def paper_request(paragraphs, compacted_summaries, start, end, total, attempt=1):
    source = "\n\n".join(
        f'<SOURCE_PARAGRAPH number="{number}">\n{paragraph}\n</SOURCE_PARAGRAPH>'
        for number, paragraph in enumerate(paragraphs, start)
    )
    batch_label = (
        f"paragraph {start}" if start == end else f"paragraphs {start}-{end}"
    )
    retry = ""
    if attempt > 1:
        retry = f"""

Transport retry attempt {attempt} of {PAPER_RESPONSE_ATTEMPTS}:
The prior response could not be parsed. Generate the complete adapted included
source batch again, preserving every supplied paragraph and its boundaries, with one nonempty
NARRATION element followed by one nonempty SUMMARY element. Do not discuss the
retry or add text outside those elements."""
    return f"""Compacted summaries from earlier source batches completed before dispatch:
{compacted_summaries}
Some immediately preceding batches may still be processing and therefore absent
from this snapshot. Adapt the current source independently rather than inventing
missing material.

Current source {batch_label} of {total}:
{source}{retry}
"""


def parse_paper_response(response):
    text = response.strip()
    if text.startswith("```") and text.endswith("```"):
        first_break = text.find("\n")
        if first_break >= 0:
            text = text[first_break + 1:-3].strip()
    matches = list(PAPER_RESPONSE_PATTERN.finditer(text))
    if len(matches) != 1:
        raise ValueError(
            "model response must contain one nonempty NARRATION element "
            "followed by one nonempty SUMMARY element"
        )
    parts = tuple(part.strip() for part in matches[0].groups())
    if not all(parts):
        raise ValueError(
            "model response must contain one nonempty NARRATION element "
            "followed by one nonempty SUMMARY element"
        )
    return parts


def is_saved_voice(path):
    directory = Path(path).expanduser()
    return bool(path) and all((directory / name).is_file() for name in VOICE_FILES)


def unconfigured_tts_models():
    return {role: {"source": "missing"} for role in ROLE_LABELS}


def configured_tts_models(args, parser):
    """Build process-owned TTS backends from web-server startup arguments."""
    models = unconfigured_tts_models()
    for role in ROLE_LABELS:
        model = getattr(args, f"voice_{role}_model")
        server = getattr(args, f"voice_{role}_server")
        server_model = getattr(args, f"voice_{role}_server_model")
        model_flag = f"--voice-{role}-model"
        server_flag = f"--voice-{role}-server"
        server_model_flag = f"--voice-{role}-server-model"
        if server_model and not server:
            parser.error(f"{server_model_flag} requires {server_flag}")
        if server:
            server_model = server_model or SERVER_MODEL_DEFAULTS[role]
            if role == "design" and server_model in NO_INSTRUCTIONS:
                parser.error(
                    f"{server_model_flag} {server_model} ignores voice descriptions; "
                    "choose a model that honours instructions"
                )
            models[role] = {
                "source": "server",
                "server": server,
                "server_model": server_model,
            }
            continue
        if not model:
            models[role] = {"source": "missing"}
            continue
        path = Path(model).expanduser()
        if path.is_dir():
            model = str(path.resolve())
        elif not args.allow_model_downloads:
            parser.error(
                f"{model_flag} is not an existing directory: {path}; "
                "use --allow-model-downloads for a Hugging Face ID"
            )
        models[role] = {
            "source": "local",
            "model": model,
            "allow_downloads": args.allow_model_downloads,
        }
    ssh_workers = tuple(args.narration_ssh_worker)
    if (
        args.narration_ssh_model is not None
        and not ssh_workers
    ):
        parser.error(
            "--narration-ssh-model requires --narration-ssh-worker"
        )
    if len(set(ssh_workers)) != len(ssh_workers):
        parser.error("--narration-ssh-worker targets must be unique")
    if not args.narration_ssh_python.strip():
        parser.error("--narration-ssh-python must not be empty")
    if ssh_workers:
        clone_model = models["clone"]
        if clone_model["source"] != "local":
            parser.error(
                "--narration-ssh-worker requires --voice-clone-model"
            )
        clone_model.update({
            "ssh_workers": ssh_workers,
            "ssh_python": args.narration_ssh_python,
            "ssh_model": (
                args.narration_ssh_model or clone_model["model"]
            ),
            "ssh_device": args.narration_ssh_device,
        })
    return models


def model_summary(model, role):
    label = ROLE_LABELS[role]
    if model["source"] == "missing":
        return f"{label}: not configured"
    if model["source"] == "server":
        return f"{label}: server {model['server']} · {model['server_model']}"
    downloads = " (downloads allowed)" if model["allow_downloads"] else ""
    workers = len(model.get("ssh_workers", ()))
    remote = (
        f" + {workers} SSH worker{'s' if workers != 1 else ''}"
        if workers
        else ""
    )
    return f"{label}: {model['model']}{downloads}{remote}"


def public_configuration(tts_models, devices=None):
    """Describe the active speech backends for browsers without paths or hosts."""
    configuration = {}
    for role, model in tts_models.items():
        entry = {"source": model["source"]}
        if model["source"] == "server":
            entry["model"] = model["server_model"]
        elif model["source"] == "local":
            # Local directories are stored resolved; Hugging Face IDs are relative.
            path = Path(model["model"])
            entry["model"] = path.name if path.is_absolute() else model["model"]
            if role == "design":
                # Voice creation always takes the automatic device.
                entry["device"] = public_device_label(
                    resolve_device("auto", devices)
                )
        configuration[role] = entry
    return configuration


def model_problem(model, role):
    if model["source"] == "missing":
        return f"{ROLE_LABELS[role]} is not configured on this server."
    if model["source"] == "server":
        if role == "design" and model["server_model"] in NO_INSTRUCTIONS:
            return (
                f"{model['server_model']} ignores voice descriptions; "
                "the server must configure a model that honours instructions"
            )
        return None
    return None if model.get("model") else f"{ROLE_LABELS[role]} is not configured on this server."


def model_arguments(model, flag):
    arguments = [flag, model["model"]]
    if model["allow_downloads"]:
        arguments.append("--allow-downloads")
    return arguments


def shared_arguments(values):
    arguments = [
        "--device", values["device"],
        "--dtype", values["dtype"],
        "--attn-implementation", values["attn"],
        "--language", values["language"] or "Auto",
    ]
    if values["seed"]:
        arguments += ["--seed", values["seed"]]
    return arguments


def narrate_command(values):
    model = values["clone"]
    arguments = [
        sys.executable,
        "-u",
        str(SCRIPT),
        "narrate",
        "--input",
        values["input"],
        "--input-encoding",
        values["encoding"],
        "--output",
        values["output"],
        "--overwrite",
        "--chunk-max-chars",
        values["chunk_max_chars"],
        "--sentence-chunks",
    ]
    if values.get("resume_dir"):
        arguments += ["--resume-dir", values["resume_dir"]]
    if values["mp3_level"]:
        arguments += ["--mp3-compression-level", values["mp3_level"]]
    if model["source"] == "server":
        return arguments + [
            "--server",
            model["server"],
            "--server-voice",
            values["clone_voice"],
            "--server-model",
            model["server_model"],
        ]
    arguments += [
        "--voice-dir",
        values["voice_dir"],
        "--batch-size",
        values["batch_size"],
    ]
    workers = values.get("workers", ())
    for worker in workers:
        if (
            worker.get("kind") == "local"
            and worker.get("device") != values["device"]
        ):
            arguments += ["--worker-device", worker["device"]]
    ssh_workers = [
        worker for worker in workers if worker.get("kind") == "ssh"
    ]
    for worker in ssh_workers:
        arguments += ["--ssh-worker", worker["target"]]
    if ssh_workers:
        arguments += [
            "--ssh-python",
            model["ssh_python"],
            "--ssh-model-path",
            model["ssh_model"],
            "--ssh-device",
            model["ssh_device"],
        ]
    return (
        arguments
        + model_arguments(model, "--clone-model-path")
        + shared_arguments(values)
    )


def create_voice_command(values):
    model = values["design"]
    arguments = [
        sys.executable,
        "-u",
        str(SCRIPT),
        "create-voice",
        "--voice-dir",
        values["new_voice_dir"],
        "--overwrite",
        "--instruct",
        values["instruct"],
        "--wav-subtype",
        values["wav_subtype"],
        "--text",
        VOICE_REFERENCE_TEXT,
    ]
    if model["source"] == "server":
        return arguments + [
            "--server",
            model["server"],
            "--server-voice",
            values["design_voice"],
            "--server-model",
            model["server_model"],
        ]
    return arguments + model_arguments(model, "--model-path") + shared_arguments(values)


def seed_problem(values):
    if values["seed"]:
        try:
            if not 0 <= int(values["seed"]) <= 4294967295:
                raise ValueError
        except ValueError:
            return "Seed must be an integer from 0 to 4294967295."
    return None


def number_problem(text, cast, valid, message):
    try:
        if not valid(cast(text)):
            return message
    except ValueError:
        return message
    return None


def numeric_problem(values):
    """Validate numeric controls used by audiobook narration."""
    server = values["clone"]["source"] == "server"
    checks = [
        (
            "chunk_max_chars",
            int,
            lambda number: number > 0,
            "Max chars must be a positive integer.",
        ),
    ]
    if not server:
        checks.append((
            "batch_size",
            int,
            lambda number: number >= 0,
            "Batch size must be 0 or more.",
        ))
    for key, cast, valid, message in checks:
        problem = number_problem(values[key], cast, valid, message)
        if problem is not None:
            return problem
    if values["mp3_level"]:
        problem = number_problem(
            values["mp3_level"],
            float,
            lambda number: 0.0 <= number <= 1.0,
            "MP3 compression must be between 0 and 1.",
        )
        if problem is not None:
            return problem
    if server:
        return None
    return seed_problem(values)


def _document_problem(values):
    if values["source_url"]:
        try:
            normalize_paper_url(values["source_url"])
        except ValueError as exc:
            return str(exc)
        name = values["download_name"]
        if name and safe_asset_name(name) != name:
            return "Enter an optional filename without a slash."
        return None
    source = Path(values["input"] or ".")
    if not source.is_file():
        return "Choose a document or enter a direct document URL."
    suffix = source.suffix.lower()
    if suffix not in PAPER_SUFFIXES:
        return "Document must be PDF, text, or Markdown."
    if suffix != ".pdf":
        try:
            "".encode(values["encoding"])
        except LookupError:
            return f"Unknown text encoding: {values['encoding']}"
    return None


def _adaptation_problem(values):
    if not values["adapt"]:
        return None
    try:
        normalize_local_server(values["local_server"])
    except ValueError as exc:
        return str(exc)
    # A local model's requests go to the saved server, which must be its type.
    provider = values["model"].partition("/")[0]
    if (
        values["local_server"]
        and provider in LOCAL_MODEL_PROVIDERS
        and provider != values["local_provider"]
    ):
        return (
            "The adaptation model doesn't match your local server's type. "
            "Choose one of its models under Advanced."
        )
    checks = (
        (
            "in_flight",
            PAPER_MAX_IN_FLIGHT,
            f"In-flight requests must be an integer from 1 to {PAPER_MAX_IN_FLIGHT}.",
        ),
        (
            "paragraphs_per_worker",
            PAPER_MAX_PARAGRAPHS_PER_WORKER,
            "Paragraphs per worker must be an integer from 1 to "
            f"{PAPER_MAX_PARAGRAPHS_PER_WORKER}.",
        ),
    )
    for key, maximum, message in checks:
        problem = number_problem(
            values[key],
            int,
            lambda count, limit=maximum: 1 <= count <= limit,
            message,
        )
        if problem is not None:
            return problem
    if not PAPER_PROMPT_PATH.is_file():
        return f"Document adaptation instructions are missing: {PAPER_PROMPT_PATH}"
    if shutil.which("omp") is None:
        return "The omp command is not available to the web server."
    return None


def audiobook_problem(values):
    problem = model_problem(values["clone"], "clone")
    if problem is not None:
        return problem
    if values["clone"]["source"] == "server":
        if not values["clone_voice"]:
            return "Enter the narration voice ID."
    elif not is_saved_voice(values["voice_dir"]):
        return "Choose a voice."
    problem = _document_problem(values)
    if problem is not None:
        return problem
    problem = _adaptation_problem(values)
    if problem is not None:
        return problem
    return numeric_problem(values)


def create_voice_problem(values):
    problem = model_problem(values["design"], "design")
    if problem is not None:
        return problem
    name = values["voice_name"]
    if not name or safe_asset_name(name) != name:
        return "Enter a voice name without a slash."
    target = Path(values["new_voice_dir"])
    if os.path.lexists(target) and (target.is_symlink() or not target.is_dir()):
        return "An existing voice must be a directory."
    if not values["instruct"].strip():
        return "Describe the voice to create."
    if values["design"]["source"] == "server" and not values["design_voice"]:
        return "Enter the voice name the server should shape."
    return seed_problem(values)





# --- per-browser state cookies ------------------------------------------------


def _state_cookie_jar(header):
    jar = SimpleCookie()
    try:
        jar.load(header or "")
    except CookieError:
        return SimpleCookie()
    return jar


def _state_cookie_count(jar):
    try:
        count = int(jar[STATE_COOKIE_COUNT].value)
    except (KeyError, TypeError, ValueError):
        count = 0
    return count if 0 <= count <= STATE_COOKIE_MAX_CHUNKS else 0


def read_state_cookie(header):
    """Decode one browser's bounded, compressed state; malformed cookies reset."""
    jar = _state_cookie_jar(header)
    count = _state_cookie_count(jar)
    if count == 0:
        return {}
    try:
        encoded = "".join(
            jar[f"{STATE_COOKIE_PREFIX}_{index}"].value
            for index in range(count)
        )
        compressed = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        )
        inflater = zlib.decompressobj()
        payload = inflater.decompress(
            compressed, STATE_COOKIE_MAX_JSON_BYTES + 1
        )
        if (
            len(payload) > STATE_COOKIE_MAX_JSON_BYTES
            or inflater.unconsumed_tail
            or not inflater.eof
        ):
            return {}
        data = json.loads(payload.decode("utf-8"))
    except (KeyError, UnicodeError, ValueError, zlib.error):
        return {}
    return data if isinstance(data, dict) else {}


def state_cookie_headers(state, previous_header=""):
    """Return Set-Cookie headers for one normalized state, expiring stale chunks."""
    payload = json.dumps(
        state, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > STATE_COOKIE_MAX_JSON_BYTES:
        raise ValueError(
            "Settings are too large for browser cookies; shorten the voice description."
        )
    encoded = base64.urlsafe_b64encode(zlib.compress(payload, 9)).decode("ascii")
    chunks = [
        encoded[offset:offset + STATE_COOKIE_CHUNK_BYTES]
        for offset in range(0, len(encoded), STATE_COOKIE_CHUNK_BYTES)
    ]
    if len(chunks) > STATE_COOKIE_MAX_CHUNKS:
        raise ValueError(
            "Settings are too large for browser cookies; shorten the voice description."
        )

    attributes = (
        f"; Path=/; Max-Age={STATE_COOKIE_MAX_AGE}; HttpOnly; SameSite=Strict"
    )
    headers = [
        ("Set-Cookie", f"{STATE_COOKIE_COUNT}={len(chunks)}{attributes}")
    ]
    headers.extend(
        ("Set-Cookie", f"{STATE_COOKIE_PREFIX}_{index}={chunk}{attributes}")
        for index, chunk in enumerate(chunks)
    )
    previous_count = _state_cookie_count(_state_cookie_jar(previous_header))
    expired = "; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
    headers.extend(
        ("Set-Cookie", f"{STATE_COOKIE_PREFIX}_{index}={expired}")
        for index in range(len(chunks), previous_count)
    )
    return tuple(headers)


def section(state, name):
    value = state.get(name)
    return value if isinstance(value, dict) else {}


def stored_text(mapping, key, fallback=""):
    value = mapping.get(key)
    return value if isinstance(value, str) else fallback




def normalize(state):
    """Return the shared-library UI's canonical per-browser state."""
    runtime, voice, audiobook, player = (
        section(state, name)
        for name in ("runtime", "voice", "audiobook", "player")
    )
    tab = state.get("tab")
    if tab not in ("voice", "audiobook", "player"):
        tab = "audiobook"
    step = audiobook.get("step")
    if step not in ("book", "voice", "create"):
        step = "book"
    local_provider = stored_text(audiobook, "local_provider")
    return {
        "schema": STATE_SCHEMA_VERSION,
        "tab": tab,
        "runtime": {
            "dtype": stored_text(runtime, "dtype", "auto"),
            "attn": stored_text(runtime, "attn", "sdpa"),
            "language": stored_text(runtime, "language", "Auto"),
            "encoding": stored_text(runtime, "encoding", "utf-8-sig"),
            "seed": stored_text(runtime, "seed"),
        },
        "voice": {
            "server_voice": stored_text(voice, "server_voice"),
            "name": stored_text(voice, "name"),
            "instruct": stored_text(voice, "instruct"),
            "wav_subtype": stored_text(voice, "wav_subtype", "FLOAT"),
        },
        "audiobook": {
            "step": step,
            "server_voice": stored_text(audiobook, "server_voice"),
            "voice": stored_text(audiobook, "voice"),
            "document": stored_text(audiobook, "document"),
            "source_url": stored_text(audiobook, "source_url"),
            "download_name": stored_text(audiobook, "download_name"),
            "adapt": audiobook.get("adapt") is not False,
            "model": stored_text(audiobook, "model"),
            "local_server": stored_text(audiobook, "local_server"),
            "local_provider": (
                local_provider if local_provider in LOCAL_MODEL_PROVIDERS else ""
            ),
            "in_flight": stored_text(
                audiobook, "in_flight", str(PAPER_DEFAULT_IN_FLIGHT)
            ),
            "paragraphs_per_worker": stored_text(
                audiobook,
                "paragraphs_per_worker",
                str(PAPER_DEFAULT_PARAGRAPHS_PER_WORKER),
            ),
            "chunk_max_chars": stored_text(
                audiobook, "chunk_max_chars", "500"
            ),
            # Schema 3 saved its default batch size of 1 in every browser;
            # those browsers move to the current default once.
            "batch_size": (
                "2"
                if state.get("schema") == 3 and audiobook.get("batch_size") == "1"
                else stored_text(audiobook, "batch_size", "2")
            ),
            "mp3_level": stored_text(audiobook, "mp3_level", "0.5"),
        },
        # The book open on the Listen page survives a refresh.
        "player": {"book": stored_text(player, "book")},
    }


def _optional_asset(directory, name):
    try:
        return str(resolve_asset(directory, name)) if name else ""
    except ValueError:
        return ""


def values_of(state, tts_models, storage):
    """Flatten canonical state and inject server-owned models and storage paths."""
    voice = state["voice"]
    audiobook = state["audiobook"]
    runtime = state["runtime"]
    voice_name = voice["name"].strip()
    document_name = audiobook["document"].strip()
    clone_voice = audiobook["server_voice"].strip()
    narrator_name = (
        clone_voice
        if tts_models["clone"]["source"] == "server"
        else audiobook["voice"].strip()
    )
    output_name = (
        narration_output_name(document_name, narrator_name)
        if document_name and narrator_name
        else ""
    )
    return {
        "device": resolve_device("auto"),
        "requested_device": "auto",
        "dtype": runtime["dtype"],
        "attn": runtime["attn"],
        "language": runtime["language"].strip(),
        "encoding": runtime["encoding"].strip() or "utf-8-sig",
        "seed": runtime["seed"].strip(),
        "design": tts_models["design"],
        "clone": tts_models["clone"],
        "design_voice": voice["server_voice"].strip(),
        "clone_voice": clone_voice,
        "voice_name": voice_name,
        "new_voice_dir": _optional_asset(storage.voices, voice_name),
        "instruct": voice["instruct"],
        "wav_subtype": voice["wav_subtype"],
        "selected_voice": audiobook["voice"].strip(),
        "voice_dir": _optional_asset(storage.voices, audiobook["voice"].strip()),
        "document_name": document_name,
        "input": _optional_asset(storage.documents, document_name),
        "source_url": audiobook["source_url"].strip(),
        "download_name": audiobook["download_name"].strip(),
        "adapt": audiobook["adapt"],
        "model": audiobook["model"].strip(),
        "local_server": audiobook["local_server"].strip(),
        "local_provider": audiobook["local_provider"],
        "in_flight": audiobook["in_flight"].strip(),
        "paragraphs_per_worker": audiobook["paragraphs_per_worker"].strip(),
        "chunk_max_chars": audiobook["chunk_max_chars"].strip(),
        "batch_size": audiobook["batch_size"].strip(),
        "mp3_level": audiobook["mp3_level"].strip(),
        "output_name": output_name,
        "output": str(storage.audiobooks / output_name) if output_name else "",
        "prepared_name": (
            prepared_document_name(document_name) if document_name else ""
        ),
    }


def audiobook_versions(values):
    input_version = file_version(values["input"])
    if values["clone"]["source"] == "server":
        voice_version = remote_voice_version(
            values["clone"], values["clone_voice"]
        )
    else:
        voice_version = saved_voice_version(values["voice_dir"])
    return input_version, voice_version


def audiobook_job_id(document_version, voice_version):
    """Return the stable identity of one document-version/voice-version pair."""
    payload = json.dumps(
        [document_version, voice_version],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def derived(state, tts_models, storage):
    values = values_of(state, tts_models, storage)
    tab = state["tab"]
    if tab == "voice":
        problem = create_voice_problem(values)
    elif tab == "audiobook":
        problem = audiobook_problem(values)
    else:
        problem = None
    target = values["new_voice_dir"]
    return {
        "problem": problem,
        "output_name": values["output_name"],
        "prepared_name": values["prepared_name"],
        "voice_dir_ok": is_saved_voice(values["voice_dir"]),
        "voice_exists": bool(target) and os.path.lexists(Path(target)),
        "design_server": values["design"]["source"] == "server",
        "clone_server": values["clone"]["source"] == "server",
        # Facts hold for the tab they were derived on; the page checks this.
        "tab": tab,
    }




# --- runs ---------------------------------------------------------------------


class Run:
    """One subprocess, its transcript, and the subscribers watching it."""

    def __init__(
        self, command, kind, predicted, temporary_dir=None, on_success=None
    ):
        self.command = command
        self.kind = kind
        self.predicted = predicted
        self.temporary_dir = temporary_dir
        self.on_success = on_success
        self.artifact = None
        self.code = None
        self.history = []
        self.subscribers = []
        self.lock = threading.Lock()
        self.process = None
        self.finished = threading.Event()
        self.started_at = time.monotonic()
        self.current_phase = None
        self.phase_label = None
        self.phase_started_at = self.started_at

    def elapsed(self):
        return max(0.0, time.monotonic() - self.started_at)

    def phase_elapsed(self):
        return max(0.0, time.monotonic() - self.phase_started_at)

    def set_phase(self, phase, label):
        self.current_phase = phase
        self.phase_label = label
        self.phase_started_at = time.monotonic()
        self.publish("phase", {"phase": phase, "label": label})

    def publish(self, event, data):
        if event == "progress" and isinstance(data, dict):
            data = {
                **data,
                "elapsed": self.elapsed(),
                "phase": self.current_phase,
                "phase_elapsed": self.phase_elapsed(),
            }
        item = (event, data)
        with self.lock:
            self.history.append(item)
            event_id = len(self.history)
            subscribers = list(self.subscribers)
        for sink in subscribers:
            sink.put((event_id, event, data))

    def subscribe(self, after=0):
        sink = queue.Queue()
        with self.lock:
            if after > len(self.history):
                after = 0
            for event_id, (event, data) in enumerate(self.history, start=1):
                if event_id > after:
                    sink.put((event_id, event, data))
            self.subscribers.append(sink)
        return sink

    def unsubscribe(self, sink):
        with self.lock:
            if sink in self.subscribers:
                self.subscribers.remove(sink)

    def snapshot(self):
        return {
            "active": not self.finished.is_set(),
            "kind": self.kind,
            "code": self.code,
            "artifact": self.artifact,
            "name": Path(self.artifact).name if self.artifact else None,
            "phase": self.current_phase,
            "phase_label": self.phase_label,
        }

    def start(self):
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self):
        if self.current_phase is None:
            label = {
                "voice": "Voice creation",
                "narrate": "Narration",
            }.get(self.kind, self.kind.title())
            self.set_phase(self.kind, label)
            if self.kind == "voice":
                self.publish("progress", {"done": 0, "total": 1})
        try:
            self.process = subprocess.Popen(
                self.command, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace",
            )
        except OSError as exc:
            self.publish("log", f"Cannot start {self.command[0]}: {exc}\n")
            self.close(1)
            return
        for line in self.process.stdout:
            self.publish("log", line)
            self.inspect(line.strip())
        self.process.stdout.close()
        self.close(self.process.wait())

    def inspect(self, line):
        progress = CHECKPOINT_LINE.match(line)
        if self.kind != "audiobook" and progress is None:
            progress = BATCH_LINE.match(line) or CHUNK_LINE.match(line)
        if progress is not None:
            done, total = (int(value) for value in progress.groups())
            self.publish("progress", {"done": done, "total": total})
        for pattern in (WROTE_LINE, SAVED_LINE):
            match = pattern.match(line)
            if match is not None:
                self.artifact = match.group(1)

    def close(self, code):
        if code == 0 and self.kind == "voice":
            self.publish("progress", {"done": 1, "total": 1})
        self.code = code
        if code == 0 and self.artifact is None:
            self.artifact = self.predicted
        if code != 0:
            self.artifact = None
            if self.temporary_dir is not None:
                shutil.rmtree(self.temporary_dir, ignore_errors=True)
        elif self.on_success is not None:
            try:
                self.on_success(self.artifact)
            except Exception as exc:
                self.publish(
                    "log", f"Could not prepare completed output for its next step: {exc}\n"
                )
        self.publish("done", {
            "code": code,
            "kind": self.kind,
            "artifact": self.artifact,
            "name": Path(self.artifact).name if self.artifact else None,
        })
        self.finished.set()

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()


class JobQueue:
    """Allocate compatible worker groups to queued audiobook jobs."""

    FINISHED_LIMIT = 32
    ACTIVE_STATUSES = frozenset(("preparing", "queued", "running"))

    def __init__(self, consumers=None):
        if consumers is None:
            consumers = ({
                "id": "default",
                "kind": "generic",
                "device": None,
                "label": "Worker",
                "worker": None,
            },)
        normalized = []
        for index, consumer in enumerate(consumers, 1):
            device = consumer.get("device")
            kind = consumer.get("kind") or (
                "local" if device is not None else "generic"
            )
            worker = consumer.get("worker")
            if worker is None and kind == "local":
                worker = {
                    "kind": "local",
                    "device": device,
                    "label": str(consumer["label"]),
                }
            public = consumer.get("public") or {}
            normalized.append({
                "id": str(consumer["id"]),
                "kind": str(kind),
                "device": device,
                "label": str(consumer["label"]),
                # Browsers see only this; the operator label may name a host.
                "public": {
                    "label": str(public.get("label") or f"Worker {index}"),
                    "detail": str(public.get("detail") or ""),
                },
                "worker": dict(worker) if worker is not None else None,
            })
        normalized = tuple(normalized)
        if not normalized or len({item["id"] for item in normalized}) != len(
            normalized
        ):
            raise ValueError("job consumers must have unique identities")
        self.lock = threading.RLock()
        self.consumers = normalized
        self.active = {}
        self.exclusive = None
        self.pending = []
        self.records = {}
        self.sequence = 0

    @staticmethod
    def _public(record, position=None):
        # Browsers learn a job's workers from the consumer snapshot's job IDs.
        result = {
            "id": record["id"],
            "kind": record["kind"],
            "status": record["status"],
            "document": record.get("document"),
            "voice": record.get("voice"),
            "output_name": record.get("output_name"),
            "document_version": record.get("document_version"),
            "voice_version": record.get("voice_version"),
        }
        if position is not None:
            result["position"] = position
        return result

    def _active_records_locked(self):
        records = []
        seen = set()
        for consumer in self.consumers:
            record = self.active.get(consumer["id"])
            if record is None or id(record) in seen:
                continue
            seen.add(id(record))
            records.append(record)
        return records

    def _position_locked(self, record):
        if any(active is record for active in self.active.values()):
            return 0
        if record in self.pending:
            return self.pending.index(record) + 1
        if record["status"] == "preparing":
            return len(self.pending) + 1
        return None

    def _eligible_consumers(self, requested_device):
        requested = (requested_device or "auto").strip()
        if requested == "auto":
            return tuple(consumer["id"] for consumer in self.consumers)
        if requested.startswith("cuda:"):
            return tuple(
                consumer["id"]
                for consumer in self.consumers
                if consumer["kind"] == "local"
                and consumer["device"] == requested
            )
        local = tuple(
            consumer["id"]
            for consumer in self.consumers
            if consumer["kind"] in ("local", "generic", "server")
        )
        return local

    @staticmethod
    def _assignment_workers(record, consumers):
        requested = record["requested_device"]
        workers = []
        for consumer in consumers:
            worker = consumer["worker"]
            if worker is None:
                continue
            assigned = dict(worker)
            if requested != "auto" and assigned.get("kind") == "local":
                assigned["device"] = (
                    record["resolved_device"] or requested
                )
            workers.append(assigned)
        return workers

    def _dispatch_locked(self):
        if self.exclusive is not None:
            return []
        launch = []
        has_local_consumers = any(
            consumer["kind"] == "local" for consumer in self.consumers
        )
        for record in tuple(self.pending):
            if record["status"] != "queued":
                continue
            eligible = [
                consumer
                for consumer in self.consumers
                if consumer["id"] not in self.active
                and consumer["id"] in record["eligible_consumers"]
            ]
            if not eligible:
                continue
            requested = record["requested_device"]
            if (
                requested == "auto"
                and has_local_consumers
                and not any(
                    consumer["kind"] == "local"
                    for consumer in eligible
                )
            ):
                continue
            selected = eligible if requested == "auto" else eligible[:1]
            workers = self._assignment_workers(record, selected)
            self.pending.remove(record)
            record["status"] = "running"
            record["consumer_ids"] = tuple(
                consumer["id"] for consumer in selected
            )
            record["consumer"] = (
                selected[0]["label"]
                if len(selected) == 1
                else f"{len(selected)} workers"
            )
            record["workers"] = workers
            record["device"] = next(
                (
                    worker.get("device")
                    for worker in workers
                    if worker.get("kind") == "local"
                ),
                None,
            )
            if workers:
                record["run"].assign_workers(workers)
            for consumer in selected:
                self.active[consumer["id"]] = record
            launch.append(record)
        return launch

    def describe(self, record):
        with self.lock:
            return self._public(record, self._position_locked(record))

    def existing(self, job_id):
        with self.lock:
            record = self.records.get(job_id)
            if record is None or record["status"] not in self.ACTIVE_STATUSES:
                return None
            return self._public(record, self._position_locked(record))

    def reserve_audiobook(
        self,
        document_version,
        voice_version,
        document,
        voice,
        output_name,
        requested_device="auto",
        resolved_device=None,
    ):
        job_id = audiobook_job_id(document_version, voice_version)
        with self.lock:
            existing = self.records.get(job_id)
            if (
                existing is not None
                and existing["status"] in self.ACTIVE_STATUSES
            ):
                return existing, False
            eligible = self._eligible_consumers(requested_device)
            if not eligible:
                raise ValueError(
                    f"no consumer is available for {requested_device}"
                )
            self.sequence += 1
            record = {
                "id": job_id,
                "kind": "audiobook",
                "status": "preparing",
                "sequence": self.sequence,
                "run": None,
                "document": document,
                "voice": voice,
                "output_name": output_name,
                "document_version": document_version,
                "voice_version": voice_version,
                "requested_device": (requested_device or "auto").strip(),
                "resolved_device": resolved_device,
                "eligible_consumers": eligible,
                "consumer_ids": (),
                "consumer": None,
                "workers": [],
                "device": None,
            }
            self.records[job_id] = record
            return record, True

    def discard(self, record):
        with self.lock:
            if (
                record["id"]
                and self.records.get(record["id"]) is record
                and record["status"] == "preparing"
            ):
                self.records.pop(record["id"], None)

    def commit(self, record, run):
        with self.lock:
            if record["status"] == "canceled":
                return self._public(record)
            record["run"] = run
            record["status"] = "queued"
            self.pending.append(record)
            launch = self._dispatch_locked()
            result = self._public(record, self._position_locked(record))
        for item in launch:
            self._launch(item)
        return result

    def start_voice(self, run):
        with self.lock:
            if self.exclusive is not None or self.active or self.pending or any(
                record["status"] == "preparing"
                for record in self.records.values()
            ):
                return False
            self.sequence += 1
            record = {
                "id": None,
                "kind": "voice",
                "status": "running",
                "sequence": self.sequence,
                "run": run,
                "consumer": "Exclusive",
                "workers": [],
                "device": None,
            }
            self.exclusive = record
        self._launch(record)
        return True

    def creating_voice(self, voice_dir):
        """Return whether the running voice creation writes this voice folder."""
        with self.lock:
            record = self.exclusive
        return record is not None and Path(record["run"].predicted) == Path(voice_dir)

    def _launch(self, record):
        threading.Thread(
            target=self._wait_for_finish,
            args=(record,),
            daemon=True,
        ).start()
        try:
            record["run"].start()
        except Exception as exc:
            record["run"].publish("log", f"Cannot start job: {exc}\n")
            record["run"].close(1)

    def _trim_finished_locked(self):
        finished = sorted(
            (
                item for item in self.records.values()
                if item["status"] not in self.ACTIVE_STATUSES
            ),
            key=lambda item: item["sequence"],
            reverse=True,
        )
        for old in finished[self.FINISHED_LIMIT:]:
            if self.records.get(old["id"]) is old:
                self.records.pop(old["id"], None)

    def _wait_for_finish(self, record):
        record["run"].finished.wait()
        with self.lock:
            code = record["run"].code
            record["status"] = (
                "done" if code == 0
                else "stopped" if code == 130
                else "failed"
            )
            if self.exclusive is record:
                self.exclusive = None
            else:
                for consumer_id in tuple(self.active):
                    if self.active.get(consumer_id) is record:
                        self.active.pop(consumer_id, None)
            launch = self._dispatch_locked()
            self._trim_finished_locked()
        for item in launch:
            self._launch(item)

    def current_runs(self):
        with self.lock:
            if self.exclusive is not None:
                return [self.exclusive["run"]]
            return [
                record["run"] for record in self._active_records_locked()
            ]

    def current_run(self):
        runs = self.current_runs()
        return runs[0] if runs else None

    def active_snapshots(self):
        with self.lock:
            records = (
                [self.exclusive]
                if self.exclusive is not None
                else self._active_records_locked()
            )
        result = []
        for record in records:
            snapshot = record["run"].snapshot()
            if record["id"] is not None:
                snapshot["job_id"] = record["id"]
            result.append(snapshot)
        return result

    def consumers_snapshot(self):
        with self.lock:
            exclusive = self.exclusive
            return [
                {
                    "id": consumer["id"],
                    "kind": consumer["kind"],
                    "device": consumer["device"],
                    "label": consumer["label"],
                    "status": (
                        "reserved"
                        if exclusive is not None
                        else "running"
                        if consumer["id"] in self.active
                        else "idle"
                    ),
                    "job_id": (
                        self.active[consumer["id"]]["id"]
                        if consumer["id"] in self.active
                        else None
                    ),
                }
                for consumer in self.consumers
            ]

    def public_consumers_snapshot(self):
        """Return each worker's device and state without hosts or paths."""
        return [
            {
                **consumer["public"],
                "status": snapshot["status"],
                "job_id": snapshot["job_id"],
            }
            for consumer, snapshot in zip(
                self.consumers, self.consumers_snapshot(), strict=True
            )
        ]

    def snapshot(self):
        with self.lock:
            result = [
                self._public(record, 0)
                for record in self._active_records_locked()
                if record["id"] is not None
            ]
            result.extend(
                self._public(record, index)
                for index, record in enumerate(self.pending, start=1)
                if record["status"] != "canceled"
            )
            preparing = sorted(
                (
                    record for record in self.records.values()
                    if record["status"] == "preparing"
                ),
                key=lambda record: record["sequence"],
            )
            result.extend(
                self._public(record, len(self.pending) + index)
                for index, record in enumerate(preparing, start=1)
            )
            return result

    def run_for(self, job_id=""):
        with self.lock:
            if job_id:
                record = self.records.get(job_id)
                return record["run"] if record is not None else None
            if self.exclusive is not None:
                return self.exclusive["run"]
            records = self._active_records_locked()
            return records[0]["run"] if records else None

    def cancel(self, job_id):
        run = None
        with self.lock:
            record = self.records.get(job_id)
            if record is None or record["status"] not in self.ACTIVE_STATUSES:
                return None
            if any(active is record for active in self.active.values()):
                run = record["run"]
            else:
                if record in self.pending:
                    self.pending.remove(record)
                record["status"] = "canceled"
            result = self._public(record, self._position_locked(record))
        if run is not None:
            run.stop()
        return result

    def stop_all(self):
        with self.lock:
            runs = (
                [self.exclusive["run"]]
                if self.exclusive is not None
                else [
                    record["run"]
                    for record in self._active_records_locked()
                ]
            )
        for run in runs:
            run.stop()
        return len(runs)

    def shutdown(self):
        with self.lock:
            pending = list(self.pending)
            self.pending.clear()
            for record in pending:
                record["status"] = "canceled"
        self.stop_all()


class PaperRun(Run):
    """Download a URL or read a local document, then process it with compacted history."""

    def __init__(
        self,
        input_path,
        output_path,
        encoding,
        model="",
        local_server="",
        in_flight=PAPER_DEFAULT_IN_FLIGHT,
        paragraphs_per_worker=PAPER_DEFAULT_PARAGRAPHS_PER_WORKER,
        summary_context_chars=None,
        executable=None,
        prompt_path=PAPER_PROMPT_PATH,
        on_success=None,
        scratch_path=None,
        adapt=True,
    ):
        super().__init__(
            [], "paper", str(output_path), on_success=on_success
        )
        self.input_source = str(input_path)
        self.input_url = normalize_paper_url(self.input_source)
        self.input_path = None if self.input_url else Path(self.input_source)
        self.output_path = Path(output_path)
        self.encoding = encoding
        self.model = model
        self.local_server = normalize_local_server(local_server)
        self.in_flight = int(in_flight)
        self.paragraphs_per_worker = int(paragraphs_per_worker)
        self.summary_context_chars = (
            paper_summary_context_limit(self.in_flight)
            if summary_context_chars is None
            else max(256, int(summary_context_chars))
        )
        self.processes = set()
        self.process_lock = threading.Lock()
        self.executable = executable or shutil.which("omp") or "omp"
        self.prompt_path = Path(prompt_path)
        self.stop_requested = threading.Event()
        self.scratch_path = Path(scratch_path) if scratch_path is not None else None
        self.adapt = bool(adapt)

    def omp_command(self, request_path, system_prompt, attachments=()):
        command = [
            self.executable,
            "-p",
            "--no-session",
            "--no-tools",
            "--no-skills",
            "--no-rules",
            "--no-extensions",
            "--no-title",
            "--hide-thinking",
            "--system-prompt",
            system_prompt,
        ]
        if self.model:
            command += ["--model", self.model]
        return (
            command
            + [f"@{request_path}"]
            + [f"@{path}" for path in attachments]
        )

    def child_output(self, command, label, environment=None):
        try:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            raise RuntimeError(f"cannot start {label}: {exc}") from exc
        with self.process_lock:
            self.processes.add(process)
            should_stop = self.stop_requested.is_set()
        if should_stop:
            process.terminate()
        try:
            stdout, stderr = process.communicate()
        finally:
            with self.process_lock:
                self.processes.discard(process)
        if self.stop_requested.is_set():
            raise InterruptedError("document processing stopped")
        if process.returncode != 0:
            details = (stderr or stdout).strip()
            if len(details) > 4000:
                details = details[-4000:]
            raise RuntimeError(
                f"{label} exited with {process.returncode}"
                + (f": {details}" if details else "")
            )
        return stdout

    def terminate_children(self):
        with self.process_lock:
            processes = tuple(self.processes)
        for process in processes:
            if process.poll() is None:
                process.terminate()

    def download_source(self, scratch):
        self.publish("log", f"Downloading document from {self.input_url}…\n")
        request = urllib.request.Request(
            self.input_url,
            headers={
                "Accept": "application/pdf,text/markdown,text/plain;q=0.9,*/*;q=0.1",
                "User-Agent": "audiobook-tts/1",
            },
        )
        try:
            response = urllib.request.urlopen(
                request, timeout=PAPER_DOWNLOAD_TIMEOUT
            )
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"document URL returned HTTP {exc.code} {exc.reason}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"cannot download document URL: {exc}") from exc

        temporary = scratch / "downloaded-document"
        with response:
            final_url = normalize_paper_url(response.geturl())
            content_type = response.headers.get_content_type().lower()
            if content_type in ("text/html", "application/xhtml+xml"):
                raise ValueError(
                    "Document URL returned HTML; use a direct PDF, text, or Markdown URL."
                )
            try:
                declared_size = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                declared_size = 0
            if declared_size > BOOK_UPLOAD_LIMIT:
                raise ValueError("Document URL exceeds the 64 MiB download limit.")
            size = 0
            with temporary.open("xb") as output:
                while True:
                    if self.stop_requested.is_set():
                        raise InterruptedError("document processing stopped")
                    chunk = response.read(min(1024 * 1024, BOOK_UPLOAD_LIMIT + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > BOOK_UPLOAD_LIMIT:
                        raise ValueError("Document URL exceeds the 64 MiB download limit.")
                    output.write(chunk)
        if size == 0:
            raise ValueError("Document URL returned an empty file.")

        suffix = Path(
            urllib.parse.unquote(urllib.parse.urlsplit(final_url).path)
        ).suffix.lower()
        content_suffix = {
            "application/pdf": ".pdf",
            "text/markdown": ".md",
            "text/plain": ".txt",
        }.get(content_type)
        if suffix not in PAPER_SUFFIXES:
            suffix = content_suffix or ""
        if not suffix:
            with temporary.open("rb") as downloaded:
                if downloaded.read(5) == b"%PDF-":
                    suffix = ".pdf"
        if suffix not in PAPER_SUFFIXES:
            raise ValueError(
                "Document URL must return a PDF, text, or Markdown document."
            )
        source_path = temporary.with_suffix(suffix)
        temporary.replace(source_path)
        self.publish("log", f"Downloaded {size} bytes.\n")
        return source_path

    def convert_pdf(self, scratch, input_path):
        markdown_path = scratch / "document.md"
        image_path = scratch / "images"
        page_path = scratch / "pdf-pages"
        image_path.mkdir(mode=0o750, parents=True, exist_ok=True)
        page_path.mkdir(mode=0o750, parents=True, exist_ok=True)
        try:
            page_count = int(self.child_output(
                [sys.executable, "-c", _PDF_PAGE_COUNT, str(input_path)],
                "PDF page counter",
            ).strip())
        except ValueError as exc:
            raise RuntimeError("PDF page counter returned an invalid count") from exc
        if page_count <= 0:
            raise ValueError("PDF contains no pages")
        for page in range(page_count):
            output = page_path / f"{page + 1:06d}.md"
            if output.is_file():
                self.publish(
                    "log",
                    f"Reusing extracted PDF page {page + 1}/{page_count}.\n",
                )
            else:
                report = self.child_output(
                    [
                        sys.executable,
                        "-c",
                        _PDF_CONVERTER,
                        str(input_path),
                        str(output),
                        str(image_path),
                        str(page),
                    ],
                    f"PDF page {page + 1} converter",
                ).strip()
                if report:
                    self.publish("log", report + "\n")
            self.publish(
                "progress",
                {"done": page + 1, "total": page_count, "unit": "page"},
            )
        temporary = markdown_path.with_name(f".{markdown_path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as combined:
            for page in range(page_count):
                if page:
                    combined.write("\n\n")
                combined.write(
                    (page_path / f"{page + 1:06d}.md").read_text(
                        encoding="utf-8"
                    ).strip()
                )
        temporary.replace(markdown_path)
        images = tuple(
            path for path in sorted(image_path.iterdir()) if path.is_file()
        )
        return markdown_path, images

    def model_response(self, request_path, system_prompt, attachments=()):
        environment = None
        provider = self.model.partition("/")[0]
        if self.local_server and provider in LOCAL_MODEL_PROVIDERS:
            environment = paper_omp_environment(self.local_server, provider)
        return self.child_output(
            self.omp_command(request_path, system_prompt, attachments),
            "omp",
            environment,
        )

    def paragraph_batch_response(
        self,
        scratch,
        paragraphs,
        summary_context,
        start,
        end,
        total,
        system_prompt,
        image_paths,
    ):
        if self.stop_requested.is_set():
            raise InterruptedError("document processing stopped")
        request_path = scratch / f"paragraphs-{start}-{end}.txt"
        source = "\n\n".join(paragraphs)
        attachments = tuple(
            path for path in image_paths if str(path) in source
        )
        figure_note = (
            f" with {len(attachments)} figure attachment"
            f"{'s' if len(attachments) != 1 else ''}"
            if attachments else ""
        )
        batch_label = (
            f"paragraph {start}" if start == end
            else f"paragraphs {start}-{end}"
        )
        self.publish(
            "log",
            f"Processing {batch_label}/{total}{figure_note}…\n",
        )
        for attempt in range(1, PAPER_RESPONSE_ATTEMPTS + 1):
            request_path.write_text(
                paper_request(
                    paragraphs,
                    summary_context,
                    start,
                    end,
                    total,
                    attempt,
                ),
                encoding="utf-8",
            )
            response = self.model_response(
                request_path, system_prompt, attachments
            )
            try:
                narration, summary = parse_paper_response(response)
                narration = _without_invisible_paragraphs(narration)
                if not narration:
                    raise ValueError("model narration contains no readable text")
                return narration, summary
            except ValueError:
                if attempt == PAPER_RESPONSE_ATTEMPTS:
                    preview = " ".join(response.split())
                    if len(preview) > 240:
                        preview = preview[:237] + "..."
                    detail = (
                        f"; last response began {preview!r}"
                        if preview else "; last response was empty"
                    )
                    raise ValueError(
                        f"{batch_label} returned malformed model transport "
                        f"after {PAPER_RESPONSE_ATTEMPTS} attempts{detail}"
                    ) from None
                self.publish(
                    "log",
                    f"{batch_label.capitalize()}/{total} returned malformed "
                    "transport; retrying model response "
                    f"({attempt + 1}/{PAPER_RESPONSE_ATTEMPTS})…\n",
                )
        raise AssertionError("unreachable document response loop")

    def process_paragraphs(self, scratch, paragraphs, image_paths, system_prompt):
        total = len(paragraphs)
        checkpoint_dir = scratch / "paragraph-checkpoints"
        checkpoint_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        batches = [
            (
                start,
                min(total, start + self.paragraphs_per_worker - 1),
            )
            for start in range(1, total + 1, self.paragraphs_per_worker)
        ]
        summaries = []
        results = {}
        completed_count = 0
        for start, end in batches:
            checkpoint = read_json_file(
                checkpoint_dir / f"{start:06d}-{end:06d}.json"
            )
            if not checkpoint or checkpoint.get("end") != end:
                continue
            narration = checkpoint.get("narration")
            summary = checkpoint.get("summary")
            if not isinstance(narration, str) or not narration.strip():
                continue
            if not isinstance(summary, str) or not summary.strip():
                continue
            results[start] = (end, narration.strip(), summary.strip())
            completed_count += end - start + 1

        futures = {}
        pending_starts = iter([
            start for start, _ in batches if start not in results
        ])
        next_commit = 1
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.in_flight,
            thread_name_prefix="document",
        )

        def submit(start):
            end = min(total, start + self.paragraphs_per_worker - 1)
            summary_context, _ = paper_summary_context(
                summaries, self.summary_context_chars
            )
            future = executor.submit(
                self.paragraph_batch_response,
                scratch,
                tuple(paragraphs[start - 1:end]),
                summary_context,
                start,
                end,
                total,
                system_prompt,
                image_paths,
            )
            futures[future] = (start, end)

        def fill_workers():
            while len(futures) < self.in_flight:
                try:
                    start = next(pending_starts)
                except StopIteration:
                    return
                submit(start)

        self.output_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        try:
            with self.output_path.open("w", encoding="utf-8", newline="\n") as output:
                def commit_ready():
                    nonlocal next_commit
                    while next_commit in results:
                        end, narration, summary = results.pop(next_commit)
                        if next_commit > 1:
                            output.write("\n\n")
                        output.write(narration)
                        output.flush()
                        os.fsync(output.fileno())
                        summaries.append((
                            next_commit,
                            end,
                            compact_paper_summary(summary),
                        ))
                        self.publish(
                            "progress",
                            {
                                "done": end,
                                "total": total,
                                "unit": "paragraph",
                            },
                        )
                        next_commit = end + 1

                commit_ready()
                fill_workers()
                while futures:
                    if self.stop_requested.is_set():
                        raise InterruptedError("document processing stopped")
                    completed, _ = concurrent.futures.wait(
                        tuple(futures),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in sorted(
                        completed, key=lambda item: futures[item][0]
                    ):
                        start, end = futures.pop(future)
                        narration, summary = future.result()
                        write_json_atomic(
                            checkpoint_dir / f"{start:06d}-{end:06d}.json",
                            {
                                "end": end,
                                "narration": narration,
                                "summary": summary,
                            },
                        )
                        results[start] = (end, narration, summary)
                        completed_count += end - start + 1
                    commit_ready()
                    committed = next_commit - 1
                    self.publish("activity", {
                        "completed": completed_count,
                        "committed": committed,
                        "total": total,
                        "waiting_for": (
                            next_commit if completed_count > committed else None
                        ),
                    })
                    fill_workers()
        except BaseException:
            for future in futures:
                future.cancel()
            self.terminate_children()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def prepare_document(self, scratch):
        scratch.mkdir(mode=0o750, parents=True, exist_ok=True)
        input_path = (
            self.download_source(scratch)
            if self.input_url
            else self.input_path
        )
        identity = {
            "schema": 2,
            "input_version": file_version(input_path),
            "adapt": self.adapt,
            "model": self.model,
            "local_server": self.local_server,
            "in_flight": self.in_flight,
            "paragraphs_per_worker": self.paragraphs_per_worker,
            "prompt_version": (
                file_version(self.prompt_path) if self.adapt else None
            ),
        }
        manifest_path = scratch / "extraction.json"
        manifest = read_json_file(manifest_path)
        complete = (
            manifest == {**identity, "complete": True}
            and self.output_path.is_file()
        )
        if complete:
            self.publish(
                "log",
                f"Reusing completed extraction for {input_path.name}.\n",
            )
            self.publish(
                "progress",
                {"done": 1, "total": 1, "unit": "document"},
            )
            return self.output_path
        if manifest is None or any(
            manifest.get(key) != value for key, value in identity.items()
        ):
            for directory_name in (
                "images",
                "pdf-pages",
                "paragraph-checkpoints",
            ):
                shutil.rmtree(scratch / directory_name, ignore_errors=True)
            for file_name in ("document.md",):
                (scratch / file_name).unlink(missing_ok=True)
            self.output_path.unlink(missing_ok=True)
        write_json_atomic(manifest_path, {**identity, "complete": False})

        image_paths = ()
        if input_path.suffix.lower() == ".pdf":
            self.publish("log", "Extracting PDF pages to Markdown…\n")
            source_path, image_paths = self.convert_pdf(scratch, input_path)
            source = source_path.read_text(encoding="utf-8")
        else:
            source = input_path.read_text(encoding=self.encoding)
        paragraphs = [
            paragraph
            for paragraph in split_paper_paragraphs(source)
            if _reader_source_is_visible(paragraph)
        ]
        paragraphs, omitted_references = omit_reference_sections(paragraphs)
        if omitted_references:
            self.publish(
                "log",
                f"Omitted {omitted_references} paragraph"
                f"{'s' if omitted_references != 1 else ''} from standalone "
                "reference sections.\n",
            )
        if not paragraphs:
            raise ValueError(
                "document contains no readable body paragraphs after "
                "removing reference sections"
            )
        if self.adapt:
            system_prompt = paper_system_prompt(
                self.prompt_path.read_text(encoding="utf-8")
            )
            self.publish(
                "log",
                f"Adapting {len(paragraphs)} paragraphs with "
                f"{self.model or 'the configured OMP model'}, up to "
                f"{self.in_flight} worker request"
                f"{'s' if self.in_flight != 1 else ''} in flight and "
                f"{self.paragraphs_per_worker} paragraph"
                f"{'s' if self.paragraphs_per_worker != 1 else ''} "
                "per worker.\n",
            )
            self.process_paragraphs(
                scratch, paragraphs, image_paths, system_prompt
            )
        else:
            temporary = self.output_path.with_name(
                f".{self.output_path.name}.tmp"
            )
            self.output_path.parent.mkdir(
                mode=0o750, parents=True, exist_ok=True
            )
            temporary.write_text(
                "\n\n".join(paragraphs),
                encoding="utf-8",
                newline="\n",
            )
            temporary.replace(self.output_path)
            self.publish(
                "progress",
                {"done": 1, "total": 1, "unit": "document"},
            )
        write_json_atomic(manifest_path, {**identity, "complete": True})
        return self.output_path

    def pump(self):
        self.set_phase("extraction", "Extraction")
        try:
            if self.scratch_path is not None:
                self.prepare_document(self.scratch_path)
            else:
                with tempfile.TemporaryDirectory(
                    prefix="document-harness-"
                ) as scratch_value:
                    self.prepare_document(Path(scratch_value))
            self.publish("log", f"Wrote {self.output_path}\n")
            self.close(0)
        except InterruptedError:
            self.publish("log", "Document processing stopped.\n")
            self.close(130)
        except (
            LookupError,
            OSError,
            RuntimeError,
            UnicodeError,
            ValueError,
            concurrent.futures.CancelledError,
        ) as exc:
            self.publish("log", f"Document processing failed: {exc}\n")
            self.close(1)

    def stop(self):
        self.stop_requested.set()
        self.terminate_children()


class AudiobookRun(Run):
    """Prepare one shared document and narrate it without manual handoffs."""

    def __init__(
        self,
        values,
        storage,
        input_version,
        voice_version,
        job_id,
    ):
        super().__init__([], "audiobook", values["output"])
        self.values = dict(values)
        self.storage = storage
        self.input_version = input_version
        self.voice_version = voice_version
        self.job_id = job_id
        self.stage = storage.in_progress / job_id
        self.prepared_input = None
        self.stop_requested = threading.Event()
        self.paper_run = None

    def assign_workers(self, workers):
        """Bind a queued job to its local and SSH chunk workers."""
        self.values["workers"] = [dict(worker) for worker in workers]
        local = next(
            (
                worker for worker in workers
                if worker.get("kind") == "local"
            ),
            None,
        )
        if local is not None:
            self.values["device"] = local["device"]

    def _migrate_legacy_stage(self):
        legacy = self.storage.in_progress / safe_output_stem(
            self.values["output_name"]
        )
        if self.stage.exists() or not legacy.is_dir() or legacy == self.stage:
            return
        manifest = read_json_file(legacy / "assets.json")
        if (
            manifest
            and manifest.get("input_version") == self.input_version
            and manifest.get("voice_version") == self.voice_version
        ):
            legacy.replace(self.stage)

    def _snapshot_assets(self):
        manifest_path = self.stage / "assets.json"
        source_dir = self.stage / "source"
        local_voice = self.values["clone"]["source"] != "server"
        voice_dir = self.stage / "voice"
        manifest = read_json_file(manifest_path) or {}
        versions_match = (
            manifest.get("input_version") == self.input_version
            and manifest.get("voice_version") == self.voice_version
        )
        source_name = safe_asset_name(manifest.get("source_name", ""))
        source_path = source_dir / source_name if source_name else None
        if versions_match and source_path is None and source_dir.is_dir():
            candidates = [
                path for path in source_dir.iterdir() if path.is_file()
            ]
            if len(candidates) == 1:
                source_path = candidates[0]
                source_name = source_path.name
        reusable = bool(
            versions_match
            and source_path is not None
            and source_path.is_file()
            and file_version(source_path) == self.input_version
        )
        if local_voice:
            reusable = bool(
                reusable
                and is_saved_voice(voice_dir)
                and saved_voice_version(voice_dir) == self.voice_version
            )
        if not reusable:
            for name in ("source", "voice", "extraction", "narration"):
                shutil.rmtree(self.stage / name, ignore_errors=True)
            source_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
            suffix = Path(self.values["document_name"]).suffix.lower()
            source_name = f"document{suffix}"
            source_path = source_dir / source_name
            shutil.copyfile(self.values["input"], source_path)
            if local_voice:
                voice_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
                for name in VOICE_FILES:
                    shutil.copyfile(
                        Path(self.values["voice_dir"]) / name,
                        voice_dir / name,
                    )
            copied_input_version = file_version(source_path)
            copied_voice_version = (
                saved_voice_version(voice_dir)
                if local_voice
                else self.voice_version
            )
            if (
                copied_input_version != self.input_version
                or copied_voice_version != self.voice_version
            ):
                shutil.rmtree(source_dir, ignore_errors=True)
                shutil.rmtree(voice_dir, ignore_errors=True)
                raise RuntimeError(
                    "Document or voice changed while the job was being queued; "
                    "submit it again."
                )
        write_json_atomic(
            manifest_path,
            {
                "schema": 2,
                "input_version": self.input_version,
                "voice_version": self.voice_version,
                "source_name": source_name,
            },
        )
        if local_voice:
            self.values["voice_dir"] = str(voice_dir)
        return source_path

    def prepare(self):
        if self.prepared_input is not None:
            return self.prepared_input
        self._migrate_legacy_stage()
        self.stage.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.prepared_input = self._snapshot_assets()
        return self.prepared_input

    def _publish_prepared_document(self, source):
        target = self.storage.documents / self.values["prepared_name"]
        temporary = target.with_name(f".{target.name}.tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(target)
        self.publish("log", f"Stored prepared document: {target}\n")
        return target

    def _run_narration(self, input_path):
        narration_dir = self.stage / "narration"
        narration_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        staged_output = narration_dir / self.values["output_name"]
        command_values = {
            **self.values,
            "input": str(input_path),
            "output": str(staged_output),
            "resume_dir": str(narration_dir / "chunks"),
        }
        command = narrate_command(command_values)
        self.publish("log", " ".join(command) + "\n\n")
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                errors="replace",
            )
        except OSError as exc:
            raise RuntimeError(f"cannot start narration: {exc}") from exc
        for line in self.process.stdout:
            self.publish("log", line)
            self.inspect(line.strip())
        self.process.stdout.close()
        code = self.process.wait()
        if self.stop_requested.is_set():
            raise InterruptedError("audiobook workflow stopped")
        if code != 0:
            raise RuntimeError(f"narration exited with {code}")
        if not staged_output.is_file():
            raise RuntimeError("narration did not create its staged MP3")
        return staged_output

    def _stage_reader(
        self,
        input_path,
        narration_input,
        needs_extraction,
        extraction_dir,
    ):
        narration_encoding = "utf-8" if needs_extraction else self.values["encoding"]
        narration = narration_input.read_text(encoding=narration_encoding)
        source_path = input_path
        adaptation_checkpoints = None
        image_roots = [input_path.parent]
        if needs_extraction:
            extracted = extraction_dir / "document.md"
            if extracted.is_file():
                source_path = extracted
                image_roots.append(extraction_dir)
            if self.values["adapt"]:
                adaptation_checkpoints = (
                    extraction_dir / "paragraph-checkpoints"
                )
        source_encoding = (
            "utf-8"
            if source_path == extraction_dir / "document.md"
            else self.values["encoding"]
        )
        source_paragraphs, _ = omit_reference_sections(
            split_paper_paragraphs(
                source_path.read_text(encoding=source_encoding)
            )
        )
        narration_dir = self.stage / "narration"
        word_aligner = None
        with WORD_ALIGNMENT_LOCK:
            self.publish("log", "Loading multilingual forced aligner…\n")
            try:
                word_aligner = ForcedWordAligner()
            except Exception as exc:
                self.publish(
                    "log",
                    f"Word alignment unavailable: {exc}\n",
                )
            try:
                markdown, synchronization = build_reader_artifacts(
                    narration,
                    "\n\n".join(source_paragraphs),
                    int(self.values["chunk_max_chars"]),
                    narration_dir / "chunks",
                    adaptation_checkpoints=adaptation_checkpoints,
                    image_roots=image_roots,
                    word_aligner=word_aligner,
                    alignment_progress=lambda done, total, failed: self.publish(
                        "progress",
                        {
                            "done": done,
                            "total": total,
                            "unit": "sentence",
                            "failed": failed,
                        },
                    ),
                )
            finally:
                if word_aligner is not None:
                    word_aligner.close()
        if synchronization["word_timing"] == "aligned":
            self.publish(
                "log",
                f"Aligned {len(synchronization['word_cues'])} words.\n",
            )
        elif synchronization["word_timing"] == "partial":
            self.publish(
                "log",
                f"Aligned {len(synchronization['word_cues'])} words; "
                f"{synchronization['alignment_failures']} sentence chunks "
                "could not be aligned.\n",
            )
        staged_markdown = narration_dir / "reader.md"
        staged_sync = narration_dir / "reader.json"
        staged_markdown.write_text(
            markdown, encoding="utf-8", newline="\n"
        )
        write_json_atomic(staged_sync, synchronization)
        return staged_markdown, staged_sync

    def _publish_reader(
        self,
        staged_output,
        staged_markdown,
        staged_sync,
        output_path,
    ):
        audio_version = file_version(staged_output)
        markdown_path, sync_path = audiobook_reader_paths(
            self.storage, output_path, audio_version
        )
        staged_markdown.replace(markdown_path)
        staged_sync.replace(sync_path)
        staged_output.replace(output_path)
        write_json_atomic(
            audiobook_version_path(self.storage, output_path),
            {
                "schema": 3,
                "job_id": self.job_id,
                "input_version": self.input_version,
                "voice_version": self.voice_version,
                "document": self.values["document_name"],
                "voice": (
                    self.values["clone_voice"]
                    if self.values["clone"]["source"] == "server"
                    else self.values["selected_voice"]
                ),
                "reader": {
                    "markdown": markdown_path.name,
                    "sync": sync_path.name,
                    "audio_sha256": audio_version,
                },
            },
        )
        keep = {markdown_path.name, sync_path.name}
        prefix = f"{output_path.name}."
        for path in self.storage.readers.iterdir():
            if (
                path.is_file()
                and path.name.startswith(prefix)
                and path.name not in keep
            ):
                path.unlink(missing_ok=True)

    def pump(self):
        try:
            input_path = self.prepare()
            narration_input = input_path
            extraction_dir = self.stage / "extraction"
            needs_extraction = (
                input_path.suffix.lower() == ".pdf" or self.values["adapt"]
            )
            if needs_extraction:
                self.set_phase("extraction", "Extraction")
                prepared_path = extraction_dir / "prepared.txt"
                self.paper_run = PaperRun(
                    input_path,
                    prepared_path,
                    self.values["encoding"],
                    self.values["model"],
                    self.values["local_server"],
                    self.values["in_flight"],
                    self.values["paragraphs_per_worker"],
                    scratch_path=extraction_dir,
                    adapt=self.values["adapt"],
                )
                self.paper_run.publish = self.publish
                self.paper_run.stop_requested = self.stop_requested
                narration_input = self.paper_run.prepare_document(
                    extraction_dir
                )
                self._publish_prepared_document(narration_input)
            if self.stop_requested.is_set():
                raise InterruptedError("audiobook workflow stopped")

            self.set_phase("narration", "Narration")
            staged_output = self._run_narration(narration_input)
            self.set_phase("alignment", "Word alignment")
            staged_markdown, staged_sync = self._stage_reader(
                input_path,
                narration_input,
                needs_extraction,
                extraction_dir,
            )
            output_path = Path(self.values["output"])
            output_path.parent.mkdir(
                mode=0o750, parents=True, exist_ok=True
            )
            self._publish_reader(
                staged_output,
                staged_markdown,
                staged_sync,
                output_path,
            )
            self.artifact = str(output_path)
            shutil.rmtree(self.stage, ignore_errors=True)
            self.publish("log", f"Completed audiobook: {output_path}\n")
            self.close(0)
        except InterruptedError:
            self.publish(
                "log",
                f"Stopped. Resume data remains in {self.stage}.\n",
            )
            self.close(130)
        except (
            LookupError,
            OSError,
            RuntimeError,
            UnicodeError,
            ValueError,
            concurrent.futures.CancelledError,
        ) as exc:
            self.publish(
                "log",
                f"Audiobook workflow failed: {exc}\n"
                f"Resume data remains in {self.stage}.\n",
            )
            self.close(1)

    def stop(self):
        self.stop_requested.set()
        if self.paper_run is not None:
            self.paper_run.terminate_children()
        super().stop()


class OpenAIOAuthLogin:
    """One server-side OpenAI device login backed by OMP's credential store."""

    URL = re.compile(r"https://auth\.openai\.com/\S+")
    CODE = re.compile(r"Enter code:\s*([A-Z0-9-]+)")

    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.status = "idle"
        self.url = ""
        self.code = ""
        self.message = ""

    def snapshot(self):
        with self.lock:
            active = self.status in ("starting", "waiting")
            return {
                "status": self.status,
                "active": active,
                "url": self.url if active else "",
                "code": self.code if active else "",
                "message": self.message,
            }

    def start(self):
        with self.lock:
            if self.status in ("starting", "waiting"):
                return self.snapshot()
            self.status = "starting"
            self.url = ""
            self.code = ""
            self.message = "Starting OpenAI device authorization…"
        threading.Thread(target=self._pump, daemon=True).start()
        return self.snapshot()

    def _pump(self):
        environment = os.environ.copy()
        browser_suppressor = shutil.which("true")
        if browser_suppressor:
            environment["BROWSER"] = browser_suppressor
        command = [
            shutil.which("omp") or "omp",
            "auth-broker",
            "login",
            OPENAI_OAUTH_PROVIDER,
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            with self.lock:
                self.status = "failed"
                self.message = f"Cannot start OpenAI authorization: {exc}"
            return
        with self.lock:
            self.process = process
            if self.status == "canceled":
                process.terminate()
        for line in process.stdout:
            clean = line.strip()
            if not clean:
                continue
            with self.lock:
                url = self.URL.search(clean)
                code = self.CODE.search(clean)
                if url is not None:
                    self.url = url.group(0)
                if code is not None:
                    self.code = code.group(1)
                if self.url or self.code:
                    self.status = "waiting"
                self.message = clean
        process.stdout.close()
        returncode = process.wait()
        with self.lock:
            self.process = None
            if self.status == "canceled":
                self.message = "OpenAI authorization canceled."
            elif returncode == 0:
                self.status = "connected"
                self.message = "OpenAI OAuth credentials saved on this server."
            else:
                self.status = "failed"
                self.message = (
                    self.message
                    if self.message and self.message != "Waiting for browser authorization…"
                    else f"OpenAI authorization exited with {returncode}."
                )

    def stop(self):
        with self.lock:
            self.status = "canceled"
            self.message = "Canceling OpenAI authorization…"
            process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
        return self.snapshot()




# --- HTTP ---------------------------------------------------------------------


def airdrop_available():
    if platform.system() != "Darwin":
        return False
    try:
        import AppKit  # noqa: F401
    except ImportError:
        return False
    return True


def airdrop(path):
    import AppKit
    import Foundation

    service = AppKit.NSSharingService.sharingServiceNamed_(
        AppKit.NSSharingServiceNameSendViaAirDrop
    )
    if service is None:
        raise RuntimeError("AirDrop sharing service is unavailable")
    service.performWithItems_([Foundation.NSURL.fileURLWithPath_(str(path))])


def copy_file_bytes(source, sink, length):
    """Copy exactly length bytes, or fewer if the source ends first."""
    while length > 0:
        block = source.read(min(length, 1 << 20))
        if not block:
            return
        sink.write(block)
        length -= len(block)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "audiobook-tts"

    # -- plumbing

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def cross_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return False
        host = urllib.parse.urlsplit(origin).netloc
        return host != self.headers.get("Host")

    def payload(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def reply(self, code, body, kind="application/json", extra=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, message):
        self.reply(code, {"error": message})

    # -- routing

    def do_GET(self):
        route, query = self.split()
        if route in ("/", "/index.html"):
            return self.reply(
                HTTPStatus.OK,
                PAGE,
                "text/html; charset=utf-8",
                extra=(("Cache-Control", "no-store"),),
            )
        if route == "/hilde-dark.png":
            return self.send_file(
                str(APP_ICON_PATH), False, allow_outside=True
            )
        if route == "/zeki.jpg":
            return self.send_file(
                str(BRAND_IMAGE_PATH), False, allow_outside=True
            )
        if route == "/api/jobs":
            return self.reply(
                HTTPStatus.OK,
                {
                    "runs": self.server.jobs.active_snapshots(),
                    "queue": self.server.jobs.snapshot(),
                    "consumers": self.server.jobs.public_consumers_snapshot(),
                },
                extra=(("Cache-Control", "no-store"),),
            )
        if route == "/api/state":
            state = normalize(read_state_cookie(self.headers.get("Cookie")))
            return self.reply(
                HTTPStatus.OK,
                {
                    "state": state,
                    "derived": derived(
                        state, self.server.tts_models, self.server.storage
                    ),
                    "assets": asset_catalog(self.server.storage),
                    "runs": self.server.jobs.active_snapshots(),
                    "queue": self.server.jobs.snapshot(),
                    "consumers": self.server.jobs.public_consumers_snapshot(),
                    "configuration": public_configuration(
                        self.server.tts_models
                    ),
                    "capabilities": {
                        "airdrop": airdrop_available(),
                    },
                },
                extra=(("Cache-Control", "no-store"),),
            )
        if route == "/api/voices":
            return self.reply(
                HTTPStatus.OK,
                {"voices": voice_catalog(self.server.storage)},
                extra=(("Cache-Control", "no-store"),),
            )
        if route == "/api/voices/preview":
            try:
                voice_dir = resolve_asset(
                    self.server.storage.voices, query.get("name", [""])[0]
                )
            except ValueError as exc:
                return self.fail(HTTPStatus.BAD_REQUEST, str(exc))
            if not is_saved_voice(voice_dir):
                return self.fail(HTTPStatus.NOT_FOUND, "no such voice")
            return self.send_file(str(voice_preview(voice_dir)[0]), False)
        if route == "/api/library":
            return self.reply(
                HTTPStatus.OK,
                {"books": library_catalog(self.server.storage)},
                extra=(("Cache-Control", "no-store"),),
            )
        if route == "/api/paper/models":
            state = normalize(read_state_cookie(self.headers.get("Cookie")))
            try:
                catalog = paper_model_catalog(
                    state["audiobook"]["local_server"],
                    state["audiobook"]["local_provider"],
                )
            except (RuntimeError, ValueError) as exc:
                return self.fail(HTTPStatus.BAD_GATEWAY, str(exc))
            return self.reply(HTTPStatus.OK, catalog)
        if route == "/api/paper/openai/status":
            return self.reply(
                HTTPStatus.OK, self.server.openai_login.snapshot()
            )
        if route == "/api/events":
            return self.events(query.get("job", [""])[0])
        if route == "/api/reader":
            try:
                payload = audiobook_reader_payload(
                    self.server.storage, query.get("name", [""])[0]
                )
            except (FileNotFoundError, OSError, UnicodeError, ValueError):
                return self.fail(
                    HTTPStatus.NOT_FOUND,
                    "Synchronized reader is unavailable for this audiobook.",
                )
            return self.reply(
                HTTPStatus.OK,
                payload,
                extra=(("Cache-Control", "no-store"),),
            )
        if route == "/api/audio" and query.get("name", [""])[0]:
            try:
                target = resolve_asset(
                    self.server.storage.audiobooks,
                    query["name"][0],
                )
            except ValueError as exc:
                return self.fail(HTTPStatus.BAD_REQUEST, str(exc))
            if target.suffix.lower() != ".mp3":
                return self.fail(HTTPStatus.NOT_FOUND, "no such audiobook")
            if query.get("container", [""])[0] == "mp4":
                return self.send_mp3_as_mp4(target)
            return self.send_file(str(target), False)
        if route == "/api/download" and query.get("asset", [""])[0]:
            try:
                target = resolve_asset(
                    self.server.storage.audiobooks,
                    query["asset"][0],
                )
            except ValueError as exc:
                return self.fail(HTTPStatus.BAD_REQUEST, str(exc))
            if target.suffix.lower() != ".mp3":
                return self.fail(HTTPStatus.NOT_FOUND, "no such audiobook")
            return self.send_file(str(target), True, target.name)
        return self.fail(HTTPStatus.NOT_FOUND, f"no route for {route}")

    def do_POST(self):
        route, query = self.split()
        if self.cross_origin():
            return self.fail(
                HTTPStatus.FORBIDDEN, "cross-origin request refused"
            )
        if route == "/api/documents/upload":
            return self.upload_document(
                query.get("name", ["document.txt"])[0]
            )
        body = self.payload()
        if route == "/api/documents/download":
            return self.download_document(body)
        if route == "/api/voices/delete":
            return self.delete_asset("voice", body.get("name", ""))
        if route == "/api/documents/delete":
            return self.delete_asset("document", body.get("name", ""))
        if route == "/api/audiobooks/delete":
            return self.delete_asset("audiobook", body.get("name", ""))
        if route == "/api/paper/openai/login":
            return self.reply(
                HTTPStatus.ACCEPTED,
                self.server.openai_login.start(),
            )
        if route == "/api/paper/openai/cancel":
            return self.reply(
                HTTPStatus.OK,
                self.server.openai_login.stop(),
            )
        if route == "/api/paper/local/check":
            try:
                local_server = normalize_local_server(body.get("server", ""))
            except ValueError as exc:
                return self.fail(HTTPStatus.BAD_REQUEST, str(exc))
            if not local_server:
                return self.fail(
                    HTTPStatus.BAD_REQUEST,
                    "Enter a local model server IP and port.",
                )
            provider = body.get("provider")
            if provider not in LOCAL_MODEL_PROVIDERS:
                return self.fail(HTTPStatus.BAD_REQUEST, "Choose the server type.")
            try:
                catalog = paper_model_catalog(local_server, provider)
            except RuntimeError as exc:
                return self.fail(HTTPStatus.BAD_GATEWAY, str(exc))
            if catalog["local_error"]:
                return self.fail(
                    HTTPStatus.BAD_GATEWAY, catalog["local_error"]
                )
            return self.reply(HTTPStatus.OK, catalog)
        if route == "/api/sync":
            state = normalize(body.get("state", {}))
            try:
                headers = state_cookie_headers(
                    state, self.headers.get("Cookie", "")
                )
            except ValueError as exc:
                return self.fail(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc)
                )
            return self.reply(
                HTTPStatus.OK,
                {
                    "state": state,
                    "derived": derived(
                        state, self.server.tts_models, self.server.storage
                    ),
                    "assets": asset_catalog(self.server.storage),
                },
                extra=headers,
            )
        if route == "/api/run":
            state = normalize(body.get("state", {}))
            try:
                headers = state_cookie_headers(
                    state, self.headers.get("Cookie", "")
                )
            except ValueError as exc:
                return self.fail(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc)
                )
            return self.run(state, bool(body.get("confirmed")), headers)
        if route == "/api/stop":
            count = self.server.jobs.stop_all()
            return self.reply(
                HTTPStatus.OK,
                {"stopping": count > 0, "count": count},
            )
        if route == "/api/jobs/cancel":
            job = self.server.jobs.cancel(str(body.get("id", "")))
            if job is None:
                return self.fail(HTTPStatus.NOT_FOUND, "no queued job with that id")
            return self.reply(
                HTTPStatus.OK,
                {
                    "job": job,
                    "runs": self.server.jobs.active_snapshots(),
                    "queue": self.server.jobs.snapshot(),
                    "consumers": self.server.jobs.public_consumers_snapshot(),
                },
            )
        if route == "/api/airdrop":
            return self.airdrop(body.get("path", ""))
        return self.fail(HTTPStatus.NOT_FOUND, f"no route for {route}")

    def split(self):
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    # -- handlers


    def upload_document(self, name):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.fail(HTTPStatus.BAD_REQUEST, "invalid upload length")
        if length <= 0:
            return self.fail(
                HTTPStatus.BAD_REQUEST, "choose a nonempty file"
            )
        if length > BOOK_UPLOAD_LIMIT:
            return self.fail(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "upload exceeds the 64 MiB limit",
            )
        safe_name = safe_asset_name(name)
        if not safe_name or Path(safe_name).suffix.lower() not in PAPER_SUFFIXES:
            return self.fail(
                HTTPStatus.BAD_REQUEST,
                "Document filename must end in PDF, text, or Markdown.",
            )
        descriptor = None
        temporary = None
        try:
            descriptor, temporary_value = tempfile.mkstemp(
                prefix=".upload-",
                dir=self.server.storage.in_progress,
            )
            temporary = Path(temporary_value)
            remaining = length
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError(
                            "upload ended before the declared length"
                        )
                    output.write(chunk)
                    remaining -= len(chunk)
            target = self.server.storage.documents / safe_name
            temporary.replace(target)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            return self.fail(
                HTTPStatus.BAD_REQUEST, f"cannot save document: {exc}"
            )
        return self.reply(
            HTTPStatus.CREATED,
            {
                "name": safe_name,
                "bytes": length,
                "assets": asset_catalog(self.server.storage),
            },
        )

    def download_document(self, body):
        try:
            url = normalize_paper_url(body.get("url", ""))
        except ValueError as exc:
            return self.fail(HTTPStatus.BAD_REQUEST, str(exc))
        name = str(body.get("name", "")).strip()
        if not url or (name and safe_asset_name(name) != name):
            return self.fail(
                HTTPStatus.BAD_REQUEST,
                "Enter a direct document URL and an optional filename without a slash.",
            )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": (
                    "application/pdf,text/markdown,text/plain;q=0.9,"
                    "*/*;q=0.1"
                ),
                "User-Agent": "audiobook-tts/1",
            },
        )
        try:
            response = urllib.request.urlopen(
                request, timeout=PAPER_DOWNLOAD_TIMEOUT
            )
        except urllib.error.HTTPError as exc:
            return self.fail(
                HTTPStatus.BAD_GATEWAY,
                f"Document URL returned HTTP {exc.code} {exc.reason}.",
            )
        except (urllib.error.URLError, OSError) as exc:
            return self.fail(
                HTTPStatus.BAD_GATEWAY,
                f"Cannot download document URL: {exc}",
            )
        descriptor = None
        temporary = None
        try:
            with response:
                final_url = normalize_paper_url(response.geturl())
                content_type = response.headers.get_content_type().lower()
                if content_type in ("text/html", "application/xhtml+xml"):
                    raise ValueError(
                        "Document URL returned HTML; use a direct document URL."
                    )
                try:
                    declared_size = int(
                        response.headers.get("Content-Length") or 0
                    )
                except ValueError:
                    declared_size = 0
                if declared_size > BOOK_UPLOAD_LIMIT:
                    raise ValueError(
                        "Document URL exceeds the 64 MiB download limit."
                    )
                descriptor, temporary_value = tempfile.mkstemp(
                    prefix=".download-",
                    dir=self.server.storage.in_progress,
                )
                temporary = Path(temporary_value)
                size = 0
                with os.fdopen(descriptor, "wb") as output:
                    descriptor = None
                    while True:
                        chunk = response.read(
                            min(
                                1024 * 1024,
                                BOOK_UPLOAD_LIMIT + 1 - size,
                            )
                        )
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > BOOK_UPLOAD_LIMIT:
                            raise ValueError(
                                "Document URL exceeds the 64 MiB download limit."
                            )
                        output.write(chunk)
            if size == 0:
                raise ValueError("Document URL returned an empty file.")
            final_component = safe_asset_name(
                Path(
                    urllib.parse.unquote(urllib.parse.urlsplit(final_url).path)
                ).name,
                "document",
            )
            candidate = name or final_component
            suffix = Path(candidate).suffix.lower()
            if suffix not in PAPER_SUFFIXES:
                with temporary.open("rb") as downloaded:
                    is_pdf = downloaded.read(5) == b"%PDF-"
                content_suffix = {
                    "application/pdf": ".pdf",
                    "text/markdown": ".md",
                    "text/plain": ".txt",
                }.get(content_type, "")
                final_suffix = Path(final_component).suffix.lower()
                suffix = (
                    ".pdf"
                    if is_pdf
                    else content_suffix
                    or (
                        final_suffix
                        if final_suffix in PAPER_SUFFIXES
                        else ""
                    )
                )
                if suffix not in PAPER_SUFFIXES:
                    raise ValueError(
                        "Cannot determine the document type; enter a filename "
                        "ending in .pdf, .md, .markdown, .txt, or .text."
                    )
                candidate = f"{candidate}{suffix}"
            name = candidate
            target = self.server.storage.documents / name
            temporary.replace(target)
        except (OSError, ValueError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            return self.fail(HTTPStatus.BAD_REQUEST, str(exc))
        return self.reply(
            HTTPStatus.CREATED,
            {
                "name": name,
                "bytes": size,
                "assets": asset_catalog(self.server.storage),
            },
        )

    def delete_asset(self, kind, name):
        storage = self.server.storage
        try:
            if kind == "voice":
                voice_dir = resolve_asset(storage.voices, name)
                if self.server.jobs.creating_voice(voice_dir):
                    return self.fail(
                        HTTPStatus.CONFLICT,
                        f"{voice_dir.name} is being created. Stop it before deleting it.",
                    )
                delete_voice(storage, name)
            elif kind == "document":
                delete_document(storage, name)
            else:
                delete_audiobook(storage, name)
        except ValueError as exc:
            return self.fail(HTTPStatus.BAD_REQUEST, str(exc))
        except FileNotFoundError:
            missing = {"voice": "voice", "document": "book"}.get(kind, "audiobook")
            return self.fail(HTTPStatus.NOT_FOUND, f"That {missing} no longer exists.")
        except OSError as exc:
            # Operating-system messages name server paths; browsers get the reason.
            return self.fail(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Could not delete {name}: {exc.strerror or 'file system error'}.",
            )
        return self.reply(HTTPStatus.OK, {"assets": asset_catalog(storage)})



    def run(self, state, confirmed=False, state_headers=()):
        if state["tab"] == "player":
            return self.fail(
                HTTPStatus.BAD_REQUEST,
                "The Listen page cannot start a job.",
            )
        facts = derived(
            state, self.server.tts_models, self.server.storage
        )
        if facts["problem"] is not None:
            return self.fail(HTTPStatus.BAD_REQUEST, facts["problem"])
        values = values_of(
            state, self.server.tts_models, self.server.storage
        )
        if state["tab"] == "voice":
            command = create_voice_command(values)
            run = Run(command, "voice", values["new_voice_dir"])
            # The fixed passage stays out of the visible run log.
            shown = [
                "<fixed preview passage>" if part == VOICE_REFERENCE_TEXT else part
                for part in command
            ]
            run.publish("log", " ".join(shown) + "\n\n")
            if not self.server.jobs.start_voice(run):
                return self.fail(
                    HTTPStatus.CONFLICT,
                    "voice creation requires an empty audiobook queue",
                )
            return self.reply(
                HTTPStatus.OK,
                {
                    "started": True,
                    "queued": False,
                    "duplicate": False,
                    "kind": run.kind,
                    "output_name": values["output_name"],
                    "runs": self.server.jobs.active_snapshots(),
                    "queue": self.server.jobs.snapshot(),
                    "consumers": self.server.jobs.public_consumers_snapshot(),
                },
                extra=state_headers,
            )

        if values["source_url"]:
            return self.fail(
                HTTPStatus.CONFLICT,
                "Download the URL into Documents before starting.",
            )
        try:
            input_version, voice_version = audiobook_versions(values)
        except OSError as exc:
            return self.fail(
                HTTPStatus.BAD_REQUEST,
                f"Cannot version the selected assets: {exc}",
            )
        job_id = audiobook_job_id(input_version, voice_version)

        def reply_job(job, duplicate=False):
            queued = job["status"] != "running"
            return self.reply(
                HTTPStatus.ACCEPTED if queued else HTTPStatus.OK,
                {
                    "started": not queued,
                    "queued": queued,
                    "duplicate": duplicate,
                    "kind": "audiobook",
                    "output_name": values["output_name"],
                    "job": job,
                    "runs": self.server.jobs.active_snapshots(),
                    "queue": self.server.jobs.snapshot(),
                    "consumers": self.server.jobs.public_consumers_snapshot(),
                },
                extra=state_headers,
            )

        existing = self.server.jobs.existing(job_id)
        if existing is not None:
            return reply_job(existing, duplicate=True)
        if (
            not confirmed
            and output_versions_match(
                self.server.storage,
                values["output"],
                input_version,
                voice_version,
            )
        ):
            return self.reply(
                HTTPStatus.CONFLICT,
                {
                    "confirmation_required": True,
                    "message": (
                        f"{values['output_name']} already contains this "
                        "document version narrated by this voice version. "
                        "Queue it again and overwrite the current output?"
                    ),
                },
            )
        narrator = (
            values["clone_voice"]
            if values["clone"]["source"] == "server"
            else values["selected_voice"]
        )
        try:
            record, created = self.server.jobs.reserve_audiobook(
                input_version,
                voice_version,
                values["document_name"],
                narrator,
                values["output_name"],
                values["requested_device"],
                values["device"],
            )
        except ValueError as exc:
            return self.fail(HTTPStatus.CONFLICT, str(exc))
        if not created:
            return reply_job(self.server.jobs.describe(record), duplicate=True)
        run = AudiobookRun(
            values,
            self.server.storage,
            input_version,
            voice_version,
            record["id"],
        )
        try:
            run.prepare()
        except (OSError, RuntimeError, ValueError) as exc:
            self.server.jobs.discard(record)
            return self.fail(
                HTTPStatus.CONFLICT,
                f"Cannot queue audiobook: {exc}",
            )
        job = self.server.jobs.commit(record, run)
        return reply_job(job)

    def events(self, job_id=""):
        run = self.server.jobs.run_for(job_id)
        if run is None:
            return self.fail(HTTPStatus.NOT_FOUND, "no run to watch")
        try:
            last_event_id = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            last_event_id = 0
        sink = run.subscribe(max(0, last_event_id))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            while True:
                try:
                    event_id, event, data = sink.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if event == "progress" and isinstance(data, dict):
                    data = {
                        **data,
                        "stream_elapsed": run.elapsed(),
                        "phase_stream_elapsed": run.phase_elapsed(),
                    }
                body = json.dumps(data)
                self.wfile.write(
                    f"id: {event_id}\nevent: {event}\ndata: {body}\n\n".encode("utf-8")
                )
                self.wfile.flush()
                if event == "done":
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            run.unsubscribe(sink)

    def send_file(
        self, path, attachment, download_name="", allow_outside=False
    ):
        target = Path(path).expanduser()
        if (
            not path
            or not target.is_file()
            or (not allow_outside and not self.server.storage.contains(target))
        ):
            return self.fail(HTTPStatus.NOT_FOUND, "no such asset")
        size = target.stat().st_size
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        extra = ()
        if attachment:
            # RFC 6266: a quoted filename cannot carry percent-encoding, so the
            # escaped name goes in filename* and a plain one stays as fallback.
            offered = safe_asset_name(download_name, target.name)
            plain = (
                offered.replace('"', "")
                .encode("ascii", "replace")
                .decode("ascii")
            )
            escaped = urllib.parse.quote(offered)
            extra = ((
                "Content-Disposition",
                f"attachment; filename=\"{plain}\"; filename*=UTF-8''{escaped}",
            ),)
        start, end = self.send_range_headers(kind, size, extra)
        with open(target, "rb") as handle:
            handle.seek(start)
            copy_file_bytes(handle, self.wfile, end - start + 1)

    def send_mp3_as_mp4(self, target):
        """Serve MP3 frames behind an MP4 index so browsers seek exact samples."""
        try:
            handle = open(target, "rb")
        except OSError:
            return self.fail(HTTPStatus.NOT_FOUND, "no such audiobook")
        with handle:
            try:
                header, payload_start, payload_end = mp3_mp4_layout(handle)
            except (OSError, ValueError) as exc:
                return self.fail(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, str(exc))
            size = len(header) + payload_end - payload_start
            start, end = self.send_range_headers("audio/mp4", size)
            if start < len(header):
                self.wfile.write(memoryview(header)[start:end + 1])
            if end >= len(header):
                offset = max(start, len(header))
                handle.seek(payload_start + offset - len(header))
                copy_file_bytes(handle, self.wfile, end + 1 - offset)

    def send_range_headers(self, kind, size, extra=()):
        """Answer a simple Range request and return the inclusive span to send."""
        start, end = 0, size - 1
        # Safari refuses to play audio from a server that ignores Range requests.
        requested = self.headers.get("Range")
        partial = False
        if requested and requested.startswith("bytes="):
            first, _, last = requested[6:].partition("-")
            try:
                start = int(first) if first else 0
                end = int(last) if last else size - 1
            except ValueError:
                start, end = 0, size - 1
            else:
                partial = True
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        return start, end

    def airdrop(self, path):
        if not airdrop_available():
            return self.fail(HTTPStatus.NOT_IMPLEMENTED,
                             "AirDrop needs this server to run on macOS with pyobjc installed")
        target = Path(path).expanduser()
        if (
            not path
            or not os.path.lexists(target)
            or not self.server.storage.contains(target)
        ):
            return self.fail(HTTPStatus.NOT_FOUND, "no such asset")
        try:
            airdrop(target)
        except (RuntimeError, ImportError) as exc:
            return self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        return self.reply(HTTPStatus.OK, {"shared": str(target)})


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hilde</title>
<link rel="icon" type="image/png" href="/hilde-dark.png">
<link rel="apple-touch-icon" href="/hilde-dark.png">
<style>
:root { color-scheme:dark;
        /* One charcoal ground, one neutral gray family, one warm accent. */
        --bg:#1b1b1b; --field:#161616; --surface:#242424; --raised:#2e2e2e;
        --hover:#383838; --line:#3a3a3a; --text:#f1eee9; --dim:#a7a29b;
        --accent:#e07b39; --accent-ink:#1b1b1b; --accent-soft:rgba(224,123,57,.16);
        --bad:#ff8a80; --radius:12px; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif; }
main { max-width:960px; margin:0 auto; padding:24px 20px 8px; }
h1, h2, h3 { margin:0; font-weight:650; line-height:1.25; }
p { margin:0; }
a { color:var(--accent); }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.hidden { display:none !important; }
.visually-hidden { position:absolute !important; width:1px; height:1px; margin:-1px;
                   padding:0; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap;
                   border:0; }
.note { color:var(--dim); font-size:13px; }
.bad, .problem { color:var(--bad); }
/* Errors never rely on color alone. */
.problem::before, .notice.bad::before { content:"⚠ "; }
.app-header { display:flex; align-items:center; gap:14px; }
.brand-logo { width:48px; height:48px; border-radius:12px; flex:0 0 auto; }
.brand-name { font-size:24px; }
.brand-tagline { color:var(--dim); font-size:13px; }
.signature { display:flex; align-items:center; justify-content:center; gap:6px;
             padding:28px 16px 24px; color:var(--dim); font-size:12px; }
.signature img { width:16px; height:16px; border-radius:4px; }
input[type=text], input[type=search], input[type=number], select, textarea {
  background:var(--field); color:var(--text); border:1px solid var(--line);
  border-radius:8px; padding:9px 12px; font:inherit; min-width:0;
}
input::placeholder, textarea::placeholder { color:#8a857e; }
textarea { width:100%; min-height:96px; resize:vertical; }
input:disabled, select:disabled, textarea:disabled { opacity:.5; }
button { background:var(--raised); color:var(--text); border:1px solid var(--line);
         border-radius:8px; padding:9px 16px; min-height:40px; font:inherit;
         cursor:pointer; }
button:hover:not(:disabled) { background:var(--hover); }
button:disabled { opacity:.45; cursor:default; }
button.primary { background:var(--accent); border-color:var(--accent);
                 color:var(--accent-ink); font-weight:650; }
button.primary:hover:not(:disabled) { background:var(--accent); filter:brightness(1.08); }
button.link { background:none; border-color:transparent; color:var(--text);
              padding:6px 8px; min-height:0; text-decoration:underline;
              text-decoration-color:var(--dim); text-underline-offset:3px; }
button.link:hover:not(:disabled) { background:none; text-decoration-color:var(--text); }
label.check { display:flex; align-items:center; gap:10px; cursor:pointer; }
label.check input { width:18px; height:18px; margin:0; accent-color:var(--accent); }
/* Folder tabs on a baseline. Inactive tabs stand 3px taller with a bevel, as if
   raised; the active tab is pressed flush and opens into the page below. */
.tabs { display:flex; align-items:flex-end; gap:8px; margin-top:20px; padding:0 8px;
        border-bottom:1px solid var(--line); }
.tab-list { display:flex; align-items:flex-end; gap:4px; }
.tab-list [role=tab] { margin-bottom:-1px; min-height:0;
                       padding:10px clamp(14px,3vw,22px) 7px;
                       border-radius:9px 9px 0 0; color:var(--dim);
                       background:linear-gradient(#383838,#292929);
                       box-shadow:inset 0 1px 0 rgba(255,255,255,.12),
                                  0 -3px 6px -2px rgba(0,0,0,.6);
                       transition:color .12s,background .12s; }
.tab-list [role=tab]:hover:not([aria-selected=true]) {
  color:var(--text); background:linear-gradient(#444,#303030);
}
.tab-list [role=tab][aria-selected=true] { padding-top:7px; color:var(--text);
                                           font-weight:650; background:var(--bg);
                                           border-bottom-color:var(--bg);
                                           box-shadow:inset 0 3px 0 var(--accent); }
.tabs .advanced { margin:0 0 6px auto; min-height:0; padding:5px 12px; font-size:13px; }
.notice { margin-top:16px; padding:10px 14px; background:var(--surface); border-radius:8px; }
.notice:empty { display:none; }
.page { padding-top:24px; }
.page-head { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between;
             gap:12px; margin-bottom:16px; }
.page-head h2 { font-size:22px; }
.card { padding:20px 22px; background:var(--surface); border-radius:var(--radius); }
.stack { display:grid; gap:14px; }
.actions { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }
.field { display:grid; gap:6px; }
.field > label { color:var(--dim); font-size:13px; }
.line { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }
.line > select, .line > input[type=text], .line > input[type=search] { flex:1 1 260px; }
.banner { display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin-bottom:12px;
          padding:12px 16px; background:var(--surface); border-radius:10px; }
.steps { display:grid; gap:12px; margin:0; padding:0; list-style:none; }
.step { padding:18px 22px; background:var(--surface); border-radius:var(--radius); }
.step-head { display:flex; align-items:center; gap:14px; min-height:32px; }
.step-marker { display:grid; place-items:center; flex:0 0 auto; width:30px; height:30px;
               border-radius:50%; background:var(--raised); color:var(--dim);
               font-size:14px; font-weight:650; }
.step.is-active .step-marker { background:var(--accent); color:var(--accent-ink); }
.step.is-done .step-marker { color:var(--text); }
.step-title { font-size:17px; }
.step.is-upcoming .step-title { color:var(--dim); }
.step-summary { flex:1; min-width:0; overflow:hidden; color:var(--dim);
                text-overflow:ellipsis; white-space:nowrap; }
.step-body { display:grid; gap:16px; margin-top:18px; padding-left:44px; }
.step:not(.is-active) .step-body { display:none; }
details > summary { width:max-content; max-width:100%; color:var(--dim); cursor:pointer; }
details.from-link[open] > summary { margin-bottom:12px; }
details.from-link .field + .field { margin-top:12px; }
details.technical pre { margin:8px 0 0; color:var(--dim); white-space:pre-wrap;
                        font:12px ui-monospace,SFMono-Regular,Menlo,monospace; }
.voice-card { display:flex; align-items:center; gap:14px; padding:12px 14px;
              background:var(--raised); border-radius:10px; }
.voice-meta { display:grid; gap:2px; min-width:0; }
.clamp { display:-webkit-box; overflow:hidden; -webkit-line-clamp:2;
         -webkit-box-orient:vertical; }
.preview-button { display:grid; place-items:center; flex:0 0 auto; width:40px; height:40px;
                  min-height:0; padding:0; border-radius:50%; }
.preview-button svg { width:16px; height:16px; fill:currentColor; }
.preview-button[aria-pressed=true],
.preview-button[aria-pressed=true]:hover:not(:disabled) {
  background:var(--accent); border-color:var(--accent); color:var(--accent-ink);
}
.stages { display:grid; gap:12px; margin:18px 0; padding:0; list-style:none; }
.stage { display:flex; align-items:center; gap:12px; color:var(--dim); }
.stage-icon { display:grid; place-items:center; flex:0 0 auto; width:24px; height:24px;
              border:2px solid var(--line); border-radius:50%; font-size:13px;
              font-weight:700; }
.stage.is-done { color:var(--text); }
.stage.is-done .stage-icon { border-color:var(--text); }
.stage.is-active { color:var(--text); font-weight:600; }
.stage.is-active .stage-icon { border-color:var(--accent); }
.stage.is-active .stage-icon::after { content:""; width:10px; height:10px;
                                      border-radius:50%; background:var(--accent); }
.progress-meter { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px;
                  align-items:center; }
progress { width:100%; height:8px; accent-color:var(--accent); }
#progress-eta { white-space:nowrap; font-variant-numeric:tabular-nums; }
#progress-detail { margin-top:6px; }
#create-progress .actions, #create-result .actions { margin-top:18px; }
#result-text { margin-top:8px; font-size:17px; }
.queue-panel { margin-top:12px; padding:16px 22px; background:var(--surface);
               border-radius:var(--radius); }
.queue-panel h3 { margin-bottom:10px; font-size:15px; }
.job-queue { display:grid; gap:8px; }
.job-row { display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:12px;
           align-items:center; padding-top:8px; border-top:1px solid var(--line); }
.job-row:first-child { padding-top:0; border-top:0; }
.job-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.job-state { color:var(--dim); font-size:13px; white-space:nowrap; }
.job-actions { display:flex; gap:6px; }
.job-row button { min-height:0; padding:5px 10px; font-size:13px; }
.advanced-panels { display:grid; gap:12px; margin-top:16px; }
fieldset { margin:0; padding:16px 22px 12px; background:var(--surface); border:0;
           border-radius:var(--radius); }
legend { float:left; width:100%; margin-bottom:12px; padding:0; font-weight:650; }
legend + * { clear:both; }
.row { display:grid; grid-template-columns:170px minmax(0,1fr); gap:10px 14px;
       align-items:center; margin-bottom:10px; }
.row > label:first-child, .row > .row-label { color:var(--dim); text-align:right; }
.worker-list { display:flex; flex-wrap:wrap; gap:6px; }
.worker-chip { display:inline-flex; align-items:center; gap:6px; padding:3px 10px;
               border:1px solid var(--line); border-radius:999px; font-size:12px;
               white-space:nowrap; }
.worker-chip::before { content:""; width:7px; height:7px; border-radius:50%;
                       background:var(--dim); }
.worker-chip.running, .worker-chip.reserved { border-color:var(--accent); }
.worker-chip.running::before, .worker-chip.reserved::before { background:var(--accent); }
.worker-status { color:var(--dim); }
.search { display:flex; flex-wrap:wrap; align-items:center; gap:12px; margin-bottom:12px; }
.search input { flex:1 1 320px; }
.data-table { width:100%; border-collapse:collapse; }
.data-table th { padding:8px 12px; border-bottom:1px solid var(--line); color:var(--dim);
                 font-size:12px; font-weight:600; letter-spacing:.04em; text-align:left;
                 text-transform:uppercase; }
.data-table td { padding:10px 12px; border-bottom:1px solid var(--surface);
                 vertical-align:middle; }
.data-table tbody tr:hover { background:var(--surface); }
.data-table tr.is-selected { background:var(--accent-soft); }
.voice-table .preview { width:64px; }
.voice-table .name { width:22%; font-weight:600; overflow-wrap:anywhere; }
.voice-table .select, .book-table .action { width:1%; text-align:right; white-space:nowrap; }
.voice-table .select > * + *, .book-table .action > * + * { margin-left:4px; }
.book-table .duration { width:120px; color:var(--dim); white-space:nowrap;
                        font-variant-numeric:tabular-nums; }
.book-table .source { width:26%; color:var(--dim); overflow-wrap:anywhere; }
.book-title { font-weight:600; }
.selected-badge { display:inline-flex; align-items:center; gap:6px; padding:0 8px;
                  color:var(--accent); font-weight:650; white-space:nowrap; }
.selected-badge svg { width:14px; height:14px; fill:currentColor; }
.more { display:block; margin:14px auto 0; }
.empty { padding:40px 20px; background:var(--surface); border-radius:var(--radius);
         color:var(--dim); text-align:center; }
.empty h3 { margin-bottom:8px; color:var(--text); font-size:18px; }
.empty p { margin-bottom:18px; }
#log { height:240px; margin-top:16px; padding:10px; overflow:auto; background:var(--field);
       border:1px solid var(--line); border-radius:8px; color:var(--dim);
       font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; }
.back { margin:0 0 12px -8px; }
audio { height:36px; }
.player-panel { background:var(--surface); border-radius:var(--radius) var(--radius) 0 0; }
.player-heading { display:flex; align-items:center; gap:16px; padding:16px 18px 6px; }
.player-title { display:grid; flex:1; gap:2px; min-width:0; }
.player-title h2 { overflow:hidden; font-size:18px; text-overflow:ellipsis;
                   white-space:nowrap; }
.player-actions { display:flex; flex-wrap:wrap; align-items:center; gap:12px; }
/* Only the controls stay on screen while the text scrolls beneath them. */
.player-controls { position:sticky; top:0; z-index:3; padding:8px 18px 14px;
                   background:var(--surface); border-radius:0 0 var(--radius) var(--radius);
                   box-shadow:0 8px 24px rgba(0,0,0,.3); }
.player-controls audio { display:block; width:100%; }
.reader-panel { margin-top:12px; padding:10px 6px; background:var(--surface);
                border-radius:var(--radius); }
#reader-unavailable { margin-top:12px; }
.reader-paragraph { padding:6px 12px; border-left:3px solid transparent;
                    border-radius:6px; line-height:1.6; transition:border-color .15s; }
.reader-paragraph.active { border-left-color:var(--accent); }
.reader-paragraph > * { margin:0; }
.reader-paragraph > * + * { margin-top:.6em; }
.reader-sentence, .reader-block { border-radius:4px; cursor:pointer;
                                  transition:background-color .15s; }
.reader-sentence { padding:.1em 0; -webkit-box-decoration-break:clone;
                   box-decoration-break:clone; }
.reader-sentence:hover, .reader-block:hover { background:var(--raised); }
.reader-sentence.active, .reader-block.active { background:var(--accent-soft); }
.reader-word { border-radius:3px; transition:background-color .08s,color .08s; }
.reader-word.active { color:var(--accent-ink); background:var(--accent);
                      box-shadow:0 0 0 2px var(--accent); }
.reader-block > :first-child { margin-top:0; }
.reader-block > :last-child { margin-bottom:0; }
.reader-block img { display:block; max-width:100%; height:auto; margin:12px auto;
                    border-radius:5px; }
.reader-block .table-scroll { max-width:100%; overflow-x:auto; margin:10px 0; }
.reader-block table { width:max-content; min-width:100%; border-collapse:collapse;
                      font-variant-numeric:tabular-nums; }
.reader-block th, .reader-block td { padding:7px 10px; border:1px solid var(--line);
                                     vertical-align:top; text-align:left; }
.reader-block th { background:var(--raised); }
.reader-block td { overflow-wrap:normal; word-break:normal; }
.reader-block code { white-space:pre-wrap; }
.footer { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-top:14px; }
.footer .grow { flex:1; }
dialog { min-width:min(560px,92vw); padding:20px; background:var(--surface);
         color:var(--text); border:1px solid var(--line); border-radius:var(--radius); }
dialog::backdrop { background:rgba(0,0,0,.6); }
dialog h2 { margin:0 0 12px; font-size:16px; }
@media (max-width:640px) {
  main { padding:14px 14px 4px; }
  .brand-logo { width:36px; height:36px; border-radius:9px; }
  .brand-name { font-size:20px; }
  .brand-tagline { display:none; }
  .tabs { gap:6px; margin-top:12px; padding:0 2px; }
  .page { padding-top:18px; }
  .card, .step, fieldset, .queue-panel { padding:16px; }
  .step-body { padding-left:0; }
  .row { grid-template-columns:1fr; }
  .row > label:first-child, .row > .row-label { text-align:left; }
  .job-row { grid-template-columns:1fr; }
  .player-heading { flex-direction:column; align-items:stretch; }
  /* Table rows become compact list items; cells keep their order. */
  .data-table thead { display:none; }
  .data-table tr { display:grid; gap:4px 12px; padding:12px 4px;
                   border-bottom:1px solid var(--surface); }
  .data-table td { display:block; width:auto !important; padding:0; border:0; }
  .voice-table tr { grid-template-columns:auto minmax(0,1fr) auto; align-items:center; }
  .voice-table .preview { grid-row:1 / span 2; }
  .voice-table .name, .voice-table .description { grid-column:2; }
  .voice-table .description { color:var(--dim); font-size:14px; }
  .voice-table .select { grid-column:3; grid-row:1 / span 2; }
  .book-table tr { grid-template-columns:minmax(0,1fr) auto; }
  .book-table .title, .book-table .duration, .book-table .source { grid-column:1; }
  .book-table .duration, .book-table .source { font-size:13px; }
  .book-table td[data-label]::before { content:attr(data-label) ": "; }
  .book-table .action { grid-column:2; grid-row:1 / span 3; align-self:center; }
  .voice-table .select, .book-table .action { display:flex; flex-direction:column;
                                             align-items:flex-end; gap:2px; }
  .voice-table .select > * + *, .book-table .action > * + * { margin-left:0; }
}
@media (max-width:640px), (pointer:coarse) {
  /* Touch targets stay at least 44px tall. */
  button, .tab-list [role=tab], .tabs .advanced, button.link, .job-row button,
  input[type=text], input[type=search], input[type=number], select, label.check {
    min-height:44px;
  }
  details > summary { padding:11px 0; }
  .preview-button { width:44px; height:44px; }
}
</style>
</head>
<body>
<main>
  <header class="app-header">
    <img class="brand-logo" src="/hilde-dark.png" width="48" height="48" alt="">
    <div>
      <h1 class="brand-name">Hilde</h1>
      <p class="brand-tagline">Audiobook Studio</p>
    </div>
  </header>

  <nav class="tabs" aria-label="Main">
    <div id="tab-list" class="tab-list" role="tablist" aria-label="Pages">
      <button id="tab-audiobook" type="button" role="tab" aria-controls="page-audiobook"
        onclick="setTab('audiobook')">Create</button>
      <button id="tab-voice" type="button" role="tab" aria-controls="page-voice"
        onclick="setTab('voice')">Voices</button>
      <button id="tab-player" type="button" role="tab" aria-controls="page-player"
        onclick="setTab('player')">Listen</button>
    </div>
    <button id="advanced" class="advanced" type="button" aria-expanded="false"
      aria-controls="advanced-panels" onclick="toggleAdvanced()">Advanced</button>
  </nav>
  <p id="status" class="notice" role="status" aria-live="polite"></p>

  <div id="advanced-panels" class="advanced-panels hidden">
    <fieldset>
      <legend>This server</legend>
      <div class="row"><span class="row-label">Narration workers</span>
        <div class="stack"><span id="compute-workers" class="worker-list"></span>
          <span id="compute-detail" class="note"></span></div></div>
      <div class="row"><span class="row-label">Models</span>
        <span id="compute-models"></span></div>
    </fieldset>
    <fieldset>
      <legend>Speech tuning</legend>
      <div class="row"><label for="dtype">Inference tuning</label><div class="line">
        <select id="dtype"><option>auto</option><option>float32</option>
          <option>float16</option><option>bfloat16</option></select>
        <select id="attn" aria-label="Attention implementation"><option>sdpa</option>
          <option>eager</option><option>flash_attention_2</option></select>
      </div></div>
      <div class="row"><label for="language">Text</label><div class="line">
        <input id="language" type="text" placeholder="Auto" aria-label="Language">
        <input id="encoding" type="text" placeholder="utf-8-sig" aria-label="Text encoding">
        <input id="seed" type="text" placeholder="Optional seed" aria-label="Seed">
      </div></div>
    </fieldset>
    <fieldset id="narration-advanced">
      <legend>Narration</legend>
      <div class="row"><label for="chunk-max-chars">Chunking</label><div class="line">
        <input id="chunk-max-chars" type="text" style="flex:0 0 90px">
        <label class="note" for="batch-size">Batch size</label>
        <input id="batch-size" type="text" style="flex:0 0 90px">
      </div></div>
      <div class="row"><label for="mp3-level">MP3 compression</label><div class="line">
        <input id="mp3-level" type="text" style="flex:0 0 90px">
      </div></div>
    </fieldset>
    <fieldset id="adaptation-advanced">
      <legend>Text adaptation</legend>
      <div class="row"><label for="paper-model">Model</label><div class="line">
        <select id="paper-model"><option value="">Loading OMP models…</option></select>
        <button id="paper-openai" type="button" onclick="openPaperOpenAI()">OpenAI</button>
        <button id="paper-local" type="button" onclick="openPaperLocal()">Add local</button>
        <input id="paper-local-server" type="hidden">
        <input id="paper-local-provider" type="hidden">
        <span id="paper-model-status" class="note"></span>
      </div></div>
      <div class="row"><label for="paper-in-flight">Workers</label><div class="line">
        <input id="paper-in-flight" type="number" min="1" max="32" step="1">
        <label class="note" for="paper-paragraphs-per-worker">Paragraphs per worker</label>
        <input id="paper-paragraphs-per-worker" type="number" min="1" max="32" step="1">
      </div></div>
    </fieldset>
    <fieldset id="voice-advanced">
      <legend>Voice files</legend>
      <div class="row"><label for="wav-subtype">Reference WAV encoding</label><div class="line">
        <select id="wav-subtype"><option>FLOAT</option><option>PCM_16</option>
          <option>PCM_24</option><option>PCM_32</option><option>DOUBLE</option></select>
        <span class="note">The reference clip is what narration clones.</span>
      </div></div>
    </fieldset>
  </div>

  <section id="page-audiobook" class="page" role="tabpanel" aria-labelledby="tab-audiobook">
    <h2 class="visually-hidden">Create an audiobook</h2>
    <div id="create-banner" class="banner hidden">
      <span id="create-banner-text"></span>
      <button class="link" type="button" onclick="viewProgress()">View progress</button>
    </div>
    <ol id="create-steps" class="steps">
      <li id="step-book" class="step">
        <div class="step-head">
          <span class="step-marker" aria-hidden="true">1</span>
          <h3 class="step-title" tabindex="-1">Add your book</h3>
          <span class="visually-hidden step-state"></span>
          <span id="book-summary" class="step-summary"></span>
          <button id="book-change" class="link" type="button" aria-label="Change book"
            onclick="setStep('book')">Change</button>
        </div>
        <div class="step-body">
          <div class="field">
            <label for="document">Your books</label>
            <div class="line">
              <select id="document"><option value="">Choose a book</option></select>
              <button id="document-delete" class="link" type="button"
                aria-label="Delete the chosen book" onclick="deleteDocument(this)">Delete</button>
            </div>
          </div>
          <div class="line">
            <input id="document-file" class="hidden" type="file"
              accept=".pdf,.txt,.text,.md,.markdown,application/pdf,text/plain,text/markdown">
            <button id="document-upload" type="button" onclick="chooseDocument()">Upload a file…</button>
            <span class="note">PDF, Markdown, or plain text</span>
          </div>
          <details id="link-details" class="from-link">
            <summary>Add from a link</summary>
            <div class="field">
              <label for="source-url">Link to a PDF, Markdown, or text file</label>
              <input id="source-url" type="text" inputmode="url"
                placeholder="https://arxiv.org/pdf/2508.21433">
            </div>
            <div class="field">
              <label for="download-name">Save as (optional)</label>
              <input id="download-name" type="text" placeholder="For example paper.pdf">
            </div>
          </details>
          <div class="actions">
            <button id="book-continue" class="primary" type="button"
              onclick="continueFromBook()">Continue</button>
          </div>
        </div>
      </li>
      <li id="step-voice" class="step">
        <div class="step-head">
          <span class="step-marker" aria-hidden="true">2</span>
          <h3 class="step-title" tabindex="-1">Choose a voice</h3>
          <span class="visually-hidden step-state"></span>
          <span id="voice-summary" class="step-summary"></span>
          <button id="voice-change" class="link" type="button" aria-label="Change voice"
            onclick="setStep('voice')">Change</button>
        </div>
        <div class="step-body">
          <div class="field" id="shared-voice-row">
            <label for="shared-voice">Voice</label>
            <div class="line">
              <select id="shared-voice"><option value="">Choose a voice</option></select>
              <button id="search-voices" type="button" onclick="searchVoices()">Search voices</button>
            </div>
          </div>
          <div class="field hidden" id="clone-voice-row">
            <label for="clone-voice">Server voice ID</label>
            <input id="clone-voice" type="text" list="server-voice-presets"
              placeholder="Preset or server voice ID">
          </div>
          <div id="selected-voice" class="voice-card hidden"></div>
          <div class="actions">
            <button id="voice-continue" class="primary" type="button"
              onclick="continueFromVoice()">Continue</button>
          </div>
        </div>
      </li>
      <li id="step-create" class="step">
        <div class="step-head">
          <span class="step-marker" aria-hidden="true">3</span>
          <h3 class="step-title" tabindex="-1">Create audiobook</h3>
          <span class="visually-hidden step-state"></span>
        </div>
        <div class="step-body">
          <div class="field">
            <label class="check"><input id="adapt" type="checkbox"> Adapt the text for listening</label>
            <span class="note">Rewrites the text so it sounds natural read aloud and leaves out
              reference lists. It takes longer.</span>
          </div>
          <p id="create-problem" class="problem hidden"></p>
          <div class="actions">
            <button id="run" class="primary" type="button"
              onclick="startAudiobook()">Create audiobook</button>
            <span id="output-name" class="note"></span>
          </div>
        </div>
      </li>
    </ol>

    <section id="create-progress" class="card hidden" aria-labelledby="progress-title">
      <h3 id="progress-title" tabindex="-1">Creating your audiobook</h3>
      <p id="progress-subject" class="note"></p>
      <ol id="stages" class="stages">
        <li class="stage" data-stage="read"><span class="stage-icon" aria-hidden="true"></span>Reading
          your document<span class="visually-hidden stage-state"></span></li>
        <li class="stage" data-stage="prepare"><span class="stage-icon" aria-hidden="true"></span>Preparing
          the narration<span class="visually-hidden stage-state"></span></li>
        <li class="stage" data-stage="audio"><span class="stage-icon" aria-hidden="true"></span>Creating
          the audio<span class="visually-hidden stage-state"></span></li>
        <li class="stage" data-stage="finish"><span class="stage-icon" aria-hidden="true"></span>Finishing
          your audiobook<span class="visually-hidden stage-state"></span></li>
      </ol>
      <div class="progress-meter">
        <progress id="progress" max="1" aria-labelledby="progress-title"
          aria-describedby="progress-detail progress-eta"></progress>
        <span id="progress-eta" class="note"></span>
      </div>
      <p id="progress-detail" class="note"></p>
      <div class="actions">
        <button id="stop" type="button" onclick="stopRun()">Stop</button>
        <button class="link" type="button" onclick="createAnother()">Start another audiobook</button>
      </div>
    </section>

    <section id="create-result" class="card hidden" aria-labelledby="result-title">
      <h3 id="result-title" tabindex="-1"></h3>
      <p id="result-text"></p>
      <details id="result-details" class="technical hidden">
        <summary>Technical details</summary><pre id="result-detail-text"></pre>
      </details>
      <div class="actions">
        <button id="result-primary" class="primary" type="button" onclick="resultAction()"></button>
        <button id="download" class="hidden" type="button" onclick="downloadArtifact()">Download MP3</button>
        <button id="airdrop" class="hidden" type="button" onclick="sendAirdrop()">AirDrop…</button>
        <button class="link" type="button" onclick="createAnother()">Create another audiobook</button>
      </div>
    </section>

    <section id="queue-panel" class="queue-panel hidden" aria-labelledby="queue-heading">
      <h3 id="queue-heading">In progress</h3>
      <div id="job-queue" class="job-queue"></div>
    </section>
  </section>

  <section id="page-voice" class="page hidden" role="tabpanel" aria-labelledby="tab-voice">
    <div class="page-head">
      <h2>Voices</h2>
      <button id="new-voice" type="button" aria-controls="voice-form" aria-expanded="false"
        onclick="openVoiceForm()">New voice</button>
    </div>
    <section id="voice-form" class="card hidden" aria-labelledby="voice-form-title">
      <div class="stack">
        <h3 id="voice-form-title">New voice</h3>
        <div class="field hidden" id="design-voice-row">
          <label for="design-voice">Server voice ID</label>
          <input id="design-voice" type="text" list="server-voice-presets"
            placeholder="Preset or server voice ID">
        </div>
        <div class="field">
          <label for="voice-name">Name</label>
          <input id="voice-name" type="text" autocomplete="off" placeholder="For example Rebecca">
          <span id="voice-exists" class="note"></span>
        </div>
        <div class="field">
          <label for="instruct">Prompt</label>
          <textarea id="instruct"
            placeholder="Warm British female narrator in her thirties; calm, clear, and unhurried."></textarea>
          <span class="note">Describe the speaker, tone, accent, pace, and delivery. Voices are
            found by searching their prompts, and every voice reads the same short passage.</span>
        </div>
        <p id="voice-problem" class="problem hidden"></p>
        <div id="voice-progress" class="progress-meter hidden">
          <progress aria-label="Creating the voice"></progress>
          <span class="note">Creating the voice…</span>
        </div>
        <div id="voice-result" class="voice-card hidden" tabindex="-1"></div>
        <div class="actions">
          <button id="voice-create" class="primary" type="button"
            onclick="createVoice()">Create voice</button>
          <button id="voice-stop" class="hidden" type="button" onclick="stopRun()">Stop</button>
          <button id="voice-close" type="button" onclick="closeVoiceForm()">Close</button>
        </div>
      </div>
    </section>
    <div class="search">
      <label for="voice-search" class="visually-hidden">Search voice prompts</label>
      <input id="voice-search" type="search" autocomplete="off"
        placeholder="Search prompts, for example warm british female">
      <span id="voice-count" class="note" aria-live="polite"></span>
    </div>
    <table id="voice-table" class="data-table voice-table">
      <caption class="visually-hidden">Voices</caption>
      <thead><tr><th scope="col">Preview</th><th scope="col">Voice name</th>
        <th scope="col">Prompt</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
      <tbody id="voice-rows"></tbody>
    </table>
    <p id="voice-none" class="empty hidden"></p>
    <button id="voice-more" class="more hidden" type="button"
      onclick="showMoreVoices()">Show more</button>
  </section>

  <div id="log"></div>

  <section id="page-player" class="page hidden" role="tabpanel" aria-labelledby="tab-player">
    <div id="library-view">
      <div class="page-head"><h2>Listen</h2></div>
      <div id="library-content" class="hidden">
        <div class="search">
          <label for="book-search" class="visually-hidden">Search titles</label>
          <input id="book-search" type="search" autocomplete="off"
            placeholder="Search titles, for example great expectations">
          <span id="book-count" class="note" aria-live="polite"></span>
        </div>
        <table id="book-table" class="data-table book-table">
          <caption class="visually-hidden">Audiobooks</caption>
          <thead><tr><th scope="col">Title</th><th scope="col">Duration</th>
            <th scope="col">Source name</th>
            <th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
          <tbody id="book-rows"></tbody>
        </table>
        <p id="book-none" class="empty hidden"></p>
        <button id="book-more" class="more hidden" type="button"
          onclick="showMoreBooks()">Show more</button>
      </div>
      <div id="library-empty" class="empty hidden">
        <h3>No audiobooks yet</h3>
        <p>Add a PDF, Markdown, or text file, choose a voice, and your audiobook will be
          waiting here.</p>
        <button class="primary" type="button" onclick="createFirstAudiobook()">Create your
          first audiobook</button>
      </div>
    </div>
    <div id="book-view" class="hidden">
      <button class="link back" type="button" onclick="closeBook()">← All audiobooks</button>
      <section class="player-panel" aria-labelledby="reader-title">
        <div class="player-heading">
          <div class="player-title">
            <h2 id="reader-title" tabindex="-1">Audiobook</h2>
            <span id="reader-meta" class="note"></span>
          </div>
          <div class="player-actions">
            <label class="check note"><input id="reader-follow" type="checkbox" checked>
              Follow along</label>
            <button type="button" onclick="downloadBook()">Download MP3</button>
          </div>
        </div>
      </section>
      <div id="artifact-player" class="player-controls"></div>
      <p id="reader-unavailable" class="notice hidden"></p>
      <section id="reader-panel" class="reader-panel hidden" aria-label="Text">
        <article id="reader-content" class="reader-content"></article>
      </section>
    </div>
  </section>

  <datalist id="server-voice-presets">
    <option value="alloy"><option value="ash"><option value="ballad">
    <option value="coral"><option value="echo"><option value="fable">
    <option value="nova"><option value="onyx"><option value="sage">
    <option value="shimmer"><option value="verse">
  </datalist>
</main>
<footer class="signature">by <img src="/zeki.jpg" width="16" height="16" alt=""> Zeki Works</footer>

<dialog id="paper-openai-dialog">
  <h2>OpenAI OAuth</h2>
  <p class="note">Sign in with ChatGPT. OMP stores and refreshes the credential
    in the server user's private credential database.</p>
  <div class="row"><label>Status:</label><div>
    <strong id="paper-openai-status"></strong>
    <div id="paper-openai-device" class="hidden">
      <p><a id="paper-openai-link" target="_blank" rel="noopener">Open sign-in</a></p>
      <p>Enter code: <code id="paper-openai-code"></code></p>
    </div>
  </div></div>
  <div class="footer">
    <button id="paper-openai-cancel" class="hidden" onclick="cancelPaperOpenAI()">Cancel login</button>
    <span class="grow"></span>
    <button onclick="closePaperOpenAI()">Close</button>
    <button id="paper-openai-start" class="primary" onclick="startPaperOpenAI()">Connect</button>
  </div>
</dialog>

<dialog id="paper-local-dialog">
  <h2>Local model server</h2>
  <div class="row"><label for="paper-local-type">Server type:</label><div class="line">
    <select id="paper-local-type">
      <option value="" disabled>Choose the server type</option>
      <option value="lm-studio">OpenAI-compatible (SGLang, vLLM, LM Studio)</option>
      <option value="ollama">Ollama</option>
    </select>
  </div></div>
  <div class="row"><label for="paper-local-url">Server:</label><div class="line">
    <input id="paper-local-url" type="text" placeholder="host:port">
  </div></div>
  <div id="paper-local-status" class="note"></div>
  <div class="footer">
    <button id="paper-local-remove" onclick="removePaperLocal()">Remove</button>
    <span class="grow"></span>
    <button onclick="closePaperLocal()">Cancel</button>
    <button id="paper-local-save" class="primary" onclick="savePaperLocal()">Use server</button>
  </div>
</dialog>

<script>
const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 50;
const STAGES = ["read", "prepare", "audio", "finish"];
// Playback from a clicked word, and its highlight, start up to this long before
// the aligned onset so a slightly late alignment never clips the word.
const READER_WORD_LEAD_SECONDS = 0.1;
const PLAY_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2.5v11L13 8z"/></svg>';
const PAUSE_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 3h3v10H4zm5 0h3v10H9z"/></svg>';
const CHECK_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.2 11.1 3 7.9 1.6 9.3l4.6 4.6 8.2-8.2-1.4-1.4z"/></svg>';
let state = null, facts = {}, assets = {}, caps = {}, jobs = [], runs = [], consumers = [];
let configuration = {}, voices = [], books = [];
let running = false, runKind = null;
let currentJobId = "", followedRunKey = "";
let syncTimer = null, syncSeq = 0, stream = null, paperOAuthTimer = null;
let paperOpenAIConnected = false, advancedOpen = false, voiceFormOpen = false;
// Create shows the steps (compose), a followed run (progress), or its outcome (result).
let submitting = false, createView = "compose", stopping = false, waitingJobId = "";
let runStage = null, recentLog = [], resultInfo = null, resultDetail = "";
let voiceResult = null, queueSignature = "";
let voiceLimit = PAGE_SIZE, bookLimit = PAGE_SIZE;
let etaSample = null, etaSecondsPerUnit = null, etaDeadline = null, etaTimer = null;
let etaPhase = null, etaPhaseStarted = null;
let readerAudio = null, readerCues = [], readerWordCues = [], readerSampleRate = 0;
let readerBlocks = [], readerWordElements = [], readerFrame = 0;
let activeReaderBlock = -1, activeReaderWord = -1;
// One shared player keeps hundreds of preview buttons cheap.
const previewAudio = new Audio();
let previewing = "";

const FIELDS = [
  ["dtype", "runtime.dtype"], ["attn", "runtime.attn"],
  ["language", "runtime.language"], ["encoding", "runtime.encoding"], ["seed", "runtime.seed"],
  ["design-voice", "voice.server_voice"], ["voice-name", "voice.name"],
  ["instruct", "voice.instruct"],
  ["wav-subtype", "voice.wav_subtype"], ["clone-voice", "audiobook.server_voice"],
  ["shared-voice", "audiobook.voice"], ["document", "audiobook.document"],
  ["source-url", "audiobook.source_url"], ["download-name", "audiobook.download_name"],
  ["paper-model", "audiobook.model"], ["paper-local-server", "audiobook.local_server"],
  ["paper-local-provider", "audiobook.local_provider"],
  ["paper-in-flight", "audiobook.in_flight"],
  ["paper-paragraphs-per-worker", "audiobook.paragraphs_per_worker"],
  ["chunk-max-chars", "audiobook.chunk_max_chars"], ["batch-size", "audiobook.batch_size"],
  ["mp3-level", "audiobook.mp3_level"],
];
const FLAGS = [["adapt", "audiobook.adapt"]];

function at(path) { const [group, key] = path.split("."); return state[group][key]; }
function put(path, value) { const [group, key] = path.split("."); state[group][key] = value; }
function clientFileName(path) {
  const parts = String(path || "").replace(/\\/g, "/").split("/");
  return parts[parts.length - 1];
}
function plural(count, noun) { return `${count} ${noun}${count === 1 ? "" : "s"}`; }

function searchWords(value) {
  return String(value || "").normalize("NFKD").replace(/\p{M}/gu, "")
    .toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
}
function matchesAll(words, terms) {
  // AND search: every term starts some word, in any order, ignoring case.
  return terms.every((term) => words.some((word) => word.startsWith(term)));
}

function fillAssetSelect(id, values, selected, placeholder) {
  const select = $(id);
  select.replaceChildren();
  const empty = document.createElement("option");
  empty.value = ""; empty.textContent = placeholder; select.append(empty);
  for (const value of values || []) {
    const option = document.createElement("option");
    option.value = value; option.textContent = value; select.append(option);
  }
  if (selected && !(values || []).includes(selected)) {
    const missing = document.createElement("option");
    missing.value = selected; missing.textContent = `${selected} (missing)`;
    select.append(missing);
  }
  select.value = selected || "";
}

function populateAssets() {
  fillAssetSelect("document", assets.documents, state.audiobook.document, "Choose a book");
  fillAssetSelect("shared-voice", assets.voices, state.audiobook.voice, "Choose a voice");
}

function load(data) {
  state = data.state; facts = data.derived; assets = data.assets || assets;
  caps = data.capabilities || caps; jobs = data.queue || [];
  configuration = data.configuration || configuration;
  runs = data.runs || []; consumers = data.consumers || [];
  const model = $("paper-model");
  if (state.audiobook.model &&
      ![...model.options].some((option) => option.value === state.audiobook.model)) {
    const saved = document.createElement("option");
    saved.value = state.audiobook.model;
    saved.textContent = `${state.audiobook.model} (saved)`;
    model.append(saved);
  }
  for (const [id, path] of FIELDS) $(id).value = at(path);
  for (const [id, path] of FLAGS) $(id).checked = !!at(path);
  $("link-details").open = !!state.audiobook.source_url;
  populateAssets();
  const activeRun = runs.find((item) => item.active) || null;
  // A refresh during an audiobook run returns to its progress.
  if (activeRun && activeRun.kind === "audiobook") createView = "progress";
  if (activeRun) followActive(activeRun, true);
  else render();
  refreshVoices();
  refreshLibrary(true);
}

function collect() {
  for (const [id, path] of FIELDS) put(path, $(id).value);
  for (const [id, path] of FLAGS) put(path, $(id).checked);
}

async function jsonRequest(path, options) {
  const response = await fetch(path, options);
  const answer = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(answer.error || response.statusText);
  return answer;
}

async function sync() {
  collect();
  const sent = ++syncSeq;
  try {
    const answer = await jsonRequest("/api/sync", {
      method:"POST", headers:{ "Content-Type":"application/json" },
      body:JSON.stringify({ state }),
    });
    // A newer local change supersedes this echo; its own sync follows.
    if (sent !== syncSeq) return;
    state = answer.state; facts = answer.derived; assets = answer.assets || assets;
    populateAssets(); render();
  } catch (error) {
    setStatus(error.message, true);
  }
}
function queueSync() { syncSeq++; clearTimeout(syncTimer); syncTimer = setTimeout(sync, 250); }

function chooseDocument() { $("document-file").click(); }
async function uploadDocument() {
  const file = $("document-file").files[0];
  if (!file) return;
  $("document-upload").disabled = true;
  setStatus(`Uploading ${file.name}…`);
  try {
    const response = await fetch(
      "/api/documents/upload?name=" + encodeURIComponent(file.name),
      { method:"POST", headers:{ "Content-Type":"application/octet-stream" }, body:file }
    );
    const answer = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(answer.error || "The file couldn't be uploaded.");
    assets = answer.assets || assets;
    state.audiobook.document = answer.name;
    state.audiobook.source_url = "";
    state.audiobook.download_name = "";
    $("source-url").value = "";
    $("download-name").value = "";
    populateAssets();
    setStatus(`Added ${answer.name}.`);
    await sync();
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    $("document-file").value = "";
    $("document-upload").disabled = false;
  }
}

async function materializeUrl() {
  if (!state.audiobook.source_url.trim()) return true;
  setStatus("Downloading your document…");
  try {
    const answer = await jsonRequest("/api/documents/download", {
      method:"POST", headers:{ "Content-Type":"application/json" },
      body:JSON.stringify({
        url:state.audiobook.source_url,
        name:state.audiobook.download_name,
      }),
    });
    assets = answer.assets || assets;
    state.audiobook.document = answer.name;
    state.audiobook.source_url = "";
    state.audiobook.download_name = "";
    $("source-url").value = "";
    $("download-name").value = "";
    populateAssets();
    setStatus(`Added ${answer.name}.`);
    await sync();
    return true;
  } catch (error) {
    setStatus(error.message, true);
    return false;
  }
}

function setStatus(text, bad) {
  $("status").textContent = text || "";
  $("status").classList.toggle("bad", !!bad);
}

function formatEtaDuration(seconds) {
  const whole = Math.max(1, Math.ceil(seconds));
  if (whole >= 3600) {
    const totalMinutes = Math.ceil(whole / 60);
    const hours = Math.floor(totalMinutes / 60), minutes = totalMinutes % 60;
    return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  if (whole >= 60)
    return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2,"0")}s`;
  return `${whole}s`;
}

function phaseHistory(phase) {
  try { return Number(localStorage.getItem(`audiobook-eta-${phase}`)); }
  catch (_) { return NaN; }
}
function rememberPhaseDuration() {
  if (!etaPhase || etaPhaseStarted === null) return;
  const seconds = (performance.now() - etaPhaseStarted) / 1000;
  if (!(seconds > 1 && seconds < 86400)) return;
  const previous = phaseHistory(etaPhase);
  const value = Number.isFinite(previous) ? previous * 0.65 + seconds * 0.35 : seconds;
  try { localStorage.setItem(`audiobook-eta-${etaPhase}`, String(value)); }
  catch (_) {}
}
function renderEta() {
  if (!running) { $("progress-eta").textContent = ""; return; }
  if (etaDeadline === null) {
    $("progress-eta").textContent =
      etaSample && etaSample.done >= etaSample.total ? "Almost done…" : "Estimating time…";
    return;
  }
  const remaining = (etaDeadline - performance.now()) / 1000;
  $("progress-eta").textContent =
    remaining > 0.5 ? `About ${formatEtaDuration(remaining)} left` : "Almost done…";
}
function beginEta(phase) {
  if (etaPhase && etaPhase !== phase) rememberPhaseDuration();
  clearInterval(etaTimer);
  etaPhase = phase; etaPhaseStarted = performance.now();
  etaSample = null; etaSecondsPerUnit = null; etaDeadline = null;
  const historical = phaseHistory(phase);
  if (Number.isFinite(historical) && historical > 0)
    etaDeadline = performance.now() + historical * 1000;
  renderEta();
  etaTimer = setInterval(renderEta, 1000);
}
function endEta() {
  rememberPhaseDuration();
  clearInterval(etaTimer);
  etaTimer = null; etaSample = null; etaSecondsPerUnit = null;
  etaDeadline = null; etaPhase = null; etaPhaseStarted = null;
  $("progress-eta").textContent = "";
}
function updateEta(done, total, elapsed, streamElapsed) {
  done = Number(done); total = Number(total); elapsed = Number(elapsed);
  streamElapsed = Number(streamElapsed);
  if (![done,total,elapsed].every(Number.isFinite) || total <= 0) return;
  const sample = { done,total,elapsed };
  if (!etaSample || etaSample.total !== total ||
      done < etaSample.done || elapsed < etaSample.elapsed) {
    etaSample = sample; etaSecondsPerUnit = null; renderEta(); return;
  }
  const advanced = done - etaSample.done, duration = elapsed - etaSample.elapsed;
  if (advanced > 0 && duration > 0) {
    const observed = duration / advanced;
    etaSecondsPerUnit = etaSecondsPerUnit === null
      ? observed : etaSecondsPerUnit * 0.65 + observed * 0.35;
    const stale = Number.isFinite(streamElapsed) ? Math.max(0, streamElapsed - elapsed) : 0;
    const remaining = Math.max(0, etaSecondsPerUnit * Math.max(0,total-done) - stale);
    etaDeadline = performance.now() + remaining * 1000;
  }
  etaSample = sample; renderEta();
}

function readerPlayer(name) {
  // Browsers seek VBR MP3 through a coarse table and then report the requested
  // time for audio from elsewhere. The MP4 index maps every frame exactly; the
  // MP3 remains a fallback for browsers without MP3-in-MP4 playback.
  const audio = document.createElement("audio");
  audio.controls = true;
  audio.preload = "metadata";
  const url = "/api/audio?name=" + encodeURIComponent(name) + "&v=" + Date.now();
  for (const [src, type] of [[url + "&container=mp4", "audio/mp4"], [url, "audio/mpeg"]]) {
    const source = document.createElement("source");
    source.src = src; source.type = type;
    audio.append(source);
  }
  return audio;
}

function clearReader() {
  cancelAnimationFrame(readerFrame); readerFrame = 0;
  readerAudio = null; readerCues = []; readerWordCues = [];
  readerSampleRate = 0; readerBlocks = []; readerWordElements = [];
  activeReaderBlock = -1; activeReaderWord = -1;
  // Removing the audio element also stops its playback.
  $("artifact-player").replaceChildren();
  $("reader-content").replaceChildren();
  $("reader-panel").classList.add("hidden");
  $("reader-unavailable").classList.add("hidden");
}

function readerCueAt(sample) {
  let low = 0, high = readerCues.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1, cue = readerCues[middle];
    if (sample < cue.start_sample) high = middle - 1;
    else if (sample >= cue.end_sample) low = middle + 1;
    else return cue;
  }
  return readerCues.length && sample >= readerCues[readerCues.length - 1].end_sample
    ? readerCues[readerCues.length - 1] : null;
}

function readerWordCueAt(sample) {
  // The spoken word, or the next one when its onset is within the lead.
  let low = 0, high = readerWordCues.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (readerWordCues[middle].end_sample <= sample) low = middle + 1;
    else high = middle;
  }
  const cue = readerWordCues[low];
  return cue && cue.start_sample - sample <= READER_WORD_LEAD_SECONDS * readerSampleRate
    ? cue : null;
}

function readerWordSeekSample(cue) {
  // Start in the pause before the word, but not inside the previous word or sentence.
  const sentence = readerCues.find((candidate) => candidate.block === cue.block);
  const previous = readerWordCues[cue.position - 1];
  const floor = Math.max(
    sentence ? sentence.start_sample : 0,
    previous ? previous.end_sample : 0,
  );
  const lead = Math.round(READER_WORD_LEAD_SECONDS * readerSampleRate);
  return Math.min(cue.start_sample, Math.max(floor, cue.start_sample - lead));
}

function followReaderAudio() {
  // timeupdate fires only a few times per second; words need frame-rate updates.
  readerFrame = 0;
  if (!readerAudio || readerAudio.paused) return;
  updateReaderHighlight();
  readerFrame = requestAnimationFrame(followReaderAudio);
}

function comparableReaderWord(value) {
  return value.normalize("NFKD").replace(/\p{M}/gu, "")
    .replaceAll("’", "'").toLocaleLowerCase();
}

function wrapReaderWords(parts, cues) {
  if (!cues.length) return;
  const tokens = [];
  const pattern = /[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)*/gu;
  for (const part of parts) {
    const walker = document.createTreeWalker(part, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      for (const match of node.data.matchAll(pattern)) {
        tokens.push({
          node,
          start:match.index,
          end:match.index + match[0].length,
          text:match[0],
        });
      }
    }
  }
  const assignments = new Map();
  let cursor = 0;
  for (const cue of cues) {
    const target = comparableReaderWord(cue.text);
    let tokenIndex = -1;
    const searchEnd = Math.min(tokens.length, cursor + 12);
    for (let index = cursor; index < searchEnd; index += 1) {
      if (comparableReaderWord(tokens[index].text) === target) {
        tokenIndex = index;
        break;
      }
    }
    if (tokenIndex < 0 && cursor < tokens.length) tokenIndex = cursor;
    if (tokenIndex < 0) continue;
    const token = tokens[tokenIndex];
    cursor = tokenIndex + 1;
    if (!assignments.has(token.node)) assignments.set(token.node, []);
    assignments.get(token.node).push({token, cue});
  }
  for (const [node, values] of assignments) {
    values.sort((left, right) => right.token.start - left.token.start);
    for (const {token, cue} of values) {
      node.splitText(token.end);
      const middle = node.splitText(token.start);
      const span = document.createElement("span");
      span.className = "reader-word";
      span.dataset.cue = cue.position;
      span.textContent = middle.data;
      middle.replaceWith(span);
      readerWordElements[cue.position] = span;
    }
  }
}

function seekReaderBlock(event, block) {
  if (event.target.closest("a") || !readerAudio) return;
  const word = readerWordCues[event.target.closest(".reader-word")?.dataset.cue];
  const sample = word
    ? readerWordSeekSample(word)
    : readerCues.find((candidate) => candidate.block === block)?.start_sample;
  if (sample === undefined) return;
  readerAudio.currentTime = sample / readerSampleRate;
  updateReaderHighlight();
}

function renderReaderBlocks(blocks, wordCuesByBlock) {
  // Sentences of one narration paragraph flow as inline spans in a shared <p>
  // or heading; lists, tables, and images stay blocks. Every part of a
  // sentence shares its cue, click target, and highlight.
  const content = $("reader-content");
  const parts = [];
  let paragraph = null, paragraphId = null, line = null;
  for (const item of blocks) {
    if (!paragraph || item.paragraph !== paragraphId) {
      paragraph = document.createElement("section");
      paragraph.className = "reader-paragraph";
      content.append(paragraph);
      paragraphId = item.paragraph; line = null;
    }
    const template = document.createElement("template");
    template.innerHTML = item.html;
    const body = template.content;
    for (const table of [...body.querySelectorAll("table")]) {
      const wrapper = document.createElement("div");
      wrapper.className = "table-scroll";
      table.before(wrapper); wrapper.append(table);
    }
    for (const image of body.querySelectorAll("img")) image.loading = "lazy";
    for (const link of body.querySelectorAll("a")) {
      link.target = "_blank"; link.rel = "noopener";
    }
    const own = [];
    let visual = null;
    for (const [index, element] of [...body.children].entries()) {
      if (!/^(P|H[1-6])$/.test(element.tagName) || element.querySelector("img")) {
        if (!visual) {
          visual = document.createElement("div");
          visual.className = "reader-block";
          paragraph.append(visual); own.push(visual);
        }
        visual.append(element); line = null;
        continue;
      }
      visual = null;
      const sentence = document.createElement("span");
      sentence.className = "reader-sentence";
      sentence.append(...element.childNodes);
      // Continue the open line unless this starts a heading or a second
      // Markdown paragraph inside the sentence.
      if (line && !index && element.tagName === "P") line.append(" ", sentence);
      else {
        element.append(sentence);
        paragraph.append(element);
        line = element;
      }
      own.push(sentence);
    }
    wrapReaderWords(own, wordCuesByBlock.get(item.id) || []);
    for (const part of own)
      part.addEventListener("click", (event) => seekReaderBlock(event, item.id));
    parts[item.id] = own;
  }
  return parts;
}

function updateReaderHighlight() {
  if (!readerAudio || !readerSampleRate) return;
  const sample = Math.round(readerAudio.currentTime * readerSampleRate);
  const candidateWordCue = readerWordCueAt(sample);
  const sentenceCue = readerCueAt(sample);
  const nextBlock = sentenceCue
    ? sentenceCue.block : candidateWordCue ? candidateWordCue.block : -1;
  const wordCue = candidateWordCue && candidateWordCue.block === nextBlock
    ? candidateWordCue : null;
  const blockChanged = nextBlock !== activeReaderBlock;
  if (blockChanged) {
    const previous = readerBlocks[activeReaderBlock] || [];
    for (const part of previous) part.classList.remove("active");
    previous[0]?.closest(".reader-paragraph").classList.remove("active");
    activeReaderBlock = nextBlock;
    const parts = readerBlocks[nextBlock] || [];
    for (const part of parts) part.classList.add("active");
    parts[0]?.closest(".reader-paragraph").classList.add("active");
    if (parts.length && $("reader-follow").checked && !readerAudio.paused) {
      parts[0].scrollIntoView({
        block:"center",
        behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto" : "smooth",
      });
    }
  }
  const nextWord = wordCue ? wordCue.position : -1;
  if (nextWord === activeReaderWord) return;
  if (activeReaderWord >= 0 && readerWordElements[activeReaderWord])
    readerWordElements[activeReaderWord].classList.remove("active");
  activeReaderWord = nextWord;
  if (nextWord >= 0 && readerWordElements[nextWord])
    readerWordElements[nextWord].classList.add("active");
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "Unknown";
  if (seconds < 59.5) return `${Math.round(seconds)} s`;
  const total = Math.round(seconds / 60);
  const hours = Math.floor(total / 60), minutes = total % 60;
  if (!hours) return `${minutes} min`;
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
}

function bookTitle(name) {
  return books.find((book) => book.name === name)?.title || name;
}

async function openAudiobook(name, focus) {
  if (!name) return;
  stopPreview();
  clearReader();
  state.player.book = name;
  state.tab = "player";
  render(); queueSync();
  const book = books.find((item) => item.name === name);
  const details = book
    ? [book.voice && `Read by ${book.voice}`, formatDuration(book.duration), book.source]
    : [];
  $("reader-title").textContent = book ? book.title : name;
  $("reader-meta").textContent = details.filter(Boolean).join(" · ");
  if (focus) $("reader-title").focus();
  const audio = readerPlayer(name);
  $("artifact-player").replaceChildren(audio);
  readerAudio = audio;
  audio.addEventListener("timeupdate", updateReaderHighlight);
  audio.addEventListener("seeking", updateReaderHighlight);
  audio.addEventListener("play", () => {
    if (!readerFrame) readerFrame = requestAnimationFrame(followReaderAudio);
  });
  try {
    const payload = await jsonRequest(
      "/api/reader?name=" + encodeURIComponent(name)
    );
    if (readerAudio !== audio) return;
    readerCues = payload.cues || [];
    readerWordCues = (payload.word_cues || []).map(
      (cue, position) => ({...cue, position})
    );
    readerWordElements = Array(readerWordCues.length);
    const wordCuesByBlock = new Map();
    for (const cue of readerWordCues) {
      if (!wordCuesByBlock.has(cue.block))
        wordCuesByBlock.set(cue.block, []);
      wordCuesByBlock.get(cue.block).push(cue);
    }
    readerSampleRate = Number(payload.sample_rate) || 0;
    readerBlocks = renderReaderBlocks(payload.blocks || [], wordCuesByBlock);
    details.push(payload.word_timing !== "unavailable"
      ? "Words highlight as they're read"
      : payload.timing_precision === "estimated"
      ? "Sentences highlight approximately"
      : "Sentences highlight as they're read");
    $("reader-meta").textContent = details.filter(Boolean).join(" · ");
    $("reader-panel").classList.remove("hidden");
    updateReaderHighlight();
  } catch (_) {
    if (readerAudio !== audio) return;
    $("reader-unavailable").textContent =
      "The text for this audiobook isn't available, but you can still listen.";
    $("reader-unavailable").classList.remove("hidden");
  }
}

function closeBook() {
  clearReader();
  state.player.book = "";
  render(); queueSync();
  $("book-search").focus();
}

function downloadBook() {
  if (state.player.book)
    location.href = "/api/download?asset=" + encodeURIComponent(state.player.book);
}

async function refreshLibrary(restore) {
  try {
    const answer = await jsonRequest("/api/library");
    books = (answer.books || []).map((book) => ({...book, words: searchWords(book.title)}));
  } catch (error) {
    setStatus(error.message, true);
    return;
  }
  renderLibrary(); renderResult();
  if (!restore || !state.player.book) return;
  // Reopen the book that was open before a refresh, unless it is gone.
  if (!books.some((book) => book.name === state.player.book)) {
    state.player.book = ""; render(); queueSync();
  } else if (state.tab === "player" && !readerAudio) {
    openAudiobook(state.player.book);
  }
}

function renderLibrary() {
  $("library-empty").classList.toggle("hidden", books.length > 0);
  $("library-content").classList.toggle("hidden", books.length === 0);
  const terms = searchWords($("book-search").value);
  const matches = books.filter((book) => matchesAll(book.words, terms));
  $("book-rows").replaceChildren(...matches.slice(0, bookLimit).map(bookRow));
  $("book-table").classList.toggle("hidden", matches.length === 0);
  $("book-none").classList.toggle("hidden", matches.length > 0);
  $("book-none").textContent = `No titles contain all of: ${terms.join(" ")}`;
  const hidden = Math.max(0, matches.length - bookLimit);
  $("book-more").classList.toggle("hidden", hidden === 0);
  $("book-more").textContent = `Show ${Math.min(hidden, PAGE_SIZE)} more`;
  $("book-count").textContent = terms.length
    ? `${matches.length} of ${plural(books.length, "audiobook")}`
    : plural(books.length, "audiobook");
}
function showMoreBooks() { bookLimit += PAGE_SIZE; renderLibrary(); }

function cell(className, label) {
  const node = document.createElement("td");
  node.className = className;
  if (label) node.dataset.label = label;
  return node;
}

function bookRow(book) {
  const row = document.createElement("tr");
  const title = cell("title");
  const name = document.createElement("div");
  name.className = "book-title"; name.textContent = book.title;
  title.append(name);
  if (book.voice) {
    const narrator = document.createElement("div");
    narrator.className = "note"; narrator.textContent = `Read by ${book.voice}`;
    title.append(narrator);
  }
  const duration = cell("duration", "Duration");
  duration.textContent = formatDuration(book.duration);
  const source = cell("source", "Source");
  source.textContent = book.source || "Unknown";
  const action = cell("action");
  const listen = document.createElement("button");
  listen.type = "button"; listen.textContent = "Listen";
  listen.setAttribute("aria-label", `Listen to ${book.title}`);
  listen.addEventListener("click", () => openAudiobook(book.name, true));
  action.append(listen, deleteButton(`Delete ${book.title}`, (button) => deleteBook(book, button)));
  row.append(title, duration, source, action);
  return row;
}

async function refreshVoices() {
  try {
    const answer = await jsonRequest("/api/voices");
    voices = (answer.voices || []).map(
      (voice) => ({...voice, words: searchWords(voice.description)})
    );
  } catch (error) {
    setStatus(error.message, true);
    return;
  }
  renderVoiceTable(); renderSelectedVoice(); renderVoiceForm();
}
function voiceByName(name) { return voices.find((voice) => voice.name === name) || null; }

function renderVoiceTable() {
  const terms = searchWords($("voice-search").value);
  const matches = voices.filter((voice) => matchesAll(voice.words, terms));
  $("voice-rows").replaceChildren(...matches.slice(0, voiceLimit).map(voiceRow));
  $("voice-table").classList.toggle("hidden", matches.length === 0);
  $("voice-none").classList.toggle("hidden", matches.length > 0);
  $("voice-none").textContent = voices.length
    ? `No voice prompts contain all of: ${terms.join(" ")}`
    : "No voices yet. Use New voice to create the first one.";
  const hidden = Math.max(0, matches.length - voiceLimit);
  $("voice-more").classList.toggle("hidden", hidden === 0);
  $("voice-more").textContent = `Show ${Math.min(hidden, PAGE_SIZE)} more`;
  $("voice-count").textContent = terms.length
    ? `${matches.length} of ${plural(voices.length, "voice")}`
    : plural(voices.length, "voice");
}
function showMoreVoices() { voiceLimit += PAGE_SIZE; renderVoiceTable(); }

function voiceRow(voice) {
  const selected = !facts.clone_server && voice.name === state.audiobook.voice;
  const row = document.createElement("tr");
  if (selected) row.className = "is-selected";
  const preview = cell("preview");
  preview.append(previewButton(voice));
  const name = cell("name");
  name.textContent = voice.name;
  const description = cell("description");
  const text = document.createElement("div");
  text.className = voice.description ? "clamp" : "clamp note";
  text.textContent = voice.description || "No prompt saved";
  description.append(text);
  if (!voice.comparable) {
    const older = document.createElement("div");
    older.className = "note";
    older.textContent = "Preview reads an older passage";
    description.append(older);
  }
  const select = cell("select");
  if (selected) {
    const badge = document.createElement("span");
    badge.className = "selected-badge";
    badge.innerHTML = CHECK_ICON;
    badge.append("Selected");
    select.append(badge);
  } else if (!facts.clone_server) {
    const button = document.createElement("button");
    button.type = "button"; button.textContent = "Select";
    button.setAttribute("aria-label", `Select ${voice.name}`);
    button.addEventListener("click", () => selectVoice(voice.name));
    select.append(button);
  }
  select.append(deleteButton(`Delete ${voice.name}`, (button) => deleteVoice(voice.name, button)));
  row.append(preview, name, description, select);
  return row;
}

function previewButton(voice) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "preview-button";
  button.dataset.voice = voice.name;
  button.setAttribute("aria-label", `Preview ${voice.name}`);
  button.addEventListener("click", () => togglePreview(voice.name, voice.preview));
  setPreviewButton(button);
  return button;
}
function setPreviewButton(button) {
  const playing = button.dataset.voice === previewing && !previewAudio.paused;
  button.innerHTML = playing ? PAUSE_ICON : PLAY_ICON;
  button.setAttribute("aria-pressed", String(playing));
}
function syncPreviewButtons() {
  for (const button of document.querySelectorAll(".preview-button")) setPreviewButton(button);
}
function togglePreview(name, version) {
  if (previewing === name && !previewAudio.paused) { previewAudio.pause(); return; }
  previewing = name;
  previewAudio.src = `/api/voices/preview?name=${encodeURIComponent(name)}` +
    `&v=${encodeURIComponent(version)}`;
  previewAudio.play().catch((error) => {
    // A newer click replaced or paused this clip before it started.
    if (error.name === "AbortError" || previewing !== name) return;
    previewing = ""; syncPreviewButtons();
    setStatus(`The preview of ${name} couldn't be played.`, true);
  });
  syncPreviewButtons();
}
function stopPreview() {
  previewAudio.pause(); previewing = ""; syncPreviewButtons();
}

// Deleting asks first; the button stays disabled while the server removes the asset.
async function deleteAsset(kind, name, question, button) {
  if (!window.confirm(question)) return null;
  button.disabled = true;
  try {
    return await jsonRequest(`/api/${kind}/delete`, {
      method:"POST", headers:{ "Content-Type":"application/json" },
      body:JSON.stringify({ name }),
    });
  } catch (error) {
    setStatus(error.message, true);
    return null;
  } finally {
    button.disabled = false;
  }
}
function deleteButton(label, onDelete) {
  const button = document.createElement("button");
  button.type = "button"; button.className = "link"; button.textContent = "Delete";
  button.setAttribute("aria-label", label);
  button.addEventListener("click", () => onDelete(button));
  return button;
}
async function deleteVoice(name, button) {
  const answer = await deleteAsset("voices", name,
    `Delete the voice ${name}? Its reference clip and preview are removed for good.`, button);
  if (!answer) return;
  if (previewing === name) stopPreview();
  if (voiceResult && voiceResult.name === name) voiceResult = null;
  assets = answer.assets || assets;
  if (state.audiobook.voice === name) state.audiobook.voice = "";
  populateAssets();
  setStatus(`Deleted the voice ${name}.`);
  render(); queueSync();
  await refreshVoices();
}
async function deleteBook(book, button) {
  const answer = await deleteAsset("audiobooks", book.name,
    `Delete the audiobook ${book.title}? The MP3 and its synchronized text are removed for good.`,
    button);
  if (!answer) return;
  setStatus(`Deleted the audiobook ${book.title}.`);
  await refreshLibrary();
}
async function deleteDocument(button) {
  const name = state.audiobook.document;
  if (!name) return;
  const answer = await deleteAsset("documents", name,
    `Delete ${name} from your books? Audiobooks made from it are kept.`, button);
  if (!answer) return;
  assets = answer.assets || assets;
  if (state.audiobook.document === name) state.audiobook.document = "";
  populateAssets();
  setStatus(`Deleted ${name}.`);
  render(); queueSync();
}

async function selectVoice(name) {
  stopPreview();
  state.audiobook.voice = name;
  populateAssets();
  state.tab = "audiobook";
  createView = "compose";
  // Continue at the step that needs the user next.
  state.audiobook.step = bookReady() ? "create" : "book";
  setStatus(`${name} is your voice.`);
  render(); renderVoiceTable();
  // Create's own checks arrive with this sync; only then is its action enabled.
  await sync();
  focusStep();
}

function renderSelectedVoice() {
  const card = $("selected-voice");
  const voice = facts.clone_server ? null : voiceByName(state.audiobook.voice);
  card.classList.toggle("hidden", !voice);
  // Rebuild only on change, so a focused preview button keeps its focus.
  const key = voice ? JSON.stringify([voice.name, voice.preview, voice.description]) : "";
  if (card.dataset.key === key) return;
  card.dataset.key = key;
  if (!voice) { card.replaceChildren(); return; }
  const meta = document.createElement("div");
  meta.className = "voice-meta";
  const name = document.createElement("strong");
  name.textContent = voice.name;
  const description = document.createElement("span");
  description.className = "note clamp";
  description.textContent = voice.description || "No prompt saved";
  meta.append(name, description);
  card.replaceChildren(previewButton(voice), meta);
}

function bookReady() {
  return !!state.audiobook.document &&
    (assets.documents || []).includes(state.audiobook.document);
}
function voiceReady() {
  return facts.clone_server
    ? !!state.audiobook.server_voice.trim()
    : (assets.voices || []).includes(state.audiobook.voice);
}
function currentStep() {
  // A later step is only reachable once the steps before it are complete.
  const step = state.audiobook.step;
  if (step !== "book" && !bookReady()) return "book";
  if (step === "create" && !voiceReady()) return "voice";
  return step;
}
function setStep(step) {
  state.audiobook.step = step;
  render(); queueSync();
  focusStep();
}
function focusStep() {
  const node = $(`step-${currentStep()}`);
  const target = [...node.querySelectorAll(
    "select, input:not([type=file]):not([type=checkbox]), button.primary"
  )].find((control) => !control.closest(".hidden") && !control.disabled);
  (target || node.querySelector(".step-title")).focus();
}
async function continueFromBook() {
  if (submitting) return;
  collect();
  if (state.audiobook.source_url.trim()) {
    submitting = true; render();
    const downloaded = await materializeUrl();
    submitting = false; render();
    if (!downloaded) return;
  }
  if (bookReady()) setStep("voice");
}
function continueFromVoice() {
  collect();
  if (voiceReady()) setStep("create");
}
function searchVoices() { setTab("voice"); $("voice-search").focus(); }
function createAnother() {
  createView = "compose";
  setStep("book");
}
function createFirstAudiobook() {
  createView = "compose";
  setTab("audiobook"); setStep("book");
}
function viewProgress() { createView = "progress"; render(); $("progress-title").focus(); }
function resultAction() {
  if (!resultInfo) return;
  if (resultInfo.code === 0) return startListening();
  retryResult();
}
function retryResult() {
  // Continue the job this card reports, not whatever the form holds by now.
  const { document: book, voice } = resultInfo;
  createView = "compose";
  if (!book || !voice) return setStep("create");
  state.audiobook.document = book;
  state.audiobook[facts.clone_server ? "server_voice" : "voice"] = voice;
  state.audiobook.source_url = ""; state.audiobook.download_name = "";
  state.audiobook.step = "create";
  for (const [id, path] of FIELDS) $(id).value = at(path);
  populateAssets();
  startAudiobook();
}
function startListening() {
  const name = resultInfo ? resultInfo.name : "";
  createView = "compose";
  state.audiobook.step = "book";
  if (name) openAudiobook(name, true);
}

function stageFor(phase, unit) {
  if (phase === "extraction")
    return unit === "paragraph" || unit === "document" ? "prepare" : "read";
  if (phase === "narration") return "audio";
  if (phase === "alignment") return "finish";
  return null;
}
function progressDetail(info) {
  if (info.unit === "document") return "Using the text prepared earlier";
  const noun = { page:"Page", paragraph:"Paragraph", sentence:"Sentence" }[info.unit]
    || (info.phase === "narration" ? "Part" : "Step");
  return `${noun} ${info.done} of ${info.total}`;
}
function progressSubject() {
  const job = jobs.find((item) => item.id === currentJobId);
  return job ? `${job.document} with ${job.voice}` : "your audiobook";
}

function renderCreate() {
  const step = currentStep();
  const done = { book:bookReady(), voice:voiceReady(), create:false };
  const composing = createView === "compose";
  $("create-steps").classList.toggle("hidden", !composing);
  $("create-progress").classList.toggle("hidden", createView !== "progress");
  $("create-result").classList.toggle("hidden", createView !== "result");
  ["book", "voice", "create"].forEach((name, index) => {
    const node = $(`step-${name}`);
    const active = name === step, complete = !active && done[name];
    node.classList.toggle("is-active", active);
    node.classList.toggle("is-done", complete);
    node.classList.toggle("is-upcoming", !active && !complete);
    node.querySelector(".step-marker").textContent = complete ? "✓" : String(index + 1);
    node.querySelector(".step-state").textContent =
      active ? "(current step)" : complete ? "(done)" : "";
  });
  $("book-summary").textContent = step !== "book" && done.book ? state.audiobook.document : "";
  $("book-change").classList.toggle("hidden", step === "book");
  const voiceName = facts.clone_server
    ? state.audiobook.server_voice.trim() : state.audiobook.voice;
  $("voice-summary").textContent = step !== "voice" && done.voice ? voiceName : "";
  $("voice-change").classList.toggle("hidden", step === "voice" || !done.book);
  $("book-continue").disabled =
    submitting || !(done.book || state.audiobook.source_url.trim());
  $("document-delete").disabled = submitting || !bookReady();
  $("voice-continue").disabled = !done.voice;
  $("clone-voice-row").classList.toggle("hidden", !facts.clone_server);
  $("shared-voice-row").classList.toggle("hidden", !!facts.clone_server);
  // Facts belong to the tab of the last sync; hold Create until its own arrive.
  const current = facts.tab === "audiobook";
  const problem = step === "create" && state.tab === "audiobook" && current ? facts.problem : "";
  $("create-problem").textContent = problem || "";
  $("create-problem").classList.toggle("hidden", !problem);
  $("run").disabled = submitting || !current || !!facts.problem;
  $("output-name").textContent = facts.output_name ? `Saved as ${facts.output_name}` : "";
  const background = running && runKind === "audiobook" && composing;
  $("create-banner").classList.toggle("hidden", !background);
  $("create-banner-text").textContent = background ? `Creating ${progressSubject()}…` : "";
  renderSelectedVoice();
}

function renderProgress() {
  const finished = !running && resultInfo && resultInfo.code === 0;
  const current = STAGES.indexOf(runStage);
  for (const node of $("stages").children) {
    const index = STAGES.indexOf(node.dataset.stage);
    const status = finished || index < current ? "done"
      : index === current ? "active" : "pending";
    node.classList.toggle("is-done", status === "done");
    node.classList.toggle("is-active", status === "active");
    node.querySelector(".stage-icon").textContent = status === "done" ? "✓" : "";
    node.querySelector(".stage-state").textContent =
      status === "done" ? " (done)" : status === "active" ? " (in progress)" : "";
  }
  $("progress-subject").textContent = progressSubject();
  $("stop").disabled = !running;
}

function renderResult() {
  const info = resultInfo;
  if (!info) return;
  const ok = info.code === 0, stopped = info.code === 130;
  $("result-title").textContent = ok ? "Your audiobook is ready"
    : stopped ? "Stopped" : "We couldn't finish this audiobook";
  $("result-text").textContent = ok ? bookTitle(info.name)
    : stopped ? "Everything finished so far is kept. Continue whenever you like."
    : "Everything finished so far is kept, so trying again continues from there.";
  $("result-primary").textContent = ok ? "Start listening" : stopped ? "Continue" : "Try again";
  $("result-details").classList.toggle("hidden", ok || stopped || !resultDetail);
  $("result-detail-text").textContent = resultDetail;
  $("download").classList.toggle("hidden", !ok);
  $("airdrop").classList.toggle("hidden", !ok || !caps.airdrop || !info.artifact);
}

function openVoiceForm() {
  voiceFormOpen = true; voiceResult = null;
  render();
  $("voice-name").focus();
}
function closeVoiceForm() {
  voiceFormOpen = false;
  render();
  $("new-voice").focus();
}

function renderVoiceForm() {
  const creating = running && runKind === "voice";
  const open = voiceFormOpen || creating;
  $("voice-form").classList.toggle("hidden", !open);
  $("new-voice").classList.toggle("hidden", open);
  $("new-voice").setAttribute("aria-expanded", String(open));
  $("design-voice-row").classList.toggle("hidden", !facts.design_server);
  $("voice-progress").classList.toggle("hidden", !creating);
  $("voice-stop").classList.toggle("hidden", !creating);
  $("voice-close").classList.toggle("hidden", creating);
  const name = state.voice.name.trim();
  $("voice-exists").textContent =
    facts.voice_exists && name ? `This replaces the voice named ${name}.` : "";
  // Voice creation needs every narration worker, so it waits for an empty queue.
  const queued = !creating && jobs.length > 0;
  const typed = !!(name || state.voice.instruct.trim());
  const problem = queued ? "New voices can be created once no audiobook is being made."
    : typed && state.tab === "voice" && facts.tab === "voice" ? facts.problem : "";
  $("voice-problem").textContent = problem || "";
  $("voice-problem").classList.toggle("hidden", !problem);
  $("voice-create").disabled =
    submitting || creating || queued || facts.tab !== "voice" || !!facts.problem;
  $("voice-create").textContent = facts.voice_exists ? "Replace voice" : "Create voice";
  const result = $("voice-result");
  const shown = !!voiceResult && !creating;
  result.classList.toggle("hidden", !shown);
  const voice = shown && voiceResult.ok ? voiceByName(voiceResult.name) : null;
  const key = shown ? JSON.stringify([voiceResult, voice && voice.preview]) : "";
  if (result.dataset.key === key) return;
  result.dataset.key = key;
  result.replaceChildren();
  if (!shown) return;
  if (voice) result.append(previewButton(voice));
  const text = document.createElement("div");
  text.className = "voice-meta";
  const message = document.createElement("span");
  message.className = voiceResult.ok || voiceResult.stopped ? "" : "problem";
  message.textContent = voiceResult.ok
    ? `${voiceResult.name} is ready and selected for your next audiobook.`
    : voiceResult.stopped ? "Stopped. Nothing was saved."
    : "We couldn't create this voice. Try again, or change the prompt.";
  text.append(message);
  if (!voiceResult.ok && voiceResult.detail) {
    const details = document.createElement("details");
    details.className = "technical";
    const summary = document.createElement("summary");
    summary.textContent = "Technical details";
    const pre = document.createElement("pre");
    pre.textContent = voiceResult.detail;
    details.append(summary, pre);
    text.append(details);
  }
  result.append(text);
}

function runKey(info) {
  return info ? (info.job_id || `kind:${info.kind}`) : "";
}

function describeModel(role, title) {
  const model = configuration[role];
  if (!model || !model.model) return `${title}: not configured`;
  const place = model.source === "server" ? " (speech server)"
    : model.device ? ` on ${model.device}` : "";
  return `${title}: ${model.model}${place}`;
}

function jobRow(job) {
  const row = document.createElement("div");
  row.className = "job-row";
  const name = document.createElement("div");
  name.className = "job-name";
  name.textContent = `${job.document} · ${job.voice}`;
  name.title = job.id;
  const status = document.createElement("span");
  status.className = "job-state";
  const workers = consumers.filter((consumer) => consumer.job_id === job.id);
  const workerLabel = workers.length > 1
    ? `${workers.length} workers` : workers[0]?.label || "";
  status.textContent = job.status === "running"
    ? `Creating${workerLabel ? ` · ${workerLabel}` : ""}`
    : job.status === "preparing" ? "Getting ready"
    : `Waiting (#${job.position})`;
  status.title = workers.map((worker) => worker.label).join("\n");
  const actions = document.createElement("div");
  actions.className = "job-actions";
  if (job.status === "running") {
    const viewing = currentJobId === job.id && createView === "progress";
    const view = document.createElement("button");
    view.type = "button";
    view.textContent = viewing ? "Viewing" : "View";
    view.disabled = viewing;
    view.addEventListener("click", () => viewJob(job.id));
    actions.append(view);
  }
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = job.status === "running" ? "Stop" : "Cancel";
  cancel.setAttribute("aria-label", `${cancel.textContent} ${job.document}`);
  cancel.addEventListener("click", () => cancelJob(job.id));
  actions.append(cancel);
  row.append(name, status, actions);
  return row;
}

function renderQueue() {
  // The job poll repaints every two seconds; skip identical repaints so
  // keyboard focus on a queue button survives.
  const signature = JSON.stringify([jobs, consumers, configuration, currentJobId, createView]);
  if (signature === queueSignature) return;
  queueSignature = signature;
  $("compute-workers").replaceChildren(...consumers.map((consumer) => {
    const chip = document.createElement("span");
    chip.className = `worker-chip ${consumer.status}`;
    const label = document.createElement("span");
    label.textContent = consumer.label;
    const status = document.createElement("span");
    status.className = "worker-status";
    status.textContent = consumer.status;
    chip.append(label, status);
    const job = jobs.find((item) => item.id === consumer.job_id);
    chip.title = [
      consumer.detail,
      job ? `${job.document} · ${job.voice}` : "",
      consumer.status === "reserved" ? "Reserved while a voice is created" : "",
    ].filter(Boolean).join("\n");
    return chip;
  }));
  // Identical GPUs share one description instead of repeating it per chip.
  $("compute-detail").textContent = [...new Set(
    consumers.map((consumer) => consumer.detail).filter(Boolean)
  )].join("; ");
  $("compute-models").textContent = [
    describeModel("clone", "Narration"), describeModel("design", "Voice design"),
  ].join(" · ");
  // The followed job already has its progress card or banner.
  $("queue-panel").classList.toggle("hidden", !jobs.some((job) => job.id !== currentJobId));
  $("job-queue").replaceChildren(...jobs.map(jobRow));
}

function viewJob(id) {
  createView = "progress";
  const active = runs.find((item) => item.active && item.job_id === id);
  if (active) followActive(active, true);
  render();
  $("progress-title").focus();
}

function followActive(info, reconnecting) {
  const key = runKey(info);
  if (running && followedRunKey === key && stream) return;
  if (stream) stream.close();
  running = true; followedRunKey = key; currentJobId = info.job_id || "";
  runKind = info.kind;
  // The "will start when…" notice is over once that job runs.
  if (info.job_id && info.job_id === waitingJobId) { waitingJobId = ""; setStatus(""); }
  runStage = stageFor(info.phase, null); recentLog = [];
  if (runKind === "voice") voiceResult = null;
  beginEta(info.phase || runKind);
  $("log").textContent = "";
  $("progress").removeAttribute("value");
  $("progress-detail").textContent = reconnecting ? "Reconnecting…" : "Starting…";
  render();
  watch(currentJobId);
}

async function refreshJobs() {
  try {
    const current = await jsonRequest("/api/jobs");
    jobs = current.queue || []; runs = current.runs || [];
    consumers = current.consumers || [];
    renderQueue();
    const followed = runs.find(
      (item) => item.active && runKey(item) === followedRunKey
    );
    if (followed) return;
    const active = runs.find((item) => item.active) || null;
    if (active) followActive(active, true);
    else if (!running) render();
  } catch (_) {}
}

function render() {
  if (!state) return;
  const tab = state.tab;
  for (const name of ["audiobook", "voice", "player"]) {
    const selected = name === tab;
    $(`tab-${name}`).setAttribute("aria-selected", String(selected));
    $(`tab-${name}`).tabIndex = selected ? 0 : -1;
    $(`page-${name}`).classList.toggle("hidden", !selected);
  }
  const creating = tab !== "player";
  $("advanced").classList.toggle("hidden", !creating);
  $("advanced").setAttribute("aria-expanded", String(creating && advancedOpen));
  $("advanced-panels").classList.toggle("hidden", !creating || !advancedOpen);
  $("narration-advanced").classList.toggle("hidden", tab !== "audiobook");
  $("adaptation-advanced").classList.toggle(
    "hidden", tab !== "audiobook" || !state.audiobook.adapt
  );
  $("voice-advanced").classList.toggle("hidden", tab !== "voice");
  const activeServer = tab === "voice" ? facts.design_server : facts.clone_server;
  for (const id of ["dtype","attn","language","seed"]) $(id).disabled = activeServer;
  $("batch-size").disabled = facts.clone_server;
  $("log").classList.toggle("hidden", !creating);
  const reading = tab === "player" && !!state.player.book;
  $("library-view").classList.toggle("hidden", reading);
  $("book-view").classList.toggle("hidden", !reading);
  renderCreate(); renderProgress(); renderResult(); renderVoiceForm(); renderQueue();
}

function setTab(tab) {
  if (!state || state.tab === tab) return;
  stopPreview();
  state.tab = tab;
  setStatus("");
  // Other browsers and the operator add voices and previews meanwhile.
  if (tab === "voice") { renderVoiceTable(); refreshVoices(); }
  if (tab === "player") {
    refreshLibrary();
    if (state.player.book && !readerAudio) openAudiobook(state.player.book);
  }
  render(); sync();
}
function toggleAdvanced() { advancedOpen = !advancedOpen; render(); }
function log(text) {
  const box = $("log");
  const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  box.append(text);
  if (atEnd) box.scrollTop = box.scrollHeight;
  const lines = String(text).split("\n").map((line) => line.trim()).filter(Boolean);
  // A failure's last lines carry its cause, its summary, and where work is kept.
  recentLog = recentLog.concat(lines).slice(-4);
}

async function requestRun(confirmed) {
  const response = await fetch("/api/run", {
    method:"POST", headers:{ "Content-Type":"application/json" },
    body:JSON.stringify({ state, confirmed:!!confirmed }),
  });
  const answer = await response.json().catch(() => ({}));
  if (response.status === 409 && answer.confirmation_required) {
    if (window.confirm(answer.message)) return requestRun(true);
    setStatus("Nothing was replaced.");
    return null;
  }
  if (!response.ok) {
    setStatus(answer.error || response.statusText, true);
    return null;
  }
  return answer;
}

async function startAudiobook() {
  // Disabled while a request is in flight, so a double click starts one job.
  if (submitting) return;
  submitting = true; render();
  let started = false;
  try {
    collect();
    state.tab = "audiobook";
    if (!(await materializeUrl())) return;
    collect();
    const answer = await requestRun(false);
    if (!answer) return;
    jobs = answer.queue || jobs; runs = answer.runs || runs;
    consumers = answer.consumers || consumers;
    const job = answer.job;
    const active = job
      ? runs.find((item) => item.active && item.job_id === job.id) : null;
    if (active) {
      createView = "progress";
      followActive(active, false);
      started = true;
    } else if (job) {
      waitingJobId = job.id;
      setStatus(answer.duplicate
        ? `${job.output_name} is already waiting to be created.`
        : `${job.output_name} will start when the audiobook ahead of it finishes.`);
    }
  } finally {
    submitting = false; render();
    // Disabling the button dropped focus; return it to the view now shown.
    $(started ? "progress-title" : "run").focus();
  }
}

async function createVoice() {
  if (submitting) return;
  submitting = true; voiceResult = null; render();
  try {
    collect();
    state.tab = "voice";
    const answer = await requestRun(false);
    if (!answer) return;
    jobs = answer.queue || jobs; runs = answer.runs || runs;
    consumers = answer.consumers || consumers;
    const active = runs.find((item) => item.active);
    if (active) { followActive(active, false); $("voice-stop").focus(); }
  } finally {
    submitting = false; render();
  }
}

async function confirmRunAfterDisconnect(source) {
  try {
    const current = await jsonRequest("/api/jobs");
    if (!running || stream !== source) return;
    runs = current.runs || []; jobs = current.queue || [];
    consumers = current.consumers || [];
    const same = runs.find(
      (item) => item.active && runKey(item) === followedRunKey
    );
    if (same) return;
    source.close(); stream = null; running = false;
    currentJobId = ""; followedRunKey = "";
    endEta();
    const active = runs.find((item) => item.active);
    if (active) return followActive(active, true);
    if (createView === "progress") createView = "compose";
    render();
    setStatus("The server restarted, so this work stopped. Start it again to continue where it left off.", true);
  } catch (_) {}
}

function watch(jobId="") {
  if (stream) stream.close();
  const path = jobId ? `/api/events?job=${encodeURIComponent(jobId)}` : "/api/events";
  const source = new EventSource(path);
  let reconnecting = false;
  stream = source;
  source.onopen = () => {
    if (!running || !reconnecting) return;
    reconnecting = false; setStatus("");
  };
  source.addEventListener("log", (event) => log(JSON.parse(event.data)));
  source.addEventListener("phase", (event) => {
    const info = JSON.parse(event.data);
    beginEta(info.phase);
    runStage = stageFor(info.phase, null) || runStage;
    $("progress").removeAttribute("value");
    $("progress-detail").textContent = "";
    renderProgress();
  });
  source.addEventListener("progress", (event) => {
    const info = JSON.parse(event.data);
    $("progress").max = info.total; $("progress").value = info.done;
    updateEta(
      info.done, info.total, info.phase_elapsed,
      info.phase_stream_elapsed
    );
    runStage = stageFor(info.phase, info.unit) || runStage;
    $("progress-detail").textContent = progressDetail(info);
    renderProgress();
  });
  source.addEventListener("activity", (event) => {
    const info = JSON.parse(event.data);
    $("progress-detail").textContent = `Paragraph ${info.completed} of ${info.total}`;
  });
  source.addEventListener("done", async (event) => {
    const info = JSON.parse(event.data);
    const job = jobs.find((item) => item.id === currentJobId);
    source.close(); if (stream === source) stream = null;
    running = false; currentJobId = ""; followedRunKey = "";
    endEta();
    let focus = null;
    // The outcome replaces this browser's "Stopping…" notice.
    if (stopping) { stopping = false; setStatus(""); }
    if (info.kind === "audiobook") {
      // Show the outcome only where its progress was being watched.
      if (createView === "progress") {
        createView = "result";
        // Continue and Try again resubmit this job, whatever the form holds by then.
        resultInfo = { ...info, document:job ? job.document : "", voice:job ? job.voice : "" };
        resultDetail = info.code !== 0 && info.code !== 130 ? recentLog.join("\n") : "";
        if (state.tab === "audiobook") focus = "result-title";
      } else if (info.code === 0) {
        if (info.name) setStatus(`${info.name} is ready in Listen.`);
      } else {
        // Away from its progress card, a stop or failure is only a notice.
        const subject = job ? `${job.document} with ${job.voice}` : "An audiobook";
        const stopped = info.code === 130 || info.code === -15;
        setStatus(
          `${subject} ${stopped ? "stopped" : "couldn't be finished"}. ` +
          "Create it again to continue where it left off.",
          !stopped,
        );
      }
    }
    const createdVoice = info.kind === "voice" && info.code === 0 && info.artifact
      ? clientFileName(info.artifact) : null;
    if (info.kind === "voice") {
      // Stop terminates the voice process, which then reports SIGTERM.
      const stopped = info.code === 130 || info.code === -15;
      voiceResult = {
        ok:info.code === 0, stopped, name:createdVoice,
        detail:info.code && !stopped ? recentLog.join("\n") : "",
      };
      if (state.tab === "voice") focus = "voice-result";
    }
    let nextRun = null;
    const synced = syncSeq;
    try {
      const current = await jsonRequest("/api/state");
      // Local changes made meanwhile win over the stored copy.
      if (synced === syncSeq) { state = current.state; facts = current.derived; }
      assets = current.assets || assets;
      jobs = current.queue || []; runs = current.runs || [];
      consumers = current.consumers || [];
      nextRun = runs.find((item) => item.active) || null;
      if (createdVoice) {
        state.voice.name = createdVoice;
        state.audiobook.voice = createdVoice;
      }
      populateAssets();
      if (createdVoice) await sync();
    } catch (_) {}
    if (info.kind === "voice") refreshVoices();
    if (info.kind === "audiobook" && info.code === 0) refreshLibrary();
    render();
    if (focus) $(focus).focus();
    if (nextRun) followActive(nextRun, true);
  });
  source.onerror = () => {
    if (!running || stream !== source) return;
    reconnecting = true; setStatus("Reconnecting…");
    setTimeout(() => confirmRunAfterDisconnect(source), 1500);
  };
}

async function stopRun() {
  if (currentJobId) return cancelJob(currentJobId);
  try {
    const answer = await jsonRequest("/api/stop", {
      method:"POST", headers:{ "Content-Type":"application/json" }, body:"{}",
    });
    if (answer.stopping) { stopping = true; setStatus("Stopping…"); }
  } catch (error) { setStatus(error.message, true); }
}

async function cancelJob(id) {
  try {
    const answer = await jsonRequest("/api/jobs/cancel", {
      method:"POST", headers:{ "Content-Type":"application/json" },
      body:JSON.stringify({ id }),
    });
    jobs = answer.queue || []; runs = answer.runs || [];
    consumers = answer.consumers || [];
    renderQueue();
    if (answer.job.status === "running" && currentJobId === id) {
      stopping = true; setStatus("Stopping…");
    }
  } catch (error) { setStatus(error.message, true); }
}

function downloadArtifact() {
  if (resultInfo && resultInfo.code === 0 && resultInfo.name)
    location.href = "/api/download?asset=" + encodeURIComponent(resultInfo.name);
}
async function sendAirdrop() {
  try {
    await jsonRequest("/api/airdrop", {
      method:"POST", headers:{ "Content-Type":"application/json" },
      body:JSON.stringify({ path:resultInfo.artifact }),
    });
    setStatus("AirDrop panel opened.");
  } catch (error) { setStatus(error.message, true); }
}

function applyPaperCatalog(catalog) {
  const selected = state.audiobook.model;
  const select = $("paper-model");
  select.replaceChildren();
  const fallback = document.createElement("option");
  fallback.value = "";
  fallback.textContent = catalog.default_model
    ? `OMP default — ${catalog.default_model}` : "OMP configured default";
  select.append(fallback);
  const groups = new Map();
  for (const model of catalog.models || []) {
    if (!groups.has(model.provider)) {
      const group = document.createElement("optgroup");
      group.label = model.provider;
      groups.set(model.provider, group); select.append(group);
    }
    const option = document.createElement("option");
    option.value = model.selector; option.textContent = model.selector;
    groups.get(model.provider).append(option);
  }
  if (selected && ![...select.options].some((option) => option.value === selected)) {
    const unavailable = document.createElement("option");
    unavailable.value = selected; unavailable.textContent = `${selected} (unavailable)`;
    select.append(unavailable);
  }
  select.value = selected;
  paperOpenAIConnected = !!catalog.openai_connected;
  $("paper-openai").textContent = paperOpenAIConnected ? "OpenAI ✓" : "OpenAI";
  $("paper-local").textContent = state.audiobook.local_server ? "Local ✓" : "Add local";
  $("paper-model-status").textContent = catalog.local_error ||
    `${(catalog.models || []).length} models`;
  $("paper-model-status").classList.toggle("bad", !!catalog.local_error);
}
async function refreshPaperModels() {
  $("paper-model-status").textContent = "Loading models…";
  try { applyPaperCatalog(await jsonRequest("/api/paper/models")); }
  catch (error) {
    $("paper-model-status").textContent = error.message;
    $("paper-model-status").classList.add("bad");
  }
}

function renderPaperOpenAI(info) {
  const connected = paperOpenAIConnected || info.status === "connected";
  $("paper-openai-status").textContent = info.status === "idle"
    ? connected ? "OpenAI OAuth is connected." : "OpenAI OAuth is not connected."
    : (info.message || info.status);
  const hasDevice = !!(info.url || info.code);
  $("paper-openai-device").classList.toggle("hidden", !hasDevice);
  $("paper-openai-link").href = info.url || "#";
  $("paper-openai-code").textContent = info.code || "waiting…";
  $("paper-openai-start").disabled = !!info.active;
  $("paper-openai-cancel").classList.toggle("hidden", !info.active);
}
async function pollPaperOpenAI() {
  clearTimeout(paperOAuthTimer);
  try {
    const info = await jsonRequest("/api/paper/openai/status");
    renderPaperOpenAI(info);
    if (info.active) paperOAuthTimer = setTimeout(pollPaperOpenAI, 1000);
    else if (info.status === "connected") await refreshPaperModels();
  } catch (error) { $("paper-openai-status").textContent = error.message; }
}
async function openPaperOpenAI() {
  $("paper-openai-dialog").showModal(); await pollPaperOpenAI();
}
function closePaperOpenAI() {
  clearTimeout(paperOAuthTimer); $("paper-openai-dialog").close();
}
async function startPaperOpenAI() {
  try {
    renderPaperOpenAI(await jsonRequest("/api/paper/openai/login", {
      method:"POST", headers:{ "Content-Type":"application/json" }, body:"{}",
    }));
    paperOAuthTimer = setTimeout(pollPaperOpenAI, 250);
  } catch (error) { $("paper-openai-status").textContent = error.message; }
}
async function cancelPaperOpenAI() {
  try {
    renderPaperOpenAI(await jsonRequest("/api/paper/openai/cancel", {
      method:"POST", headers:{ "Content-Type":"application/json" }, body:"{}",
    }));
  } catch (error) { $("paper-openai-status").textContent = error.message; }
}

function openPaperLocal() {
  $("paper-local-type").value = state.audiobook.local_provider;
  $("paper-local-url").value = state.audiobook.local_server;
  $("paper-local-status").textContent = state.audiobook.local_server
    ? `Current server: ${state.audiobook.local_server}` : "";
  $("paper-local-remove").disabled = !state.audiobook.local_server;
  $("paper-local-dialog").showModal();
}
function closePaperLocal() { $("paper-local-dialog").close(); }
async function savePaperLocal() {
  $("paper-local-save").disabled = true;
  $("paper-local-status").textContent = "Checking local models…";
  try {
    const catalog = await jsonRequest("/api/paper/local/check", {
      method:"POST", headers:{ "Content-Type":"application/json" },
      body:JSON.stringify({
        server:$("paper-local-url").value, provider:$("paper-local-type").value,
      }),
    });
    state.audiobook.local_server = catalog.local_server;
    state.audiobook.local_provider = catalog.local_provider;
    $("paper-local-server").value = catalog.local_server;
    $("paper-local-provider").value = catalog.local_provider;
    await sync(); applyPaperCatalog(catalog); closePaperLocal();
  } catch (error) {
    $("paper-local-status").textContent = error.message;
  } finally { $("paper-local-save").disabled = false; }
}
async function removePaperLocal() {
  state.audiobook.local_server = ""; $("paper-local-server").value = "";
  state.audiobook.local_provider = ""; $("paper-local-provider").value = "";
  await sync(); closePaperLocal(); await refreshPaperModels();
}

for (const [id] of FIELDS) {
  const node = $(id);
  node.addEventListener(node.tagName === "SELECT" ? "change" : "input", () => {
    collect(); render(); queueSync();
  });
}
for (const [id] of FLAGS)
  $(id).addEventListener("change", () => { collect(); render(); sync(); });
$("shared-voice").addEventListener("change", () => { stopPreview(); renderVoiceTable(); });
$("document-file").addEventListener("change", uploadDocument);
$("voice-search").addEventListener("input", () => { voiceLimit = PAGE_SIZE; renderVoiceTable(); });
$("book-search").addEventListener("input", () => { bookLimit = PAGE_SIZE; renderLibrary(); });
previewAudio.addEventListener("play", syncPreviewButtons);
previewAudio.addEventListener("pause", syncPreviewButtons);
previewAudio.addEventListener("ended", () => { previewing = ""; syncPreviewButtons(); });
$("tab-list").addEventListener("keydown", (event) => {
  // Arrow keys, Home, and End move between tabs, as in native tab strips.
  const order = ["audiobook", "voice", "player"];
  const index = order.indexOf(state.tab);
  const next = {
    ArrowRight:order[(index + 1) % order.length],
    ArrowLeft:order[(index + order.length - 1) % order.length],
    Home:order[0], End:order[order.length - 1],
  }[event.key];
  if (!next) return;
  event.preventDefault();
  setTab(next);
  $(`tab-${next}`).focus();
});

fetch("/api/state").then((response) => response.json()).then((data) => {
  load(data);
  if (!caps.airdrop) $("airdrop").title = "AirDrop requires a macOS server";
  refreshPaperModels();
  setInterval(refreshJobs, 2000);
});
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0",
                        help="Interface to bind (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8800, help="Port to bind (default: 8800).")
    parser.add_argument("--open", action="store_true", help="Open the page in a browser.")
    parser.add_argument("--verbose", action="store_true", help="Log every request to stderr.")
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=DEFAULT_STORAGE_ROOT,
        help=(
            "Shared library root containing Voices, Audiobooks, Documents, "
            f"and in_progress (default: {DEFAULT_STORAGE_ROOT})."
        ),
    )
    models = parser.add_argument_group("voice model configuration")
    models.add_argument(
        "--allow-model-downloads",
        action="store_true",
        help="Allow configured Hugging Face model IDs and model downloads.",
    )
    design = models.add_mutually_exclusive_group()
    design.add_argument(
        "--voice-design-model",
        metavar="PATH_OR_ID",
        help="VoiceDesign model directory, or Hugging Face ID with --allow-model-downloads.",
    )
    design.add_argument(
        "--voice-design-server",
        metavar="URL",
        type=speech_endpoint,
        help="OpenAI-compatible speech server used for voice design instead of a local model.",
    )
    models.add_argument(
        "--voice-design-server-model",
        metavar="NAME",
        help="Remote voice-design model (default with --voice-design-server: gpt-4o-mini-tts).",
    )
    clone = models.add_mutually_exclusive_group()
    clone.add_argument(
        "--voice-clone-model",
        metavar="PATH_OR_ID",
        help="Base model directory, or Hugging Face ID with --allow-model-downloads.",
    )
    clone.add_argument(
        "--voice-clone-server",
        metavar="URL",
        type=speech_endpoint,
        help="OpenAI-compatible speech server used for narration instead of a local model.",
    )
    models.add_argument(
        "--voice-clone-server-model",
        metavar="NAME",
        help="Remote narration model (default with --voice-clone-server: tts-1).",
    )
    models.add_argument(
        "--render-voice-previews",
        action="store_true",
        help=(
            "Narrate the fixed preview passage with every saved voice whose clip "
            "reads other words, using --voice-clone-model, then exit. Run it while "
            "no audiobook is being made."
        ),
    )
    models.add_argument(
        "--narration-ssh-worker",
        action="append",
        default=[],
        type=ssh_target,
        metavar="TARGET",
        help=(
            "Passwordless OpenSSH target used as an audiobook chunk worker; "
            "repeat for multiple hosts. Requires --voice-clone-model."
        ),
    )
    models.add_argument(
        "--narration-ssh-python",
        default="python3",
        metavar="PATH",
        help="Python executable on every narration SSH worker (default: python3).",
    )
    models.add_argument(
        "--narration-ssh-model",
        metavar="PATH_OR_ID",
        help=(
            "Base model path or ID on narration SSH workers; defaults to the "
            "local clone model value."
        ),
    )
    models.add_argument(
        "--narration-ssh-device",
        default="cuda:0",
        metavar="DEVICE",
        help="PyTorch device on narration SSH workers (default: cuda:0).",
    )
    args = parser.parse_args()
    if not SCRIPT.is_file():
        parser.error(f"audiobook_tts.py is missing next to this script: {SCRIPT}")
    tts_models = configured_tts_models(args, parser)
    storage = SharedStorage(args.storage_root)
    try:
        prepare_library(storage)
    except OSError as exc:
        parser.error(f"cannot create shared storage under {storage.root}: {exc}")
    if args.render_voice_previews:
        if tts_models["clone"]["source"] != "local":
            parser.error("--render-voice-previews needs a local --voice-clone-model")
        failed = render_voice_previews(
            storage, tts_models["clone"], resolve_device("auto")
        )
        if failed:
            raise SystemExit(f"Could not render previews for: {', '.join(failed)}")
        print("Every saved voice now previews the same passage.")
        return
    loopback = args.host in ("127.0.0.1", "::1", "localhost")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.jobs = JobQueue(audiobook_consumers(tts_models["clone"]))
    server.openai_login = OpenAIOAuthLogin()
    server.tts_models = tts_models
    server.storage = storage
    server.verbose = args.verbose
    browser_host = "127.0.0.1" if loopback or args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{browser_host}:{server.server_port}/"
    print(f"Hilde listening on {args.host}:{server.server_port}", flush=True)
    print(f"Open: {url}", flush=True)
    print("State: per-browser cookies", flush=True)
    print(f"Shared storage: {storage.root}", flush=True)
    print(model_summary(tts_models["design"], "design"), flush=True)
    print(model_summary(tts_models["clone"], "clone"), flush=True)
    print(
        "Audiobook consumers: "
        + ", ".join(
            consumer["label"]
            for consumer in server.jobs.consumers_snapshot()
        ),
        flush=True,
    )
    if not loopback:
        print("Remote access enabled: paths and runs act as this user.", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping", flush=True)
    finally:
        server.jobs.shutdown()
        server.openai_login.stop()
        server.server_close()


if __name__ == "__main__":
    main()
