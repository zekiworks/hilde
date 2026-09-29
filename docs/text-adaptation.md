# Text adaptation

**Adapt the text for listening**, in the **Create audiobook** step, rewrites a
document for someone listening, who cannot skim or glance back. The author's
prose stays word for word. Whatever would be a chore to hear is left out:
tables of contents, lists of sections, section numbers, page numbers, citation
marks, and the bibliography. Whatever the ear cannot hold is tuned down to its
point: tables, formulas, long lists, and runs of numbers. A table of contents
or bibliography under its own heading is removed before the model sees the
text, even without adaptation and even when the PDF runs the heading into the
paragraph before it. Inline attributions and the sections after the
bibliography, such as appendices, remain.
While adaptation is selected, its settings appear under **Advanced** in
**Text adaptation**:

- **Model** picks the model that rewrites the text. Left on Default, a job uses
  your local server's first model once you add one under **Add local**; if that
  server does not answer, the job stops rather than send your document to a
  cloud provider. Without a local server, Default is OpenAI's first model once
  signed in, then Anthropic's. A cloud provider that is busy or has a passing
  error is asked again, up to four times.
- **Providers** connects cloud models. **OpenAI** signs this server in with a
  ChatGPT account: open the sign-in page it shows and enter the code; the
  sign-in renews itself. **Anthropic** takes an API key from the
  [Claude Console](https://platform.claude.com/), since Anthropic allows
  Claude subscriptions only in its own apps; usage is billed to that key. The
  server keeps both in `~/.hilde/`, readable only by the user running it.
  **Remove** deletes the Anthropic key; delete `~/.hilde/openai.json` to sign
  out of OpenAI.
- **Add local** connects a model server on your network. Choose its type,
  **Ollama** or **OpenAI-compatible** (SGLang, vLLM, LM Studio), enter its
  `host:port`, for example `127.0.0.1:8010`, then choose one of its models.
  Tick **This model sees images** when the model accepts images, as Gemma 4
  does; figures and equations then go to it as images.
- **Workers** (default 4, at most 32) is how many paragraph batches are adapted
  at once, and **Paragraphs per worker** (default 1, at most 32) is how many
  paragraphs each batch holds. A figure or table is never split: its image,
  the labels read from inside it, and its caption always go to the model
  together, so it is described once.

Figures reach OpenAI and Anthropic models as images, and a local model too once
**This model sees images** is ticked. Otherwise a local model receives the text
extracted from each figure instead, and equations printed as images, which
carry no text, are left out.

Each description of a figure, a table, or an equation opens with a spoken cue
such as "Figure 2 shows…" or "The equation says…", so a listener hears where
the author's text stops. The job log names any description that doesn't.

In a PDF, a sentence that a page break splits is joined back together before
the model sees it, even when a footnote or a figure sat between its halves;
these then follow the sentence. The job log counts the sentences it rejoined.

The model follows the instructions in `prompts/PAPER-AUDIO-BOOK.md`; each job
reads them when it starts, so edits apply to the next job, and a job adapted
under other instructions starts its adaptation over.

When text extraction leaves reference entries outside a standalone References
section, the model leaves them out one by one. The log shows each as `Paragraph
149/174 has nothing to read aloud: …` with the model's reason, and nothing is
narrated for it. A figure the model leaves out still shows in the reader, after
the text before it.
