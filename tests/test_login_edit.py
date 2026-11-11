"""Editing controls on the real HTTP page; native keyboard calls remain offline."""
import asyncio
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient,TestServer
from playwright.async_api import async_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
from login_link import BrowserLoginLink
from zoom_desktop import NativeDesktopLink


class LoginEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.browser=SimpleNamespace(page=SimpleNamespace(evaluate=AsyncMock(return_value=True),keyboard=SimpleNamespace(press=AsyncMock())))
        self.link=BrowserLoginLink(self.browser)
        self.client=TestClient(TestServer(self.link.application()))
        await self.client.start_server()
        self.base=f'/s/{self.link.token}/'

    async def asyncTearDown(self):await self.client.close()

    async def test_clear_only_the_focused_editable_field(self):
        response=await self.client.post(self.base+'edit',json={'action':'clear'})
        self.assertEqual(response.status,200)
        self.assertEqual([c.args[0] for c in self.browser.page.keyboard.press.await_args_list],['ControlOrMeta+A','Backspace'])

    async def test_clear_refuses_to_operate_outside_text_fields(self):
        self.browser.page.evaluate.return_value=False
        response=await self.client.post(self.base+'edit',json={'action':'clear'})
        self.assertEqual(response.status,409)
        self.browser.page.keyboard.press.assert_not_awaited()

    async def test_unknown_edit_action_does_not_send_keys(self):
        response=await self.client.post(self.base+'edit',json={'action':'Alt+F4'})
        self.assertEqual(response.status,400)
        self.browser.page.keyboard.press.assert_not_awaited()


class NativeEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.native=SimpleNamespace(command=AsyncMock(return_value=b''))
        self.link=NativeDesktopLink(self.native)
        self.client=TestClient(TestServer(self.link.application()))
        await self.client.start_server()
        self.base=f'/s/{self.link.token}/'

    async def asyncTearDown(self):await self.client.close()

    async def test_native_clear_and_backspace_have_distinct_behavior(self):
        for action,keys in [('backspace',('BackSpace',)),('select_all',('ctrl+a',)),('clear',('ctrl+a','BackSpace'))]:
            self.native.command.reset_mock()
            response=await self.client.post(self.base+'edit',json={'action':action})
            self.assertEqual(response.status,200)
            self.native.command.assert_awaited_once_with('xdotool','key','--clearmodifiers',*keys,check=True)

    async def test_edit_requires_valid_token_origin_and_unexpired_link(self):
        self.assertEqual((await self.client.post('/s/wrong/edit',json={'action':'clear'})).status,404)
        self.assertEqual((await self.client.post(self.base+'edit',json={'action':'clear'},headers={'Origin':'https://example.test'})).status,403)
        self.link.deadline=0
        self.assertEqual((await self.client.post(self.base+'edit',json={'action':'clear'})).status,410)
        self.native.command.assert_not_awaited()

    async def test_native_failure_is_not_reported_as_success(self):
        self.native.command.side_effect=RuntimeError('action failed')
        response=await self.client.post(self.base+'edit',json={'action':'clear'})
        self.assertEqual(response.status,503)
        self.assertFalse((await response.json())['ok'])

    async def test_native_typing_append_keeps_secrets_off_process_arguments(self):
        response=await self.client.post(self.base+'type',json={'text':'test-password','replace':False})
        self.assertEqual(response.status,200)
        self.native.command.assert_awaited_once_with('xdotool','type','--clearmodifiers','--file','-',input=b'test-password',check=True)

    async def test_real_page_buttons_send_edit_and_append_requests(self):
        pw=await async_playwright().start()
        browser=await pw.chromium.launch(headless=True,args=['--no-sandbox'])
        try:
            page=await browser.new_page()
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            await page.route('**/assets/core/rfb.js',lambda route:route.fulfill(content_type='application/javascript',body="export default class RFB extends EventTarget {constructor(){super();setTimeout(()=>this.dispatchEvent(new Event('connect')),10)}}"))
            await page.goto(str(self.client.make_url(self.base)))
            await page.wait_for_timeout(100)
            await page.fill('#entry','mistyped')
            await page.click('#type')
            await page.wait_for_function("document.querySelector('#entry').value===''&&!document.querySelector('#type').disabled")
            self.native.command.reset_mock()
            await page.click('#backspace')
            await page.wait_for_function("!document.querySelector('#backspace').disabled")
            self.native.command.assert_awaited_once_with('xdotool','key','--clearmodifiers','BackSpace',check=True)
            self.native.command.reset_mock()
            await page.click('#clear')
            await page.wait_for_function("!document.querySelector('#clear').disabled")
            self.native.command.assert_awaited_once_with('xdotool','key','--clearmodifiers','ctrl+a','BackSpace',check=True)
            self.native.command.reset_mock()
            await page.uncheck('#replace')
            await page.fill('#entry','corrected')
            await page.click('#type')
            await page.wait_for_function("document.querySelector('#entry').value===''&&!document.querySelector('#type').disabled")
            self.native.command.assert_awaited_once_with('xdotool','type','--clearmodifiers','--file','-',input=b'corrected',check=True)
            self.assertEqual(errors,[])
        finally:
            await browser.close();await pw.stop()

if __name__=='__main__':unittest.main()
