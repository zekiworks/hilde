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
  signed in, then Claude Code's, then the Anthropic API's. A cloud provider
  that is busy or has a passing error is asked again, up to four times.
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
    [Claude Console](https://platform.claude.com/), billed per use. **Remove**
    deletes it.

  The server keeps the ChatGPT sign-in and the API key in `~/.hilde/`,
  readable only by the user running it.
- **Add local** connects a model server on your network. Choose its type,
  **Ollama** or **OpenAI-compatible** (SGLang, vLLM, LM Studio), enter its
  `host:port`, for example `127.0.0.1:8010`, then choose one of its models.
  Tick **This model sees images** when the model accepts images, as Gemma 4
  does; figures and equations then go to it as images.
- **Workers** (default 4, at most 32) is how many paragraph batches are adapted
  at once, and **Paragraphs per worker** (default 1, at most 32) is how many
  paragraphs each batch holds. A figure or table is never split: its image,
  the labels read from inside it, and its caption always go to the model
  together, so it is described once. A figure or table with a caption is
  always a passage of its own in the book's text.

Figures reach OpenAI and Claude models as images, and a local model too once
**This model sees images** is ticked. Otherwise a local model receives the text
extracted from each figure instead, and equations printed as images, which
carry no text, are left out.

Each description of a figure, a table, or an equation opens with a spoken cue
such as "Figure 2 shows…" or "The equation says…", so a listener hears where
the author's text stops. The job log names any description that doesn't.

In a PDF, a sentence that a page break, a figure, or a footnote splits is
joined back together before the model sees it; what split it then follows the
sentence. An equation printed as a picture stays inside its sentence and goes
to the model with it, so the sentence is read through. A listing set in a
typewriter font, such as a program, a prompt, or a skill file, reaches the
model as one block from the page's own lines, even across a page break or a
table printed inside it, so a sentence wrapped from one line to the next is
read through rather than split into two passages. A table whose caption the
PDF stores as ordinary text ("Table 6: …") still gets that caption. A figure
or table printed before the text that first mentions it ("as Figure 3
shows…"), on the same page or the next, moves after that text, so its
description never comes before the author introduces it or in the middle of
the argument. A footnote moves to the paragraph that cites it and goes to the
model with that paragraph, which reads it right after the sentence carrying its
marker, saying whom or what it is about ("Aidan Gomez did this work while at
Google Brain"). A note about one author comes before a note every author line
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
description of a table keeps the values that bound the comparison, such as the
sizes and scores of the smallest and largest model, and every caveat its
caption gives about how values were measured. Every statement of a footnote
stays ("Equal contribution. Listing order is random."), and so does every
quantity in the author's prose, in the author's own voice ("we"). A figure
description says only what the caption states or the image plainly shows. A
link the text depends on becomes where in words ("in the tensor2tensor
repository on GitHub"). An acronym is expanded only where the author spells it
out, once; the rest, such as GPU, stay acronyms.

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
under other instructions starts its adaptation over.

When text extraction leaves reference entries outside a standalone References
section, the model leaves them out one by one. The log shows each as `Paragraph
149/174 has nothing to read aloud: …` with the model's reason, and nothing is
narrated for it. A figure the model leaves out still shows in the reader, after
the text before it.
