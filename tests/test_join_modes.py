import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
from join_modes import mode_for,set_mode
from recorder import ZoomRecorder,WebAuthRequired,NativeAuthRequired,DesktopRequired,MeetingJoinError
from meeting_state import classify_meeting

URL='https://example.zoom.us/j/12345678900'
STATE={'cookies':[{'name':'private','value':'secret'}],'origins':[]}


class PreferenceTests(unittest.TestCase):
    def test_private_persistent_preferences_are_separate_per_user(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,ZOOM_JOIN_MODE='auto'):
            path=Path(directory)/'preferences.json'
            set_mode(7,'app-auth',path)
            set_mode(8,'web-guest',path)
            self.assertEqual(mode_for(7,path),'app-auth')
            self.assertEqual(mode_for(8,path),'web-guest')
            self.assertEqual(mode_for(9,path),'auto')
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):set_mode(9,'bad',path)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rec=ZoomRecorder(use_profile_name=False)
        self.rec._monitor_desktop_handoff=AsyncMock()
        self.rec._choose_join_target=AsyncMock(return_value=(URL,False))
        self.contexts=[]
        async def context(**kwargs):
            obj=SimpleNamespace(new_page=AsyncMock(return_value=Mock()),close=AsyncMock())
            self.contexts.append((obj,kwargs))
            return obj
        self.browser=SimpleNamespace(new_context=AsyncMock(side_effect=context))

    async def test_auto_guest_first_does_not_depend_on_mongo(self):
        self.rec._join_meeting=AsyncMock(return_value=True)
        store=SimpleNamespace(configured=True,load=AsyncMock(side_effect=OSError('offline')))
        with patch('recorder.auth_store',store):
            await self.rec._enter_web(self.browser,URL,URL,'auto')
        store.load.assert_not_awaited()
        self.assertIsNone(self.contexts[0][1]['storage_state'])

    async def test_auto_retries_web_auth_only_after_guest_is_rejected(self):
        self.rec._join_meeting=AsyncMock(side_effect=[WebAuthRequired('required'),True])
        store=SimpleNamespace(configured=True,load=AsyncMock(return_value=STATE))
        with patch('recorder.auth_store',store):
            await self.rec._enter_web(self.browser,URL,URL,'auto')
        self.assertEqual(len(self.contexts),2)
        self.contexts[0][0].close.assert_awaited_once()
        self.assertIsNone(self.contexts[0][1]['storage_state'])
        self.assertEqual(self.contexts[1][1]['storage_state'],STATE)

    async def test_missing_web_auth_can_switch_to_app(self):
        self.rec._join_meeting=AsyncMock(side_effect=WebAuthRequired('required'))
        with patch('recorder.auth_store',SimpleNamespace(configured=True,load=AsyncMock(return_value=None))):
            with self.assertRaises(DesktopRequired):
                await self.rec._enter_web(self.browser,URL,URL,'auto')

    async def test_explicit_web_guest_does_not_use_saved_credentials(self):
        self.rec._join_meeting=AsyncMock(return_value=True)
        with patch('recorder.auth_store',SimpleNamespace(configured=True,load=AsyncMock(side_effect=AssertionError()))):
            await self.rec._enter_web(self.browser,URL,URL,'web-guest')
        self.assertIsNone(self.contexts[0][1]['storage_state'])

    async def test_explicit_web_auth_without_credentials_fails_before_join(self):
        with patch('recorder.auth_store',SimpleNamespace(configured=True,load=AsyncMock(return_value=None))):
            with self.assertRaisesRegex(MeetingJoinError,'zoomlogin'):
                await self.rec._enter_web(self.browser,URL,URL,'web-auth')
        self.browser.new_context.assert_not_awaited()

    async def test_app_guest_then_app_auth_is_bounded_to_two_attempts(self):
        self.rec._join_mode='auto'
        attempts=[]
        async def run(url):
            attempts.append((self.rec._native_guest,self.rec._native_require_auth))
            if len(attempts)==1:raise NativeAuthRequired('required')
        self.rec._record_native=AsyncMock(side_effect=run)
        await self.rec._record_desktop(URL)
        self.assertEqual(attempts,[(True,False),(False,True)])

    async def test_explicit_app_guest_cannot_silently_use_account(self):
        self.rec._join_mode='app-guest'
        self.rec._record_native=AsyncMock(side_effect=NativeAuthRequired('required'))
        with self.assertRaisesRegex(MeetingJoinError,'zoomapplogin'):
            await self.rec._record_desktop(URL)
        self.rec._record_native.assert_awaited_once()

    async def test_stop_interrupts_a_hung_native_snapshot(self):
        entered=asyncio.Event()
        async def hung():
            entered.set()
            await asyncio.Event().wait()
        task=asyncio.create_task(self.rec._native_snapshot(SimpleNamespace(snapshot=hung)))
        await entered.wait()
        await self.rec.stop()
        self.assertIsNone(await asyncio.wait_for(task,.5))

    async def test_stop_interrupts_native_startup_and_closes_resources(self):
        entered=asyncio.Event()
        async def opening(*args,**kwargs):
            entered.set()
            await asyncio.Event().wait()
        native=SimpleNamespace(open=opening,close=AsyncMock(),share_seen=False,terminal_reference=None)
        with patch('zoom_desktop.NativeZoom',return_value=native):
            task=asyncio.create_task(self.rec._record_native(URL))
            await entered.wait()
            await self.rec.stop()
            await asyncio.wait_for(task,.5)
        native.close.assert_awaited_once()

    async def test_authentication_only_errors_are_not_invalid_meeting_ids(self):
        self.assertEqual(classify_meeting('This meeting is for authorized attendees only'),'auth_required')
        self.assertEqual(classify_meeting('This meeting requires authentication'),'auth_required')

    async def test_native_auth_wait_expires_instead_of_waiting_for_host_deadline(self):
        rec=ZoomRecorder(user_id=999,use_profile_name=False)
        clock=SimpleNamespace(now=0)
        async def delay(seconds):
            clock.now+=20
            return False
        rec._wait_or_stop=AsyncMock(side_effect=delay)
        native=SimpleNamespace(open=AsyncMock(),close=AsyncMock(),snapshot=AsyncMock(return_value='auth_required'),
                               share_seen=False,terminal_reference=None)
        with patch.dict(os.environ,OWNER_TELEGRAM_ID='999',ZOOM_RECORD_AUTH_TIMEOUT='30'), \
                patch('zoom_desktop.NativeZoom',return_value=native), \
                patch('recorder.time',SimpleNamespace(monotonic=lambda:clock.now)):
            with self.assertRaisesRegex(MeetingJoinError,'login kutish vaqti tugadi'):
                await rec._record_native(URL)
        native.close.assert_awaited_once()


if __name__=='__main__':unittest.main()
