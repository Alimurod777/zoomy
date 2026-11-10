"""Rootless native Zoom fallback. Never treats an open app as admission."""
import asyncio
import csv
import fcntl
import io
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import time
from urllib.parse import parse_qsl, urlencode, urlparse

from meeting_state import classify_meeting
from login_link import BrowserLoginLink
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)


def native_join_uri(url, name):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    match = re.search(r'/(?:j|wc)/(\d+)', parsed.path)
    if not (host == 'zoom.us' or host.endswith('.zoom.us')) or not match:
        raise ValueError('Invalid Zoom invitation')
    # Keep invitation/authentication-exception/registration tokens intact.
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
              if k not in {'action', 'confno', 'uname', 'zc'}]
    params += [('action', 'join'), ('confno', match.group(1)), ('uname', name), ('zc', '0')]
    return f'zoommtg://{host}/join?{urlencode(params)}'


def ocr_lines(tsv):
    groups = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter='\t'):
        try:
            if not row.get('text', '').strip() or float(row['conf']) < 35:
                continue
            key = tuple(row[k] for k in ('block_num', 'par_num', 'line_num'))
            groups.setdefault(key, []).append(row)
        except (KeyError, ValueError):
            continue
    lines = []
    for rows in groups.values():
        left = min(int(r['left']) for r in rows)
        top = min(int(r['top']) for r in rows)
        right = max(int(r['left']) + int(r['width']) for r in rows)
        bottom = max(int(r['top']) + int(r['height']) for r in rows)
        lines.append((' '.join(r['text'] for r in rows), (left + right)//2, (top + bottom)//2))
    return lines


def native_state(lines, meeting_window=False, previously_admitted=False):
    text = ' '.join(line[0] for line in lines)
    state = classify_meeting(text)
    if state != 'connecting':
        return state
    lower = text.lower()
    # A launch/home window or video preview is never proof of admission.
    controls = 'participants' in lower and ('leave' in lower or 'share' in lower)
    if controls or (previously_admitted and meeting_window):
        return 'admitted'
    if 'sign in' in lower and ('join' in lower or 'account' in lower):
        return 'auth_required'
    return 'connecting'


class NativeDesktopLink(BrowserLoginLink):
    """Reuse the expiring VNC transport, with no browser-cookie save operation."""
    async def index(self, request):
        response = await super().index(request)
        html = response.text
        # Remove the browser login save control; native auth is local to Zoom.
        html = html.replace('id="save"', 'id="save" hidden')
        html = html.replace('Loginni saqlash', 'Zoom Desktop').replace('Zoom login', 'Zoom Desktop')
        return web.Response(text=html, content_type='text/html')

    async def status(self, request):
        return web.json_response({'phase': 'manual', 'message': 'Haqiqiy Zoom Desktop. Login yoki kirishni oynada yakunlang; yozuv kirish aniqlangach boshlanadi.'})

    async def save(self, request):
        return web.json_response({'ok': False, 'message': 'Desktop logini ilovaning o‘zida saqlanadi. MongoDB brauzer sessiyasini saqlaydi.'}, status=409)

    async def reload(self, request):
        return web.json_response({'ok': True, 'message': 'Zoom Desktop oynasida davom eting.'})

    async def viewport(self, request):
        return web.json_response({'ok': True})

    async def type_text(self, request):
        data = await request.json()
        text = str(data.get('text', ''))
        if len(text) > 4096:
            raise web.HTTPBadRequest()
        async with self.lock:
            if data.get('replace', True):
                await self.browser.command('xdotool', 'key', '--clearmodifiers', 'ctrl+a')
            # stdin avoids putting passwords/OTP in the process command line.
            await self.browser.command('xdotool', 'type', '--clearmodifiers', '--file', '-', input=text.encode())
            if data.get('enter'):
                await self.browser.command('xdotool', 'key', 'Return')
        return web.json_response({'ok': True, 'message': 'Matn faol maydonga kiritildi.'})

    async def enter(self, request):
        await self.browser.command('xdotool', 'key', 'Return')
        return web.json_response({'ok': True})


class NativeZoom:
    def __init__(self, display, sink, stop_event):
        self.display = display
        self.display_num = int(display.lstrip(':').split('.')[0])
        self.sink = sink
        self.stop_event = stop_event
        self.process = None
        self.wm = None
        self.link = None
        self.lock_file = None
        self.log = None
        self.admitted = False
        self.mic_module = None
        self.lines = []
        self.meeting_window_id = None
        self._fullscreen_window_id = None
        self._share_panel_hidden = False
        self._last_view_attempt = float('-inf')
        self.env = {**os.environ, 'DISPLAY': display, 'PULSE_SINK': sink,
                    'LIBGL_ALWAYS_SOFTWARE': '1', 'QT_QPA_PLATFORM': 'xcb',
                    'OMP_THREAD_LIMIT': '2', 'OMP_NUM_THREADS': '2',
                    'XDG_DATA_DIRS': str(ROOT / '.runtime/root/usr/share') + ':' + os.getenv('XDG_DATA_DIRS', '/usr/local/share:/usr/share')}

    async def command(self, *args, input=None, timeout=8):
        proc = await asyncio.create_subprocess_exec(*args, env=self.env,
                stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(input), timeout)
            return out if proc.returncode == 0 else b''
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def open(self, url, name):
        binary = ROOT / '.runtime/zoom/opt/zoom/ZoomLauncher'
        if not binary.is_file() or not all(shutil.which(p) for p in ('xdotool', 'tesseract', 'openbox')):
            raise RuntimeError('Desktop tayyor emas. bash scripts/setup-zoom-desktop.sh ni bajaring.')
        existing = (await self.command('pgrep', '-u', str(os.getuid()), '-x', 'zoom')).strip()
        if existing:
            raise RuntimeError('Zoom Desktop allaqachon ochiq. Boshqa oynaga ulanmaslik uchun avval uni yoping.')
        # Zoom is a per-user singleton. Never reuse a different recording's app,
        # display, microphone or audio sink.
        self.lock_file = open(ROOT / '.runtime/zoom-desktop.lock', 'a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError('Zoom Desktop boshqa yozuvda band. U tugagach qayta urinib ko‘ring.') from None
        # A dedicated silent microphone prevents capturing the server/user's
        # microphone even if Zoom resets its own mute preference.
        mic = f'zoomy_native_mic_{self.display_num}'
        module = await self.command('pactl', 'load-module', 'module-null-sink', 'sink_name='+mic)
        if not module.strip().isdigit():
            raise RuntimeError('Desktop uchun ovozsiz mikrofon tayyorlanmadi.')
        self.mic_module = module.decode().strip()
        self.env['PULSE_SOURCE'] = mic+'.monitor'
        path = ROOT / '.runtime/zoom-desktop.log'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.log = os.fdopen(fd, 'w')
        self.wm = await asyncio.create_subprocess_exec('openbox', '--sm-disable', env=self.env,
                stdout=self.log, stderr=self.log, start_new_session=True)
        self.uri = native_join_uri(url, name)
        self.process = await asyncio.create_subprocess_exec(str(binary), '--url='+self.uri,
                env=self.env, cwd=binary.parent, stdout=self.log, stderr=self.log, start_new_session=True)

    async def control_link(self):
        if not self.link:
            self.link = NativeDesktopLink(self, ttl=900)
            await self.link.open()
        return self.link.url

    async def snapshot(self):
        if self.process and self.process.returncode is not None:
            return 'disconnected'
        window_ids = (await self.command('xdotool', 'search', '--onlyvisible', '--class', 'zoom')).decode().split()
        meeting_window = False
        for wid in window_ids[:20]:
            title = (await self.command('xdotool', 'getwindowname', wid)).decode(errors='replace').strip()
            if re.fullmatch(r'Zoom (Meeting|Webinar)(?:\s.*)?', title, re.I):
                meeting_window = True
                if self.meeting_window_id != wid:
                    self._share_panel_hidden = False
                self.meeting_window_id = wid
        await self.read_screen(margins_only=self.admitted and meeting_window)
        if any('zoom quit unexpectedly' in line[0].lower() for line in self.lines):
            await self.click_label("Don't send") or await self.click_label('Don’t send')
            return 'connecting'
        state = native_state(self.lines, meeting_window, self.admitted)
        if state == 'admitted':
            self.admitted = True
        return state

    async def read_screen(self, margins_only=False):
        # Read controls/status, never infer host removal from meeting chat text.
        image = await self.command('ffmpeg', '-loglevel', 'error', '-f', 'x11grab', '-draw_mouse', '0',
                '-s', '1920x1200', '-i', self.display, '-frames:v', '1', '-threads', '1', '-f', 'image2pipe', '-vcodec', 'png', '-')
        if not image:
            self.lines = []
            return
        # Once admitted, OCR only toolbar/status margins while a meeting window
        # remains. Main content may itself contain words like "meeting ended".
        if margins_only:
            from PIL import Image
            source = Image.open(io.BytesIO(image))
            canvas = Image.new('RGB', (1920, 1200), 'white')
            canvas.paste(source.crop((0, 0, 1920, 100)), (0, 0))
            canvas.paste(source.crop((0, 1040, 1920, 1200)), (0, 1040))
            out = io.BytesIO()
            canvas.save(out, format='PNG')
            image = out.getvalue()
        tsv = await self.command('tesseract', 'stdin', 'stdout', '-l', 'eng', '--psm', '11', 'tsv', input=image, timeout=12)
        self.lines = ocr_lines(tsv.decode(errors='replace'))

    async def park_pointer(self):
        # The bottom/right edges reveal Zoom's auto-hidden controls. The
        # pointer itself is excluded by FFmpeg; leave it on the content edge.
        await self.command('xdotool', 'mousemove', '4', '600')

    async def click_label(self, text):
        for label, x, y in self.lines:
            if label.strip().casefold() == text.casefold():
                await self.command('xdotool', 'mousemove', str(x), str(y), 'click', '1')
                await self.park_pointer()
                return True
        return False

    async def clean_shared_view(self):
        """Use the app's Hide Video Panel command once the share menu exists.

        Positive hide commands are safe to repeat; show/hide shortcuts and
        unchecked side-by-side toggles could restore the panel instead.
        """
        if not self.admitted or self._share_panel_hidden or self.stop_event.is_set():
            return
        if time.monotonic() - self._last_view_attempt < 30:
            return
        anchor = next(((x, y) for label, x, y in self.lines
                       if label.strip().casefold() == 'view options' and y < 150), None)
        if not anchor:
            return
        self._last_view_attempt = time.monotonic()
        if not await self.click_label('View Options'):
            return
        try:
            await asyncio.sleep(0.3)
            await self.read_screen()
            # Read only the opened menu, not words inside a shared slide.
            x, y = anchor
            self.lines = [(label, px, py) for label, px, py in self.lines
                          if abs(px-x) < 360 and y < py < y+650]
            if await self.click_label('Hide Video Panel'):
                self._share_panel_hidden = True
            elif any(label.strip().casefold() == 'show video panel' for label, _, _ in self.lines):
                self._share_panel_hidden = True
                await self.command('xdotool', 'key', '--clearmodifiers', 'Escape')
            else:
                # Collapse only the menu we opened. Avoid Escape here, since
                # an absent menu could cause Zoom to exit its full-screen mode.
                await self.command('xdotool', 'mousemove', str(x), str(y), 'click', '1')
        finally:
            await self.park_pointer()

    async def ensure_fullscreen(self):
        wid = self.meeting_window_id
        if wid and wid != self._fullscreen_window_id and shutil.which('wmctrl'):
            # Set state explicitly. Alt+F is a Windows shortcut and blindly
            # toggling full screen can shrink an already-full-screen meeting.
            await self.command('wmctrl', '-i', '-r', hex(int(wid)), '-b', 'add,fullscreen')
            self._fullscreen_window_id = wid

    async def prepare(self):
        # Exact labels only: never toggle Unmute / Start Video / Share Screen.
        for label in ('Join with Computer Audio', 'Join Audio', 'Mute', 'Stop Video'):
            await self.click_label(label)
        try:
            await self.ensure_fullscreen()
            await self.clean_shared_view()
        except Exception:
            logger.warning('Could not apply native view preferences; preserving the full frame')
        await self.park_pointer()
        # Give Zoom's own auto-hide setting time to clear the toolbar before
        # FFmpeg starts. /stop remains responsive during this short wait.
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass

    async def close(self):
        if self.link:
            try:
                await self.link.close()
            except Exception:
                pass
        # Never press Enter on a host's leave dialog: its default can be
        # "End Meeting for All". Terminate only the processes this session owns.
        for proc in (self.process, self.wm):
            if proc and proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    await asyncio.wait_for(proc.wait(), 5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    if proc.returncode is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        await proc.wait()
        if self.mic_module:
            await self.command('pactl', 'unload-module', self.mic_module)
            self.mic_module = None
        if self.log:
            self.log.close()
        if self.lock_file:
            self.lock_file.close()
