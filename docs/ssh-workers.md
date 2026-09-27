# SSH narration workers

Machines reached over passwordless SSH can narrate chunks of the same
audiobook, for the web server and for `audiobook_tts.py narrate` alike. Each
worker loads its own Base model and pulls chunk batches like a
[local GPU worker](../README.md#multiple-gpus); no shared storage is required.

## Requirements

Each SSH target must already accept `ssh -o BatchMode=yes TARGET` and have a
compatible Python environment, PyTorch/Qwen TTS stack, and Base model. The
coordinator uses `scp` to stage `audiobook_tts.py` plus `reference.wav` and
`transcript.txt`, sends narration chunks over the SSH process, receives WAV
results, and removes its `/tmp/audiobook-tts-*` workspace. Consequently, every
SSH worker is trusted with the saved voice and narration text. The model itself
is never copied.

SSH workers are not measured for free GPU memory, and one that runs out of
memory fails the run.

## Web server

Remote membership in the server's
[narration workers](../README.md#narration-workers) comes only from the
repeated `--narration-ssh-worker` startup flags. To add homogeneous
passwordless SSH narration workers to the web pool:

```bash
python audiobook_tts_web.py \
  --voice-clone-model /models/Qwen3-TTS-12Hz-1.7B-Base \
  --narration-ssh-worker user@spark-one \
  --narration-ssh-worker user@spark-two \
  --narration-ssh-python /opt/qwen/bin/python \
  --narration-ssh-model /models/Qwen3-TTS-12Hz-1.7B-Base
```

| Option | Default / meaning |
| --- | --- |
| `--narration-ssh-worker` | Unset; repeat to add passwordless SSH narration workers. |
| `--narration-ssh-python` | `python3`; the Python executable on every SSH worker. |
| `--narration-ssh-model` | The `--voice-clone-model` value; the Base model path or ID on every SSH worker. |
| `--narration-ssh-device` | `cuda:0`; the PyTorch device on every SSH worker. |

All configured SSH workers currently share those Python, model, and device
settings. The pool is fixed at startup, so restart the server after changing
them.

## Command line

`narrate` uses the same protocol. Like local workers, SSH workers need
`--resume-dir`:

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

`--ssh-python`, `--ssh-model-path`, and `--ssh-device` apply to every target;
their defaults are in [Narration options](command-line-options.md#narration-options).
