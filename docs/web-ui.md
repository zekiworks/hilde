# Web UI

Open the address the server prints when it starts, for example
`http://127.0.0.1:8800/`; `--open` opens it for you. The page has three tabs,
**Create**, **Voices**, and **Listen**, plus **Advanced** for settings.

## Create an audiobook

**Create** walks through three steps, one at a time: **Add the source document**,
**Choose a voice**, and **Create audiobook**. A finished step collapses to a
summary with **Change**.

A book is made from a document chosen under **Your documents**, uploaded with
**Upload a file** or dropped anywhere on the first step's panel, or downloaded
from a direct HTTP(S) URL
under **Add from a link**. The save-as filename is optional: the server infers
the document type and adds the matching extension when needed, including for
extensionless PDF URLs such as arXiv `/pdf/<id>` links. Files are limited to
64 MiB. Supported document types are PDF (`.pdf`), Markdown (`.md` or
`.markdown`), and plain text (`.txt` or `.text`), rather than HTML landing
pages. The voice step keeps the saved-voice dropdown for voices you know by
name, with **Search voices** beside it for finding one by description.

**Delete** beside the dropdown removes the chosen document after you confirm.
Audiobooks made from it are kept, and a job already queued keeps its own copy.

Long document names and book titles take at most two lines, ending in an
ellipsis, in the step summaries, the progress card, the **In progress** list,
and the **Listen** table. Hover over one in the step summary, the list, or the
table to see the full name.

A document whose content is already a book, under any file name, shows **You
already have this one** with **Open** and **Change voice**. Change voice
continues to the voice step, and **Add this voice** reads the book's own text
in that voice: no model runs and nothing is written again. A link typed under
**Add from a link** is the next book whatever the dropdown shows, so that
notice gives way to **Continue**.

**Start another audiobook** and **Create another audiobook** open the first
step empty, as does **Start listening**: the document just made into a book is
no longer chosen.

**Create audiobook** starts the job when a compatible narration worker is idle,
or it waits in the shared queue:
1. A PDF is extracted page by page, and a sentence a page break splits is
   joined back together. Text and Markdown skip extraction unless
   **Adapt the text for listening** is selected.
2. Optional adaptation runs the language model chosen under **Adapt the text for
   listening** in bounded concurrent paragraph batches; see
   [Text adaptation](text-adaptation.md).
