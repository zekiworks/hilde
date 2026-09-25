# Hilde: notes for coding agents

Hilde is an audiobook studio on Qwen3-TTS. `audiobook_tts_web.py` is the web
server; its page (HTML, CSS, and JavaScript) is the `PAGE` string in the same
file. `audiobook_tts.py` is the command-line tool, which the server runs to
narrate and to create voices.

- Read `ARCHITECTURE.md` before changing behavior. It records the routes,
  storage, browser state, and the invariants the code must keep.
- Update `ARCHITECTURE.md` (internals) and `README.md` (users) in the same
  change as the code.
- Run `python -m unittest test_audiobook_tts` before and after a change. It
  needs no GPU or model.
- Then prove the change with a real run: the CLI command, or a throwaway web
  server on a free port with a temporary `--storage-root`.
- Keep hosts, secrets, and personal paths out of tracked files.
  `example_run.sh` is the one launch example; its paths are meant to be edited.
