# Synchronized Multi-Language Countdown Bots

Three Discord bots (English / Chinese / Japanese) run from **one program**.
When someone in a voice channel types `!countdown 10`, all three bots join
**that same voice channel** and count down together, each in its language.
Everyone mutes the two bots they don't want to hear. Because the bots share
one clock, every number starts at the same instant.

## Why three bot accounts?

One bot account produces one voice. To have three languages that people can
individually mute, each language needs its own bot account. They all run from
this single script, which is what keeps them in sync.

## Setup

### A. Create three bot accounts in Discord (do this 3 times)

1. https://discord.com/developers/applications → **New Application** → name it
   clearly, e.g. **Countdown 🇬🇧 English**, **Countdown 🇨🇳 中文**,
   **Countdown 🇯🇵 日本語** (the name is what people see when muting).
2. Left sidebar → **Bot** → **Reset Token** → copy the token into
   `config.json` (step C). You only see it once.
3. **For the first (English) bot only:** on the same Bot page, under
   *Privileged Gateway Intents*, enable **Message Content Intent** → Save.
   (It's the one that reads chat commands. Enabling it on all three is fine.)
4. Sidebar → **OAuth2** → **URL Generator**. Scope: `bot`. Permissions:
   View Channels, Send Messages, Add Reactions, Connect, Speak.
5. Copy the URL, open it, choose your server, Authorize.

### B. Install and configure

Requires **Python 3.9** (pinned; see `.python-version`), **ffmpeg**, and
**libopus** (Discord's voice codec).

```bash
# macOS:   brew install ffmpeg opus python@3.9
# Debian:  sudo apt install ffmpeg libopus0 python3.9 python3.9-venv
# Windows: winget install ffmpeg      (opus comes bundled with discord.py[voice])
#          Python 3.9: https://www.python.org/downloads/release/python-3913/
```

Then run the setup script once. It finds Python 3.9, creates a `.venv`,
installs the requirements, and copies `config.example.json` to `config.json`:

```bash
./setup.sh          # macOS / Linux
setup.bat           # Windows
```

(If you use pyenv or uv, they read `.python-version` and pick 3.9 for you.)

Edit `config.json` and paste the three tokens.

### C. Run

```bash
.venv/bin/python bots.py            # Windows: .venv\Scripts\python bots.py
```

The script refuses to start on any Python other than 3.9, so if you see a
version error, re-run the setup script.

You should see three "logged in as …" lines. Then, while in a voice channel,
type in any text channel:

| You type             | Result                                              |
|----------------------|-----------------------------------------------------|
| `!countdown 10`      | all 3 bots join your channel and count 10 → 1       |
| `!countdown 20 to 5` | 20 → 5                                              |
| `!stop`              | stop a running countdown (also `!countdown stop`)   |

The bots leave when the countdown ends. Keep the terminal open; the bots only
run while the script does.

### D. Tell your users: mute the other two

In the voice channel, right-click a bot → **Mute**. Discord remembers this
per user, per server, so each person does it once. Someone who wants Japanese
mutes the English and Chinese bots and only hears Japanese.

## Config reference

| key                    | meaning                                                   |
|------------------------|-----------------------------------------------------------|
| `prefix`               | chat command prefix                                       |
| `stop_prefix`          | command that stops a running countdown (default `!stop`)  |
| `interval_seconds`     | gap between numbers                                       |
| `lead_in_seconds`      | pause after all bots connect before the first number      |
| `max_steps`            | safety cap on countdown length                            |
| `trim_leading_silence` | cut silence from the start of each TTS clip (keeps languages aligned) |
| `opus_path`            | leave empty normally; full path to libopus if it isn't auto-found |
| `bots[].tts_lang`      | Google TTS code: `en`, `zh` (Mandarin), `zh-TW`, `ja`, `ko`, `es`, … |

Add a fourth language by creating another bot account and adding an entry.

## How "same time" works (and its limits)

- All bots run in one process on one monotonic clock. Each word is started at
  `t0 + i × interval`, so the send times are identical to within a millisecond.
- Discord's voice servers then relay the audio to each listener, who hears it
  after their own network latency + jitter buffer - typically within
  ~50–150 ms of each other. No bot can remove that step.
- TTS clips have leading silence trimmed so the voiced onset of each number
  lands on the beat in every language, even though the words differ in length.