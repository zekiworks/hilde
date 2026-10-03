# Command line

`audiobook_tts.py` works on its own, and the web server runs it for every voice
and audiobook. It has two commands:

1. **`create-voice`** uses a **VoiceDesign** model to generate a reference clip and save its exact transcript.
2. **`narrate`** uses a **Base** model to clone that saved reference for every chunk, across batches, books, and process sessions.

Only the Base model is needed after you have created a voice. The examples use
the model directories from [Installation](installation.md#5-download-the-models) and Freyja,
one of the stock voices in the repository's `voices/` folder.

## Create a voice

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
- Parent directories are created as needed. Without `--overwrite`, an existing voice directory is rejected. With `--overwrite`, the existing voice's `reference.wav`, `transcript.txt`, and `description.txt` are first copied into `.versions/vN` inside the voice directory (`v1`, then `v2`, and so on), then the newly staged files replace them after generation succeeds, and a `preview.wav` rendered from the old voice is removed; other files in the directory are left alone.
- A voice is a WAV and a UTF-8 transcript, not serialized model tensors. Copy the files together when moving a voice to another machine. Do not change the transcript independently of its audio.
- Narration needs both `reference.wav` and a nonempty UTF-8 `transcript.txt`. The WAV must contain nonempty, finite, non-silent mono audio. Do not point `--voice-dir` at a model directory.

## Narrate an audiobook

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

For another audiobook, change `--input` and `--output`, but keep the same `--voice-dir`. Narration loads only the Base model and reconstructs one clone prompt from the saved voice. The reference clip itself is not appended to the book. See [Batch size](configuration.md#batch-size) for choosing `--batch-size`.

## Multiple GPUs

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

A local GPU starts its worker only when `nvidia-smi` reports at least 6 GiB
free: the 4 GiB model plus room for a batch. A fuller GPU waits and is checked
again every minute, so it joins the book once another program frees memory:

```text
[Local cuda:0] 3.7 GiB free; waiting for 6 GiB, checked every minute
```

A local worker that runs out of GPU memory mid-book hands its chunks back to the
queue and waits the same way, so the book slows down instead of failing. If no
worker can run, the book waits; Ctrl+C stops it, and completed chunks stay in
`--resume-dir`. `nvidia-smi` opens no CUDA context, so checking never takes
memory from a full GPU. Without `nvidia-smi`, every GPU starts at once.

Other machines can take part over passwordless SSH; see
[SSH narration workers](ssh-workers.md).

## CPU only

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

## Through an OpenAI-compatible speech server

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

## Input and output

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

## Options

Every flag, with its default and requirements, is listed in
[Command-line options](command-line-options.md). `--help` on either command
prints them too.
