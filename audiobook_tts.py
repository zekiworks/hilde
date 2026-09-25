"""Create a reusable narrator once, then use it across audiobooks and sessions.

Dependencies: qwen-tts, soundfile, and matching PyTorch/TorchAudio builds for
your platform. FlashAttention 2 is needed only when explicitly selected and
must be compatible with your PyTorch/CUDA installation.

Commands:
  create-voice  Use a VoiceDesign model (--model-path), a voice description
                (--instruct), and a short passage (--text or --input).
                Save reference.wav, its exact UTF-8 transcript.txt, and the
                description as description.txt in --voice-dir. The default
                FLOAT WAV preserves model samples.
                Use --overwrite to replace an existing saved voice.

  narrate       Use a Base model (--clone-model-path), an existing --voice-dir,
                book text (--text or --input), and an --output MP3 or WAV.
                Only the Base model is loaded; no new voice is designed.

  Either one  …or skip local inference with --server IP:PORT, which calls an
                OpenAI-compatible POST /v1/audio/speech endpoint instead of
                loading a model: narration posts one request per chunk, and
                voice design posts the passage with --instruct as the schema's
                `instructions` field, saving the returned clip as the reference.

The voice directory is portable: copy its files together. There are no saved
model tensors, device-specific caches, or dependencies on the original location.
Each single-device narration session rebuilds one clone prompt from the saved
audio and text. --batch-size 0 (default) submits the whole book in one call.

With --resume-dir, repeat --worker-device for additional local GPUs or
--ssh-worker for passwordless OpenSSH targets. Each worker loads one Base model,
pulls chunk batches dynamically, and returns independently validated WAV
checkpoints; the coordinator assembles them in source order. SSH workers receive
the script, saved voice, and narration text and need their own compatible Python
environment and model.

Larger batches narrate faster until GPU memory runs out; a batch that runs out
of CUDA memory is retried one chunk at a time instead of failing the book. Batch
size can change sampled audio even with the same seed, but every batch retains
the same saved voice.

Local narration and voice creation require an explicit device and attention
backend. Models are loaded locally/offline unless --allow-downloads is supplied,
which also permits Hugging Face model IDs. Input text, including Markdown, is
read literally.

Remote work (--server) replaces the model and device with an OpenAI-compatible
speech endpoint: --server-model names the server-side model and --server-voice
its server-side voice (a preset speaker, or a "clone:Profile" name where the
server supports one). Voice design sends --instruct as `instructions`, which
shapes that voice for the request; tts-1 and tts-1-hd are refused there because
the schema documents them as ignoring the field. Narration needs no saved voice:
reference clips are never uploaded, since cloning is not part of the schema.
The model supplies the narration sample rate and mono channel layout.

Voice creation can replace an existing saved voice with --overwrite. Narration's
--overwrite can replace an audiobook, but never its saved voice or input text.
This script saves audio; it does not configure or start playback.
"""

import argparse
import base64
import hashlib
import io
import json
import math
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from contextlib import ExitStack, redirect_stdout
from pathlib import Path


VOICE_DESCRIPTION_FILE = "description.txt"
VOICE_PREVIEW_FILE = "preview.wav"
WORKER_PROTOCOL = "@@AUDIOBOOK_TTS_WORKER@@"
MAX_WORKER_WAV_BYTES = 256 * 1024 * 1024


def split_sentences(text):
    """Split prose at spoken sentence boundaries without crossing paragraphs."""
    return [
        sentence.strip()
        for paragraph in re.split(r"\n\s*\n", text.strip())
        for sentence in re.split(r"(?<=[.!?。！？])\s+", paragraph)
        if sentence.strip()
    ]


def split_text(text, max_chars, *, sentence_chunks=False):
    """Prefer sentence boundaries and wrap oversized sentences to fit."""
    chunks = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        sentences = split_sentences(paragraph)
        if sentence_chunks:
            for sentence in sentences:
                chunks.extend(textwrap.wrap(
                    sentence, width=max_chars, break_on_hyphens=False
                ))
            continue
        chunk = ""
        for sentence in sentences:
            for part in textwrap.wrap(
                sentence, width=max_chars, break_on_hyphens=False
            ):
                if chunk and len(chunk) + len(part) + 1 > max_chars:
                    chunks.append(chunk)
                    chunk = ""
                chunk = f"{chunk} {part}" if chunk else part
        if chunk:
            chunks.append(chunk)
    return chunks


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def compression_level(value):
    level = float(value)
    if not 0.0 <= level <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return level


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def speech_endpoint(value):
    """Accept IP:PORT, host:port, or a full URL; return the OpenAI base URL."""
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("must not be empty")
    parsed = urllib.parse.urlsplit(text if "//" in text else f"//{text}", scheme="http")
    if not parsed.hostname:
        raise argparse.ArgumentTypeError("must contain a host, for example 127.0.0.1:8880")
    try:
        port = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError("port must be a number from 1 to 65535") from None
    if parsed.scheme not in ("http", "https"):
        raise argparse.ArgumentTypeError("scheme must be http or https")
    authority = parsed.hostname if port is None else f"{parsed.hostname}:{port}"
    if parsed.username or parsed.password:
        raise argparse.ArgumentTypeError("credentials belong in --api-key, not the URL")
    # A bare host:port means the conventional OpenAI prefix; an explicit path wins.
    path = parsed.path.rstrip("/") or "/v1"
    return f"{parsed.scheme}://{authority}{path}"


def ssh_target(value):
    """Validate an OpenSSH destination without accepting option injection."""
    target = value.strip()
    if (
        not target
        or target.startswith("-")
        or not re.fullmatch(r"[A-Za-z0-9_.@%+\-\[\]:]+", target)
    ):
        raise argparse.ArgumentTypeError(
            "must be an SSH host or user@host without whitespace or options"
        )
    return target


