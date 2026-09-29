
<p align="center">
  <img width="400" height="400" alt="hilde-avatar-light" src="https://github.com/user-attachments/assets/22672d4b-1c01-4589-a6e0-70910e516fa4" />
</p>

<h1 align="center">Hilde</h1>

<p align="center"><strong>Turn papers into audiobooks, figures included.</strong><br>
One narrator from start to finish, a synced reader, and every GPU in your house.</p>

> [!WARNING]
> 🚧 **Work in progress.** Early version, things may break. First release coming soon. Feedback and issues welcome.

https://github.com/user-attachments/assets/3bbf7fd4-e17e-487b-81cd-e5916ba34db2

<p align="center"><sub>Demo: Figure 2 of Vaswani et al., <a href="https://arxiv.org/abs/1706.03762">“Attention Is All You Need”</a> (2017), narrated with Hilde.</sub></p>

## What it does

- **Figures, read aloud.** Before narration, a language model rewrites the
  paper for listening and describes each figure, chart, and table at the point
  where the text introduces it.
- **Written for the ear.** Page headers, footers, the table of contents, and
  the bibliography are removed before any model sees the text. Adaptation also
  leaves out section numbers, cross-references, citation marks, and email
  addresses, and turns formulas, notation, and tables into plain words. See
  [How Hilde reads a paper](#how-hilde-reads-a-paper).
- **One narrator, start to finish.** A voice is designed once from a written
  description, then cloned for every chunk of every book, across sessions.
  Eight stock voices are included.
- **A reader that follows along.** The current word lights up as it is spoken;
  click any word to jump there.
- **Every GPU in the house.** Narration spreads across all local GPUs and, over
  SSH, other machines. A busy local GPU joins once it has room, and one that
  runs out of memory hands its chunks to the others.
- **Local first.** Speech is generated on your own hardware by default. Text
  adaptation runs on a local model server, or on OpenAI or Anthropic if you
  connect one.
- **Resumable.** Press **Stop** or restart the server: finished chunks are kept,
  and creating the same audiobook again picks up where it left off.

## How Hilde reads a paper

Plain text-to-speech reads a paper exactly as printed. Hilde first rewrites it
for someone listening, who cannot skim, glance back, or see the page.

| On the page | Read as printed | Read by Hilde |
| --- | --- | --- |
| **Table of contents** | "Contents. Part one. Context and history. Page 3. Section I.1. Need for an actionable definition… Page 3. Section I.2…" | *Skipped. The narration goes straight to the first chapter.* |
| **Numbered headings**<br>`I.1 Need for an actionable definition…` | "I point one. Need for an actionable definition…" | "Need for an actionable definition and measure of intelligence." |
| **Cross-references**<br>`We noted in II.1.1 that…` | "We noted in Section Two point One point One that…" | "We noted earlier that…" |
| **Formulas** | "I superscript theta sub T, sub I S comma scope, equals the average over tasks T in the scope of omega sub T times theta sub T…" | "Intelligence across a given scope is the average, over the tasks in that scope, of the system's skill-acquisition efficiency…" |
| **Notation**<br>`we will denote θ^max_T,IS as Θ` | "…we will denote theta superscript max, sub T comma I S, as capital theta." | *Skipped. Every quantity is named in words.* |
| **Citation marks**<br>`…Loebner Prize [75]` | "…Loebner Prize seventy-five…" | "…the Total Turing Test and Loebner Prize…" |
| **Contact details**<br>`author@example.com` | "author at example dot com" | *Skipped.* |
| **Figures** | The labels inside the image, jumbled: "G General intelligence Extreme generalization Broad Broad Broad…" | "The figure presents intelligence as a hierarchy. At the bottom, task-specific skills support only local generalization…" |
| **Tables and long lists** | Every cell and every item, in order | What they show and what stands out |
| **The argument itself** | Every sentence | Every sentence, in full and in order. Only the changes above are made; nothing is summarized away. |

<sub>Examples from François Chollet, <a href="https://arxiv.org/abs/1911.01547">“On the Measure of Intelligence”</a> (2019).</sub>

## Quickstart

```bash
git clone https://github.com/zekiworks/hilde.git && cd hilde
python3.12 -m venv .venv && source .venv/bin/activate
python -m pip install torch==2.10.0 torchaudio==2.10.0 \
  --index-url https://download.pytorch.org/whl/cu130
python -m pip install -r requirements.txt
python audiobook_tts_web.py --open --allow-model-downloads \
  --voice-clone-model Qwen/Qwen3-TTS-12Hz-1.7B-Base \
  --voice-design-model Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign
```

Without an NVIDIA GPU, replace `cu130` with `cpu` in the third command. The
page opens at `http://127.0.0.1:8800/`, reachable only from this computer, with
eight stock voices. Each model, about 4.3 GB, downloads the first time it is
used. Text adaptation needs a language model: connect a provider (OpenAI or
Anthropic) or add a local model server under **Advanced**, or clear **Adapt the
text for listening** before you create an audiobook; see
[Text adaptation](#text-adaptation).
[Installation](#installation) covers other platforms, local model folders, and
FlashAttention.

## About

An audiobook studio built on [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS). The Hilde web app turns PDF, Markdown, or text documents into narrated MP3 audiobooks with a synchronized reader. Underneath it, the `audiobook_tts.py` command-line tool designs a voice from a written description, saves a portable reference, and turns narration-ready text into one MP3 or WAV file. Run locally on CPU or CUDA, with optional FlashAttention 2 and batched narration.

Giving every text chunk the same voice description does **not** establish a shared speaker identity—even when those chunks are generated in one batch. Hilde therefore separates voice creation from narration: a **VoiceDesign** model generates a reference clip once and saves it with its exact transcript, and a **Base** model clones that saved reference for every chunk, across batches, books, and sessions. The speaker reference stays the same; pacing, expression, and sampled audio can still vary.

Hilde's own code is released under the [MIT License](LICENSE); some dependencies have other terms, listed under [License](#license). Speech generation is provided by Qwen3-TTS; GPU attention acceleration uses [FlashAttention](https://github.com/Dao-AILab/flash-attention). [ARCHITECTURE.md](ARCHITECTURE.md) describes how the code fits together and how to test a change.

- [Known limitations](#known-limitations)
- [Installation](#installation)
- [Configuration](#configuration)
- [Web UI](docs/web-ui.md)
- [Command line](docs/command-line.md)
- [License](#license)

## Known limitations

- **Adaptation is done by a language model.** It is told to keep every
  sentence of the author's prose, but it can occasionally drop, reword, or
  misdescribe something. The reader shows exactly the text that was narrated.
- **Figure descriptions are model-generated and can be wrong.** Check the
  original figure before relying on a number or a trend.
- **Text-only local models never see the figure.** They describe it from the
  text extracted from it (labels, numbers, caption), so a figure with few
  labels gets a thin description, and an equation printed as an image is left
  out. OpenAI and Anthropic models, and a local model marked as seeing images,
  receive figures as images.
- **Cloud providers receive your document.** A document goes to OpenAI or
  Anthropic when you choose one of their models, or, with **Model** left on
  Default, when you have added no local server. The text and figures of each
  adapted document are then sent to that provider. Use a local model server for
  private documents.
- **Expression can still vary.** The saved reference keeps the speaker the
  same, but pacing and energy are sampled per chunk and can shift between
  sentences.
- **SSH workers are not checked for free GPU memory.** One that runs out of
  memory fails the run; local GPUs wait for room instead.
- **No authentication.** The web server is meant for your own machine or a
  trusted network; see [Access and security](#access-and-security).
- **Tested stack.** The GPU workflow is tested on Linux with Python 3.12 and
  PyTorch 2.10 + CUDA 13.0; CPU inference has also been exercised. Other
  platforms may need adjustments.
- **Inputs.** PDF, Markdown, and plain text up to 64 MiB. HTML pages and EPUB
  files are not supported.

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

## Configuration

`audiobook_tts_web.py` serves **Hilde**, a browser app over a shared
server-side library. Its configuration comes from command-line options when the
server starts; browsers cannot replace that process-wide configuration.
Settings each browser may change, such as precision, chunking, batch size, and
the text-adaptation model, are under **Advanced** in the page; see
[Advanced](docs/web-ui.md#advanced).

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
| `--host` | `127.0.0.1`, this machine only; pass `0.0.0.0` to reach it from other devices. See [Access and security](#access-and-security). |
| `--port` | `8800`. |
| `--open` | Opens the page in a browser. |
| `--verbose` | Logs every request to stderr. |
| `--storage-root` | `User/` in the project folder; see [Storage](#storage). |
| `--voice-design-model` | VoiceDesign model directory, or a Hugging Face ID with `--allow-model-downloads`. |
| `--voice-design-server`, `--voice-design-server-model` | A speech server for voice design instead of the model; the server model defaults to `gpt-4o-mini-tts`. See [Speech models](#speech-models). |
| `--voice-clone-model` | Base model directory, or a Hugging Face ID with `--allow-model-downloads`. |
| `--voice-clone-server`, `--voice-clone-server-model` | A speech server for narration instead of the model; the server model defaults to `tts-1`. |
| `--allow-model-downloads` | Disabled; permits Hugging Face model IDs and downloads. |
| `--narration-ssh-worker`, `--narration-ssh-python`, `--narration-ssh-model`, `--narration-ssh-device` | Passwordless SSH narration workers; see [SSH narration workers](docs/ssh-workers.md). |
| `--render-voice-previews` | Renders comparable previews for older voices, then exits. See [Voices](docs/web-ui.md#voices). |

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
example to keep narration off GPU 0 even when it has room:

```bash
CUDA_VISIBLE_DEVICES=1,2,3 ./example_run.sh
```

Remote membership comes only from the repeated `--narration-ssh-worker` startup
flags; see [SSH narration workers](docs/ssh-workers.md).

The server probes PyTorch-visible CUDA and MPS devices in a short-lived child.
It counts CUDA devices after initialization, so a GPU the runtime cannot open,
such as one waiting for a reset, is skipped and the remaining GPUs stay in the
pool instead of the server falling back to CPU. The pool is fixed at startup:
restart the server after changing `CUDA_VISIBLE_DEVICES` or the SSH workers,
or after GPUs are added, removed, or recovered. Voice design runs alone, on the
GPU with the most free memory at that moment, so a GPU another program has
filled is passed over; `nvidia-smi` measures this without touching any GPU.
Without CUDA it uses MPS, then CPU.
Unsupported precision or `flash_attention_2` combinations still fail through
CLI validation.

A book with several workers starts only on GPUs with about 6 GiB free: the
4 GiB model plus room for a batch. The others are checked every minute and join
once another program frees memory. A GPU that runs out of memory mid-book hands
its chunks to the other workers and waits the same way, so the book slows down
instead of failing. If every GPU is full, the book waits until one has room;
**Stop** ends it. The log names the waiting GPUs; their chips still show
`running`, because the book holds them.

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
memory for narration. A single chunk that still does not fit fails a
single-device run; with several workers, that GPU hands its chunks to the others
and waits (see [Multiple GPUs](docs/command-line.md#multiple-gpus)).
With `--resume-dir`, rerunning the same command reuses the completed chunks.

Changing batch sizes can change sampled audio even with the same `--seed`. A saved reference establishes the shared speaker reference, not bit-for-bit reproducibility across hardware, batching decisions, or library versions.

### Text adaptation

**Adapt the text for listening**, in the **Create audiobook** step, rewrites a
document for someone listening, who cannot skim or glance back. The author's
prose stays word for word. Whatever would be a chore to hear is left out:
tables of contents, lists of sections, section numbers, page numbers, citation
marks, and the bibliography. Whatever the ear cannot hold is tuned down to its
point: tables, formulas, long lists, and runs of numbers. A table of contents
or bibliography under its own heading is removed before the model sees the
text, even without adaptation. Inline attributions and appendices remain.
While adaptation is selected, its settings appear under **Advanced** in
**Text adaptation**:

- **Model** picks the model that rewrites the text. Left on Default, a job uses
  your local server's first model once you add one under **Add local**; if that
  server does not answer, the job stops rather than send your document to a
  cloud provider. Without a local server, Default is OpenAI's first model once
  signed in, then Anthropic's. A cloud provider that is busy or has a passing
  error is asked again, up to four times.
- **Providers** connects cloud models. **OpenAI** signs this server in with a
  ChatGPT account: open the sign-in page it shows and enter the code; the
  sign-in renews itself. **Anthropic** takes an API key from the
  [Claude Console](https://platform.claude.com/), since Anthropic allows
  Claude subscriptions only in its own apps; usage is billed to that key. The
  server keeps both in `~/.hilde/`, readable only by the user running it.
  **Remove** deletes the Anthropic key; delete `~/.hilde/openai.json` to sign
  out of OpenAI.
- **Add local** connects a model server on your network. Choose its type,
  **Ollama** or **OpenAI-compatible** (SGLang, vLLM, LM Studio), enter its
  `host:port`, for example `127.0.0.1:8010`, then choose one of its models.
  Tick **This model sees images** when the model accepts images, as Gemma 4
  does; figures and equations then go to it as images.
- **Workers** (default 4, at most 32) is how many paragraph batches are adapted
  at once, and **Paragraphs per worker** (default 1, at most 32) is how many
  paragraphs each batch holds. A figure or table is never split: its image,
  the labels read from inside it, and its caption always go to the model
  together, so it is described once.

Figures reach OpenAI and Anthropic models as images, and a local model too once
**This model sees images** is ticked. Otherwise a local model receives the text
extracted from each figure instead, and equations printed as images, which
carry no text, are left out.

Each description of a figure, a table, or an equation opens with a spoken cue
such as "Figure 2 shows…" or "The equation says…", so a listener hears where
the author's text stops. The job log names any description that doesn't.

In a PDF, a sentence that a page break splits is joined back together before
the model sees it, even when a footnote or a figure sat between its halves;
these then follow the sentence. The job log counts the sentences it rejoined.

The model follows the instructions in `prompts/PAPER-AUDIO-BOOK.md`; each job
reads them when it starts, so edits apply to the next job, and a job adapted
under other instructions starts its adaptation over.

When text extraction leaves reference entries outside a standalone References
section, the model leaves them out one by one. The log shows each as `Paragraph
149/174 has nothing to read aloud: …` with the model's reason, and nothing is
narrated for it. A figure the model leaves out still shows in the reader, after
the text before it.

### Access and security

The web server has no built-in authentication or authorization, and by default
it listens only on this machine. **Do not expose it directly to the public
Internet.** Anyone who can reach it can read or replace shared assets, submit
or cancel jobs, and stop running work. `--host 0.0.0.0` lets in every device
that can reach the port, so use it only on a trusted network. A public
deployment needs an authenticated TLS reverse proxy plus request/rate limits;
with the proxy on the same machine, keep the default host. Cross-origin POST
rejection is CSRF hardening, not access control.

## Web UI

**Create** turns a document into an audiobook, **Listen** plays it with its
synchronized text, and **Voices** designs narrators: see [Web UI](docs/web-ui.md).

## Command line

`audiobook_tts.py` designs voices and narrates text on its own, on one or more
GPUs, the CPU, or a speech server: see [Command line](docs/command-line.md).

## License

Hilde's own code is released under the [MIT License](LICENSE). Its
dependencies keep their own terms:

- PDF extraction uses [PyMuPDF](https://github.com/pymupdf/PyMuPDF) and
  [pymupdf4llm](https://github.com/pymupdf/pymupdf4llm), which are
  dual-licensed under the GNU AGPL v3.0 or a commercial license from Artifex.
  `pip install -r requirements.txt` installs them under the AGPL. If you
  distribute Hilde or run it as a service for other people, review the AGPL's
  terms.
- Speech is generated by [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
  models; follow their model licenses.
- Word timing in the reader uses TorchAudio's
  [MMS_FA](https://docs.pytorch.org/audio/stable/generated/torchaudio.pipelines.MMS_FA.html)
  alignment model, downloaded on first use. Its weights are published under
  [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/), which
  permits only non-commercial use.
- Use texts and voices you have the rights to use.
