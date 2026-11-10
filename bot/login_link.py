"""An expiring, secret-link noVNC desktop for a Zoom browser login."""
import asyncio
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import tempfile
import time
from urllib.parse import urlparse

from aiohttp import web, WSMsgType
from auth_store import auth_store

ROOT = Path(__file__).resolve().parents[1]

def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BrowserLoginLink:
    def __init__(self, browser, ttl=900, on_saved=None):
        self.browser = browser
        self.deadline = time.monotonic() + ttl
        self.token = secrets.token_urlsafe(32)
        self.password = secrets.token_urlsafe(6)
        self.on_saved = on_saved
        self.saved = None
        self.url = None
        self.port = None
        self.vnc_port = None
        self.runner = None
        self.vnc = None
        self.tunnel = None
        self.tunnel_reader = None
        self.temp = None
        self.sockets = set()
        self.lock = asyncio.Lock()

    def application(self):
        @web.middleware
        async def gate(request, handler):
            if not hmac.compare_digest(request.match_info.get("token", ""), self.token):
                raise web.HTTPNotFound()
            if time.monotonic() >= self.deadline or self.saved is not None:
                return web.json_response({"message": "Havola muddati tugagan yoki login saqlangan."}, status=410)
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                raise web.HTTPForbidden()
            response = await handler(request)
            response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"})
            return response
        app = web.Application(middlewares=[gate])
        app.router.add_get('/s/{token}/', self.index)
        app.router.add_get('/s/{token}/assets/{path:.*}', self.asset)
        app.router.add_get('/s/{token}/websockify', self.websocket)
        app.router.add_post('/s/{token}/save', self.save)
        app.router.add_post('/s/{token}/reload', self.reload)
        app.router.add_get('/s/{token}/status', self.status)
        app.router.add_post('/s/{token}/type', self.type_text)
        app.router.add_post('/s/{token}/enter', self.enter)
        app.router.add_post('/s/{token}/viewport', self.viewport)
        return app

    async def index(self, request):
        template = (ROOT / "bot/login_link.html").read_text()
        return web.Response(text=template.replace("__PASSWORD__", json.dumps(self.password)), content_type="text/html")

    async def target_page(self):
        # SSO can open another tab/window; type in the page the user focused.
        context = getattr(self.browser, "context", None)
        if context:
            for page in reversed(context.pages):
                if not page.is_closed():
                    try:
                        if await page.evaluate("document.hasFocus()"):
                            return page
                    except Exception:
                        continue
        return self.browser.page

    async def status(self, request):
        try:
            page = await self.target_page()
            focused = await page.evaluate("""() => {
                const e=document.activeElement;
                return {editable:!!e && (['INPUT','TEXTAREA'].includes(e.tagName)||e.isContentEditable),
                        type:e?.type||'', name:e?.getAttribute('autocomplete')||e?.name||e?.id||''};
            }""")
            label = (focused["type"] + " " + focused["name"]).lower()
            if not focused["editable"]:
                phase, _ = await self.browser.inputs()
            elif "password" in label:
                phase = "password"
            elif any(word in label for word in ("code", "otp", "one-time", "numeric")):
                phase = "code"
            else:
                phase = "email"
            hints = {"email": "Email maydonini bosing → emailni kiriting → Enter.",
                "password": "Parol maydonini bosing → parolni kiriting → Enter.",
                "code": "Tasdiqlash kodi maydonini bosing → kodni kiriting → Enter.",
                "manual": "Zoom oynasidagi tugmalar bilan davom eting. Kirgach Loginni saqlashni bosing."}
            return web.json_response({"phase": phase, "focused": focused["editable"], "message": hints[phase]})
        except Exception:
            return web.json_response({"phase": "manual", "focused": False,
                "message": "Zoom oynasi yuklanmoqda. Kerakli maydonni bosing."})

    async def type_text(self, request):
        try:
            data = await request.json()
            text = data.get("text", "")
            if not isinstance(text, str) or not text or len(text) > 4096:
                raise ValueError()
        except Exception:
            return web.json_response({"ok": False, "message": "Kiritish uchun matn yozing (4096 belgigacha)."}, status=400)
        async with self.lock:
            try:
                page = await self.target_page()
                editable = await page.evaluate("""() => {
                    const e=document.activeElement;
                    return !!e && !e.disabled && !e.readOnly && ((e.tagName==='INPUT' && !['checkbox','radio','button','submit'].includes(e.type))
                        || e.tagName==='TEXTAREA'||e.isContentEditable);
                }""")
                if not editable:
                    return web.json_response({"ok": False, "message": "Avval Zoom oynasida email, parol yoki kod maydonini bosing."}, status=409)
                if data.get("replace", True):
                    await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.insert_text(text)
                if data.get("enter", False):
                    await page.keyboard.press("Enter")
                return web.json_response({"ok": True, "message": "Matn kiritildi. Keyingi qadam uchun Enter yoki Zoom’dagi Next tugmasini bosing."})
            except Exception:
                return web.json_response({"ok": False, "message": "Matn kiritilmadi. Zoom’dagi maydonni qayta bosing."}, status=503)

    async def enter(self, request):
        async with self.lock:
            try:
                await (await self.target_page()).keyboard.press("Enter")
                return web.json_response({"ok": True, "message": "Enter bosildi. Zoom javobini kuting."})
            except Exception:
                return web.json_response({"ok": False, "message": "Zoom oynasini bosing va qayta urinib ko‘ring."}, status=503)

    async def viewport(self, request):
        """Resize the login browser and capture area, keeping the same session."""
        try:
            data = await request.json()
            width = max(500, min(1280, int(data["width"])))
            height = max(520, min(960, int(data["height"])))
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False}, status=400)
        async with self.lock:
            session = None
            try:
                page = await self.target_page()
                session = await self.browser.context.new_cdp_session(page)
                window = await session.send("Browser.getWindowForTarget")
                await session.send("Browser.setWindowBounds", {"windowId": window["windowId"],
                    "bounds": {"windowState": "normal", "left": 0, "top": 0, "width": width, "height": height}})
                proc = await asyncio.create_subprocess_exec("x11vnc", "-display", f":{self.browser.display_num}",
                    "-remote", f"clip:{width}x{height}+0+0",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=4)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise RuntimeError()
                return web.json_response({"ok": proc.returncode == 0, "width": width, "height": height})
            except Exception:
                return web.json_response({"ok": False})
            finally:
                if session:
                    await session.detach()

    async def asset(self, request):
        root = (ROOT / ".runtime/root/usr/share/novnc").resolve()
        path = (root / request.match_info["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def websocket(self, request):
        ws = web.WebSocketResponse(protocols=("binary",), max_msg_size=4 * 1024 * 1024, heartbeat=30)
        await ws.prepare(request)
        self.sockets.add(ws)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.vnc_port)

        async def to_vnc():
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    writer.write(message.data)
                    await writer.drain()

        async def to_browser():
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
        tasks = [asyncio.create_task(to_vnc()), asyncio.create_task(to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            await ws.close()
            self.sockets.discard(ws)
        return ws

    async def save(self, request):
        async with self.lock:
            if self.saved is not None:
                return web.json_response({"ok": True, "message": "Login allaqachon saqlangan."})
            try:
                state = await self.browser.verified_state()
                if state is None:
                    return web.json_response({"ok": False, "message": "Zoom login hali tasdiqlanmadi. Zoom sahifasida kirishni yakunlang."})
                self.saved = await auth_store.save(state)
            except Exception:
                # No browser call log, cookies, passwords or DB URI in HTTP errors.
                return web.json_response({"ok": False, "message": "Saqlash bajarilmadi. Login yoki MongoDB ulanishini tekshirib qayta bosing."}, status=503)
            if self.on_saved:
                asyncio.create_task(self._notify_saved())
            return web.json_response({"ok": True, "message": "✅ Zoom sessiyasi saqlandi. Bu oynani yopishingiz mumkin."})

    async def _notify_saved(self):
        await asyncio.sleep(3)  # Let the successful save response reach the user.
        await self.on_saved()

    async def reload(self, request):
        async with self.lock:
            if self.saved is not None:
                raise web.HTTPGone()
            try:
                await self.browser.page.goto("https://zoom.us/signin", wait_until="commit", timeout=10000)
            except Exception:
                pass  # The desktop stays usable even on a slow navigation.
            return web.json_response({"message": "Zoom oynasi qayta ochildi. Brauzer ichida davom eting."})

    async def open(self):
        cloudflared = shutil.which("cloudflared")
        if not cloudflared:
            raise RuntimeError("Run bash scripts/setup-login-link.sh first")
        self.temp = tempfile.TemporaryDirectory(prefix="zoomy-link-", dir="/tmp")
        password_file = Path(self.temp.name) / "vnc-password"
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(self.password + "\n")
        self.vnc_port = available_port()
        self.vnc = await asyncio.create_subprocess_exec(
            "x11vnc", "-display", f":{self.browser.display_num}", "-listen", "127.0.0.1",
            "-rfbport", str(self.vnc_port), "-passwdfile", str(password_file),
            "-forever", "-shared", "-quiet", "-noxdamage",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        for _ in range(50):
            if self.vnc.returncode is not None:
                raise RuntimeError("Remote desktop could not start")
            try:
                r, w = await asyncio.open_connection("127.0.0.1", self.vnc_port)
                w.close()
                await w.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Remote desktop startup timed out")
        self.runner = web.AppRunner(self.application(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.tunnel = await asyncio.create_subprocess_exec(
            cloudflared, "tunnel", "--no-autoupdate", "--protocol", "http2",
            "--url", f"http://127.0.0.1:{self.port}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        ready = asyncio.get_running_loop().create_future()

        async def watch():
            base = None
            async for raw in self.tunnel.stderr:
                line = raw.decode(errors="replace")
                found = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if found:
                    base = found.group(0)
                if base and "Registered tunnel connection" in line and not ready.done():
                    ready.set_result(base)
            if not ready.done():
                ready.set_exception(RuntimeError("Temporary public tunnel could not connect"))
        self.tunnel_reader = asyncio.create_task(watch())
        base = await asyncio.wait_for(ready, timeout=45)
        self.url = f"{base}/s/{self.token}/"
        return self.url

    async def close(self):
        for ws in tuple(self.sockets):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()
        for process in (self.tunnel, self.vnc):
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if self.tunnel_reader:
            self.tunnel_reader.cancel()
            await asyncio.gather(self.tunnel_reader, return_exceptions=True)
        if self.temp:
            self.temp.cleanup()
