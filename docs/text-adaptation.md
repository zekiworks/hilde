# Text adaptation

**Adapt the text for listening**, in the **Create audiobook** step, rewrites a
document for someone listening, who cannot skim or glance back. The author's
prose stays word for word. Whatever would be a chore to hear is left out:
tables of contents, lists of sections, page numbers, citation
marks, and the bibliography. Whatever the ear cannot hold is tuned down to its
point: tables, formulas, long lists, and runs of numbers. A table of contents
or bibliography under its own heading is removed before the model sees the
text, even without adaptation and even when the PDF runs the heading into the
paragraph before it. Inline attributions and the sections after the
bibliography, such as appendices, remain.
While adaptation is selected, **Model**, **Providers**, and **Add local**
appear right under it, and **Workers** under **Advanced** in **Text
adaptation**:

- **Model** picks the model that rewrites the text. Left on Default, a job uses
  your local server's first model once you add one under **Add local**; if that
  server does not answer, the job stops rather than send your document to a
  cloud provider. Without a local server, Default is OpenAI's first model once
  signed in, then Claude Code's, then the Anthropic API's, the OpenAI API's,
  and the Google Gemini API's. A cloud provider that is busy or has a passing
  error is asked again, up to four times.
- **Providers** connects cloud models.
  - **OpenAI** signs this server in with a ChatGPT account: open the sign-in
    page it shows and enter the code, which **Copy** puts on the clipboard (or
    one click selects); the sign-in renews itself. Delete
    `~/.hilde/openai.json` to sign out.
  - **Claude Code** uses a Claude Pro or Max subscription through your own
    [Claude Code](https://code.claude.com/): install it on the machine running
    Hilde and sign in by running `claude` once in a terminal. Hilde then runs
    `claude` for each passage, with its tools turned off and Hilde's
    instructions in place of Claude Code's, and offers `sonnet`, `opus`, and
    `haiku`. Hilde never sees the sign-in, which Anthropic permits only
    inside its own apps, and passages count against your plan's usage limits;
    a limit reached ends the job with Claude Code's message. **Check again**
    looks for Claude Code after you install it or sign in. Connecting any
    provider updates **Create** at once: a "needs a model" message goes away
    without choosing another model.
  - **Anthropic API** takes an API key from the
    [Claude Console](https://platform.claude.com/), billed per use. A Claude
    subscription works only through Claude Code, above, as Anthropic's terms
    require.
  - **OpenAI API** takes an API key from the
    [OpenAI platform](https://platform.openai.com/api-keys), billed per use,
    and lists the key's text models, newest first. It is separate from the
    ChatGPT sign-in: its models appear as `openai/…`, the sign-in's as
    `openai-codex/…`.
  - **Google Gemini API** takes an API key from
    [Google AI Studio](https://aistudio.google.com/apikey), billed per use,
    and lists the key's Gemini text models, newest first.

  Each API key is checked with its provider before it is kept; **Remove**
  deletes it. The server keeps the ChatGPT sign-in in `~/.hilde/openai.json`
  and the keys in `~/.hilde/anthropic.json`, `~/.hilde/openai-api.json`, and
  `~/.hilde/gemini.json`, readable only by the user running it.
- **Add local** connects a model server on your network. Choose its type,
  **Ollama** or **OpenAI-compatible** (SGLang, vLLM, LM Studio), enter its
  `host:port`, for example `127.0.0.1:8010`, then choose one of its models.
  Tick **This model sees images** when the model accepts images, as Gemma 4
  does; figures and equations then go to it as images. Removing the server
  clears the tick, so check it when you add the server again: from labels
  alone, Gemma 4 misread chart values and trends that it read right from
  the image.
- **Workers** (default 4, at most 32) is how many paragraph batches are adapted
  at once, and **Paragraphs per worker** (default 1, at most 32) is how many
  paragraphs each batch holds. A figure or table is never split: its image,
  the labels read from inside it, and its caption always go to the model
  together, so it is described once. A figure or table with a caption is
  always a passage of its own in the book's text.
- **Check and correct descriptions** (off unless ticked) checks each description
  of a figure, table, or equation against the paper. One with a problem, such
  as a value given to the wrong method or a condition turned around, is written
  again, and the new description replaces the first only if a second check
  finds the problem fixed and nothing new wrong. **Checking model** is the
  model that checks; it is the text model unless you choose another. The
  checking model gets the figure as an image when it is a cloud model or a
  local one marked as seeing images. Checking takes longer: up to three more
  requests per figure, table, or equation. The job log names every problem a
  check found and whether the correction was kept, and the book records the
  checking model and the counts. A check can be wrong too: on one test paper
  it fixed 16 of 21 wrong descriptions and made 3 of 93 right ones wrong, by
  misreading where a plot's points sit. On another, a model checking its own
  chart descriptions fixed none of the wrong ones, which is why it is off by
  default.

Unless **Interpret and narrate figures/charts** is ticked, charts are described
from text, never from the picture. Before describing a figure, the model is
asked whether it is a chart: a plot, bar chart, or anything else that shows
values by position, length, or color. A chart's description says what it
plots, from its caption and labels, then what the author's text says it shows,
with the author's numbers. It never gives a value, a trend, or a comparison
that only the picture shows, because models often misread plots: tick marks
taken for results, two series swapped, a falling line called rising. Every
number in a chart's description must be printed in its caption or in the
author's text about it. A description that says another is asked for again,
and a sentence that still does is left out; the job log names both. A chart
the paper says nothing about is described by what it plots alone. Diagrams,
illustrations, and tables are described as before.

**Interpret and narrate figures/charts (Experimental)**, off unless ticked,
reads every figure, chart, and table from its picture instead, with no chart
rule, and **Figure model** picks the model that does it: the text model unless
you choose another. A strong vision model reads plots far better than a small
local one: on one test paper, GPT Astra read from the pictures got 2 or 3
checked values wrong per run, as few as Gemma 4 describing charts from text,
and kept about 20 more right ones. With a cloud figure model and a local text
model, the figures, tables, and equations go to that provider, with their
captions, the text read from them, and the author's text about each; the rest
of the paper stays on your network. The book records the figure model.

Figures reach OpenAI, Claude, and Gemini models as images, and a local model too once
**This model sees images** is ticked. Otherwise a local model receives the text
extracted from each figure instead. An equation the PDF lays out as a picture
goes along with its printed text, so even a model that reads no images knows
what it says. Its description opens with the number printed beside it,
"Equation 3" for one printed with "(3)", and "the equation" for one the paper
does not number; Hilde sets that name itself, whatever the model called it,
including "Figure 4", and replaces an equation number the paper never prints,
also when the equation is read inside a sentence. A figure or table described
on its own is named by its caption the same way: "Figure 6 shows…", whatever
name the model opened with, and a figure or table number the paper never
prints is replaced too. The job log names any other figure, table, or equation
number in a description, which may be a slip or a real reference. A picture
without a caption is an equation only when its printed text is math; a chart
stays a figure.

Each description of a figure, a table, or an equation opens with a spoken cue
such as "Figure 2 shows…" or "The equation says…", so a listener hears where
the author's text stops. The job log names any description that doesn't.

A figure, table, or equation reaches the model on its own, with its caption,
labels or cells, and picture, and without the model's running summary of the
paper so far. That summary differs from run to run, so with it the same figure
was described differently every time. It does get what the author writes about
it: the paragraphs that mention it ("In Table 3 rows (B)…"), and for an
equation the sentences around it, for their meaning only and never read again
in the description. That is how a table description knows which column a
paragraph is about, and which way a score is better.

An equation keeps its powers and indices: "warmup_steps to the power of −1.5"
reaches the model with the power on warmup_steps alone, never run into the
product before it, and "d k" in a sentence reaches it as d with the index k.

With each passage the model also writes a one-line summary and up to six tags,
which Hilde keeps with the book. They are not read aloud: **Chat with Hilde**
uses them to find the paragraphs a question is about.

In a PDF, a sentence that a page break, a figure, or a footnote splits is
joined back together before the model sees it; what split it then follows the
sentence. An equation printed as a picture stays inside its sentence and goes
to the model with it, so the sentence is read through, the equation in words as
part of it ("the total utility is the expected value of…"), without "Equation 2
says" breaking in. A listing set in a
typewriter font, such as a program, a prompt, or a skill file, reaches the
model as one block from the page's own lines, even across a page break or a
table printed inside it, so a sentence wrapped from one line to the next is
read through rather than split into two passages. A caption is recognized
as "Table 6: …", "Figure 2. …", or with its label in bold and nothing after
the number ("**Figure 6** CLMs outperform…"), also when the PDF stores it as
ordinary text, and a figure or table with a caption on each side takes the
one that names it. A picture no bigger than a letter or two, such as a logo
in a line of text, is not a figure and is left out. A figure
or table printed before the text that first mentions it ("as Figure 3
shows…"), on the same page or the next, moves after that text, so its
description never comes before the author introduces it or in the middle of
the argument. A footnote moves to the paragraph that cites it and goes to the
model with that paragraph, its mark moved to the end of the sentence carrying
it, so the note is read after that sentence rather than splitting it, saying
whom or what it is about ("Aidan Gomez did this work while at Google Brain").
A note under a table, marked with a letter ("a"), moves with its table. A
prompt the paper prints, such as instructions written for a model, is read
word for word. A command is read with its exact name and the names of its
options, without dashes or brackets ("sync, with the options source and
target"), and never with a name borrowed from the text around it. Program code
is still described rather than read. A note marked beside one author alone reaches the model with
that author's name, so it is never read as a bare "Work performed while at
Google Brain". A note about one author comes before a note every author line
shares, so "Illia Polosukhin did this work while at Google Research" follows
his name rather than the whole contribution note. A PDF table goes to the model
as its picture and the cells read from it; the model says what the table shows,
and the cells are never read aloud. The job log counts the sentences it
rejoined and the figures, tables, and footnotes it moved.

A few more rules keep the narration exact. Math is said as it is wherever it
is spoken, in the author's sentences as in a description: "divided by the
square root of", never "scaled by", and "one over the square root of d k",
never "the square root of d k". Nested operations are said as steps, innermost
first: "multiply the queries by the transposed keys, divide the result by the
square root of d k, apply a softmax, then multiply by the values". A
description of a table names every row or model the author's text discusses,
with its key values, and says which metric each comparison is about and which
direction is better; it pairs each model's score with that model's own cost,
and keeps every caveat its
caption gives about how values were measured. Big-O notation is a growth rate:
O(1) is "constant", never "one operation". Every statement of a footnote
stays ("Equal contribution. Listing order is random."), and so does every
quantity in the author's prose, in the author's own voice ("we"). A figure
description says only what the caption states or the image plainly shows; for a
figure dense with lines, such as an attention map, it describes the pattern
rather than naming which words a line joins. A
link the text depends on becomes where in words ("in the tensor2tensor
repository on GitHub"). An acronym is expanded only where the author spells it
out, once; the rest, such as GPU, stay acronyms.

A citation by number that the sentence needs, as in "similar to [30]", is read
as whom the paper's reference list names, "similar to Press and Wolf", so the
narration never guesses an author. Several works cited together are named by
their first authors, "such as Kalchbrenner and Gehring", and one the list lacks
becomes "earlier work". A citation in passing, as in "networks [13]", is left
out. Afterwards the job log names what a passage states that its source does
not: an author of the paper or of its references, or a name it credits work
to, that its own text never mentions, and a number the source does not print
(a rounded one is fine), in a description or in the author's prose. For prose
it also names each word the narration changed, such as "readers → listeners",
a symbol that lost its mark ("ŷ → y"), and a "not" or "all" dropped or added,
leaving out math read aloud ("does not equal"). A passage that states a number
or name its source doesn't print, says "orders of magnitude" where the source
doesn't, says math in an order that can be heard two ways ("the product of a
and b squared"), leaves out a number its figure's or table's caption gives,
or drops or adds a "not" or an "all", is sent back to the model once, naming
what was found; the better answer is kept, and
one still wrong is marked, so **Original** shows it under the passage as
"Check: …". The other lines only point at passages worth a look; the narration
stays as written. When a stopped job resumes, the batches already done get the
same naming and checks.

A local model server is asked at temperature 0.2. Left at a model's default,
often 1.0, the same paper read noticeably differently on every run; at 0.2 its
prose comes out word for word the same far more often, in the same tone. When
the connection to any model drops or is refused, Hilde asks again after 1, 2,
4, and then 8 seconds, each noted in the job log, before the job fails. A local
model may write at most 16,000 tokens for one passage, its reasoning included.
One still writing at that limit has usually fallen into repeating itself; Hilde
asks again, up to three times, and then stops the job rather than wait on it.

When a PDF prints each author's affiliation under the name, in columns, Hilde
pairs them before the model sees the line: "Llion Jones, Google Research;
Aidan N. Gomez, University of Toronto; Łukasz Kaiser, Google Brain". The job
log counts the authors paired. Otherwise the model pairs an author with an
affiliation only when the title block lists exactly one per name, and never
guesses. A heading the paper numbers is read exactly as printed, number or
appendix letter included ("4 Why Self-Attention", "B. Baseline Methods"),
without asking the model, so every heading of a book follows one rule and
none gets "Part" or "Section" added. A paper's title, which PDF extraction
can mistake for a page header and leave out, is put back at the top as a
heading, so the narration opens with it; the job log says when it did.

The model follows the instructions in `prompts/PAPER-AUDIO-BOOK.md`; each job
reads them when it starts, so edits apply to the next job, and a job adapted
under other instructions starts its adaptation over. Their examples are
invented rather than taken from a real paper, since a model sometimes copied
an example into the narration in place of the paper's own text.

When the model leaves out a whole passage of the author's text, Hilde asks
once more, saying that only what the instructions name may be left out, so a
sentence the model judged a mere definition is read after all. A passage with
the paper's author lines, bold names on its first page before the abstract, is
asked for as the title block, and if the model leaves it out again it is read
as printed, without marks or email addresses. A stray reference entry is not
asked for again. A passage the model narrates keeping less than 80% of the
author's words is asked for once more, with the sentences it left out or
reworded named, and the answer that keeps more of the author's words is used.
The job log notes each step.

When text extraction leaves reference entries outside a standalone References
section, the model leaves them out one by one. The log shows each as `Paragraph
149/174 has nothing to read aloud: …` with the model's reason, and nothing is
narrated for it. A figure the model leaves out still shows in the reader, after
the text before it.