3. Narration reads the text in the chosen voice.
4. The finished book appears in **Listen**: its text, its follow-along view,
   and the voice's audio, kept together in one folder per book (see
   [Storage](configuration.md#storage)).
5. Sentence-aligned completed WAV chunks supply exact sample boundaries for the
   synchronized narration-ready Markdown reader.
6. TorchAudio `MMS_FA` and Uroman force-align the known transcript to each
   completed chunk on CPU and persist integer-sample word boundaries.

Output folders and filenames are derived; the form never asks for either.
For a remote narration backend, the configured server voice ID is used as the
voice name. When the job finishes, **Start listening** opens the book in
**Listen** and **Download MP3** saves the voice's audio as
`<document>-<voice>.mp3`. The card also gives the total time, counted from
when the job left the queue, and each stage's share: for example
`Total time: 6m 03s (363.2 s): reading 12s, adapting 1m 30s, narrating 4m 00s,
aligning 21s`. The job log ends with the same line. A run continued after a
stop counts only its own work.

While a job runs, Create names its stage in plain words: Reading your
document, Preparing the narration, Creating the audio, and Finishing your
audiobook. Under the bar, a line says what is happening: reading page 3 of
12, rewriting paragraph 40 of 135, speaking part 900 of 35,226, joining the
parts into one recording after the last one is spoken, and matching the words
to the audio for the follow-along view. Each step has its own time left,
worked out from its average pace so far, so it settles instead of jumping as
chunks finish in bursts; until a stage is a little way in, the browser counts
down how long that stage took last time. A reload returns
to the running job's progress and reconnects to its server-sent event stream
without duplicating received log lines.

The queue is global across browsers. Every web submission uses the
server-owned pool of [narration workers](configuration.md#narration-workers): one job claims
all currently idle compatible workers, and each worker dynamically pulls chunk
batches from that audiobook. A second job waits when the first has claimed the
whole pool. Browsers see each worker's device (GPU index, model, memory) and
live state plus active and waiting jobs. GPUs of other machines appear as
`Node 1 · GPU 2` and so on. Speech-server URLs and filesystem paths are not
published; the nodes' hosts and paths are shown only to a browser on the
server's own machine.

A job ID is derived only from the selected document's content version and the
voice version, plus whether it makes a new voice or remakes the book. Submitting
the same job while it is preparing, queued, or running returns the existing
job, even through another browser or under renamed assets. The first accepted
submission supplies the display names and runtime settings used by that job.

Documents and voices may replace an existing asset with the same name. Making
a voice the book already has asks for confirmation first.

Extraction and narration are resumable:

- source bytes and local voice files are snapshotted when the job is queued;
- converted PDF pages and committed adapted paragraph batches are reused;
- each completed narration chunk is an independently validated WAV checkpoint;
- restarting the server or pressing **Stop** leaves the job under
  `in_progress`;
- choosing **Create audiobook** again (or **Continue** / **Try again**) with the
  same document and voice versions reuses matching checkpoints and publishes
  the finished book in one step;
- checkpoint manifests still reject incompatible extraction or inference
  settings even though those settings are not part of the public job ID.

The active and pending queue order is held by the server process. After a server
restart, submit unfinished document/voice pairs again to resume their durable
`in_progress` stages.

## Listen

Open **Listen** to find a completed audiobook in a table of titles, durations,
source names, and when a voice of it was last made. A title is the book's first heading, or its document's name
when that heading is a section such as Abstract. Search looks only at titles;
every word you type must
appear, in any order and case. **Delete** removes an audiobook, every voice of
it, and its text after you confirm. **Listen** opens the book's player and
narration text, whose audio controls stay on screen while the text scrolls,
and **Download MP3** retrieves the voice being played. A book read by more
than one voice shows a voice picker. **Change voice** reads the same text in
another saved voice: nothing is rewritten, and the voice you have keeps
playing until the new one is ready. **Recreate with the latest Hilde** writes
the book again from its document with the current text adaptation, after you
confirm; it is the only way a book's text changes. Voices made from the
earlier text are kept but no longer play against the new one: the book offers
**Make <voice> again** for each. Text keeps its paragraphs:
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
away. Safari and Chrome both play this MP4, so a clicked word is the word you
hear; browsers without MP3-in-MP4 playback fall back to the plain MP3 and its
approximate seeking. From the first press of play the whole book downloads in
the background, with its progress under the title; once it is in, playback moves
to that copy at the next sentence start, and from then on nothing waits on the
network, seeking included. Each sentence cue takes precedence over a stray word cue,
so a word alignment error never outlives its sentence. **Follow along** turns
the text like pages: it scrolls only once the playing sentence leaves the
screen, bringing it a quarter of the way down, so a figure stays in view while
its description is read. The web shell is served with `Cache-Control:
no-store`, so subsequent ordinary refreshes load
the current player code. A table in a PDF shows as printed, a picture cut from
its page, because the cells PDF extraction rebuilds can split words and carry
stray markup; tables in Markdown or text documents remain selectable tables.
Extracted figures and formulas remain embedded as validated raster
images. Narration-ready descriptions above those visuals receive word timing;
table cells are never read aloud, and image markup does not consume spoken-word cues. A visual-only
interval remains active for its complete audio cue instead of advancing to the
next text block. Standalone invisible PDF format-control artifacts are removed
before new narration; an existing audiobook's artifact interval is assigned to
the following visible block. When adaptation changes the prose, the reader
shows the narration-ready version, and a description the model wrote of a
figure, table, or equation carries a **Description** label. **Original**
shows the author's text muted beneath the passage made from it, headed with the
PDF page it starts on, and shows passages the model left out, such as a
copyright notice, as **Not narrated**. Books narrated without adaptation, or
adapted before Hilde recorded the original text, have no **Original**.
A PDF table's picture carries its whole caption for screen readers. If
forced alignment is partially or fully unavailable, exact sentence timing
remains usable. Existing paragraph-era reader sidecars receive estimated
sentence cues. Audiobooks created before reader sidecars were introduced remain
playable and downloadable without synchronized text.

If the player remains silent, check the browser and selected output device. A
Linux sound server exposing only **Dummy Output** has no real playback sink;
that does not establish that the generated file is silent.

## Voices

**Voices** lists every saved voice in a compact table: Preview, Voice name,
Prompt, Modified (when its sample, transcript, or prompt last changed), and
**Select**, **Use**, **Rename**, and **Delete** buttons, 50 rows at a time. The
prompt is the voice description given to VoiceDesign, and search looks only at
prompts: every word you type must appear, in any order and case, so
`warm british female` finds voices whose prompt contains all three. The
preview plays in place. **Use** makes the voice current and returns to
**Create**; the voice in use shows **In use** instead. **Delete** removes a
voice after you confirm; audiobooks made with it are kept, and a job already
queued keeps its own copy. **Rename** gives a voice a new name and keeps
everything else: its sample, transcript, and prompt stay as they are, and the
audiobooks it read show the new name too. It waits while an audiobook is being
made with the voice, and refuses a name another voice already has.

A new library comes with eight stock voices, each with its prompt: Balder,
Bragi, Mimir, and Vidar (male) and Eir, Freyja, Idun, and Sigrun (female).

To change a voice, choose **Select**: its name and prompt load into the editor
at the top. Change the prompt and choose **Listen**. VoiceDesign makes a new
version, which plays as soon as it is ready, while the saved voice stays as it
was. Listen again after each change until you like it, then choose **Save**,
which keeps exactly the version you heard. The version it replaces is kept
inside the voice's folder, in `.versions/v1`, `v2`, and so on, so books made
with it still count as read by this voice. Save under another name to keep both
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
  memory; hover a chip for its job. **This machine narrates on** has a box for
  each of the server's own devices: untick one and its chip reads `off`, and
  it takes no book until ticked again. **Other machines** lists the nodes that
  narrate over SSH, each with **Remove**, and **Add workers** adds one: enter
  its address, **Connect**, **Set up** if it lacks Hilde's Python or the speech
  model, untick any GPU to leave alone, **Add node**; see
  [Narration workers on other machines](ssh-workers.md). Only a browser on
  the server's own machine sees the hosts and may change them. It also names
  the narration and voice-design models and the device voice creation uses.
- **Speech tuning**: precision and attention implementation, language, text
  encoding, and an optional seed.
- **Narration**, on **Create**: **Chunking**, the longest chunk in characters;
  [**Batch size**](configuration.md#batch-size); and **MP3 compression**.
- **Text adaptation**, on **Create** while adaptation is selected: how many
  batches run at once; see [Text adaptation](text-adaptation.md).
- **Voice files**, on **Voices**: **Reference WAV encoding**. It remains
  because the generated sample is required for local voice cloning; it is not
  an output-location choice.

Settings a remote speech server cannot use are disabled. Browsers cannot
select, pin, or name devices.

## Browser state

Editable form settings are stored in bounded `HttpOnly; SameSite=Strict`
cookies per browser. Model paths, model IDs, speech-server endpoints, worker
devices/hosts, credentials, storage paths, and output paths remain
server-owned; only a browser on the server's machine may add or remove the
nodes in `workers.yaml`. Public API responses name local devices and models but omit
hostnames, SSH targets, speech-server URLs, and paths; only `/api/workers`,
which answers a browser on the server's own machine alone, names the nodes'
hosts and paths. **AirDrop…** appears
only when the server runs on macOS with `pyobjc-framework-Cocoa`; other
clients use **Download MP3**.
