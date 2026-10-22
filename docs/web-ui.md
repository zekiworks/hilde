# Web UI

Open the address the server prints when it starts, for example
`http://127.0.0.1:8800/`; `--open` opens it for you.

## Header and navigation

The app is called **Hilde Reader**, and so is its browser tab. The header shows
its emblem, a copper leaf, beside the name "Hilde Reader", and under the name
"Listen to your documents. Follow the text. Ask to understand." (smaller on a
phone). The same leaf is the tab's icon and, when you add the page to a phone's
home screen, the icon there, labelled "Hilde". Below the header are the tabs: **Create**,
**Voices**, and **Listen**, then **Progress** while there is a job to follow.
**Listen** opens the **Library**, the list of your audiobooks, or the book you
had open.

**Progress** shows while anything is being made, queued, or prepared on this
server, from any browser, and after an audiobook stopped or failed until you
dismiss it. It opens the [progress view](#following-an-audiobook-being-made);
once nothing is left it goes, and if you were on it you are back on **Create**.

At the right end of the tab row is **Settings**: a tab like the others (the
same height and rounded top corners, standing on the same line), blue with a
white gear, named "Settings" for screen readers; its right edge is as far in
from the row's end as **Create**'s left edge is from its start, on a wide
screen and on a phone. It opens the settings in place of the page; while they
are open it shows as the selected tab does, pressed down and joined to the
page below, with a white bar along its top (and no other tab is selected).
**Settings** again, **Close settings**, or any tab closes them. On the
narrowest phones the tabs tighten so the row never scrolls sideways. See
[Settings](#settings).

The header and the tab row sit in the same centred column on every page, and
do not move when you change tab or open a book: only the book itself (its
text, the side panel, and the player bar) uses the whole width of the window.
The short status line under the tabs ("Added …", "Deleted …") stays in that
column too.

## Create an audiobook

**Create** walks through three steps, shown as rows on one page, one open at a
time: **Add the source document**, **Choose a voice**, and **How Hilde prepares
the text**. A step's colour says where you are: the open step is yellow, with
a yellow border and a filled yellow circle with its number. A finished step is
green and folds into one row: a check in a green circle and its title in
green, then what you chose (the file name and its type, or the voice), then
**Change** (a pencil and the word, in blue). On a phone, **Change** sits under
what you chose. A step not reached yet is gray, and so is step 3 while you
have gone back to an earlier step. A long file name is cut short with "…".

**Change**, or a click on a finished step's title, opens that step again;
nothing in the other steps is cleared. **Continue** in a reopened step goes
straight back to the furthest step you reached, with every choice kept: after
**Change** next to the document in step 3, picking another document and
pressing **Continue** returns to step 3 with the voice and text settings you
chose.

The first step offers three ways to choose a document, as three cards side by
side (stacked on a phone). A radio circle in each card's header says which one
is used: exactly one is chosen, with a filled dot and a blue border. The other
two are dimmed, with gray text and their fields switched off, but they keep
what you entered. Click anywhere on a dimmed card to choose it; its first field
gets the focus. With the keyboard, the arrow keys move between the three
choices. The first time, **Upload a file** is chosen; after that, this browser
remembers the card you used last.

- **Saved document**: one you added before. The field opens the list of your
  saved documents, newest first, each with the date it was added and its own
  trash button on the right (gray; red under the pointer). Once you have chosen
  one, a trash button after the field deletes it. Either asks first: 'Delete
  "X.pdf"?', "It is removed from your saved documents. Audiobooks already made
  from it stay in your library." Delete removes only that saved file, never an
  audiobook, a voice, a note, or a conversation, and a job already queued keeps
  its own copy. With the keyboard: Enter, Space, or the down arrow on the field
  opens the list; the up and down arrows move through it, Home and End go to
  the first and last document, Enter chooses one, and Escape closes the list
  and returns to the field. Tab goes from a document to its trash button, and
  then on out of the list. Delete and Backspace do nothing in the list: only a
  trash button deletes, after its question. In the question, Tab stays inside
  it, Escape is **Cancel**, and the focus goes back to the field (or to the
  trash button after it, if you used that one).
- **From a URL**: a direct HTTP(S) link to a PDF (or Markdown or text file) or
  an arXiv page, downloaded when you press **Continue**. **Save as** is
  optional: the server works out the document type and adds the matching
  extension when needed, including for PDF links without one, such as arXiv
  `/pdf/<id>` links. An arXiv link (on `arxiv.org` or `www.arxiv.org`, its
  abstract page `/abs/<id>` or its PDF `/pdf/<id>`, new ids such as
  `2401.12345v2` or old ones such as `cs/0112017`) downloads the paper's PDF,
  saved under its id (`1706.03762.pdf`), and the step then shows the paper's
  title (from arXiv, else from the PDF, else the file name), with the saved
  file's name in its tooltip. If that download fails, the step says why:
  "Couldn't download this arXiv paper: arXiv has no paper at that link (404).
  Check the link or try again.", or that arXiv could not be reached, or did
  not send a PDF.
- **Upload a file**: drop a PDF on the dashed area, or **browse**; the card
  then names the uploaded file. Dropping a file anywhere on the step chooses
  this card and uploads it.

**Continue** uses only the chosen card, whatever the others hold: no link is
downloaded unless **From a URL** is chosen. Beside **Continue**, the step says
what that card holds: "Chosen (URL): https://…" (after an arXiv download, the
paper's title and type), "Chosen (Saved document): X.pdf · PDF", or "Chosen
(Upload): X.pdf · PDF", and "No document chosen yet"
before that. Pressed with the chosen card empty, **Continue** stays and says
"Choose a document first". Files are limited to 64 MiB. Supported document
types are PDF (`.pdf`), Markdown (`.md` or `.markdown`), and plain text (`.txt`
or `.text`), not HTML web pages. A problem, such as a link that is not a
document or a file type Hilde can't read, is said in a short sentence inside
the step. Adding a document under a name you already have asks first, naming
the file it would replace.

The voice step is a dropdown of your saved voices (the same choice as
**Select** on **Voices**), with a card that previews the one you choose and
shows its tags and the start of its description, as on **Voices**.

The third step, **How Hilde prepares the text**, starts with **Model for this
book**: the model all three options use. **Use different models per option**
shows a picker in the figure and checking options too; they start at **Same
as the text model (<model>)**, and each has its own Local or Cloud choice. Then
each option has its own card: a checkbox with its title, one line on what it
does, a smaller line on what it can change, and **More** with the longer
explanation.

A model picker is two dropdown boxes, each with its label above it: **Runs
on**, **Local** (open models on your own machines, a green **Local · <server
name>** badge with a chip) or **Cloud** (cloud providers, a purple **Cloud:
<Provider> · Subscription** or **Cloud: <Provider> · API key** badge with a
cloud), and **Model**, which lists only models of that kind; **Change** beside
them opens the model list. Switching Runs on sends nothing: it opens the list
of that kind, and closing it without a choice keeps the model you had.
Choosing Cloud while no provider is connected opens no empty list: the Model
box reads **Connect a provider** (in Settings, which only assigns models, it
opens the list with its **Connect** rows instead). Any connected cloud model takes three
clicks: Runs on, Cloud, the model. Each list, from the live model lists (only
a greyed row keeps a model of a local server that does not answer):

- The default first (marked **Default**), then the models this browser used
  recently, then the rest, each by a short name such as "Model 35B" with a
  **reads images** tag where it applies (a local model once it is found to
  read images, see [Which local models read images](configuration.md#which-local-models-read-images),
  a DeepSeek model when DeepSeek's model
  list says it takes images, every other cloud model); the full model code
  shows on hover. An image picker (the figure model in Create, **Default
  image narration** in Settings) lists only models known to read images
  (tested, reported by the server, or set by hand), with the muted line "Only
  models that can read images are listed." under its boxes; text-only models
  and models not checked yet are left out, and a remembered model that reads
  images stays as a greyed "server unreachable" row while its server is down.
  Its **Same as the text model** entry is there only while the text model
  reads images. When no model of that kind is known to read images, a muted
  line at the top of the list says so.
- Local: the models each local server under **Settings › Providers** lists,
  as "<model> · <server name>" with the server's address underneath. An
  Ollama model that is installed but not loaded is tagged **loads on first
  use** and can be chosen. While a server does not answer, the models it
  listed when it last answered stay in the list, greyed and not selectable,
  marked "server unreachable"; they can be chosen again once it answers and
  the list is fetched again (opening Ask Hilde, or reloading the page). A
  server that has never answered is one greyed row, "<server name> — server
  unreachable". A default model (**Settings › Models and connection**) that
  no server lists is a greyed row by its
  name, "not listed by any reachable server", with no server named. **Add
  local** is at the bottom. Create step 3, Ask Hilde, and Settings show the
  same list; a local model is never mixed up with a cloud provider of the
  same name (a local DeepSeek model is Local, the DeepSeek API is Cloud).
- Cloud: the connected providers' models; a provider not connected is a
  greyed row with **Connect** (Anthropic's covers Claude Code too).
- **Connect** opens the provider dialog of **Settings › Providers** on Cloud
  with that provider chosen, **Add local** opens it on Local, and **Connect a
  provider** and **No model available** open it too. In a browser that may
  not change providers, they open **Settings** at **Providers** instead.
- With more than ten models a search box at the top filters the list. Arrow
  keys, Home, and End move through it, Enter chooses, and Escape closes it and
  returns to its box.
- **Suggested for <job>**: above the list, when Hilde has ratings of the
  models for this job on this tab (**Local** or **Cloud**), a short section
  "★ Suggested for preparing the text" (or describing figures, checking the
  text, answering questions) lists at most three of them, each with its short
  name, its tags (such as **reads images**), one line of reason, and one
  label: **Best for this job** (the highest score on Hilde's own checks),
  **Fastest** (the quickest that scores at least 95% of the best), or **Good
  balance** (only when a third model is clearly in between). When two scores
  are within what varies from run to run, the reason says "Similar accuracy"
  instead of claiming one is better. Only models you can use right now and
  that can do the job are suggested; for figures only models that read
  images. A click chooses the model like any row (in Settings, as there, it
  sets the default); nothing is chosen or changed by the suggestions
  themselves, and the suggested models stay in the full list below. Under the
  list a grey line says where the suggestions come from ("Suggestions come
  from Hilde's own checks on 5 papers, run on spark on 20 Oct.") with **How
  this works**, which opens a short explanation in place (Tab reaches it from
  the list). Without ratings for the job and tab there is no section at all,
  and without a ratings file the pickers are exactly as before (see
  [Suggested models](configuration.md#suggested-models)). Narration and voice
  design are set when Hilde starts and have no picker, so they show no
  suggestions.

Each picker, for text and for figures separately, preselects: the model this
browser used last, if it is reachable; on first use, the server's default
(**Default text narration**, and for figures **Default image narration**,
under **Settings › Models and connection**, see
[Default models](configuration.md#default-models)), if it is reachable, said
as "Default on this server"; with none set, the only model there is, with its
badge ("The only model available"); otherwise nothing ("Choose a model"), its
list opens once, and **Create audiobook** waits. When the last used or default
model does not answer, nothing is selected: the model row says in red "⚠
<name> is unreachable", **Create audiobook** names that reason, and under it
one-click alternatives follow, the other reachable models, local ones first,
each with its name, Local or Cloud badge, and size when known; nothing is used
until you click one. Ask Hilde does the same and never answers with another
model by itself. With a default image narration model set, Create shows the
per-option pickers and the figure model starts with it; otherwise figures
follow the text model. When one model serves all options and it can't read images,
the figure option says so, and blocks Create only while it is ticked. With no
model at all the picker reads **No model available**, with **Add local** and
**Connect a provider**. **Create audiobook** stays disabled only while an
option you turned on has no usable model; with "Adapt the text for
listening" off no text model is needed. Beside the button it names the option
and why, such as "Figures: no cloud provider connected" or "Figures: the text model
can't read images", with one-click fixes: **Use the text model (Local)** (or
Cloud, where the text model runs; offered only when the text model can do
that option's work, for figures read images), **Use the default model**
for the text option itself, and **Turn off this option**. The summary says
"Not ready: …" for that option until then.

Cloud providers are connected under **Settings › Providers** (see
[Providers](#providers)); keys and sign-ins are kept on the server, for every
browser, and the page never receives a key or a token.

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
be made again. While **From a URL** is chosen and its link not yet
downloaded, the link is the next book, so that notice gives way to
**Continue**.

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

The progress view opens from the **Progress** tab or **View progress**, and by
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

A model that stops answering during a book (still refused after the retries)
pauses the job instead: the card reads **Paused**, with a red "⚠ <model>
became unreachable at part N", the parts kept and which model wrote them,
**Retry** (the same model, after the model list is asked again), and
**Continue with another model…**, which lists the other reachable models
(local ones first) and sends only the remaining parts to the one you
click; the parts already written stay. Hilde never switches models inside a
job by itself. The finished card and the book's details on **Listen** say in
one line which model wrote the text, by model with the number of parts and
why, such as "Text written by DeepSeek V4.1 Flash (98 parts) and GPT-6 Astra
(42 parts: figures, tables and equations)" or "(42 parts: continued after
DeepSeek V4.1 Flash became unreachable at part 25)", with every range under
**Which parts** (its write times on hover); the job's log says "Written by:
parts 1–12: …". Each job also keeps a model history with times, which goes
into the book; see [Model history](configuration.md#model-history).

The queue is global across browsers. Every web submission uses the
server-owned pool of [narration workers](configuration.md#narration-workers): one job claims
all currently idle compatible workers, and each worker dynamically pulls chunk
batches from that audiobook. A second job waits when the first has claimed the
whole pool. The progress view and **Settings** show each worker's device and
live state. GPUs of other machines appear as `Machine 1 · GPU 2` and so on.
Speech-server URLs and filesystem paths are not published; the nodes' hosts
and paths are shown only to a browser on the server's own machine (to any
browser with `--manage-from-network`). The
server's own short host name is shown to every browser.

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
button at its top right (on a phone too; blue text and books icon in a thin
blue outline, like **Change** on Create) that goes back to the list in one
press, as does the **Listen** tab pressed while a book is open (from another
tab, **Listen** brings back the book you had open). A book read by more than one voice shows a
voice picker in that line. Above the text, and staying on screen while it
scrolls (on a phone, it scrolls away with the text), are **Follow along**,
the **Narration text | Source text** switch, and **⋯** (More). The player is
one bar docked at the bottom of the screen, on a computer and on a phone alike,
and stays there while you scroll; its background spans the width under a thin
line. It has two rows: the thin seek bar, with the elapsed time at its left end
and the total at its right end; then the round blue play/pause button and the
speed, a small outlined pill, on the left (it shows, for example, **1.0×** or
**1.25×**; click it to choose 0.75× to 2×), and **Ask Hilde** on the right: a small teal button (teal
is Ask Hilde's own colour) with Hilde's sparkle and the words **Ask Hilde**. On
a computer both rows sit in one centred column at most 560 px wide; on a phone
they run the full width, 16 px from the screen's edges, with a larger
play/pause. The bar keeps clear of the phone's home indicator. The bar never
covers the text: the page leaves room for it below the last paragraph. The
text runs in a comfortable reading column; on a wide screen the panel for the
chat, a figure, or a source page opens beside it, and the player bar then
stays under the text. The panel starts just under the lowest bar at the top of
the window: the tab row while you can see it, the slim paper bar (below) once
it shows, otherwise the top of the window. It never covers the header, the
tabs, **Settings**, or the paper bar, and its top follows them as you scroll.

Once the book's header has scrolled away, a slim bar stays at the top of the
window: the title on one line (cut with "…" when it is long), the voice and
length in grey (for example "Mimir · 44 min"), and **Library** in exactly the
place it has in the header. It is hidden while the header shows, so the title
never appears twice, and it lies over the text without moving it. Click the
title to go back to the top of the paper. If the keyboard focus is on
**Library** as the header scrolls away, it moves to the bar's **Library**, and
back when the header returns. On a phone (under 600 px wide) the bar holds only
**Library** and the title; it sits below the browser's own bar, and text the
page brings into view (a seek, **Follow along**, a link) lands below it.

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
line at its left and a faint blue tint, the sentence being read by a stronger
blue tint (it stays while paused, so it is easy to tap to pause and resume),
and the word being read by a 2 px blue
underline and slightly brighter text; with **Highlight style B** (Settings,
**Reading**; a test for now) the word also gets a faint blue fill. Text you
select is marked in neutral gray. Sentence-era readers created before paragraph
grouping was recorded still show one sentence per paragraph; paragraph-era
readers regain their paragraphs automatically.

A tap on a word (a click on a computer) outside the sentence being read plays
the book from that word, like rewinding to it; a tap anywhere in the sentence
being read pauses it, and that sentence stays marked, so the next tap in it
goes on from exactly where it stopped. Nothing opens and nothing is asked, and
the book answers at once, without waiting. Only a short, still tap counts:
scrolling, dragging, selecting, and holding your finger down never start,
stop, or move the book, nor does the second tap of a double tap, nor a tap
that only clears a selection. A double tap never asks anything, and it does
not zoom the text (pinch to zoom instead). To ask about text, select it.

- On a phone or tablet, long-press a word (its handles stretch the selection
  to a phrase or a sentence). A teal **Ask Hilde** button appears just above
  the player bar, away from your phone's own Copy menu, and stays while the
  text is selected; tap it. It goes away when the selection is cleared.
- On a computer, drag over any span or double-click a word. Hilde's **Ask
  about this** button appears next to the selection; press it. A laptop with
  a touch screen shows the button for the way you selected: **Ask about
  this** for the mouse, **Ask Hilde** above the player bar for a finger.

Nothing opens until you press that button (or **Ask Hilde** in the
player): then the book pauses and the chat opens (on a phone, as a sheet from
the bottom) with a card headed **Selected text**: your words in quotes, and under
**Context details** the section they are in, all of your selection, and the
sentence around it as context. The card's **Word | Sentence | Paragraph**
switch widens what you ask about, and the book marks it in gray; **Listen from
here** on the card plays from the start of it. A question you had already
typed stays in the box; otherwise the box is empty and the button reads
**Explain this part**.

One question can bring at most 8,000 characters of selected text from at most
6 paragraphs. A longer selection is never cut short: its card says how long
it is and what fits (for example "This selection is 8,001 characters over 9
paragraphs; Hilde can attach at most 8,000 characters from 6 paragraphs.
Select less."), shows the whole selection under its details, and attaches
nothing until you select less (then **Change**) or remove it with **×**.

Nothing else you do in the view moves the audio or your place: closing the chat,
switching between Narration and Source text, opening a figure or a
page, going to another tab and back. **Follow along** turns the page when the
sentence being read leaves the screen. If you scroll the word being read out
of sight yourself, it stops following and a small **↑ Back to reading
position** pill appears just above the player bar (↓ when the word is below);
it hides while the word is on screen, and while you have text selected in the
book (on a phone, the selection's **Ask Hilde** takes its place).
Clicking it brings the word back on screen and turns **Follow along**
on again. The pill appears too, once the word is out of sight, after you open
a figure or a page, follow a ¶ link, or click the paper bar's title. With
**Follow along** off, the page never scrolls by itself.

A figure shows on a light card with its caption under **From the paper**. On
a wide screen it shows as printed in the PDF: its part of the page, with the
titles, labels and legend printed around its panels, without the caption or
the text around it; on a phone the panels of a figure of several stack, each
its own part of the page with its title, named by its place, such as "Left
panel of the source figure". Click a figure to see it large beside the text
with its caption, a link to its PDF page, **Hilde's description**, and **Ask
about this figure**.

**Ask Hilde** opens the chat beside the book (on a phone or a window narrower
than 900 px, as a sheet rising from the bottom over about 60% of the screen,
with a grabber and rounded top, the book dimmed above it: **×** or a tap on the
book closes it; with the keyboard open it grows so **Send** stays in view). From
900 px it is a panel at the right, 360 px wide in a window under 1100 px so the
text keeps about 50 characters a line beside it. Its header is Hilde's sparkle
and **Ask Hilde** in teal, then the chat's model box, **⋯**, and **×** at the
right; in a panel under 420 px wide (a phone, a narrow window) the model box has
a line of its own under them.
It pauses the book, as **Ask about this** or the phone's **Ask Hilde** on
selected words and **Ask about this figure** do; the book waits at that sentence until you press play in the
player bar (the chat has no play button for the book); closing the chat never
starts it. Ask
about the book, or ask Hilde to write a Markdown file, such as a summary or
notes on a section. Hilde sees a one-line summary of every paragraph and reads
the paragraphs it needs before answering. For a long book on a model with a
small memory, it sees one line per section instead and searches the book for
the paragraphs it needs; a question that still doesn't fit says so plainly. A
paragraph it cites, such as ¶12,
is a link that scrolls the book to it. When the server has web search (see
[Web search for Chat](configuration.md#web-search-for-chat)), Hilde can also
search the web and read pages, for background or later work, and links what
it used; those links open in a new tab, and the chat's **⋯** menu says **Web
search on**. Its answer streams in as it writes. The model is one of yours:
pick it in the model box in the chat's header, with the same model picker as on
**Create** (see above), in a compact form (a **Runs on** box showing Local or
Cloud, and the **Model** box with the model's name and badge; the model box
opens the list); a local model answers from the server that offers it. The
badge shows **Local** or **Cloud**, so you see where your question goes before
you send it (screen readers hear the server or the provider and how it is
reached too, as the list shows them), and the picker remembers the model you
used last; it never starts on a cloud model by itself. A model that does not
answer shows in red under the header, with others to use instead.
Changing the model while Hilde is still writing an answer stops that answer,
as **Stop** does: what had arrived stays, marked **Stopped**, and nothing more
of it appears. Your unsent question, its card, and the conversation stay, and
your next question goes to the new model at once. Every question is a new
request, also about the same words as before. Under each answer a small gray
label names the model that wrote it (such as "GPT-5.6 Sol"), as it was when
the question went, whatever the model box shows later; answers from before
this version have none. If the model can't answer (you are not signed in, its
server is not running, the model is not loaded, or the connection fails), the
chat says so in a red box under your question with what to do and a
**Providers…** link; Hilde never switches to another model by itself.
**⋯** next to the model box opens the chat's menu: the book and the section
asked about, as a line of text, then **Read answers aloud** (a tick shows it is
on; it stays open so you see the change) and **New conversation**. Enter,
Space, or the down arrow opens it from the keyboard; the arrows, Home and End
move; Escape closes it.
With no model at all, the chat asks you to add one first, with a **Providers…**
link that opens **Settings** at **Providers**. ChatGPT, the Anthropic, OpenAI, Google Gemini, and
DeepSeek API keys, and local models can chat; Claude Code models cannot:
Hilde says so when you send with one. The conversation is kept with the book,
one per book, for everyone using this server, and survives a refresh. **New
conversation** starts over. Your unsent question, what is attached to it, and
the conversation stay as they were when you look at a figure or a page, or
close the chat and open it again; an answer keeps arriving meanwhile. When a
conversation grows long, its oldest messages and reads leave what Hilde
remembers, and the chat says so.

Ask Hilde attaches the part being read (or the words you selected) as a card
above the message box, in teal, Ask Hilde's colour (as are the cards of past
questions, and the card while it is prepared or when it couldn't be attached):
headed **Selected text** for a selection, else the
section heading as in the book, below it your words in quotes or the part's
first words, **Change** to attach what
you have selected, or else what is playing now, and **×** to remove it. It
stays as attached while you write, even if you play the book again. Its
collapsed **Context details** list exactly what goes with
your question, grouped by where it comes from, each with its ¶ number and
page: "Your selection", "The sentence around it" (for less than a sentence),
"Hilde's narration" of those paragraphs and, where the adaptation changed
them, "From the paper". A selection brings only the
paragraphs it takes words from, never their neighbours. A question about a figure or table brings its caption, every
paragraph of the paper that names it by number ("Figure 2", "Fig. 2",
"Figures 1 and 2"; never one only printed near it), and "Hilde's description".
Its picture, laid out as in the PDF, goes too when the chosen model takes
pictures: ChatGPT and the Anthropic, OpenAI, and Gemini keys do, a DeepSeek model when DeepSeek's model list says
it takes images, a local model only when it is known to read images; otherwise the details say the
picture is not sent. The picture goes with that question only. Past questions
show the same card, collapsed and without its buttons.

The attached part sits directly above the message box and lights up briefly
when attached, also when you ask about the same words again (a steady outline
instead when your system asks for reduced motion). It reads **Preparing…** while Hilde gathers it; **Send** then reads
**Preparing context…**, and pressing it (or Enter) sends your question as soon
as the part is ready. If gathering takes longer than 10 seconds or fails, the
part reads **Couldn't attach: Retry · Remove**: **Retry** asks for the same
part again, **Remove** drops it. A question sent then goes without it, and the
chat says **Sent without the selected text**. The question box is one bordered
field with a small outlined button inside it on the right. The box stays empty
for your own question; its hint says what Enter does, for example "Type a
question, or press Enter to explain the selection" (or the figure, or this
part), and with nothing attached "Type a question". With a part attached and
nothing typed, the button reads **Explain this part** and it, or Enter, sends
"Explain this part."; once you type it reads **Send**.
Your question stays in the box until Hilde has it: if
sending fails, the chat says why above the box and the text is still there.
The chat also says why when nothing can go yet: an empty box, models still
loading or none added, or Hilde still answering. That last one shows in a teal
box, naming the model still writing: "Hilde is still answering with … Your
question stays here: wait for the answer, or press Stop, then send it." If the
conversation can't be loaded again after an answer, the chat says so in red,
keeps trying by itself, and never blocks your next question meanwhile.

**🔈 Read aloud**, under each of Hilde's answers, reads it aloud in Hilde's
own voice (or, when the Hilde voice is gone from **Voices**, the first voice
there by name); the answer stays plain text while it is read, kept in view.
While it is read, pause it with the chat's bottom button (below), which then
offers **Resume**; under a paused answer, **Start over** reads it from the
beginning. Listening to another answer keeps
this one's place too. **Read answers aloud**, in the chat's **⋯** menu,
reads each answer as soon as it is finished (not one you stopped). One sound
plays at a time: reading an answer pauses the book, and when it ends the book
stays paused; starting the book, or a voice preview, stops the others. The
first answer after a while takes a few seconds longer while the narration
model loads; it stays loaded for five minutes after the last answer read, then
frees its GPU. Code, web addresses, and Markdown are not read; a citation such
as ¶12 is read as "paragraph 12". Reading aloud needs the server's own
narration model (`--voice-clone-model`); without it, neither shows.

At the bottom of the chat, just above the question box, one teal button with a
short gray note beside it says what Hilde is doing and stays in place while
the conversation scrolls (on a phone too, at the bottom of the sheet): **■
Stop** with "Writing…" while an answer arrives, **❚❚ Pause** with "Reading
aloud" while Hilde reads an answer aloud, and **▶ Resume** with "Paused" once
you paused it. When Hilde is neither writing nor reading, it is gone and only
the question box stays. **Stop** ends the answer: the text that had arrived
stays in the conversation, marked **Stopped** under it (also after a reload).

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
away. Safari and Chrome both play this MP4, so clicking a word starts at
the word you chose; browsers without MP3-in-MP4 playback fall back to the plain MP3 and its
the background, with its progress under the book's title; once it is in, playback moves
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
the chat, never triggers them, and Space on a focused button presses that
button. The web shell is served with `Cache-Control:
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

**Voices** lists every saved voice, 50 rows at a time: a green play button,
the voice (two lines, below), **Modified** (the date its sample, transcript, or
description last changed, the exact time in its tooltip; hidden on a phone),
and **Select**. The selected voice comes first, on a faint green row, then
every other voice A–Z by name, ignoring upper and lower case, with letters
such as Ö, Ş, and Ü where your browser's language sorts them.

Each voice is two lines. The first is its name, then its tags as pink chips:
at most four on a wide screen and one on a phone, then "+N" for the rest. The
second is one line of plain gray text that ends in "…" when it is longer: the
voice's description, or, for a voice made from tags, its **More details**. A
click on that text (on a phone, a tap anywhere on the row) opens the row: the
whole saved description and every tag; another click folds it. A voice
without a description says "No description".

### Voice tags

Tags are short words that describe a voice: **warm**, **husky**,
**low-pitched**, **confident**, and so on. Hilde comes with about forty, in
groups:

| Group | Tags |
| --- | --- |
| Voice | female, male, young, adult, mature |
| Pitch | deep, low-pitched, mid-pitched, high-pitched |
| Texture | husky, smooth, breathy, raspy, gravelly, bright, resonant, crisp, soft |
| Pace | slow, measured, brisk |
| Manner | warm, calm, confident, gentle, playful, serious, authoritative, intimate, energetic, soothing, composed, expressive, conversational, matter-of-fact |
| Suited to | narrator, storyteller, lecturer, news reader, academic |

You can add your own (**Yours**). A tag is at most 32 characters, on one line,
without commas; a voice has at most 12.

The list shows only the tags you have confirmed. When Hilde has **suggested**
tags for a voice, its first line ends in one dashed pink pill instead, "4
suggested · Review" ("4 to review" on a phone, in place of the tags), and a
banner above the list says "Hilde suggested tags for **3 voices**. Nothing
changes until you confirm." (on a phone just the first sentence). **Later**
hides the banner until your next visit, which here means this browser tab: a
reload keeps it hidden, a new tab or a restarted browser shows it again. It is
gone for good once no suggestions are left.

A suggestion is a tag whose word appears in the description as a whole word
(or a few related words, such as "low pitch" or "middle register" for a pitch,
"warmth" for **warm**, "older" for **mature**, "narration" for **narrator**,
"research" for **academic**), but not in what the description rules out:
words after "without", "not", "never", "rather than", or "instead of" in the
same sentence, or a sentence that starts with "Avoid". No model is asked. A
tag of your own that the description uses is suggested too, also for a voice
you have already reviewed.

#### Review tags

**Review tags** (on the banner) or a voice's pill opens one voice at a time, in
a dialog (on a phone, a sheet that fills the screen): the play button, the
name, and "Voice 2 of 5". The pill starts at its voice, the banner at the
first waiting voice in the list; either goes on through the rest.

- The full description, read only, with the words Hilde matched highlighted.
- **Suggested by Hilde**: the suggested tags, each kept (filled, with a check).
  A click drops one (dashed); another click keeps it again.
- **Add your own**: **+ Add a tag** opens a box that finds tags from the list
  as you type, under small group headings, or adds the word you typed as a tag
  of your own (**Add "…" as a new tag**; it joins **Yours**). Arrow keys move,
  Enter picks, Escape closes the box. Next to it, two or three dashed ideas
  from the list: for **Suited to** and then **Manner**, the tags after the
  last one the voice keeps in that group (from the group's start when it keeps
  none), never one already in the dialog. A click adds one.
- **Skip this voice** leaves it waiting; **Save and next** keeps the voice's
  tags (its confirmed ones, the kept suggestions and the ones you added) and
  shows the next voice. After the last voice the dialog closes. Escape or ×
  closes it at any time, and the keyboard focus goes back where it was.

"Tags only help you find and describe voices. They never change how a voice
sounds." Saving writes only the tag file, never the voice's folder. A
suggestion you dropped is remembered, so Hilde does not suggest it again; add
it back with **+ Add a tag** if you change your mind.

Tags are kept in one file, `voice-tags.json` in the library folder beside
`Voices/` (readable only by you); a voice's own folder never changes when you
tag it. Renaming a voice carries its tags along, and deleting it removes them.

The play button plays the voice's preview in place: while it plays, the circle
is filled and "Playing preview" shows under the name, in place of the text.
Playing another stops
the first, and a preview never changes which voice is selected.

**Select** chooses the voice for your next audiobook: the same choice as the
voice step on **Create**. The chosen voice moves to the top and shows
**✓ Selected** instead, the one chosen before returns to its place, and a line
says it is selected, with **Back to Create**. **⋯** on a row holds **Edit
tags**, **Rename…**, and **Delete…**:

- **Delete** removes a voice after a confirmation naming it; audiobooks made
  with it keep their audio, and a job already queued keeps its own copy.
- **Rename** asks for the new name in a dialog and keeps everything else: its
  sample, transcript, description, and tags stay as they are, and the
  audiobooks it read show the new name too. It waits while an audiobook is
  being made with the voice, and refuses a name another voice already has.
- **Edit tags** opens the same dialog for that one voice: its tags as kept
  chips (a click drops one), any suggestions waiting under **Suggested by
  Hilde**, and **Add your own**. **Save tags** keeps them, **Cancel** changes
  nothing, and **Make a new voice from these tags** opens **New voice** with
  the same tags on.

Search (**Search voices by tag or description**) looks at descriptions and
confirmed tags: every word you type must begin a word of the description or
of a tag, in any order, ignoring case and accents, so `warm calm female` finds
voices that have all three. Tags only add to what the description alone finds;
a suggestion counts once you keep it. Chips whose words you typed are filled
and come first in their row, and under the list a count says how many voices
match ("1 of 9 voices"). A click on a chip adds its tag to the search; a click
on a filled chip takes it out again; **×** in the search box empties it. A
preview keeps playing while you search, and the selected voice stays selected
even when the search hides it. When none matches, the page says "No voice
matches all of: warm, british, zzqx." with **Clear search**.

A new library comes with eight stock voices, each with its description:
Balder, Bragi, Mimir, and Vidar (male) and Eir, Freyja, Idun, and Sigrun
(female).

### Making a new voice

**New voice** opens the editor: the tags (the same groups as above, and your
own), **More details** in your own words (optional), and **Name**. Under them,
**Sent to the voice model:** shows exactly what Generate preview sends, as you
change the tags or details. Hilde writes it as one or more sentences, each
part left out when no tag is on for it:

1. "A" (or "An" before a vowel) and the Manner tags, your own tags, the
   Texture tags, and the Pitch tags, separated by commas, then the age and
   gender tags and "voice": "A warm, husky, low-pitched adult male voice."
2. The Pace tags: "Reads at a measured pace."
3. The Suited to tags as narration, storytelling, lectures, news reading,
   academic texts: "Suited to narration and lectures."
4. More details, with line breaks and extra spaces made single spaces.

For example **warm**, **husky**, **low-pitched**, **adult**, **male**,
**measured**, **narrator** and the details "Reads like a late-night radio
host." give "A warm, husky, low-pitched adult male voice. Reads at a measured
pace. Suited to narration. Reads like a late-night radio host." At least one
tag or some details are needed.

**Generate preview** has VoiceDesign make a version, which plays as soon as
it is ready; **Stop** cancels it. Voices can be designed only while no
audiobook is being made. **Save voice** turns on once you have heard this
preview to its end or for at least 3 seconds, and only while the tags and
details are the ones previewed; change them and you generate a new preview
first. Save keeps exactly the version you heard, with the sent text as its
description, and then its tags and details.

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
saved have none; give them tags with **Edit** to make them searchable.

## Settings

**Settings**, the blue tab with the gear at the right end of the tabs, replaces the
page until you choose **Settings** again, **Close settings**, or a tab. Each
group of settings ends with one note on when it takes effect: "Applies to the
next audiobook" or "Applies now". The groups:

- **Reading**: **Highlight style**, A (the word being read underlined) or B
  (underlined, with a faint fill behind it), applies now and is remembered by
  this browser. It is a temporary test: one style will stay and the choice
  will go.
- **Speech tuning**: precision (**auto (recommended)**), attention (**sdpa
  (recommended)**), language, and an optional seed. They change speed, memory
  use, and small details of the sound, never the voice. They apply to the next
  audiobook and the next voice preview you generate under **New voice**; saved
  voices' previews and answers read aloud always use the defaults. With a
  speech server narrating they apply only to the previews you generate, and
  with speech servers narrating and designing they are turned off.
- **Narration**: text encoding, [**Batch size**](configuration.md#batch-size)
  (off with a speech server), **Chunking** (the longest chunk in characters;
  when you give an existing book a new voice, the book keeps its own
  chunking), and **MP3 compression**, for the next audiobook.
- **Text preparation**: **Parallel text requests** (how many requests adapt
  the text at once) and **Paragraphs per request**; see
  [Text adaptation](text-adaptation.md).
- **Voices**: **Reference WAV encoding** (FLOAT unless changed), under the
  collapsed **Advanced voice settings**: the format of the reference recording
  saved with a new voice, which local voice cloning reads.
- **Machines and devices**: only what the server reports. **This server** is
  the machine Hilde runs on, by its short host name, with every device its
  probe found: a CUDA GPU by its name, memory, and a stable ID (the start of
  its UUID, such as `GPU-7a85a208`, which stays when GPUs are added or
  hidden), an Apple GPU (MPS), or the CPU, each saying whether it is a
  narration worker and whether that worker is idle, narrating, held while a
  voice is designed, or off, excluded in settings. On the server's own machine
  each narration worker has a box: untick it and it takes no book until ticked
  again (applies now). **Other machines** lists the nodes that narrate over
  SSH, each with its devices as `workers.yaml` names them (Hilde does not know
  their model or ID) and, on the server's own machine, **Remove** (after a
  confirmation naming it) and **Add narration workers**: enter its address,
  **Connect**, **Set up** if it lacks Hilde's Python or the speech model,
  untick any GPU to leave alone, **Add node**; see
  [Narration workers on other machines](ssh-workers.md). Changes apply now; a
  running audiobook keeps its narration workers. With none: "No other
  machines connected." Each machine lists each job on its own: **Text
  preparation**, **Narration**, and **Voice design**, each "available", "not
  available" (with the reason), or "not tested". This server can prepare
  text when the model list has a model it can reach (the model itself runs
  on its own server), narrates while one of its narration workers is on, and
  designs voices with a local voice-design model; other machines only
  narrate.
- <a id="providers"></a>**Providers**: one row per local model server (its
  name, a **Local** badge, its address, "set at
  startup" for one given with `--local-server`, and whether it is reachable)
  and per cloud connection, such as "OpenAI · Subscription — Signed in as
  <e-mail>", "OpenAI · API key — Key saved", or "Anthropic · Subscription —
  Claude Code: <its status>". Each row has **Edit** and **Delete**; Delete asks
  on the page first ("Remove 'Workstation'?", "Sign out of OpenAI
  (Subscription)?", "Remove the Google API key?"). A server set at startup can
  only be renamed. **Add** at the end opens the provider dialog: **Local** or
  **Cloud** (fixed when editing). Local takes **Name** and **Address**, then
  **Check and add** (or **Save**), which asks the address what it is (Ollama
  or OpenAI-compatible) and keeps it only if it lists models. Edit of a local
  server also lists its models (live, or as last listed while it is down),
  each with what was found out about images ("reads images (tested)", "reads
  images (reported by the server)", "text only (tested)", "not checked yet",
  "checking…") and **Reads images: auto / yes / no**; **Check again** checks
  all of its models anew. Hilde sends each local model one small test picture
  by itself unless its server reports it; see
  [Which local models read images](configuration.md#which-local-models-read-images). Cloud takes the provider (OpenAI, Anthropic, Google,
  DeepSeek, or xAI) and an **API key** (a password field, never shown again;
  afterwards "Key saved") with **Save key**, or **Subscription**, offered only
  for OpenAI (ChatGPT sign-in), Anthropic (Claude Code on the server: turns it
  on and shows its status), and xAI (SuperGrok or X Premium+ sign-in); see
  [Text adaptation](text-adaptation.md) for each. Applies now. On other
  devices, unless Hilde started with `--manage-from-network`, the rows are
  read-only, without Add, Edit, or Delete, under the note "Read-only from this
  device. Manage providers from a browser on the Hilde server, or start Hilde
  with --manage-from-network."
- **Models and connection** only assigns models to their jobs; adding servers
  and connecting providers is under **Providers**, so this section has no
  **Add local**, **Connect a provider**, or **Providers…** link. **Default
  TTS** (the narration model) and **Voice design** show their models by
  readable name (such as `Qwen3-TTS 1.7B (Base)`; the model ID is in the
  tooltip), with **Local** or **Speech server**. Voice design adds the device
  its last design ran on, by name and ID, or "Device chosen on first use"
  before the first; with several GPUs, each design takes the one with the
  most free memory. Hilde sets these when it starts; to change them, restart
  Hilde with different settings. **Default text narration** (the model that
  prepares and adapts the text) and **Default image narration** (the model
  that reads figures) are the server's defaults for every browser, set with
  the same picker as Create; see
  [Default models](configuration.md#default-models). Image narration lists
  only models known to read images, as the line under it says ("Only models
  that can read images are listed."). Each row shows the chosen model's name and
  badge, "Not set" when unset (with no image default, figures in Create
  follow the text model), and, when its server is down, the red "⚠ <name> is
  unreachable" with one-click alternatives that set another default. Their
  lists end with **Add local** and **Connect**, which open the provider
  dialog; with Cloud chosen and no provider connected, a Model box opens that
  list instead of reading **Connect a provider**. On other devices, unless
  Hilde started with `--manage-from-network`, both rows are read-only, in the
  look of the speech models' rows, under "Read-only from this device. Set the
  default models from a browser on the Hilde server, or start Hilde with
  --manage-from-network." A browser's own model is chosen in Create or Ask
  Hilde and remembered there. **Connection to Hilde**
  says "local (this computer)" on this computer, "HTTPS" over HTTPS, or, over
  plain HTTP from another device, that browsers may warn about or block
  downloads such as audiobook exports, with a link to
  [HTTPS](../README.md#https); when the server runs with `--https`, it offers
  **Download Hilde's certificate** to install once on this device.

Only a browser on the server's own machine sees the nodes' hosts and may change
the devices and machines, unless Hilde started with `--manage-from-network`
(see [Configuration](configuration.md#manage-from-other-devices)); elsewhere
one note at the top of **Machines and devices** says "Read-only from this
device. Manage machines from a browser on the Hilde server, or start Hilde with
--manage-from-network.", and other machines appear as `Machine 1` and so on.
Browsers cannot select, pin, or name devices.

## Asking before risky actions

Deleting a voice, a book, or a document, renaming a voice, recreating or
overwriting a book, replacing a voice's recording, saving a voice or adding a
document over an existing name, stopping a book, removing a machine, and
starting a new conversation each ask first in a dialog on the page that names
what is affected. Previewing, opening, and playing never ask.

## Browser state

Editable form settings are stored in bounded `HttpOnly; SameSite=Strict`
cookies per browser. Local model servers and cloud providers are the
server's, under **Settings › Providers**, the same for every browser. This
browser alone also remembers, in its local storage,
each book's place and speed, the side panel's width, the highlight style, and which stopped or
failed jobs you dismissed. Model paths, model IDs, speech-server endpoints,
worker devices/hosts, credentials, storage paths, and output paths remain
server-owned; only a browser on the server's machine (any browser with
`--manage-from-network`) may add or remove the nodes in `workers.yaml`.
Public API responses name local devices and models but omit hostnames, SSH
targets, speech-server URLs, and paths; only `/api/workers`, which answers a
browser that may manage machines alone, names the nodes' hosts and paths.
**AirDrop…** appears
only when the server runs on macOS with `pyobjc-framework-Cocoa`; other
clients use **Download MP3**.

When Hilde's server stops answering, for example while the machine restarts,
the page says so at the top and turns off everything that needs the server:
creating, saving, deleting, settings, and Chat. The tabs, the player, and the
text stay, so a book already loaded in this browser plays on. The page checks
every two seconds and, once the server answers, reloads its state and turns
everything back on by itself; there is no need to refresh.
