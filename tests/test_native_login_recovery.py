import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from PIL import Image,ImageDraw

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
from native_processes import living_pids
from desktop_auth_store import DesktopAuthStore,AccountDesktopAuthStore
from zoom_desktop import NativeZoom,blue_audio_buttons


class ProfileTests(unittest.TestCase):
    def test_exited_processes_do_not_block_native_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for pid,state in ((11,'Z'),(12,'X'),(13,'S')):
                (root/str(pid)).mkdir()
                (root/str(pid)/'stat').write_text(f'{pid} (zoom) {state} 1 1')
            self.assertEqual(living_pids(['11','12','13','14'],root),['13'])

    def test_account_namespace_is_separate_from_legacy_and_other_devices(self):
        legacy=DesktopAuthStore()
        account=AccountDesktopAuthStore()
        other=AccountDesktopAuthStore()
        other.device='another-device'
        self.assertNotEqual(account.identity,legacy.identity)
        self.assertNotEqual(account.identity,other.identity)
        app=NativeZoom(':82','sink',asyncio.Event(),account=True)
        self.assertEqual(app.auth_store.home,app.account_home)
        self.assertNotEqual(app.account_home,Path.home())

    def test_different_account_identity_has_a_different_home(self):
        with patch.dict(os.environ,ZOOM_AUTH_ID='first'):
            first=NativeZoom(':82','sink',asyncio.Event(),account=True)
        with patch.dict(os.environ,ZOOM_AUTH_ID='second'):
            second=NativeZoom(':82','sink',asyncio.Event(),account=True)
        self.assertNotEqual(first.account_home,second.account_home)
        with self.assertRaises(ValueError):
            NativeZoom(':82','sink',asyncio.Event(),account=True,guest=True)

    def test_small_preview_join_button_is_not_sign_in(self):
        image=Image.new('RGB',(640,552),'white')
        draw=ImageDraw.Draw(image)
        draw.rectangle((568,507,620,538),fill=(13,108,225))
        draw.rounded_rectangle((484,507,559,538),radius=8,outline='gray',width=1)
        self.assertEqual(blue_audio_buttons(image,preview=True),[(568,507,621,539)])


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_screen_cannot_be_reported_as_verified_account(self):
        app=NativeZoom(':82','sink',asyncio.Event(),account=True)
        app.read_screen=AsyncMock()
        for labels,expected,verified in ((['Sign out'],'signed-in',True),
                (['Email or ph'],'sign-in',False),(['Try again','Sign out'],'retry-required',False)):
            app.lines=[(label,400,400) for label in labels]
            self.assertEqual(await app.login_status(),expected)
            self.assertEqual(app._account_verified,verified)

    async def test_unverified_account_profile_is_not_uploaded(self):
        app=NativeZoom(':82','sink',asyncio.Event(),account=True)
        app.process=SimpleNamespace(pid=12345,returncode=0)
        app.auth_store=SimpleNamespace(backup=AsyncMock(return_value='saved'),close=AsyncMock())
        with patch('zoom_desktop.os.killpg'):
            await app.close()
            app.auth_store.backup.assert_not_awaited()
            app._account_verified=True
            await app.close()
            app.auth_store.backup.assert_awaited_once()

    async def test_preview_selects_join_and_does_not_request_login(self):
        app=NativeZoom(':82','sink',asyncio.Event(),guest=True)
        async def command(*args,**kwargs):
            if args[:2]==('xdotool','search'):return b'101\n102'
            if args[:2]==('xdotool','getwindowname'):
                return b'Zoom Workplace' if args[-1]=='101' else b'Meeting preview'
            if args[:2]==('xdotool','getwindowgeometry'):
                return b'X=640\nY=494\nWIDTH=640\nHEIGHT=552'
            return b''
        app.command=AsyncMock(side_effect=command)
        async def read(**kwargs):
            app.preview_join=(1234,1017)
            app.lines=[('Always show this preview when joining',900,953),('Sign in',1160,1017)]
        app.read_screen=AsyncMock(side_effect=read)
        with patch('zoom_desktop.transient_parent',return_value='101'):
            self.assertEqual(await app.snapshot(),'connecting')
        app.command.assert_any_await('xdotool','mousemove','1234','1017','click','1')
        self.assertFalse(any('1160' in call.args for call in app.command.await_args_list))
        self.assertFalse(app.admitted)


if __name__=='__main__':unittest.main()
