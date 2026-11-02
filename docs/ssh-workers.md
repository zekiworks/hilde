# Narration workers on other machines

Machines reached over passwordless SSH can narrate chunks of the same
audiobook, for the web server and for `audiobook_tts.py narrate` alike. Each of
their GPUs loads its own Base model and pulls chunk batches like a
[local GPU worker](command-line.md#multiple-gpus); no shared storage is required.

## Requirements

The server's machine must reach the other machine with
`ssh -o BatchMode=yes TARGET`: a key, not a password, and a host key it has
accepted. In the web server, **Add narration workers** does both from a
browser (below), so nobody needs a terminal on the server: turn on the other
machine's SSH server (on a Mac, System Settings › General › Sharing › Remote
Login), paste the line the dialog shows into a terminal there, and trust its
fingerprint when Hilde asks. For the command line, `ssh-copy-id TARGET` and one
`ssh TARGET` in a terminal on the server's machine do the same. The other
machine also needs `git` and `curl`.

Narrating there takes a Python environment with PyTorch and Qwen TTS and the
Base model. The web server's **Set up** installs both (below); for the command
line, run Hilde's installer there, which makes `~/hilde/.venv`, and download
the model.

The coordinator uses `scp` to stage `audiobook_tts.py` plus `reference.wav` and
`transcript.txt`, sends narration chunks over the SSH process, receives WAV
results, and removes its `/tmp/audiobook-tts-*` workspace. Every SSH worker is
therefore trusted with the saved voice and the narration text. The model itself
is never copied.

SSH workers are not measured for free GPU memory, and one that runs out of
memory while narrating fails the run. Leave a GPU unticked when another
program fills it. A worker that cannot be staged, fails while loading the
model (out of memory included), or is not ready within
`--worker-ready-timeout` is left out of that book with a warning, and the
other workers narrate it; see
[Narration workers](configuration.md#narration-workers).

## Web server

Open **Settings** in a browser on the server's own machine, at
`http://127.0.0.1:8800/` (or on a device it approved, or on any device when Hilde
started with `--manage-from-network`). **Other narration machines**, under **Machines and devices**,
lists the nodes, and **Add narration workers** adds one:

1. The first time, open **First time: let this server in**: it shows a line
   with this server's public SSH key (the first identity `ssh` offers) to paste
   into a terminal on the other computer, with **Copy**; it adds the key to
   that account's `~/.ssh/authorized_keys`. Then enter the machine as an IP
   address, a host name, or `user@host`, and press **Connect**. Hilde connects
   over SSH, or says why it could not: it asks for a password (the key is not
   in yet), it does not answer, or its key changed since this server last
   connected (Hilde then trusts nothing; check with whoever runs it). A
   machine this server has never connected to shows its key fingerprints
   (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` there shows the ED25519
   one to compare) and **Trust … and connect**, which adds its host key to this
   server's `~/.ssh/known_hosts`, as answering yes to `ssh` would, but only if
   the key is still the one shown.
2. Hilde shows the Python it found with PyTorch and Qwen TTS, the Base model it
   found, and every GPU with its free memory, all ticked. It looks for Python in
   `~/hilde/.venv`, `python3`, and pyenv and conda environments, and for the
   model at the server's own model path, in the Hugging Face cache, and in a
   folder of the same name up to four levels inside the home folder. Edit
   **Python** or **Model** and press **Connect** again to use others.

   When it finds no such Python or no model, **Set up** installs them in that
   account, without sudo: it runs Hilde's installer there over SSH, which makes
   `~/hilde` and its `.venv` with PyTorch and Qwen TTS, and downloads the
   server's Base model into `~/hilde/models`. It skips whichever is already
   there. The download is several gigabytes, so it takes a while; the dialog
   shows each step and the latest output, and **Stop setup** ends it. When it is
   done, the dialog shows what it found, as after **Connect**.
3. Untick any GPU the server should leave alone. Tick **Everyone's books may
   use it too** for a machine every browser's **Any GPU that is on** should use;
   leave it unticked for your own computer: it then narrates only the books of
   a browser that chooses one of its devices under **Settings › Machines and
   devices › Narrate my books on**. Then press **Add node**.

The node narrates from the next book on, without a restart; a book already
being narrated keeps its workers. Adding a machine again replaces its settings
and GPUs. **Remove** takes a node away, except while it narrates a book.

The server's own devices narrate too. Under **Machines and devices** ›
**This server** in **Settings**, each narration worker has a box; untick one to keep it out of narration
from the next book on, for example to time a book on one Spark alone. It then
reads `off, excluded in settings`. A GPU narrating a book cannot be unticked until the book is
done, and one worker always stays: the last local device cannot be unticked
without a node, nor the last node removed while every local device is off.

Hilde keeps the nodes, and the local devices that are off, in `workers.yaml`
in the library folder (`--storage-root`):

```yaml
local_off: [GPU-12345678-abcd-ef01-2345-6789abcdef01]
nodes:
- host: user@gpu-box
  python: /home/user/hilde/.venv/bin/python
  model: /home/user/models/Qwen3-TTS-12Hz-1.7B-Base
  devices: [cuda:1, cuda:2, cuda:3]
- host: ayse@ayse-mac
  python: /Users/ayse/hilde/.venv/bin/python
  model: /Users/ayse/hilde/models/Qwen3-TTS-12Hz-1.7B-Base
  devices: [mps]
  shared: false
```

`devices` take `cuda:N`, `mps`, or `cpu`. `shared: false` keeps a node out of
everyone's **Any GPU that is on**: it narrates only books whose browser chose
one of its devices; without it a node is shared. `local_off` names this machine's GPUs
by UUID, as `nvidia-smi -L` shows them, because a GPU that fails renumbers the
ones after it; it also takes `mps` or `cpu`. A GPU listed there stays off even
while it is missing. A file from an earlier Hilde that lists `cuda:N` is
rewritten once, at the next start, with the UUID of the GPU numbered N then.
To edit the file by hand, stop the server first; it reads
the file when it starts and refuses to start if a node is invalid or no worker
would be left.

The server has no sign-in, so only a browser on its own machine, or a device
it approved, sees the hosts and paths or changes the nodes. A browser on
another device sees each node as `Machine 1` and so on, its GPUs as
`Machine 1 · GPU 2`, and the server refuses its requests to change them.
`--manage-from-network` lifts this for every browser that reaches the
server; anyone there can then add SSH machines that run programs as this
user (see [Manage from other devices](configuration.md#manage-from-other-devices)).

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
commas. A target may appear once per device. With `--no-local-worker`, only the
SSH workers narrate; `--device` starts none. The defaults are in
[Narration options](command-line-options.md#narration-options).
