# Installation

## One command

On Linux or macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/zekiworks/hilde/main/install.sh | sh
```

`install.sh` needs `git` and `curl`, and no sudo. It:

1. installs [uv](https://docs.astral.sh/uv/) into `~/.local/bin` if it is
   missing; uv downloads Python 3.12 when the system has none;
2. clones Hilde into `~/hilde`, or pulls the latest version if it is there;
3. creates `~/hilde/.venv` and installs PyTorch 2.10: the CUDA 13.0 build for
   an NVIDIA driver 580 or newer, CUDA 12.6 for 525 or newer, otherwise the CPU
   build; on macOS, the standard build, which uses the GPU through MPS;
4. installs `requirements.txt` and checks that everything imports;
5. writes a `hilde` command into `~/.local/bin` that starts the web server with
   both Qwen3-TTS models from Hugging Face, downloaded on first use. Arguments
   pass through, and a repeated option replaces the built-in one, so
   `hilde --voice-clone-model /path/to/Base` uses a local folder.

Running it again updates Hilde and its dependencies. Your library in
`~/hilde/User` is never touched; an update stops if you changed tracked files
in `~/hilde`. Optional settings:

| Variable | Default / meaning |
| --- | --- |
| `HILDE_HOME` | `~/hilde`: where Hilde and its environment live. |
| `HILDE_BIN_DIR` | `~/.local/bin`: where the `hilde` command goes. |
| `HILDE_TORCH` | Chosen from the driver: `cu130`, `cu128`, `cu126`, or `cpu` forces a PyTorch build (Linux). |
| `HILDE_REPO` | This repository: the Git URL or path to clone. |

For example, `curl -fsSL …/install.sh | HILDE_TORCH=cpu sh`. MP3 writing,
SoX, and FlashAttention notes below apply to both ways of installing.

## By hand

Python **3.12** is the tested version. Shell examples use Bash; on Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell instead.

## 1. Get the code

```bash
git clone https://github.com/zekiworks/hilde.git
cd hilde
```

Run the remaining commands from this directory.

## 2. Create an environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

## 3. Install PyTorch, TorchAudio, and the dependencies

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

## 4. Optional: install FlashAttention 2

FlashAttention is needed only when selecting `--attn-implementation flash_attention_2`. It requires compatible CUDA hardware and a compatible build for your PyTorch installation. It cannot be used with CPU or float32 inference.

```bash
python -m pip install setuptools ninja packaging wheel
MAX_JOBS=4 python -m pip install flash-attn==2.8.3 --no-build-isolation
```

Use a matching [official FlashAttention wheel](https://github.com/Dao-AILab/flash-attention/releases) when available. Source builds require a compatible CUDA toolkit and C++ compiler; set `CUDA_HOME` to **your** toolkit installation if needed. If the import or build fails, confirm that the build matches your PyTorch and CUDA installation: a GPU driver's reported CUDA capability is not the same as the installed CUDA compiler/toolkit. See the [FlashAttention installation instructions](https://github.com/Dao-AILab/flash-attention#installation-and-features) for compatibility details.

The GPU workflow has been exercised on Linux with Python 3.12, PyTorch/TorchAudio 2.10.0 + CUDA 13.0, Qwen TTS 0.1.1, SoundFile 0.14.0, and FlashAttention 2.8.3. These versions are a tested combination, not a guarantee for every platform.

You can skip FlashAttention and select SDPA attention instead: `--attn-implementation sdpa`, or `sdpa` under **Advanced** in the web UI. CPU inference with SDPA has also been exercised.

## 5. Download the models

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
