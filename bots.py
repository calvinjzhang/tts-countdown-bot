"""
Multi-language synchronized Discord countdown - single voice channel.

One process logs in as several bot accounts (one per language). When a user
in a voice channel types a countdown request, ALL bots join that same voice
channel and count down together, each in its own language. Listeners mute
the bots they don't want to hear (right-click bot -> Mute).

All bots share one clock, so every number starts at the same instant.

Usage (Python 3.9 - run ./setup.sh or setup.bat once first):
    .venv/bin/python bots.py                # uses config.json
    .venv/bin/python bots.py other.json

Chat commands (the FIRST bot in config.json listens for these):
    !cd 10          -> 10 ... 1
    !cd 10-1        -> same
    !cd 20 to 5     -> 20 ... 5
    !cd stop        -> stop the running countdown (also: !stop)
    !cd buttons     -> post the panel of one-click countdown buttons

The panel has the fixed buttons from config (default: 10) plus the three most
recently used countdowns, newest first.

Every command (and every button click) re-posts the button panel as the newest
message. Only one countdown runs at a time; stop it before starting another.
"""

from __future__ import annotations  # allows "X | None" hints on Python 3.9
import asyncio
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

if sys.version_info[:2] != (3, 9):
    sys.exit(
        f"This bot is pinned to Python 3.9 (you are running {sys.version.split()[0]}).\n"
        "Run ./setup.sh (macOS/Linux) or setup.bat (Windows) to create a 3.9 venv, then:\n"
        "  .venv/bin/python bots.py     (Windows: .venv\\Scripts\\python bots.py)"
    )

import discord
from gtts import gTTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("countdown")

HERE = Path(__file__).parent
CONFIG_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "config.json"
if not CONFIG_PATH.exists():
    raise SystemExit(f"{CONFIG_PATH.name} not found. Copy config.example.json to config.json and fill in your tokens.")
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

PREFIX: str = CONFIG.get("prefix", "!cd")
STOP_PREFIX: str = CONFIG.get("stop_prefix", "!stop")
STOP_WORDS = ("stop", "cancel", "exit", "pause")
PANEL_WORDS = ("buttons", "button", "panel")
BUTTONS: list = CONFIG.get("buttons", [10])           # fixed buttons; each entry: 10, or a range like "20 to 5"
RECENT_COUNT: int = int(CONFIG.get("recent_buttons", 3))   # extra buttons for the most recently used countdowns
RECENT_PATH = HERE / "recent.json"                    # remembered across restarts
PANEL_TEXT = "Join a voice channel, then click to start a countdown:"
BUTTON_ID = "cd"                                      # custom_id prefix: "cd:<start>:<end>" / "cd:stop"
INTERVAL: float = float(CONFIG.get("interval_seconds", 1.0))
LEAD_IN: float = float(CONFIG.get("lead_in_seconds", 2.0))
MAX_SECONDS: float = float(CONFIG.get("max_seconds", 600))   # longest countdown allowed
TRIM_SILENCE: bool = bool(CONFIG.get("trim_leading_silence", True))

OPUS_PATH: str = CONFIG.get("opus_path", "")

CACHE_DIR = HERE / "tts_cache"
CACHE_DIR.mkdir(exist_ok=True)


