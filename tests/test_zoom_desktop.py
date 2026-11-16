import asyncio
import json
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
        self.assertEqual(native_state([('Join a meeting Sign in',0,0)],False,True),'ended')

    def test_welcome_screen_overrides_reused_meeting_window(self):
        for join in ('Join a meeting', 'Jain a meeting'):
            lines=[('Zoom Workplace',900,400),('Sign in',900,500),
                   (join,900,600),('Sign up',900,700)]
            self.assertEqual(native_state(lines,True,True),'ended')
            self.assertEqual(native_state(lines,True,False),'connecting')
        self.assertEqual(native_state([('Sign in',900,500)],True,True),'admitted')

    def test_meeting_passcode_prompt_is_distinct_from_invalid_passcode(self):
        self.assertEqual(native_state([('Enter meeting passcode',890,470)]),'passcode_required')
        self.assertEqual(native_state([('Enter meeting passcode Incorrect meeting passcode',890,470)]),'passcode')

    def test_audio_join_checkbox_and_signin_are_never_meeting_end(self):
        lines=[('Sign in',1300,230),('Join with Computer Audio',960,565),
               ('Automatically join audio by computer when joining a meeting',950,770),
               ('Participants',850,980),('Share',1100,980),('Leave',1500,980)]
        self.assertEqual(native_state(lines,False,True),'admitted')
        self.assertEqual(native_state(lines[:3],False,True),'connecting')

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

    def test_waiting_card_with_dropped_ocr_prefix_keeps_waiting(self):
        lines=[('Host has joined.',730,360),("t them know you're here.",950,360),
               ('Test Speaker and Mic',800,400)]
        self.assertEqual(native_state(lines),'waiting_room')
        self.assertEqual(native_state(lines,True,True),'waiting_room')
        self.assertEqual(native_state([('Host has joined.',730,360)]),'connecting')

    def test_ocr_ignores_uncertain_words_and_preserves_click_position(self):
        tsv = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        tsv += '5\t1\t1\t1\t1\t1\t10\t20\t40\t10\t95\tJoin\n'
        tsv += '5\t1\t1\t1\t1\t2\t60\t20\t60\t10\t96\tAudio\n'
        tsv += '5\t1\t2\t1\t1\t1\t0\t0\t9\t9\t10\tNoise\n'
        self.assertEqual(ocr_lines(tsv), [('Join Audio',65,25)])

    def test_ocr_quote_does_not_swallow_subsequent_controls(self):
        tsv='block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        tsv+='1\t1\t1\t10\t10\t20\t10\t95\t"noise\n'
        tsv+='2\t1\t1\t10\t100\t40\t10\t95\tJoin\n'
        self.assertEqual(ocr_lines(tsv),[('"noise',20,15),('Join',30,105)])

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

    async def test_passcode_outline_is_detected_and_secret_uses_stdin(self):
        import io
        from PIL import Image, ImageDraw
        image=Image.new('RGB',(1920,1200),'white')
        ImageDraw.Draw(image).rectangle((740,511,1181,550),outline=(0,112,243),width=1)
        buffer=io.BytesIO()
        image.save(buffer,format='PNG')
        self.app._last_screen_png=buffer.getvalue()
        self.app.lines=[('Enter meeting passcode',890,471)]
        self.assertTrue(await self.app.enter_passcode('private-passcode'))
        self.app.command.assert_any_await('xdotool','mousemove','961','531','click','1',check=True)
        self.app.command.assert_any_await('xdotool','type','--clearmodifiers','--file','-',input=b'private-passcode',check=True)
        self.assertFalse(any('private-passcode' in call.args for call in self.app.command.await_args_list))

    async def test_passcode_never_types_into_an_unconfirmed_window(self):
        self.app.lines=[('Sign in',890,471)]
        self.assertFalse(await self.app.enter_passcode('private-passcode'))
        self.app.command.assert_not_awaited()

    async def test_audio_routing_checks_only_this_displays_zoom_streams(self):
        sinks=[{'index':7,'name':'virtual_85'}]
        own={'sink':7,'properties':{'application.process.binary':'zoom','window.x11.display':':85'}}
        other={'sink':0,'properties':{'application.process.binary':'zoom','window.x11.display':':99'}}
        self.app.command=AsyncMock(side_effect=[json.dumps(sinks).encode(),json.dumps([own,other]).encode()])
        self.assertTrue(await self.app.audio_output_ready())
        self.app.command=AsyncMock(side_effect=[json.dumps(sinks).encode(),json.dumps([other]).encode()])
        self.assertFalse(await self.app.audio_output_ready())

    async def test_zoom_speaker_menu_selects_session_sink(self):
        self.app.meeting_window_id='4096'
        self.app.audio_output_ready=AsyncMock(side_effect=[False,True])
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'X=0\nY=0\nWIDTH=1920\nHEIGHT=1200'
            return b''
        screens=[[('Audio',50,1184)],[('Select a speaker',80,709),('virtual_85',100,778)]]
        async def read(**kwargs):
            self.app.lines=screens.pop(0)
        self.app.command=AsyncMock(side_effect=command)
        self.app.read_screen=AsyncMock(side_effect=read)
        with patch('zoom_desktop.asyncio.sleep',new=AsyncMock()):
            self.assertTrue(await self.app.ensure_audio_output())
        self.app.command.assert_any_await('xdotool','mousemove','78','1158','click','1')
        self.app.command.assert_any_await('xdotool','mousemove','100','778','click','1')
        self.assertFalse(any('set-default-sink' in call.args for call in self.app.command.await_args_list))

    async def test_prepare_retries_audio_after_admission_recreates_windows(self):
        self.app.join_computer_audio=AsyncMock(return_value=True)
        self.app.ensure_fullscreen=AsyncMock()
        self.app.close_auxiliary_windows=AsyncMock()
        self.app.clean_shared_view=AsyncMock()
        self.app.ensure_audio_output=AsyncMock(side_effect=[False,True])
        self.app.snapshot=AsyncMock(return_value='admitted')
        self.app.stop_event.wait=AsyncMock(side_effect=asyncio.TimeoutError())
        with patch('zoom_desktop.asyncio.sleep',new=AsyncMock()):
            await self.app.prepare()
        self.assertEqual(self.app.ensure_audio_output.await_count,2)
        self.app.snapshot.assert_awaited_once()
        self.assertEqual(self.app.ensure_fullscreen.await_count,2)

    async def test_unverified_speaker_blocks_starting_silent_recording(self):
        self.app.join_computer_audio=AsyncMock(return_value=True)
        self.app.ensure_fullscreen=AsyncMock()
        self.app.close_auxiliary_windows=AsyncMock()
        self.app.clean_shared_view=AsyncMock()
        self.app.ensure_audio_output=AsyncMock(return_value=False)
        with self.assertRaisesRegex(RuntimeError,'ovozsiz yozuv'):
            await self.app.prepare()

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
        self.app.command.assert_any_await('wmctrl','-i','-r','0x1000','-b','add,fullscreen')
        self.app.command.assert_any_await('xdotool','windowactivate','4096')
        self.app.command.assert_any_await('xdotool','windowraise','4096')
        self.assertEqual(self.app.command.await_count,3)

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
        self.app.stop_event.wait=AsyncMock(side_effect=asyncio.TimeoutError())
        self.app.ensure_audio_output=AsyncMock(return_value=True)  # skip toolbar-settle wait
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

    async def test_visible_audio_window_blocks_recording_even_when_ocr_misses_text(self):
        self.app.lines=[]
        self.app.read_screen=AsyncMock()
        self.app.audio_dialog_visible=AsyncMock(return_value=True)
        self.assertFalse(await self.app.join_computer_audio())
        with self.assertRaisesRegex(RuntimeError,'audiosiga'):
            await self.app.prepare()

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

    async def test_recreated_window_is_found_by_title_when_class_search_is_empty(self):
        self.app.meeting_window_id='4096'
        self.app._fullscreen_window_id='4096'
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4097' if '--name' in args else b''
            if args[:2]==('xdotool','getwindowname'):
                return b'Meeting'
            return b''
        async def read(**kwargs):
            self.app.lines=[('Participants Share Leave',900,1175)]
        self.app.command=AsyncMock(side_effect=command)
        self.app.read_screen=AsyncMock(side_effect=read)
        self.assertEqual(await self.app.snapshot(),'admitted')
        self.assertEqual(self.app.meeting_window_id,'4097')
        self.assertIsNone(self.app._fullscreen_window_id)

    async def test_controls_without_a_visible_window_do_not_start_recording(self):
        self.app.admitted=False
        self.app.meeting_window_id='4096'
        self.app.command=AsyncMock(return_value=b'')
        async def read(**kwargs):
            self.app.lines=[('Participants Share Leave',900,1175)]
        self.app.read_screen=AsyncMock(side_effect=read)
        self.assertEqual(await self.app.snapshot(),'connecting')
        self.assertIsNone(self.app.meeting_window_id)
        self.assertFalse(self.app.admitted)

    async def test_generic_meeting_title_is_found_behind_home(self):
        self.app.admitted=False
        self.app.meeting_window_id=None
        active=['4096']
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096\n4097'
            if args[:2]==('xdotool','getwindowname'):
                return b'Meeting' if args[-1]=='4097' else b'Zoom Workplace'
            if args[:2]==('xdotool','windowactivate'):
                active[0]=args[-1]
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'X=366\nY=200\nWIDTH=1188\nHEIGHT=800'
            return b''
        async def read(**kwargs):
            self.app.lines=([('Participants Share Leave',900,980)] if active[0]=='4097' else
                            [('Sign in',900,500),('Join a meeting',850,600),('Sign up',1000,600)])
        self.app.command=AsyncMock(side_effect=command)
        self.app.read_screen=AsyncMock(side_effect=read)
        with patch('zoom_desktop.asyncio.sleep',new=AsyncMock()):
            self.assertEqual(await self.app.snapshot(),'admitted')
        self.assertEqual(self.app.meeting_window_id,'4097')
        self.assertEqual(active[0],'4097')

    async def test_modern_zoom_workplace_meeting_uses_visible_controls_instead_of_size(self):
        self.app.meeting_window_id=None
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096\n4097'
            if args[:2]==('xdotool','getwindowname'):
                return b'Zoom Workplace'
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'X=0\nY=0\nWIDTH=1920\nHEIGHT=1200' if args[-1]=='4097' else b'X=0\nY=0\nWIDTH=1230\nHEIGHT=723'
            return b''
        self.app.command=AsyncMock(side_effect=command)
        async def read(**kwargs):
            self.app.lines=[('Participants Share Leave',900,1175),("Phone's screen",520,36)]
        self.app.read_screen=AsyncMock(side_effect=read)
        self.assertEqual(await self.app.snapshot(),'admitted')
        self.assertEqual(self.app.meeting_window_id,'4097')
        self.assertTrue(self.app.share_seen)

    async def test_workplace_titled_preview_gets_its_join_button_read(self):
        self.app.admitted=False
        self.app.meeting_window_id=None
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096'
            if args[:2]==('xdotool','getwindowname'):
                return b'Zoom Workplace'
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'X=350\nY=200\nWIDTH=1200\nHEIGHT=850'
            return b''
        async def read(**kwargs):
            self.app.lines=[('Always show this preview when joining',700,1000)]
            self.app.preview_join=(1230,1010) if (350,200,1550,1050) in kwargs.get('extra_regions',[]) else None
        self.app.command=AsyncMock(side_effect=command)
        self.app.read_screen=AsyncMock(side_effect=read)
        self.assertEqual(await self.app.snapshot(),'connecting')
        self.app.command.assert_any_await('xdotool','mousemove','1230','1010','click','1')

    async def test_preview_join_is_clicked_before_other_windows_are_raised(self):
        self.app.admitted=False
        self.app.meeting_window_id=None
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096\n4097'
            if args[:2]==('xdotool','getwindowname'):
                return b'Zoom Workplace'
            return b''
        async def read(**kwargs):
            self.app.lines=[('Video Preview',900,400)]
            self.app.preview_join=(1100,800)
        self.app.command=AsyncMock(side_effect=command)
        self.app.read_screen=AsyncMock(side_effect=read)
        self.assertEqual(await self.app.snapshot(),'connecting')
        self.app.command.assert_any_await('xdotool','mousemove','1100','800','click','1')
        self.assertFalse(any(call.args[:2] in {('xdotool','windowactivate'),('xdotool','windowraise')}
                             for call in self.app.command.await_args_list))

    async def test_home_covering_smaller_meeting_is_replaced_by_actual_meeting(self):
        home=[('Sign in',900,500),('Join a meeting',850,600),('Sign up',1000,600)]
        meeting=[('Participants Share Leave',900,980)]
        self.app.meeting_window_id='4096'  # old size-based choice picked Home
        active=['4096']
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096\n4097'
            if args[:2]==('xdotool','getwindowname'):
                return b'Zoom Workplace'
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'X=345\nY=238\nWIDTH=1230\nHEIGHT=723' if args[-1]=='4096' else b'X=366\nY=200\nWIDTH=1188\nHEIGHT=800'
            if args[:2]==('xdotool','windowactivate'):
                active[0]=args[-1]
            return b''
        async def read(**kwargs):
            self.app.lines=list(home if active[0]=='4096' else meeting)
        self.app.command=AsyncMock(side_effect=command)
        self.app.read_screen=AsyncMock(side_effect=read)
        with patch('zoom_desktop.asyncio.sleep',new=AsyncMock()):
            self.assertEqual(await self.app.snapshot(),'admitted')
        self.assertEqual(self.app.meeting_window_id,'4097')
        self.assertEqual(active[0],'4097')
        self.assertFalse(any(call.args[:2]==('wmctrl','-c') for call in self.app.command.await_args_list))

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

    async def test_home_probe_reads_center_when_meeting_window_is_reused(self):
        import io
        from PIL import Image
        buffer=io.BytesIO()
        Image.new('RGB',(1920,1200),'white').save(buffer,format='PNG')
        tsv='level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        for i,label in enumerate(('Sign in','Jain a meeting','Sign up')):
            tsv+=f'5\t1\t{i+1}\t1\t1\t1\t800\t{400+i*100}\t200\t30\t95\t{label}\n'
        self.app.command=AsyncMock(side_effect=[buffer.getvalue(),b'',tsv.encode()])
        await self.app.read_screen(margins_only=True)
        self.assertEqual(native_state(self.app.lines,True,True),'ended')

    async def test_share_switch_rechecks_hide_panel_even_in_the_same_window(self):
        self.app.meeting_window_id='4096'
        self.app._share_signature="phone's screen"
        self.app._share_panel_hidden=True
        self.app._last_view_attempt=123
        async def command(*args,**kwargs):
            return b'4096' if args[:2]==('xdotool','search') else b'Zoom Meeting'
        self.app.command=AsyncMock(side_effect=command)
        async def read(**kwargs):
            self.app.lines=[("Computer's screen",550,36),('Participants Share Leave',900,1175)]
        self.app.read_screen=AsyncMock(side_effect=read)
        self.assertEqual(await self.app.snapshot(),'admitted')
        self.assertFalse(self.app._share_panel_hidden)
        self.assertEqual(self.app._last_view_attempt,float('-inf'))

    async def test_normal_app_exit_after_admission_is_terminal_but_crash_can_recover(self):
        self.app.process=SimpleNamespace(returncode=0)
        self.assertEqual(await self.app.snapshot(),'ended')
        self.app.process.returncode=-9
        self.assertEqual(await self.app.snapshot(),'disconnected')

    async def test_owned_end_popup_with_generic_title_gets_a_terminal_reference(self):
        self.app.meeting_window_id='4096'
        self.app._last_screen_png=b'private-terminal-image'
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096\n4097'
            if args[:2]==('xdotool','getwindowname'):
                return b'Zoom Workplace'
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'X=674\nY=380\nWIDTH=572\nHEIGHT=422'
            return b''
        self.app.command=AsyncMock(side_effect=command)
        async def read(**kwargs):
            self.app.lines=[('This meeting has been ended by the host',957,564)]
        self.app.read_screen=AsyncMock(side_effect=read)
        with patch('zoom_desktop.transient_parent',return_value='4096'):
            self.assertEqual(await self.app.snapshot(),'ended')
        self.assertEqual(self.app.terminal_reference,(b'private-terminal-image',(674,380,1246,802)))

    async def test_notification_is_not_used_as_an_end_dialog_reference(self):
        self.app.meeting_window_id='4096'
        self.app._last_screen_png=b'private-terminal-image'
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):
                return b'4096\n4097\n4098'
            if args[:2]==('xdotool','getwindowname'):
                return b'Zoom Workplace'
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'X=674\nY=380\nWIDTH=572\nHEIGHT=422' if args[-1]=='4097' else b'X=1400\nY=30\nWIDTH=450\nHEIGHT=100'
            return b''
        self.app.command=AsyncMock(side_effect=command)
        async def read(**kwargs):
            self.app.lines=[('This meeting has been ended by the host',957,564),('Take notes',1600,70)]
        self.app.read_screen=AsyncMock(side_effect=read)
        with patch('zoom_desktop.transient_parent',return_value='4096'):
            self.assertEqual(await self.app.snapshot(),'ended')
        self.assertEqual(self.app.terminal_reference[1],(674,380,1246,802))


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
    async def test_native_cleanup_selects_camera_or_share_view(self):
        for shared in (False,True):
            rec=ZoomRecorder()
            rec.output_path=Path('/tmp/test-camera-choice.mp4')
            rec._native_shared_seen=shared
            process=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(b'cleaned',b'')))
            launch=AsyncMock(return_value=process)
            with patch.dict(os.environ,ZOOM_NATIVE_CLEAN_VIDEO='true'), \
                 patch('recorder.asyncio.create_subprocess_exec',new=launch):
                await rec._clean_native_video()
            args=launch.call_args.args
            self.assertEqual('--camera-view' in args,not shared)
            self.assertNotIn('--keep-full-view',args)

    async def test_terminal_reference_is_private_and_deleted_after_worker(self):
        rec=ZoomRecorder()
        with tempfile.TemporaryDirectory() as folder:
            rec.output_path=Path(folder)/'original.mp4'
            rec._native_terminal_reference=(b'private-end-screen',(10,20,200,100))
            process=SimpleNamespace(pid=12345,returncode=0,
                communicate=AsyncMock(return_value=(b'end-trimmed',b'')))
            references=[]
            async def launch(*args,**kwargs):
                index=args.index('--terminal-reference')
                reference=Path(args[index+1])
                references.append(reference)
                self.assertEqual(reference.stat().st_mode&0o777,0o600)
                self.assertEqual(reference.read_bytes(),b'private-end-screen')
                return process
            with patch('recorder.asyncio.create_subprocess_exec',new=AsyncMock(side_effect=launch)):
                await rec._clean_native_video()
            self.assertTrue(references)
            self.assertFalse(references[0].exists())

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
                if user==7:
                    with self.assertRaisesRegex(recorder_module.MeetingJoinError,'login kerak'):
                        await rec._record_native(URL)
                else:
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

    async def test_guest_join_uses_saved_account_name(self):
        rec=ZoomRecorder(use_profile_name=True)
        rec._native_guest=True
        native=Mock(open=AsyncMock(),close=AsyncMock(),snapshot=AsyncMock(return_value='ended'))
        saved=SimpleNamespace(configured=True,load=AsyncMock(return_value={
            'profile':{'display_name':'Real Account Name'}}))
        with patch('zoom_desktop.NativeZoom',return_value=native), patch('recorder.auth_store',saved):
            await rec._record_native(URL)
        native.open.assert_awaited_once_with(URL,'Real Account Name')

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

    async def test_waiting_for_host_admission_does_not_hit_60_second_setup_timeout(self):
        rec=ZoomRecorder(use_profile_name=False,on_waiting=AsyncMock())
        clock=SimpleNamespace(now=0)
        lines=[('Host has joined.',730,360),("t them know you're here.",950,360)]
        native=Mock(open=AsyncMock(),close=AsyncMock(),control_link=AsyncMock(),
                    snapshot=AsyncMock(side_effect=[native_state(lines)]*5+['ended']))
        async def delay(seconds):
            clock.now+=20
            return False
        rec._wait_or_stop=AsyncMock(side_effect=delay)
        with patch('zoom_desktop.NativeZoom',return_value=native), \
             patch.object(recorder_module,'time',SimpleNamespace(monotonic=lambda:clock.now)):
            await rec._record_native(URL)
        self.assertGreater(clock.now,60)
        self.assertIsNone(rec.output_path)
        native.control_link.assert_not_awaited()
        native.close.assert_awaited_once()

    async def test_native_uses_separately_supplied_meeting_passcode(self):
        rec=ZoomRecorder(use_profile_name=False,meeting_password='private-passcode')
        native=Mock(open=AsyncMock(),close=AsyncMock(),
                    enter_passcode=AsyncMock(return_value=True),
                    snapshot=AsyncMock(side_effect=['passcode_required','waiting_room','ended']))
        rec._wait_or_stop=AsyncMock(return_value=False)
        with patch('zoom_desktop.NativeZoom',return_value=native):
            await rec._record_native(URL)
        native.enter_passcode.assert_awaited_once_with('private-passcode')
        self.assertIsNone(rec.output_path)

    async def test_missing_passcode_reports_the_real_requirement(self):
        rec=ZoomRecorder(use_profile_name=False)
        native=Mock(open=AsyncMock(),close=AsyncMock(),snapshot=AsyncMock(return_value='passcode_required'))
        with patch('zoom_desktop.NativeZoom',return_value=native):
            with self.assertRaisesRegex(recorder_module.MeetingJoinError,'uchrashuv parolini'):
                await rec._record_native('https://example.zoom.us/j/12345678900')
        native.close.assert_awaited_once()

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
