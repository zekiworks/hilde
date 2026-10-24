# Hilde Reader help

> **DRAFT** for review by Ümit and Can.
<!-- Each ## heading is one section of the help document Ask Hilde reads; these comments never reach the model. -->

## What Hilde Reader does

Hilde Reader turns a document, such as a research paper, into an audiobook. Before a voice reads it aloud, a language model can rewrite the text for listening: it removes page headers and reference marks, and describes figures, tables and equations in words. You can ask Hilde about any part of the book.

The tabs are **Create** (make an audiobook), **Voices**, **Listen** (your **Library** and the open book), **Progress** (while a book is being made) and **Settings** (the gear at the right; technical options sit under **Advanced**).

<!-- more: README.md › What it does -->
<!-- checked: docs/web-ui.md "Header and navigation", "Create an audiobook" steps 1-4; README.md "What it does" -->

## Adding a document

1. Open **Create**.
2. In **Add the source document**, pick one of four cards: **Saved document** (one you added before), **From a URL** (a link to a PDF, TXT or Markdown file, or an arXiv page), **Upload a file** (drop a file or **browse**; on a phone or tablet, **Choose a file**), or **Paste text** (an optional title, else its first line). Here **Cmd+V** or **Ctrl+V** outside a text field puts a copied file, link or text into the matching card.
3. Press **Continue**, then choose a voice.
4. In **How Hilde prepares the text**, choose the model and the options, such as **Adapt the text for listening**. The **Summary** shows, with a **Local** or **Cloud** badge, where your text goes.
5. Press **Create audiobook**.

You can queue several books; they are made in order, each stage shown in **Progress**. **Stop** keeps what is finished; starting the book again continues from there. Once done, **Start listening** opens it.

If you add or paste a document you already have, Hilde says **You already have this one** and offers **Open**, **Overwrite** and **Change voice**.

To go back, click or tap a step's row or press **← Back**; your choices are kept. The browser's Back and Forward move through the steps you visited; once a book has started, they open its progress, so it is never made twice.

<!-- more: docs/web-ui.md › Create an audiobook -->
<!-- checked: docs/web-ui.md "Create an audiobook" (four cards, Paste text, Cmd+V, finished rows, Back, browser history, Continue, Summary, You already have this one, queue), "Following an audiobook being made" (Stop, Continue from the last completed part) -->

## Choosing and changing a voice

**Voices** lists your saved voices. Press the green play button to hear a voice. **Select** makes it the voice Create starts with. Click or tap a voice's description to read all of it.

To make a new voice, press **New voice**, pick tags such as *warm* or *low-pitched*, add details in your own words if you like, and name it. **Generate preview** makes a sample. **Save voice** turns on once you have listened to the preview. New voices can be made only while no audiobook is being made.

To hear a finished book in another voice, open it in **Listen**, then **⋯** › **Change voice…**. The text stays the same; the new voice is added beside the others. If a book has several voices, pick one in the line under its title.

<!-- more: docs/web-ui.md › Voices -->
<!-- checked: docs/web-ui.md "Voices", "Making a new voice", Listen "⋯ holds … Change voice…", "A book read by more than one voice shows a voice picker" -->

## Voice tags

Tags are short words that describe a voice, in groups: voice, pitch, texture, pace, manner, and what it suits (for example *narrator*). You can add your own. A tag has at most 32 characters, and a voice at most 12 tags. Hilde may suggest tags it finds in a voice's description; nothing changes until you confirm them under **Review**.

<!-- more: docs/web-ui.md › Voice tags -->
<!-- checked: docs/web-ui.md "Voice tags" (table, 32 chars, 12 tags, "4 suggested · Review", "Nothing changes until you confirm") -->

## Playback and speed

**Listen** opens the **Library**. Books you are in the middle of are under **Continue listening**, the rest under **All books**; **Search titles** filters them. The round play button on a card plays the book without opening it; a click on the card opens it.

The player bar sits at the bottom: play/pause, the seek bar, and the speed. Click the speed (**1.0×**) to choose 0.75× to 2×. Tap a word to play from it; tap the sentence being read to pause.

