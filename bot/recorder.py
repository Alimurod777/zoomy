import asyncio
import json
import logging
import os
import re
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

from playwright.async_api import async_playwright, Page
from auth_store import auth_store
from meeting_state import classify_meeting, SNAPSHOT_JS
from zoom_profile import profile_name_from_page

logger = logging.getLogger(__name__)

class MeetingJoinError(RuntimeError):
    pass


class DesktopRequired(MeetingJoinError):
    pass

RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/recordings"))
GUEST_NAME = os.environ.get("GUEST_NAME", "zoomy.ircham.dev")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# ── Signed-in join ────────────────────────────────────────────────────────────
# Playwright storage_state JSON holding a logged-in zoom.us session, produced by
# `python3 bot/zoom_login.py` and mounted read-only. Hosts can require every
# participant to be signed in with a Zoom account; without this the bot joins
# anonymously and is turned away at the sign-in wall.
#
# storage_state (a cookie snapshot loaded per browser context) is used rather
# than a persistent Chrome profile directory because Chrome locks a profile to
# one process — a shared profile would cap the bot at one concurrent session,
# defeating MAX_CONCURRENT_SESSIONS.
ZOOM_STATE_PATH = Path(os.environ.get("ZOOM_STATE_PATH", "/secrets/zoom_state.json"))

# ── Audio-wait settings ───────────────────────────────────────────────────────
# How long (seconds) to wait for audio before starting the recording anyway.
# Set to 0 to disable the feature and start FFmpeg immediately on join.
AUDIO_WAIT_TIMEOUT = int(os.environ.get("AUDIO_WAIT_TIMEOUT") or "5")
# Minimum RMS level (out of 32 767) considered "audio present".
AUDIO_RMS_THRESHOLD = int(os.environ.get("AUDIO_RMS_THRESHOLD", "100"))
_AUDIO_SAMPLE_RATE = 16_000   # Hz — enough for level detection, low CPU
_AUDIO_CHUNK_SECS = 0.25      # read window per iteration

# ── Silence-warning settings ──────────────────────────────────────────────────
# Seconds of continuous silence during recording before Telegram is notified.
# Set to 0 to disable.
SILENT_WARN_SECS = int(os.environ.get("SILENT_WARN_SECS", "60"))
# Interval (seconds) between silence reminders in "alert when audio returns" mode.
SILENT_SNOOZE_SECS = int(os.environ.get("SILENT_SNOOZE_SECS", "300"))

# ── Breakout-room settings ────────────────────────────────────────────────────
# When the host invites the bot to a breakout room, Telegram is prompted first.
# If nobody answers within this many seconds, the bot auto-joins. Set to 0 to
# join immediately without prompting; set to a negative value to ignore invites.
BREAKOUT_AUTOJOIN_SECS = int(os.environ.get("BREAKOUT_AUTOJOIN_SECS", "100"))

Callback = Callable[..., Awaitable[None]]


def parse_zoom_url(url: str) -> tuple[str, str]:
    match = re.search(r"/(?:j|wc)/(\d+)", url)
    if not match:
        raise ValueError("No meeting ID found in Zoom invitation")
    meeting_id = match.group(1)
    params = parse_qs(urlparse(url).query)
    password = params.get("pwd", [""])[0]
    return meeting_id, password


# Query parameters dropped when rewriting a join link for the web client.
# `uname` only prefills the display name, which we set ourselves from
# guest_name — leaving it in would override the name the user picked.
_DROPPED_JOIN_PARAMS = ("uname",)


def build_web_client_url(url: str) -> str:
    """Rewrite a Zoom join link into a web-client link (/wc/<id>/join).

    Every query parameter except `uname` is carried across verbatim. `tk`
    matters most: it is the token in a host-issued *authentication exception*
    link, and it is what lets a named invitee — including this bot — past the
    "only signed-in participants" wall. Rebuilding the URL from just the
    meeting ID and `pwd` discarded it, so an exception link degraded back into
    an anonymous guest join and was refused.

    The original *.zoom.us host is preserved too: vanity hosts serve the web
    client, and a domain-restricted meeting expects its own host.
    """
    parsed = urlparse(url)
    meeting_id, _ = parse_zoom_url(url)

    host = parsed.netloc.lower()
    if not (host == "zoom.us" or host.endswith(".zoom.us")):
        host = "zoom.us"

    params = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k not in _DROPPED_JOIN_PARAMS
    ]
    web_url = f"https://{host}/wc/{meeting_id}/join"
    if params:
        web_url += "?" + urlencode(params)
    return web_url


# ── Display pool ──────────────────────────────────────────────────────────────