def generate_clone_batch(model, texts, language, prompt):
    """Clone one batch; if CUDA memory runs out, retry its chunks one at a time."""
    import torch

    try:
        return model.generate_voice_clone(
            text=texts, language=[language] * len(texts), voice_clone_prompt=prompt,
        )
    except torch.cuda.OutOfMemoryError:
        if len(texts) == 1:
            raise
    # Retry outside the handler, so the failed batch's tensors are released.
    print(
        f"Out of GPU memory for a batch of {len(texts)} chunks; "
        "retrying them one at a time",
        flush=True,
    )
    waveforms = []
    for text in texts:
        generated, sample_rate = model.generate_voice_clone(
            text=[text], language=[language], voice_clone_prompt=prompt,
        )
        waveforms.extend(generated)
    return waveforms, sample_rate


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    shared = argparse.ArgumentParser(add_help=False)
    runtime = shared.add_argument_group("runtime")
    runtime.add_argument(
        "--allow-downloads", action="store_true",
        help="Allow Hugging Face network access. Default: local models, offline.",
    )
    runtime.add_argument(
        "--device",
        help="Explicit PyTorch device, e.g. cuda:0 or cpu. Required for local inference; "
        "rejected with --server.",
    )
    runtime.add_argument(
        "--dtype", choices=("auto", "float32", "float16", "bfloat16"), default="auto",
        help="Default: auto (CUDA: bfloat16 if supported, otherwise float16; other devices: float32).",
    )
    runtime.add_argument(
        "--attn-implementation",
        choices=("flash_attention_2", "sdpa", "eager"),
        help="FlashAttention 2 requires CUDA and float16/bfloat16; SDPA/eager also support CPU. "
        "Required for local inference; rejected with --server.",
    )
    runtime.add_argument(
        "--seed", type=nonnegative_int,
        help="Optional seed from 0 to 4294967295; repeatability is not guaranteed "
        "across batch sizes, devices or library versions.",
    )
    text = shared.add_argument_group("text")
    source = text.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="Text to synthesize instead of a file.")
    source.add_argument("--input", type=Path, help="Text file; Markdown is read literally.")
    text.add_argument(
        "--input-encoding", default="utf-8-sig",
        help="Encoding used for --input (default: utf-8-sig, accepts UTF-8 with or without BOM).",
    )
    text.add_argument(
        "--language", default="Auto",
        help="Language of the text being synthesized (default: Auto).",
    )
    server = shared.add_argument_group("openai-compatible server")
    server.add_argument(
        "--server", type=speech_endpoint, metavar="IP:PORT",
        help="Use POST /v1/audio/speech on this endpoint instead of loading a model. "
        "Accepts IP:PORT, host:port, or a full URL; a bare authority implies /v1.",
    )
    server.add_argument(
        "--server-model", default="tts-1",
        help="Model name sent to the server (default: tts-1). Voice design needs a model that "
        "honours instructions, so tts-1 and tts-1-hd are refused there.",
    )
    server.add_argument(
        "--server-voice",
        help="Voice name sent to the server: a preset speaker, or a clone profile the server "
        "already holds. Voice design modulates it with --instruct. Required with --server.",
    )
    server.add_argument(
        "--server-timeout", type=positive_float, default=300.0,
        help="Seconds to wait for one request's response (default: 300).",
    )
    server.add_argument(
        "--api-key",
        help="Bearer token for the server; defaults to the OPENAI_API_KEY environment "
        "variable when set. Local servers usually need none.",
    )

    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create-voice", parents=[shared],
        help="Design and save a reusable narrator.",
        description="Generate one reference clip and save it with its exact UTF-8 transcript.",
    )
    create.add_argument(
        "--model-path",
        help="VoiceDesign model directory, or Hugging Face ID with --allow-downloads. "
        "Required for local voice design; rejected with --server.",
    )
    create.add_argument(
        "--voice-dir", type=Path, required=True,
        help="Voice directory; parents are created as needed. Existing voices require --overwrite.",
    )
    create.add_argument(
        "--overwrite", action="store_true",
        help="Replace reference.wav, transcript.txt, and description.txt in an existing "
        "voice directory and remove a preview.wav rendered from the old voice.",
    )
    create.add_argument("--instruct", required=True, help="Description of the voice to create.")
    create.add_argument(
        "--wav-subtype", choices=("PCM_16", "PCM_24", "PCM_32", "FLOAT", "DOUBLE"),
        default="FLOAT", help="Reference WAV encoding (default: FLOAT, preserves model samples).",
    )

    narrate = commands.add_parser(
        "narrate", parents=[shared],
        help="Generate an audiobook using a saved narrator.",
        description="Reuse a saved voice in every chunk. Only the Base model is loaded.",
    )
    narrate.add_argument(
        "--clone-model-path",
        help="Base model directory, or Hugging Face ID with --allow-downloads. "
        "Required for local narration; rejected with --server.",
    )
    narrate.add_argument(
        "--voice-dir", type=Path,
        help="Saved voice directory containing reference.wav and transcript.txt. "
        "Required for local narration; rejected with --server.",
    )
    narration = narrate.add_argument_group("narration")
    narration.add_argument(
        "--chunk-max-chars", type=positive_int, default=500,
        help="Maximum characters per narration chunk (default: 500).",
    )
    narration.add_argument(
        "--sentence-chunks",
        action="store_true",
        help="Start each narration chunk at a sentence boundary for synchronized reading.",
    )
    narration.add_argument(
        "--batch-size", type=nonnegative_int, default=0,
        help="Chunks per clone call; 0 submits the whole book on one device (default: 0), "
        "but becomes 1 per worker in distributed mode. A batch that runs out of CUDA "
        "memory is retried one chunk at a time. All batches share the saved voice.",
    )
    narration.add_argument(
        "--worker-device",
        action="append",
        default=[],
        metavar="DEVICE",
        help=(
            "Additional local device that may narrate chunks for this job; "
            "repeat for more devices. Requires --resume-dir."
        ),
    )
    narration.add_argument(
        "--ssh-worker",
        action="append",
        default=[],
        type=ssh_target,
        metavar="TARGET",
        help=(
            "Passwordless OpenSSH target that narrates chunks on its own GPU; "
            "repeat for more hosts. Requires --resume-dir."
        ),
    )
    narration.add_argument(
        "--ssh-python",
        default="python3",
        metavar="PATH",
        help="Python executable on every SSH worker (default: python3).",
    )
    narration.add_argument(
        "--ssh-model-path",
        metavar="PATH_OR_ID",
        help=(
            "Base model path or Hugging Face ID on SSH workers; defaults to "
            "--clone-model-path."
        ),
    )
    narration.add_argument(
        "--ssh-device",
        default="cuda:0",
        metavar="DEVICE",
        help="PyTorch device on every SSH worker (default: cuda:0).",
    )
    output = narrate.add_argument_group("audio output")
    output.add_argument(
        "--output", type=Path, required=True,
        help="Destination ending in .mp3 or .wav; the suffix selects the format.",
    )
    output.add_argument(
        "--overwrite", action="store_true",
        help="Allow replacing an existing audiobook, never the saved voice or input file.",
    )
    output.add_argument(
        "--resume-dir",
        type=Path,
        help="Durable chunk checkpoint directory. Matching completed chunks are reused.",
    )
    output.add_argument(
        "--wav-subtype", choices=("PCM_16", "PCM_24", "PCM_32", "FLOAT", "DOUBLE"),
        help="WAV sample encoding (default: PCM_16). Not valid for MP3.",
    )
    output.add_argument(
        "--mp3-compression-level", type=compression_level,
        help="MP3 compression from 0 (minimum) to 1 (maximum); default: encoder setting. "
        "Not valid for WAV.",
    )

    worker = commands.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--clone-model-path", required=True)
    worker.add_argument("--voice-dir", type=Path, required=True)
    worker.add_argument("--device", required=True)
    worker.add_argument(
        "--dtype",
        choices=("auto", "float32", "float16", "bfloat16"),
        default="auto",
    )
    worker.add_argument(
        "--attn-implementation",
        choices=("flash_attention_2", "sdpa", "eager"),
        required=True,
    )
    worker.add_argument("--language", default="Auto")
    worker.add_argument("--seed", type=nonnegative_int)
    worker.add_argument("--allow-downloads", action="store_true")
    return parser


def resolve_model(model, allow_downloads):
    path = Path(model).expanduser()
    if path.is_dir():
        return str(path.resolve())
    if allow_downloads:
        return model
    raise ValueError(
        f"Local model directory does not exist: {path}. "
        "For a Hugging Face model ID, use --allow-downloads."
    )


def read_text(args, parser):
    if args.input is not None:
        try:
            text = args.input.expanduser().read_text(encoding=args.input_encoding)
        except (OSError, UnicodeError, LookupError) as exc:
            parser.error(f"Cannot read input file: {exc}")
    else:
        text = args.text
    if not text.strip():
        parser.error("Input contains no text to synthesize")
    return text


def load_model(model, args, parser):
    try:
        model_path = resolve_model(model, args.allow_downloads)
    except ValueError as exc:
        parser.error(str(exc))
    # Set policy before importing Hugging Face: nested loads must obey it too.
    offline = "0" if args.allow_downloads else "1"
    os.environ["HF_HUB_OFFLINE"] = offline
    os.environ["TRANSFORMERS_OFFLINE"] = offline

    import torch

    try:
        device = torch.device(args.device)
    except (RuntimeError, ValueError) as exc:
        parser.error(f"Invalid --device: {exc}")
    if device.type == "cuda":
        if not torch.cuda.is_available():
            parser.error("--device requests CUDA, but CUDA is unavailable")
        if device.index is not None and device.index >= torch.cuda.device_count():
            parser.error(f"CUDA device index is unavailable: {device.index}")
    if args.dtype == "auto":
        dtype = torch.float32
        if device.type == "cuda":
            with torch.cuda.device(device):
                dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    else:
        dtype = getattr(torch, args.dtype)
    if args.attn_implementation == "flash_attention_2":
        if device.type != "cuda" or dtype not in (torch.float16, torch.bfloat16):
            parser.error("FlashAttention 2 requires a CUDA device and float16 or bfloat16")
    if args.seed is not None:
        from transformers import set_seed

        set_seed(args.seed)

    from qwen_tts import Qwen3TTSModel

    print(f"Runtime: {device}, {dtype}, {args.attn_implementation}", flush=True)
    print(f"Loading model: {model_path}", flush=True)
    model = Qwen3TTSModel.from_pretrained(
        model_path, local_files_only=not args.allow_downloads,
        device_map=str(device), dtype=dtype,
        attn_implementation=args.attn_implementation,
    )
    # The talker stops at its end-of-speech code but has no pad token, so
    # transformers pads with that code anyway and logs so on every chunk.
    model.model.talker.generation_config.pad_token_id = (
        model.model.config.talker_config.codec_eos_token_id
    )
    return model


def save_voice(
    voice_dir, waveform, sample_rate, transcript, subtype, overwrite=False, description="",
):
    import soundfile as sf

    voice_dir = Path(voice_dir)
    # Stage every file before publishing; transcript remains the commit marker.
    with tempfile.TemporaryDirectory(prefix=".voice-", dir=voice_dir.parent) as temporary:
        staged = Path(temporary)
        sf.write(staged / "reference.wav", waveform, sample_rate, subtype=subtype)
        (staged / "transcript.txt").write_text(transcript, encoding="utf-8", newline="")
        if description:
            (staged / VOICE_DESCRIPTION_FILE).write_text(
                description, encoding="utf-8", newline=""
            )
        if overwrite:
            if voice_dir.is_symlink():
                raise FileExistsError(f"refusing to overwrite voice-directory symlink: {voice_dir}")
            voice_dir.mkdir(exist_ok=True)
            if not voice_dir.is_dir():
                raise NotADirectoryError(f"voice destination is not a directory: {voice_dir}")
        else:
            # Reserve the destination after generation; a concurrent creator still wins safely.
            voice_dir.mkdir()
        (staged / "reference.wav").replace(voice_dir / "reference.wav")
        if description:
            (staged / VOICE_DESCRIPTION_FILE).replace(voice_dir / VOICE_DESCRIPTION_FILE)
        else:
            # A replaced voice must not keep describing the audio it replaced,
            (voice_dir / VOICE_DESCRIPTION_FILE).unlink(missing_ok=True)
        # nor keep a preview rendered from it.
        (voice_dir / VOICE_PREVIEW_FILE).unlink(missing_ok=True)
        (staged / "transcript.txt").replace(voice_dir / "transcript.txt")


def voice_destination(args, parser):
    voice_dir = args.voice_dir.expanduser()
    if os.path.lexists(voice_dir):
        if not args.overwrite:
            parser.error(
                f"Voice directory already exists: {voice_dir}; use --overwrite to replace it"
            )
        if voice_dir.is_symlink() or not voice_dir.is_dir():
            parser.error(f"Voice overwrite destination must be a directory, not: {voice_dir}")
    return voice_dir


def create_voice(args, parser, text):
    import soundfile as sf

    voice_dir = voice_destination(args, parser)
    if not args.instruct.strip():
        parser.error("--instruct must contain a voice description")
    if not sf.check_format("WAV", args.wav_subtype):
        parser.error(f"Installed libsndfile does not support WAV/{args.wav_subtype} encoding")
    model = load_model(args.model_path, args, parser)
    print("Designing the reusable narrator reference", flush=True)
    wavs, sr = model.generate_voice_design(
        text=text, language=args.language, instruct=args.instruct,
    )
    try:
        voice_dir.parent.mkdir(parents=True, exist_ok=True)
        save_voice(
            voice_dir, wavs[0], sr, text, args.wav_subtype, args.overwrite,
            description=args.instruct.strip(),
        )
    except (OSError, sf.LibsndfileError) as exc:
        parser.error(f"Cannot save voice: {exc}")
    print(f"Saved voice: {voice_dir.resolve()} ({len(wavs[0]) / sr:.2f}s, {sr} Hz)")


