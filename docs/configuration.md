# Configuration

`audiobook_tts_web.py` serves **Hilde**, a browser app over a shared
server-side library. Its configuration comes from command-line options when the
server starts; browsers cannot replace that process-wide configuration.
Settings each browser may change, such as precision, chunking, batch size, and
the text-adaptation model, are under **Advanced** in the page; see
[Advanced](web-ui.md#advanced).

## Start the server

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
| `--host` | `127.0.0.1`, this machine only; pass `0.0.0.0` to reach it from other devices. See [Access and security](../README.md#access-and-security). |
| `--port` | `8800`. |
| `--open` | Opens the page in a browser. |
| `--verbose` | Logs every request to stderr. |
| `--storage-root` | `User/` in the project folder; see [Storage](#storage). |
| `--voice-design-model` | VoiceDesign model directory, or a Hugging Face ID with `--allow-model-downloads`. |
| `--voice-design-server`, `--voice-design-server-model` | A speech server for voice design instead of the model; the server model defaults to `gpt-4o-mini-tts`. See [Speech models](#speech-models). |
| `--voice-clone-model` | Base model directory, or a Hugging Face ID with `--allow-model-downloads`. |
| `--voice-clone-server`, `--voice-clone-server-model` | A speech server for narration instead of the model; the server model defaults to `tts-1`. |
| `--allow-model-downloads` | Disabled; permits Hugging Face model IDs and downloads. |
| `--narration-ssh-worker`, `--narration-ssh-python`, `--narration-ssh-model`, `--narration-ssh-device` | Passwordless SSH narration workers; see [SSH narration workers](ssh-workers.md). |
| `--render-voice-previews` | Renders comparable previews for older voices, then exits. See [Voices](web-ui.md#voices). |

## Storage

`--storage-root` defaults to `User/` in the project folder, which git ignores.
The server creates and owns this fixed layout:

```text
User/
├── Voices/
├── Audiobooks/
│   └── attention-is-all-you-need--bdfaa68d8984/
│       ├── book.json
│       ├── source.pdf
│       ├── narration.json
│       ├── reader.md
│       └── voices/
│           └── Eir/
│               ├── audio.mp3
│               └── timings.json
├── Documents/
└── in_progress/
```

- **Voices** contains one directory per narrator: `reference.wav`, the exact
  UTF-8 `transcript.txt` used to create it, its `description.txt`, and, for
  voices made before fixed previews, a rendered `preview.wav`.
- **Documents** contains uploaded files and URL downloads. URL downloads use an
  optional filename or infer one from the response.
- **Audiobooks** holds one folder per book, named for its title and the first
  12 hexadecimal digits of the SHA-256 of its document's bytes, so the same
  document under another file name is the same book. A book is made from its
  document once:
  - `book.json` records the document's full SHA-256 and the file names it came
    under, the Hilde version and git commit, the prompt, extraction schema, and
    model that wrote its text, how much of the author's prose the text kept,
    and each voice with when it was made, its length, and whether it reads the
    book's current text (`ready`) or an earlier one (`stale`);
  - `source.pdf` (or `.md`, `.txt`) is a copy of the document;
  - `narration.json` is the text read aloud, as passages typed body, heading,
    figure, table, or equation, each with its PDF page, the author's original
    text, and the author's paragraphs it came from, typed body, heading,
    figure, table, equation, footnote, or caption;
  - `reader.md` is the follow-along view with its pictures embedded;
  - `voices/<voice>/` holds that voice's `audio.mp3` and its own word and
    sentence `timings.json`.

  Each book or voice is written into a hidden folder and renamed into place
  when it is complete, so a crash never leaves a half-made one; the next start
  removes what was left unfinished.
- **in_progress** contains durable source snapshots, extraction checkpoints,
  and narration chunks for unfinished jobs. It is removed for a job only after
  its book is published.

On its first start, this version moves audiobooks kept in the earlier layout
(`Audiobooks/<name>.mp3` with hidden `.readers` and `.versions` folders) into
book folders without calling any model, keeping each MP3 as it is. A book gets
its text back from the `Documents/<name>-narration.txt` file its job stored,
when that matches its reader; without it, the book plays as before but needs
**Recreate** before it can take another voice. The earlier files wait in
`Audiobooks/.backup/` until the book has played once.

Existing files are not migrated automatically when the storage root changes.
Move them into the appropriate directory yourself. The web app's **Delete**
buttons remove voices, documents, and audiobooks with every voice.

A new library starts with the stock voices from the repository's `voices/`
folder. They are copied only when the library is created, so a stock voice you
delete stays deleted.

Because `User/` lives in the project folder, deleting or re-cloning the folder,
or running `git clean -x`, deletes your library as well. Back it up, or pass
`--storage-root` to keep the library somewhere else.

## Speech models

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

## Narration workers

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
flags; see [SSH narration workers](ssh-workers.md).

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

## Batch size

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
and waits (see [Multiple GPUs](command-line.md#multiple-gpus)).
With `--resume-dir`, rerunning the same command reuses the completed chunks.

Changing batch sizes can change sampled audio even with the same `--seed`. A saved reference establishes the shared speaker reference, not bit-for-bit reproducibility across hardware, batching decisions, or library versions.
