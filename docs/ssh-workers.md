# Narration workers on other machines

Machines reached over passwordless SSH can narrate chunks of the same
audiobook, for the web server and for `audiobook_tts.py narrate` alike. Each of
their GPUs loads its own Base model and pulls chunk batches like a
[local GPU worker](command-line.md#multiple-gpus); no shared storage is required.

## Requirements

The server's machine must already reach the other machine with
`ssh -o BatchMode=yes TARGET`: a key, not a password, and a host key it has
accepted before. In a terminal on the server's machine, `ssh-copy-id TARGET`
installs the key, and one `ssh TARGET` accepts the host key. The other machine
needs a Python environment with PyTorch and Qwen TTS (Hilde's installer makes
one in `~/hilde/.venv`) and the Base model.

The coordinator uses `scp` to stage `audiobook_tts.py` plus `reference.wav` and
`transcript.txt`, sends narration chunks over the SSH process, receives WAV
results, and removes its `/tmp/audiobook-tts-*` workspace. Every SSH worker is
therefore trusted with the saved voice and the narration text. The model itself
is never copied.

SSH workers are not measured for free GPU memory, and one that runs out of
memory fails the run. Leave a GPU unticked when another program fills it.

## Web server

Open **Advanced** in a browser on the server's own machine, at
`http://127.0.0.1:8800/`. Under **This server**, **Other machines** lists the
nodes, and **Add workers** adds one:

1. Enter the machine as an IP address, a host name, or `user@host`, and press
   **Connect**. Hilde connects over SSH, or says why it could not: SSH does not
   know the machine yet, it asks for a password, or it does not answer.
2. Hilde shows the Python it found with PyTorch and Qwen TTS, the Base model it
   found, and every GPU with its free memory, all ticked. It looks for Python in
   `~/hilde/.venv`, `python3`, and pyenv and conda environments, and for the
   model at the server's own model path, in the Hugging Face cache, and in a
   folder of the same name up to four levels inside the home folder. Edit
   **Python** or **Model** and press **Connect** again to use others.
3. Untick any GPU the server should leave alone, then press **Add node**.

The node narrates from the next book on, without a restart; a book already
being narrated keeps its workers. Adding a machine again replaces its settings
and GPUs. **Remove** takes a node away, except while it narrates a book.

Hilde keeps the nodes in `workers.yaml` in the library folder (`--storage-root`):

```yaml
nodes:
- host: user@gpu-box
  python: /home/user/hilde/.venv/bin/python
  model: /home/user/models/Qwen3-TTS-12Hz-1.7B-Base
  devices: [cuda:1, cuda:2, cuda:3]
```

`devices` takes `cuda:N`, `mps`, or `cpu`. To edit the file by hand, stop the
server first; it reads the file when it starts and refuses to start if a node
is invalid.

The server has no sign-in, so only a browser on its own machine sees the hosts
and paths or changes the nodes. A browser on another device sees the node's
GPUs as chips such as `Node 1 · GPU 2`, and the server refuses its requests to
change them.

## Command line

`narrate` uses the same protocol. Like local workers, SSH workers need
`--resume-dir`. Each `--ssh-worker` is one device of one machine; settings after
the target apply to that worker, and the rest come from `--ssh-device`,
`--ssh-python`, and `--ssh-model-path`:

```bash
python audiobook_tts.py narrate \
  --clone-model-path /models/Qwen3-TTS-12Hz-1.7B-Base \
  --voice-dir voices/Freyja \
  --input book.txt \
  --output book.mp3 \
  --resume-dir work/book-chunks \
  --device cuda:0 \
  --ssh-worker user@spark-one \
  --ssh-worker user@spark-two,device=cuda:1,python=/opt/qwen/bin/python \
  --ssh-worker user@spark-two,device=cuda:2,python=/opt/qwen/bin/python \
  --ssh-python /usr/bin/python3 \
  --ssh-model-path /models/Qwen3-TTS-12Hz-1.7B-Base \
  --dtype bfloat16 \
  --attn-implementation sdpa \
  --batch-size 2
```

Settings take `device=`, `python=`, and `model=`; their values cannot contain
commas. A target may appear once per device. The defaults are in
[Narration options](command-line-options.md#narration-options).