def load_opus() -> None:
    """Make sure libopus is loaded. discord.py often can't find it on macOS by itself."""
    if discord.opus.is_loaded():
        return
    import ctypes.util
    import glob

    candidates: list[str] = []
    if OPUS_PATH:
        candidates.append(OPUS_PATH)
    found = ctypes.util.find_library("opus")
    if found:
        candidates.append(found)
    candidates += glob.glob("/opt/homebrew/lib/libopus*.dylib")      # macOS Apple Silicon (Homebrew)
    candidates += glob.glob("/usr/local/lib/libopus*.dylib")         # macOS Intel (Homebrew)
    candidates += glob.glob("/opt/local/lib/libopus*.dylib")         # macOS MacPorts
    candidates += glob.glob("/usr/lib/*/libopus.so*")                # Debian/Ubuntu
    candidates += glob.glob("/usr/lib64/libopus.so*")                # Fedora
    candidates += ["libopus.so.0", "libopus-0.dll", "opus.dll", "opus"]

    for c in candidates:
        try:
            discord.opus.load_opus(c)
            log.info("Loaded opus from %s", c)
            return
        except Exception:
            continue

    raise SystemExit(
        "libopus not found - it's required for voice.\n"
        "  macOS:  brew install opus\n"
        "  Debian: sudo apt install libopus0\n"
        "  Windows: pip install discord.py[voice] (bundles it)\n"
        "If it's installed somewhere unusual, set \"opus_path\" in config.json to the library file."
    )

RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-|–|to|\.\.)\s*(\d+)\s*$", re.IGNORECASE)
SINGLE_RE = re.compile(r"^\s*(\d+)\s*$")


# ---------------------------------------------------------------- parsing
def parse_range(text: str) -> tuple[int, int] | None:
    m = RANGE_RE.match(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return max(a, b), min(a, b)
    m = SINGLE_RE.match(text)
    if m:
        return int(m.group(1)), 1
    return None


def extract_request(content: str) -> tuple[int, int] | None:
    s = content.strip()
    if s.lower().startswith(PREFIX.lower()):
        return parse_range(s[len(PREFIX):])
    return None


def is_stop_request(content: str) -> bool:
    """`!stop` or `!cd stop` (also cancel/exit/pause)."""
    s = content.strip().lower()
    if s == STOP_PREFIX.lower():
        return True
    if s.startswith(PREFIX.lower()):
        return s[len(PREFIX):].strip() in STOP_WORDS
    return False


def is_panel_request(content: str) -> bool:
    """`!cd buttons` (also button/panel)."""
    s = content.strip().lower()
    return s.startswith(PREFIX.lower()) and s[len(PREFIX):].strip() in PANEL_WORDS


def fixed_ranges() -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for entry in BUTTONS[:20]:                        # Discord allows 25 buttons; keep room for recents + Stop
        rng = parse_range(str(entry))
        if rng is None:
            log.warning("Ignoring invalid entry in \"buttons\": %r", entry)
        elif rng not in ranges:
            ranges.append(rng)
    return ranges


def build_panel(recent: list[tuple[int, int]] = ()) -> discord.ui.View:
    """The fixed buttons, then the most recently used countdowns (newest first), then Stop.

    Buttons carry their range in the custom_id, so old panels keep working after a restart.
    """
    view = discord.ui.View(timeout=None)
    ranges = fixed_ranges()
    fixed = len(ranges)
    ranges += [r for r in recent if r not in ranges]
    for i, (start, end) in enumerate(ranges):
        label = f"CD {start}" if end == 1 else f"CD {start} → {end}"
        view.add_item(discord.ui.Button(
            label=label, emoji="⏱️", custom_id=f"{BUTTON_ID}:{start}:{end}",
            style=discord.ButtonStyle.primary if i < fixed else discord.ButtonStyle.success,
        ))
    view.add_item(discord.ui.Button(
        label="Stop", emoji="🛑", style=discord.ButtonStyle.danger, custom_id=f"{BUTTON_ID}:stop",
    ))
    return view


class UserError(Exception):
    """A request we refuse, with a message meant for the person who asked."""


# ---------------------------------------------------------------- audio
def _trim_leading_silence(path: Path) -> None:
    """Cut silence before the voice starts so all languages begin on the beat."""
    tmp = path.with_suffix(".trim.mp3")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
        "-af", "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=30)
        tmp.replace(path)
    except Exception as e:  # ffmpeg missing or failed: keep the untrimmed clip
        log.warning("Could not trim %s: %s", path.name, e)
        tmp.unlink(missing_ok=True)


def _make_clip(text: str, lang: str, path: Path) -> None:
    gTTS(text=text, lang=lang).save(str(path))
    if TRIM_SILENCE:
        _trim_leading_silence(path)


async def ensure_clip(text: str, lang: str) -> Path:
    safe = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE).strip("_") or "word"
    path = CACHE_DIR / f"{lang}_{safe}.mp3"
    if not path.exists():
        await asyncio.to_thread(_make_clip, text, lang, path)
    return path


