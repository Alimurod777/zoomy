"""Rootless native Zoom fallback. Never treats an open app as admission."""
import asyncio
from collections import deque
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
from desktop_auth_store import DesktopAuthStore, AuthStoreError

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
    # Home offers both Join and Sign in; that is not an authentication demand.
    # Explicit meeting authentication requirements are handled by the classifier.
    return 'connecting'


def blue_audio_buttons(image):
    """Candidate filled buttons, only inside a positively identified dialog."""
    from PIL import ImageChops
    r,g,b=image.convert('RGB').split()
    mask=ImageChops.multiply(r.point(lambda v:255 if v<120 else 0),
                            b.point(lambda v:255 if v>160 else 0))
    mask=ImageChops.multiply(mask,g.point(lambda v:255 if 45<v<195 else 0))
    width,height=image.size
    pixels=bytearray(mask.tobytes())
    boxes=[]
    for start in range(len(pixels)):
        if not pixels[start]:
            continue
        pixels[start]=0
        todo=deque([start])
        left=right=start%width
        top=bottom=start//width
        count=0
        while todo:
            index=todo.popleft()
            x,y=index%width,index//width
            left,right=min(left,x),max(right,x)
            top,bottom=min(top,y),max(bottom,y)
            count+=1
            for neighbor in (index-width if y else -1,index+width if y<height-1 else -1,
                             index-1 if x else -1,index+1 if x<width-1 else -1):
                if neighbor>=0 and pixels[neighbor]:
                    pixels[neighbor]=0
                    todo.append(neighbor)
        w,h=right-left+1,bottom-top+1
        if (100<=w<=width*.8 and 18<=h<=70 and count/(w*h)>.55
                and abs((left+right)/2-width/2)<width*.2 and height*.2<top<height*.75):
            boxes.append((left,top,right+1,bottom+1))
    return boxes


