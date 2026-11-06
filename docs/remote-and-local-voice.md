# Talking with Hilde away from home, and on another computer

Two setups for **Talk with Hilde**, **Listen for “Hilde”** and **🔈 Read aloud**
(see [Talking with Hilde](configuration.md#talking-with-hilde)):

- [Away from home](#away-from-home-through-tailscale): your phone or laptop on
  4G/5G or in another country reaches the Hilde server at home, privately.
- [On another computer](#on-another-computer-on-its-cpu): a laptop or desktop
  runs its own Hilde and its own speech server on its CPU, with no GPU and no
  other server.

Both need the speech server: LiquidAI's LFM3-Audio-2B, run by llama.cpp's
`llama-server`. It hears and speaks; the answers come from the model chosen
for Ask Hilde.

## Away from home, through Tailscale

Hilde has no login, so never open its port to the Internet (see
[Access and security](../README.md#access-and-security)). Instead,
[Tailscale](https://tailscale.com) joins your devices in a private network
that works over any connection, mobile data and other countries included. A
browser lets a page use the microphone only over HTTPS (or on the computer
itself), so Talk from a phone needs an HTTPS address; Tailscale gives one with
a certificate every browser already trusts.

1. Install Tailscale on the Hilde server and on each phone or computer you will
   use, and sign them in to the same account.
2. In the Tailscale admin console, under **DNS**, turn on **MagicDNS** and
   **HTTPS Certificates**. The server now has a name such as
   `hilde-server.your-tailnet.ts.net` (`tailscale status` shows it).
3. On the server, get its certificate and give it to the user Hilde runs as:

   ```bash
   mkdir -p ~/.hilde/tailscale && cd ~/.hilde/tailscale
   sudo tailscale cert hilde-server.your-tailnet.ts.net
   sudo chown "$USER" hilde-server.your-tailnet.ts.net.*
   chmod 600 hilde-server.your-tailnet.ts.net.key
   ```

4. Start Hilde with the certificate, keeping your other options:

   ```bash
   python audiobook_tts_web.py --host 0.0.0.0 --port 8800 \
     --tls-cert ~/.hilde/tailscale/hilde-server.your-tailnet.ts.net.crt \
     --tls-key ~/.hilde/tailscale/hilde-server.your-tailnet.ts.net.key \
     --live-voice http://127.0.0.1:9941
   ```

   Running Hilde as a service, add the two options to its `ExecStart`
   ([Start the server](configuration.md#start-the-server)).
5. On the phone, with the Tailscale app connected, open
   `https://hilde-server.your-tailnet.ts.net:8800`. Nothing needs installing:
   the certificate is a public one. The first **Talk with Hilde**, or the first
   tap with **Listen for “Hilde”** on, asks for the microphone.
6. Tailscale's certificates last 90 days. Run step 3 again before then (a
   monthly timer will do) and restart Hilde.

Hilde's own certificate (`--https`, see [HTTPS](../README.md#https)) is made
for its names and addresses on your home network, not for the Tailscale name,
so through Tailscale use Tailscale's. What you say goes from the phone,
encrypted by Tailscale, to the speech server on your Hilde server; nothing of
it goes to a cloud, and a cloud model in Ask Hilde gets only the words heard.
To change settings from the phone, approve it once as under
[Manage from other devices](configuration.md#manage-from-other-devices).

## On another computer, on its CPU

The speech server needs no GPU: on a CPU it began speaking in 0.08 s and heard
a turn in 0.12–0.15 s (measured on 6 Nov with 16 threads of a 24-core
workstation), as fast as on a GPU. It needs about 1.4 GB of disk and 7 GB of
memory. A laptop is slower; try it.

1. Install Hilde on that computer ([Installation](installation.md); one command
   on Linux or macOS).
2. Get llama.cpp v0.8.0 or newer, which has LFM3-Audio: a prebuilt download from
   [its releases](https://github.com/ggml-org/llama.cpp/releases), or build
   `llama-server` from its source:

   ```bash
   cmake -B build
   cmake --build build --config Release -t llama-server
   ```

3. Get LFM3-Audio-2B as two GGUF files, the model and its `mmproj` (the audio
   encoder and decoder): `LFM3-Audio-2B-Q4_K_M.gguf` (1.0 GB) and
   `mmproj-LFM3-Audio-2B-Q8_0.gguf` (0.3 GB). llama.cpp's
   `tools/tts/README.md` says how to make them from LiquidAI's checkpoint with
   `convert_hf_to_gguf.py`, if you have the checkpoint rather than the files.
4. Start the speech server on the CPU:

   ```bash
   CUDA_VISIBLE_DEVICES= llama-server \
     -m LFM3-Audio-2B-Q4_K_M.gguf -mm mmproj-LFM3-Audio-2B-Q8_0.gguf \
     --no-mmproj-offload --host 127.0.0.1 --port 9941 -ngl 0 -np 3 -t 8
   ```

   `-ngl 0` keeps the model off any GPU (`CUDA_VISIBLE_DEVICES=` hides an
   NVIDIA GPU from it too); `-t` is the number of CPU threads (about the
   number of cores); `-np 3` lets Talk, listening for “Hilde” and Read aloud
   be heard at once. Check it with `curl http://127.0.0.1:9941/health`, which
   answers `{"status":"ok"}`.
5. Start Hilde with `--live-voice http://127.0.0.1:9941`. **Talk with Hilde**
   then shows in Ask Hilde; on the computer itself the address can stay
   `http://localhost:8800`, which browsers let use the microphone.
6. To keep the speech server running on Linux, a user service
   (`~/.config/systemd/user/hilde-speech.service`), with your own paths:

   ```ini
   [Unit]
   Description=Hilde's speech server (LFM3-Audio on the CPU)

   [Service]
   Environment=CUDA_VISIBLE_DEVICES=
   ExecStart=/path/to/llama-server -m /path/to/LFM3-Audio-2B-Q4_K_M.gguf -mm /path/to/mmproj-LFM3-Audio-2B-Q8_0.gguf --no-mmproj-offload --host 127.0.0.1 --port 9941 -ngl 0 -np 3 -t 8
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=default.target
   ```

   then `systemctl --user enable --now hilde-speech.service` (and
   `loginctl enable-linger "$USER"` so it starts without a login).

Talk, listening for “Hilde” and Read aloud need nothing more. Answers need a
model for Ask Hilde: a cloud one (connected under **Settings › Providers**) or a
local model server. Making new audiobooks narrates with Qwen3-TTS, which is
slow without a GPU: make them on a computer with one, or add one as a
[narration machine](ssh-workers.md).

The speech server has no login either. To use one speech server from another
computer, start it with `--host 0.0.0.0` only on a network you trust (or
Tailscale), and point Hilde at it with `--live-voice http://that-computer:9941`.
