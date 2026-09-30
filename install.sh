#!/bin/sh
# Install or update Hilde:
#
#   curl -fsSL https://raw.githubusercontent.com/zekiworks/hilde/main/install.sh | sh
#
# It clones Hilde into ~/hilde (or pulls it if already there), creates a
# Python 3.12 environment there with uv, installs the PyTorch build that suits
# this machine's NVIDIA driver (or the CPU build), installs the dependencies,
# and adds a `hilde` command to ~/.local/bin. Nothing needs sudo, and your
# library in ~/hilde/User is kept. Run it again to update.
#
# Settings, all optional:
#   HILDE_HOME     where Hilde lives (default ~/hilde)
#   HILDE_BIN_DIR  where the `hilde` command goes (default ~/.local/bin)
#   HILDE_TORCH    PyTorch build: cu130, cu128, cu126, or cpu (default: chosen
#                  from the NVIDIA driver; ignored on macOS)
#   HILDE_REPO     the Git repository to clone
#
# Everything sits inside main(), so a download cut short runs nothing.

set -eu

say() { printf 'hilde: %s\n' "$*"; }
fail() { printf 'hilde: %s\n' "$*" >&2; exit 1; }

# The PyTorch build for this machine. A CUDA 13 build needs driver 580 or
# newer; CUDA 12 builds run on 525 or newer.
torch_build() {
  if [ -n "${HILDE_TORCH:-}" ]; then
    printf '%s' "$HILDE_TORCH"
    return
  fi
  driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null \
    | head -n 1 | cut -d. -f1) || driver=
  case $driver in
    '' | *[!0-9]*) printf cpu ;;
    *)
      if [ "$driver" -ge 580 ]; then printf cu130
      elif [ "$driver" -ge 525 ]; then printf cu126
      else printf cpu
      fi ;;
  esac
}

main() {
  home=${HILDE_HOME:-"$HOME/hilde"}
  bin_dir=${HILDE_BIN_DIR:-"$HOME/.local/bin"}
  repo=${HILDE_REPO:-https://github.com/zekiworks/hilde.git}

  system=$(uname -s)
  case $system in
    Linux | Darwin) ;;
    *) fail "$system is not supported by this installer; see docs/installation.md in the repository." ;;
  esac
  command -v git >/dev/null 2>&1 || fail "git is needed; install it and run this again."
  command -v curl >/dev/null 2>&1 || fail "curl is needed; install it and run this again."

  # uv installs Python 3.12 itself when the system has none.
  uv=$(command -v uv 2>/dev/null) || uv=
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [ -n "$uv" ] || [ ! -x "$candidate" ] || uv=$candidate
  done
  if [ -z "$uv" ]; then
    say "Installing uv, the Python package manager, into ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
    uv=$HOME/.local/bin/uv
    [ -x "$uv" ] || fail "uv did not install; see https://docs.astral.sh/uv/"
  fi

  if [ -d "$home/.git" ]; then
    say "Updating $home"
    git -C "$home" pull --ff-only --quiet \
      || fail "$home has local changes that block the update; commit or undo them, then run this again."
  elif [ -e "$home" ]; then
    fail "$home exists but is not a Hilde checkout; move it, or set HILDE_HOME to another folder."
  else
    say "Downloading Hilde into $home"
    git clone --quiet --depth 1 "$repo" "$home"
  fi

  say "Preparing Python 3.12 in $home/.venv"
  "$uv" venv --quiet --allow-existing --python 3.12 "$home/.venv"
  python=$home/.venv/bin/python

  if [ "$system" = Darwin ]; then
    say "Installing PyTorch"
    "$uv" pip install --quiet --python "$python" torch==2.10.0 torchaudio==2.10.0
  else
    build=$(torch_build)
    case $build in
      cpu | cu126 | cu128 | cu130) ;;
      *) fail "HILDE_TORCH must be cpu, cu126, cu128, or cu130, not $build." ;;
    esac
    say "Installing PyTorch ($build build; this is the largest download)"
    "$uv" pip install --quiet --python "$python" torch==2.10.0 torchaudio==2.10.0 \
      --index-url "https://download.pytorch.org/whl/$build"
  fi
  say "Installing Hilde's dependencies"
  "$uv" pip install --quiet --python "$python" -r "$home/requirements.txt"
  # qwen_tts warns on import about optional flash-attn and SoX; Hilde runs
  # without either, so its output shows only when the import fails.
  check=$("$python" -c "import torch, torchaudio, qwen_tts, soundfile, pymupdf4llm" 2>&1) \
    || { printf '%s\n' "$check" >&2; fail "the installed packages do not import; see the errors above."; }

  mkdir -p "$bin_dir"
  cat >"$bin_dir/hilde" <<EOF
#!/bin/sh
# Start Hilde, installed in $home. Written by install.sh; running the
# installer again rewrites it. Arguments pass through to audiobook_tts_web.py,
# and a repeated option replaces the one below.
cd "$home" || exit 1
exec "$python" audiobook_tts_web.py --allow-model-downloads \\
  --voice-clone-model Qwen/Qwen3-TTS-12Hz-1.7B-Base \\
  --voice-design-model Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign \\
  "\$@"
EOF
  chmod +x "$bin_dir/hilde"

  gpu=$("$python" -c "import torch; print('CUDA' if torch.cuda.is_available() else 'MPS' if torch.backends.mps.is_available() else 'CPU')" 2>/dev/null) || gpu=CPU
  say "Installed. Speech will run on: $gpu"
  case :$PATH: in
    *:"$bin_dir":*) start="hilde --open" ;;
    *) start="$bin_dir/hilde --open"
       say "$bin_dir is not on your PATH; add it to run plain 'hilde'." ;;
  esac
  cat <<EOF

  Start Hilde:  $start
  It serves http://127.0.0.1:8800/ on this computer only. The two speech
  models, about 4.3 GB each, download the first time they are used.
  To update, run the install command again.

EOF
}

main "$@"
