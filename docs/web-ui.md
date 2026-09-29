# Web UI

Open the address the server prints when it starts, for example
`http://127.0.0.1:8800/`; `--open` opens it for you. The page has three tabs,
**Create**, **Voices**, and **Listen**, plus **Advanced** for settings.

## Create an audiobook

**Create** walks through three steps, one at a time: **Add your book**,
**Choose a voice**, and **Create audiobook**. A finished step collapses to a
summary with **Change**.

A book is a document from the dropdown, an upload, or a direct HTTP(S) URL
under **Add from a link**. The save-as filename is optional: the server infers
the document type and adds the matching extension when needed, including for
extensionless PDF URLs such as arXiv `/pdf/<id>` links. Files are limited to
64 MiB. Supported document types are PDF (`.pdf`), Markdown (`.md` or
`.markdown`), and plain text (`.txt` or `.text`), rather than HTML landing
pages. The voice step keeps the saved-voice dropdown for voices you know by
name, with **Search voices** beside it for finding one by description.

**Delete** beside the dropdown removes the chosen document after you confirm.
Audiobooks made from it are kept, and a job already queued keeps its own copy.

**Create audiobook** starts the job when a compatible narration worker is idle,
or it waits in the shared queue:
1. A PDF is extracted page by page, and a sentence a page break splits is
   joined back together. Text and Markdown skip extraction unless
   **Adapt the text for listening** is selected.
2. Optional adaptation runs the chosen language model in bounded concurrent
   paragraph batches; see [Text adaptation](text-adaptation.md).
3. Prepared text is saved as
   `Documents/<input-stem>-narration.txt`.
4. Narration writes
   `Audiobooks/<input-stem>-<voice-name>.mp3`.
5. Sentence-aligned completed WAV chunks supply exact sample boundaries for the
   synchronized narration-ready Markdown reader.
6. TorchAudio `MMS_FA` and Uroman force-align the known transcript to each
   completed chunk on CPU and persist integer-sample word boundaries.

Output directories and filenames are derived; the form never asks for either.
For a remote narration backend, the configured server voice ID is used as the
voice name. When the job finishes, **Start listening** opens the book in
**Listen** and **Download MP3** saves the retained server copy.

While a job runs, Create names its stage in plain words: Reading your
document, Preparing the narration, Creating the audio, and Finishing your
audiobook. Each stage has its own progress count and ETA; the browser also
retains a smoothed historical duration for the first estimate. A reload returns
to the running job's progress and reconnects to its server-sent event stream
without duplicating received log lines.

