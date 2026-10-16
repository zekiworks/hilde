# Web UI

Open the address the server prints when it starts, for example
`http://127.0.0.1:8800/`; `--open` opens it for you.

## Header and navigation

The header shows Hilde's name and "Listen to your documents. Follow the text.
Ask to understand." (smaller on a phone). Below it are three tabs: **Create**,
**Voices**, and **Listen**. **Listen** opens the **Library**, the list of your
audiobooks, or the book you had open.

At the right of the header:

- **Settings** opens the settings in place of the page; **Close settings**, or
  any tab, closes them again. See [Settings](#settings).
- While anything is being made, or after an audiobook stopped or failed, a
  small indicator counts every job on this server, from any browser, for
  example `1 running · 2 queued` (or `1 failed`, `1 stopped`). It opens the
  [progress view](#following-an-audiobook-being-made).

## Create an audiobook

**Create** walks through three steps, one at a time: **Add the source document**,
**Choose a voice**, and **How Hilde prepares the text**. A finished step
collapses to a summary with **Change**; going back to a step keeps what you
chose in the others.

The first step offers three separate ways to choose a document:

- **Saved document**: one you added before. **Delete** beside it removes the
  chosen document after you confirm. Audiobooks made from it are kept, and a
  job already queued keeps its own copy.
- **From a URL**: a direct HTTP(S) link to a PDF, Markdown, or text file,
  downloaded when you press **Continue**. **Save as** is optional: the server
  works out the document type and adds the matching extension when needed,
  including for PDF links without one, such as arXiv `/pdf/<id>` links.
- **Upload a file**: **Choose a file…**, or drop a file anywhere on the step.

Files are limited to 64 MiB. Supported document types are PDF (`.pdf`),
Markdown (`.md` or `.markdown`), and plain text (`.txt` or `.text`), not HTML
web pages. Once chosen, the step shows **Chosen:** with the file name and its
type (PDF, Markdown, or Text). A problem, such as a link that is not a
document or a file type Hilde can't read, is said in a short sentence inside
the step. Adding a document under a name you already have asks first, naming
the file it would replace.

The voice step is a dropdown of your saved voices (the same choice as
**Select** on **Voices**), with a card that previews the one you choose.

The third step, **How Hilde prepares the text**, lists three options. Each
says in one line what it does and in another what it can change; **More**
under an option holds the longer explanation. Under each option's title a
model row names the model it uses with a badge saying where the text goes:
**Local** (your own machine or a model server you added) or **Cloud:
<provider>**, for example `Model: gpt-5 · Cloud: OpenAI ▾`. A local model is
shown by its own name. The figure and checking options start at `Model: Same
as the text model (<model> · <badge>) ▾`.

Press the row to open the model list in place: models grouped by provider and
by local server, with **Add local** and **Connect a provider** at the bottom.
Click a model to choose it and close the list; Enter or Escape closes it too.

When you added local servers and none of them answers, Hilde does not move
your text to the cloud on its own: the row reads `Model: choose a model ▾`
with "Your local server is not running." With no model at all it reads **No
model available** with "Add a local server or connect a provider."
**Create audiobook** stays disabled until every option you turned on has a
model, and says why beside it, for example "Choose a model for Check and
correct descriptions."

A **Summary** above **Create audiobook** lists the document, the voice, and
what each option will do, with the same badges, so you see where the text goes
before you send it. While Hilde is still finding out which models you have,
the badges read **Checking…** and, with adapting on, **Create audiobook**
waits until it knows where the text would go. When a speech server narrates, the summary names it
("Speech server: <model>") without a Local or Cloud badge, because the page
cannot know where that server runs.

Long document names and book titles take at most two lines, ending in an
ellipsis, in the step summaries, the progress view, the **In progress** list,
and the **Library**. Hover over one to see the full name.

A document whose content is already a book, under any file name, shows **You
already have this one** with **Open**, **Overwrite**, and **Change voice**.
Change voice continues to the voice step, and **Add this voice** reads the
book's own text in that voice: no model runs and nothing is written again.
Overwrite continues through the voice and preparation steps as for a new book,
and **Overwrite audiobook** asks (naming the book), then makes the book again
from this document with those settings, as **Recreate with the latest Hilde**
does on Listen: its text is written again, and its other voices will need to
be made again. A link typed under **From a URL** is the next book whatever the
dropdown shows, so that notice gives way to **Continue**.

Once **Create audiobook** has queued a book, the first step is empty again,
ready for the next one; **Start listening** empties it too. You can queue as
many books as you like; Hilde starts them in the order you add them. Below the
steps, **In progress** lists every book in the queue with its document, voice,
and stage; **View progress** opens the progress view.

**Create audiobook** starts the job when a compatible narration worker is idle,
or it waits in the shared queue:
1. A PDF is extracted page by page, and a sentence a page break splits is
   joined back together. Text and Markdown skip extraction unless
   **Adapt the text for listening** is selected.
2. Optional adaptation runs the language model chosen under **Adapt the text for
   listening** in bounded concurrent paragraph batches; a chart is described
   from its caption and the author's text, never from values read off the
   picture, unless **Interpret and narrate figures/charts (Experimental)** is
   ticked, when the **Figure model** reads every figure from its picture. With
   **Check and correct descriptions** (off unless ticked), each figure, table,
   and equation description is checked by the **Checking model** and corrected
   when a check finds a problem. See [Text adaptation](text-adaptation.md).
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
voice name. When a job you are watching finishes, **Create** shows **Your
audiobook is ready** above the steps: **Start listening** opens the book in
**Listen** and **Download MP3** saves the voice's audio as
`<book title> - <voice>.mp3` (a long title is shortened at a word; a book
without a title uses its document's name). The MP3 is there as soon as the
book is, so the download starts at once; the button then reads **Download
started** for a few seconds. It never says the file was saved, because only
the browser knows that. The card also gives the total time, counted from when
the job left the queue, and each stage's share: for example
`Total time: 6m 03s (363.2 s): reading 12s, adapting 1m 30s, narrating 4m 00s,
aligning 21s`. The job log ends with the same line. A run continued after a
stop counts only its own work.

### Following an audiobook being made

The progress view opens from the header indicator or **View progress**, and by
itself when a book you start begins at once. **Create another book** returns to
**Create** with the first step empty. The view lists every job on this server
under **Running** and **Queued**, each with its document and voice; a running
one also names the workers narrating it, and a queued one its place in line.

The book you follow shows five stages: **Reading the document**, **Preparing
the text for listening**, **Generating audio**, **Aligning text and audio**,
and **Ready**. A finished stage has a check mark and what it did, such as
`212 of 840 paragraphs`; a stage the job does not need shows **Skipped**: a
text or Markdown file is not read page by page, so **Reading the document** is
skipped (and so is preparing, when it is not adapted), and a new voice of an
existing book skips both. Under
the bar the count reads, for example, `900 of 35,226 parts · 2%`, and a line
says what is happening. Counts come only from the work itself. A time
estimate, "About N min left at the current pace", appears only once the
current stage has made headway; there is none before.

**Stop** ends a running book after you confirm ("Stop making '<title>'?");
what is finished is kept. **Cancel** takes a waiting one out of the queue. A
reload keeps following the running job without repeating its log.

When a book stopped or failed, both **Create** and the progress view show a
card for the latest one, also after a reload: the stage it stopped or failed
in, what was finished (for example `Reading the document: done · Preparing the
text: 212 of 840 paragraphs`), **Continue from the last completed part** (with
"Changing the settings or the voice since then starts the changed steps
again." and where the text goes), **Technical details** with its last log
lines, and **Dismiss**, which hides the card in this browser only. The server
forgets these when it restarts; a job you were following then says "The
server restarted, so this work stopped", and starting it again continues
where it left off.

The queue is global across browsers. Every web submission uses the
server-owned pool of [narration workers](configuration.md#narration-workers): one job claims
all currently idle compatible workers, and each worker dynamically pulls chunk
batches from that audiobook. A second job waits when the first has claimed the
whole pool. The progress view and **Settings** show each worker's device and
live state. GPUs of other machines appear as `Node 1 · GPU 2` and so on.
Speech-server URLs and filesystem paths are not published; the nodes' hosts
and paths are shown only to a browser on the server's own machine.

A job ID is derived only from the selected document's content version and the
voice version, plus whether it makes a new voice or remakes the book. Submitting
the same job while it is preparing, queued, or running returns the existing
job, even through another browser or under renamed assets. The first accepted
submission supplies the display names and runtime settings used by that job.

Documents and voices may replace an existing asset with the same name, after a
confirmation that names it. Making a voice the book already has asks first too.

Extraction and narration are resumable:

- source bytes and local voice files are snapshotted when the job is queued;
- converted PDF pages and committed adapted paragraph batches are reused;
- each completed narration chunk is an independently validated WAV checkpoint;
- restarting the server or pressing **Stop** leaves the job under
  `in_progress`;
- choosing **Create audiobook** again (or **Continue from the last completed
  part**) with the same document and voice versions reuses matching
  checkpoints and publishes the finished book in one step;
- checkpoint manifests still reject incompatible extraction or inference
  settings even though those settings are not part of the public job ID.

The active and pending queue order is held by the server process. After a server
restart, submit unfinished document/voice pairs again to resume their durable
`in_progress` stages.

## Listen

**Listen** opens the **Library**: a table of your audiobooks with **Title**,
**Duration**, **Source file**, **Voice** (with `(+2)` when other voices read
it too), **Last position (this device)**, for example `12:45 of 43:53` or
`Not started`, and **Open**, which reads **Continue** when this browser has a
place in the book. Continue opens the book in the voice you last heard, paused
at that place. A title is the book's first heading, or its document's name
when that heading is a section such as Abstract. Search (**Search titles**)
looks only at titles; every word you type must appear, in any order and case. **⋯** on a row holds
**Delete…**, which removes the audiobook, every voice of it, its text, its
notes, and its conversation after a confirmation naming the book. With no
audiobooks yet, the Library says so and offers **Create your first
audiobook**.

A click anywhere on a book's row opens it too: its header shows the title and
one line with the voice, the length, and the source file, with a **Library**
button at its top right (on a phone too) that goes back to the list in one
press, as does the **Listen** tab pressed while a book is open (from another
tab, **Listen** brings back the book you had open). A book read by more than one voice shows a
voice picker in that line. Above the text, and staying on screen while it
scrolls, are **Follow along**, the **Narration text | Source text** switch,
**⋯** (More), and the player: play, the seek bar, the time, the speed (it
shows, for example, **1.25×**; click it to choose 0.75× to 2×), mute and
volume, and **Ask Hilde**. On a phone the player sits at the bottom of the
screen instead and never covers the text. The text runs in a comfortable
reading column; on a wide screen the panel for the chat, a figure, or a source
page opens beside it.

**⋯** holds **Download MP3** (the voice being played; the button reads
**Download started** for a few seconds), **Download source** (the document the
book was made from, under the name it was added with; only while that document
is still there: the book's own copy, or a document with the same content in
**Documents**), **Change voice…**, **Recreate with the latest Hilde…**, and
**Keyboard shortcuts**. **Change voice** asks in a dialog which saved voice
should read the book and says what happens: "Read '<title>' with <voice>? The
text stays the same; <voice> is added beside the voices it has. Nothing is
replaced." Nothing is rewritten, and the voice you have keeps playing until
the new one is ready. **Recreate with the latest Hilde** asks first, naming
the text model and where it runs: Hilde prepares the text again from the
document with your current settings and records it with the voice you are
playing; the current text and that voice's recording are replaced. It is the
only way a book's text changes. Voices made from the earlier text are kept but
no longer play against the new one: the book offers **Make <voice> again** for
each. Notes stay; the conversation is cleared.

Your browser remembers, for each book, where you were (the sentence and the
time) and the speed you chose, and the width of the side panel: opening the
book again, or reloading the page, finds it paused at that sentence. This is
kept in the browser only, never in the book or on the server; the speed never
changes the audio.

Text keeps its paragraphs: the paragraph being read is marked by a thin blue
line at its left and a faint blue tint, and the word being read by a soft blue
background and underline; the text keeps its color. Sentence-era readers created before paragraph
grouping was recorded still show one sentence per paragraph; paragraph-era
readers regain their paragraphs automatically. Clicking a word seeks to that
word (just before its aligned onset); clicking elsewhere in a sentence seeks
to the start of the sentence. Selecting text (dragging, or a long press on a
phone) never seeks: a small menu offers **Ask about this**, **Listen from
here** (from the first selected word), and **Copy**.

Nothing you do in the view moves the audio or your place: closing the chat,
switching between Narration and Source text, opening a figure or a
page, going to another tab and back. If you scroll away while **Follow
along** is on, it stops following and **Back to where you're listening**
appears; it appears too after you open a figure or a page or follow a ¶ link.
With **Follow along** off, the page never scrolls by itself.

A figure shows on a light card with its caption under **From the paper**. On
a wide screen it shows as printed in the PDF: its part of the page, with the
titles, labels and legend printed around its panels, without the caption or
the text around it; on a phone the panels of a figure of several stack, each
its own part of the page with its title, named by its place, such as "Left
panel of the source figure". Click a figure to see it large beside the text
with its caption, a link to its PDF page, **Hilde's description**, and **Ask
about this figure**.

**Ask Hilde** opens the chat beside the book (on a phone or a narrow window,
in its place, with **← Back to the book**) and pauses the book, as **Ask about
this** on selected words and **Ask about this figure** do; it waits at that
sentence until you press play. Ask
about the book, or ask Hilde to write a Markdown file, such as a summary or
notes on a section. Hilde sees a one-line summary of every paragraph and reads
the paragraphs it needs before answering. For a long book on a model with a
small memory, it sees one line per section instead and searches the book for
the paragraphs it needs; a question that still doesn't fit says so plainly. A
paragraph it cites, such as ¶12,
is a link that scrolls the book to it. When the server has web search (see
[Web search for Chat](configuration.md#web-search-for-chat)), Hilde can also
search the web and read pages, for background or later work, and links what
it used; those links open in a new tab, and the chat's header shows **Web
search on**. Its answer streams in as it writes;
**Stop** ends it. The model is one of yours: pick it at the top of the chat
from the same list as on **Create**, grouped by provider and by local server;
a local server that does not answer is listed as "<host:port> — not running"
and cannot be chosen. **Add local** beside the picker adds a server; a local
model answers from the server that offers it. Each model shows **Local** or
**Cloud: <provider>**, so you see where your question goes before you send
it. With none, the chat asks you to add one first. ChatGPT, the Anthropic, OpenAI, and Google Gemini API
keys, and local models can chat; Claude Code models cannot:
Hilde says so when you send with one. The conversation is kept with the book,
one per book, for everyone using this server, and survives a refresh. **New
conversation** starts over. Your unsent question, what is attached to it, and
the conversation stay as they were when you look at a figure or a page, or
close the chat and open it again; an answer keeps arriving meanwhile. When a
conversation grows long, its oldest messages and reads leave what Hilde
remembers, and the chat says so.

Ask Hilde attaches the part being read (or the words you selected) as a card
above the message box: the section heading as in the book, below it your
selected words in quotes or the part's first words, **Change** to attach what
you have selected, or else what is playing now, and **×** to remove it. It
stays as attached while you write, even if you play the book again. Its
collapsed **Context details** list exactly what goes with
your question, grouped by where it comes from, each with its ¶ number and
page: "Your selection", "Hilde's narration" of those paragraphs and, where the
adaptation changed them, "From the paper". A selection brings only the
paragraphs it takes words from, never their neighbours. A question about a figure or table brings its caption, every
paragraph of the paper that names it by number ("Figure 2", "Fig. 2",
"Figures 1 and 2"; never one only printed near it), and "Hilde's description".
Its picture, laid out as in the PDF, goes too when the chosen model takes
pictures: ChatGPT and the Anthropic, OpenAI, and Gemini keys do, a local model
only when its server is marked as seeing images; otherwise the details say the
picture is not sent. The picture goes with that question only. Past questions
show the same card, collapsed and without its buttons.

The attached part sits directly above the message box and lights up briefly
when attached (a steady outline instead when your system asks for reduced
motion). It reads **Preparing…** while Hilde gathers it; **Send** then reads
**Preparing context…**, and pressing it (or Enter) sends your question as soon
as the part is ready. If gathering takes longer than 10 seconds or fails, the
part reads **Couldn't attach: Retry · Remove**: **Retry** asks for the same
part again, **Remove** drops it. A question sent then goes without it, and the
chat says **Sent without the selected text**. The box stays empty for your own
question; its hint says what Enter does, for example "Type a question, or
press Enter to explain the selection" (or the figure, or this part), and with
nothing attached "Ask about this book, or ask Hilde to write a summary file".
With a part attached and nothing typed, the button reads **Explain this part**
and it, or Enter, sends "Explain this part."; once you type it reads **Send**.
Your question stays in the box until Hilde has it: if
sending fails, the chat says why above the box and the text is still there.
The chat also says why when nothing can go yet: an empty box, models still
loading or none added, or Hilde still answering.

**🔈 Read aloud**, under each of Hilde's answers, reads it aloud in Hilde's
own voice (or, when the Hilde voice is gone from **Voices**, the first voice
there by name), marking the paragraph or list item being read. **❚❚ Pause
reading** stops at the word; **🔈 Resume reading** goes on from there, and
**Start over** reads from the beginning. Listening to another answer keeps
this one's place too. **Read answers aloud**, in the chat's header, reads each
answer as soon as it is finished. One sound plays at a time: reading an
answer pauses the book, and when it ends the book stays paused and **▶ Resume
book** appears under the answer; starting the book, or a voice preview, stops
the others. The first answer after a while takes a few seconds longer while the
narration model loads; it stays loaded for five minutes after the last answer
read, then frees its GPU. Code, web addresses, and Markdown are not read; a
citation such as ¶12 is read as "paragraph 12". Reading aloud needs the
server's own narration model (`--voice-clone-model`); without it, neither
shows.

Notes Hilde writes are listed under the book's title in **Notes Hilde
wrote**. Each shows its file name, the book, the section you asked about when
it was written (or "No section"), that it is kept in this book's files on the
server, and its size, with **Copy text** and **Download .md**. In the chat, a
note's line reads "· Saved" only once the server has written it. Hilde never
overwrites a note: it writes a new one under a new name. Notes stay until you
ask Hilde to delete one. **Recreate with the latest Hilde** keeps them and
starts a new conversation, since paragraph numbers change. Books made before
this version of Hilde have no paragraph summaries: the chat says to recreate
the book first.

The reader streams the retained MP3's frames, unchanged, inside an exactly
indexed MP4, because browsers seek variable-bitrate MP3 through a coarse table
and then report the requested time while playing audio from up to a minute
away. Safari and Chrome both play this MP4, so a clicked word is the word you
hear; browsers without MP3-in-MP4 playback fall back to the plain MP3 and its
approximate seeking. From the first press of play the whole book downloads in
the background, with its progress in the player; once it is in, playback moves
to that copy at the next sentence start, and from then on nothing waits on the
network, seeking included. Each sentence cue takes precedence over a stray word cue,
so a word alignment error never outlives its sentence. **Follow along** turns
the text like pages: it scrolls only once the playing sentence leaves the
screen, bringing it a quarter of the way down, so a figure stays in view while
its description is read. While a book is open, the player takes YouTube's
keyboard shortcuts: Space or K plays or pauses, J and L go back and forward
10 seconds, ← and → 5 seconds, ↑ and ↓ change the volume while the player has
focus, M mutes, 0 to 9 jump to that tenth of the book, Home and End go to its
start and end, < and > slow it down and speed it up (0.25× to 2×), and ?
opens the **Keyboard shortcuts** list. Typing in a text field, such as
the chat, never triggers them. The web shell is served with `Cache-Control:
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
shows the narration-ready version (**Narration text**: what Hilde reads
aloud), and a description the model wrote of a figure, table, or equation
carries the label **Hilde's description**. **Source text** shows the
document's own words in place of the narration made from them, each passage
tagged with the PDF page it starts on (click the page to see it beside the
text): **Original** where the adaptation changed it, **Unchanged** where Hilde
reads it word for word, and **Not narrated** for passages the model left out,
such as a copyright notice. Switching keeps your place: the source paragraph
of the sentence being read takes its place on screen, and switching back
brings the sentence back. Books narrated without adaptation, or adapted before
Hilde recorded the original text, have no **Source text**. A passage Hilde
asked the model about again and still found stating a number or name the
source doesn't print, or a lost "not", shows "Check: …" under its original,
so you know what to listen for.
A PDF table's picture carries its whole caption for screen readers. If
forced alignment is partially or fully unavailable, exact sentence timing
remains usable. Existing paragraph-era reader sidecars receive estimated
sentence cues. Audiobooks created before reader sidecars were introduced remain
playable and downloadable without synchronized text.

If the player remains silent, check the browser and selected output device. A
Linux sound server exposing only **Dummy Output** has no real playback sink;
that does not establish that the generated file is silent.

## Voices

**Voices** lists every saved voice in a compact table: a preview button,
**Voice name**, **Voice description**, **Modified** (when its sample,
transcript, or description last changed; hidden on a phone), and **Select**,
50 rows at a time. Each description shows one line; **Full description** under
it opens the whole text in place, and **Show less** closes it, without
selecting the voice. The preview plays in place; playing another stops the
first, and a preview never changes which voice is selected.

**Select** chooses the voice for your next audiobook: the same choice as the
voice step on **Create**. The chosen voice shows **✓ Selected** instead, and a
line says it is selected, with **Back to Create**. **⋯** on a row holds
**Edit**, **Rename…**, and **Delete…**:

- **Delete** removes a voice after a confirmation naming it; audiobooks made
  with it keep their audio, and a job already queued keeps its own copy.
- **Rename** asks for the new name in a dialog and keeps everything else: its
  sample, transcript, and description stay as they are, and the audiobooks it
  read show the new name too. It waits while an audiobook is being made with
  the voice, and refuses a name another voice already has.
- **Edit** opens the voice in the editor at the top.

Search looks only at descriptions: every word you type must begin a word of
the description, in any order, ignoring case and accents, so
`warm british female` finds voices whose description contains all three. When
none matches, the page says "No voice description contains all of: warm,
british, female." with **Clear search**.

A new library comes with eight stock voices, each with its description:
Balder, Bragi, Mimir, and Vidar (male) and Eir, Freyja, Idun, and Sigrun
(female).

### Making a new voice

**New voice** opens the editor. **Describe the voice** takes a description
such as "Warm British female narrator in her thirties; calm, clear, and
unhurried.", and **Name** the voice's name. **Generate preview** has
VoiceDesign make a version, which plays as soon as it is ready; **Stop**
cancels it. Voices can be designed only while no audiobook is being made.
**Save voice** turns on once you have heard this preview to its end or for at
least 3 seconds, and only while the description is the one previewed; change
the description and you generate a new preview first. Save keeps exactly the
version you heard. **Edit** on a voice opens the same editor with its name and
description, as **Edit <name>**; the saved voice stays as it was until you
save.

When the name is taken, the editor says "A voice named '<name>' exists.
Saving replaces it; its current version is kept in its history.", and **Save
voice** asks first, naming the voice. The version it replaces is kept inside
the voice's folder, in `.versions/v1`, `v2`, and so on, so books made with it
still count as read by this voice. Save under another name to keep both
voices. Every voice reads the same fixed passage, so previews compare pace,
tone, and naturalness on the same words; the passage is saved as
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
that their previews read an older passage. Voices made before descriptions were
saved have none, so search cannot find them; write the description into the
voice folder's `description.txt` to make one searchable.

## Settings

**Settings**, in the header, replaces the page until you choose **Close
settings** or a tab. Each setting says when it takes effect: "Applies to the
next audiobook" (some also to the next voice preview) or "Applies now". The
groups:

- **Narration**: precision, attention, language, text encoding, an optional
  seed, [**Batch size**](configuration.md#batch-size), **Chunking** (the
  longest chunk in characters; a book's new voice keeps the book's own), and
  **MP3 compression**. Settings a speech server cannot use are turned off.
- **Text preparation**: how many workers adapt the text at once and how many
  paragraphs each takes; see [Text adaptation](text-adaptation.md).
- **Voices**: **Reference WAV encoding**. It remains because the generated
  sample is required for local voice cloning; it is not an output-location
  choice.
- **This machine's GPUs**: the server's own narration workers as live chips,
  for example `GPU 0 idle` or `GPU 3 running`, with the device model and
  memory; hover a chip for its job. **Narrates on** has a box for each device:
  untick one and its chip reads `off`, and it takes no book until ticked again
  (applies now).
- **Other machines**: the workers of the nodes that narrate over SSH, each node
  with **Remove** (after a confirmation naming it), and **Add workers**: enter
  its address, **Connect**, **Set up** if it lacks Hilde's Python or the speech
  model, untick any GPU to leave alone, **Add node**; see
  [Narration workers on other machines](ssh-workers.md). Changes apply now; a
  running audiobook keeps its workers.
- **Models and connection**, shown but not changed here: the narration,
  voice-design, and text models, and **Connection**, which says whether this
  browser will warn about downloads (plain HTTP from another device does) and,
  when the server runs with `--https`, offers **Download Hilde's certificate**
  to install once on this device; see [HTTPS](../README.md#https).

Only a browser on the server's own machine sees the nodes' hosts and may change
the GPUs and machines; elsewhere those groups say "Only a browser on the
server's own machine can change this." Browsers cannot select, pin, or name
devices.

## Asking before risky actions

Deleting a voice, a book, or a document, renaming a voice, recreating or
overwriting a book, replacing a voice's recording, saving a voice or adding a
document over an existing name, stopping a book, removing a machine, and
starting a new conversation each ask first in a dialog on the page that names
what is affected. Previewing, opening, and playing never ask.

## Browser state

Editable form settings, the local model servers added under **Add local**
among them, are stored in bounded `HttpOnly; SameSite=Strict`
cookies per browser. Local servers the operator started Hilde with
(`--local-server`) are offered to every browser: they come first in the model
lists, marked "added on the server", and **Add local** shows them without
**Remove**. This browser alone also remembers, in its local storage,
each book's place and speed, the side panel's width, and which stopped or
failed jobs you dismissed. Model paths, model IDs, speech-server endpoints,
worker devices/hosts, credentials, storage paths, and output paths remain
server-owned; only a browser on the server's machine may add or remove the
nodes in `workers.yaml`. Public API responses name local devices and models but omit
hostnames, SSH targets, speech-server URLs, and paths; only `/api/workers`,
which answers a browser on the server's own machine alone, names the nodes'
hosts and paths. **AirDrop…** appears
only when the server runs on macOS with `pyobjc-framework-Cocoa`; other
clients use **Download MP3**.

When Hilde's server stops answering, for example while the machine restarts,
the page says so at the top and turns off everything that needs the server:
creating, saving, deleting, settings, and Chat. The tabs, the player, and the
text stay, so a book already loaded in this browser plays on. The page checks
every two seconds and, once the server answers, reloads its state and turns
everything back on by itself; there is no need to refresh.
