# Hilde

**Create a narrator once. Reuse that voice across your audiobooks.**

An audiobook studio built on [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS). The Hilde web app turns PDF, Markdown, or text documents into narrated MP3 audiobooks with a synchronized reader. Underneath it, the `audiobook_tts.py` command-line tool designs a voice from a written description, saves a portable reference, and turns narration-ready text into one MP3 or WAV file. Run locally on CPU or CUDA, with optional FlashAttention 2 and batched narration.

## Why save a voice?

Giving every text chunk the same voice description does **not** establish a shared speaker identity—even when those chunks are generated in one batch.

This tool separates voice creation from audiobook generation:

1. **`create-voice`** uses a **VoiceDesign** model to generate a reference clip and save its exact transcript.
2. **`narrate`** uses a **Base** model to clone that saved reference for every chunk, across batches, books, and process sessions.

Only the Base model is needed after you have created a voice. The speaker reference stays the same; pacing, expression, and sampled audio can still vary.

### Features

- Portable voice folders containing a WAV and a UTF-8 transcript—not serialized model tensors.
- Paragraph- and sentence-aware text chunking, joined back into book order.
- Whole-book or fixed-size batches; a batch that runs out of GPU memory is retried one chunk at a time.
- Explicit model paths, device, attention backend, narrator description, and output destination.
- Offline model loading by default; Hugging Face downloads require an explicit flag.
- MP3 and WAV output, with configurable compression or WAV sample encoding.
- Optional remote narration through an OpenAI-compatible `POST /v1/audio/speech` server, needing no local model or GPU.
- A shared-library web server with one-click PDF/document preparation,
  resumable extraction and narration, phase-specific ETA, forced word-synchronized
  Markdown reading with tables and embedded visuals, playback, and downloads.
- CLI overwrite guards and web confirmation only when an audiobook would
  replace the same input and voice versions.

## Contents

