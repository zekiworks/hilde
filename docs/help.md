# Hilde Reader help

> **DRAFT** for review by Ümit and Can.
<!-- Each ## heading is one section of the help document Ask Hilde reads; these comments never reach the model. -->

## What Hilde Reader does

Hilde Reader turns a document, such as a research paper, into an audiobook. Before it reads, a language model can rewrite the text for listening: it removes page headers and reference marks, and describes figures, tables and equations in words. Then a voice reads it aloud. While you listen, the word being read is marked in the text. You can ask Hilde about any part of the book.

The tabs are **Create** (make an audiobook), **Voices** (your voices), **Listen** (your **Library** and the open book), **Progress** (while a book is being made) and **Settings** (the gear at the right).

<!-- checked: docs/web-ui.md "Header and navigation", "Create an audiobook" steps 1-4; README.md "What it does" -->

## Adding a document

1. Open **Create**.
2. In **Add the source document**, pick one of three cards: **Saved document** (one you added before), **From a URL** (a link to a PDF, TXT or Markdown file, or an arXiv page), or **Upload a file** (drop a file or **browse**; on a phone or tablet, **Choose a file**).
3. Press **Continue**, then choose a voice.
4. In **How Hilde prepares the text**, choose the model and the options, such as **Adapt the text for listening**. The **Summary** shows, with a **Local** or **Cloud** badge, where your text goes.
5. Press **Create audiobook**.

You can queue several books; they are made in order. **Progress** shows each stage. **Stop** keeps what is finished, and starting the same book again continues from there. When it is done, **Start listening** opens it.

If you add a document you already have, Hilde says **You already have this one** and offers **Open**, **Overwrite** and **Change voice**.

<!-- checked: docs/web-ui.md "Create an audiobook" (three cards, Continue, Summary, You already have this one, queue), "Following an audiobook being made" (Stop, Continue from the last completed part) -->

## Choosing and changing a voice

**Voices** lists your saved voices. Press the green play button to hear a voice. **Select** makes it the voice Create starts with. Click or tap a voice's description to read all of it; on a phone it opens under the name, one at a time.

To make a new voice, press **New voice**, pick tags such as *warm* or *low-pitched*, add details in your own words if you like, and give it a name. **Generate preview** makes a sample. **Save voice** turns on once you have listened to the preview. New voices can be made only while no audiobook is being made.

To hear a finished book in another voice, open it in **Listen**, then **⋯** › **Change voice…**. The text stays the same; the new voice is added beside the others, and nothing is replaced. If a book has several voices, pick one in the line under its title.

<!-- checked: docs/web-ui.md "Voices", "Making a new voice", Listen "⋯ holds … Change voice…", "A book read by more than one voice shows a voice picker" -->

## Voice tags

Tags are short words that describe a voice, in groups: voice, pitch, texture, pace, manner, and what it suits (for example *narrator*). You can add your own. A tag has at most 32 characters, and a voice at most 12 tags. Hilde may suggest tags it finds in a voice's description; nothing changes until you confirm them under **Review**.

<!-- checked: docs/web-ui.md "Voice tags" (table, 32 chars, 12 tags, "4 suggested · Review", "Nothing changes until you confirm") -->

## Playback and speed

**Listen** opens the **Library**. Books you are in the middle of are under **Continue listening**; the rest under **All books**. **Search titles** filters by title. The round play button on a card plays the book without opening it; a click on the card opens it.

The player bar sits at the bottom: play/pause, the seek bar, and the speed. Click the speed (for example **1.0×**) to choose from 0.75× to 2×. Tap a word to play from it; tap the sentence being read to pause.

Your place and speed are remembered by this browser only. A book continues from the start of the sentence where you stopped.

Keyboard: Space or K plays or pauses, J goes back and L forward 10 seconds, ← and → 5 seconds, < and > change the speed, ? lists all shortcuts.

**⋯** in the book also offers **Download MP3** and **Download source**.

<!-- checked: docs/web-ui.md "Listen" (Library groups, play button, player bar, speed 0.75×–2×, tap a word, browser-only place, shortcuts incl. < > 0.25×–2×, ⋯ menu) -->

## Follow-along highlighting

While a book plays, the paragraph being read has a thin blue line at its left, the sentence a blue tint, and the word a blue underline. **Follow along** scrolls the page when the sentence leaves the screen. If you scroll away yourself, it stops; press **↑ Back to reading position** (or ↓) to return.

**Narration text** shows what Hilde reads aloud. **Source text** shows the document's own words, with the page each passage starts on and a label: **Original**, **Unchanged** or **Not narrated**. Descriptions the model wrote of figures and tables are labelled **Hilde's description**. Books made without adapting the text have no **Source text**.

