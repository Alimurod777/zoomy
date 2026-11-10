import asyncio
import copy
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
from zoom_browser_auth import ZoomBrowserLogin
from zoom_auth_bot import ZoomAuthBot

LOGIN_HTML = '''<title>Sign In | Zoom</title><body><main></main><script>
let step=0;
function show() {
const views = [
 '<input type="email" name="email"><button onclick="step++;show()">Next</button>',
 '<input type="password" name="password"><button onclick="step++;show()">Sign In</button>',
 '<input autocomplete="one-time-code" name="code"><button onclick="finish()">Verify</button>'
]; document.querySelector('main').innerHTML=views[step];
}
function finish() {document.cookie='zoom-session=test; path=/; secure';
history.replaceState({}, '', '/profile'); document.title='Profile | Zoom';
document.body.innerHTML='<h1>Profile</h1>';}
show();</script>'''


class BrowserAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True, args=["--no-sandbox"])
        self.context = await self.browser.new_context()

        async def route(request_route):
            request = request_route.request
            if request.url.startswith("https://zoom.us/profile"):
                if "zoom-session=test" in request.headers.get("cookie", ""):
                    await request_route.fulfill(body="<title>Profile | Zoom</title><h1>Profile</h1>")
                else:
                    await request_route.fulfill(status=302, headers={"location": "/signin"})
            else:
                await request_route.fulfill(body=LOGIN_HTML)
        await self.context.route("https://zoom.us/**", route)
        self.login = ZoomBrowserLogin()
        self.login.context = self.context
        self.login.page = await self.context.new_page()
        await self.login.page.goto("https://zoom.us/signin")
        # No network in these fixtures; no need for a production settle delay.
        self.login.settle = AsyncMock()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_email_password_code_and_verified_state(self):
        self.assertEqual((await self.login.inputs())[0], "email")
        await self.login.submit("person@example.com")
        self.assertEqual((await self.login.inputs())[0], "password")
        await self.login.submit("test-password")
        self.assertEqual((await self.login.inputs())[0], "code")
        await self.login.submit("123456")
        state = await self.login.verified_state()
        self.assertEqual(state["cookies"][0]["name"], "zoom-session")

    async def test_unsigned_profile_is_not_saved(self):
        self.login.page.wait_for_timeout = AsyncMock()
        self.assertIsNone(await self.login.verified_state())

    async def test_multiple_otp_boxes_are_filled_in_order(self):
        await self.login.page.set_content(
            '<input maxlength="1"><input maxlength="1"><input maxlength="1"><input maxlength="1">'
            '<button>Verify</button>')
        await self.login.submit("12 34")
        values = await self.login.page.locator("input").evaluate_all("els => els.map(e => e.value)")
        self.assertEqual(values, ["1", "2", "3", "4"])

    async def test_foreign_page_does_not_receive_credentials(self):
        async def foreign_route(route):
            await route.fulfill(body='<input type="password">', content_type="text/html")
        await self.context.route("https://example.com/**", foreign_route)
        await self.login.page.goto("https://example.com/")
        with self.assertRaises(ValueError):
            await self.login.submit("test-password")
        self.assertEqual(await self.login.page.locator("input").input_value(), "")


class AuthBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ui = ZoomAuthBot()
        self.env = patch.dict(os.environ, OWNER_TELEGRAM_ID="7")
        self.env.start()
        self.addCleanup(self.env.stop)
        self.message = SimpleNamespace(text="test-code", reply_text=AsyncMock(), delete=AsyncMock())
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=7, type="private"), message=self.message,
            callback_query=None)
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
        self.context = SimpleNamespace(bot=self.bot)

    async def test_non_owner_cannot_open_auth(self):
        self.update.effective_user.id = 8
        with patch("zoom_auth_bot.ZoomBrowserLogin") as browser:
            await self.ui.start(self.update, self.context)
        browser.assert_not_called()

    async def test_owner_cannot_open_auth_in_group(self):
        self.update.effective_chat.type = "group"
        self.update.effective_chat.id = -1
        with patch("zoom_auth_bot.ZoomBrowserLogin") as browser:
            await self.ui.start(self.update, self.context)
        browser.assert_not_called()

    async def test_credentials_are_deleted_even_on_browser_error(self):
        self.ui.user_id = 7
        self.ui.browser = SimpleNamespace(submit=AsyncMock(side_effect=RuntimeError("test-password")))
        with self.assertLogs("zoom_auth_bot", level="WARNING") as logs:
            await self.ui.text(self.update, self.context)
        self.message.delete.assert_awaited_once()
        self.assertNotIn("test-password", str(logs.output))
        self.assertNotIn("test-password", str(self.bot.send_message.call_args_list))

    async def test_old_nonce_cannot_click_browser(self):
        self.ui.user_id = 7
        self.ui.nonce = "new"
        self.ui.browser = SimpleNamespace(click_control=AsyncMock())
        query = SimpleNamespace(data="zoomauth:old:b0", answer=AsyncMock())
        self.update.callback_query = query
        await self.ui.callback(self.update, self.context)
        self.ui.browser.click_control.assert_not_awaited()
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])

    async def test_browser_error_refreshes_controls_with_a_new_nonce(self):
        self.ui.user_id = 7
        self.ui.nonce = "old"
        self.ui.browser = SimpleNamespace(
            submit=AsyncMock(side_effect=RuntimeError("temporary browser failure")),
            inputs=AsyncMock(return_value=("code", [])),
            controls=AsyncMock(return_value=[]), screenshot=AsyncMock(return_value=b"preview"))
        await self.ui.text(self.update, self.context)
        self.assertIsNotNone(self.ui.nonce)
        self.assertNotEqual(self.ui.nonce, "old")
        self.bot.send_photo.assert_awaited_once()

    async def test_failed_verification_never_saves_auth(self):
        self.ui.user_id = 7
        self.ui.browser = SimpleNamespace(verified_state=AsyncMock(return_value=None))
        with patch("zoom_auth_bot.auth_store.save", new=AsyncMock()) as save:
            self.assertFalse(await self.ui._save(self.bot))
        save.assert_not_awaited()

    async def test_timeout_closes_browser(self):
        self.ui.user_id = 7
        browser = self.ui.browser = SimpleNamespace(close=AsyncMock())
        with patch.dict(os.environ, ZOOM_LOGIN_TIMEOUT="0"):
            await self.ui._timeout(self.bot, 7)
        browser.close.assert_awaited_once()
        self.assertFalse(self.ui.active(7))
