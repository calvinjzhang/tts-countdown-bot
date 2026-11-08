"""
Multi-language synchronized Discord countdown - single voice channel.

One process logs in as several bot accounts (one per language). When a user
in a voice channel types a countdown request, ALL bots join that same voice
channel and count down together, each in its own language. Listeners mute
the bots they don't want to hear (right-click bot -> Mute).

All bots share one clock, so every number starts at the same instant.

Usage:
    python bots.py                # uses config.json
    python bots.py other.json

Chat commands (the FIRST bot in config.json listens for these):
    !countdown 10          -> 10 ... 1
    !countdown 10-1        -> same
    !countdown 20 to 5     -> 20 ... 5
    !countdown stop        -> stop the running countdown (also: !stop)
"""

import asyncio
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import discord
from gtts import gTTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("countdown")

HERE = Path(__file__).parent
CONFIG_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "config.json"
if not CONFIG_PATH.exists():
    raise SystemExit(f"{CONFIG_PATH.name} not found. Copy config.example.json to config.json and fill in your tokens.")
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

PREFIX: str = CONFIG.get("prefix", "!countdown")
STOP_PREFIX: str = CONFIG.get("stop_prefix", "!stop")
STOP_WORDS = ("stop", "cancel", "exit", "pause")
INTERVAL: float = float(CONFIG.get("interval_seconds", 1.0))
LEAD_IN: float = float(CONFIG.get("lead_in_seconds", 2.0))
MAX_STEPS: int = int(CONFIG.get("max_steps", 60))
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
    """`!stop` or `!countdown stop` (also cancel/exit/pause)."""
    s = content.strip().lower()
    if s == STOP_PREFIX.lower():
        return True
    if s.startswith(PREFIX.lower()):
        return s[len(PREFIX):].strip() in STOP_WORDS
    return False


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
        self.running: dict[int, asyncio.Task] = {}   # guild id -> countdown task
        bots[0].client.event(self.on_message)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        gid = message.guild.id

        if is_stop_request(message.content):
            task = self.running.get(gid)
            if task is None or task.done():
                await message.reply("No countdown is running.", mention_author=False)
            else:
                task.cancel()
                await message.add_reaction("🛑")
            return

        rng = extract_request(message.content)
        if rng is None:
            return
        start, end = rng
        steps = start - end + 1
        if steps > MAX_STEPS:
            await message.reply(f"That's {steps} numbers - max is {MAX_STEPS}.", mention_author=False)
            return

        vs = message.author.voice
        if vs is None or vs.channel is None:
            await message.reply("Join a voice channel first.", mention_author=False)
            return

        if gid in self.running and not self.running[gid].done():
            await message.reply(f"A countdown is already running. `{STOP_PREFIX}` to stop it.", mention_author=False)
            return

        # Run in its own task so `!stop` can cancel it without killing this event handler.
        task = asyncio.create_task(self.run(vs.channel.id, start, end))
        self.running[gid] = task
        try:
            await message.add_reaction("⏱️")
            await task
        except asyncio.CancelledError:
            log.info("Countdown stopped by user")
            await message.reply("Countdown stopped.", mention_author=False)
        except Exception as e:
            log.exception("Countdown failed")
            await message.reply(f"Countdown failed: {e}", mention_author=False)
        finally:
            if self.running.get(gid) is task:
                del self.running[gid]

    async def run(self, channel_id: int, start: int, end: int) -> None:
        # 1. Generate/cache all clips for all languages (network work happens here, not during the count).
        clip_lists = await asyncio.gather(*(b.prepare_clips(start, end) for b in self.bots))
        # 2. Every bot joins the same channel; wait until ALL are connected.
        vcs = await asyncio.gather(*(b.connect(channel_id) for b in self.bots))
        try:
            # 3. One shared start instant on the process-wide monotonic clock.
            t0 = asyncio.get_running_loop().time() + LEAD_IN
            log.info("Countdown %d->%d starting in %.1fs with %d bots", start, end, LEAD_IN, len(self.bots))
            await asyncio.gather(
                *(b.play_schedule(vc, clips, t0) for b, vc, clips in zip(self.bots, vcs, clip_lists))
            )
        finally:
            # Shielded so a cancel (!stop) still lets every bot leave the channel cleanly.
            await asyncio.shield(self._leave(vcs))

    async def _leave(self, vcs) -> None:
        await asyncio.sleep(0.3)
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