- [Installation](#installation)
- [Models](#models)
- [Create a voice](#create-a-voice)
- [Narrate an audiobook](#narrate-an-audiobook)
- [Web interface](#web-interface)
- [Batching](#batching)
- [Command reference](#command-reference)
- [Troubleshooting](#troubleshooting)
- [Tests](#tests)

## Installation

Python **3.12** is the tested version. Run the following commands from the project directory. Shell examples use Bash; on Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell instead.

### 1. Create an environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 2. Install matching PyTorch and TorchAudio builds

Choose **one** build appropriate for your platform. Keep PyTorch and TorchAudio versions matched. See the [official PyTorch installation guidance](https://pytorch.org/get-started/locally/) for other platforms and CUDA builds.

**CUDA 13.0 example — the tested GPU stack:**

```bash
python -m pip install torch==2.10.0 torchaudio==2.10.0 \
  --index-url https://download.pytorch.org/whl/cu130
```

**CPU-only alternative:**

```bash
python -m pip install torch==2.10.0 torchaudio==2.10.0 \
  --index-url https://download.pytorch.org/whl/cpu
```

Then install the application dependencies from the repository manifest. PyTorch and TorchAudio stay
outside this file because their wheel source depends on the selected CPU/CUDA platform.

```bash
python -m pip install -r requirements.txt
```

The web reader force-aligns transcript words with TorchAudio's `MMS_FA` bundle
and Uroman. The first alignment downloads and caches the approximately 1.2 GB
MMS model through TorchAudio. Alignment runs on CPU after narration, so it does
not compete with the narration workers for GPU memory.

### 3. Optional: install FlashAttention 2

FlashAttention is needed only when selecting `--attn-implementation flash_attention_2`. It requires compatible CUDA hardware and a compatible build for your PyTorch installation. It cannot be used with CPU or float32 inference.

```bash
python -m pip install setuptools ninja packaging wheel
MAX_JOBS=4 python -m pip install flash-attn==2.8.3 --no-build-isolation
```

Use a matching [official FlashAttention wheel](https://github.com/Dao-AILab/flash-attention/releases) when available. Source builds require a compatible CUDA toolkit and C++ compiler; set `CUDA_HOME` to **your** toolkit installation if needed. See the [FlashAttention installation instructions](https://github.com/Dao-AILab/flash-attention#installation-and-features) for compatibility details.

The GPU workflow has been exercised on Linux with Python 3.12, PyTorch/TorchAudio 2.10.0 + CUDA 13.0, Qwen TTS 0.1.1, SoundFile 0.14.0, and FlashAttention 2.8.3. These versions are a tested combination, not a guarantee for every platform.

You can skip FlashAttention and select `--attn-implementation sdpa` instead. CPU inference with SDPA has also been exercised.

## Models

The two commands use different models:

| Command | Flag | Model role | Example model |
| --- | --- | --- | --- |
| `create-voice` | `--model-path` | Designs the narrator from a description. | [Qwen3-TTS-12Hz-1.7B-VoiceDesign](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign) |
| `narrate` | `--clone-model-path` | Reuses the saved narrator to speak new text. | [Qwen3-TTS-12Hz-1.7B-Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) |

Download complete model directories, including their tokenizer files:

```bash
hf download Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
  --local-dir models/Qwen3-TTS-12Hz-1.7B-VoiceDesign

hf download Qwen/Qwen3-TTS-12Hz-1.7B-Base \
  --local-dir models/Qwen3-TTS-12Hz-1.7B-Base
```

These are example locations, not built-in defaults. Supply the paths you chose when running the script. With local directories, model loading stays offline by default.

Alternatively, pass a Hugging Face model ID in place of a local path and add `--allow-downloads`. That flag permits network access for both the main model and nested tokenizer/model loads. Without it, the model arguments must point to existing local directories.

## Create a voice

The following example uses CUDA and FlashAttention 2. Choose a short, natural reference passage; listen to the result before using it for a full book.

```bash
python audiobook_tts.py create-voice \
  --model-path models/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
  --voice-dir voices/my-narrator \
  --instruct "A warm, clear narrator speaking at a relaxed pace." \
  --text "The morning sunlight falls across the window. Take a seat, and let me tell you a story in a calm and familiar voice." \
  --language English \
  --device cuda:0 \
  --dtype bfloat16 \
  --attn-implementation flash_attention_2
```

For reference text stored in a file, replace `--text` with `--input reference.txt`. Use `--input-encoding` if it is not UTF-8.

The command creates:

```text
voices/my-narrator/
├── reference.wav
├── transcript.txt
└── description.txt
```

- `reference.wav` contains the generated voice. Its default encoding is 32-bit floating-point WAV, preserving the model's waveform samples.
- `transcript.txt` contains the exact text passed to voice generation, saved as UTF-8.
- `description.txt` keeps the `--instruct` description; the web app searches voices by it.
- Parent directories are created as needed. Without `--overwrite`, an existing voice directory is rejected. With `--overwrite`, the newly staged files replace the existing voice after generation succeeds, and a `preview.wav` rendered from the old voice is removed; other files in the directory are left alone.
- Copy the files together when moving a voice to another machine. Do not change the transcript independently of its audio.

You do not need to keep the VoiceDesign model on a machine that only narrates books using saved voices.

## Narrate an audiobook

Prepare narration-ready text as `book.txt`, then reuse your voice:

```bash
python audiobook_tts.py narrate \
  --clone-model-path models/Qwen3-TTS-12Hz-1.7B-Base \
  --voice-dir voices/my-narrator \
  --input book.txt \
  --output book.mp3 \
  --language English \
  --device cuda:0 \
  --dtype bfloat16 \
  --attn-implementation flash_attention_2 \
  --batch-size 2
```

For another audiobook, change `--input` and `--output`, but keep the same `--voice-dir`. Narration loads only the Base model and reconstructs one clone prompt from the saved voice. The reference clip itself is not appended to the book.

### One audiobook across multiple GPUs

With durable checkpoints enabled, one narration can use several model workers.
The primary `--device` and every repeated `--worker-device` each load one Base
model and pull the next available chunk batch:

```bash
python audiobook_tts.py narrate \
  --clone-model-path models/Qwen3-TTS-12Hz-1.7B-Base \
  --voice-dir voices/my-narrator \
  --input book.txt \
  --output book.mp3 \
  --resume-dir work/book-chunks \
  --device cuda:0 \
  --worker-device cuda:1 \
  --dtype bfloat16 \
  --attn-implementation sdpa \
  --batch-size 2
```

Faster workers naturally receive more batches. Every result is an atomic WAV
checkpoint, and the coordinator encodes them into the final file in source
order. `--resume-dir` is required because it is the handoff boundary between
workers.

Passwordless SSH workers use the same protocol without requiring shared
storage:

```bash
python audiobook_tts.py narrate \
  --clone-model-path /models/Qwen3-TTS-12Hz-1.7B-Base \
  --voice-dir voices/my-narrator \
  --input book.txt \
  --output book.mp3 \
  --resume-dir work/book-chunks \
  --device cuda:0 \
  --ssh-worker user@spark-one \
  --ssh-worker user@spark-two \
  --ssh-python /opt/qwen/bin/python \
  --ssh-model-path /models/Qwen3-TTS-12Hz-1.7B-Base \
  --ssh-device cuda:0 \
  --dtype bfloat16 \
  --attn-implementation sdpa \
  --batch-size 2
```

Each SSH target must already accept `ssh -o BatchMode=yes TARGET` and have a
compatible Python environment, PyTorch/Qwen TTS stack, and Base model. The
coordinator uses `scp` to stage this script plus `reference.wav` and
`transcript.txt`, sends narration chunks over the SSH process, receives WAV
results, and removes its `/tmp/audiobook-tts-*` workspace. Consequently, every
SSH worker is trusted with the saved voice and narration text. The model itself
is never copied.

### CPU usage

For CPU execution, use `--device cpu --dtype float32 --attn-implementation sdpa` in **either command**. For example, with an existing saved voice:

```bash
python audiobook_tts.py narrate \
  --clone-model-path models/Qwen3-TTS-12Hz-1.7B-Base \
  --voice-dir voices/my-narrator \
  --input book.txt \
  --output book.wav \
  --language English \
  --device cpu \
  --dtype float32 \
  --attn-implementation sdpa \
  --batch-size 1
```

### Working through an OpenAI-compatible server

`--server` narrates without loading a model at all. Each chunk is sent to `POST /v1/audio/speech` on the given endpoint and the returned WAV is encoded into your `--output` file locally, so `--chunk-max-chars`, `--mp3-compression-level`, `--wav-subtype`, and `--overwrite` keep working.

```bash
python audiobook_tts.py narrate \
  --server 192.168.1.50:8880 \
  --server-model tts-1 \
  --server-voice Ryan \
  --input book.txt \
  --output book.mp3 \
  --chunk-max-chars 500
```

The same endpoint can design a voice, with `--instruct` carried in the schema's `instructions` field:

```bash
python audiobook_tts.py create-voice \
  --server 192.168.1.50:8880 \
  --server-model gpt-4o-mini-tts \
  --server-voice ash \
  --voice-dir voices/remote-narrator \
  --instruct "A warm, clear narrator speaking at a relaxed pace." \
  --text "The morning sunlight falls across the window."
```

That writes the usual `reference.wav` + `transcript.txt`, so the voice remains portable and can be cloned **locally** afterwards.

- `--server` accepts `IP:PORT`, `host:port`, or a full URL. A bare authority implies the conventional `/v1` prefix; an explicit path is used as given, so `http://box:8880/v1` and `box:8880` are equivalent.
- `--server-voice` is required: it names a voice **the server already has**, such as a preset speaker or a `clone:Profile` entry where the server supports one.
- `--server-model` defaults to `tts-1`. Many servers encode the language in this name (for example `tts-1-es`), which is why `--language` is refused in server mode—the OpenAI speech schema has no language field.
- `--api-key` sends a bearer token; without it, `OPENAI_API_KEY` is used when set. Local servers usually need neither.
- `--server-timeout` bounds one chunk's request; the default is 300 seconds.
- **Reference clips are never uploaded.** Voice cloning is not part of the OpenAI speech schema, so `--voice-dir` does not apply. Server-side cloning is a server-specific extension; register the voice there and name it with `--server-voice`.
- Because no model is loaded, these flags are **rejected** with `--server`: `--clone-model-path`, `--voice-dir`, `--device`, `--worker-device`, `--ssh-worker`, `--ssh-model-path`, `--dtype`, `--attn-implementation`, `--allow-downloads`, `--seed`, `--batch-size`, and a non-`Auto` `--language`.
- Chunks must come back in one consistent format; if a later chunk changes its sample rate or channel count, the run fails rather than writing mismatched audio.
- Voice **design** works over `--server` too: `--instruct` is sent as the schema's `instructions` field, which shapes `--server-voice` for that one request, and the returned clip plus the exact passage become the saved voice. `tts-1` and `tts-1-hd` are refused there, because the schema documents them as ignoring `instructions`.

### Input and output behavior

- Supply exactly one of `--text` and `--input`.
- UTF-8 input, with or without a BOM, is supported by default. Other encodings require `--input-encoding`.
- Markdown is read **literally**. The script does not strip markup or extract text from PDF/EPUB files.
- `--chunk-max-chars` defaults to `500`. Paragraph and sentence boundaries are preferred; oversized sentences are wrapped, including splitting words longer than the limit.
  `--sentence-chunks` starts a new chunk at every sentence; the web workflow
  enables it so newly generated readers have exact sentence boundaries.
- Output chunks retain book order and use the same saved reference, regardless of batch size.
- The `.mp3` or `.wav` suffix selects the output format. The model supplies the sample rate and mono channel layout.
- WAV narration defaults to `PCM_16`; use `--wav-subtype PCM_24`, for example, to change it. This flag is not valid for MP3 output.
- MP3 accepts `--mp3-compression-level` from `0` to `1`, from minimum to maximum compression. Without it, the encoder's default is used. This flag is not valid for WAV output.
- The audiobook's output directory must already exist. Existing output files require `narrate --overwrite`; that flag still cannot target the saved voice files or input text.
- `--resume-dir DIR` stores one validated WAV checkpoint per completed chunk.
  Reusing the directory skips matching chunks and atomically assembles the final
  output only after every checkpoint exists. Changed text, chunking, voice, or
  inference identity clears incompatible checkpoints.
- The script saves audio. It does not start playback or configure your sound device.

## Web interface

`audiobook_tts_web.py` serves **Hilde**, a browser app over a shared server-side
library with a one-click document-to-audiobook workflow. Configure the TTS
backends once when starting the server; browsers cannot replace that
process-wide configuration:

```bash
python audiobook_tts_web.py \
  --voice-design-model models/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
  --voice-clone-model models/Qwen3-TTS-12Hz-1.7B-Base \
  --storage-root ~/AudiobookTTS \
  --port 8800 --open
```

`example_run.sh` fills in this command for one machine's interpreter and model
paths; edit those for yours. Extra arguments pass through to the server.

`--storage-root` defaults to `~/AudiobookTTS`. The server creates and owns this
fixed layout:

```text
~/AudiobookTTS/
├── Voices/
├── Audiobooks/
│   ├── .readers/
│   └── .versions/
├── Documents/
└── in_progress/
```

- **Voices** contains one directory per narrator: `reference.wav`, the exact
  UTF-8 `transcript.txt` used to create it, its `description.txt`, and, for
  voices made before fixed previews, a rendered `preview.wav`.
- **Documents** contains uploaded files, URL downloads, and prepared narration
  text. URL downloads use an optional filename or infer one from the response.
- **Audiobooks** contains completed MP3 files. Hidden `.readers` and `.versions`
  directories hold content-addressed Markdown/timing sidecars and their commit
  records.
- **in_progress** contains durable source snapshots, extraction checkpoints,
  and narration chunks for unfinished jobs. It is removed for a job only after
  its final audiobook is committed.

Existing files are not migrated automatically when the storage root changes.
Move them into the appropriate directory yourself.

For local backends, run the web server with the interpreter that has `torch`,
`qwen-tts`, and `soundfile`; it launches the CLI through `sys.executable`. A
configured model must be an existing directory unless
`--allow-model-downloads` is set, in which case it may be a Hugging Face ID.
Each role can instead use its own OpenAI-compatible speech server:

| Role | Local/Hugging Face configuration | Remote configuration |
| --- | --- | --- |
| Voice creation | `--voice-design-model PATH_OR_ID` | `--voice-design-server URL` and optional `--voice-design-server-model NAME` |
| Narration | `--voice-clone-model PATH_OR_ID` | `--voice-clone-server URL` and optional `--voice-clone-server-model NAME` |

The local-model and remote-server options are mutually exclusive within each
role. A role left unconfigured stays unavailable. Remote API credentials come
from `OPENAI_API_KEY` in the server environment.

To add homogeneous passwordless SSH narration workers to the web pool:

```bash
python audiobook_tts_web.py \
  --voice-clone-model /models/Qwen3-TTS-12Hz-1.7B-Base \
  --narration-ssh-worker user@spark-one \
  --narration-ssh-worker user@spark-two \
  --narration-ssh-python /opt/qwen/bin/python \
  --narration-ssh-model /models/Qwen3-TTS-12Hz-1.7B-Base
```

`--narration-ssh-model` defaults to the local clone-model value, and the remote
device defaults to `cuda:0`. All configured SSH workers currently share those
Python, model, and device settings.

The pool is process-owned configuration, not a browser setting or a hardcoded
machine count. Local membership is the CUDA devices visible to the server
process; use `CUDA_VISIBLE_DEVICES` to select a deployment-specific subset.
Remote membership comes only from the repeated `--narration-ssh-worker`
startup flags. Restart the server after changing either configuration.

### Creating an audiobook

**Create** walks through three steps, one at a time: **Add your book**,
**Choose a voice**, and **Create audiobook**. A finished step collapses to a
summary with **Change**.

A book is a document from the dropdown, an upload, or a direct HTTP(S) URL
under **Add from a link**. The save-as filename is optional: the server infers
the document type and adds the matching extension when needed, including for
extensionless PDF URLs such as arXiv `/pdf/<id>` links. Files are limited to
64 MiB. Supported document types are PDF (`.pdf`), Markdown (`.md` or
`.markdown`), and plain text (`.txt` or `.text`), rather than HTML landing
pages. The voice step keeps the saved-voice dropdown for voices you know by
name, with **Search voices** beside it for finding one by description.

**Create audiobook** starts the job when a compatible narration worker is idle,
or it waits in the shared queue:
1. A PDF is extracted page by page. Text and Markdown skip extraction unless
   **Adapt the text for listening** is selected.
2. Optional adaptation runs the configured OMP model in bounded concurrent
   paragraph batches. Terminal bibliography sections are omitted; inline
   attributions and later appendices remain.
3. Prepared text is saved as
   `Documents/<input-stem>-narration.txt`.
4. Narration writes
   `Audiobooks/<input-stem>-<voice-name>.mp3`.
5. Sentence-aligned completed WAV chunks supply exact sample boundaries for the
   synchronized narration-ready Markdown reader.
6. TorchAudio `MMS_FA` and Uroman force-align the known transcript to each
   completed chunk on CPU and persist integer-sample word boundaries.

Output directories and filenames are derived; the form never asks for either.
For a remote narration backend, the configured server voice ID is used as the
voice name. When the job finishes, **Start listening** opens the book in
**Listen** and **Download MP3** saves the retained server copy.

Open **Listen** to find a completed audiobook in a table of titles, durations,
and source names. Search looks only at titles; every word you type must
appear, in any order and case. **Listen** opens the book's player and
narration text, whose audio controls stay on screen while the text scrolls,
and **Download MP3** retrieves the retained server copy. Text keeps its paragraphs:
the playing sentence is tinted inside its paragraph, the paragraph is marked
with an accent bar, and the current word is filled. Sentence-era readers
created before paragraph grouping was recorded still show one sentence per
paragraph; paragraph-era readers regain their paragraphs automatically.
Clicking a word seeks to that word (just before its aligned onset); clicking
elsewhere in a sentence, or on its attached figure or table, seeks to the
start of the sentence.
The reader streams the retained MP3's frames, unchanged, inside an exactly
indexed MP4, because browsers seek variable-bitrate MP3 through a coarse table
and then report the requested time while playing audio from up to a minute
away. Browsers without MP3-in-MP4 playback fall back to the plain MP3 and its
approximate seeking. Each sentence cue takes precedence over a stray word cue,
so a word alignment error never outlives its sentence. **Follow narration**
controls auto-scrolling. The active implementation identifies itself as
**Word-synchronized · word seeking** beside the player. The web shell
is served with `Cache-Control: no-store`, so subsequent ordinary refreshes load
the current player code. Straightforward extracted tables remain selectable
Markdown tables, and extracted figures and formulas remain embedded as validated raster
images. Narration-ready descriptions above those visuals receive word timing;
raw table cells and image markup do not consume spoken-word cues. A visual-only
interval remains active for its complete audio cue instead of advancing to the
next text block. Standalone invisible PDF format-control artifacts are removed
before new narration; an existing audiobook's artifact interval is assigned to
the following visible block. When adaptation changes the prose, the reader
shows only the narration-ready version, not a second original-source view. If
forced alignment is partially or fully unavailable, exact sentence timing
remains usable. Existing paragraph-era reader sidecars receive estimated
sentence cues. Audiobooks created before reader sidecars were introduced remain
playable and downloadable without synchronized text.

The queue is global across browsers. With a local narration model, the server
creates one worker for every CUDA device visible to its process and adds any
SSH workers configured at startup. Every web submission uses that server-owned
pool: one job claims all currently idle compatible workers, and each worker
dynamically pulls chunk batches from that audiobook. A second job waits when
the first has claimed the whole pool. A remote OpenAI-compatible narration
backend, or a local host without CUDA, has one fallback worker. Browsers see
each worker's device (GPU index, model, memory) and live state plus active and
waiting jobs. SSH workers appear as numbered SSH workers; SSH targets, server
hostnames, speech-server URLs, and filesystem paths are not published.

A job ID is derived only from the selected document's content version and the
voice version. Submitting that same pair while it is preparing, queued, or
running returns the existing job—even through another browser or under renamed
assets. The first accepted submission supplies the display names, output name,
and runtime settings used by that job.

Documents, voices, prepared text, and audiobook outputs may replace an existing
asset with the same name. The UI asks for confirmation only when the target
audiobook's recorded input-content version **and** voice version both match the
current selection. A changed input or changed voice replaces the old output
without an additional prompt.

Extraction and narration are resumable:

- source bytes and local voice files are snapshotted when the job is queued;
- converted PDF pages and committed OMP paragraph batches are reused;
- each completed narration chunk is an independently validated WAV checkpoint;
- restarting the server or pressing **Stop** leaves the job under
  `in_progress`;
- choosing **Create audiobook** again (or **Continue** / **Try again**) with the
  same document and voice versions reuses matching checkpoints and completes
  the final MP3 atomically;
- checkpoint manifests still reject incompatible extraction or inference
  settings even though those settings are not part of the public job ID.

The active and pending queue order is held by the server process. After a server
restart, submit unfinished document/voice pairs again to resume their durable
`in_progress` stages.

While a job runs, Create names its stage in plain words: Reading your
document, Preparing the narration, Creating the audio, and Finishing your
audiobook. Each stage has its own progress count and ETA; the browser also
retains a smoothed historical duration for the first estimate. A reload returns
to the running job's progress and reconnects to its server-sent event stream
without duplicating received log lines.

### Voices

**Voices** lists every saved voice in a compact table: Preview, Voice name,
Prompt, and Select, 50 rows at a time. The prompt is the voice description
given to VoiceDesign, and search looks only at prompts: every word you type
must appear, in any order and case, so `warm british female` finds voices
whose prompt contains all three. The preview plays in place, and **Select**
makes the voice current and returns to **Create**.

**New voice** asks for a name and a prompt. Every voice reads the same fixed
passage, so previews compare pace, tone, and naturalness on the same words;
the passage is saved as `transcript.txt` beside `reference.wav` and is neither
shown nor editable. **Stop** cancels a voice being created without saving
anything. Listen to the new voice's preview; to refine it, adjust the prompt
and choose **Replace voice**, which regenerates the same name. The new voice is
already selected in **Create**.

Voices made before the fixed passage read other words. Render their comparable
previews once, while no audiobook is being made:

```bash
python audiobook_tts_web.py \
  --voice-clone-model models/Qwen3-TTS-12Hz-1.7B-Base \
  --storage-root ~/AudiobookTTS \
  --render-voice-previews
```

This narrates the fixed passage with each such voice into its `preview.wav`
and exits; the voices themselves are unchanged. Until then, **Voices** notes
that their previews read an older passage. Voices made before prompts were
saved have none, so search cannot find them; write the prompt into the voice
folder's `description.txt` to make one searchable.

**Advanced** (hidden on **Listen**) shows the server's narration workers as
live chips, for example `GPU 0 idle` through `GPU 3 running`, followed by the
device model and memory; hover a chip for its job. It also names the narration
and voice-design models and the device voice creation uses, and holds
precision/attention tuning, narration chunk and batch controls, OMP
adaptation settings, and **Reference WAV encoding**. Browsers cannot select,
pin, or name devices and cannot configure SSH hosts. The reference WAV setting
remains because the generated sample is required for local voice cloning; it
is not an output-location choice.

Text adaptation uses OMP's default model unless you pick another under
**Advanced**. **Add local** connects a model server on your network. Choose its
type, **Ollama** or **OpenAI-compatible** (SGLang, vLLM, LM Studio), enter its
`host:port`, for example `127.0.0.1:8010`, then choose one of its models. The
model follows the instructions in `prompts/PAPER-AUDIO-BOOK.md`; each job reads
them when it starts, so edits apply to the next job.

### Access and browser state

The web server has no built-in authentication or authorization. **Do not expose
it directly to the public Internet.** Anyone who can reach it can read or
replace shared assets, submit or cancel jobs, and stop running work. A public
deployment needs an authenticated TLS reverse proxy plus request/rate limits.
Use `--host 127.0.0.1` when the reverse proxy runs on the same machine.
Cross-origin POST rejection is CSRF hardening, not access control.

Editable form settings are stored in bounded `HttpOnly; SameSite=Strict`
cookies per browser. Model paths, model IDs, speech-server endpoints, worker
devices/hosts, credentials, storage paths, and output paths remain
server-owned. Public API responses name local devices and models but omit
hostnames, SSH targets, speech-server URLs, and paths. **AirDrop…** appears
only when the server runs on macOS with `pyobjc-framework-Cocoa`; other
clients use **Download MP3**.

The server probes PyTorch-visible CUDA and MPS devices in a short-lived child.
It counts CUDA devices after initialization, so a GPU the runtime cannot open,
such as one waiting for a reset, is skipped and the remaining GPUs stay in the
pool instead of the server falling back to CPU. The pool is fixed at startup;
restart the server after GPUs are added, removed, or recovered.
Local audiobook jobs claim all idle local CUDA and configured SSH workers.
Voice creation resolves the server-managed device to the first CUDA device,
then MPS, then CPU. Unsupported precision or `flash_attention_2` combinations
still fail through CLI validation.

## Batching

All batch sizes reuse the same voice prompt. Batching controls memory use and throughput—not narrator identity.

`--batch-size N` generates up to `N` chunks per clone call; `0`, the default,
submits the whole book on one device. In distributed mode it is the number of
chunks one worker takes at a time, and `0` becomes one chunk per worker so
faster workers can pull more work. The web app's **Batch size** (under
**Advanced**) defaults to 2.

Larger batches narrate faster until GPU memory runs out. On an RTX PRO 6000
with about 6 GiB left beside another model, one sentence per call narrated at
1.6× real time and two at 2.6×; four or more ran out of memory. Each sentence
needed about 0.7 GiB on top of the 4 GiB model.

A batch that runs out of CUDA memory is retried one chunk at a time, so a batch
size that is too large costs time rather than the book. A single chunk that
still does not fit fails the run; with `--resume-dir`, rerunning the same
command reuses the completed chunks.

Changing batch sizes can change sampled audio even with the same `--seed`. A saved reference establishes the shared speaker reference, not bit-for-bit reproducibility across hardware, batching decisions, or library versions.

## Command reference

```bash
python audiobook_tts.py --help
python audiobook_tts.py create-voice --help
python audiobook_tts.py narrate --help
```

### Shared options

| Flag | Default / requirement |
| --- | --- |
| `--device` | Required for local inference; for example, `cuda:0` or `cpu`. Rejected with `--server`. |
| `--attn-implementation` | Required for local inference: `flash_attention_2`, `sdpa`, or `eager`. Rejected with `--server`. |
| `--dtype` | `auto`: CUDA uses bfloat16 when supported, otherwise float16; other devices use float32. Also accepts an explicit `float32`, `float16`, or `bfloat16`. |
| `--text` / `--input` | Exactly one is required. |
| `--input-encoding` | `utf-8-sig`; applies to `--input`. |
| `--language` | `Auto`; otherwise use a language supported by the selected model. |
| `--seed` | Unset; optional integer from `0` to `4294967295`. |
| `--allow-downloads` | Disabled; explicitly permits Hugging Face network access. |

### Voice creation options

| Flag | Default / requirement |
| --- | --- |
| `--model-path` | Required VoiceDesign model directory or permitted Hub ID. |
| `--voice-dir` | Required destination directory; an existing directory requires `--overwrite`. |
| `--overwrite` | Disabled; replace the existing `reference.wav`, `transcript.txt`, and `description.txt` after the new files are staged, removing a `preview.wav` rendered from the old voice. |
| `--instruct` | Required nonempty voice description. |
| `--wav-subtype` | `FLOAT`; choices: `PCM_16`, `PCM_24`, `PCM_32`, `FLOAT`, `DOUBLE`. |

### Narration options

| Flag | Default / requirement |
| --- | --- |
| `--clone-model-path` | Required Base model directory or permitted Hub ID, unless `--server` is used. |
| `--voice-dir` | Required existing saved voice directory, unless `--server` is used. |
| `--output` | Required `.mp3` or `.wav` destination. |
| `--chunk-max-chars` | `500`; must be positive. |
| `--batch-size` | `0` for all chunks on one device and one chunk per distributed worker; positive values set the chunks per clone call. A batch that runs out of CUDA memory is retried one chunk at a time. |
| `--worker-device` | Unset; repeat to add local devices to one resumable narration. |
| `--ssh-worker` | Unset; repeat passwordless `HOST` or `USER@HOST` targets. |
| `--ssh-python` | `python3`; Python executable shared by SSH workers. |
| `--ssh-model-path` | Uses `--clone-model-path`; remote model path or permitted Hub ID. |
| `--ssh-device` | `cuda:0`; device used on every SSH worker. |
| `--wav-subtype` | `PCM_16` for WAV; choices as above. |
| `--mp3-compression-level` | Encoder default; optional value from `0` to `1` for MP3 only. |
| `--overwrite` | Disabled; allows replacing an audiobook, never its saved reference or input file. |
| `--resume-dir` | Unset; durable narration chunk directory used to resume matching work; required with local or SSH worker additions. |

### Server options (either command)

| Flag | Default / requirement |
| --- | --- |
| `--server` | Unset; `IP:PORT`, `host:port`, or a URL. A bare authority implies `/v1`. Replaces local inference. |
| `--server-model` | `tts-1`; the model name sent to the server. `create-voice` refuses `tts-1`/`tts-1-hd`, which ignore `instructions`. |
| `--server-voice` | Required with `--server`; a voice the server already holds. Voice design shapes it with `--instruct`. |
| `--server-timeout` | `300`; seconds allowed for one chunk's response. |
| `--api-key` | Unset; falls back to `OPENAI_API_KEY`, then sends no Authorization header. |

## Troubleshooting

### CUDA out of memory

- A batch that runs out of memory is retried one chunk at a time. If the log
  often says so, lower `--batch-size` (the web app's **Batch size**) to skip
  the wasted attempt.
- Reduce `--chunk-max-chars` when individual chunks are too expensive.
- Other GPU processes, such as another model server, leave less memory for
  narration.
- Without `--resume-dir`, a failed run can leave partial output. With it,
  restart the same command to reuse validated chunks; use `--overwrite` as
  usual if the final destination already exists.

### FlashAttention import or build errors

Confirm that the FlashAttention build matches your PyTorch and CUDA installation. A GPU driver's reported CUDA capability is not the same as the installed CUDA compiler/toolkit. Use a compatible wheel or toolkit; consult the linked upstream installation instructions. To avoid FlashAttention, select `--attn-implementation sdpa`.

### TorchAudio undefined-symbol errors

Install matching PyTorch and TorchAudio versions from the same CPU/CUDA build family. A package importing successfully elsewhere does not establish ABI compatibility in this environment.

### Missing SoX or unsupported MP3 encoding

The Python `sox` package and the SoX executable are separate. If Qwen reports a missing executable, install SoX through your operating system's package manager.

MP3 writing depends on the installed SoundFile/libsndfile build. If the script reports unsupported MP3 encoding, use WAV or install a build with MP3 support.

### Missing or invalid saved voice

A voice folder needs both `reference.wav` and a nonempty UTF-8 `transcript.txt`. The WAV must contain nonempty, finite, non-silent mono audio. Move both files together and keep the transcript matched to the spoken reference. Do not point `--voice-dir` at a model directory.

### No sound during playback

CLI generation does not play the file automatically. In the web interface,
open a completed audiobook under **Listen**. Check the browser and selected
output device if its player remains silent. A Linux sound server exposing only
**Dummy Output** has no real playback sink; that does not establish that the
generated file is silent.

## Tests

```bash
python -m unittest -v test_audiobook_tts
```

The 61-test regression suite covers voice round trips and replacement
(including stale prompts and previews), shared asset naming,
document/voice-only job identity, gang scheduling across local and SSH
workers, internal device pinning, FIFO scheduling and deduplication, GPU
enumeration when CUDA cannot open one device, public worker and model
configuration without hosts or paths, exact-version overwrite confirmation,
browser-cookie isolation, GPU-preferred resolution, extensionless document
URLs, resumable extraction and narration, the unified PDF-to-MP3 workflow, the
persisted Listen page, Create step, and open book, voice and library catalogs
with fixed-passage previews, library entries refreshed after a book is
narrated again, preview refusal for voice folders linked from outside the
library, preview rendering that never publishes a failed clip, exact and
legacy-estimated sentence readers, persisted word cues, tables
and embedded images, lossless exactly indexed reader audio with keep-alive
byte ranges, safe retained-MP3 downloads, job-specific SSE reconnection,
batches retried one chunk at a time after running out of memory, the batch
size default for older browser settings, bounded adaptation
concurrency/context, ordered commits, and local model servers listed by the
type you choose, refusing a type or model that doesn't match the server. It
does not load TTS models or require a GPU.

How the code fits together (routes, storage, browser state, and the invariants
every change must keep) is described in [ARCHITECTURE.md](ARCHITECTURE.md).
Coding agents also load the short [AGENTS.md](AGENTS.md).

## Acknowledgments

Speech generation is provided by [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS); GPU attention acceleration uses [FlashAttention](https://github.com/Dao-AILab/flash-attention). Follow the applicable model and dependency licenses, and use texts and voices you have the rights to use.
