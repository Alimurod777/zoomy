"""Offline checks for recording progress, lifecycle and Telegram backoff."""
import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from live_panel import RecordingPanel
from recorder import ZoomRecorder
from telegram.error import BadRequest, RetryAfter


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stats = dict(phase='recording', seconds=12, bytes=1_500_000,
                          name='<Lesson>', source='shared-screen')
        self.recorder = Mock(progress_snapshot=Mock(side_effect=lambda: self.stats.copy()))
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=44)),
                                   edit_message_text=AsyncMock())
        self.panel = RecordingPanel(self.bot, 7, 1, self.recorder)

    async def asyncTearDown(self):
        await self.panel.finish()

    async def test_updates_one_message_and_finishes_without_stop_button(self):
        await self.panel.start()
        self.stats.update(seconds=17, bytes=2_000_000)
        await self.panel.update()
        edit = self.bot.edit_message_text.call_args.kwargs
        self.assertEqual(edit['message_id'], 44)
        self.assertIn('00:00:17', edit['text'])
        self.assertIn('2.00 MB', edit['text'])
        self.assertIn('&lt;Lesson&gt;', edit['text'])
        self.assertEqual(edit['reply_markup'].inline_keyboard[0][0].callback_data, 'stop_session_1')
        await self.panel.finish()
        self.assertIsNone(self.panel.task)
        self.bot.send_message.assert_awaited_once()
        self.assertIsNone(self.bot.edit_message_text.call_args.kwargs['reply_markup'])

    async def test_refreshes_after_five_seconds(self):
        await self.panel.update()
        self.stats['seconds'] = 17
        async def tick(seconds):
            self.assertEqual(seconds, 5)
            if self.bot.edit_message_text.await_count:
                raise asyncio.CancelledError
        with patch('live_panel.asyncio.sleep', side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await self.panel._run()
        self.bot.edit_message_text.assert_awaited_once()

    async def test_deleted_panel_is_not_recreated_until_user_requests_it(self):
        await self.panel.update()
        self.stats['seconds'] += 5
        self.bot.edit_message_text.side_effect = BadRequest('Message to edit not found')
        await self.panel.update()
        await self.panel.update()
        self.assertTrue(self.panel.disabled)
        self.bot.send_message.assert_awaited_once()
        await self.panel.show()
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_start_honors_telegram_rate_limit(self):
        self.bot.send_message.side_effect = RetryAfter(30)
        await self.panel.start()
        self.assertGreaterEqual(self.panel.retry_delay, 30)

    async def test_error_is_preserved_during_final_cleanup(self):
        await self.panel.start()
        await self.panel.finish(error='Encoder failed')
        await self.panel.finish()
        self.assertIn('Xato bilan tugadi', self.bot.edit_message_text.call_args.kwargs['text'])


class ProgressTests(unittest.TestCase):
    def test_wait_pause_resume_and_final_processing_do_not_inflate_duration(self):
        recorder = ZoomRecorder(display=':99', sink='test.monitor')
        recorder.is_recording = True
        self.assertEqual(recorder.progress_snapshot()['phase'], 'waiting')
        recorder._record_start_time = 100
        with patch('recorder.time.monotonic', return_value=120):
            self.assertEqual(recorder.progress_snapshot()['seconds'], 20)
        recorder._paused = True
        recorder._pause_started = 120
        recorder._capture_stopped_at = 121
        with patch('recorder.time.monotonic', return_value=220):
            self.assertEqual(recorder.progress_snapshot()['seconds'], 20)
            self.assertEqual(recorder.progress_snapshot()['phase'], 'paused')
        recorder._paused = False
        recorder._pause_started = None
        recorder._paused_total = 100
        recorder._capture_stopped_at = None
        with patch('recorder.time.monotonic', return_value=230):
            self.assertEqual(recorder.progress_snapshot()['seconds'], 30)
        recorder._capture_stopped_at = 230
        with patch('recorder.time.monotonic', return_value=500):
            self.assertEqual(recorder.progress_snapshot()['seconds'], 30)
            self.assertEqual(recorder.progress_snapshot()['phase'], 'processing')
        recorder._native_final_duration = 29.6
        recorder.is_recording = False
        self.assertEqual(recorder.progress_snapshot()['seconds'], 29)
        self.assertEqual(recorder.progress_snapshot()['phase'], 'finished')

    def test_size_sums_segments_and_uses_decimal_mb(self):
        recorder = ZoomRecorder(display=':99', sink='test.monitor')
        with tempfile.TemporaryDirectory() as directory:
            recorder._segments = [Path(directory)/'one.mp4', Path(directory)/'two.mp4']
            for path in recorder._segments:
                with path.open('wb') as output:
                    output.truncate(750_000)
            self.assertEqual(recorder.progress_snapshot()['bytes'], 1_500_000)
            self.assertEqual(recorder.file_size_str(), '1.50 MB')


class BrowserCrashTests(unittest.IsolatedAsyncioTestCase):
    async def test_crash_event_is_disconnection_without_dom_access(self):
        recorder = ZoomRecorder(display=':99', sink='test.monitor')
        page = Mock()
        recorder._track_browser_page(page)
        page.on.call_args.args[1](page)
        self.assertEqual(await recorder._state(page), 'disconnected')
        page.evaluate.assert_not_called()

    async def test_crash_exception_cannot_leave_watch_loop_recording_forever(self):
        recorder = ZoomRecorder(display=':99', sink='test.monitor')
        page = Mock(is_closed=Mock(return_value=False), url='https://zoom.us/wc/123/join',
                    evaluate=AsyncMock(side_effect=RuntimeError('Page.evaluate: Target crashed')))
        self.assertEqual(await recorder._watch_meeting(page), 'disconnected')
        self.assertIn(id(page), recorder._crashed_page_ids)

    async def test_recovery_replaces_crashed_tab_and_keeps_existing_segments(self):
        recorder = ZoomRecorder(display=':99', sink='test.monitor', on_waiting=AsyncMock())
        replacement = Mock()
        old = Mock(is_closed=Mock(return_value=False), close=AsyncMock(),
                   context=SimpleNamespace(new_page=AsyncMock(return_value=replacement)))
        recorder._crashed_page_ids.add(id(old))
        recorder._segments = [Path('existing.mp4')]
        recorder.request_pause = Mock()
        recorder._notice_admission = AsyncMock()
        recorder._wait_or_stop = AsyncMock(return_value=False)
        recorder._state = AsyncMock(return_value='unknown')
        recorder._join_meeting = AsyncMock(return_value=True)
        recorder._prepare_recording_view = AsyncMock()
        recorder._wait_for_audio = AsyncMock()
        recorder._resume_recording = Mock()
        result = await recorder._recover_connection(old, 'https://zoom.us/wc/123/join')
        self.assertIs(result, replacement)
        recorder.request_pause.assert_called_once()
        old.close.assert_awaited_once()
        recorder._join_meeting.assert_awaited_once_with(replacement,
            'https://zoom.us/wc/123/join', resume_waiting_room=False)
        recorder._resume_recording.assert_called_once()
        self.assertEqual(recorder._segments, [Path('existing.mp4')])
