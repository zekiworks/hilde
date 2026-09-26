<p align="center">
  <img src="assets/hilde-dark.png" alt="Hilde logo" width="128">
</p>

<h1 align="center">Hilde</h1>

<p align="center"><strong>Create a narrator once. Reuse that voice across your audiobooks.</strong></p>

An audiobook studio built on [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS). The Hilde web app turns PDF, Markdown, or text documents into narrated MP3 audiobooks with a synchronized reader. Underneath it, the `audiobook_tts.py` command-line tool designs a voice from a written description, saves a portable reference, and turns narration-ready text into one MP3 or WAV file. Run locally on CPU or CUDA, with optional FlashAttention 2 and batched narration.

Giving every text chunk the same voice description does **not** establish a shared speaker identity—even when those chunks are generated in one batch. Hilde therefore separates voice creation from narration: a **VoiceDesign** model generates a reference clip once and saves it with its exact transcript, and a **Base** model clones that saved reference for every chunk, across batches, books, and sessions. The speaker reference stays the same; pacing, expression, and sampled audio can still vary.

Hilde is released under the [MIT License](LICENSE). Speech generation is provided by Qwen3-TTS; GPU attention acceleration uses [FlashAttention](https://github.com/Dao-AILab/flash-attention). Follow the applicable model and dependency licenses, and use texts and voices you have the rights to use. [ARCHITECTURE.md](ARCHITECTURE.md) describes how the code fits together and how to test a change.

- [Installation](#installation)
- [Configuration](#configuration)
- [Web UI](#web-ui)
- [Command line](#command-line)

## Installation

Python **3.12** is the tested version. Shell examples use Bash; on Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell instead.

### 1. Get the code

```bash
git clone https://github.com/zekiworks/hilde.git
cd hilde
```

Run the remaining commands from this directory.

### 2. Create an environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 3. Install PyTorch, TorchAudio, and the dependencies

Choose **one** build appropriate for your platform. Keep PyTorch and TorchAudio versions matched, from the same CPU/CUDA build family: a mismatched TorchAudio fails with undefined-symbol errors, even when the same package imports successfully elsewhere. See the [official PyTorch installation guidance](https://pytorch.org/get-started/locally/) for other platforms and CUDA builds.

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

- MP3 writing depends on the installed SoundFile/libsndfile build. If Hilde
  reports unsupported MP3 encoding, use WAV or install a build with MP3 support.
- The Python `sox` package and the SoX executable are separate. If Qwen reports
  a missing executable, install SoX through your operating system's package
  manager.
- The web reader force-aligns transcript words with TorchAudio's `MMS_FA`
  bundle and Uroman. The first alignment downloads and caches the approximately
  1.2 GB MMS model through TorchAudio. Alignment runs on CPU after narration, so
  it does not compete with the narration workers for GPU memory.

### 4. Optional: install FlashAttention 2

FlashAttention is needed only when selecting `--attn-implementation flash_attention_2`. It requires compatible CUDA hardware and a compatible build for your PyTorch installation. It cannot be used with CPU or float32 inference.

```bash
python -m pip install setuptools ninja packaging wheel
MAX_JOBS=4 python -m pip install flash-attn==2.8.3 --no-build-isolation
```

Use a matching [official FlashAttention wheel](https://github.com/Dao-AILab/flash-attention/releases) when available. Source builds require a compatible CUDA toolkit and C++ compiler; set `CUDA_HOME` to **your** toolkit installation if needed. If the import or build fails, confirm that the build matches your PyTorch and CUDA installation: a GPU driver's reported CUDA capability is not the same as the installed CUDA compiler/toolkit. See the [FlashAttention installation instructions](https://github.com/Dao-AILab/flash-attention#installation-and-features) for compatibility details.

The GPU workflow has been exercised on Linux with Python 3.12, PyTorch/TorchAudio 2.10.0 + CUDA 13.0, Qwen TTS 0.1.1, SoundFile 0.14.0, and FlashAttention 2.8.3. These versions are a tested combination, not a guarantee for every platform.

You can skip FlashAttention and select SDPA attention instead: `--attn-implementation sdpa`, or `sdpa` under **Advanced** in the web UI. CPU inference with SDPA has also been exercised.

### 5. Download the models

Hilde uses two Qwen3-TTS models:

| Model | Used for | Web server | Command line |
| --- | --- | --- | --- |
| [Qwen3-TTS-12Hz-1.7B-VoiceDesign](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign) | Designing a voice from a description | `--voice-design-model` | `create-voice --model-path` |
| [Qwen3-TTS-12Hz-1.7B-Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) | Narrating with a saved voice | `--voice-clone-model` | `narrate --clone-model-path` |

Download complete model directories, including their tokenizer files:

```bash
hf download Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
  --local-dir models/Qwen3-TTS-12Hz-1.7B-VoiceDesign

hf download Qwen/Qwen3-TTS-12Hz-1.7B-Base \
  --local-dir models/Qwen3-TTS-12Hz-1.7B-Base
```

These are example locations, not built-in defaults. Supply the paths you chose when running Hilde. With local directories, model loading stays offline by default. A machine that only narrates, with the stock voices in `voices/` or other saved voices, needs only the Base model.

Alternatively, pass a Hugging Face model ID in place of a local path and allow downloads: `--allow-downloads` on the command line, or `--allow-model-downloads` for the web server. That permits network access for both the main model and nested tokenizer/model loads. Without it, the model options must point to existing local directories.

### 6. Optional: install OMP for text adaptation

The web app can adapt a document for listening before narrating it; see
[Text adaptation](#text-adaptation). It runs the adaptation model through the
`omp` command of [OMP](https://github.com/can1357/oh-my-pi), so install OMP
where the web server finds `omp` on its `PATH`. Everything else works without
it.

## Configuration

`audiobook_tts_web.py` serves **Hilde**, a browser app over a shared
server-side library. Its configuration comes from command-line options when the
server starts; browsers cannot replace that process-wide configuration.
Settings each browser may change, such as precision, chunking, batch size, and
the text-adaptation model, are under **Advanced** in the page; see
[Advanced](#advanced).

### Start the server

```bash
python audiobook_tts_web.py \
  --voice-design-model models/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
  --voice-clone-model models/Qwen3-TTS-12Hz-1.7B-Base \
  --port 8800 --open
```

For local backends, run the web server with the interpreter that has `torch`,
`qwen-tts`, and `soundfile`; it launches the CLI through `sys.executable`.
`example_run.sh` fills in this command for one machine's interpreter and model
paths; edit those for yours. Extra arguments pass through to the server.

| Option | Default / meaning |
| --- | --- |
| `--host` | `0.0.0.0`, every interface; see [Access and security](#access-and-security). |
| `--port` | `8800`. |
| `--open` | Opens the page in a browser. |
| `--verbose` | Logs every request to stderr. |
| `--storage-root` | `User/` in the project folder; see [Storage](#storage). |
| `--voice-design-model` | VoiceDesign model directory, or a Hugging Face ID with `--allow-model-downloads`. |
| `--voice-design-server`, `--voice-design-server-model` | A speech server for voice design instead of the model; the server model defaults to `gpt-4o-mini-tts`. See [Speech models](#speech-models). |
| `--voice-clone-model` | Base model directory, or a Hugging Face ID with `--allow-model-downloads`. |
| `--voice-clone-server`, `--voice-clone-server-model` | A speech server for narration instead of the model; the server model defaults to `tts-1`. |
| `--allow-model-downloads` | Disabled; permits Hugging Face model IDs and downloads. |
| `--narration-ssh-worker` | Unset; repeat to add passwordless SSH narration workers. See [Narration workers](#narration-workers). |
| `--narration-ssh-python` | `python3`; the Python executable on every SSH worker. |
| `--narration-ssh-model` | The `--voice-clone-model` value; the Base model path or ID on every SSH worker. |
| `--narration-ssh-device` | `cuda:0`; the PyTorch device on every SSH worker. |
| `--render-voice-previews` | Renders comparable previews for older voices, then exits. See [Voices](#voices). |

### Storage

`--storage-root` defaults to `User/` in the project folder, which git ignores.
The server creates and owns this fixed layout:

```text
User/
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
Move them into the appropriate directory yourself. The web app's **Delete**
buttons remove voices, documents, and audiobooks with their reader files.

A new library starts with the stock voices from the repository's `voices/`
folder. They are copied only when the library is created, so a stock voice you
delete stays deleted.

Because `User/` lives in the project folder, deleting or re-cloning the folder,
or running `git clean -x`, deletes your library as well. Back it up, or pass
`--storage-root` to keep the library somewhere else.

### Speech models

A configured model must be an existing directory unless
`--allow-model-downloads` is set, in which case it may be a Hugging Face ID.
Each role can instead use its own OpenAI-compatible speech server:

| Role | Local/Hugging Face configuration | Remote configuration |
| --- | --- | --- |
| Voice creation | `--voice-design-model PATH_OR_ID` | `--voice-design-server URL` and optional `--voice-design-server-model NAME` |
| Narration | `--voice-clone-model PATH_OR_ID` | `--voice-clone-server URL` and optional `--voice-clone-server-model NAME` |

The local-model and remote-server options are mutually exclusive within each
role. A role left unconfigured stays unavailable. Remote API credentials come
from `OPENAI_API_KEY` in the server environment.

### Narration workers

With a local narration model, the server creates one worker for every CUDA
device visible to its process and adds any SSH workers configured at startup.
A remote OpenAI-compatible narration backend, or a local host without CUDA, has
one fallback worker.

The pool is process-owned configuration, not a browser setting or a hardcoded
machine count. Local membership is the CUDA devices visible to the server
process; use `CUDA_VISIBLE_DEVICES` to select a deployment-specific subset, for
example to leave GPU 0 to another program:

```bash
CUDA_VISIBLE_DEVICES=1,2,3 ./example_run.sh
```

Remote membership comes only from the repeated `--narration-ssh-worker` startup
flags. To add homogeneous passwordless SSH narration workers to the web pool:

```bash
python audiobook_tts_web.py \
  --voice-clone-model /models/Qwen3-TTS-12Hz-1.7B-Base \
  --narration-ssh-worker user@spark-one \
  --narration-ssh-worker user@spark-two \
  --narration-ssh-python /opt/qwen/bin/python \
  --narration-ssh-model /models/Qwen3-TTS-12Hz-1.7B-Base
```

`--narration-ssh-model` defaults to the local clone-model value, and
`--narration-ssh-device` defaults to `cuda:0`. All configured SSH workers
currently share those Python, model, and device settings. Each target must meet
the requirements in [Multiple GPUs and SSH workers](#multiple-gpus-and-ssh-workers)
and is trusted with the saved voice and narration text.

The server probes PyTorch-visible CUDA and MPS devices in a short-lived child.
It counts CUDA devices after initialization, so a GPU the runtime cannot open,
such as one waiting for a reset, is skipped and the remaining GPUs stay in the
pool instead of the server falling back to CPU. The pool is fixed at startup:
restart the server after changing `CUDA_VISIBLE_DEVICES` or the SSH workers,
or after GPUs are added, removed, or recovered. Voice creation resolves the
server-managed device to the first CUDA device, then MPS, then CPU.
Unsupported precision or `flash_attention_2` combinations still fail through
CLI validation.

### Batch size

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
size that is too large costs time rather than the book. If the log often says
so, lower the batch size to skip the wasted attempt, and reduce
`--chunk-max-chars` (**Chunking** under **Advanced**) when individual chunks are
too expensive. Other GPU processes, such as another model server, leave less
memory for narration. A single chunk that still does not fit fails the run;
with `--resume-dir`, rerunning the same command reuses the completed chunks.

Changing batch sizes can change sampled audio even with the same `--seed`. A saved reference establishes the shared speaker reference, not bit-for-bit reproducibility across hardware, batching decisions, or library versions.

### Text adaptation

**Adapt the text for listening**, in the **Create audiobook** step, rewrites a
document so it sounds natural read aloud before it is narrated. Terminal
bibliography sections are omitted; inline attributions and later appendices
remain. Adaptation needs [OMP](#6-optional-install-omp-for-text-adaptation) on
the server. While adaptation is selected, its settings appear under
**Advanced** in **Text adaptation**:

- **Model** uses OMP's default model unless you pick another.
- **OpenAI** signs OMP in with ChatGPT. OMP stores and refreshes the credential
  in the server user's private credential database.
- **Add local** connects a model server on your network. Choose its type,
  **Ollama** or **OpenAI-compatible** (SGLang, vLLM, LM Studio), enter its
  `host:port`, for example `127.0.0.1:8010`, then choose one of its models.
- **Workers** (default 4, at most 32) is how many paragraph batches are adapted
  at once, and **Paragraphs per worker** (default 1, at most 32) is how many
  paragraphs each batch holds.

The model follows the instructions in `prompts/PAPER-AUDIO-BOOK.md`; each job
reads them when it starts, so edits apply to the next job.

### Access and security

The web server has no built-in authentication or authorization, and by default
it listens on every interface. **Do not expose it directly to the public
Internet.** Anyone who can reach it can read or replace shared assets, submit
or cancel jobs, and stop running work. A public deployment needs an
authenticated TLS reverse proxy plus request/rate limits. Use
`--host 127.0.0.1` to accept connections only from this machine, for example
when the reverse proxy runs on it. Cross-origin POST rejection is CSRF
hardening, not access control.

## Web UI

Open the address the server prints when it starts, for example
`http://127.0.0.1:8800/`; `--open` opens it for you. The page has three tabs,
**Create**, **Voices**, and **Listen**, plus **Advanced** for settings.

### Create an audiobook

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

**Delete** beside the dropdown removes the chosen document after you confirm.
Audiobooks made from it are kept, and a job already queued keeps its own copy.

**Create audiobook** starts the job when a compatible narration worker is idle,
or it waits in the shared queue:
1. A PDF is extracted page by page. Text and Markdown skip extraction unless
   **Adapt the text for listening** is selected.
2. Optional adaptation runs the configured OMP model in bounded concurrent
   paragraph batches; see [Text adaptation](#text-adaptation).
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

While a job runs, Create names its stage in plain words: Reading your
document, Preparing the narration, Creating the audio, and Finishing your
audiobook. Each stage has its own progress count and ETA; the browser also
retains a smoothed historical duration for the first estimate. A reload returns
to the running job's progress and reconnects to its server-sent event stream
without duplicating received log lines.

The queue is global across browsers. Every web submission uses the
server-owned pool of [narration workers](#narration-workers): one job claims
all currently idle compatible workers, and each worker dynamically pulls chunk
batches from that audiobook. A second job waits when the first has claimed the
whole pool. Browsers see each worker's device (GPU index, model, memory) and
live state plus active and waiting jobs. SSH workers appear as numbered SSH
workers; SSH targets, server hostnames, speech-server URLs, and filesystem
paths are not published.

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

### Listen

Open **Listen** to find a completed audiobook in a table of titles, durations,
and source names. Search looks only at titles; every word you type must
appear, in any order and case. **Delete** removes an audiobook and its
synchronized text after you confirm. **Listen** opens the book's player and
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

If the player remains silent, check the browser and selected output device. A
Linux sound server exposing only **Dummy Output** has no real playback sink;
that does not establish that the generated file is silent.

### Voices

**Voices** lists every saved voice in a compact table: Preview, Voice name,
Prompt, and **Select** and **Delete** buttons, 50 rows at a time. The prompt is
the voice description given to VoiceDesign, and search looks only at prompts:
every word you type must appear, in any order and case, so
`warm british female` finds voices whose prompt contains all three. The
preview plays in place, and **Select** makes the voice current and returns to
**Create**. **Delete** removes a voice after you confirm. A voice that is being
created can't be deleted until it finishes or you stop it; audiobooks made
with a deleted voice are kept, and a job already queued keeps its own copy.

A new library comes with eight stock voices, each with its prompt: Balder,
Bragi, Mimir, and Vidar (male) and Eir, Freyja, Idun, and Sigrun (female).

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
  --render-voice-previews
```

This narrates the fixed passage with each such voice into its `preview.wav`
and exits; the voices themselves are unchanged. Until then, **Voices** notes
that their previews read an older passage. Voices made before prompts were
saved have none, so search cannot find them; write the prompt into the voice
folder's `description.txt` to make one searchable.

### Advanced

**Advanced**, beside the tabs and hidden on **Listen**, shows the server's
narration workers and the settings a browser may change:

- **This server** shows the narration workers as live chips, for example
  `GPU 0 idle` through `GPU 3 running`, followed by the device model and
  memory; hover a chip for its job. It also names the narration and
  voice-design models and the device voice creation uses.
- **Speech tuning**: precision and attention implementation, language, text
  encoding, and an optional seed.
- **Narration**, on **Create**: **Chunking**, the longest chunk in characters;
  [**Batch size**](#batch-size); and **MP3 compression**.
- **Text adaptation**, on **Create** while adaptation is selected; see
  [Text adaptation](#text-adaptation).
- **Voice files**, on **Voices**: **Reference WAV encoding**. It remains
  because the generated sample is required for local voice cloning; it is not
  an output-location choice.

Settings a remote speech server cannot use are disabled. Browsers cannot
select, pin, or name devices and cannot configure SSH hosts.

### Browser state

Editable form settings are stored in bounded `HttpOnly; SameSite=Strict`
cookies per browser. Model paths, model IDs, speech-server endpoints, worker
devices/hosts, credentials, storage paths, and output paths remain
server-owned. Public API responses name local devices and models but omit
hostnames, SSH targets, speech-server URLs, and paths. **AirDrop…** appears
only when the server runs on macOS with `pyobjc-framework-Cocoa`; other
clients use **Download MP3**.

## Command line

`audiobook_tts.py` works on its own, and the web server runs it for every voice
and audiobook. It has two commands:

1. **`create-voice`** uses a **VoiceDesign** model to generate a reference clip and save its exact transcript.
2. **`narrate`** uses a **Base** model to clone that saved reference for every chunk, across batches, books, and process sessions.

Only the Base model is needed after you have created a voice. The examples use
the model directories from [Installation](#5-download-the-models) and Freyja,
one of the stock voices in the repository's `voices/` folder.

### Create a voice

The following example uses CUDA and FlashAttention 2. Choose a short, natural reference passage; listen to the result before using it for a full book.

```bash
python audiobook_tts.py create-voice \
  --model-path models/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
  --voice-dir User/Voices/My-Narrator \
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
User/Voices/My-Narrator/
├── reference.wav
├── transcript.txt
└── description.txt
```

Saving into the web library's `Voices` folder, as here, makes the voice appear
in Hilde too; any other folder works for the command line alone.

- `reference.wav` contains the generated voice. Its default encoding is 32-bit floating-point WAV, preserving the model's waveform samples.
- `transcript.txt` contains the exact text passed to voice generation, saved as UTF-8.
- `description.txt` keeps the `--instruct` description; the web app searches voices by it.
- Parent directories are created as needed. Without `--overwrite`, an existing voice directory is rejected. With `--overwrite`, the newly staged files replace the existing voice after generation succeeds, and a `preview.wav` rendered from the old voice is removed; other files in the directory are left alone.
- A voice is a WAV and a UTF-8 transcript, not serialized model tensors. Copy the files together when moving a voice to another machine. Do not change the transcript independently of its audio.
- Narration needs both `reference.wav` and a nonempty UTF-8 `transcript.txt`. The WAV must contain nonempty, finite, non-silent mono audio. Do not point `--voice-dir` at a model directory.

### Narrate an audiobook

Prepare narration-ready text as `book.txt`, then narrate it with a saved voice:

```bash
python audiobook_tts.py narrate \
  --clone-model-path models/Qwen3-TTS-12Hz-1.7B-Base \
  --voice-dir voices/Freyja \
  --input book.txt \
  --output book.mp3 \
  --language English \
  --device cuda:0 \
  --dtype bfloat16 \
  --attn-implementation flash_attention_2 \
  --batch-size 2
```

For another audiobook, change `--input` and `--output`, but keep the same `--voice-dir`. Narration loads only the Base model and reconstructs one clone prompt from the saved voice. The reference clip itself is not appended to the book. See [Batch size](#batch-size) for choosing `--batch-size`.

### Multiple GPUs and SSH workers

With durable checkpoints enabled, one narration can use several model workers.
The primary `--device` and every repeated `--worker-device` each load one Base
model and pull the next available chunk batch:

```bash
python audiobook_tts.py narrate \
  --clone-model-path models/Qwen3-TTS-12Hz-1.7B-Base \
  --voice-dir voices/Freyja \
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
  --voice-dir voices/Freyja \
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

### CPU only

For CPU execution, use `--device cpu --dtype float32 --attn-implementation sdpa` in **either command**. For example, with an existing saved voice:

```bash
python audiobook_tts.py narrate \
  --clone-model-path models/Qwen3-TTS-12Hz-1.7B-Base \
  --voice-dir voices/Freyja \
  --input book.txt \
  --output book.wav \
  --language English \
  --device cpu \
  --dtype float32 \
  --attn-implementation sdpa \
  --batch-size 1
```

### Through an OpenAI-compatible speech server

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
  --voice-dir User/Voices/Remote-Narrator \
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

### Input and output

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
- Without `--resume-dir`, a failed run can leave partial output. With it,
  restart the same command to reuse validated chunks; use `--overwrite` as
  usual if the final destination already exists.
- The script saves audio. It does not start playback or configure your sound device.

### Options

```bash
python audiobook_tts.py --help
python audiobook_tts.py create-voice --help
python audiobook_tts.py narrate --help
```

#### Shared options

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

#### Voice creation options

| Flag | Default / requirement |
| --- | --- |
| `--model-path` | Required VoiceDesign model directory or permitted Hub ID. |
| `--voice-dir` | Required destination directory; an existing directory requires `--overwrite`. |
| `--overwrite` | Disabled; replace the existing `reference.wav`, `transcript.txt`, and `description.txt` after the new files are staged, removing a `preview.wav` rendered from the old voice. |
| `--instruct` | Required nonempty voice description. |
| `--wav-subtype` | `FLOAT`; choices: `PCM_16`, `PCM_24`, `PCM_32`, `FLOAT`, `DOUBLE`. |

#### Narration options

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

#### Speech-server options (either command)

| Flag | Default / requirement |
| --- | --- |
| `--server` | Unset; `IP:PORT`, `host:port`, or a URL. A bare authority implies `/v1`. Replaces local inference. |
| `--server-model` | `tts-1`; the model name sent to the server. `create-voice` refuses `tts-1`/`tts-1-hd`, which ignore `instructions`. |
| `--server-voice` | Required with `--server`; a voice the server already holds. Voice design shapes it with `--instruct`. |
| `--server-timeout` | `300`; seconds allowed for one chunk's response. |
| `--api-key` | Unset; falls back to `OPENAI_API_KEY`, then sends no Authorization header. |