def check_dave_versions() -> None:
    """Warn about the known 'speaking ring but silence' combination (davey >= 0.2 with discord.py 2.7.x)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        davey_ver = version("davey")
    except PackageNotFoundError:
        log.warning("'davey' is not installed - Discord voice requires it (pip install 'davey<0.2').")
        return
    major, minor = (int(x) for x in davey_ver.split(".")[:2])
    if (major, minor) >= (0, 2):
        log.warning(
            "davey %s detected. With discord.py %s this is known to send audio nobody can hear "
            "(bot shows as speaking, silence for listeners). Fix: pip install \"davey<0.2\"",
            davey_ver, discord.__version__,
        )


# ---------------------------------------------------------------- one bot
class LangBot:
    def __init__(self, cfg: dict, leader: bool):
        self.name: str = cfg["name"]
        self.token: str = cfg["token"]
        self.lang: str = cfg["tts_lang"]
        self.leader = leader

        intents = discord.Intents.default()
        intents.voice_states = True
        if leader:
            intents.message_content = True  # only the leader reads chat
        self.client = discord.Client(intents=intents)
        self.client.event(self.on_ready)

    async def on_ready(self) -> None:
        log.info("[%s] logged in as %s", self.name, self.client.user)

    def words(self, start: int, end: int) -> list[str]:
        return [str(n) for n in range(start, end - 1, -1)]

    async def prepare_clips(self, start: int, end: int) -> list[Path]:
        return [await ensure_clip(w, self.lang) for w in self.words(start, end)]

    async def connect(self, channel_id: int) -> discord.VoiceClient:
        """Join the channel using THIS bot's own view of the server."""
        channel = self.client.get_channel(channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise RuntimeError(f"{self.name} can't see voice channel {channel_id} - is it in the server with Connect permission?")
        vc = channel.guild.voice_client
        if vc is None:
            return await channel.connect()
        if vc.channel != channel:
            await vc.move_to(channel)
        return vc

    async def play_schedule(self, vc: discord.VoiceClient, clips: list[Path], t0: float) -> None:
        """Start clip i exactly at t0 + i*INTERVAL (shared monotonic clock)."""
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()

        def after_last(err: Exception | None) -> None:
            if err:
                log.error("[%s] playback error: %s", self.name, err)
            loop.call_soon_threadsafe(finished.set)

        last = len(clips) - 1
        try:
            for i, clip in enumerate(clips):
                delay = (t0 + i * INTERVAL) - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if vc.is_playing():           # previous word ran long: cut it so we stay on the beat
                    vc.stop()
                vc.play(discord.FFmpegPCMAudio(str(clip)), after=after_last if i == last else None)
            await finished.wait()
        except asyncio.CancelledError:        # !stop: cut the current word immediately
            if vc.is_playing():
                vc.stop()
            raise


# ---------------------------------------------------------------- coordinator
class Hub:
    def __init__(self, bots: list[LangBot]):
        self.bots = bots
        self.task: asyncio.Task | None = None            # the ONE countdown allowed at a time
        self.stopping = False                            # stop requested, bots still leaving
        self.recent: list[tuple[int, int]] = self._load_recent()   # recently used countdowns, newest first
        self.panels: dict[int, discord.Message] = {}     # text channel id -> newest button panel
        self.panel_locks: dict[int, asyncio.Lock] = {}
        bots[0].client.event(self.on_message)
        bots[0].client.event(self.on_interaction)

    # ---- recently used countdowns (the extra buttons)
    @staticmethod
    def _load_recent() -> list[tuple[int, int]]:
        try:
            data = json.loads(RECENT_PATH.read_text(encoding="utf-8"))
            return [(int(a), int(b)) for a, b in data][:RECENT_COUNT]
        except FileNotFoundError:
            return []
        except Exception as e:
            log.warning("Ignoring unreadable %s: %s", RECENT_PATH.name, e)
            return []

    def remember(self, rng: tuple[int, int]) -> None:
        """Move `rng` to the front of the recent list (typed or clicked). Fixed buttons and over-long ones don't count."""
        if rng in fixed_ranges() or (rng[0] - rng[1] + 1) * INTERVAL > MAX_SECONDS:
            return
        updated = ([rng] + [r for r in self.recent if r != rng])[:RECENT_COUNT]
        if updated == self.recent:
            return
        self.recent = updated
        try:
            RECENT_PATH.write_text(json.dumps(self.recent), encoding="utf-8")
        except OSError as e:
            log.warning("Could not save %s: %s", RECENT_PATH.name, e)

    # ---- shared by chat commands and buttons
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def launch(self, member, start: int, end: int) -> asyncio.Task:
        """Validate and start a countdown in `member`'s voice channel. Raises UserError if refused."""
        seconds = (start - end + 1) * INTERVAL
        if seconds > MAX_SECONDS:
            raise UserError(f"That countdown would take {seconds:g} seconds - the max is {MAX_SECONDS:g}.")
        vs = getattr(member, "voice", None)
        if vs is None or vs.channel is None:
            raise UserError("Join a voice channel first.")
        if self.busy():
            if self.stopping:
                raise UserError("The last countdown is still stopping - try again in a second.")
            raise UserError(f"A countdown is already running. Stop it first (`{STOP_PREFIX}` or the Stop button).")
        # Own task so a stop request can cancel it without killing the event handler.
        self.task = asyncio.create_task(self.run(vs.channel.id, start, end))
        return self.task

    async def finish(self, task: asyncio.Task) -> str | None:
        """Wait for a countdown to end. Returns a message for the requester if it didn't end normally."""
        try:
            await task
        except asyncio.CancelledError:
            log.info("Countdown stopped by user")
            return "Countdown stopped."
        except Exception as e:
            log.exception("Countdown failed")
            return f"Countdown failed: {e}"
        finally:
            if self.task is task:
                self.task = None
                self.stopping = False
        return None

    def stop(self) -> bool:
        if not self.busy():
            return False
        if not self.stopping:        # cancel only once: a second cancel would interrupt the bots leaving
            self.stopping = True
            self.task.cancel()
        return True

    async def refresh_panel(self, channel, old: discord.Message | None = None) -> None:
        """Re-post the button panel so it is the newest message; remove the previous copy."""
        lock = self.panel_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            stale = {m.id: m for m in (old, self.panels.pop(channel.id, None)) if m is not None}
            for m in stale.values():
                try:
                    await m.delete()
                except discord.HTTPException:
                    pass                                  # already gone
            try:
                self.panels[channel.id] = await channel.send(PANEL_TEXT, view=build_panel(self.recent))
            except discord.HTTPException as e:
                log.warning("Could not post the button panel in #%s: %s", channel, e)

    # ---- chat commands
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        if is_stop_request(message.content):
            if self.stop():
                await message.add_reaction("🛑")
            else:
                await message.reply("No countdown is running.", mention_author=False)
            await self.refresh_panel(message.channel)
            return

        if is_panel_request(message.content):
            await self.refresh_panel(message.channel)
            return

        rng = extract_request(message.content)
        if rng is None:
            return
        self.remember(rng)
        try:
            task = self.launch(message.author, *rng)
        except UserError as e:
            await message.reply(str(e), mention_author=False)
            await self.refresh_panel(message.channel)
            return
        try:
            await message.add_reaction("⏱️")
        except discord.HTTPException:
            pass
        await self.refresh_panel(message.channel)
        outcome = await self.finish(task)
        if outcome:
            await message.reply(outcome, mention_author=False)
            await self.refresh_panel(message.channel)     # keep the panel below our reply

    # ---- buttons
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component or interaction.guild is None:
            return
        parts = str((interaction.data or {}).get("custom_id", "")).split(":")
        if parts[0] != BUTTON_ID:
            return

        async def answer(text: str) -> None:
            # Must answer within 3s or Discord shows "interaction failed", so this goes before the re-post.
            try:
                await interaction.response.send_message(text, ephemeral=True)
            except discord.HTTPException as e:
                log.warning("Could not acknowledge button press: %s", e)
            await self.refresh_panel(interaction.channel, old=interaction.message)

        if parts[1:] == ["stop"]:
            await answer("🛑 Countdown stopped." if self.stop() else "No countdown is running.")
            return
        try:
            start, end = int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            return
        self.remember((start, end))
        try:
            task = self.launch(interaction.user, start, end)
        except UserError as e:
            await answer(str(e))
            return
        await answer(f"⏱️ Counting down {start} → {end}")
        outcome = await self.finish(task)
        if outcome and not outcome.endswith("stopped."):   # whoever pressed Stop was already told
            try:
                await interaction.followup.send(outcome, ephemeral=True)
            except discord.HTTPException:
                pass

    async def run(self, channel_id: int, start: int, end: int) -> None:
        try:
            # 1. Generate/cache all clips for all languages (network work happens here, not during the count).
            clip_lists = await asyncio.gather(*(b.prepare_clips(start, end) for b in self.bots))
            # 2. Every bot joins the same channel; wait until ALL are connected.
            vcs = await asyncio.gather(*(b.connect(channel_id) for b in self.bots))
            # 3. One shared start instant on the process-wide monotonic clock.
            t0 = asyncio.get_running_loop().time() + LEAD_IN
            log.info("Countdown %d->%d starting in %.1fs with %d bots", start, end, LEAD_IN, len(self.bots))
            await asyncio.gather(
                *(b.play_schedule(vc, clips, t0) for b, vc, clips in zip(self.bots, vcs, clip_lists))
            )
        finally:
            # Every bot must be fully out before this task ends, even on a stop: the next
            # countdown may start right away and must not have its connection torn down by us.
            leave = asyncio.ensure_future(self._leave(channel_id))
            try:
                await asyncio.shield(leave)
            except asyncio.CancelledError:
                await leave
                raise

    async def _leave(self, channel_id: int) -> None:
        await asyncio.sleep(0.3)
        vcs = []
        for b in self.bots:
            channel = b.client.get_channel(channel_id)
            vc = channel.guild.voice_client if channel is not None else None
            if vc is not None:
                vcs.append(vc)
        await asyncio.gather(*(vc.disconnect(force=True) for vc in vcs), return_exceptions=True)


# ---------------------------------------------------------------- main
async def main() -> None:
    bot_cfgs = CONFIG.get("bots", [])
    if not bot_cfgs:
        raise SystemExit("config.json has no bots.")
    for c in bot_cfgs:
        if not c.get("token") or "paste" in c["token"].lower():
            raise SystemExit(f"Bot '{c.get('name')}' has no token set in config.json.")

    load_opus()  # fail fast with a clear message instead of mid-countdown
    check_dave_versions()
    bots = [LangBot(c, leader=(i == 0)) for i, c in enumerate(bot_cfgs)]
    Hub(bots)
    log.info("Starting %d bots. Commands: %r / %r (listened for by %s)", len(bots), PREFIX, STOP_PREFIX, bots[0].name)
    await asyncio.gather(*(b.client.start(b.token) for b in bots))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass