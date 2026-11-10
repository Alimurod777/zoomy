import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
from login_link import BrowserLoginLink


class LoginLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.browser = SimpleNamespace(verified_state=AsyncMock(return_value=None),
            page=SimpleNamespace(goto=AsyncMock()))
        self.link = BrowserLoginLink(self.browser)
        self.client = TestClient(TestServer(self.link.application()))
        await self.client.start_server()
        self.base = f"/s/{self.link.token}/"

    async def asyncTearDown(self):
        await self.client.close()

    async def test_secret_link_is_required_and_not_cached(self):
        response = await self.client.get("/s/wrong/")
        self.assertEqual(response.status, 404)
        response = await self.client.get(self.base)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    async def test_expired_link_is_rejected(self):
        self.link.deadline = time.monotonic() - 1
        response = await self.client.get(self.base)
        self.assertEqual(response.status, 410)

    async def test_other_origin_cannot_control_or_save_session(self):
        response = await self.client.post(self.base + "save", headers={"Origin": "https://example.com"})
        self.assertEqual(response.status, 403)
        self.browser.verified_state.assert_not_awaited()

    async def test_unverified_login_cannot_be_saved(self):
        with patch("login_link.auth_store.save", new=AsyncMock()) as save:
            response = await self.client.post(self.base + "save")
            result = await response.json()
        self.assertFalse(result["ok"])
        save.assert_not_awaited()

    async def test_successful_save_revokes_link(self):
        state = {"cookies": []}
        self.browser.verified_state.return_value = state
        with patch("login_link.auth_store.save", new=AsyncMock(return_value={"mongo": True})) as save:
            response = await self.client.post(self.base + "save")
            self.assertTrue((await response.json())["ok"])
        save.assert_awaited_once_with(state)
        self.assertEqual((await self.client.get(self.base)).status, 410)

    async def test_websocket_relays_vnc_binary_data(self):
        async def vnc(reader, writer):
            try:
                writer.write(b"RFB 003.008\n")
                await writer.drain()
                data = await reader.read(128)
                writer.write(data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(vnc, "127.0.0.1", 0)
        self.link.vnc_port = server.sockets[0].getsockname()[1]
        try:
            async with self.client.ws_connect(self.base + "websockify", protocols=("binary",)) as ws:
                self.assertEqual((await ws.receive()).data, b"RFB 003.008\n")
                await ws.send_bytes(b"test-message")
                self.assertEqual((await ws.receive()).data, b"test-message")
        finally:
            server.close()
            await server.wait_closed()

    async def test_typing_requires_a_selected_zoom_field(self):
        self.browser.page.evaluate = AsyncMock(return_value=False)
        self.browser.page.keyboard = SimpleNamespace(insert_text=AsyncMock(), press=AsyncMock())
        response = await self.client.post(self.base + "type", json={"text": "test"})
        self.assertEqual(response.status, 409)
        self.browser.page.keyboard.insert_text.assert_not_awaited()

    async def test_unicode_text_is_inserted_once_then_enter_is_optional(self):
        self.browser.page.evaluate = AsyncMock(return_value=True)
        self.browser.page.keyboard = SimpleNamespace(insert_text=AsyncMock(), press=AsyncMock())
        response = await self.client.post(self.base + "type",
            json={"text": "O‘zbekcha matn ✅", "enter": True})
        self.assertTrue((await response.json())["ok"])
        self.browser.page.keyboard.insert_text.assert_awaited_once_with("O‘zbekcha matn ✅")
        self.assertEqual([c.args[0] for c in self.browser.page.keyboard.press.call_args_list],
                         ["ControlOrMeta+A", "Enter"])

    async def test_guidance_reports_password_stage_without_reading_a_value(self):
        self.browser.page.evaluate = AsyncMock(return_value={"editable": True, "type": "password", "name": ""})
        response = await self.client.get(self.base + "status")
        result = await response.json()
        self.assertEqual(result["phase"], "password")
        self.assertNotIn("value", result)

    async def test_empty_typing_request_is_rejected(self):
        response = await self.client.post(self.base + "type", json={"text": ""})
        self.assertEqual(response.status, 400)

    async def test_browser_viewport_matches_client_and_has_safe_bounds(self):
        session = SimpleNamespace(send=AsyncMock(side_effect=[{"windowId": 12}, {}]), detach=AsyncMock())
        self.browser.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session), pages=[])
        self.browser.display_num = 94
        proc = SimpleNamespace(returncode=0, wait=AsyncMock())
        with patch("login_link.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as command:
            response = await self.client.post(self.base + "viewport", json={"width": 390, "height": 600})
        result = await response.json()
        self.assertEqual((result["width"], result["height"]), (500, 600))
        self.assertIn("clip:500x600+0+0", command.call_args.args)
        bounds = session.send.call_args_list[1].args[1]["bounds"]
        self.assertEqual((bounds["width"], bounds["height"]), (500, 600))
        session.detach.assert_awaited_once()

    async def test_invalid_viewport_is_rejected(self):
        response = await self.client.post(self.base + "viewport", json={"width": "bad"})
        self.assertEqual(response.status, 400)
