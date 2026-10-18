# Configuration

`audiobook_tts_web.py` serves **Hilde**, a browser app over a shared
server-side library. Its configuration comes from command-line options when the
server starts; browsers cannot replace that process-wide configuration.
Settings each browser may change, such as precision, chunking, and batch size,
are under **Settings** in the page; see [Settings](web-ui.md#settings). The
text-adaptation model is on **Create**, under **Adapt the text for listening**.

## Start the server

```bash
python audiobook_tts_web.py \
  --voice-design-model models/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
  --voice-clone-model models/Qwen3-TTS-12Hz-1.7B-Base \
  --port 8800 --open
```

For local backends, run the web server with the interpreter that has `torch`,
`qwen-tts`, and `soundfile`; it launches the CLI through `sys.executable`.
`example_run.sh` fills in this command for one machine's interpreter and model
paths; edit those for yours. Extra arguments pass through to the server.

| Option | Default / meaning |
| --- | --- |
| `--host` | `127.0.0.1`, this machine only; pass `0.0.0.0` to reach it from other devices. See [Access and security](../README.md#access-and-security). |
| `--port` | `8800`. |
| `--https` | Off. Serves over HTTPS with Hilde's own certificate authority, made on first start in `~/.hilde/tls`; see [HTTPS](../README.md#https). A plain `http://` address is sent to the `https://` one. |
| `--tls-cert`, `--tls-key` | Serve over HTTPS with your own certificate and key (PEM, chain included), such as one for your domain or from Tailscale, instead of Hilde's. |
| `--open` | Opens the page in a browser. |
| `--verbose` | Logs every request to stderr. |
| `--manage-from-network` | Off: only a browser on the server's own machine adds, changes, and removes providers and machines and sets the default models. On: any browser that reaches the server may; see [Manage from other devices](#manage-from-other-devices). |
| `--storage-root` | `~/.hilde/library`; see [Storage](#storage). |
| `--search-server` | Off. A SearXNG address, such as `http://127.0.0.1:8890`, lets Ask Hilde search the web and read public pages; see [Web search for Chat](#web-search-for-chat). |
| `--local-server PROVIDER=URL[,vision]` | None. A local model server (repeatable): `lm-studio` for any OpenAI-compatible server, or `ollama` for an Ollama server; `,vision` says its models read images. At startup a server whose address is not saved yet is added once to `~/.hilde/providers.yaml` (see [Local model servers](#local-model-servers)), named after its host; its row under **Settings › Providers** says "set at startup". Its name can be edited there, but it cannot be deleted, nor its address or images changed, while the flag is given. |
| `--voice-design-model` | VoiceDesign model directory, or a Hugging Face ID with `--allow-model-downloads`. |
| `--voice-design-server`, `--voice-design-server-model` | A speech server for voice design instead of the model; the server model defaults to `gpt-4o-mini-tts`. See [Speech models](#speech-models). |
| `--voice-clone-model` | Base model directory, or a Hugging Face ID with `--allow-model-downloads`. |
| `--voice-clone-server`, `--voice-clone-server-model` | A speech server for narration instead of the model; the server model defaults to `tts-1`. |
| `--allow-model-downloads` | Disabled; permits Hugging Face model IDs and downloads. |
| `--render-voice-previews` | Renders comparable previews for older voices, then exits. See [Voices](web-ui.md#voices). |

### Manage from other devices

By default only a browser on the server's own machine may add, change, or
remove providers (**Settings › Providers**) and narration machines
(**Settings › Machines and devices**), and set the
[default models](#default-models) (**Settings › Models and connection**);
other devices see them read-only.
`--manage-from-network` lets every browser do so, and the server says at
startup "Providers and machines can be changed from any device that reaches
this server." Hilde has no sign-in: with the flag, anyone who reaches the
server can add API keys, sign it out of a provider, or add SSH machines that
then run programs as this user, so use it only on a network you trust.

### Start at boot

On Linux, a systemd user service starts Hilde with the machine and restarts it
if it stops. Save this as `~/.config/systemd/user/hilde.service`, with your
checkout, interpreter, and server options:

```ini
[Unit]
Description=Hilde audiobook studio
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/path/to/hilde
ExecStart=/path/to/python audiobook_tts_web.py --host 0.0.0.0 --port 8800 \
  --voice-design-model /path/to/VoiceDesign --voice-clone-model /path/to/Base
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Then run `systemctl --user daemon-reload && systemctl --user enable --now hilde`,
and once `sudo loginctl enable-linger $USER`, so user services start at boot
without a login. `systemctl --user restart hilde` restarts it and
`journalctl --user -u hilde` shows its log. Restarting stops any book being
made; started again with the same document and voice, it continues where it
stopped.

## Default models

Hilde uses no model nobody chose. Under **Settings › Models and connection**
two server-wide defaults are set with the same model picker as Create
(**Runs on** Local or Cloud, then **Model**):

- **Default text narration**: the model that prepares and adapts the text
  for listening. Create and Ask Hilde start with it.
- **Default image narration**: the model that reads figures from their
  pictures. It offers only models that read images, and **Same as text
  narration** (the default), which sends figures to the text narration model.

Each row shows the chosen model's name and its Local or Cloud badge; an unset
text row reads "Not set". Only a browser on the server's own machine sets
them, or any browser when Hilde starts with `--manage-from-network`; other
devices see the rows read-only under "Read-only from this device. Set the
default models from a browser on the Hilde server, or start Hilde with
--manage-from-network." Hilde keeps them for the whole server in
`~/.hilde/providers.yaml` (see [Local model servers](#local-model-servers)).
A browser's own choice is made in Create or Ask Hilde and remembered there;
Settings has no per-browser default.

For text and for figures separately, a browser uses:

1. the model it used last, if it is reachable;
2. on first use, the server's default, if it is reachable (said as "Default
   on this server");
3. with no default set, the only model there is, if exactly one is connected
   (shown with its Local or Cloud badge);
4. otherwise nothing: the picker asks for a model.

When the model from 1 or 2 does not answer, nothing is selected: the model
row says "⚠ <name> is unreachable", Create waits, and one-click alternatives
follow: the other reachable models, local ones first. Nothing falls back to
another model by itself. In Settings an unreachable default keeps its name and
badge with the same red line, and a browser that may set the defaults gets the
same alternatives, one click setting a new default. A local server is
"available" when it answers its OpenAI-compatible `/v1/models`, "server
unreachable" when it does not; its models stay in every picker then, greyed
(see [Local model servers](#local-model-servers)). A default model no server
lists is a greyed row "not listed by any reachable server". If a model stops
answering during a book, the job pauses with the parts finished so far kept;
**Retry** asks the same model again, and **Continue with another model…**
sends only the remaining parts to the model you click. The book records which
model wrote which parts.

## Web search for Chat

Ask Hilde can search the web and read pages when the server knows a
[SearXNG](https://docs.searxng.org/) instance with its JSON format turned on.
One runs in Docker on the same machine:

```bash
mkdir -p ~/.hilde/searxng
cat > ~/.hilde/searxng/settings.yml <<EOF
use_default_settings: true
server:
  secret_key: "$(openssl rand -hex 32)"
  limiter: false
  image_proxy: false
search:
  formats: [html, json]
EOF
docker run -d --name hilde-searxng --restart unless-stopped \
  -p 127.0.0.1:8890:8080 -v ~/.hilde/searxng:/etc/searxng searxng/searxng
```

Then start Hilde with `--search-server http://127.0.0.1:8890`; the startup
lines say `Web search for Chat:` and the address. SearXNG sends each search,
with words from the conversation, to public engines such as Google and Bing;
one engine that is rate-limited or asks for a CAPTCHA is left out of that
search. Hilde reads only pages at public addresses: never this machine or the
local network, also when a page redirects there.

## Storage

`--storage-root` defaults to `~/.hilde/library`, outside the project folder, so
every copy of Hilde finds the same library and re-cloning the project leaves it
alone. The server creates and owns this fixed layout:

```text
~/.hilde/library/
├── Voices/
├── Audiobooks/
│   └── attention-is-all-you-need--bdfaa68d8984/
│       ├── book.json
│       ├── source.pdf
│       ├── narration.json
│       ├── reader.md
│       └── voices/
│           └── Eir/
│               ├── audio.mp3
│               └── timings.json
├── Documents/
├── in_progress/
└── workers.yaml
```

- **Voices** contains one directory per narrator: `reference.wav`, the exact
  UTF-8 `transcript.txt` used to create it, its `description.txt`, and, for
  voices made before fixed previews, a rendered `preview.wav`.
- **Documents** contains uploaded files and URL downloads. URL downloads use an
  optional filename or infer one from the response.
- **Audiobooks** holds one folder per book, named for its title and the first
  12 hexadecimal digits of the SHA-256 of its document's bytes, so the same
  document under another file name is the same book. A book is made from its
  document once:
  - `book.json` records the document's full SHA-256 and the file names it came
    under, the Hilde version and git commit, the prompt, extraction schema, and
    model that wrote its text, whether each local model that worked on it saw
    its figures as images (`local_vision`), the checking model of error correction and what it
    checked and corrected (`verification`), the model that read its figures
    from their pictures (`figure_model`), how much of the author's prose the text kept,
    and each voice with when it was made, its length, and whether it reads the
    book's current text (`ready`) or an earlier one (`stale`);
  - `source.pdf` (or `.md`, `.txt`) is a copy of the document;
  - `narration.json` is the text read aloud, as passages typed body, heading,
    figure, table, or equation, each with its PDF page, the author's original
    text, and the author's paragraphs it came from, typed body, heading,
    figure, table, equation, footnote, or caption;
  - `reader.md` is the follow-along view with its pictures embedded;
  - `voices/<voice>/` holds that voice's `audio.mp3` and its own word and
    sentence `timings.json`.

  Each book or voice is written into a hidden folder and renamed into place
  when it is complete, so a crash never leaves a half-made one; the next start
  removes what was left unfinished.
- **in_progress** contains durable source snapshots, extraction checkpoints,
  and narration chunks for unfinished jobs. It is removed for a job only after
  its book is published.
- **workers.yaml**, present once a node is added or a local device turned off,
  lists the other machines that narrate over SSH and this machine's devices
  that do not; see [Narration workers on other machines](ssh-workers.md).

On its first start, this version moves audiobooks kept in the earlier layout
(`Audiobooks/<name>.mp3` with hidden `.readers` and `.versions` folders) into
book folders without calling any model, keeping each MP3 as it is. A book gets
its text back from the `Documents/<name>-narration.txt` file its job stored,
when that matches its reader; without it, the book plays as before but needs
**Recreate** before it can take another voice. The earlier files wait in
`Audiobooks/.backup/` until the book has played once.

Existing files are not migrated automatically when the storage root changes.
Move them into the appropriate directory yourself. The web app's **Delete**
buttons remove voices, documents, and audiobooks with every voice.

A new library starts with the stock voices from the repository's `voices/`
folder. They are copied only when the library is created, so a stock voice you
delete stays deleted.

Earlier versions kept the library in `User/` in the project folder. When
`~/.hilde/library` does not exist yet and `User/` holds voices or books, the
server does not start an empty library; it stops and prints the command that
moves `User/` there (`mkdir -p ~/.hilde && mv User ~/.hilde/library`, with full
paths). Pass `--storage-root` with that folder to keep it where it is instead.

Every copy of Hilde started without `--storage-root` uses the same library. Give
an older copy you keep for testing its own `--storage-root`: it may rewrite
books made by newer versions into the layout it knows.

### Provider sign-ins and keys

Cloud providers for text adaptation and Ask Hilde are connected once for the
whole server, under **Settings › Providers** (see [Web UI](web-ui.md#providers)),
and kept next to the library, readable only by the user running Hilde:

```text
~/.hilde/
├── openai.json       # the ChatGPT sign-in (its account's e-mail is shown as "Signed in as …")
├── openai-api.json   # OpenAI API key
├── anthropic.json    # Anthropic API key
├── gemini.json       # Google Gemini API key
├── deepseek.json     # DeepSeek API key
├── xai.json          # the xAI sign-in, encrypted (see below)
├── xai-api.json      # xAI API key
└── credentials.key   # this server's key for the encrypted sign-ins
```

Delete a file to sign out or forget a key (**Delete** under **Settings ›
Providers** does the same). The page never receives a key or a sign-in
token; a saved key shows only as "Key saved". These files follow the server
user's home, not `--storage-root`. Providers are added, changed, and removed
only from a browser on the server's own machine, unless Hilde starts with
[`--manage-from-network`](#manage-from-other-devices); other devices see them
read-only.

**Add** under **Settings › Providers** offers the ways in Hilde supports for
each cloud provider:

| Provider | Sign-in with your account | API key |
| --- | --- | --- |
| OpenAI | ChatGPT sign-in (device code) | yes |
| Google (Gemini) | — | yes |
| Anthropic (Claude) | Subscription: through **Claude Code** on the server | yes |
| DeepSeek | — | yes |
| xAI (Grok) | xAI sign-in (SuperGrok or X Premium+; device code with PKCE) | yes |

The xAI sign-in uses the public OAuth client of xAI's Grok CLI against
`auth.x.ai`: Hilde shows xAI's page (`accounts.x.ai`) and a code, the user
approves there, and the server collects the token, since the browser may be on
another machine. The token is kept in `xai.json` encrypted with AES-256-GCM
under `credentials.key` (32 random bytes made on first use, mode 0600 like the
file itself), and renewed in the background about 15 minutes before it
expires. If xAI refuses a renewal, the page says **Sign in to xAI again**; if
xAI answers 403 to the account, it says **xAI refused this account (403)** and
offers **Use an API key instead**. Hilde never switches to the key by itself:
the key's models are listed separately ("xAI API"), and a book or question
uses them only when the user chooses one. Copying `xai.json` without
`credentials.key` gives nothing usable; anyone who can read both as the server
user can still use the sign-in.

### Local model servers

Local model servers are kept for the whole server in `~/.hilde/providers.yaml`,
written by **Settings › Providers** (owner-only: the file 0600, its folder
0700, replaced atomically). The [default models](#default-models) set under
**Settings › Models and connection** are kept in the same file. Stop the
server before editing it by hand; an invalid file stops startup with an error
naming it. At most 16 servers:

```yaml
local:
- name: Workstation               # 1–32 characters, unique
  address: http://192.0.2.10:8000 # unique
  type: openai-compatible         # or ollama (detected when added)
  images: true                    # "Models here read images"
claude_code: false                # only after Claude Code was removed
defaults:                         # only the defaults set
  text: lm-studio/my-big-model    # Default text narration
  image: lm-studio/my-vision-model  # Default image narration; left out: same as text narration
```

`claude_code: false` means Hilde no longer uses this server's Claude Code;
**Subscription** for Anthropic turns it on again. A default is written as the
model picker shows a model in its details: `lm-studio/<model>` or
`ollama/<model>` for a model on a local server (as that server lists it), or
`PROVIDER/<model>` for a cloud model (such as `openai/<model>`).

Each server's model list, as it last answered, is remembered beside it in
`~/.hilde/local-models.json` (owner-only, replaced atomically, and written
only when a list changes), so that while the server does not answer its
models stay in every picker, greyed and marked "server unreachable", instead
of disappearing. Hilde keeps this file itself; removing a server under
**Settings › Providers**, or changing its address, drops its models, and a
server no longer in `providers.yaml` at startup is dropped too. A server that
has never answered has no models to show yet. The file looks like this:

```json
{"schema": 1, "servers": {"http://192.0.2.10:8000": {"provider": "lm-studio", "models": ["model-a", "model-b"]}}}
```

Local servers that browsers once added for themselves (earlier versions kept
up to eight per browser) are no longer used and are not migrated: they were
personal. Add them again under **Settings › Providers**.

## Speech models

A configured model must be an existing directory unless
`--allow-model-downloads` is set, in which case it may be a Hugging Face ID.
Each role can instead use its own OpenAI-compatible speech server:

| Role | Local/Hugging Face configuration | Remote configuration |
| --- | --- | --- |
| Voice creation | `--voice-design-model PATH_OR_ID` | `--voice-design-server URL` and optional `--voice-design-server-model NAME` |
| Narration | `--voice-clone-model PATH_OR_ID` | `--voice-clone-server URL` and optional `--voice-clone-server-model NAME` |

The local-model and remote-server options are mutually exclusive within each
role. A role left unconfigured stays unavailable. Remote API credentials come
from `OPENAI_API_KEY` in the server environment.

## Narration workers

With a local narration model, the server creates one worker for every CUDA
device visible to its process and one for every device of the nodes in
`workers.yaml`. A remote OpenAI-compatible narration backend, or a local host
without CUDA, has one fallback worker.

The pool is process-owned configuration, not a browser setting or a hardcoded
machine count. Local membership is the CUDA devices visible to the server
process; use `CUDA_VISIBLE_DEVICES` to select a deployment-specific subset, for
example to keep narration off GPU 0 even when it has room:

```bash
CUDA_VISIBLE_DEVICES=1,2,3 ./example_run.sh
```

Without a restart, each narration worker's box under **Settings** › **Machines and devices** › **This server** turns each
local device off or on; with a node added, every local device can be off, so
only the other machines narrate.

Remote membership comes from the nodes in `workers.yaml`, which **Add narration workers**
under **Settings** writes; nodes added or removed there take effect without a
restart. See [Narration workers on other machines](ssh-workers.md).

The server probes PyTorch-visible CUDA and MPS devices in a short-lived child.
It counts CUDA devices after initialization, so a GPU the runtime cannot open,
such as one waiting for a reset, is skipped and the remaining GPUs stay in the
pool instead of the server falling back to CPU. Local membership is fixed at
startup: restart the server after changing `CUDA_VISIBLE_DEVICES` or editing
`workers.yaml` by hand, or after GPUs are added, removed, or recovered. Voice
design runs alone, on the
GPU with the most free memory at that moment, so a GPU another program has
filled is passed over; `nvidia-smi` measures this without touching any GPU.
Without CUDA it uses MPS, then CPU.
Unsupported precision or `flash_attention_2` combinations still fail through
CLI validation.

A book with several workers starts only on GPUs with about 6 GiB free: the
4 GiB model plus room for a batch. The others are checked every minute and join
once another program frees memory. A GPU that runs out of memory mid-book hands
its chunks to the other workers and waits the same way, so the book slows down
instead of failing. If every GPU is full, the book waits until one has room;
**Stop** ends it. The log names the waiting GPUs; their chips still show
`running`, because the book holds them.

## Batch size

All batch sizes reuse the same voice prompt. Batching controls memory use and throughput—not narrator identity.

`--batch-size N` generates up to `N` chunks per clone call; `0`, the default,
submits the whole book on one device. In distributed mode it is the number of
chunks one worker takes at a time, and `0` becomes one chunk per worker so
faster workers can pull more work. The web app's **Batch size** (under
**Settings**) defaults to 2.

Larger batches narrate faster until GPU memory runs out. On an RTX PRO 6000
with about 6 GiB left beside another model, one sentence per call narrated at
1.6× real time and two at 2.6×; four or more ran out of memory. Each sentence
needed about 0.7 GiB on top of the 4 GiB model.

A batch that runs out of CUDA memory is retried one chunk at a time, so a batch
size that is too large costs time rather than the book. If the log often says
so, lower the batch size to skip the wasted attempt, and reduce
`--chunk-max-chars` (**Chunking** under **Settings**) when individual chunks are
too expensive. Other GPU processes, such as another model server, leave less
memory for narration. A single chunk that still does not fit fails a
single-device run; with several workers, that GPU hands its chunks to the others
and waits (see [Multiple GPUs](command-line.md#multiple-gpus)).
With `--resume-dir`, rerunning the same command reuses the completed chunks.

Changing batch sizes can change sampled audio even with the same `--seed`. A saved reference establishes the shared speaker reference, not bit-for-bit reproducibility across hardware, batching decisions, or library versions.
