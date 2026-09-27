# Command-line options

Every flag of `audiobook_tts.py`, the tool described under
[Command line](../README.md#command-line). Each command prints its own list too:

```bash
python audiobook_tts.py --help
python audiobook_tts.py create-voice --help
python audiobook_tts.py narrate --help
```

## Shared options

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

## Voice creation options

| Flag | Default / requirement |
| --- | --- |
| `--model-path` | Required VoiceDesign model directory or permitted Hub ID. |
| `--voice-dir` | Required destination directory; an existing directory requires `--overwrite`. |
| `--overwrite` | Disabled; replace the existing `reference.wav`, `transcript.txt`, and `description.txt` after the new files are staged, removing a `preview.wav` rendered from the old voice. |
| `--instruct` | Required nonempty voice description. |
| `--wav-subtype` | `FLOAT`; choices: `PCM_16`, `PCM_24`, `PCM_32`, `FLOAT`, `DOUBLE`. |

## Narration options

| Flag | Default / requirement |
| --- | --- |
| `--clone-model-path` | Required Base model directory or permitted Hub ID, unless `--server` is used. |
| `--voice-dir` | Required existing saved voice directory, unless `--server` is used. |
| `--output` | Required `.mp3` or `.wav` destination. |
| `--chunk-max-chars` | `500`; must be positive. |
| `--batch-size` | `0` for all chunks on one device and one chunk per distributed worker; positive values set the chunks per clone call. A batch that runs out of CUDA memory is retried one chunk at a time. |
| `--worker-device` | Unset; repeat to add local devices to one resumable narration. A GPU with less than 6 GiB free joins once it has room. |
| `--ssh-worker` | Unset; repeat passwordless `HOST` or `USER@HOST` targets. See [SSH narration workers](ssh-workers.md). |
| `--ssh-python` | `python3`; Python executable shared by SSH workers. |
| `--ssh-model-path` | Uses `--clone-model-path`; remote model path or permitted Hub ID. |
| `--ssh-device` | `cuda:0`; device used on every SSH worker. |
| `--wav-subtype` | `PCM_16` for WAV; choices as above. |
| `--mp3-compression-level` | Encoder default; optional value from `0` to `1` for MP3 only. |
| `--overwrite` | Disabled; allows replacing an audiobook, never its saved reference or input file. |
| `--resume-dir` | Unset; durable narration chunk directory used to resume matching work; required with local or SSH worker additions. |

## Speech-server options (either command)

| Flag | Default / requirement |
| --- | --- |
| `--server` | Unset; `IP:PORT`, `host:port`, or a URL. A bare authority implies `/v1`. Replaces local inference. |
| `--server-model` | `tts-1`; the model name sent to the server. `create-voice` refuses `tts-1`/`tts-1-hd`, which ignore `instructions`. |
| `--server-voice` | Required with `--server`; a voice the server already holds. Voice design shapes it with `--instruct`. |
| `--server-timeout` | `300`; seconds allowed for one chunk's response. |
| `--api-key` | Unset; falls back to `OPENAI_API_KEY`, then sends no Authorization header. |