class NativeDesktopLink(BrowserLoginLink):
    """Reuse the expiring VNC transport, with no browser-cookie save operation."""
    async def index(self, request):
        response = await super().index(request)
        html = response.text
        # A consistent native snapshot requires closing Zoom first.
        html = html.replace('id="save"', 'id="save" hidden')
        html = html.replace('Loginni saqlash', 'Zoom Desktop').replace('Zoom login', 'Zoom Desktop')
        return web.Response(text=html, content_type='text/html')

    async def status(self, request):
        sync = getattr(self.browser, 'profile_sync', 'disabled')
        detail = {
            'disabled': 'Profil shu serverda saqlanadi; MongoDB nusxalash o‘chirilgan.',
            'conflict': 'Mahalliy profil MongoDB nusxasidan farq qiladi; umumiy nusxa almashtirilmaydi.',
            'unavailable': 'MongoDB sinxronlash bajarilmadi; mahalliy profil saqlanadi.',
        }.get(sync, 'Profil Zoom yopilgach MongoDB’ga shifrlab nusxalanadi; boshqa serverda qayta login so‘ralishi mumkin.')
        return web.json_response({'phase': 'manual', 'message': 'Zoom Desktop maydonini bosing, so‘ng matn kiriting yoki o‘chiring. “Keep me signed in” bo‘lsa belgilang. ' + detail})

    async def save(self, request):
        return web.json_response({'ok': False, 'message': 'Desktop profilining nusxasi Zoom yopilgach avtomatik saqlanadi. Hozir Zoom oynasida davom eting.'}, status=409)

    async def reload(self, request):
        return web.json_response({'ok': True, 'message': 'Zoom Desktop oynasida davom eting.'})

    async def viewport(self, request):
        return web.json_response({'ok': True})

    async def type_text(self, request):
        try:
            data = await request.json()
            text = data.get('text', '')
            if not isinstance(text, str) or not text or len(text) > 4096:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            return web.json_response({'ok': False, 'message': 'Kiritish uchun matn yozing (4096 belgigacha).'}, status=400)
        async with self.lock:
            try:
                if data.get('replace', True):
                    await self.browser.command('xdotool', 'key', '--clearmodifiers', 'ctrl+a', check=True)
                # stdin avoids putting passwords/OTP in the process command line.
                await self.browser.command('xdotool', 'type', '--clearmodifiers', '--file', '-', input=text.encode(), check=True)
                if data.get('enter'):
                    await self.browser.command('xdotool', 'key', 'Return', check=True)
            except (OSError, RuntimeError, asyncio.TimeoutError):
                return web.json_response({'ok': False, 'message': 'Matn kiritilmadi. Zoom maydonini qayta bosing.'}, status=503)
        return web.json_response({'ok': True, 'message': 'Matn faol maydonga kiritildi.'})

    async def enter(self, request):
        async with self.lock:
            try:
                await self.browser.command('xdotool', 'key', '--clearmodifiers', 'Return', check=True)
            except (OSError, RuntimeError, asyncio.TimeoutError):
                return web.json_response({'ok': False, 'message': 'Enter bajarilmadi. Zoom oynasini qayta bosing.'}, status=503)
        return web.json_response({'ok': True})

    async def edit(self, request):
        try:
            action = (await request.json()).get('action')
            keys = {'backspace': ('BackSpace',), 'select_all': ('ctrl+a',),
                    'clear': ('ctrl+a', 'BackSpace')}[action]
        except (ValueError, KeyError, TypeError, AttributeError):
            return web.json_response({'ok': False, 'message': 'Noma’lum tahrirlash amali.'}, status=400)
        async with self.lock:
            try:
                await self.browser.command('xdotool', 'key', '--clearmodifiers', *keys, check=True)
            except (OSError, RuntimeError, asyncio.TimeoutError):
                return web.json_response({'ok': False, 'message': 'Tahrirlash bajarilmadi. Zoom maydonini qayta bosing.'}, status=503)
        return web.json_response({'ok': True, 'message': 'Tanlangan Zoom maydoni tahrirlandi.'})


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
        self.auth_store = DesktopAuthStore()
        self.profile_sync = 'disabled'
        self.share_seen = False
        self.audio_dialog_open = False
        self.audio_window_rect = None
        self._last_header_ocr = float('-inf')
        self.env = {**os.environ, 'DISPLAY': display, 'PULSE_SINK': sink,
                    'LIBGL_ALWAYS_SOFTWARE': '1', 'QT_QPA_PLATFORM': 'xcb',
                    'OMP_THREAD_LIMIT': '2', 'OMP_NUM_THREADS': '2',
                    'XDG_DATA_DIRS': str(ROOT / '.runtime/root/usr/share') + ':' + os.getenv('XDG_DATA_DIRS', '/usr/local/share:/usr/share')}

    async def command(self, *args, input=None, timeout=8, check=False):
        proc = await asyncio.create_subprocess_exec(*args, env=self.env,
                stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(input), timeout)
            if check and proc.returncode != 0:
                raise RuntimeError('Native desktop action failed')
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
        try:
            outcome = await self.auth_store.prepare()
            self.profile_sync = outcome
            logger.info('Desktop profile sync: %s (profile restore does not verify account login)', outcome)
            if outcome == 'conflict':
                logger.warning('Local Desktop profile differs from another server; neither profile will be overwritten')
        except Exception as exc:
            self.profile_sync = 'unavailable'
            logger.warning('Desktop profile sync unavailable: %s; keeping local profile',
                           str(exc) if isinstance(exc, AuthStoreError) else type(exc).__name__)
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
        meeting_window = self.meeting_window_id is not None and self.meeting_window_id in window_ids
        main_candidates = []
        audio_dialog = False
        self.audio_window_rect = None
        popup_regions = []
        for wid in window_ids[:20]:
            title = (await self.command('xdotool', 'getwindowname', wid)).decode(errors='replace').strip()
            is_audio = bool(re.search(r'(?:audio conference options|join audio)', title, re.I))
            if is_audio or title.casefold() == 'zoom':
                audio_dialog = audio_dialog or is_audio
                geometry=(await self.command('xdotool','getwindowgeometry','--shell',wid)).decode()
                values=dict(re.findall(r'^(X|Y|WIDTH|HEIGHT)=(-?\d+)$',geometry,re.M))
                if len(values)==4:
                    x,y,w,h=(int(values[k]) for k in ('X','Y','WIDTH','HEIGHT'))
                    if 0<=x<x+w<=1920 and 0<=y<y+h<=1200:
                        popup_regions.append((x,y,x+w,y+h))
                        if is_audio:
                            self.audio_window_rect=(x,y,x+w,y+h)
            if title.casefold() == 'zoom workplace':
                main_candidates.append(wid)
            if re.fullmatch(r'Zoom (Meeting|Webinar)(?:\s.*)?', title, re.I):
                meeting_window = True
                if self.meeting_window_id != wid:
                    self._share_panel_hidden = False
                self.meeting_window_id = wid
        # The computer-audio dialog is centered, outside the status margins.
        await self.read_screen(margins_only=self.admitted and meeting_window, extra_regions=popup_regions)
        self.share_seen = self.share_seen or any(
            y < 150 and re.search(r"(?:['’]s\s+screen|view options|you are viewing)", label, re.I)
            for label, _, y in self.lines)
        self.audio_dialog_open = audio_dialog
        state_lines = list(self.lines)
        if audio_dialog:
            await self.join_computer_audio(refresh=False)
        if any('zoom quit unexpectedly' in line[0].lower() for line in self.lines):
            await self.click_label("Don't send") or await self.click_label('Don’t send')
            return 'connecting'
        state = native_state(state_lines, meeting_window, self.admitted)
        if state == 'admitted':
            self.admitted = True
            # Recent Linux clients title the meeting "Zoom Workplace" too.
            # Choose the largest owned window only after actual admission.
            if not self.meeting_window_id and main_candidates:
                sizes = []
                for wid in main_candidates:
                    geometry = (await self.command('xdotool', 'getwindowgeometry', '--shell', wid)).decode()
                    width = re.search(r'^WIDTH=(\d+)$', geometry, re.M)
                    height = re.search(r'^HEIGHT=(\d+)$', geometry, re.M)
                    if width and height:
                        sizes.append((int(width[1])*int(height[1]),wid))
                if sizes:
                    self.meeting_window_id = max(sizes)[1]
        return state

    async def read_screen(self, margins_only=False, extra_regions=()):
        # Read controls/status, never infer host removal from meeting chat text.
        image = await self.command('ffmpeg', '-loglevel', 'error', '-f', 'x11grab', '-draw_mouse', '0',
                '-s', '1920x1200', '-i', self.display, '-frames:v', '1', '-threads', '1', '-f', 'image2pipe', '-vcodec', 'png', '-')
        if not image:
            self.lines = []
            return
        from PIL import Image, ImageOps
        source = Image.open(io.BytesIO(image))
        # Once admitted, OCR only toolbar/status margins while a meeting window
        # remains. Main content may itself contain words like "meeting ended".
        if margins_only:
            canvas = Image.new('RGB', (1920, 1200), 'white')
            canvas.paste(source.crop((0, 0, 1920, 100)), (0, 0))
            canvas.paste(source.crop((0, 1040, 1920, 1200)), (0, 1040))
            for rect in extra_regions:
                canvas.paste(source.crop(rect), rect[:2])
            out = io.BytesIO()
            canvas.save(out, format='PNG')
            image = out.getvalue()
        tsv = await self.command('tesseract', 'stdin', 'stdout', '-l', 'eng', '--psm', '11', 'tsv', input=image, timeout=12)
        self.lines = ocr_lines(tsv.decode(errors='replace'))
        async def enhanced(region,rect,scale,psm='11',button=False):
            if button:
                region=ImageOps.invert(region.convert('L').point(lambda v:255 if v>200 else 0))
            region=region.resize((region.width*scale,region.height*scale))
            buffer=io.BytesIO()
            region.save(buffer,format='PNG')
            result=await self.command('tesseract','stdin','stdout','-l','eng','--psm',psm,'tsv',
                                      input=buffer.getvalue(),timeout=12)
            self.lines.extend((label,rect[0]+x//scale,rect[1]+y//scale)
                              for label,x,y in ocr_lines(result.decode(errors='replace')))
        if not self.share_seen and time.monotonic()-self._last_header_ocr>5:
            self._last_header_ocr=time.monotonic()
            await enhanced(source.crop((0,0,1920,100)),(0,0,1920,100),3)
        if self.audio_window_rect:
            rect=self.audio_window_rect
            region=source.crop(rect)
            await enhanced(region,rect,2)
            for box in blue_audio_buttons(region):
                absolute=(rect[0]+box[0],rect[1]+box[1],rect[0]+box[2],rect[1]+box[3])
                await enhanced(region.crop(box),absolute,3,psm='7',button=True)

    async def park_pointer(self):
        # The bottom/right edges reveal Zoom's auto-hidden controls. The
        # pointer itself is excluded by FFmpeg; leave it on the content edge.
        await self.command('xdotool', 'mousemove', '4', '600')

    async def join_computer_audio(self, refresh=True):
        """Positive Join actions only; never open the speaker/mic test wizard."""
        if refresh:
            await self.read_screen()
        footer = next(((x,y) for label,x,y in self.lines
                       if label.strip().casefold() == 'join audio' and y > 1120), None)
        if footer:
            await self.command('xdotool', 'mousemove', str(footer[0]), str(footer[1]), 'click', '1')
            await asyncio.sleep(0.3)
            await self.read_screen()
        join = next(((x, y) for label, x, y in self.lines
                     if re.fullmatch(r'(?:join\s+)?with computer audio|join with computer audio',
                                     label.strip(), re.I)), None)
        if join:
            x, y = join
            await self.command('xdotool', 'mousemove', str(x), str(y), 'click', '1')
            await self.park_pointer()
            self.audio_window_rect = None
            # Refresh before any Mute action: the footer may just have changed.
            await asyncio.sleep(0.3)
            await self.read_screen()
            self.audio_dialog_open = any('test speaker and microphone' in label.lower()
                     or re.fullmatch(r'(?:join\s+)?with computer audio|join with computer audio',label.strip(),re.I)
                     for label, _, _ in self.lines)
            return not self.audio_dialog_open
        self.audio_dialog_open = any('test speaker and microphone' in label.lower()
                     or label.strip().casefold() == 'join audio' and y > 1120
                     for label, _, y in self.lines)
        return not self.audio_dialog_open

    async def close_auxiliary_windows(self):
        """Close only named auxiliary Zoom windows, never the meeting itself."""
        if not shutil.which('wmctrl'):
            return
        ids = (await self.command('xdotool', 'search', '--onlyvisible', '--class', 'zoom')).decode().split()
        for wid in ids[:20]:
            if wid == self.meeting_window_id:
                continue
            title = (await self.command('xdotool', 'getwindowname', wid)).decode(errors='replace').strip()
            if re.fullmatch(r'(?:Participants|Meeting Chat|In-Meeting Chat|Notes)', title, re.I):
                await self.command('wmctrl', '-i', '-c', hex(int(wid)))

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
        # Snapshot may have read only the footer; refresh the central dialog.
        for _ in range(3):
            if await self.join_computer_audio():
                break
            if self.stop_event.is_set():
                return
        if self.audio_dialog_open:
            raise RuntimeError('Zoom kompyuter audiosiga ulanmagan; audio oynasi ustidan yozuv boshlanmadi.')
        # Exact labels only: never toggle Unmute / Start Video / Share Screen.
        for label in ('Mute', 'Stop Video'):
            await self.click_label(label)
        try:
            await self.ensure_fullscreen()
            await self.close_auxiliary_windows()
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
        graceful = self.process is not None and self.process.returncode != -signal.SIGKILL
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
                        if proc is self.process:
                            graceful = False
                        os.killpg(proc.pid, signal.SIGKILL)
                        await proc.wait()
        try:
            if graceful:
                outcome = await self.auth_store.backup()
                logger.info('Desktop profile backup: %s (account login not verified)', outcome)
        except Exception as exc:
            logger.warning('Desktop profile backup failed: %s; local profile preserved',
                           str(exc) if isinstance(exc, AuthStoreError) else type(exc).__name__)
        finally:
            await self.auth_store.close()
        if self.mic_module:
            await self.command('pactl', 'unload-module', self.mic_module)
            self.mic_module = None
        if self.log:
            self.log.close()
        if self.lock_file:
            self.lock_file.close()