<!-- checked: docs/web-ui.md "Listen" (highlight paragraphs, Follow along, Back to reading position pill, Narration text | Source text, Original/Unchanged/Not narrated, Hilde's description); Settings "Highlight style" omitted as a temporary test -->

## Ask Hilde

Press **Ask Hilde** in the player bar to ask about the book. To ask about a passage, select it: on a computer, press **Ask about this**; on a phone, tap the teal **Ask Hilde** button that appears above the player bar while text is selected. To ask about a figure, click it and press **Ask about this figure**. The book pauses while you ask.

What you asked about shows as a card above the question box. **Context details** lists exactly what goes with your question. **Word | Sentence | Paragraph** widens the selection. Press **Explain this part**, or type your own question.

Hilde looks up the paragraphs it needs and cites them, such as ¶12; click one to jump there. Ask follow-ups, such as "give an example". Hilde can also write Markdown notes, such as a summary, listed under **Notes Hilde wrote**. If the server has web search, Hilde can search the web too.

Pick the model in the chat header; its badge says **Local** or **Cloud**. Under each answer, a gray label names the model that wrote it. **🔈 Read aloud** reads an answer in Hilde's voice. The conversation is kept with the book; **⋯** › **New conversation** starts over.

Hilde can also answer questions about Hilde Reader itself, from this help. If the help doesn't cover something, Hilde says it isn't sure.

The narration can be wrong; when it differs from the document, Hilde says so and goes by the document.

<!-- checked: docs/web-ui.md "Listen" (Ask about this, phone Ask Hilde shown while text is selected, Ask about this figure, Selected text card, Context details, Word|Sentence|Paragraph, Explain this part, ¶ links, write_file notes, Web search on, model box badge, model label, Read aloud, New conversation); audiobook_tts_web.py CHAT_TOOLS (search_document, read_paragraphs, write_file, list_files, delete_file) and CHAT_WEB_TOOLS (web_search, read_web_page); the help is the document `help` (ask_help(), ask_system_prompt()). -->

## Local and Cloud: what data leaves your computer

**Local** models run on your own machines, or on a model server on your network that you added under **Settings** › **Providers**. **Cloud** models run at a provider you connected there: OpenAI (ChatGPT or an API key), Anthropic (an API key or Claude Code), Google Gemini, DeepSeek or xAI.

**Always local:** your documents, books, voices, notes and conversations are stored on the Hilde server. Voices are designed and books are narrated on the Hilde server's own GPUs or on your other machines, unless the server was set up to use a speech server. Your place and speed stay in your browser. Keys and sign-ins stay on the server; the page never receives them.

**When you make a book with a Cloud model**, the document's text, and figure pictures when figures are read from their pictures, go to that provider.

**When you ask a Cloud model**, the provider receives, with each question: Hilde's instructions and the list of help sections; the book's title and abstract (or its first few hundred words); your question and the card you attached (your selection, its sentence, the paragraph's narration and the paper's text, a figure's caption and description, and its picture if the model reads images); earlier questions, and earlier answers cut to their first and last sentence; and the paragraphs, help sections and web results Hilde looks up while answering. The whole book is not sent. Only the provider of the model in the header receives it.

**Web search**, if set up, sends the search words Hilde chooses to the server's search service (SearXNG), which passes them to public search engines such as Google and Bing. Pages Hilde reads are fetched by the Hilde server; their text goes to the model.

Use a Local model for private documents.

<!-- checked: docs/configuration.md "Provider sign-ins and keys" (providers table, page never receives a key), "Web search for Chat", "Ask Hilde settings", "Storage"; README.md "Known limitations: Cloud providers receive your document", "Local first"; docs/web-ui.md "Listen" (Context details, picture sent only when the model takes pictures; browser-only place), Settings "Providers"; audiobook_tts_web.py ask_system_prompt() (instructions, help sections, title, abstract or opening cut to ASK_DEFAULTS abstract_tokens, 500 tokens), AskLedger.render() (questions in full, ask_short_answer(), tool results), chat_attachment() (narration and source text), read_web_page() (fetched by the server, long pages stored in .cache/chat-docs), web_search() (SearXNG JSON API) -->

## File types

Hilde Reader reads PDF, TXT or Markdown files (`.pdf`, `.txt` or `.text`, `.md` or `.markdown`), up to 64 MiB. A file of another type is refused with a message naming these. Links may point to such a file or to an arXiv paper. Web pages (HTML) and EPUB books are not supported.

<!-- checked: docs/web-ui.md "Create an audiobook" (supported types, 64 MiB, arXiv); README.md "Known limitations: Inputs" -->

## Common problems and fixes

- **"Choose a document first"**: the card you chose is empty. Fill it in, or pick another card.
- **Create audiobook stays gray**: the text beside it names the option and why, with one-click fixes such as **Use the text model** or **Turn off this option**.
- **"⚠ … is unreachable"**: that model's server does not answer. Click one of the models offered below it. Hilde never switches models by itself.
- **The chat shows a red box**: you are not signed in, the model's server is not running, or the connection failed. Follow the hint, or use **Providers…**.
- **Claude Code models can't chat**: choose another model for Ask Hilde.
- **The chat asks you to recreate the book**: the book was made before Hilde wrote paragraph summaries. Use **⋯** › **Recreate with the latest Hilde…**. This writes the text again; other voices must be made again.
- **A book stopped or failed**: on **Create** or **Progress**, press **Continue from the last completed part**.
- **Paused: a model became unreachable**: press **Retry**, or **Continue with another model…**.
- **No sound**: check the browser's volume and the computer's output device.
- **Settings are read-only**: providers and machines can be changed only from a browser on the Hilde server.

<!-- checked: docs/web-ui.md "Create an audiobook" ("Choose a document first", Not ready fixes, unreachable alternatives), "Following an audiobook being made" (stopped/failed card, Paused, Retry), "Listen" (chat red box + Providers…, Claude Code models cannot, no paragraph summaries → recreate, player silent), "Settings" (Read-only from this device) -->