class DisplayPool:
    def __init__(self, base: int = 99, max_slots: int = 5, screen_size: str = "1920x1200"):
        self._base = base
        self._max = max_slots
        self._screen_size = screen_size
        self._lock = asyncio.Lock()
        self._procs: dict[int, subprocess.Popen] = {}  # display_num → Xvfb proc

    @staticmethod
    def _unload_sinks(names):
        result = subprocess.run(['pactl', 'list', 'short', 'modules'], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if any('sink_name='+name in line.split() for name in names):
                subprocess.run(['pactl', 'unload-module', line.split()[0]], capture_output=True)

    async def _ensure_pulseaudio(self) -> None:
        """Restart PulseAudio if it's not responding."""
        check = subprocess.run(["pactl", "info"], capture_output=True)
        if check.returncode != 0:
            logger.warning("PulseAudio not responding — restarting...")
            subprocess.run(
                ["pulseaudio", "--start", "--exit-idle-time=-1", "--log-level=error"],
                capture_output=True,
            )
            await asyncio.sleep(2)
            logger.info("PulseAudio restarted")

    async def acquire(self) -> tuple[int, str, str]:
        """Allocate a display slot. Returns (display_num, ':N', 'virtual_N.monitor')."""
        async with self._lock:
            await self._ensure_pulseaudio()
            if len(self._procs) >= self._max:
                raise RuntimeError(f"No available display slots (max {self._max})")
            for n in range(self._base, self._base + max(20, self._max)):
                if n not in self._procs:
                    lock_file = f"/tmp/.X{n}-lock"
                    if os.path.exists(lock_file):
                        # Another bot or an orphan may still own this X server.
                        # Never remove a live server's lock and reuse its screen.
                        continue
                    proc = subprocess.Popen(
                        [
                            "Xvfb", f":{n}",
                            "-screen", "0", f"{self._screen_size}x24",
                            "-nolisten", "tcp", "-ac", "+extension", "GLX", "+render", "-noreset",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    await asyncio.sleep(1)
                    if proc.poll() is not None:
                        raise RuntimeError(f"Xvfb could not start on :{n}")
                    sink = f"virtual_{n}"
                    try:
                        self._unload_sinks({sink, sink+'_mic'})
                        for name in (sink, sink+'_mic'):
                            subprocess.run(
                                ['pactl', 'load-module', 'module-null-sink', f'sink_name={name}',
                                 f'sink_properties=device.description={name}'],
                                check=True, capture_output=True)
                    except subprocess.CalledProcessError as e:
                        proc.terminate()
                        self._unload_sinks({sink, sink+'_mic'})
                        raise RuntimeError(f"Failed to create audio sink: {e.stderr.decode().strip()}") from e
                    self._procs[n] = proc
                    logger.info("DisplayPool: allocated :%d / %s", n, sink)
                    return n, f":{n}", f"{sink}.monitor"
            raise RuntimeError(f"No available display slots (max {self._max})")

    async def release(self, display_num: int) -> None:
        """Free a display slot and clean up Xvfb + PulseAudio sink."""
        async with self._lock:
            proc = self._procs.pop(display_num, None)
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            sink = f"virtual_{display_num}"
            try:
                self._unload_sinks({sink, sink+'_mic'})
            except Exception as e:
                logger.warning("Could not unload sink %s: %s", sink, e)
            logger.info("DisplayPool: released :%d", display_num)


# Concurrent recording sessions. Each takes an Xvfb display + PulseAudio sink,
# plus a Chrome and an FFmpeg — so this is really a host-resource ceiling. It
# caps how many breakout rooms can be covered at once, one bot per room.
MAX_CONCURRENT_SESSIONS = int(os.environ.get("MAX_CONCURRENT_SESSIONS") or "1")

_display_pool = DisplayPool(max_slots=MAX_CONCURRENT_SESSIONS)


def log_zoom_url(url):
    parsed = urlparse(url)
    return parsed._replace(query='', fragment='').geturl()


def _reserve_recording_dir(prefix: str, datestamp: str) -> Path:
    """Claim a fresh output folder, suffixing _2, _3 … if the name is taken.

    Several bots routinely record one meeting at once (one per breakout room),
    so two live sessions can share a prefix on the same day. They would then
    derive the same output path and both FFmpeg processes — each running with
    -y — would write the same .mp4, destroying each other's recording with no
    error. The same applies to re-recording a name used earlier the same day.

    mkdir(exist_ok=False) is the reservation: it is atomic, so two sessions
    racing here cannot both win the same folder.
    """
    candidate = RECORDINGS_DIR / f"{prefix}_{datestamp}"
    n = 2
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            candidate = RECORDINGS_DIR / f"{prefix}_{datestamp}_{n}"
            n += 1


def _fmt_duration(secs: int) -> str:
    """Format a duration in seconds as e.g. '5m03s' or '1h05m03s'."""
    h, rem = divmod(max(0, int(secs)), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


# Zoom refuses guest joins when the host requires signed-in participants.
# These phrases appear on the resulting sign-in wall (see the "Sign in to join
# this meeting" page). With a valid saved session the bot sails past it; without
# one — or once the saved cookies expire — the wall is fatal and we abort.
_SIGNIN_REQUIRED_PHRASES = (
    "sign in to join this meeting",
    "requires participants to be signed in",
    "host requires participants to be signed",
)

_SIGNIN_HELP_GUEST = (
    "Host requires participants to be signed in with a Zoom account — the bot "
    "joined as a guest and was refused. Fix: give the bot a signed-in Zoom "
    "session (see README → “Signed-in join”), or ask the host for an "
    "authentication-exception link. Recording aborted."
)

_SIGNIN_HELP_EXPIRED = (
    "Zoom rejected the bot's saved sign-in — the session has almost certainly "
    "expired (Zoom cookies last a few weeks). Re-run the login helper "
    "(README → “Signed-in join”) and restart the container. Recording aborted."
)


def _load_zoom_state() -> Optional[str]:
    """Path to the saved Zoom session, or None to join as an anonymous guest.

    A malformed or cookie-less file is treated as absent rather than fatal: a
    guest join still works for the many meetings that permit one, and failing
    the whole recording over a stale secrets file would be worse than joining
    the way the bot always used to.
    """
    if not ZOOM_STATE_PATH.exists():
        logger.info("No Zoom session at %s — joining as guest", ZOOM_STATE_PATH)
        return None
    try:
        data = json.loads(ZOOM_STATE_PATH.read_text(encoding="utf-8"))
        cookies = [c for c in data.get("cookies", []) if "zoom.us" in c.get("domain", "")]
    except Exception as e:
        logger.warning("Zoom session at %s unreadable (%s) — joining as guest",
                       ZOOM_STATE_PATH, e)
        return None
    if not cookies:
        logger.warning("Zoom session at %s has no zoom.us cookies — joining as guest",
                       ZOOM_STATE_PATH)
        return None
    age_days = (time.time() - ZOOM_STATE_PATH.stat().st_mtime) / 86_400
    logger.info(
        "Signed-in join enabled — %d zoom.us cookies, saved %.1f days ago",
        len(cookies), age_days,
    )
    return str(ZOOM_STATE_PATH)


# ── Breakout-invite locator (shared JS) ───────────────────────────────────────
# Locates the host-initiated breakout invite dialog.
#
# Anchors on the "Not Now" button and walks *up* to the smallest ancestor that
# holds the "Join" button AND the invite text. Zoom stacks dialogs — the "This
# meeting is recorded" notice sits behind the invite inside shared wrappers — so
# matching an outer container by text/class instead would lump both dialogs'
# text and buttons together and pick the wrong button.
#
# Visibility is tested via offsetWidth/getClientRects rather than offsetParent,
# which is null for position:fixed modals.
_FIND_BREAKOUT_FN = r"""
    const _label = el => (el.textContent || el.getAttribute('aria-label') || '')
                         .replace(/\s+/g, ' ').trim().toLowerCase();
    const _visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const _buttons = root => [...root.querySelectorAll('button, [role="button"]')].filter(_visible);
    const _findBreakout = () => {
        for (const nn of _buttons(document)) {
            if (_label(nn) !== 'not now') continue;
            let el = nn.parentElement;
            for (let depth = 0; el && depth < 8; depth++, el = el.parentElement) {
                const join = _buttons(el).find(b => _label(b) === 'join');
                if (!join) continue;
                const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
                if (!text.toLowerCase().includes('inviting you to join')) continue;
                return { dialog: el, join, notNow: nn, text };
            }
        }
        return null;
    };
"""

# want=null → return the invite's text (or null); "join"/"not now" → click that
# button and return "clicked".
_BREAKOUT_JS = "(want) => {" + _FIND_BREAKOUT_FN + r"""
    const found = _findBreakout();
    if (!found) return null;
    if (!want) return found.text;
    (want === 'join' ? found.join : found.notNow).click();
    return 'clicked';
}"""

_DISMISS_JS = "() => {" + _FIND_BREAKOUT_FN + """
                const OK_LABELS = ['ok', 'got it', 'i understand', 'accept', 'dismiss', 'continue', 'close'];

                // Never touch the breakout invite — it has its own flow.
                const breakout = (_findBreakout() || {}).dialog || null;
                const guarded = btn => breakout && breakout.contains(btn);

                // Zoom uses both <button> and [role="button"] divs/spans
                function clickables(root) {
                    return [...root.querySelectorAll('button, [role="button"]')];
                }

                // Describe the dismissed dialog without dragging in the stacked
                // invite's text: walk up from the button, stopping before any
                // ancestor that wraps the invite too.
                function describe(btn) {
                    let el = btn.parentElement, best = '';
                    for (let d = 0; el && d < 6; d++, el = el.parentElement) {
                        if (breakout && el.contains(breakout)) break;
                        const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (t) best = t;
                    }
                    return best;
                }

                // Pass 1: modal/dialog containers (including Zoom-specific classes)
                const containers = document.querySelectorAll(
                    '[role="dialog"], [role="alertdialog"], '
                    + '[class*="modal"], [class*="dialog"], [class*="overlay"], [class*="popup"], '
                    + '[class*="zm-modal"], [class*="consent"], [class*="recording-notice"], '
                    + '[class*="notify"], [class*="notice"]'
                );
                for (const el of containers) {
                    if (el.offsetParent === null) continue;
                    for (const btn of clickables(el)) {
                        if (guarded(btn)) continue;
                        const txt = (btn.textContent || btn.getAttribute('aria-label') || '')
                                    .trim().toLowerCase();
                        if (OK_LABELS.includes(txt) && btn.offsetParent !== null) {
                            const text = describe(btn) || el.innerText.trim();
                            btn.click();
                            return text;
                        }
                    }
                }

                // Pass 2: <button> elements only — must be exactly "ok" to avoid over-matching
                for (const btn of document.querySelectorAll('button')) {
                    if (guarded(btn)) continue;
                    const txt = (btn.textContent || btn.getAttribute('aria-label') || '')
                                .trim().toLowerCase();
                    if (txt === 'ok' && btn.offsetParent !== null) {
                        const container = btn.closest('[class]');
                        const text = describe(btn)
                                     || (container?.innerText || btn.parentElement?.innerText || '').trim();
                        if (!text) continue;
                        btn.click();
                        return text;
                    }
                }

                // Pass 3: feature-intro popovers ("Introducing My Notes" → "Got it").
                // These are position:fixed and don't carry modal/dialog classes, so
                // passes 1-2 miss them (offsetParent is null for fixed elements).
                for (const btn of clickables(document)) {
                    if (guarded(btn) || !_visible(btn)) continue;
                    if (_label(btn) !== 'got it') continue;
                    const text = describe(btn) || 'Got it popup';
                    btn.click();
                    return text;
                }

                // Pass 4: toast banners whose only control is a bare close icon
                // (e.g. "enable hardware acceleration ... Learn more  ×"). Anchor on
                // the toast text, then look for an icon-only/close-ish button in the
                // nearest ancestors.
                const TOAST_HINTS = ['hardware acceleration'];
                const isCloseish = btn => {
                    const lbl = _label(btn);
                    if (lbl.includes('learn more')) return false;
                    if (['x', '×', '✕', '✖', 'close', 'dismiss'].includes(lbl)) return true;
                    const meta = ((btn.getAttribute('aria-label') || '') + ' '
                                  + (btn.getAttribute('title') || '') + ' '
                                  + (btn.getAttribute('class') || '')).toLowerCase();
                    if (meta.includes('close') || meta.includes('dismiss')) return true;
                    return !lbl && !!btn.querySelector('svg, i, img');
                };
                for (const node of document.querySelectorAll('div, span, p')) {
                    if (node.children.length > 3) continue;
                    const nt = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (!nt || nt.length > 300) continue;
                    const low = nt.toLowerCase();
                    if (!TOAST_HINTS.some(h => low.includes(h)) || !_visible(node)) continue;
                    let el = node;
                    for (let d = 0; el && d < 5; d++, el = el.parentElement) {
                        if (breakout && el.contains(breakout)) break;
                        const btn = clickables(el).find(b => !guarded(b) && _visible(b) && isCloseish(b));
                        if (btn) {
                            btn.click();
                            return nt;
                        }
                    }
                }

                return null;
        }"""



# ── ZoomRecorder ──────────────────────────────────────────────────────────────

class ZoomRecorder:
    def __init__(
        self,
        display: str = ":99",
        sink: str = "virtual.monitor",
        user_id: int | None = None,
        guest_name: str = GUEST_NAME,
        recording_prefix: Optional[str] = None,
        resolution: str = "1080p",
        meeting_password: Optional[str] = None,
        on_started: Optional[Callback] = None,
        on_stopped: Optional[Callback] = None,
        on_error: Optional[Callback] = None,
        on_dialog: Optional[Callback] = None,
        on_waiting: Optional[Callback] = None,
        on_silence_warn: Optional[Callback] = None,
        on_audio_returned: Optional[Callback] = None,
        on_silence_cleared: Optional[Callback] = None,
        on_breakout: Optional[Callback] = None,
        on_breakout_result: Optional[Callback] = None,
        use_profile_name: bool = True,
    ):
        self.display = display
        self.user_id = user_id
        # `sink` is the monitor source FFmpeg records from ("virtual_99.monitor");
        # `sink_name` is the sink itself ("virtual_99"), which is what Chrome must
        # be pointed at via PULSE_SINK so its audio lands where we record.
        self.sink = sink
        self.sink_name = sink[: -len(".monitor")] if sink.endswith(".monitor") else sink
        self.guest_name = guest_name
        self.use_profile_name = use_profile_name
        self._admission_notice = None
        self._reconnect_count = 0
        self.recording_prefix = recording_prefix
        self.resolution = resolution
        self.meeting_password = meeting_password
        self.on_started = on_started
        self.on_stopped = on_stopped
        self.on_error = on_error
        self.on_dialog = on_dialog
        self.on_waiting = on_waiting
        self.on_silence_warn = on_silence_warn
        self.on_audio_returned = on_audio_returned
        self.on_silence_cleared = on_silence_cleared
        self.on_breakout = on_breakout
        self.on_breakout_result = on_breakout_result
        # Silence-monitor state — mutated by silence_wait_for_audio() / request_pause()
        self._silence_state: str = "monitoring"   # monitoring | warned | waiting | paused
        self._snooze_until: float = 0.0
        # Breakout-invite state — answered by answer_breakout() (via bot.py callback)
        self._breakout_state: str = "idle"        # idle | pending
        self._breakout_decision: Optional[str] = None  # "join" | "decline"
        self._breakout_deadline: float = 0.0
        self.is_recording = False
        self.current_url: Optional[str] = None
        self.output_path: Optional[Path] = None
        self._start_time: Optional[float] = None
        # Set when FFmpeg actually starts (after audio is detected). Displayed
        # duration is based on this so it stays in sync with the file size —
        # the pre-recording "waiting for audio" gap is not counted.
        self._record_start_time: Optional[float] = None
        self._started_at: Optional[datetime] = None
        # Pause/resume: recording is captured as one or more segment files.
        # A pause finalizes the current segment; resume starts a new one. All
        # segments are concatenated into output_path when the recording ends.
        self._chrome_height: int = 0
        self._capture_bounds: Optional[dict] = None
        self._segments: list[Path] = []
        self._paused: bool = False
        self._paused_total: float = 0.0          # accumulated paused seconds
        self._pause_started: Optional[float] = None
        self._auto_ended = False
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._ffmpeg_log = None
        self._stop_event = asyncio.Event()
        self._dialog_pending: bool = False
        self._session_errored: bool = False
        # True when this session loaded a saved Zoom login. Decides which advice
        # the sign-in wall reports: "set one up" vs "yours expired".
        self._signed_in: bool = False
        self._native_handoff_seen = False

    def recording_elapsed_secs(self) -> int:
        """Seconds of *actual* recording, measured from when FFmpeg started.

        Returns 0 before FFmpeg starts (e.g. while still waiting for audio), and
        excludes any paused time, so the displayed duration and file size
        describe the same footage.
        """
        if self._record_start_time is None:
            return 0
        total = time.monotonic() - self._record_start_time - self._paused_total
        if self._paused and self._pause_started is not None:
            total -= time.monotonic() - self._pause_started
        return int(max(0, total))

    def elapsed_str(self) -> str:
        return _fmt_duration(self.recording_elapsed_secs())

    def _current_total_bytes(self) -> int:
        """Total size of all recorded segments (or the single output file)."""
        if self._segments:
            return sum(p.stat().st_size for p in self._segments if p.exists())
        if self.output_path and self.output_path.exists():
            return self.output_path.stat().st_size
        return 0

    def file_size_str(self) -> str:
        try:
            size = self._current_total_bytes()
            if size > 0:
                if size >= 1_073_741_824:
                    return f"{size / 1_073_741_824:.1f} GB"
                if size >= 1_048_576:
                    return f"{size / 1_048_576:.1f} MB"
                return f"{size / 1024:.1f} KB"
        except Exception:
            pass
        return "—"

    async def record(self, url: str) -> None:
        self.is_recording = True
        self.current_url = url
        if self._stop_event.is_set():
            self.is_recording = False
            return
        self._start_time = time.monotonic()
        self._started_at = datetime.now()

        try:
            mode = (os.getenv("ZOOM_JOIN_MODE") or "auto").strip().lower()
            if mode not in {"auto", "web", "desktop"}:
                raise ValueError("ZOOM_JOIN_MODE must be auto, web or desktop")
            if mode == "desktop":
                logger.info("Joining directly with native Zoom Desktop (display=%s)", self.display)
                await self._record_native(url)
                return
            web_url = build_web_client_url(url)

            logger.info("Preparing Zoom invitation %s (display=%s)", log_zoom_url(url), self.display)

            # Pin this session's Chrome to its own display *and* its own audio
            # sink. Without PULSE_SINK every Chrome plays into PulseAudio's
            # default sink, so with two sessions the first sink captures both
            # meetings (merged audio) while the second records silence — and a
            # silent sink also stalls _wait_for_audio, so that session never
            # starts recording at all.
            session_env = {
                **os.environ,
                "DISPLAY": self.display,
                "PULSE_SINK": self.sink_name,
                "PULSE_SOURCE": self.sink_name + '_mic.monitor',
            }
            logger.info(
                "Session env — DISPLAY=%s PULSE_SINK=%s (recording %s)",
                self.display, self.sink_name, self.sink,
            )

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=False,
                    env=session_env,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--use-fake-ui-for-media-stream",
                        "--autoplay-policy=no-user-gesture-required",
                        "--disable-blink-features=AutomationControlled",
                        "--start-maximized",
                        "--window-position=0,0",
                        "--window-size=1920,1200",
                    ],
                )
                # storage_state=None is Playwright's "fresh context" default,
                # so an absent session file simply means the old guest join.
                # MongoDB is authoritative when configured. Read just before
                # joining so other servers' new browser logins are picked up.
                zoom_state = await auth_store.load() if auth_store.configured else _load_zoom_state()
                self._signed_in = zoom_state is not None
                if isinstance(zoom_state, str):
                    zoom_state = json.loads(Path(zoom_state).read_text())
                if self._signed_in and self.use_profile_name and isinstance(zoom_state, dict):
                    self.guest_name = zoom_state.get("profile", {}).get("display_name") or self.guest_name
                ctx = await browser.new_context(
                    no_viewport=True,
                    permissions=["microphone", "camera"],
                    storage_state=({k: v for k, v in zoom_state.items() if k in ("cookies", "origins")}
                                   if isinstance(zoom_state, dict) else zoom_state),
                    locale="en-US",
                )
                page = await ctx.new_page()
                await self._monitor_desktop_handoff(page)
                resume_current_page = False
                if mode == 'auto':
                    web_url, resume_current_page = await self._choose_join_target(page, url, web_url)
                logger.info("Joining Web App %s (display=%s)", log_zoom_url(web_url), self.display)
                if not await self._join_meeting(page, web_url, resume_waiting_room=resume_current_page):
                    return
                await self._prepare_recording_view(page)
                # Measure browser chrome height for cropping
                try:
                    chrome_height = await page.evaluate(
                        "window.outerHeight - window.innerHeight"
                    )
                    chrome_height = max(0, int(chrome_height))
                except Exception:
                    chrome_height = 0
                logger.info("Browser chrome height: %dpx", chrome_height)

                await self._wait_for_audio(page)

                if self._session_errored or self._stop_event.is_set():
                    return

                if not self.recording_prefix:
                    self.recording_prefix = await self._automatic_recording_name(page, url)
                datestamp = datetime.now().strftime("%Y%m%d")
                recording_dir = _reserve_recording_dir(self.recording_prefix, datestamp)
                self.output_path = recording_dir / f"{self.recording_prefix}.mp4"
                await self._configure_capture_bounds(page)
                self._chrome_height = chrome_height
                self._segments = [self.output_path]
                self._ffmpeg = self._start_ffmpeg(str(self.output_path), chrome_height)
                logger.info("FFmpeg started → %s", self.output_path)
                # Anchor duration to the real recording start (not browser-join),
                # so /peek and the hourly notice match the file that's growing.
                self._record_start_time = time.monotonic()
                self._started_at = datetime.now()

                if self.on_started:
                    await self.on_started(str(self.output_path))

                outcome = "stopped"
                while not self._stop_event.is_set():
                    silence_task = asyncio.create_task(self._silence_monitor())
                    try:
                        outcome = await self._watch_meeting(page)
                    finally:
                        silence_task.cancel()
                        await asyncio.gather(silence_task, return_exceptions=True)
                    if outcome not in {"disconnected", "waiting_room", "waiting_host"}:
                        break
                    recovered = await self._recover_connection(page, web_url, waiting_room=outcome == "waiting_room")
                    if recovered is None:
                        outcome = "connection_failed"
                        break
                    page = recovered

                self._auto_ended = outcome == "ended"
                if not self._auto_ended:
                    # Stop encoding first so the video ends at /stop rather than
                    # on the leave dialog, then leave properly: killing Chrome
                    # outright leaves the bot's name in the participant list
                    # until Zoom times the dead connection out (minutes).
                    await asyncio.to_thread(self._stop_ffmpeg)
                    try:
                        await page.evaluate("window.__zoomyRecordingView?.stop()")
                    except Exception:
                        pass
                    await self._leave_meeting(page)
                await browser.close()

        except DesktopRequired:
            # Playwright has closed before native Zoom takes the same display
            # and sink. No simultaneous duplicate participant/audio capture.
            try:
                logger.info("Switching to native Zoom Desktop (display=%s)", self.display)
                await self._record_native(url)
            except Exception as exc:
                self._session_errored = self._session_errored or self.output_path is None
                if self.on_error:
                    await self.on_error(str(exc))
        except Exception as exc:
            logger.exception("Recorder failed")
            # Prevent the finalizer from reporting a partially-created output
            # as a successful recording after a runtime or encoder failure.
            self._session_errored = True
            if self.on_error:
                await self.on_error(str(exc))
        finally:
            elapsed = self.recording_elapsed_secs()
            self._stop_ffmpeg()
            # Concat can be lengthy for big files — keep the loop responsive.
            await asyncio.to_thread(self._finalize_segments)
            self.is_recording = False
            self.current_url = None
            self._paused = False

            if self.output_path and not self._session_errored:
                metadata = {
                    "user_id": self.user_id,
                    "recording_name": self.recording_prefix,
                    "url": url,
                    "duration_seconds": elapsed,
                    "started_at": self._started_at.isoformat() if self._started_at else None,
                }
                try:
                    meta_path = self.output_path.parent / ".metadata.json"
                    meta_path.write_text(
                        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                except Exception as e:
                    logger.warning("Could not write metadata: %s", e)

            if self.output_path and not self._session_errored and self.on_stopped:
                duration_str = _fmt_duration(elapsed)
                size_str = self.file_size_str()
                reason = "host ended" if self._auto_ended else "manual stop"
                logger.info(
                    "Recording done — %s | duration=%s | size=%s | reason=%s",
                    self.output_path.parent.name, duration_str, size_str, reason,
                )
                await self.on_stopped(
                    str(self.output_path), duration_str, size_str, self._auto_ended,
                )

    async def stop(self) -> None:
        self._stop_event.set()

    async def _leave_meeting(self, page: Page) -> bool:
        """Click Leave → Leave Meeting so Zoom drops the bot from the roster now.

        Best effort: the Zoom web client's markup can change, so any failure is
        logged and the caller just closes the browser as before. Returns whether
        the leave was confirmed. Only the exact "Leave Meeting" button is ever
        clicked — a host's popup also offers "End Meeting for All".
        """
        async def _click_through() -> None:
            # The toolbar auto-hides until the pointer moves.
            await page.mouse.move(960, 600)
            leave = page.locator(
                "button.footer__leave-btn, button[aria-label='Leave' i], button[title='Leave' i]"
            ).first
            await leave.click(timeout=3_000)
            confirm = page.get_by_role("button", name=re.compile(r"^\s*Leave Meeting\s*$", re.I)).first
            await confirm.click(timeout=3_000)
            try:
                await leave.wait_for(state="hidden", timeout=5_000)
            except Exception:
                await asyncio.sleep(1.0)   # let the leave request go out before the browser dies

        try:
            if page.is_closed():
                return False
            await asyncio.wait_for(_click_through(), timeout=15)
        except Exception as e:
            logger.warning("Could not leave via the Leave button (%s: %s) — closing the browser instead",
                           type(e).__name__, str(e).splitlines()[0] if str(e) else "")
            return False
        logger.info("Left the meeting via the Leave button")
        return True

    # ------------------------------------------------------------------

    async def _screenshot(self, page: Page, label: str) -> None:
        if not DEBUG:
            return
        try:
            path = RECORDINGS_DIR / f"debug_{label}.png"
            await page.screenshot(path=str(path), full_page=False)
            logger.info("Screenshot: %s", path.name)
        except Exception as e:
            logger.warning("Screenshot failed (%s): %s", label, e)

    async def _dismiss_notifications(self, page: Page) -> None:
        try:
            await page.evaluate("""
                document.querySelectorAll(
                    '[class*="notification"] button, [class*="banner"] button, [class*="alert"] button'
                ).forEach(btn => {
                    const txt = (btn.textContent || btn.getAttribute('aria-label') || '');
                    if (txt.includes('×') || txt.includes('✕') || txt.toLowerCase().includes('close') || txt === 'x') {
                        btn.click();
                    }
                });
            """)
        except Exception:
            pass
        try:
            close_btns = page.locator(
                '[class*="notification"] button, [class*="banner"] button'
            )
            count = await close_btns.count()
            for i in range(count):
                try:
                    btn = close_btns.nth(i)
                    if await btn.is_visible():
                        await btn.click(timeout=1_000)
                except Exception:
                    pass
        except Exception:
            pass

    async def _dismiss_blocking_dialogs(self, page: Page) -> None:
        """Detect any blocking modal with an OK-type button, notify, and dismiss it.

        Runs even while a breakout invite is pending — Zoom stacks the invite on
        top of notices like "This meeting is recorded", and clearing the notice
        underneath leaves the invite alone on screen. Any button belonging to the
        invite dialog is protected (OK_LABELS includes 'close'/'dismiss', which
        would otherwise match the invite's own close control).
        """
        if self._dialog_pending:
            return
        try:
            self._dialog_pending = True
            dialog_text = await page.evaluate(_DISMISS_JS)
            if dialog_text:
                logger.info("Dismissed blocking dialog: %s", dialog_text[:80])
                if self.on_dialog:
                    try:
                        await self.on_dialog(dialog_text)
                    except Exception:
                        logger.exception("on_dialog callback failed")
        except Exception as e:
            logger.warning("_dismiss_blocking_dialogs failed: %s", e)
        finally:
            self._dialog_pending = False

    async def _prepare_recording_view(self, page: Page) -> None:
        if os.getenv("ZOOM_CLEAN_RECORDING", "true").lower() != "true":
            return
        source = os.getenv("ZOOM_RECORDING_SOURCE") or "shared-only"
        if source not in {"shared-only", "shared-and-speaker"}:
            raise ValueError("Invalid ZOOM_RECORDING_SOURCE")
        try:
            script = Path(__file__).with_name("recording_view.js").read_text()
            await page.evaluate("settings=>{window.__zoomyRecordingSource=settings.source;window.__zoomyRecordingFps=settings.fps}",
                                {"source": source, "fps": int(os.getenv("RECORD_FPS") or "15")})
            await page.evaluate(script)
            if self._capture_bounds:
                await page.evaluate("bounds=>window.__zoomyRecordingView.setOutputRegion(bounds)", self._capture_bounds)
            await page.mouse.move(0, 0)
            logger.info("Clean recording view enabled (%s); complete source retained", source)
        except Exception as exc:
            if source == 'shared-only':
                raise RuntimeError('Ulashilgan ekran uchun himoyalangan ko‘rinish tayyorlanmadi; kamera yozuvi boshlanmadi.') from None
            if self._capture_bounds:
                raise RuntimeError("Could not restore the fixed recording area after reconnection") from None
            logger.warning("Clean recording view unavailable (%s); keeping the complete native view", type(exc).__name__)

    async def _configure_capture_bounds(self, page: Page) -> None:
        """Match output shape to media; subsequent sources fit inside this area."""
        if (os.getenv("ZOOM_CLEAN_RECORDING", "true").lower() != "true"
                or (os.getenv("ZOOM_RECORDING_ASPECT") or "auto").strip().lower() != "auto"):
            return
        try:
            await page.wait_for_function("""() => {
                const state=window.__zoomyRecordingView?.status();
                return state?.frames>0 && state?.rectangle?.sourceWidth>1;
            }""", timeout=4000)
            geometry = await page.evaluate("""() => ({width:innerWidth,height:innerHeight,
                media:window.__zoomyRecordingView.status().rectangle})""")
            available_w, available_h = geometry["width"], geometry["height"]
            aspect = geometry["media"]["sourceWidth"] / geometry["media"]["sourceHeight"]
            width = min(available_w, available_h * aspect)
            height = min(available_h, available_w / aspect)
            width, height = max(2, int(width) // 2 * 2), max(2, int(height) // 2 * 2)
            bounds = {"x": (available_w - width) // 2, "y": (available_h - height) // 2,
                      "width": width, "height": height}
            # Fit every source into the selected area before trimming only the
            # unused margins around that area. The source itself is never cut.
            await page.evaluate("bounds=>window.__zoomyRecordingView.setOutputRegion(bounds)", bounds)
            self._capture_bounds = bounds
            logger.info("Recording output follows media aspect: %dx%d; complete content retained", width, height)
        except Exception as exc:
            logger.info("Media aspect is not ready (%s); retaining full-view recording", type(exc).__name__)

    # ── Breakout-room invite handling ────────────────────────────────────────


    async def _cancel_silence_for_breakout(self) -> None:
        """A breakout invite outranks any active silence/pause handling.

        Resumes a paused recording (so the breakout audio is captured), resets
        the silence monitor to normal, and asks the bot to delete the silence
        Telegram message(s) via on_silence_cleared — keeping the user's focus on
        the breakout invite. No-op when no silence handling was in effect.
        """
        was_active = self._paused or self._silence_state != "monitoring"
        if not was_active:
            return
        if self._paused:
            self._resume_recording()   # also resets silence_state → "monitoring"
        else:
            self._silence_state = "monitoring"
            self._snooze_until = 0.0
        logger.info("Breakout invite overrides silence handling — cleared")
        if self.on_silence_cleared:
            try:
                await self.on_silence_cleared()
            except Exception:
                logger.exception("on_silence_cleared callback failed")

    def answer_breakout(self, decision: str) -> bool:
        """Record the user's Join/Not-Now answer. Returns False if no pending invite.

        The actual button click happens on the next _watch_meeting iteration,
        which owns the Playwright page.
        """
        if self._breakout_state != "pending":
            return False
        self._breakout_decision = decision
        logger.info("Breakout invite answered: %s", decision)
        return True

    async def _handle_breakout_invite(self, page: Page) -> None:
        """Detect a host breakout-room invite, prompt via Telegram, then act.

        Flow: detect → notify (on_breakout) → wait for answer_breakout() or the
        BREAKOUT_AUTOJOIN_SECS deadline → click Join/Not Now → report result.
        Called once per _watch_meeting iteration.
        """
        if BREAKOUT_AUTOJOIN_SECS < 0:
            return

        if self._breakout_state == "resolved":
            # Re-arm only once the invite has actually left the DOM, so a dialog
            # that survived the click can't trigger a fresh prompt every loop.
            try:
                if not await page.evaluate(_BREAKOUT_JS, None):
                    self._breakout_state = "idle"
                    logger.info("Breakout dialog gone — watching for new invites")
            except Exception:
                pass
            return

        if self._breakout_state == "idle":
            try:
                text = await page.evaluate(_BREAKOUT_JS, None)
            except Exception:
                return
            if not text:
                return
            # Set pending *before* any await so the silence monitor immediately
            # stops emitting silence warnings (breakout outranks silence).
            self._breakout_state = "pending"
            self._breakout_decision = None
            self._breakout_deadline = time.monotonic() + BREAKOUT_AUTOJOIN_SECS
            logger.info("Breakout room invite detected — prompting (auto-join in %ds)",
                        BREAKOUT_AUTOJOIN_SECS)
            # A silent room is common while the host sets up breakout rooms — drop
            # any silence/pause treatment so the user focuses on the invite.
            await self._cancel_silence_for_breakout()
            if self.on_breakout:
                try:
                    await self.on_breakout(text)
                except Exception:
                    logger.exception("on_breakout callback failed")
            return

        # state == "pending" — decide what to do this iteration
        decision = self._breakout_decision
        auto = False
        if decision is None and time.monotonic() >= self._breakout_deadline:
            decision = "join"
            auto = True

        if decision is None:
            # Still waiting for an answer — bail out if the invite was withdrawn.
            try:
                still_open = await page.evaluate(_BREAKOUT_JS, None)
            except Exception:
                still_open = True  # assume present; try again next loop
            if not still_open:
                logger.info("Breakout invite withdrawn before an answer")
                self._breakout_state = "idle"
                self._breakout_decision = None
                if self.on_breakout_result:
                    try:
                        await self.on_breakout_result("withdrawn", False)
                    except Exception:
                        logger.exception("on_breakout_result callback failed")
            return

        # A decision is in hand — click the matching button, then confirm the
        # dialog actually went away (a click Zoom ignored must not be reported
        # as success).
        want = "join" if decision == "join" else "not now"
        try:
            clicked = await page.evaluate(_BREAKOUT_JS, want) == "clicked"
        except Exception as e:
            logger.warning("Breakout button click failed: %s", e)
            clicked = False

        gone = False
        if clicked:
            await asyncio.sleep(0.5)   # let Zoom tear the dialog down
            try:
                gone = not await page.evaluate(_BREAKOUT_JS, None)
            except Exception:
                gone = True

        self._breakout_state = "resolved"
        self._breakout_decision = None
        outcome = decision if (clicked and gone) else "failed"
        logger.info(
            "Breakout %s (auto=%s, clicked=%s, dialog_gone=%s)", decision, auto, clicked, gone,
        )
        if self.on_breakout_result:
            try:
                await self.on_breakout_result(outcome, auto)
            except Exception:
                logger.exception("on_breakout_result callback failed")

    # ── Silence-monitor control (called by bot.py callbacks) ─────────────────

    def silence_wait_for_audio(self) -> None:
        """Keep recording, alert when audio returns.

        A follow-up reminder is still sent every SILENT_SNOOZE_SECS while the
        room stays silent, so a never-ending silence doesn't go unnoticed.
        """
        self._silence_state = "waiting"
        self._snooze_until = time.monotonic() + SILENT_SNOOZE_SECS
        logger.info("Silence: keep recording, alert when audio returns")

    def request_pause(self) -> None:
        """Pause encoding until audio returns.

        The current segment is finalized immediately; a fresh segment starts
        automatically when the silence monitor detects audio again. The silent
        gap is therefore left out of the final video.
        """
        if self._paused:
            return
        self._paused = True
        self._pause_started = time.monotonic()
        self._silence_state = "paused"
        self._stop_ffmpeg()
        logger.info("Recording paused — waiting for audio to resume")

    def _next_segment_path(self) -> Path:
        idx = len(self._segments) + 1
        return self.output_path.with_name(f"{self.recording_prefix}.part{idx}.mp4")

    def _resume_recording(self) -> None:
        """Start a new segment after a pause (called when audio returns)."""
        if not self._paused:
            return
        if self._pause_started is not None:
            self._paused_total += time.monotonic() - self._pause_started
            self._pause_started = None
        self._paused = False
        self._silence_state = "monitoring"
        self._snooze_until = 0.0
        seg = self._next_segment_path()
        self._segments.append(seg)
        self._ffmpeg = self._start_ffmpeg(str(seg), self._chrome_height)
        logger.info("Recording resumed — new segment %s", seg.name)

    def _finalize_segments(self) -> None:
        """Concatenate paused segments into the single output file.

        No-op when the recording was never paused (one continuous segment
        already sits at output_path).
        """
        parts = [p for p in self._segments if p.exists() and p.stat().st_size > 0]
        if len(parts) <= 1 or not self.output_path:
            return
        list_path = self.output_path.parent / ".concat.txt"
        tmp_out = self.output_path.with_name(f"{self.recording_prefix}.merged.mp4")
        try:
            entries = []
            for part in parts:
                escaped = part.as_posix().replace("'", "'\\''")
                entries.append(f"file '{escaped}'\n")
            list_path.write_text(
                "".join(entries), encoding="utf-8"
            )
            logger.info("Concatenating %d segments → %s", len(parts), self.output_path.name)
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "warning",
                 "-f", "concat", "-safe", "0", "-i", str(list_path),
                 "-c", "copy", "-movflags", "+faststart", str(tmp_out)],
                capture_output=True, timeout=600,
            )
            if result.returncode != 0 or not tmp_out.exists():
                logger.error(
                    "Segment concat failed (code %d): %s",
                    result.returncode, result.stderr.decode(errors="replace")[-300:],
                )
                return
            os.replace(tmp_out, self.output_path)   # output_path == parts[0]
            for p in parts[1:]:
                p.unlink(missing_ok=True)
            self._segments = [self.output_path]
            logger.info("Segments merged into %s", self.output_path.name)
        except Exception:
            logger.exception("Segment concat error")
        finally:
            list_path.unlink(missing_ok=True)
            tmp_out.unlink(missing_ok=True)

    # ── Silence monitor ───────────────────────────────────────────────────────

    async def _silence_monitor(self) -> None:
        """Background task: watch for continuous silence on the virtual sink.

        Runs off parec (independent of FFmpeg), so it keeps monitoring even
        while recording is paused.

        States:
          monitoring : silence ≥ SILENT_WARN_SECS → on_silence_warn()
          waiting    : keep recording; re-remind every SILENT_SNOOZE_SECS while silent
          paused     : encoding stopped; stay quiet until audio returns
        Audio return → on_audio_returned(resumed); if paused, a new segment starts.

        External transitions (via bot.py callbacks):
          silence_wait_for_audio() : warned → waiting
          request_pause()          : warned → paused
        """
        if SILENT_WARN_SECS <= 0:
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                "parec",
                f"--device={self.sink}",
                "--format=s16le",
                f"--rate={_AUDIO_SAMPLE_RATE}",
                "--channels=1",
                "--latency-msec=100",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("parec not found — silence monitor disabled")
            return

        chunk_bytes = int(_AUDIO_SAMPLE_RATE * 2 * _AUDIO_CHUNK_SECS)
        silent_since: float | None = None

        logger.info(
            "Silence monitor started (warn_secs=%d, rms_threshold=%d)",
            SILENT_WARN_SECS, AUDIO_RMS_THRESHOLD,
        )

        try:
            while not self._stop_event.is_set():
                if proc.returncode is not None:
                    logger.warning("Silence monitor: parec exited (code %d)", proc.returncode)
                    return

                try:
                    data = await asyncio.wait_for(proc.stdout.read(chunk_bytes), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if not data:
                    logger.warning("Silence monitor: parec EOF")
                    return

                n = len(data) // 2
                if n == 0:
                    continue

                samples = struct.unpack(f"{n}h", data[: n * 2])
                rms = (sum(s * s for s in samples) / n) ** 0.5
                now = time.monotonic()

                if rms <= AUDIO_RMS_THRESHOLD:
                    # ── SILENCE ───────────────────────────────────────────────
                    if silent_since is None:
                        silent_since = now
                    silence_secs = int(now - silent_since)

                    if self._breakout_state == "pending":
                        # A breakout invite is on screen — it takes priority over
                        # silence; stay quiet so the user focuses on the invite.
                        pass

                    elif self._silence_state == "monitoring" and silence_secs >= SILENT_WARN_SECS:
                        self._silence_state = "warned"
                        logger.info("Silence warning: %ds of continuous silence", silence_secs)
                        if self.on_silence_warn:
                            await self.on_silence_warn(silence_secs)

                    elif (
                        self._silence_state == "waiting"
                        and now >= self._snooze_until
                        and silence_secs >= SILENT_WARN_SECS
                    ):
                        # "Alert when audio returns" mode: still-silent reminder.
                        self._snooze_until = now + SILENT_SNOOZE_SECS  # re-arm
                        logger.info(
                            "Still silent in wait-for-audio mode — reminder (%ds)", silence_secs
                        )
                        if self.on_silence_warn:
                            await self.on_silence_warn(silence_secs)

                    # state == "paused" → recording is stopped; stay quiet.

                else:
                    # ── AUDIO ─────────────────────────────────────────────────
                    was_paused = self._paused
                    if was_paused:
                        self._resume_recording()   # restart encoder, clear paused

                    if silent_since is not None:
                        prev_state = "paused" if was_paused else self._silence_state
                        silence_secs = int(now - silent_since)
                        silent_since = None
                        if not was_paused:
                            self._silence_state = "monitoring"
                            self._snooze_until = 0.0

                        if prev_state in ("warned", "waiting", "paused"):
                            logger.info(
                                "Audio returned after %ds of silence (was '%s')",
                                silence_secs, prev_state,
                            )
                            if self.on_audio_returned:
                                await self.on_audio_returned(was_paused)

        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, Exception):
                pass
            logger.info("Silence monitor stopped")

    async def _wait_for_audio(self, page: Optional[Page] = None) -> None:
        """Hold until audio is detected on the virtual sink, then return.

        Uses *parec* (PulseAudio record) to sample the virtual monitor source
        in 250 ms chunks.  Computes RMS of each chunk and returns as soon as
        the level exceeds AUDIO_RMS_THRESHOLD.

        When *page* is given, breakout invites and blocking dialogs are also
        polled here: hosts routinely set breakout rooms up in a silent meeting,
        and this wait is unbounded, so _watch_meeting (the usual handler) would
        not run until audio finally arrives — leaving an invite unanswered on
        screen indefinitely.

        Falls back to immediate return on:
          • AUDIO_WAIT_TIMEOUT == 0 (feature disabled)
          • parec binary not found
          • parec exits early (e.g. sink not ready)
          • _stop_event set (user called /stop before meeting started)
        """
        if AUDIO_WAIT_TIMEOUT <= 0:
            return

        chunk_bytes = int(_AUDIO_SAMPLE_RATE * 2 * _AUDIO_CHUNK_SECS)  # 1 ch, s16le

        try:
            proc = await asyncio.create_subprocess_exec(
                "parec",
                f"--device={self.sink}",
                "--format=s16le",
                f"--rate={_AUDIO_SAMPLE_RATE}",
                "--channels=1",
                "--latency-msec=100",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("parec not found — starting FFmpeg immediately (no audio wait)")
            return

        # Only notify "waiting" if silence persists beyond this many seconds.
        # Active meetings typically have audio within 3–5 s of joining.
        _NOTIFY_GRACE_SECS = 5.0

        start = time.monotonic()
        notified_waiting = False
        # Poll the page on the same cadence as _watch_meeting rather than on
        # every 250 ms audio chunk.
        _PAGE_POLL_SECS = 3.0
        last_page_poll = 0.0
        logger.info(
            "Waiting for audio on %s (threshold=%d RMS, timeout=%ds)",
            self.sink, AUDIO_RMS_THRESHOLD, AUDIO_WAIT_TIMEOUT,
        )

        try:
            while not self._stop_event.is_set():
                if time.monotonic() - start >= AUDIO_WAIT_TIMEOUT:
                    logger.info("Audio wait deadline reached; starting recording with the current audio stream")
                    return
                # parec exited early (e.g. sink not ready yet)
                if proc.returncode is not None:
                    logger.warning(
                        "parec exited (code %d) — starting FFmpeg immediately", proc.returncode
                    )
                    return

                try:
                    data = await asyncio.wait_for(
                        proc.stdout.read(chunk_bytes), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    pass
                else:
                    if not data:
                        logger.warning("parec EOF — starting FFmpeg immediately")
                        return

                    n = len(data) // 2
                    if n > 0:
                        samples = struct.unpack(f"{n}h", data[: n * 2])
                        rms = (sum(s * s for s in samples) / n) ** 0.5

                        if rms > AUDIO_RMS_THRESHOLD:
                            elapsed = time.monotonic() - start
                            logger.info(
                                "Audio detected — RMS=%.1f > threshold=%d at %.1fs elapsed",
                                rms, AUDIO_RMS_THRESHOLD, elapsed,
                            )
                            if notified_waiting and self.on_waiting:
                                await self.on_waiting("▶️ Audio detected — starting recording now!")
                            return

                # A breakout invite can arrive before any audio does — handle it
                # here too, otherwise it would sit unanswered until the meeting
                # makes a sound and _watch_meeting finally starts.
                if page is not None and (time.monotonic() - last_page_poll) >= _PAGE_POLL_SECS:
                    last_page_poll = time.monotonic()
                    if not page.is_closed():
                        try:
                            state = await self._state(page)
                            if state in {"ended", "removed", "auth_required", "app_required", "app_launch", "invalid", "passcode"}:
                                self._stop_event.set()
                                if self.on_error:
                                    await self.on_error(self._join_error(state))
                                return
                            if state in {"disconnected", "waiting_room", "waiting_host"}:
                                try:
                                    if not await self._join_meeting(page, build_web_client_url(self.current_url),
                                                                    resume_waiting_room=state == "waiting_room"):
                                        return
                                    await self._prepare_recording_view(page)
                                except Exception:
                                    self._stop_event.set()
                                    if self.on_error:
                                        await self.on_error("Ovozni kutishda ulanish tiklanmadi. /record bilan qayta boshlang.")
                                    return
                            await self._handle_breakout_invite(page)
                            await self._dismiss_blocking_dialogs(page)
                        except Exception as e:
                            logger.debug("Pre-recording page poll failed (will retry): %s", e)
                    else:
                        self._stop_event.set()
                        if self.on_error:
                            await self.on_error("Zoom oynasi yopildi. /record bilan qayta boshlang.")
                        return

                # Send "waiting" notification only after grace period expires
                if not notified_waiting and (time.monotonic() - start) >= _NOTIFY_GRACE_SECS:
                    notified_waiting = True
                    if self.on_waiting:
                        await self.on_waiting(
                            "⏸ Joined meeting — waiting for audio to start recording.\n"
                            "Use /stop to abort."
                        )

        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _check_signin_required(self, page: Page) -> bool:
        """True if Zoom is showing the 'sign in to join' wall (join rejected)."""
        try:
            text = (await page.evaluate("document.body.innerText")).lower()
        except Exception:
            return False
        return any(p in text for p in _SIGNIN_REQUIRED_PHRASES)

    async def _abort_signin_required(self, page: Page) -> None:
        """Fail the session on the sign-in wall, advising by how we joined."""
        msg = _SIGNIN_HELP_EXPIRED if self._signed_in else _SIGNIN_HELP_GUEST
        logger.warning(
            "Sign-in wall hit (signed_in=%s, aborting): %s", self._signed_in, page.url,
        )
        self._session_errored = self._record_start_time is None
        self._stop_event.set()
        if self.on_error:
            await self.on_error(msg)

    async def _state(self, page: Page) -> str:
        if page.is_closed():
            return "disconnected"
        if re.search(r"/(signin|login)(?:[/?#]|$)", page.url, re.I):
            return "auth_required"
        if page.url.startswith("chrome-error:"):
            return "disconnected"
        snapshot = await asyncio.wait_for(page.evaluate(SNAPSHOT_JS), timeout=5)
        if self._native_handoff_seen:
            snapshot['app_launch'] = True
        return classify_meeting(snapshot)

    def _observe_desktop_handoff(self, event):
        url = urlparse(event.get('url', ''))
        host = (url.hostname or '').lower()
        if url.scheme not in {'zoommtg', 'zoomus'} or not (host == 'zoom.us' or host.endswith('.zoom.us')):
            return
        if url.path.rstrip('/') != '/join' or not self.current_url:
            return
        meeting_id, _ = parse_zoom_url(self.current_url)
        if parse_qs(url.query).get('confno') == [meeting_id]:
            self._native_handoff_seen = True
            logger.info('Zoom requested native Desktop handoff')

    async def _monitor_desktop_handoff(self, page):
        try:
            session = await page.context.new_cdp_session(page)
            await session.send('Page.enable')
            session.on('Page.frameRequestedNavigation', self._observe_desktop_handoff)
            session.on('Page.windowOpen', self._observe_desktop_handoff)
        except Exception:
            # DOM detection still handles ordinary Launch Meeting pages.
            logger.debug('Native launch protocol monitor unavailable')

    async def _choose_join_target(self, page, invitation, web_url):
        """Inspect the real invitation before replacing it with a /wc URL.

        An app-only invitation can request zoommtg:// while its manually-built
        web-client URL merely sits on a preview form. Rewriting first discarded
        precisely the information needed to select the desktop client.
        """
        if self._stop_event.is_set() or '/wc/' in urlparse(invitation).path:
            return web_url, False
        deadline = time.monotonic() + 10
        try:
            await page.goto(invitation, wait_until='domcontentloaded', timeout=8000)
        except Exception:
            if self._stop_event.is_set():
                return web_url, False
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            browser_link = page.get_by_text(re.compile(r'join from (?:your |the )?browser', re.I))
            if await browser_link.count() and await browser_link.first.is_visible():
                self._native_handoff_seen = False
                return web_url, False
            try:
                state = await self._state(page)
            except Exception:
                if self._native_handoff_seen:
                    raise self._join_exception('app_launch')
                state = 'connecting'
            if state in {'app_launch', 'app_required'}:
                logger.info('Invitation requests Zoom Desktop; skipping web-client preview')
                raise self._join_exception(state)
            if state in {'admitted', 'waiting_room', 'waiting_host'}:
                return web_url, True
            if state in {'auth_required', 'removed', 'ended', 'invalid', 'passcode'}:
                raise self._join_exception(state)
            if await self._wait_or_stop(0.25):
                break
        if self._native_handoff_seen:
            raise self._join_exception('app_launch')
        return web_url, False

    async def _wait_or_stop(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _notice_admission(self, state: str) -> None:
        if self._admission_notice == state:
            return
        self._admission_notice = state
        messages = {
            "waiting_host": "Konferensiya hali boshlanmagan. Hostni kutyapman; avtomatik qayta tekshiraman. /stop bilan bekor qilishingiz mumkin.",
            "waiting_room": "Host kiritishini kutyapman (waiting room). Navbatni saqlash uchun sahifa yangilanmaydi. /stop bilan bekor qilish mumkin.",
            "disconnected": "Ulanish uzildi. Avtomatik qayta ulanishga harakat qilyapman.",
            "admitted": "Konferensiyaga kirdim. Ovoz va yozuv avtomatik tayyorlanadi.",
        }
        if self.on_waiting and state in messages:
            try:
                await self.on_waiting(messages[state])
            except Exception:
                logger.warning("Could not send admission status notification")

    @staticmethod
    def _join_error(state: str) -> str:
        return {
            "app_required": "Bu konferensiya haqiqiy Zoom desktop ilovasini talab qiladi. Hozirgi bot Zoom Web App orqali ishlaydi.",
            "app_launch": "Zoom ilovada ochishni so‘radi. ZOOM_JOIN_MODE=auto yoki desktop bilan qayta urinib ko‘ring.",
            "passcode": "Meeting passcode noto‘g‘ri yoki yetishmayapti. /record ga pwd parametri bor to‘liq taklif havolasini yuboring.",
            "invalid": "Konferensiya havolasi noto‘g‘ri yoki eskirgan.",
            "removed": "Host botni chiqardi yoki kirishni rad etdi. Avtomatik qayta kirish to‘xtatildi.",
            "ended": "Konferensiya tugagan.",
            "auth_required": "Zoom sessiyasi qabul qilinmadi. /zoomlogin orqali qayta kiring.",
        }[state]

    def _join_exception(self, state):
        if state in {"app_required", "app_launch"} and (os.getenv("ZOOM_JOIN_MODE") or "auto").lower() == "auto":
            return DesktopRequired("Zoom desktop ilovasi talab qilindi")
        return MeetingJoinError(self._join_error(state))

    async def _record_native(self, url):
        from zoom_desktop import NativeZoom
        if self._stop_event.is_set():
            return
        if self.use_profile_name:
            state = await auth_store.load() if auth_store.configured else _load_zoom_state()
            if isinstance(state, str):
                state = json.loads(Path(state).read_text())
            if isinstance(state, dict):
                self.guest_name = state.get("profile", {}).get("display_name") or self.guest_name
        native = NativeZoom(self.display, self.sink_name, self._stop_event)
        try:
            await native.open(url, self.guest_name)
            if self.on_waiting:
                await self.on_waiting("Haqiqiy Zoom Desktop orqali kiryapman. Brauzer logini Desktop loginidan alohida.")
            deadline = time.monotonic() + max(30, int(os.getenv("ZOOM_HOST_WAIT_SECONDS") or "7200"))
            link_sent = False
            recovery_since = None
            paused_for_connection = False
            unknown_since = time.monotonic()
            while not self._stop_event.is_set():
                try:
                    state = await asyncio.wait_for(native.snapshot(), timeout=25)
                except (asyncio.TimeoutError, OSError):
                    state = "disconnected"
                if self._stop_event.is_set():
                    break
                if state in {"ended", "removed", "invalid", "passcode", "app_required"}:
                    self._auto_ended = state == "ended"
                    if state != "ended" and self.on_error:
                        await self.on_error(self._join_error(state))
                    break
                if state == "admitted":
                    unknown_since = time.monotonic()
                    recovery_since = None
                    if self.output_path is None:
                        await native.prepare()
                        if self._stop_event.is_set():
                            break
                        if not self.recording_prefix:
                            meeting_id, _ = parse_zoom_url(url)
                            self.recording_prefix = f"Zoom_{meeting_id}_{datetime.now():%H%M%S}"
                        folder = _reserve_recording_dir(self.recording_prefix, datetime.now().strftime("%Y%m%d"))
                        self.output_path = folder / f"{self.recording_prefix}.mp4"
                        # Native pixels cannot use the web canvas renderer. Keep
                        # the entire frame: never hide content by guessing crops.
                        self._chrome_height = 0
                        self._capture_bounds = None
                        self._segments = [self.output_path]
                        self._ffmpeg = self._start_ffmpeg(str(self.output_path))
                        self._record_start_time = time.monotonic()
                        self._started_at = datetime.now()
                        if self.on_started:
                            await self.on_started(str(self.output_path))
                    elif paused_for_connection:
                        self._resume_recording()
                        paused_for_connection = False
                    try:
                        await native.clean_shared_view()
                    except Exception:
                        logger.warning("Could not hide the native video panel; recording continues")
                    if self._ffmpeg and not self._paused and self._ffmpeg.poll() is not None:
                        self._session_errored = True
                        raise RuntimeError("FFmpeg Desktop yozuvi vaqtida to‘xtadi.")
                else:
                    if self.output_path and not self._paused:
                        await asyncio.to_thread(self.request_pause)
                        paused_for_connection = True
                    await self._notice_admission(state)
                    if not link_sent and time.monotonic() - unknown_since >= 15:
                        # The native app contains the owner's account, so only
                        # its owner receives an interactive desktop link.
                        owner = os.getenv("OWNER_TELEGRAM_ID", "")
                        if owner and str(self.user_id) == owner and self.on_waiting:
                            try:
                                link = await native.control_link()
                                await self.on_waiting("Desktop’da login yoki kirishni yakunlash uchun (15 daqiqa): " + link)
                            except Exception:
                                await self.on_waiting("Desktop boshqaruv havolasi ochilmadi. setup-login-link.sh sozlamalarini tekshiring.")
                        link_sent = True
                    if self.output_path or state == "disconnected":
                        recovery_since = recovery_since or time.monotonic()
                        if time.monotonic() - recovery_since >= 30:
                            if self.output_path is None:
                                raise RuntimeError("Zoom Desktop ishga tushmadi yoki ulanishi tiklanmadi.")
                            if self.on_error:
                                await self.on_error("Desktop ulanishi 30 soniyada tiklanmadi. Yozilgan qism saqlandi.")
                            break
                    elif state == "connecting" and time.monotonic() - unknown_since >= 900:
                        raise RuntimeError("Desktop’da kirish tasdiqlanmadi. Login yoki taklif havolasini tekshiring.")
                if self.output_path is None and time.monotonic() >= deadline:
                    raise RuntimeError("Desktop’da hostni kutish muddati tugadi.")
                if await self._wait_or_stop(2):
                    break
        finally:
            # Finalize video before any leave/login UI can enter the recording.
            await asyncio.to_thread(self._stop_ffmpeg)
            await native.close()

    async def _join_meeting(self, page: Page, web_url: str, resume_waiting_room: bool = False) -> bool:
        deadline = time.monotonic() + max(30, int(os.getenv("ZOOM_HOST_WAIT_SECONDS") or "7200"))
        retry_seconds = max(2, int(os.getenv("ZOOM_JOIN_RETRY_SECONDS") or "30"))
        failures = 0
        navigate = not resume_waiting_room
        attempt_start = time.monotonic()
        connecting_since = time.monotonic()
        connect_timeout = max(10, int(os.getenv("ZOOM_CONNECT_TIMEOUT") or "30"))
        connect_attempts = max(1, min(5, int(os.getenv('ZOOM_JOIN_ATTEMPTS') or '3')))
        connect_failures = 0
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            if navigate:
                attempt_start = time.monotonic()
                try:
                    # Each attempt finishes within 25 seconds. Waiting for the
                    # host/admission is a separate, cancellable background state.
                    opening = asyncio.create_task(self._open_join_page(page, web_url))
                    stopping = asyncio.create_task(self._stop_event.wait())
                    try:
                        done, _ = await asyncio.wait((opening, stopping), timeout=25,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if stopping in done:
                            return False
                        if opening not in done:
                            raise asyncio.TimeoutError()
                        await opening
                    finally:
                        for task in (opening, stopping):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(opening, stopping, return_exceptions=True)
                except MeetingJoinError:
                    raise
                except (asyncio.TimeoutError, OSError):
                    pass
                except Exception:
                    if self._stop_event.is_set():
                        return False
                    state = await self._state(page)
                    if state in {"auth_required", "invalid", "passcode", "app_required", "app_launch", "removed", "ended"}:
                        raise self._join_exception(state) from None
                navigate = False
            if self._stop_event.is_set():
                return False
            try:
                state = await self._state(page)
            except Exception:
                state = "disconnected"
            if state == "admitted":
                await self._join_audio(page)
                await self._notice_admission(state)
                return True
            if state in {"auth_required", "invalid", "passcode", "app_required", "app_launch", "removed", "ended"}:
                raise self._join_exception(state)
            if state in {"waiting_room", "waiting_host"}:
                connecting_since = time.monotonic()
                connect_failures = 0
            elif time.monotonic() - connecting_since >= connect_timeout:
                connect_failures += 1
                if connect_failures >= connect_attempts:
                    raise MeetingJoinError(f"Zoom kirish oynasi tayyor bo‘lmadi ({connect_attempts} urinish, har biri {connect_timeout} soniya). /record bilan qayta urinib ko‘ring.")
                if self.on_waiting:
                    try:
                        await self.on_waiting(f'Zoomga yana ulanishga harakat qilyapman ({connect_failures+1}/{connect_attempts}). /stop bilan bekor qilish mumkin.')
                    except Exception:
                        logger.warning('Could not send connection retry notice')
                connecting_since = time.monotonic()
                navigate = True
                continue
            await self._notice_admission(state)
            elapsed = time.monotonic() - attempt_start
            if state == "waiting_room":
                # Zoom admits this same browser automatically. Reloading would
                # throw away the participant's place in the host's queue.
                failures = 0
            elif state == "waiting_host":
                if elapsed >= retry_seconds:
                    navigate = True
            elif state == "disconnected" or elapsed >= 30:
                if failures >= int(os.getenv("ZOOM_RECONNECT_ATTEMPTS") or "5"):
                    raise RuntimeError("Zoom ulanishi tiklanmadi. Saqlangan qism saqlanadi; /record bilan qayta boshlang.")
                failures += 1
                if await self._wait_or_stop(min(retry_seconds, 2 ** failures)):
                    return False
                navigate = True
            if await self._wait_or_stop(1):
                return False
        if not self._stop_event.is_set():
            raise RuntimeError("Hostni kutish muddati tugadi. /record bilan yana boshlashingiz mumkin.")
        return False

    async def _open_join_page(self, page: Page, web_url: str) -> None:
        await page.goto(web_url, wait_until="domcontentloaded", timeout=18_000)
        if await self._wait_or_stop(0.5):
            return
        try:
            if await self._state(page) in {'admitted', 'waiting_room', 'waiting_host'}:
                return
        except Exception:
            pass
        try:
            # Zoom hydrates its React preview after DOMContentLoaded. A single
            # check at 0.5s misses the name input and then waits on disabled Join.
            await page.wait_for_function(r'''() => {
                const visible=e=>{const r=e.getBoundingClientRect();return r.width>1&&r.height>1&&getComputedStyle(e).visibility!=='hidden'};
                return [...document.querySelectorAll('input[type="text"],input#input-for-name,button.footer__leave-btn,[role="dialog"],a[href^="zoommtg:"]')].some(visible)
                    || [...document.querySelectorAll('button')].some(e=>visible(e)&&/^(join|join meeting|launch meeting|join from zoom workplace app)$/i.test(e.innerText.trim()));
            }''', timeout=6000)
        except Exception:
            if self._stop_event.is_set():
                return
        await self._screenshot(page, "01_initial")

        # Detect invalid / expired meeting before doing anything else
        page_text = (await page.evaluate("document.body.innerText")).lower()
        _ERROR_PHRASES = (
            "this meeting link is invalid",
            "meeting link is invalid",
            "invalid meeting",
            "meeting has ended",
            "this meeting is for authorized attendees only",
            "meeting is expired",
        )
        for phrase in _ERROR_PHRASES:
            if phrase in page_text:
                raise RuntimeError(f"Zoom error on join: {phrase}")

        # Bail out before touching any input. When the wall is up, the page is
        # Zoom's sign-in form — and the NAME_SELECTORS fallback ('input[type=
        # "text"]') would type the bot's display name into the *email* field and
        # JOIN_SELECTORS ('button[type="submit"]') would then submit it, firing
        # junk login attempts at Zoom before the loop below noticed.
        if await self._check_signin_required(page):
            await self._abort_signin_required(page)
            return

        state = await self._state(page)
        if state in {"admitted", "waiting_host", "waiting_room"}:
            return
        if state in {"app_required", "app_launch", "auth_required", "removed", "invalid"}:
            raise self._join_exception(state)
        try:
            link = page.get_by_text("Join from Your Browser", exact=False)
            if await link.count() > 0 and await link.first.is_visible():
                await link.first.click(timeout=2000)
                await self._wait_or_stop(0.5)
        except Exception:
            pass

        # Fill passcode if provided — passcode and name fields appear on the same form,
        # so just fill the value; the existing name-fill + Join button click handles submission.
        if self.meeting_password:
            PASSCODE_SELECTORS = [
                'input[placeholder="Meeting Passcode"]',
                'input[placeholder="Passcode"]',
                'input[placeholder="passcode" i]',
                'input[type="password"]',
            ]
            for sel in PASSCODE_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.fill(self.meeting_password)
                        logger.info("Passcode filled using: %s", sel)
                        break
                except Exception:
                    continue
        elif not parse_qs(urlparse(web_url).query).get("pwd"):
            passcode = page.locator('input[placeholder*="passcode" i], input[type="password"]')
            if await passcode.count() and await passcode.first.is_visible():
                raise MeetingJoinError(self._join_error("passcode"))

        NAME_SELECTORS = [
            'input#input-for-name',
            'input[placeholder="Your Name"]',
            'input[placeholder="your name"]',
            'input#inputname',
            'input[data-testid*="name"]',
            'input[class*="name" i]',
            'input[type="text"]:not([placeholder*="passcode" i]):not([name*="email" i])',
        ]
        name_filled = False
        for sel in NAME_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    existing = (await el.input_value()).strip()
                    if self._signed_in and self.use_profile_name and existing:
                        self.guest_name = existing
                    else:
                        await el.fill(self.guest_name, timeout=2000)
                    name_filled = True
                    logger.info("Name filled using: %s", sel)
                    break
            except Exception:
                continue
        if not name_filled:
            logger.warning("Name field not found")

        for label in ["Mute", "Stop Video"]:
            try:
                btn = page.get_by_text(label, exact=True)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click(timeout=2_000)
                    logger.info("Pre-join clicked: %s", label)
            except Exception:
                pass
        await self._screenshot(page, "02_name_filled")

        JOIN_SELECTORS = [
            'button.preview-join-button',
            'button#joinBtn',
            'button[data-testid="join-button"]',
            'input[type="submit"][value="Join" i]',
        ]
        join_clicked = False
        for sel in JOIN_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click(timeout=5_000)
                    join_clicked = True
                    logger.info("Join clicked: %s", sel)
                    break
            except Exception:
                continue
        if not join_clicked:
            try:
                btn = page.get_by_role("button", name=re.compile(r"^\s*join(?: meeting)?\s*$", re.I))
                await btn.first.click(timeout=5_000)
                join_clicked = True
                logger.info("Join clicked via role fallback")
            except Exception:
                logger.warning("Join button not found")

    async def _join_audio(self, page: Page) -> None:
        try:
            audio_btn = page.get_by_role("button", name=re.compile(r"join.*(?:computer|audio)|computer audio", re.I))
            if await audio_btn.count() > 0:
                await audio_btn.first.click(timeout=2000)
        except Exception:
            pass
        for name in (r"^mute(?: my audio)?$", r"^stop video$"):
            try:
                button = page.get_by_role("button", name=re.compile(name, re.I))
                if await button.count() and await button.first.is_visible():
                    await button.first.click(timeout=1000)
            except Exception:
                pass
        await self._dismiss_notifications(page)

    async def _automatic_recording_name(self, page: Page, url: str) -> str:
        try:
            title = await page.evaluate("""() => {
                for(const selector of ['.meeting-info-topic','#meeting-topic','[data-testid="meeting-topic"]']){
                    const text=document.querySelector(selector)?.textContent?.trim();if(text)return text;
                }return document.title;
            }""")
            title = re.sub(r"\s*[-|]\s*Zoom(?: Workplace| Meeting)?\s*$", "", title or "", flags=re.I).strip()
            if title.lower() not in {"", "zoom", "zoom meeting", "zoom workplace", "join meeting", "zoom web client"}:
                name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:80]
                if name:
                    return name
        except Exception:
            pass
        meeting_id, _ = parse_zoom_url(url)
        return f"Zoom_{meeting_id}_{datetime.now():%H%M%S}"

    async def _recover_connection(self, page: Page, web_url: str, waiting_room: bool = False):
        self._reconnect_count += 1
        if self._reconnect_count > int(os.getenv("ZOOM_RECONNECT_ATTEMPTS") or "5"):
            if self.on_error:
                await self.on_error("Qayta ulanish urinishlari tugadi. Yozilgan qism saqlanadi.")
            return None
        was_paused = self._paused
        if not was_paused:
            await asyncio.to_thread(self.request_pause)
        await self._notice_admission("disconnected")
        try:
            if page.is_closed():
                page = await page.context.new_page()
            if not waiting_room and await self._wait_or_stop(5):
                return None
            current = await self._state(page)
            if current in {"ended", "removed", "auth_required", "app_required", "app_launch", "invalid", "passcode"}:
                raise MeetingJoinError(self._join_error(current))
            if current != "admitted":
                if not await self._join_meeting(page, web_url, resume_waiting_room=current == "waiting_room"):
                    return None
            await self._prepare_recording_view(page)
            await self._wait_for_audio(page)
            if self._stop_event.is_set():
                return None
            if not was_paused:
                self._resume_recording()
            if self.on_waiting:
                await self.on_waiting("Ulanish tiklandi. Yozuv davom etyapti; uzilgan oraliq faylga qo‘shilmaydi.")
            return page
        except Exception as exc:
            if self.on_error:
                await self.on_error(str(exc) if isinstance(exc, RuntimeError) else
                    "Ulanish tiklanmadi. Shu paytgacha yozilgan qism saqlanadi.")
            return None

    async def _watch_meeting(self, page: Page) -> str:
        while not self._stop_event.is_set():
            try:
                # FFmpeg health check — if process exited, encoding stopped
                if not self._paused and self._ffmpeg and self._ffmpeg.poll() is not None:
                    logger.error("FFmpeg exited unexpectedly (code %d)", self._ffmpeg.returncode)
                    self._session_errored = True
                    self._stop_event.set()
                    try:
                        if self.on_error:
                            await self.on_error(f"FFmpeg stopped unexpectedly (code {self._ffmpeg.returncode})")
                    except Exception:
                        logger.exception("on_error callback failed after FFmpeg exit")
                    return "encoder_failed"

                state = await self._state(page)
                if state in {"ended", "removed", "app_required", "app_launch", "auth_required", "invalid", "passcode"}:
                    logger.info("Meeting terminal state: %s", state)
                    if state != "ended" and self.on_error:
                        await self.on_error(self._join_error(state))
                    return state
                if state in {"disconnected", "waiting_room", "waiting_host"}:
                    return state
                if "zoom.us/wc" not in page.url:
                    return "disconnected"
            except Exception as e:
                logger.debug("Watch loop iteration error (will retry): %s", e)
            # Breakout invites are handled before generic dialog dismissal so the
            # Join/Not Now modal is never swept away as a stray notification;
            # _dismiss_blocking_dialogs protects it and clears whatever Zoom
            # stacked underneath (e.g. the "meeting is recorded" notice).
            await self._handle_breakout_invite(page)
            await self._dismiss_blocking_dialogs(page)
            # Poll every 3s, but wake at once on /stop instead of finishing the nap.
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
        return "stopped"

    _RESOLUTION_SCALE = {
        "360p": "640:360",
        "720p": "1280:720",
        "1080p": None,
    }

    def _build_vf(self, chrome_height: int) -> Optional[str]:
        """Build -vf filter string.

        Display is 1920×1200. Browser chrome sits at the top (chrome_height px).
        Crop away the chrome rows; output width stays 1920, height is whatever
        remains. An odd final row is padded for H.264 rather than removed.
        """
        if self._capture_bounds:
            region = self._capture_bounds
            return (f"crop={region['width']}:{region['height']}:{region['x']}:"
                    f"{region['y'] + max(0, chrome_height)}:exact=1,setsar=1")
        crop_y = max(0, chrome_height)
        out_h = 1200 - crop_y
        if out_h <= 0:
            crop_y = 0
            out_h = 1200
        logger.info("Crop filter: 1920x%d at y=%d (chrome=%dpx)", out_h, crop_y, chrome_height)
        # Pad an odd final row instead of throwing away the bottom of the content.
        return (f"crop=1920:{out_h}:0:{crop_y}:exact=1,pad=1920:ceil(ih/2)*2:0:0:black"
                if crop_y > 0 else None)

    def _start_ffmpeg(self, output: str, chrome_height: int = 0) -> subprocess.Popen:
        cmd = [
            "ffmpeg", "-y",
            "-loglevel", "warning", "-nostats",
            "-f", "x11grab", "-draw_mouse", "0", "-r", str(max(1,min(30,int(os.getenv("RECORD_FPS") or "15")))) , "-s", "1920x1200", "-i", self.display,
            "-f", "pulse", "-i", self.sink,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-threads", str(max(1,min(8,int(os.getenv("FFMPEG_THREADS") or "2")))),
            "-pix_fmt", "yuv420p",
        ]
        vf = self._build_vf(chrome_height)
        if vf:
            cmd += ["-vf", vf]
        cmd += [
            "-c:a", "aac", "-b:a", "128k",
            # Fragmented MP4: the index is written as the recording goes instead
            # of once at the end. A killed ffmpeg (container stop, crash, OOM)
            # then leaves a playable file, not one without a moov atom.
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            output,
        ]
        log_path = Path(output).parent / ".ffmpeg.log"
        self._ffmpeg_log = open(log_path, "w", encoding="utf-8")
        logger.info("FFmpeg log → %s", log_path)
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=self._ffmpeg_log,
            stderr=self._ffmpeg_log,
        )

    def _stop_ffmpeg(self) -> None:
        if not self._ffmpeg:
            return
        if self._ffmpeg.poll() is None:
            try:
                self._ffmpeg.stdin.write(b"q")
                self._ffmpeg.stdin.flush()
                self._ffmpeg.wait(timeout=15)
            except (subprocess.TimeoutExpired, BrokenPipeError, OSError):
                self._ffmpeg.kill()
        self._ffmpeg = None
        if self._ffmpeg_log:
            try:
                self._ffmpeg_log.close()
            except Exception:
                pass
            self._ffmpeg_log = None
        logger.info("FFmpeg stopped")