def read_voice(voice_dir):
    import numpy as np
    import soundfile as sf

    with (voice_dir / "transcript.txt").open(encoding="utf-8", newline="") as source:
        transcript = source.read()
    if not transcript.strip():
        raise ValueError("Saved voice transcript is empty")
    waveform, sr = sf.read(voice_dir / "reference.wav", dtype="float32")
    if waveform.ndim != 1 or not waveform.size:
        raise ValueError("Saved voice must contain nonempty mono audio")
    if not np.isfinite(waveform).all() or not np.any(waveform):
        raise ValueError("Saved voice contains invalid or silent audio")
    return waveform, sr, transcript


def prepare_output(args, parser, voice_dir=None):
    """Validate the destination before any model load or network call."""
    import soundfile as sf

    output_path = args.output.expanduser()
    output_format = {".mp3": "MP3", ".wav": "WAV"}.get(output_path.suffix.lower())
    if output_format is None:
        parser.error("--output must end in .mp3 or .wav")
    if output_format == "MP3" and args.wav_subtype is not None:
        parser.error("--wav-subtype requires a .wav output")
    if output_format == "WAV" and args.mp3_compression_level is not None:
        parser.error("--mp3-compression-level requires an .mp3 output")
    if output_path.exists():
        protected = []
        if voice_dir is not None:
            protected += [voice_dir / "reference.wav", voice_dir / "transcript.txt"]
        if args.input is not None:
            protected.append(args.input.expanduser())
        if any(output_path.samefile(path) for path in protected):
            parser.error("--output must not replace the saved voice or input file")
        if not args.overwrite:
            parser.error(f"Output already exists: {output_path}; use --overwrite to replace it")
    if not output_path.parent.is_dir():
        parser.error(f"Output directory does not exist: {output_path.parent}")
    if output_path.is_dir():
        parser.error(f"Output is a directory: {output_path}")
    subtype = "MPEG_LAYER_III" if output_format == "MP3" else (args.wav_subtype or "PCM_16")
    if not sf.check_format(output_format, subtype):
        parser.error(f"Installed libsndfile does not support {output_format}/{subtype} encoding")
    return output_path, output_format, subtype

def _digest_files(paths):
    digest = hashlib.sha256()
    for path in paths:
        target = Path(path)
        digest.update(target.name.encode("utf-8"))
        digest.update(b"\0")
        with target.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _resume_identity(args, text, chunks, voice_version):
    chunk_digest = hashlib.sha256()
    for chunk in chunks:
        encoded = chunk.encode("utf-8")
        chunk_digest.update(len(encoded).to_bytes(8, "big"))
        chunk_digest.update(encoded)
    return {
        "schema": 1,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "chunks_sha256": chunk_digest.hexdigest(),
        "chunk_count": len(chunks),
        "language": args.language,
        "voice_version": voice_version,
    }


def _checkpoint_path(directory, index):
    return directory / f"chunk-{index:06d}.wav"


def prepare_resume(args, text, chunks, voice_version, sf):
    """Return a durable checkpoint directory and verified completed chunk indexes."""
    if args.resume_dir is None:
        return None, set()
    directory = args.resume_dir.expanduser()
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    identity = _resume_identity(args, text, chunks, voice_version)
    manifest_path = directory / "manifest.json"
    try:
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    if current != identity:
        for checkpoint in directory.glob("chunk-*.wav"):
            checkpoint.unlink(missing_ok=True)
        temporary = directory / ".manifest.json.tmp"
        temporary.write_text(
            json.dumps(identity, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(manifest_path)
    completed = set()
    for index in range(1, len(chunks) + 1):
        checkpoint = _checkpoint_path(directory, index)
        if not checkpoint.is_file():
            continue
        try:
            info = sf.info(checkpoint)
        except sf.LibsndfileError:
            checkpoint.unlink(missing_ok=True)
            continue
        if info.frames > 0 and info.samplerate > 0 and info.channels > 0:
            completed.add(index)
        else:
            checkpoint.unlink(missing_ok=True)
    if completed:
        print(
            f"Resuming narration with {len(completed)}/{len(chunks)} completed chunks",
            flush=True,
        )
    return directory, completed


def save_checkpoint(directory, index, waveform, sample_rate, sf):
    target = _checkpoint_path(directory, index)
    temporary = target.with_name(f".{target.name}.tmp")
    sf.write(
        temporary,
        waveform,
        sample_rate,
        format="WAV",
        subtype="FLOAT",
    )
    temporary.replace(target)


def assemble_checkpoints(
    directory,
    count,
    output_path,
    output_format,
    subtype,
    compression_level,
    sf,
):
    """Atomically encode ordered checkpoints into the requested final container."""
    temporary = output_path.with_name(f".{output_path.name}.assembling")
    temporary.unlink(missing_ok=True)
    total_samples = 0
    sample_rate = None
    channels = None
    try:
        with sf.SoundFile(
            temporary,
            mode="w",
            samplerate=sf.info(_checkpoint_path(directory, 1)).samplerate,
            channels=sf.info(_checkpoint_path(directory, 1)).channels,
            format=output_format,
            subtype=subtype,
            compression_level=compression_level,
        ) as output:
            sample_rate = output.samplerate
            channels = output.channels
            for index in range(1, count + 1):
                waveform, current_rate = sf.read(
                    _checkpoint_path(directory, index),
                    dtype="float32",
                    always_2d=channels > 1,
                )
                current_channels = 1 if waveform.ndim == 1 else waveform.shape[1]
                if (current_rate, current_channels) != (sample_rate, channels):
                    raise ValueError(
                        f"Chunk {index} is {current_rate} Hz / {current_channels} "
                        f"channels, expected {sample_rate} Hz / {channels} channels"
                    )
                output.write(waveform)
                total_samples += len(waveform)
        temporary.replace(output_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return total_samples, sample_rate




def request_speech(args, parser, chunk, instructions=None):
    """One POST /v1/audio/speech call; returns WAV bytes."""
    body = {
        "model": args.server_model,
        "voice": args.server_voice,
        "input": chunk,
        # WAV carries its own rate and channel count, so chunks stay self-describing
        # and the final file is encoded locally with the requested MP3/WAV settings.
        "response_format": "wav",
    }
    if instructions is not None:
        # The schema's voice-design field: it shapes the named voice for this request.
        body["instructions"] = instructions
    payload = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "audio/wav"}
    key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"{args.server}/audio/speech", data=payload, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.server_timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace").strip()
        parser.error(f"Server returned {exc.code} {exc.reason}: {detail or '(no body)'}")
    except (urllib.error.URLError, OSError) as exc:
        parser.error(f"Cannot reach {args.server}: {exc}")


def create_voice_server(args, parser, text):
    """Design a voice through /v1/audio/speech: --instruct becomes `instructions`.

    The saved folder is identical to a local one — the returned clip plus the exact
    text that produced it — so it stays usable for local cloning afterwards.
    """
    import numpy as np
    import soundfile as sf

    voice_dir = voice_destination(args, parser)
    if not args.instruct.strip():
        parser.error("--instruct must contain a voice description")
    if not sf.check_format("WAV", args.wav_subtype):
        parser.error(f"Installed libsndfile does not support WAV/{args.wav_subtype} encoding")
    print(f"Server: {args.server} (model {args.server_model}, voice {args.server_voice})",
          flush=True)
    print("Designing the reusable narrator reference", flush=True)
    try:
        waveform, sr = sf.read(
            io.BytesIO(request_speech(args, parser, text, instructions=args.instruct)),
            dtype="float32",
        )
    except (sf.LibsndfileError, RuntimeError) as exc:
        parser.error(f"Server response is not readable audio: {exc}")
    # Refuse anything read_voice would later reject, rather than publishing a dead voice.
    if waveform.ndim != 1 or not waveform.size:
        parser.error("A saved voice must be nonempty mono audio; the server returned neither")
    if not np.isfinite(waveform).all() or not np.any(waveform):
        parser.error("The server returned invalid or silent audio")
    try:
        voice_dir.parent.mkdir(parents=True, exist_ok=True)
        save_voice(
            voice_dir, waveform, sr, text, args.wav_subtype, args.overwrite,
            description=args.instruct.strip(),
        )
    except (OSError, sf.LibsndfileError) as exc:
        parser.error(f"Cannot save voice: {exc}")
    print(f"Saved voice: {voice_dir.resolve()} ({len(waveform) / sr:.2f}s, {sr} Hz)")


def narrate_server(args, parser, text):
    """Narrate through an OpenAI-compatible endpoint with optional checkpoints."""
    import soundfile as sf

    output_path, output_format, subtype = prepare_output(args, parser)
    chunks = split_text(
        text,
        args.chunk_max_chars,
        sentence_chunks=getattr(args, "sentence_chunks", False),
    )
    print(
        f"Server: {args.server} (model {args.server_model}, voice {args.server_voice})",
        flush=True,
    )
    if args.resume_dir is not None:
        voice_version = hashlib.sha256(json.dumps(
            {
                "server": args.server,
                "model": args.server_model,
                "voice": args.server_voice,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        try:
            checkpoint_dir, completed = prepare_resume(
                args, text, chunks, voice_version, sf
            )
            if completed:
                print(
                    f"Checkpointed chunk {len(completed)}/{len(chunks)}",
                    flush=True,
                )
            for index, chunk in enumerate(chunks, 1):
                if index in completed:
                    continue
                print(
                    f"Requesting chunk {index}/{len(chunks)} from the server",
                    flush=True,
                )
                try:
                    waveform, sample_rate = sf.read(
                        io.BytesIO(request_speech(args, parser, chunk)),
                        dtype="float32",
                    )
                except (sf.LibsndfileError, RuntimeError) as exc:
                    parser.error(
                        f"Server response for chunk {index} is not readable audio: {exc}"
                    )
                save_checkpoint(
                    checkpoint_dir, index, waveform, sample_rate, sf
                )
                completed.add(index)
                print(
                    f"Checkpointed chunk {len(completed)}/{len(chunks)}",
                    flush=True,
                )
            total_samples, sample_rate = assemble_checkpoints(
                checkpoint_dir,
                len(chunks),
                output_path,
                output_format,
                subtype,
                args.mp3_compression_level,
                sf,
            )
        except (OSError, ValueError, sf.LibsndfileError) as exc:
            parser.error(f"Cannot resume narration: {exc}")
        print(
            f"Wrote {output_path.resolve()} "
            f"({total_samples / sample_rate:.2f}s, {sample_rate} Hz)"
        )
        return

    total_samples = 0
    sample_rate = None
    with ExitStack() as stack:
        audio = None
        for index, chunk in enumerate(chunks, 1):
            print(
                f"Requesting chunk {index}/{len(chunks)} from the server",
                flush=True,
            )
            try:
                waveform, chunk_rate = sf.read(
                    io.BytesIO(request_speech(args, parser, chunk)),
                    dtype="float32",
                )
            except (sf.LibsndfileError, RuntimeError) as exc:
                parser.error(
                    f"Server response for chunk {index} is not readable audio: {exc}"
                )
            channels = 1 if waveform.ndim == 1 else waveform.shape[1]
            if audio is None:
                sample_rate = chunk_rate
                destination = stack.enter_context(
                    output_path.open("wb" if args.overwrite else "xb")
                )
                audio = stack.enter_context(sf.SoundFile(
                    destination,
                    mode="w",
                    samplerate=sample_rate,
                    channels=channels,
                    format=output_format,
                    subtype=subtype,
                    compression_level=args.mp3_compression_level,
                ))
            elif (chunk_rate, channels) != (sample_rate, audio.channels):
                parser.error(
                    f"Chunk {index} returned {chunk_rate} Hz / {channels} channels, "
                    f"but the audiobook is {sample_rate} Hz / {audio.channels}; "
                    "the server changed formats"
                )
            audio.write(waveform)
            total_samples += len(waveform)
    print(
        f"Wrote {output_path.resolve()} "
        f"({total_samples / sample_rate:.2f}s, {sample_rate} Hz)"
    )


def _emit_worker_message(stream, payload):
    stream.write(
        WORKER_PROTOCOL
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )
    stream.flush()


def narration_worker(args, parser):
    """Run one model process and exchange chunk batches over JSON lines."""
    import soundfile as sf

    protocol = sys.stdout
    try:
        with redirect_stdout(sys.stderr):
            ref_audio, ref_rate, ref_text = read_voice(args.voice_dir.expanduser())
            model = load_model(args.clone_model_path, args, parser)
            voice_clone_prompt = model.create_voice_clone_prompt(
                ref_audio=(ref_audio, ref_rate),
                ref_text=ref_text,
                x_vector_only_mode=False,
            )
        _emit_worker_message(protocol, {"type": "ready"})
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("type") == "stop":
                return 0
            if request.get("type") != "generate":
                raise ValueError("worker received an unknown request")
            indexes = request.get("indexes")
            texts = request.get("texts")
            if (
                not isinstance(indexes, list)
                or not indexes
                or not all(isinstance(index, int) and index > 0 for index in indexes)
                or not isinstance(texts, list)
                or len(texts) != len(indexes)
                or not all(isinstance(text, str) and text for text in texts)
            ):
                raise ValueError("worker received an invalid chunk batch")
            with redirect_stdout(sys.stderr):
                waveforms, sample_rate = generate_clone_batch(
                    model, texts, args.language, voice_clone_prompt
                )
            encoded = []
            for waveform in waveforms:
                buffer = io.BytesIO()
                sf.write(
                    buffer,
                    waveform,
                    sample_rate,
                    format="WAV",
                    subtype="FLOAT",
                )
                encoded.append(
                    base64.b64encode(buffer.getvalue()).decode("ascii")
                )
            if len(encoded) != len(indexes):
                raise RuntimeError(
                    "worker returned a different number of waveforms than chunks"
                )
            _emit_worker_message(
                protocol,
                {
                    "type": "result",
                    "indexes": indexes,
                    "waves": encoded,
                },
            )
    except (OSError, RuntimeError, ValueError, sf.LibsndfileError) as exc:
        _emit_worker_message(
            protocol,
            {"type": "error", "message": f"{type(exc).__name__}: {exc}"},
        )
        return 1
    return 0


class NarrationWorkerProcess:
    """One local or SSH model process speaking the chunk-worker protocol."""

    def __init__(self, label, command, events, cleanup=None):
        self.label = label
        self.command = command
        self.events = events
        self.cleanup = cleanup
        self.process = None
        self.input_lock = threading.Lock()

    def start(self):
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            errors="replace",
        )
        threading.Thread(target=self._read_output, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()

    def _read_output(self):
        try:
            for raw in self.process.stdout:
                line = raw.rstrip("\r\n")
                if not line.startswith(WORKER_PROTOCOL):
                    if line:
                        self.events.put(
                            (self, {"type": "log", "message": line})
                        )
                    continue
                try:
                    payload = json.loads(line[len(WORKER_PROTOCOL):])
                except ValueError:
                    payload = {
                        "type": "error",
                        "message": "worker emitted malformed protocol JSON",
                    }
                self.events.put((self, payload))
        finally:
            self.process.stdout.close()
            self.events.put(
                (self, {"type": "exit", "code": self.process.wait()})
            )

    def _read_errors(self):
        for raw in self.process.stderr:
            line = raw.rstrip("\r\n")
            if line:
                self.events.put((self, {"type": "log", "message": line}))
        self.process.stderr.close()

    def send(self, payload):
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )
        with self.input_lock:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"{self.label} exited before accepting more work"
                )
            self.process.stdin.write(encoded + "\n")
            self.process.stdin.flush()

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self.send({"type": "stop"})
        except (BrokenPipeError, OSError, RuntimeError):
            pass

    def finish(self, graceful):
        try:
            if self.process is not None and self.process.poll() is None:
                if graceful:
                    self.stop()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.process.terminate()
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        finally:
            if self.process is not None and self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            if self.cleanup is not None:
                self.cleanup()


def _worker_arguments(args, model_path, voice_dir, device):
    arguments = [
        "_worker",
        "--clone-model-path",
        str(model_path),
        "--voice-dir",
        str(voice_dir),
        "--device",
        device,
        "--dtype",
        args.dtype,
        "--attn-implementation",
        args.attn_implementation,
        "--language",
        args.language,
    ]
    if args.seed is not None:
        arguments += ["--seed", str(args.seed)]
    if args.allow_downloads:
        arguments.append("--allow-downloads")
    return arguments


def _transport_options():
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
    ]


def _run_transport(command, action):
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{action} timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(
            f"{action} failed" + (f": {detail}" if detail else "")
        ) from exc


def _stage_ssh_worker(args, target, voice_dir):
    ssh = shutil.which("ssh")
    scp = shutil.which("scp")
    if ssh is None or scp is None:
        raise RuntimeError("SSH workers require both ssh and scp executables")
    remote_root = f"/tmp/audiobook-tts-{uuid.uuid4().hex}"
    options = _transport_options()
    cleanup_command = [
        ssh,
        *options,
        target,
        shlex.join(["rm", "-rf", "--", remote_root]),
    ]

    def cleanup():
        try:
            subprocess.run(
                cleanup_command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    try:
        _run_transport(
            [
                ssh,
                *options,
                target,
                shlex.join(["mkdir", "-m", "700", "--", remote_root]),
            ],
            f"creating workspace on {target}",
        )
        _run_transport(
            [
                scp,
                "-q",
                *options,
                str(Path(__file__).resolve()),
                str(voice_dir / "reference.wav"),
                str(voice_dir / "transcript.txt"),
                f"{target}:{remote_root}/",
            ],
            f"staging worker files on {target}",
        )
    except BaseException:
        cleanup()
        raise

    remote_model = args.ssh_model_path or args.clone_model_path
    remote_arguments = [
        args.ssh_python,
        "-u",
        f"{remote_root}/{Path(__file__).name}",
        *_worker_arguments(
            args,
            remote_model,
            remote_root,
            args.ssh_device,
        ),
    ]
    command = [ssh, *options, target, shlex.join(remote_arguments)]
    return (
        f"SSH {target} {args.ssh_device}",
        command,
        cleanup,
    )


def _build_narration_workers(args, voice_dir, events):
    specifications = []
    for device in [args.device, *args.worker_device]:
        specifications.append(
            (
                f"Local {device}",
                [
                    sys.executable,
                    "-u",
                    str(Path(__file__).resolve()),
                    *_worker_arguments(
                        args,
                        args.clone_model_path,
                        voice_dir,
                        device,
                    ),
                ],
                None,
            )
        )
    try:
        for target in args.ssh_worker:
            specifications.append(
                _stage_ssh_worker(args, target, voice_dir)
            )
    except BaseException:
        for _, _, cleanup in specifications:
            if cleanup is not None:
                cleanup()
        raise
    return [
        NarrationWorkerProcess(label, command, events, cleanup)
        for label, command, cleanup in specifications
    ]


def _save_worker_checkpoint(directory, index, encoded, sf):
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"worker returned invalid audio for chunk {index}") from exc
    if not payload or len(payload) > MAX_WORKER_WAV_BYTES:
        raise ValueError(
            f"worker returned an invalid audio size for chunk {index}"
        )
    target = _checkpoint_path(directory, index)
    temporary = target.with_name(f".{target.name}.worker")
    try:
        temporary.write_bytes(payload)
        info = sf.info(temporary)
        if info.frames <= 0 or info.samplerate <= 0 or info.channels <= 0:
            raise ValueError(f"worker returned empty audio for chunk {index}")
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _worker_topology(args):
    return {
        "local_devices": [args.device, *args.worker_device],
        "ssh_workers": list(args.ssh_worker),
        "ssh_device": args.ssh_device if args.ssh_worker else None,
        "ssh_model": (
            args.ssh_model_path or args.clone_model_path
            if args.ssh_worker
            else None
        ),
    }


def _narrate_distributed(
    args,
    parser,
    chunks,
    voice_dir,
    checkpoint_dir,
    completed,
    pending,
    sf,
):
    events = queue.Queue()
    try:
        workers = _build_narration_workers(args, voice_dir, events)
    except (OSError, RuntimeError) as exc:
        parser.error(f"Cannot prepare narration workers: {exc}")
    print(
        "Narration workers: " + ", ".join(worker.label for worker in workers),
        flush=True,
    )
    stop_requested = threading.Event()
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(
            signal.SIGTERM,
            lambda _signum, _frame: stop_requested.set(),
        )
    graceful = False
    try:
        for worker in workers:
            worker.start()
        ready = set()
        while len(ready) < len(workers):
            if stop_requested.is_set():
                return False
            try:
                worker, event = events.get(timeout=0.2)
            except queue.Empty:
                continue
            kind = event.get("type")
            if kind == "log":
                print(f"[{worker.label}] {event.get('message', '')}", flush=True)
            elif kind == "ready":
                ready.add(worker)
                print(f"[{worker.label}] Ready", flush=True)
            elif kind == "error":
                raise RuntimeError(
                    f"{worker.label}: {event.get('message', 'worker failed')}"
                )
            elif kind == "exit":
                raise RuntimeError(
                    f"{worker.label} exited during startup with code "
                    f"{event.get('code')}"
                )

        batch_size = args.batch_size or 1
        batches = deque(
            [
                pending[offset:offset + batch_size]
                for offset in range(0, len(pending), batch_size)
            ]
        )
        inflight = {}

        def dispatch(worker):
            if not batches:
                worker.stop()
                return
            indexes = batches.popleft()
            inflight[worker] = indexes
            worker.send(
                {
                    "type": "generate",
                    "indexes": indexes,
                    "texts": [chunks[index - 1] for index in indexes],
                }
            )
            print(
                f"[{worker.label}] Generating chunks "
                f"{','.join(str(index) for index in indexes)}/{len(chunks)}",
                flush=True,
            )

        for worker in workers:
            dispatch(worker)

        while inflight:
            if stop_requested.is_set():
                return False
            try:
                worker, event = events.get(timeout=0.2)
            except queue.Empty:
                continue
            kind = event.get("type")
            if kind == "log":
                print(f"[{worker.label}] {event.get('message', '')}", flush=True)
                continue
            if kind == "error":
                raise RuntimeError(
                    f"{worker.label}: {event.get('message', 'worker failed')}"
                )
            if kind == "exit":
                if worker in inflight:
                    raise RuntimeError(
                        f"{worker.label} exited with code {event.get('code')}"
                    )
                continue
            if kind != "result" or worker not in inflight:
                raise RuntimeError(
                    f"{worker.label} emitted an unexpected protocol event"
                )
            expected = inflight.pop(worker)
            indexes = event.get("indexes")
            waves = event.get("waves")
            if (
                indexes != expected
                or not isinstance(waves, list)
                or len(waves) != len(expected)
            ):
                raise RuntimeError(
                    f"{worker.label} returned the wrong chunk batch"
                )
            for index, encoded in zip(indexes, waves, strict=True):
                _save_worker_checkpoint(
                    checkpoint_dir, index, encoded, sf
                )
                completed.add(index)
                print(
                    f"Checkpointed chunk {len(completed)}/{len(chunks)}",
                    flush=True,
                )
            dispatch(worker)
        graceful = True
        return True
    except KeyboardInterrupt:
        return False
    except (OSError, RuntimeError, ValueError, sf.LibsndfileError) as exc:
        parser.error(f"Distributed narration failed: {exc}")
    finally:
        for worker in workers:
            worker.finish(graceful)
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


def _narrate_resumable_local(
    args,
    parser,
    text,
    chunks,
    voice_dir,
    ref_audio,
    ref_rate,
    ref_text,
    output_path,
    output_format,
    subtype,
    sf,
):
    voice_files_version = _digest_files(
        (voice_dir / "reference.wav", voice_dir / "transcript.txt")
    )
    voice_identity = {
        "voice_files": voice_files_version,
        "clone_model": args.clone_model_path,
        "device": args.device,
        "dtype": args.dtype,
        "attention": args.attn_implementation,
        "seed": args.seed,
        "batch_size": args.batch_size,
    }
    distributed = bool(args.worker_device or args.ssh_worker)
    if distributed:
        voice_identity["workers"] = _worker_topology(args)
    voice_version = hashlib.sha256(json.dumps(
        voice_identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    try:
        checkpoint_dir, completed = prepare_resume(
            args, text, chunks, voice_version, sf
        )
    except (OSError, ValueError, sf.LibsndfileError) as exc:
        parser.error(f"Cannot prepare narration checkpoints: {exc}")
    if completed:
        print(f"Checkpointed chunk {len(completed)}/{len(chunks)}", flush=True)
    pending = [
        index for index in range(1, len(chunks) + 1) if index not in completed
    ]
    if pending and distributed:
        completed_normally = _narrate_distributed(
            args,
            parser,
            chunks,
            voice_dir,
            checkpoint_dir,
            completed,
            pending,
            sf,
        )
        if not completed_normally:
            raise SystemExit(130)
        pending = []
    if pending:
        model = load_model(args.clone_model_path, args, parser)
        print(f"Using saved narrator: {voice_dir.resolve()}", flush=True)
        voice_clone_prompt = model.create_voice_clone_prompt(
            ref_audio=(ref_audio, ref_rate),
            ref_text=ref_text,
            x_vector_only_mode=False,
        )
        batch_size = args.batch_size or len(pending)
        position = 0
        batch_index = 0
        while position < len(pending):
            indexes = pending[position:position + batch_size]
            batch = [chunks[index - 1] for index in indexes]
            batch_index += 1
            print(
                f"Generating batch {batch_index} "
                f"(chunks {indexes[0]}-{indexes[-1]}/{len(chunks)}) "
                "with the saved narrator",
                flush=True,
            )
            waveforms, sample_rate = generate_clone_batch(
                model, batch, args.language, voice_clone_prompt
            )
            try:
                for index, waveform in zip(indexes, waveforms, strict=True):
                    save_checkpoint(
                        checkpoint_dir, index, waveform, sample_rate, sf
                    )
                    completed.add(index)
                    print(
                        f"Checkpointed chunk {len(completed)}/{len(chunks)}",
                        flush=True,
                    )
            except (OSError, ValueError, sf.LibsndfileError) as exc:
                parser.error(f"Cannot save narration checkpoint: {exc}")
            position += len(indexes)
            del waveforms
    try:
        total_samples, sample_rate = assemble_checkpoints(
            checkpoint_dir,
            len(chunks),
            output_path,
            output_format,
            subtype,
            args.mp3_compression_level,
            sf,
        )
    except (OSError, ValueError, sf.LibsndfileError) as exc:
        parser.error(f"Cannot assemble narration checkpoints: {exc}")
    print(
        f"Wrote {output_path.resolve()} "
        f"({total_samples / sample_rate:.2f}s, {sample_rate} Hz)"
    )


def narrate(args, parser, text):
    import soundfile as sf

    voice_dir = args.voice_dir.expanduser()
    try:
        ref_audio, ref_rate, ref_text = read_voice(voice_dir)
    except (OSError, UnicodeError, ValueError, sf.LibsndfileError) as exc:
        parser.error(f"Cannot read saved voice {voice_dir}: {exc}")

    output_path, output_format, subtype = prepare_output(
        args, parser, voice_dir
    )
    chunks = split_text(
        text,
        args.chunk_max_chars,
        sentence_chunks=getattr(args, "sentence_chunks", False),
    )
    if args.resume_dir is not None:
        _narrate_resumable_local(
            args,
            parser,
            text,
            chunks,
            voice_dir,
            ref_audio,
            ref_rate,
            ref_text,
            output_path,
            output_format,
            subtype,
            sf,
        )
        return

    model = load_model(args.clone_model_path, args, parser)
    print(f"Using saved narrator: {voice_dir.resolve()}", flush=True)
    voice_clone_prompt = model.create_voice_clone_prompt(
        ref_audio=(ref_audio, ref_rate),
        ref_text=ref_text,
        x_vector_only_mode=False,
    )
    batch_size = args.batch_size or len(chunks)
    total_samples = 0
    start = 0
    batch_index = 0
    with ExitStack() as stack:
        audio = None
        while start < len(chunks):
            stop = min(start + batch_size, len(chunks))
            batch = chunks[start:stop]
            batch_index += 1
            print(
                f"Generating batch {batch_index} "
                f"(chunks {start + 1}-{stop}/{len(chunks)}) "
                "with the saved narrator",
                flush=True,
            )
            waveforms, sample_rate = generate_clone_batch(
                model, batch, args.language, voice_clone_prompt
            )
            if audio is None:
                destination = stack.enter_context(
                    output_path.open("wb" if args.overwrite else "xb")
                )
                audio = stack.enter_context(sf.SoundFile(
                    destination,
                    mode="w",
                    samplerate=sample_rate,
                    channels=1,
                    format=output_format,
                    subtype=subtype,
                    compression_level=args.mp3_compression_level,
                ))
            for waveform in waveforms:
                audio.write(waveform)
                total_samples += len(waveform)
            start = stop
            del waveforms
    print(
        f"Wrote {output_path.resolve()} "
        f"({total_samples / sample_rate:.2f}s, {sample_rate} Hz)"
    )


def check_mode(args, parser):
    """argparse cannot express 'required unless --server', so enforce it here."""
    narrating = args.command == "narrate"
    if args.server is None:
        required = (
            ("--clone-model-path", args.clone_model_path), ("--voice-dir", args.voice_dir),
        ) if narrating else (("--model-path", args.model_path),)
        missing = [
            name for name, value in (
                ("--device", args.device),
                ("--attn-implementation", args.attn_implementation),
                *required,
            ) if value is None
        ]
        if missing:
            parser.error(f"the following arguments are required: {', '.join(missing)}")
        return
    if not args.server_voice:
        parser.error("--server-voice is required with --server")
    local_only = [
        ("--device", args.device), ("--attn-implementation", args.attn_implementation),
        ("--allow-downloads", args.allow_downloads or None), ("--seed", args.seed),
    ]
    if narrating:
        local_only += [
            ("--clone-model-path", args.clone_model_path),
            ("--voice-dir", args.voice_dir),
            ("--batch-size", args.batch_size or None),
            ("--worker-device", args.worker_device or None),
            ("--ssh-worker", args.ssh_worker or None),
            ("--ssh-model-path", args.ssh_model_path),
        ]
    else:
        local_only.append(("--model-path", args.model_path))
    supplied = [name for name, value in local_only if value is not None]
    if supplied:
        parser.error(
            f"--server runs no local model, so these do not apply: {', '.join(supplied)}"
        )
    if args.dtype != "auto":
        parser.error("--server runs no local model, so these do not apply: --dtype")
    if args.language != "Auto":
        parser.error(
            "the OpenAI speech schema has no language field; select the language through "
            "--server-model (many servers accept aliases such as tts-1-es)"
        )
    if not narrating and args.server_model in ("tts-1", "tts-1-hd"):
        parser.error(
            f"--server-model {args.server_model} ignores the instructions field, so it cannot "
            "design a voice; choose a model that honours instructions"
        )


def check_distributed_mode(args, parser):
    """Validate local/SSH chunk workers before any model process starts."""
    distributed = bool(args.worker_device or args.ssh_worker)
    if args.ssh_model_path and not args.ssh_worker:
        parser.error("--ssh-model-path requires at least one --ssh-worker")
    if not distributed:
        return
    if args.resume_dir is None:
        parser.error("--worker-device and --ssh-worker require --resume-dir")
    local_devices = [args.device, *args.worker_device]
    if len(set(local_devices)) != len(local_devices):
        parser.error("local narration worker devices must be unique")
    if len(set(args.ssh_worker)) != len(args.ssh_worker):
        parser.error("SSH narration worker targets must be unique")
    if not args.ssh_python.strip():
        parser.error("--ssh-python must not be empty")


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.seed is not None and args.seed >= 2**32:
        parser.error("--seed must be between 0 and 4294967295")
    if args.command == "_worker":
        raise SystemExit(narration_worker(args, parser))
    check_mode(args, parser)
    if args.command == "narrate":
        check_distributed_mode(args, parser)
    text = read_text(args, parser)
    if args.command == "create-voice":
        remote, local = create_voice_server, create_voice
    else:
        remote, local = narrate_server, narrate
    (remote if args.server is not None else local)(args, parser, text)


if __name__ == "__main__":
    main()
