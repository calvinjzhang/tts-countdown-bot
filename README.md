# Synchronized Multi-Language Countdown Bots

Three Discord bots (English / Chinese / Japanese) run from **one program**.
When someone in a voice channel types `!cd 10`, all three bots join
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

| You type      | Result                                          |
|---------------|-------------------------------------------------|
| `!cd 10`      | all 3 bots join your channel and count 10 → 1   |
| `!cd 20 to 5` | 20 → 5                                          |
| `!stop`       | stop a running countdown (also `!cd stop`)      |
| `!cd buttons` | post the panel of one-click buttons (see below) |

Only **one countdown runs at a time**. To start a different one, stop the
current one first. A countdown can last at most 600 seconds (`max_seconds`).

The bots stay in the voice channel after a countdown ends or is stopped, and
move to the requester's channel if the next countdown is started elsewhere.
Keep the terminal open; the bots only run while the script does.

#### Buttons

The bot posts a panel with **CD 10**, then buttons for the **three most
recently used countdowns** (newest first - e.g. after `!cd 45` it shows
**CD 45**), and **Stop**. Anyone in a voice channel can click one
to start that countdown in their channel - no typing.

The panel is re-posted as the newest message (and the old copy removed) every
time a button is clicked or any `!cd` / `!stop` command is used, so it never
gets buried by chat. Buttons on an old panel keep working after a restart.

Change the fixed buttons with `"buttons"` in `config.json`, e.g.
`"buttons": [10, 30, "20 to 5"]`, and how many recent ones are shown with
`"recent_buttons"`. A countdown counts as "used" when it is typed or its button
is clicked. The recent list is saved in `recent.json`, so it survives restarts.

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
| `max_seconds`          | longest countdown allowed, in seconds (default 600)       |
| `trim_leading_silence` | cut silence from the start of each TTS clip (keeps languages aligned) |
| `speech_rate`          | how fast each number is spoken (default `1.2` = 20% faster; `1` = normal). The gap between numbers is unchanged |
| `zh_short_numbers`     | Chinese reads digits above 20: 44 → 四四 instead of 四十四 (default `true`) |
| `track_cache_size`     | how many recent countdowns keep their finished audio in memory so repeats start without rebuilding (default `10`; `0` = off) |
| `opus_path`            | leave empty normally; full path to libopus if it isn't auto-found |
| `bots[].tts_lang`      | Google TTS code: `en`, `zh` (Mandarin), `zh-TW`, `ja`, `ko`, `es`, … |

Add a fourth language by creating another bot account and adding an entry.

## How "same time" works (and its limits)

- All bots run in one process on one monotonic clock. Before the count starts,
  each bot's whole countdown is built as one continuous audio track with number
  `i` placed exactly `i × interval` in, and all tracks start at the same instant.
  Nothing is launched or decoded mid-countdown, so the gaps are exact.
- Discord's voice servers then relay the audio to each listener, who hears it
  after their own network latency + jitter buffer - typically within
  ~50–150 ms of each other. No bot can remove that step.
- TTS clips have leading silence trimmed so the voiced onset of each number
  lands on the beat in every language, even though the words differ in length.