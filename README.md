
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
  leaves out cross-references, citation marks, and email addresses, and turns
  formulas, notation, and tables into plain words; headings keep the paper's
  own numbers. See [How Hilde reads a paper](#how-hilde-reads-a-paper).
- **One narrator, start to finish.** A voice is designed once from a written
  description, then cloned for every chunk of every book, across sessions.
  Eight stock voices are included.
- **One text, any voice.** A book is written once from its document. Another
  voice reads the same text with no model, and the same document added again
  under another name opens the book you already have.
- **A reader that follows along.** The current word lights up as it is spoken;
  click any word to jump there. **Original** shows the author's text and its
  PDF page beneath each adapted passage, and model-written figure
  descriptions are labelled.
- **Every GPU in the house.** Narration spreads across all local GPUs and, over
  SSH, other machines. A busy local GPU joins once it has room, and one that
  runs out of memory hands its chunks to the others.
- **Local first.** Speech is generated on your own hardware by default. Text
  adaptation runs on a local model server, or on OpenAI or Claude, through
  your ChatGPT or Claude subscription or an Anthropic API key.
- **Resumable.** Press **Stop** or restart the server: finished chunks are kept,
  and creating the same audiobook again picks up where it left off.

## How Hilde reads a paper

Plain text-to-speech reads a paper exactly as printed. Hilde first rewrites it
for someone listening, who cannot skim, glance back, or see the page.

| On the page | Read as printed | Read by Hilde |
| --- | --- | --- |
| **Table of contents** | "Contents. Part one. Context and history. Page 3. Section I.1. Need for an actionable definition… Page 3. Section I.2…" | *Skipped. The narration goes straight to the first chapter.* |
| **Cross-references**<br>`We noted in II.1.1 that…` | "We noted in Section Two point One point One that…" | "We noted earlier that…" |
| **Formulas** | "I superscript theta sub T, sub I S comma scope, equals the average over tasks T in the scope of omega sub T times theta sub T…" | "Intelligence across a given scope is the average, over the tasks in that scope, of the system's skill-acquisition efficiency…" |
| **Notation**<br>`we will denote θ^max_T,IS as Θ` | "…we will denote theta superscript max, sub T comma I S, as capital theta." | *Skipped. Every quantity is named in words.* |
| **Citation marks**<br>`…Loebner Prize [75]` | "…Loebner Prize seventy-five…" | "…the Total Turing Test and Loebner Prize…" |
| **Contact details**<br>`author@example.com` | "author at example dot com" | *Skipped.* |
| **Figures** | The labels inside the image, jumbled: "G General intelligence Extreme generalization Broad Broad Broad…" | "The figure presents intelligence as a hierarchy. At the bottom, task-specific skills support only local generalization…" |
| **Tables and long lists** | Every cell and every item, in order | What they show and what stands out |
| **The argument itself** | Every sentence | Every sentence, in full and in order. Only the changes above are made; nothing is summarized away. |

<sub>Examples from François Chollet, <a href="https://arxiv.org/abs/1911.01547">“On the Measure of Intelligence”</a> (2019).</sub>

### Measured

Each audiobook's record keeps the model that adapted it, how much of the
author's prose came through, and how long each stage took. Two papers, adapted
by Gemma 4 31B (FP8, served by vLLM on one of the same GPUs) and narrated on
four RTX PRO 6000 GPUs:

| Paper | PDF pages | Audio | Made in | Prose paragraphs read word for word¹ |
| --- | ---: | ---: | ---: | ---: |
| Vaswani et al., “Attention Is All You Need” | 15 | 36 min | 6.5 min | 50 of 64 |
| Chollet, “On the Measure of Intelligence” | 64 | 2 h 45 min | 32 min | 266 of 328 |

<sub>¹ Keeping at least 95% of the author's words of four letters or more. The
paragraph that kept the least, 56%, defines `SkillProgramGen : ISState →
[SkillProgram, SPState]`, which Hilde reads as “maps the intelligent system's
state to a skill program and a new system state”; others lost a cross-reference
such as “as described in Section 3.2.2”. The adaptation instructions ask for
both.</sub>

## Quickstart

On Linux or macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/zekiworks/hilde/main/install.sh | sh
hilde --open
```

The installer needs only `git` and `curl`, and no sudo. It puts Hilde in
`~/hilde` with its own Python 3.12 (through [uv](https://docs.astral.sh/uv/),
which it installs if missing), picks the PyTorch build for your NVIDIA driver
or the CPU, and adds the `hilde` command to `~/.local/bin`. Run it again to
update; your library in `~/hilde/User` stays.

The page opens at `http://127.0.0.1:8800/`, reachable only from this computer,
with eight stock voices. Each speech model, about 4.3 GB, downloads the first
time it is used. Text adaptation needs a language model: connect a provider
(ChatGPT, your own Claude Code, or an Anthropic API key) or add a local model server under **Advanced**, or clear
**Adapt the text for listening** before you create an audiobook; see
[Text adaptation](docs/text-adaptation.md).
[Installation](docs/installation.md) covers the installer's settings, installing
by hand, Windows, local model folders, and FlashAttention.

## About

An audiobook studio built on [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS). The Hilde web app turns PDF, Markdown, or text documents into narrated MP3 audiobooks with a synchronized reader. Underneath it, the `audiobook_tts.py` command-line tool designs a voice from a written description, saves a portable reference, and turns narration-ready text into one MP3 or WAV file. Run locally on CPU or CUDA, with optional FlashAttention 2 and batched narration.

Giving every text chunk the same voice description does **not** establish a shared speaker identity—even when those chunks are generated in one batch. Hilde therefore separates voice creation from narration: a **VoiceDesign** model generates a reference clip once and saves it with its exact transcript, and a **Base** model clones that saved reference for every chunk, across batches, books, and sessions. The speaker reference stays the same; pacing, expression, and sampled audio can still vary.

Hilde's own code is released under the [MIT License](LICENSE); some dependencies have other terms, listed under [License](#license). Speech generation is provided by Qwen3-TTS; GPU attention acceleration uses [FlashAttention](https://github.com/Dao-AILab/flash-attention). [ARCHITECTURE.md](ARCHITECTURE.md) describes how the code fits together and how to test a change.

- [Known limitations](#known-limitations)
- [Installation](docs/installation.md)
- [Configuration](docs/configuration.md)
- [Text adaptation](docs/text-adaptation.md)
- [Access and security](#access-and-security)
- [Web UI](docs/web-ui.md)
- [Command line](docs/command-line.md)
- [License](#license)

## Known limitations

- **Adaptation is done by a language model.** It is told to keep every
  sentence of the author's prose, but it can occasionally drop, reword, or
  misdescribe something. The reader shows exactly the text that was narrated,
  with the author's text beneath it under **Original**,
  and the job log names every prose paragraph that lost more than a fifth of
  its words; a paragraph the model left out whole is named with its reason
  instead. That count measures dropped wording, not meaning: a sentence
  reworded with the same words, or an added claim, goes unnoticed.
- **Figure descriptions are model-generated and can be wrong.** Check the
  original figure before relying on a number or a trend. A book's text is
  written once: a new voice reads it as it is, and only **Recreate with the
  latest Hilde** writes it again.
- **Text-only local models never see the figure.** They describe it from the
  text extracted from it (labels, numbers, caption), so a figure with few
  labels gets a thin description, and an equation printed as an image is left
  out. OpenAI and Claude models, and a local model marked as seeing images,
  receive figures as images.
- **Cloud providers receive your document.** A document goes to OpenAI or
  Anthropic (directly, or through Claude Code) when you choose one of their models, or, with **Model** left on
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

[Quickstart](#quickstart) installs the tested stack: Python 3.12 with PyTorch
2.10 for CUDA 13.0, or its CPU build. [Installation](docs/installation.md)
covers other platforms, MP3 support, FlashAttention 2, and
downloading the models ahead of time.

## Configuration

The server takes its configuration from command-line options when it starts;
each browser changes only its own settings, under **Advanced**.
[Configuration](docs/configuration.md) covers:

- [starting the server](docs/configuration.md#start-the-server) and every
  option;
- [storage](docs/configuration.md#storage): the library lives in `User/` in the
  project folder, which git ignores, so deleting the folder deletes the library;
- [speech models](docs/configuration.md#speech-models), local or on a speech
  server;
- [narration workers](docs/configuration.md#narration-workers): every GPU the
  server sees, plus [SSH workers](docs/ssh-workers.md);
- [batch size](docs/configuration.md#batch-size) and GPU memory.

[Text adaptation](docs/text-adaptation.md) covers the models that rewrite a
document for listening: a local model server, ChatGPT, Claude Code, or the Anthropic API.

## Access and security

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
