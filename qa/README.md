# Hilde QA

The records, rules and IDs of Hilde's QA are in [AGENTS.md](AGENTS.md); fact sets are described in
[facts/README.md](facts/README.md). This page covers the model rating script.

## Rating the models: `rate_models.py`

`rate_models.py` rates every model Hilde can use, job by job, on Hilde's own checks and writes the ratings file
the model pickers' "Suggested for <job>" sections read (`~/.hilde/ratings.json`; its schema and the label rules are
in [docs/configuration.md](../docs/configuration.md#suggested-models)). **Do not run it without Can's go**: it
sends the test papers to every model and narrates on the GPUs.

```sh
python qa/rate_models.py --dry-run                    # the plan; no model, server or speech-to-text is asked
python qa/rate_models.py --stages prepare_text,figures --check-text-model lm-studio/<model> ...
```

| Job (stage) | What runs | Score (0–100) | errors | speed |
| --- | --- | --- | --- | --- |
| `prepare_text` | each text model adapts each test paper fully, as a book's text | share of the paper's facts right (`facts/<paper>.yaml`, scored by `qa.py`'s rules) | facts dropped (missing) or changed (wrong) per run | pages per minute |
| `figures` | each model that reads images describes figures, tables and equations from their pictures (descriptions only, as `describe.py --interpret`) | share of the figure facts ("Figure N") right | figure facts not right | pages per minute |
| `check` | `--check-text-model` writes the descriptions, each model checks and corrects them (as `describe.py --verify`) | share of facts right after checking | errors missed: facts wrong after checking; `found`: facts wrong in most of the text model's own unchecked runs and right after checking | pages per minute |
| `ask` | left out: Hilde has no question set (`questions.yaml` holds open questions for humans) | | | |
| `narration` | each `--narration-model` reads `narration_passages.yaml` (numbers, units, dates, names from the QA papers) with `--voice`; a speech-to-text server that already runs (`--stt-url`, OpenAI-compatible `/v1/audio/transcriptions`) writes it back | share of words right | wrong, missing or extra words (both texts as spoken: numbers as words, "%" as percent) | seconds of audio per second of work, model loading included |
| `voice_design` | none: Can or Ümit add models they rated by ear to the file by hand | | | |

- Each model runs every paper `--runs` times (3). `score` and `errors` are the means over the runs, `noise` the
  standard deviation of the runs' scores (it decides "Similar accuracy"), `speed` is measured on this machine and
  `machine` names it (`--machine`, default the host name).
- The models are those the running Hilde server lists (`GET <--hilde-url>/api/paper/models`, default
  `http://127.0.0.1:8800`), or a saved answer of that route (`--catalog`); `--models` keeps only some. A dry run
  without `--catalog` reads only Hilde's remembered local models and lists no cloud model.
- The papers are those with a fact set (`--papers` to choose), found by their `papers.yaml` hash in `--pdf-dir`
  (default Hilde's Documents folder) and copied into the work folder first; the reference voice is copied too.
- Before it starts and before every run it reads `GET <--hilde-url>/api/jobs` and stops while any job is queued or
  running, or when it can't tell. It only reads from the Hilde server and changes nothing on it or on any model
  server.
- Without a speech-to-text server (`--stt-url` or `HILDE_STT_URL`) a run with narration stops before anything
  runs and asks you to ask Can which one to use; leave narration out with `--stages`.
- Runs are kept in `--work` (default `~/.cache/hilde-ratings`), so a stopped rating continues where it stopped. The
  ratings file (`--out`, default Hilde's) is written once at the end, atomically (a private temporary file, then
  a rename), checked against Hilde's own schema; stages not rated now, voice design's hand entries above all, are
  kept from the file there.
- Unit tests with stubs: `python -m unittest test_audiobook_tts.RateModelsScriptTests`.
