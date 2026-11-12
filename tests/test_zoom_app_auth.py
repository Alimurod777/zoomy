import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
from zoom_app_auth import ZoomAppAuth


class AppAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,OWNER_TELEGRAM_ID='999')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.auth=ZoomAppAuth()
        self.update=SimpleNamespace(effective_user=SimpleNamespace(id=999),
            effective_chat=SimpleNamespace(id=999,type='private'),
            message=SimpleNamespace(reply_text=AsyncMock()))
        self.context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    async def asyncTearDown(self):
        await self.auth.close()

    def active(self,status='signed-in'):
        native=SimpleNamespace(login_status=AsyncMock(return_value=status),close=AsyncMock(),
            link=SimpleNamespace(url='https://example.test/private-link'),last_backup='saved')
        self.auth.native=native
        self.auth.pool=SimpleNamespace(release=AsyncMock())
        self.auth.number=92
        self.auth.user_id=999
        return native,self.auth.pool

    async def test_nonowner_cannot_open_account_login(self):
        self.update.effective_user.id=7
        await self.auth.start(self.update,self.context)
        self.assertIsNone(self.auth.task)
        self.assertIsNone(self.auth.user_id)

    async def test_group_chat_cannot_open_shared_profile(self):
        self.update.effective_chat.type='group'
        await self.auth.start(self.update,self.context)
        self.assertIsNone(self.auth.task)

    async def test_unknown_login_cannot_be_saved_as_verified(self):
        native,pool=self.active('unknown')
        await self.auth.done(self.update,self.context)
        native.close.assert_not_awaited()
        pool.release.assert_not_awaited()
        self.assertTrue(self.auth.active(999))

    async def test_verified_login_saves_and_releases_display(self):
        native,pool=self.active()
        await self.auth.done(self.update,self.context)
        native.close.assert_awaited_once_with(save_profile=True)
        pool.release.assert_awaited_once_with(92)
        self.assertFalse(self.auth.active(999))
        self.assertIn('MongoDB',self.update.message.reply_text.call_args.args[0])

    async def test_cancel_does_not_replace_shared_snapshot(self):
        native,pool=self.active()
        await self.auth.cancel(self.update,self.context)
        native.close.assert_awaited_once_with(save_profile=False)
        pool.release.assert_awaited_once()

    async def test_verification_failure_is_retryable_and_cancelable(self):
        native,pool=self.active()
        native.login_status.side_effect=asyncio.TimeoutError()
        await self.auth.done(self.update,self.context)
        self.assertTrue(self.auth.active(999))
        await self.auth.cancel(self.update,self.context)
        self.assertFalse(self.auth.active(999))

    async def test_expiry_closes_without_backup(self):
        native,pool=self.active()
        with patch('zoom_app_auth.asyncio.sleep',new=AsyncMock()):
            await self.auth._expire(self.context.bot,999)
        native.close.assert_awaited_once_with(save_profile=False)
        pool.release.assert_awaited_once()

    async def test_cancel_interrupts_initial_setup(self):
        entered=asyncio.Event()
        async def launch(bot,user):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.auth,'_launch',new=launch):
            await self.auth.start(self.update,self.context)
            await entered.wait()
            await asyncio.wait_for(self.auth.cancel(self.update,self.context),.5)
        self.assertFalse(self.auth.active(999))

    async def test_partial_startup_failure_releases_resources(self):
        pool=SimpleNamespace(acquire=AsyncMock(return_value=(92,':92','virtual_92.monitor')),release=AsyncMock())
        native=SimpleNamespace(open=AsyncMock(side_effect=RuntimeError('private-data')),close=AsyncMock())
        self.auth.user_id=999
        with patch('recorder.DisplayPool',return_value=pool),patch('zoom_app_auth.NativeZoom',return_value=native):
            await self.auth._launch(self.context.bot,999)
        native.close.assert_awaited_once_with(save_profile=False)
        pool.release.assert_awaited_once_with(92)
        self.assertNotIn('private-data',str(self.context.bot.send_message.call_args))

    async def test_try_again_reports_connectivity_without_saving_auth(self):
        native,pool=self.active('retry-required')
        native.network_status=AsyncMock(return_value='Zoom HTTPS: HTTP 200')
        await self.auth.done(self.update,self.context)
        self.assertIn('HTTP 200',self.update.message.reply_text.call_args.args[0])
        native.close.assert_not_awaited()

    async def test_retry_reopens_without_overwriting_snapshot(self):
        native,pool=self.active()
        with patch.object(self.auth,'start',new=AsyncMock()) as start:
            await self.auth.retry(self.update,self.context)
        native.close.assert_awaited_once_with(save_profile=False)
        start.assert_awaited_once_with(self.update,self.context)


if __name__=='__main__':unittest.main()
