"""One editable Telegram message per recording; no repeated screenshots."""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton,InlineKeyboardMarkup
from telegram.error import BadRequest,Forbidden,RetryAfter,TelegramError

logger=logging.getLogger(__name__)


def panel_text(number,stats,final=None):
    state=final or stats['phase']
    labels={'waiting':'⏳ Kirish kutilmoqda','recording':'🔴 Yozilmoqda','paused':'⏸ Pauza',
            'processing':'⚙️ Yakunlanmoqda','finished':'✅ Tugadi','error':'⚠️ Xato bilan tugadi'}
    seconds=max(0,int(stats['seconds']))
    hours,tail=divmod(seconds,3600)
    minutes,seconds=divmod(tail,60)
    source={'shared-screen':'Ulashilgan ekran','active-speaker':'Faol video',
            'waiting':'Ulashilgan ekran kutilmoqda','desktop':'Zoom ilovasi'}.get(stats['source'],'Aniqlanmoqda')
    return (f'<b>Yozuv — Session {number}</b>\n{labels[state]}\n'
            f'⏱ Davomiylik: <b>{hours:02}:{minutes:02}:{seconds:02}</b>\n'
            f'💾 Hajm: <b>{stats["bytes"]/1_000_000:.2f} MB</b>\n'
            f'🎞 Manba: {source}\n'
            f'📁 {html.escape(str(stats["name"])[:180])}\n'
            'Faol yozuvda har 5 soniyada yangilanadi.')


class RecordingPanel:
    def __init__(self,bot,chat_id,number,recorder):
        self.bot,self.chat_id,self.number,self.recorder=bot,chat_id,number,recorder
        self.message_id=None
        self.last_text=None
        self.task=None
        self.disabled=False
        self.error=None
        self.retry_delay=5
        self.lock=asyncio.Lock()

    async def update(self,final=None):
        if self.disabled:
            return
        stats=await asyncio.to_thread(self.recorder.progress_snapshot)
        text=panel_text(self.number,stats,final)
        if text==self.last_text:
            return
        state=final or stats['phase']
        keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('⏹ Yozuvni to‘xtatish',
            callback_data=f'stop_session_{self.number}')]]) if state in {'waiting','recording','paused'} else None
        async with self.lock:
            try:
                if self.message_id is None:
                    msg=await self.bot.send_message(self.chat_id,text,parse_mode='HTML',reply_markup=keyboard)
                    self.message_id=msg.message_id
                else:
                    await self.bot.edit_message_text(chat_id=self.chat_id,message_id=self.message_id,
                        text=text,parse_mode='HTML',reply_markup=keyboard)
                self.last_text=text
            except BadRequest as exc:
                detail=str(exc).lower()
                if 'not modified' in detail:
                    self.last_text=text
                elif 'not found' in detail or "can't be edited" in detail or 'cannot be edited' in detail:
                    self.disabled=True
                else:
                    logger.warning('Recording panel edit rejected')
            except Forbidden:
                self.disabled=True
            except RetryAfter:
                raise
            except TelegramError:
                logger.warning('Recording panel temporarily unavailable')

    async def _run(self):
        while not self.disabled:
            await asyncio.sleep(self.retry_delay)
            try:
                await self.update()
                self.retry_delay=5
            except RetryAfter as exc:
                value=exc.retry_after
                self.retry_delay=max(5,value.total_seconds() if hasattr(value,'total_seconds') else float(value))

    async def start(self):
        try:
            await self.update()
        except RetryAfter as exc:
            value=exc.retry_after
            self.retry_delay=max(5,value.total_seconds() if hasattr(value,'total_seconds') else float(value))
        if not self.disabled and (self.task is None or self.task.done()):
            self.task=asyncio.create_task(self._run())

    async def show(self):
        if self.disabled:
            self.disabled=False
            self.message_id=self.last_text=None
        await self.start()

    async def finish(self,error=None):
        self.error=error or self.error
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        self.task=None
        if self.message_id is not None:
            try:
                await self.update(final='error' if self.error else 'finished')
            except RetryAfter:
                pass
