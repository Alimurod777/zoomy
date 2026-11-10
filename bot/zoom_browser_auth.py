"""A real Zoom web sign-in, controlled through Telegram prompts/screenshots."""
import os
import re
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
from auth_store import validate_state, zoom_domain
from zoom_profile import profile_name_from_page


class LoginInputError(ValueError):
    pass


class ZoomBrowserLogin:
    def __init__(self):
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self.display_num = None
        self.pool = None
        self.actions = []

    async def open(self, manual=False):
        from recorder import DisplayPool
        self.pool = DisplayPool(base=int(os.getenv("ZOOM_LOGIN_DISPLAY", "90")), max_slots=1,
                                screen_size="1280x960" if manual else "1920x1200")
        try:
            self.display_num, display, _ = await self.pool.acquire()
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=False, env={**os.environ, "DISPLAY": display},
                args=["--no-sandbox", "--disable-dev-shm-usage", "--mute-audio",
                      "--disable-blink-features=AutomationControlled", "--window-position=0,0",
                      "--window-size=1280,960" if manual else "--window-size=1280,1000"])
            # A fresh context avoids importing someone else's existing login.
            self.context = await self.browser.new_context(
                **({"no_viewport": True} if manual else {"viewport": {"width": 1280, "height": 900}}))
            self.context.set_default_timeout(7000)
            self.page = await self.context.new_page()
            if manual:
                try:
                    # A person can use the desktop while Zoom's app/assets load.
                    await self.page.goto("https://zoom.us/signin", wait_until="commit", timeout=10000)
                except Exception:
                    pass
                return
            try:
                await self.page.goto("https://zoom.us/signin", wait_until="domcontentloaded", timeout=45000)
            except BrowserTimeout:
                # Slow third-party assets must not discard a visible login page.
                if not zoom_domain(urlparse(self.page.url).hostname or ""):
                    raise
            await self.settle()
            reject = self.page.get_by_role("button", name=re.compile(r"decline cookies|reject all", re.I))
            if await reject.count() and await reject.first.is_visible():
                try:
                    await reject.first.click(timeout=3000)
                except BrowserTimeout:
                    pass  # The owner can dismiss it using the screenshot UI.
        except BaseException:
            await self.close()
            raise

    async def settle(self):
        await self.page.wait_for_timeout(1500)

    async def inputs(self):
        """Classify visible fields, never reading their entered values."""
        if not zoom_domain(urlparse(self.page.url).hostname or ""):
            return "manual", []
        fields = self.page.locator("input:visible:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button])")
        descriptors = await fields.evaluate_all("""els => els.map(e => ({
            type: e.type, label: [e.id, e.name, e.autocomplete, e.placeholder,
            e.getAttribute('aria-label'), e.inputMode].join(' '), max: e.maxLength
        }))""")
        password = [fields.nth(i) for i, d in enumerate(descriptors) if d["type"] == "password"]
        if password:
            return "password", password[:1]
        codes = [fields.nth(i) for i, d in enumerate(descriptors)
                 if re.search(r"one-time|otp|verif|code|numeric", d["label"], re.I) or d["max"] == 1]
        if codes:
            return "code", codes
        emails = [fields.nth(i) for i, d in enumerate(descriptors)
                  if d["type"] == "email" or re.search(r"email|username|phone", d["label"], re.I)]
        if emails:
            return "email", emails[:1]
        return "manual", []

    async def submit(self, value: str):
        phase, fields = await self.inputs()
        if not fields:
            raise LoginInputError("Bu sahifada email/parol/kod maydoni aniqlanmadi. Tugma yoki /zoomclick dan foydalaning.")
        if not value or len(value) > 512:
            raise LoginInputError("Kiritilgan qiymat bo‘sh yoki juda uzun.")
        if phase == "code":
            value = re.sub(r"[\s-]", "", value)
            if len(fields) > 1:
                if len(value) != len(fields):
                    raise LoginInputError(f"{len(fields)} belgili tasdiqlash kodini yuboring.")
                for field, character in zip(fields, value):
                    await field.fill(character, timeout=5000)
            else:
                await fields[0].fill(value, timeout=5000)
        else:
            await fields[0].fill(value, timeout=5000)
        button = self.page.get_by_role("button", name=re.compile(
            r"^(next|continue|sign in|log in|verify|submit|confirm|verify and continue)$", re.I))
        for i in range(await button.count()):
            if await button.nth(i).is_visible() and await button.nth(i).is_enabled():
                await button.nth(i).click(timeout=5000)
                break
        else:
            # Some OTP forms submit automatically after the final digit.
            if await fields[-1].count() and await fields[-1].is_visible():
                await fields[-1].press("Enter", timeout=5000)
        await self.settle()

    async def controls(self):
        self.actions = []
        buttons = self.page.get_by_role("button")
        names = set()
        for i in range(await buttons.count()):
            button = buttons.nth(i)
            if not await button.is_visible() or not await button.is_enabled():
                continue
            name = (await button.inner_text()).strip()
            if not name or name in names or len(name) > 100:
                continue
            # Bind by an exact accessible name, rather than a shifting DOM index.
            target = self.page.get_by_role("button", name=name, exact=True)
            if await target.count() != 1:
                continue
            names.add(name)
            self.actions.append((name, target))
            if len(self.actions) == 6:
                break
        return [name for name, _ in self.actions]

    async def click_control(self, index: int):
        if index < 0 or index >= len(self.actions):
            raise LoginInputError("Tugma eskirgan. Oynani yangilang.")
        await self.actions[index][1].click(timeout=5000)
        await self.settle()

    async def click(self, x: int, y: int):
        if not (0 <= x < 1280 and 0 <= y < 900):
            raise LoginInputError("Koordinata: x=0..1279, y=0..899.")
        await self.page.mouse.click(x, y)
        await self.settle()

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(type="jpeg", quality=75,
            mask=[self.page.locator("input, textarea, [contenteditable=true]")])

    async def verified_state(self):
        """A fresh profile navigation must pass before any auth is persisted."""
        verification = await self.context.new_page()
        try:
            response = await verification.goto("https://zoom.us/profile",
                wait_until="domcontentloaded", timeout=30000)
            try:
                await verification.wait_for_function(
                    "document.title.toLowerCase().includes('profile')", timeout=8000)
            except Exception:
                return None
            parsed = urlparse(verification.url)
            if (not response or response.status >= 400 or not zoom_domain(parsed.hostname or "")
                    or not parsed.path.startswith("/profile")
                    or re.search(r"sign.?in|login", verification.url, re.I)):
                return None
            state = await self.context.storage_state()
            name = await profile_name_from_page(verification)
            if name:
                state["profile"] = {"display_name": name}
            return validate_state(state)
        finally:
            await verification.close()

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            try:
                if self.playwright:
                    await self.playwright.stop()
            finally:
                if self.pool and self.display_num is not None:
                    await self.pool.release(self.display_num)
                self.page = self.browser = self.playwright = self.context = None
                self.display_num = None
