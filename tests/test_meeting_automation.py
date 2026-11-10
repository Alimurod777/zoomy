import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import recorder as recorder_module
from recorder import ZoomRecorder
from meeting_state import classify_meeting, SNAPSHOT_JS
from zoom_profile import profile_name_from_page

URL = 'https://zoom.us/wc/97856007427/join'


class MeetingStateTests(unittest.TestCase):
    def test_admission_is_detected_even_while_join_url_remains(self):
        self.assertEqual(classify_meeting({'text': '', 'admitted': True}), 'admitted')

    def test_waiting_room_and_waiting_host_are_distinct(self):
        self.assertEqual(classify_meeting('Please wait, the host will let you in soon'), 'waiting_room')
        self.assertEqual(classify_meeting('Waiting for the host to start this meeting'), 'waiting_host')
        self.assertEqual(classify_meeting({'text':'Waiting for the host to start this meeting',
                                          'waiting_room':True}), 'waiting_room')

    def test_explicit_removal_and_meeting_end_are_terminal(self):
        self.assertEqual(classify_meeting('You have been removed from this meeting'), 'removed')
        self.assertEqual(classify_meeting('Meeting has been ended by host'), 'ended')

    def test_app_only_requirement_is_reported_separately(self):
        self.assertEqual(classify_meeting('This meeting requires the Zoom app'), 'app_required')
        self.assertNotEqual(classify_meeting('Join this meeting using the Zoom desktop or Join from your browser'), 'app_required')

    def test_unstable_network_warning_does_not_force_rejoin(self):
        self.assertEqual(classify_meeting({'text': 'Your internet connection is unstable', 'admitted': True}), 'admitted')


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.recorder = ZoomRecorder(on_waiting=AsyncMock(), on_error=AsyncMock())
        self.page = SimpleNamespace(is_closed=lambda: False, url=URL)
        self.recorder._open_join_page = AsyncMock()
        self.recorder._join_audio = AsyncMock()
        self.clock = SimpleNamespace(now=0.0)

        async def delay(seconds):
            self.clock.now += seconds
            return False
        self.recorder._wait_or_stop = AsyncMock(side_effect=delay)

    async def test_host_start_is_retried_and_then_recording_can_join(self):
        self.recorder._state = AsyncMock(side_effect=['waiting_host'] * 4 + ['admitted'])
        with patch.object(recorder_module, 'time', SimpleNamespace(monotonic=lambda: self.clock.now)), \
             patch.dict(os.environ, ZOOM_JOIN_RETRY_SECONDS='2'):
            self.assertTrue(await self.recorder._join_meeting(self.page, URL))
        self.assertGreaterEqual(self.recorder._open_join_page.await_count, 2)
        self.recorder._join_audio.assert_awaited_once()

    async def test_waiting_room_is_not_reloaded(self):
        self.recorder._state = AsyncMock(side_effect=['waiting_room'] * 5 + ['admitted'])
        with patch.object(recorder_module, 'time', SimpleNamespace(monotonic=lambda: self.clock.now)):
            self.assertTrue(await self.recorder._join_meeting(self.page, URL))
        self.recorder._open_join_page.assert_awaited_once()

    async def test_stop_cancels_slow_page_open_immediately(self):
        async def slow(*args):
            await asyncio.sleep(3600)
        self.recorder._open_join_page = AsyncMock(side_effect=slow)
        task = asyncio.create_task(self.recorder._join_meeting(self.page, URL))
        await asyncio.sleep(0)
        await self.recorder.stop()
        self.assertFalse(await asyncio.wait_for(task, timeout=1))

    async def test_app_required_is_not_treated_as_a_network_retry(self):
        self.recorder._state = AsyncMock(return_value='app_required')
        with self.assertRaisesRegex(RuntimeError, 'desktop'):
            await self.recorder._join_meeting(self.page, URL)
        self.recorder._open_join_page.assert_awaited_once()

    async def test_cancel_before_start_creates_no_recording(self):
        await self.recorder.stop()
        with patch('recorder.async_playwright') as browser:
            await self.recorder.record(URL)
        browser.assert_not_called()
        self.assertIsNone(self.recorder.output_path)
        self.assertFalse(self.recorder.is_recording)

    async def test_native_reconnect_preserves_capture_area_and_resumes(self):
        self.recorder._state = AsyncMock(return_value='admitted')
        self.recorder._join_meeting = AsyncMock()
        self.recorder._prepare_recording_view = AsyncMock()
        self.recorder._wait_for_audio = AsyncMock()
        self.recorder._resume_recording = Mock()
        self.recorder._capture_bounds = {'x': 682, 'y': 0, 'width': 556, 'height': 1112}
        original = dict(self.recorder._capture_bounds)
        self.assertIs(await self.recorder._recover_connection(self.page, URL), self.page)
        self.recorder._join_meeting.assert_not_awaited()
        self.recorder._resume_recording.assert_called_once()
        self.assertEqual(self.recorder._capture_bounds, original)

    async def test_removal_during_disconnect_grace_does_not_rejoin(self):
        self.recorder._state = AsyncMock(return_value='removed')
        self.recorder._join_meeting = AsyncMock()
        self.assertIsNone(await self.recorder._recover_connection(self.page, URL))
        self.recorder._join_meeting.assert_not_awaited()

    async def test_reconnect_waiting_room_keeps_the_existing_queue(self):
        self.recorder._state = AsyncMock(return_value='waiting_room')
        self.recorder._join_meeting = AsyncMock(return_value=True)
        self.recorder._prepare_recording_view = AsyncMock()
        self.recorder._wait_for_audio = AsyncMock()
        self.recorder._resume_recording = Mock()
        self.assertIs(await self.recorder._recover_connection(self.page, URL, waiting_room=True), self.page)
        self.assertTrue(self.recorder._join_meeting.call_args.kwargs['resume_waiting_room'])

    async def test_retry_budget_stops_without_deleting_partial_output(self):
        self.recorder._reconnect_count = 5
        self.recorder._segments = [Path('/recordings/part.mp4')]
        with patch.dict(os.environ, ZOOM_RECONNECT_ATTEMPTS='5'):
            self.assertIsNone(await self.recorder._recover_connection(self.page, URL))
        self.assertEqual(self.recorder._segments, [Path('/recordings/part.mp4')])

    async def test_automatic_filename_is_sanitized_and_meaningful(self):
        self.page.evaluate = AsyncMock(return_value='Python: O‘zbek/darsi - Zoom')
        name = await self.recorder._automatic_recording_name(self.page, URL)
        self.assertEqual(name, 'Python_ O‘zbek_darsi')
        self.page.evaluate.return_value = 'Zoom'
        self.assertRegex(await self.recorder._automatic_recording_name(self.page, URL), r'^Zoom_97856007427_\d{6}$')


class BrowserAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True, args=['--no-sandbox'])
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_join_keeps_prefilled_profile_name_and_does_not_wait_for_url_change(self):
        html = '''<input placeholder="Your Name" value="Profile Name">
        <button class="preview-join-button" onclick="this.outerHTML='<button class=footer__leave-btn>Leave</button>'">Join</button>'''
        async def route(r):
            await r.fulfill(body=html, content_type='text/html')
        await self.page.route(URL, route)
        rec = ZoomRecorder(guest_name='Old Bot Name', use_profile_name=True)
        rec._signed_in = True
        rec._wait_or_stop = AsyncMock(return_value=False)
        rec._join_audio = AsyncMock()
        self.assertTrue(await rec._join_meeting(self.page, URL))
        self.assertEqual(rec.guest_name, 'Profile Name')
        self.assertEqual(await self.page.locator('input').input_value(), 'Profile Name')
        self.assertTrue(self.page.url.endswith('/join'))

    async def test_chat_text_does_not_trigger_false_disconnect_or_end(self):
        await self.page.set_content('<button class="footer__leave-btn">Leave</button><div>Chat: the old meeting has ended. Reconnecting tomorrow.</div>')
        snapshot = await self.page.evaluate(SNAPSHOT_JS)
        self.assertEqual(classify_meeting(snapshot), 'admitted')

    async def test_current_zoom_profile_markup_yields_display_name(self):
        await self.page.set_content('<h2><span data-testid="base-info-fullname">First Last</span></h2><p class="grey-text">Zoom Nickname</p>')
        self.assertEqual(await profile_name_from_page(self.page), 'Zoom Nickname')

    async def test_host_admits_existing_waiting_browser_without_reload(self):
        requests = []
        html = '''<div>Please wait, the host will let you in soon</div><script>
        setTimeout(()=>document.body.innerHTML='<button class="footer__leave-btn">Leave</button>',1200);
        </script>'''
        async def route(r):
            requests.append(r.request.url)
            await r.fulfill(body=html, content_type='text/html')
        await self.page.route(URL, route)
        rec = ZoomRecorder(on_waiting=AsyncMock())
        rec._join_audio = AsyncMock()
        self.assertTrue(await asyncio.wait_for(rec._join_meeting(self.page, URL), timeout=5))
        self.assertEqual(len(requests), 1)
        self.assertTrue(any('waiting room' in c.args[0] for c in rec.on_waiting.call_args_list))
