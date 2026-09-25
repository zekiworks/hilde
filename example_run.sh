#!/usr/bin/env bash
# Start the Hilde web server for this machine: both local Qwen3-TTS models,
# the shared library in ~/AudiobookTTS, and port 8800 on every interface.
# It has no login: anyone who can reach port 8800 can use it.
#
# Narration uses every GPU that CUDA can open. To leave out a GPU that another
# program needs:
#   CUDA_VISIBLE_DEVICES=1,2,3 ./example_run.sh
#
# Extra arguments pass through, and a repeated option replaces the one below.
# For example, --port 8876 serves on another port, and --render-voice-previews
# renders comparable previews for older voices and exits without serving.
set -eu

# The interpreter with torch, qwen-tts, and soundfile; the server also runs
# audiobook_tts.py with it.
PYTHON=$HOME/.pyenv/versions/3.12.13/bin/python
MODELS=$HOME/code/models/Qwen

# Serve the checkout this script lives in.
cd "$(dirname "$0")"
exec "$PYTHON" audiobook_tts_web.py \
  --host 0.0.0.0 \
  --port 8800 \
  --storage-root "$HOME/AudiobookTTS" \
  --voice-design-model "$MODELS/Qwen3-TTS-12Hz-1.7B-VoiceDesign" \
  --voice-clone-model "$MODELS/Qwen3-TTS-12Hz-1.7B-Base" \
  "$@"