The queue is global across browsers. Every web submission uses the
server-owned pool of [narration workers](configuration.md#narration-workers): one job claims
all currently idle compatible workers, and each worker dynamically pulls chunk
batches from that audiobook. A second job waits when the first has claimed the
whole pool. Browsers see each worker's device (GPU index, model, memory) and
live state plus active and waiting jobs. SSH workers appear as numbered SSH
workers; SSH targets, server hostnames, speech-server URLs, and filesystem
paths are not published.

A job ID is derived only from the selected document's content version and the
voice version. Submitting that same pair while it is preparing, queued, or
running returns the existing job—even through another browser or under renamed
assets. The first accepted submission supplies the display names, output name,
and runtime settings used by that job.

Documents, voices, prepared text, and audiobook outputs may replace an existing
asset with the same name. The UI asks for confirmation only when the target
audiobook's recorded input-content version **and** voice version both match the
current selection. A changed input or changed voice replaces the old output
without an additional prompt.

Extraction and narration are resumable:

- source bytes and local voice files are snapshotted when the job is queued;
- converted PDF pages and committed adapted paragraph batches are reused;
- each completed narration chunk is an independently validated WAV checkpoint;
- restarting the server or pressing **Stop** leaves the job under
  `in_progress`;
- choosing **Create audiobook** again (or **Continue** / **Try again**) with the
  same document and voice versions reuses matching checkpoints and completes
  the final MP3 atomically;
- checkpoint manifests still reject incompatible extraction or inference
  settings even though those settings are not part of the public job ID.

The active and pending queue order is held by the server process. After a server
restart, submit unfinished document/voice pairs again to resume their durable
`in_progress` stages.

## Listen

Open **Listen** to find a completed audiobook in a table of titles, durations,
and source names. A title is the book's first heading, or its document's name
when that heading is a section such as Abstract. Search looks only at titles;
every word you type must
appear, in any order and case. **Delete** removes an audiobook and its
synchronized text after you confirm. **Listen** opens the book's player and
narration text, whose audio controls stay on screen while the text scrolls,
and **Download MP3** retrieves the retained server copy. Text keeps its paragraphs:
the playing sentence is tinted inside its paragraph, the paragraph is marked
with an accent bar, and the current word is filled in light orange with dark
text. Sentence-era readers created before paragraph grouping was recorded still
show one sentence per paragraph; paragraph-era readers regain their paragraphs
automatically.
Clicking a word seeks to that word (just before its aligned onset); clicking
elsewhere in a sentence, or on its attached figure or table, seeks to the
start of the sentence.

The reader streams the retained MP3's frames, unchanged, inside an exactly
indexed MP4, because browsers seek variable-bitrate MP3 through a coarse table
and then report the requested time while playing audio from up to a minute
away. Browsers without MP3-in-MP4 playback fall back to the plain MP3 and its
approximate seeking. Each sentence cue takes precedence over a stray word cue,
so a word alignment error never outlives its sentence. **Follow along** turns
the text like pages: it scrolls only once the playing sentence leaves the
screen, bringing it a quarter of the way down, so a figure stays in view while
its description is read. The web shell is served with `Cache-Control:
no-store`, so subsequent ordinary refreshes load
the current player code. Straightforward extracted tables remain selectable
Markdown tables, and extracted figures and formulas remain embedded as validated raster
images. Narration-ready descriptions above those visuals receive word timing;
raw table cells and image markup do not consume spoken-word cues. A visual-only
interval remains active for its complete audio cue instead of advancing to the
next text block. Standalone invisible PDF format-control artifacts are removed
before new narration; an existing audiobook's artifact interval is assigned to
the following visible block. When adaptation changes the prose, the reader
shows the narration-ready version, and a description the model wrote of a
figure, table, or equation carries a **Description** label. **Original**
shows the author's text muted beneath the passage made from it, headed with the
PDF page it starts on, and shows passages the model left out, such as a
copyright notice, as **Not narrated**. Books narrated without adaptation, or
adapted before Hilde recorded the original text, have no **Original**. If
forced alignment is partially or fully unavailable, exact sentence timing
remains usable. Existing paragraph-era reader sidecars receive estimated
sentence cues. Audiobooks created before reader sidecars were introduced remain
playable and downloadable without synchronized text.

If the player remains silent, check the browser and selected output device. A
Linux sound server exposing only **Dummy Output** has no real playback sink;
that does not establish that the generated file is silent.

## Voices

**Voices** lists every saved voice in a compact table: Preview, Voice name,
Prompt, and **Select**, **Use**, and **Delete** buttons, 50 rows at a time. The
prompt is the voice description given to VoiceDesign, and search looks only at
prompts: every word you type must appear, in any order and case, so
`warm british female` finds voices whose prompt contains all three. The
preview plays in place. **Use** makes the voice current and returns to
**Create**; the voice in use shows **In use** instead. **Delete** removes a
voice after you confirm; audiobooks made with it are kept, and a job already
queued keeps its own copy.

A new library comes with eight stock voices, each with its prompt: Balder,
Bragi, Mimir, and Vidar (male) and Eir, Freyja, Idun, and Sigrun (female).

To change a voice, choose **Select**: its name and prompt load into the editor
at the top. Change the prompt and choose **Listen**. VoiceDesign makes a new
version, which plays as soon as it is ready, while the saved voice stays as it
was. Listen again after each change until you like it, then choose **Save**,
which keeps exactly the version you heard. Save under another name to keep both
voices. **New voice** opens the same editor empty, and **Stop** cancels a
version being made. Every voice reads the same fixed passage, so previews
compare pace, tone, and naturalness on the same words; the passage is saved as
`transcript.txt` beside `reference.wav` and is neither shown nor editable.

Voices made before the fixed passage read other words. Render their comparable
previews once, while no audiobook is being made:

```bash
python audiobook_tts_web.py \
  --voice-clone-model models/Qwen3-TTS-12Hz-1.7B-Base \
  --render-voice-previews
```

This narrates the fixed passage with each such voice into its `preview.wav`
and exits; the voices themselves are unchanged. Until then, **Voices** notes
that their previews read an older passage. Voices made before prompts were
saved have none, so search cannot find them; write the prompt into the voice
folder's `description.txt` to make one searchable.

## Advanced

**Advanced**, beside the tabs and hidden on **Listen**, shows the server's
narration workers and the settings a browser may change; while they show, the
button reads **Simple** and hides them again:

- **This server** shows the narration workers as live chips, for example
  `GPU 0 idle` through `GPU 3 running`, followed by the device model and
  memory; hover a chip for its job. It also names the narration and
  voice-design models and the device voice creation uses.
- **Speech tuning**: precision and attention implementation, language, text
  encoding, and an optional seed.
- **Narration**, on **Create**: **Chunking**, the longest chunk in characters;
  [**Batch size**](configuration.md#batch-size); and **MP3 compression**.
- **Text adaptation**, on **Create** while adaptation is selected; see
  [Text adaptation](text-adaptation.md).
- **Voice files**, on **Voices**: **Reference WAV encoding**. It remains
  because the generated sample is required for local voice cloning; it is not
  an output-location choice.

Settings a remote speech server cannot use are disabled. Browsers cannot
select, pin, or name devices and cannot configure SSH hosts.

## Browser state

Editable form settings are stored in bounded `HttpOnly; SameSite=Strict`
cookies per browser. Model paths, model IDs, speech-server endpoints, worker
devices/hosts, credentials, storage paths, and output paths remain
server-owned. Public API responses name local devices and models but omit
hostnames, SSH targets, speech-server URLs, and paths. **AirDrop…** appears
only when the server runs on macOS with `pyobjc-framework-Cocoa`; other
clients use **Download MP3**.
