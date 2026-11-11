import asyncio
import os
from pathlib import Path
import sys
import tempfile
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from zoom_desktop import native_join_uri, native_state, ocr_lines, NativeDesktopLink, NativeZoom
from zoom_desktop import blue_audio_buttons
from recorder import ZoomRecorder, DesktopRequired
import recorder as recorder_module

URL = 'https://example.zoom.us/j/12345678900?pwd=encrypted%2Bpass&tk=exception&uname=old'


class DesktopStateTests(unittest.TestCase):
    def test_native_uri_preserves_host_and_invitation_tokens(self):
        uri = urlparse(native_join_uri(URL, 'O‘zbek ism'))
        self.assertEqual(uri.scheme, 'zoommtg')
        self.assertEqual(uri.hostname, 'example.zoom.us')
        self.assertEqual(parse_qs(uri.query), {'pwd':['encrypted+pass'], 'tk':['exception'],
            'uname':['O‘zbek ism'], 'action':['join'], 'confno':['12345678900'], 'zc':['0']})

    def test_native_uri_rejects_foreign_hosts_and_nonmeetings(self):
        for url in ('https://zoom.us.evil.test/j/12345678900', 'https://zoom.us/signin'):
            with self.assertRaises(ValueError):
                native_join_uri(url, 'Zoomy')

    def test_home_and_preview_are_never_admission(self):
        self.assertEqual(native_state([('Join a meeting Sign in', 0, 0)]), 'connecting')
        self.assertEqual(native_state([('Sign in to join this meeting', 0, 0)]), 'auth_required')
        self.assertEqual(native_state([('Video Preview Join', 0, 0)]), 'connecting')
        self.assertEqual(native_state([], meeting_window=True), 'connecting')

    def test_actual_controls_confirm_admission(self):
        self.assertEqual(native_state([('Participants Share Leave', 0, 0)]), 'admitted')
        self.assertEqual(native_state([], True, True), 'admitted')
        self.assertEqual(native_state([], False, True), 'connecting')

    def test_host_wait_room_and_removal_remain_distinct(self):
        for text, expected in [('Waiting for the host to start this meeting', 'waiting_host'),
                ('Please wait, the host will let you in soon', 'waiting_room'),
                ('You have been removed from this meeting', 'removed'),
                ('Invalid meeting ID', 'invalid')]:
            self.assertEqual(native_state([(text,0,0)]), expected)

    def test_ocr_ignores_uncertain_words_and_preserves_click_position(self):
        tsv = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        tsv += '5\t1\t1\t1\t1\t1\t10\t20\t40\t10\t95\tJoin\n'
        tsv += '5\t1\t1\t1\t1\t2\t60\t20\t60\t10\t96\tAudio\n'
        tsv += '5\t1\t2\t1\t1\t1\t0\t0\t9\t9\t10\tNoise\n'
        self.assertEqual(ocr_lines(tsv), [('Join Audio',65,25)])

    def test_only_auto_mode_requests_native_fallback(self):
        recorder=ZoomRecorder()
        with patch.dict(os.environ, ZOOM_JOIN_MODE='auto'):
            self.assertIsInstance(recorder._join_exception('app_required'), DesktopRequired)
            self.assertNotIsInstance(recorder._join_exception('auth_required'), DesktopRequired)
        with patch.dict(os.environ, ZOOM_JOIN_MODE='web'):
            self.assertNotIsInstance(recorder._join_exception('app_required'), DesktopRequired)


class NativeViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app=NativeZoom(':85','virtual_85',asyncio.Event())
        self.app.admitted=True
        self.app.command=AsyncMock(return_value=b'')

    async def test_pointer_parks_away_from_toolbar_edges(self):
        self.app.lines=[('Mute',25,1150)]
        self.assertTrue(await self.app.click_label('Mute'))
        self.app.command.assert_any_await('xdotool','mousemove','4','600')
        self.assertFalse(any('1199' in call.args for call in self.app.command.await_args_list))

    async def test_fullscreen_sets_state_once_instead_of_toggling(self):
        self.app.meeting_window_id='4096'
        with patch('zoom_desktop.shutil.which',return_value='/runtime/wmctrl'):
            await self.app.ensure_fullscreen()
            await self.app.ensure_fullscreen()
        self.app.command.assert_awaited_once_with('wmctrl','-i','-r','0x1000','-b','add,fullscreen')

    async def test_hide_video_panel_uses_only_opened_menu(self):
        self.app.lines=[('View Options',900,40)]
        async def menu(**kwargs):
            self.app.lines=[('Hide Video Panel',920,150),('Hide Video Panel',300,800)]
        self.app.read_screen=AsyncMock(side_effect=menu)
        await self.app.clean_shared_view()
        self.assertTrue(self.app._share_panel_hidden)
        self.app.command.assert_any_await('xdotool','mousemove','920','150','click','1')
        self.assertFalse(any('800' in call.args for call in self.app.command.await_args_list))
        before=self.app.command.await_count
        await self.app.clean_shared_view()
        self.assertEqual(self.app.command.await_count,before)

    async def test_already_hidden_panel_is_never_shown_again(self):
        self.app.lines=[('View Options',900,40)]
        async def menu(**kwargs):
            self.app.lines=[('Show Video Panel',920,150)]
        self.app.read_screen=AsyncMock(side_effect=menu)
        await self.app.clean_shared_view()
        self.assertTrue(self.app._share_panel_hidden)
        self.assertFalse(any('150' in call.args for call in self.app.command.await_args_list))
        self.app.command.assert_any_await('xdotool','key','--clearmodifiers','Escape')

    async def test_slide_text_does_not_open_view_menu(self):
        self.app.lines=[('View Options',900,500),('Hide Video Panel',920,800)]
        await self.app.clean_shared_view()
        self.app.command.assert_not_awaited()

    async def test_prepare_does_not_use_windows_shortcut_or_change_capture(self):
        self.app.stop_event.set()  # skip toolbar-settle wait
        self.app.ensure_fullscreen=AsyncMock()
        self.app.clean_shared_view=AsyncMock()
        self.app.lines=[('Unmute',50,1150),('Start Video',160,1150)]
        await self.app.prepare()
        self.app.ensure_fullscreen.assert_awaited_once()
        self.assertFalse(any('key' in call.args for call in self.app.command.await_args_list))

    async def test_missing_menu_is_throttled_and_does_not_exit_fullscreen(self):
        self.app.lines=[('View Options',900,40)]
        async def menu(**kwargs):
            self.app.lines=[('Fit to Window',920,150)]
        self.app.read_screen=AsyncMock(side_effect=menu)
        await self.app.clean_shared_view()
        self.assertFalse(self.app._share_panel_hidden)
        self.assertFalse(any('Escape' in call.args for call in self.app.command.await_args_list))
        self.app.lines=[('View Options',900,40)]
        await self.app.clean_shared_view()
        self.app.read_screen.assert_awaited_once()

    async def test_central_computer_audio_dialog_is_joined_without_opening_test_wizard(self):
        screens = [[('Join with Computer Audio',960,564),('Test speaker and microphone',960,597)],
                   [('Mute',50,1150),('Participants',850,1180)]]
        async def read(**kwargs):
            self.app.lines=screens.pop(0)
        self.app.read_screen=AsyncMock(side_effect=read)
        with patch('zoom_desktop.asyncio.sleep',new=AsyncMock()):
            self.assertTrue(await self.app.join_computer_audio())
        self.app.command.assert_any_await('xdotool','mousemove','960','564','click','1')
        self.assertFalse(any('597' in call.args for call in self.app.command.await_args_list))
        self.assertFalse(self.app.audio_dialog_open)

    async def test_recording_is_blocked_if_audio_dialog_does_not_disappear(self):
        self.app.read_screen=AsyncMock()
        self.app.lines=[('Join with Computer Audio',960,564),('Test speaker and microphone',960,597)]
        with patch('zoom_desktop.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError,'audiosiga'):
                await self.app.prepare()

    async def test_close_only_named_auxiliary_window_never_meeting(self):
        self.app.meeting_window_id='4096'
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096\n4097\n4098\n'
            if args[:2]==('xdotool','getwindowname'):
                return {'4097':b'Meeting Chat','4098':b'Zoom Workplace'}[args[2]]
            return b''
        self.app.command=AsyncMock(side_effect=command)
        with patch('zoom_desktop.shutil.which',return_value='/runtime/wmctrl'):
            await self.app.close_auxiliary_windows()
        self.app.command.assert_any_await('wmctrl','-i','-c','0x1001')
        self.assertEqual(sum(call.args[:1]==('wmctrl',) for call in self.app.command.await_args_list),1)

    async def test_modern_zoom_workplace_meeting_gets_the_largest_owned_window(self):
        self.app.meeting_window_id=None
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096\n4097'
            if args[:2]==('xdotool','getwindowname'):
                return b'Zoom Workplace'
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'WIDTH=1920\nHEIGHT=1200' if args[-1]=='4097' else b'WIDTH=1230\nHEIGHT=723'
            return b''
        self.app.command=AsyncMock(side_effect=command)
        async def read(**kwargs):
            self.app.lines=[('Participants Share Leave',900,1175),("Phone's screen",520,36)]
        self.app.read_screen=AsyncMock(side_effect=read)
        self.assertEqual(await self.app.snapshot(),'admitted')
        self.assertEqual(self.app.meeting_window_id,'4097')
        self.assertTrue(self.app.share_seen)

    async def test_audio_popup_is_read_alongside_margins_without_reading_shared_slide(self):
        self.app.meeting_window_id='4096'
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096\n4097'
            if args[:2]==('xdotool','getwindowname'):
                return b'Zoom Workplace' if args[-1]=='4096' else b'Choose one of the audio conference options'
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'X=674\nY=380\nWIDTH=572\nHEIGHT=422'
            return b''
        self.app.command=AsyncMock(side_effect=command)
        async def read(**kwargs):
            self.app.lines=[('Participants Share Leave',900,1175),('Join with Computer Audio',957,564)]
        self.app.read_screen=AsyncMock(side_effect=read)
        self.app.join_computer_audio=AsyncMock(return_value=True)
        self.assertEqual(await self.app.snapshot(),'admitted')
        self.app.read_screen.assert_awaited_once_with(margins_only=True,extra_regions=[(674,380,1246,802)])
        self.app.join_computer_audio.assert_awaited_once_with(refresh=False)


class AudioButtonGeometryTests(unittest.TestCase):
    def test_filled_join_button_is_separated_from_test_link_and_checkbox(self):
        from PIL import Image, ImageDraw
        image=Image.new('RGB',(572,422),'white')
        draw=ImageDraw.Draw(image)
        draw.rectangle((170,169,401,198),fill=(0,112,243))
        draw.rectangle((30,380,45,395),fill=(0,112,243))
        for x in range(200,370,15):
            draw.rectangle((x,213,x+5,220),fill=(0,112,243))
        self.assertEqual(blue_audio_buttons(image),[(170,169,402,199)])


class DesktopFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_cleanup_timeout_stops_worker_group_and_keeps_original(self):
        rec=ZoomRecorder()
        with tempfile.TemporaryDirectory() as folder:
            rec.output_path=Path(folder)/'original.mp4'
            rec.output_path.write_bytes(b'original-recording')
            process=SimpleNamespace(pid=12345,returncode=None,
                communicate=AsyncMock(side_effect=asyncio.TimeoutError()),wait=AsyncMock())
            with patch('recorder.asyncio.create_subprocess_exec',new=AsyncMock(return_value=process)), \
                    patch('recorder.os.killpg') as kill:
                await rec._clean_native_video()
            kill.assert_called_once_with(12345,signal.SIGKILL)
            process.wait.assert_awaited_once()
            self.assertEqual(rec.output_path.read_bytes(),b'original-recording')

    async def test_native_cleanup_launch_failure_keeps_recording_available(self):
        rec=ZoomRecorder()
        rec.output_path=Path('/tmp/native-test.mp4')
        with patch('recorder.asyncio.create_subprocess_exec',new=AsyncMock(side_effect=OSError('offline'))):
            await rec._clean_native_video()

    async def test_forced_desktop_bypasses_browser(self):
        rec=ZoomRecorder()
        rec._record_native=AsyncMock()
        with patch.dict(os.environ, ZOOM_JOIN_MODE='desktop'), patch('recorder.async_playwright') as browser:
            await rec.record(URL)
        rec._record_native.assert_awaited_once_with(URL)
        browser.assert_not_called()
        self.assertFalse(rec.is_recording)

    async def test_browser_closes_before_auto_fallback(self):
        rec=ZoomRecorder()
        closed=[]
        browser_context=AsyncMock()
        browser_context.__aenter__.return_value=SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(side_effect=DesktopRequired('desktop'))))
        async def close(*args):
            closed.append(True)
        browser_context.__aexit__.side_effect=close
        async def native(url):
            self.assertEqual(closed,[True])
        rec._record_native=AsyncMock(side_effect=native)
        with patch.dict(os.environ, ZOOM_JOIN_MODE='auto'), patch('recorder.async_playwright',return_value=browser_context):
            await rec.record(URL)
        rec._record_native.assert_awaited_once_with(URL)

    async def test_native_recovery_keeps_segments_and_stops_on_removal(self):
        rec=ZoomRecorder(use_profile_name=False,on_started=AsyncMock(),on_error=AsyncMock())
        native=Mock(open=AsyncMock(),close=AsyncMock(),prepare=AsyncMock(),clean_shared_view=AsyncMock(),
            snapshot=AsyncMock(side_effect=['admitted','disconnected','admitted','removed']))
        rec._start_ffmpeg=Mock(return_value=Mock(poll=Mock(return_value=None)))
        rec._stop_ffmpeg=Mock()
        rec._wait_or_stop=AsyncMock(return_value=False)
        with tempfile.TemporaryDirectory() as folder, patch.object(recorder_module,'RECORDINGS_DIR',Path(folder)), \
                patch('zoom_desktop.NativeZoom',return_value=native):
            await rec._record_native(URL)
        self.assertEqual(len(rec._segments),2)
        rec.on_started.assert_awaited_once()
        self.assertEqual(rec._start_ffmpeg.call_count,2)
        self.assertFalse(rec._paused)
        native.close.assert_awaited_once()
        self.assertFalse(rec._auto_ended)

    async def test_interactive_link_is_only_sent_to_owner(self):
        for user,expected in ((7,0),(999,1)):
            rec=ZoomRecorder(user_id=user,use_profile_name=False,on_waiting=AsyncMock())
            clock=SimpleNamespace(now=0)
            async def delay(seconds):
                clock.now+=20
                return False
            rec._wait_or_stop=AsyncMock(side_effect=delay)
            native=Mock(open=AsyncMock(),close=AsyncMock(),control_link=AsyncMock(return_value='https://example.test/private'),
                snapshot=AsyncMock(side_effect=['auth_required','auth_required','ended']))
            with patch.dict(os.environ,OWNER_TELEGRAM_ID='999'), patch('zoom_desktop.NativeZoom',return_value=native), \
                    patch.object(recorder_module,'time',SimpleNamespace(monotonic=lambda:clock.now)):
                await rec._record_native(URL)
            self.assertEqual(native.control_link.await_count,expected)

    async def test_guest_desktop_records_when_browser_auth_store_is_unavailable(self):
        rec=ZoomRecorder(use_profile_name=True, on_started=AsyncMock())
        native=Mock(open=AsyncMock(),close=AsyncMock(),prepare=AsyncMock(),clean_shared_view=AsyncMock(),
            snapshot=AsyncMock(side_effect=['admitted','ended']))
        rec._start_ffmpeg=Mock(return_value=Mock(poll=Mock(return_value=None)))
        rec._stop_ffmpeg=Mock()
        rec._wait_or_stop=AsyncMock(return_value=False)
        broken_store=SimpleNamespace(configured=True,load=AsyncMock(side_effect=RuntimeError('private-uri')))
        with tempfile.TemporaryDirectory() as folder, patch.object(recorder_module,'RECORDINGS_DIR',Path(folder)), \
                patch('zoom_desktop.NativeZoom',return_value=native), patch('recorder.auth_store',broken_store):
            await rec._record_native(URL)
        rec.on_started.assert_awaited_once()
        native.open.assert_awaited_once_with(URL,rec.guest_name)
        native.control_link.assert_not_called()

    async def test_host_and_waiting_room_never_prompt_for_login(self):
        rec=ZoomRecorder(user_id=999,use_profile_name=False,on_waiting=AsyncMock())
        clock=SimpleNamespace(now=0)
        async def delay(seconds):
            clock.now+=20
            return False
        rec._wait_or_stop=AsyncMock(side_effect=delay)
        native=Mock(open=AsyncMock(),close=AsyncMock(),control_link=AsyncMock(),
            snapshot=AsyncMock(side_effect=['waiting_host','waiting_host','waiting_room','waiting_room','ended']))
        with patch.dict(os.environ,OWNER_TELEGRAM_ID='999'), patch('zoom_desktop.NativeZoom',return_value=native), \
                patch.object(recorder_module,'time',SimpleNamespace(monotonic=lambda:clock.now)):
            await rec._record_native(URL)
        native.control_link.assert_not_awaited()

    async def test_native_cancel_does_not_open_an_app(self):
        rec=ZoomRecorder()
        await rec.stop()
        with patch('zoom_desktop.NativeZoom') as native:
            await rec._record_native(URL)
        native.assert_not_called()

    async def test_native_end_before_admission_creates_no_recording(self):
        rec=ZoomRecorder(use_profile_name=False)
        native=Mock(open=AsyncMock(),close=AsyncMock(),snapshot=AsyncMock(return_value='ended'))
        with patch('zoom_desktop.NativeZoom',return_value=native):
            await rec._record_native(URL)
        self.assertIsNone(rec.output_path)
        native.close.assert_awaited_once()
        self.assertTrue(rec._auto_ended)

    async def test_owner_only_control_link_and_no_cookie_save(self):
        native=Mock()
        link=NativeDesktopLink(native)
        response=await link.save(None)
        self.assertEqual(response.status,409)
        page=await link.index(None)
        self.assertIn('id="save" hidden',page.text)
        self.assertIn("$('#save').onclick",page.text)

if __name__=='__main__':
    unittest.main()