Your place and speed stay in this browser only. A book continues from the start of the sentence where you stopped; straight back from the **Library** (no reload, no other book), it is exactly where you left it.

Keyboard: Space or K plays or pauses, J and L skip 10 seconds, ← and → 5, < and > change the speed, ? lists all shortcuts.

**⋯** in the book also offers **Download MP3** and **Download source**.

<!-- more: docs/web-ui.md › Listen -->
<!-- checked: docs/web-ui.md "Listen" (Library groups, play button, player bar, speed 0.75×–2×, tap a word, browser-only place, Library and back exactly as left, shortcuts incl. < > 0.25×–2×, ⋯ menu) -->

## Follow-along highlighting

While a book plays, the paragraph being read has a thin blue line at its left, the sentence a blue tint, and the word a blue underline. **Follow along** scrolls the page when the sentence leaves the screen. If you scroll away yourself, it stops and never pulls the page back; press **↓ Back to reading position** to return.

**Narration text** shows what Hilde reads aloud. **Source text** shows the document's own words, with the page each passage starts on and a label: **Original**, **Unchanged** or **Not narrated**. Descriptions the model wrote of figures and tables are labelled **Hilde's description**. Books made without adapting the text have no **Source text**.

<!-- more: docs/web-ui.md › Listen -->
<!-- checked: docs/web-ui.md "Listen" (highlight paragraphs, Follow along, Back to reading position pill, Narration text | Source text, Original/Unchanged/Not narrated, Hilde's description); Settings "Highlight style" omitted as a temporary test -->

## Ask Hilde

**Ask Hilde** is at the top right of every page (on a phone, the round sparkle). In an open book it asks about the book, like the player bar's. To ask about a passage, select it, then press **Ask about this** (on a phone, the teal **Ask Hilde** above the player bar). For a figure, click it and press **Ask about this figure**. The book pauses while you ask.

What you asked about shows as a card above the question box. **Context details** lists exactly what goes with your question. **Word | Sentence | Paragraph** widens the selection. Press **Explain this part**, or type your own question.

Hilde looks up the paragraphs it needs and cites them, such as ¶12; click one to jump there. Hilde can write Markdown notes (**Notes Hilde wrote**) and search the web if the server allows.

On other pages it answers questions about Hilde Reader from this help, else the developer docs or the code (saying so, with a link), or says it isn't sure. It only explains, never changes settings.

Pick the model in the panel's header; this browser keeps it, apart from the book's model. Its badge says **Local** or **Cloud**, and a gray label under each answer names its model. **🔈 Read aloud** reads an answer aloud. **⋯** › **New conversation** starts over: the book's, or the help questions alone.

**While a book is being made** (its card says **Being made · N%**), open it to read the paper and ask Hilde; narration and summaries arrive part by part, and the player appears when it's done. Using the book's model for Hilde too makes answers slower meanwhile.

The narration can be wrong; when it differs from the document, Hilde says so and goes by the document.

<!-- more: docs/web-ui.md › Ask Hilde on every page -->
<!-- checked: docs/web-ui.md "Listen" (Ask about this, phone Ask Hilde shown while text is selected, Ask about this figure, Selected text card, Context details, Word|Sentence|Paragraph, Explain this part, ¶ links, write_file notes, Web search on, model box badge, model label, Read aloud, New conversation; "A book being made": Being made · N% card, reading view without player, conversation kept and moved into the book, model per browser in player.chat_model); audiobook_tts_web.py CHAT_TOOLS (search_document, read_paragraphs, write_file, list_files, delete_file) and CHAT_WEB_TOOLS (web_search, read_web_page); the help is the document `help` (ask_help(), ask_system_prompt()); making_passages(), ASK_NOT_READY. -->

## Local and Cloud: what data leaves your computer

**Local** models run on your own machines, or on a model server on your network that you added under **Settings** › **Providers**. **Cloud** models run at a provider you connected there: OpenAI (ChatGPT or an API key), Anthropic (an API key or Claude Code), Google Gemini, DeepSeek or xAI.

**Always local:** your documents, books, voices, notes and conversations are stored on the Hilde server. Voices are designed and books are narrated on the Hilde server's own GPUs or on your other machines, unless the server was set up to use a speech server. Your place and speed stay in your browser. Keys and sign-ins stay on the server; the page never receives them.

**When you make a book with a Cloud model**, the document's text, and figure pictures when figures are read from their pictures, go to that provider.

**When you ask a Cloud model**, the provider receives, with each question: Hilde's instructions and the list of help sections; the book's title and abstract (or its first few hundred words; none on other pages); your question and the card you attached (your selection, its sentence, the paragraph's narration and the paper's text, a figure's caption and description, and its picture if the model reads images); earlier questions, and earlier answers cut to their first and last sentence; and the paragraphs, help sections, developer docs, code and web results Hilde looks up while answering. The whole book is not sent. Only the provider of the model in the header receives it.

**Web search**, if set up, sends the search words Hilde chooses to the server's search service (SearXNG), which passes them to public search engines. Pages Hilde reads are fetched by the Hilde server; their text goes to the model.

Use a Local model for private documents.

<!-- more: docs/configuration.md › Provider sign-ins and keys -->
<!-- checked: docs/configuration.md "Provider sign-ins and keys" (providers table, page never receives a key), "Web search for Chat", "Ask Hilde settings", "Storage"; README.md "Known limitations: Cloud providers receive your document", "Local first"; docs/web-ui.md "Listen" (Context details, picture sent only when the model takes pictures; browser-only place), Settings "Providers"; audiobook_tts_web.py ask_system_prompt() (instructions, help sections, title, abstract or opening cut to ASK_DEFAULTS abstract_tokens, 500 tokens), AskLedger.render() (questions in full, ask_short_answer(), tool results), chat_attachment() (narration and source text), read_web_page() (fetched by the server, long pages stored in .cache/chat-docs), web_search() (SearXNG JSON API) -->

## File types

Hilde Reader reads PDF, TXT or Markdown files (`.pdf`, `.txt` or `.text`, `.md` or `.markdown`), up to 64 MiB. A file of another type is refused with a message naming these. Links may point to such a file or to an arXiv paper. Web pages (HTML) and EPUB books are not supported.

<!-- more: docs/web-ui.md › Create an audiobook -->
<!-- checked: docs/web-ui.md "Create an audiobook" (supported types, 64 MiB, arXiv); README.md "Known limitations: Inputs" -->

## Common problems and fixes

- **"Choose a document first"**: the chosen card is empty: fill it in or pick another.
- **Create audiobook stays gray**: the text beside it names the reason, with one-click fixes such as **Use the text model**.
- **"⚠ … is unreachable"**: that model's server does not answer. Click one of the models offered below it. Hilde never switches models by itself.
- **The chat shows a red box**: you are not signed in, the model's server is not running, or the connection failed. Follow the hint, or use **Providers…**.
- **Claude Code models can't chat**: choose another model for Ask Hilde.
- **The chat asks you to recreate the book**: the book was made before Hilde wrote paragraph summaries. Use **⋯** › **Recreate with the latest Hilde…**. This writes the text again; other voices must be made again.
- **A book stopped or failed**: on **Create** or **Progress**, press **Continue from the last completed part**.
- **Paused: a model became unreachable**: press **Retry**, or **Continue with another model…**.
- **No sound**: check the volume and the output device.
- **Settings are read-only**: providers and machines can be changed only from a browser on the Hilde server.

<!-- more: docs/web-ui.md › Following an audiobook being made -->
<!-- checked: docs/web-ui.md "Create an audiobook" ("Choose a document first", Not ready fixes, unreachable alternatives), "Following an audiobook being made" (stopped/failed card, Paused, Retry), "Listen" (chat red box + Providers…, Claude Code models cannot, no paragraph summaries → recreate, player silent), "Settings" (Read-only from this device) -->
