"""Standalone, owner-only native login, with bounded startup and expiry."""
import asyncio
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from zoom_auth_bot import ZoomAuthBot
from zoom_desktop import NativeZoom

logger=logging.getLogger(__name__)


class ZoomAppAuth:
    def __init__(self):
        self.native=None
        self.pool=None
        self.number=None
        self.user_id=None
        self.task=None
        self.timer=None
        self.lock=asyncio.Lock()

    def active(self,user_id):
        return self.user_id==user_id

    async def _cleanup(self,save=False):
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer=None
        native,self.native=self.native,None
        number,self.number=self.number,None
        self.user_id=None
        try:
            if native:
                await native.close(save_profile=save)
        finally:
            if number is not None:
                await self.pool.release(number)
        return native

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        async with self.lock:
            await self._cleanup()

    async def _link(self,bot):
        await bot.send_message(self.user_id,
            'Zoom Desktop’da o‘zingiz login qiling. Parol yoki kodni Telegram’ga yubormang. '
            'Tayyor bo‘lgach /zoomappdone; tekshirish /zoomappstatus; bekor qilish /zoomappcancel. '
            'Home oynasida login tasdiqlanmasa profil menyusini oching (Sign out ko‘rinsin). '
            'Havola va jarayon 15 daqiqagacha amal qiladi.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Zoom ilovasini ochish',url=self.native.link.url)]]))

    async def start(self,update,context):
        if not ZoomAuthBot.allowed(update):
            await update.message.reply_text('Desktop login faqat OWNER_TELEGRAM_ID egasining shaxsiy chatida ishlaydi.')
            return
        if self.user_id:
            if self.native and self.native.link and self.native.link.url:
                await self._link(context.bot)
            else:
                await update.message.reply_text('Desktop ochilyapti. /zoomappcancel bilan bekor qilishingiz mumkin.')
            return
        self.user_id=update.effective_user.id
        try:
            await update.message.reply_text('Zoom Desktop login oynasi ochilyapti…')
        except Exception:
            self.user_id=None
            raise
        self.task=asyncio.create_task(self._launch(context.bot,self.user_id))

    async def _launch(self,bot,user_id):
        async def open_window():
            from recorder import DisplayPool
            self.pool=DisplayPool(base=int(os.getenv('ZOOM_LOGIN_DISPLAY') or '90'),max_slots=1)
            self.number,display,sink=await self.pool.acquire()
            self.native=NativeZoom(display,sink.removesuffix('.monitor'),asyncio.Event(),account=True)
            await self.native.open()
            # Sign in may be entered manually if the initial button is not yet visible.
            await self.native.read_screen()
            await self.native.click_label('Sign In')
            await self.native.control_link()
        try:
            await asyncio.wait_for(open_window(),60)
            await self._link(bot)
            self.timer=asyncio.create_task(self._expire(bot,user_id))
        except asyncio.CancelledError:
            await self._cleanup()
            raise
        except Exception as exc:
            logger.warning('Desktop login startup failed (%s)',type(exc).__name__)
            await self._cleanup()
            await bot.send_message(user_id,'Desktop login ochilmadi yoki 60 soniya tugadi. '
                'Desktop yozuvi band emasligini va setup-zoom-desktop.sh / setup-login-link.sh o‘rnatilganini tekshiring. '
                '/zoomapplogin bilan qayta urinishingiz mumkin.')

    async def _expire(self,bot,user_id):
        await asyncio.sleep(900)
        async with self.lock:
            await self._cleanup()
        await bot.send_message(user_id,'Desktop login uchun 15 daqiqa tugadi. /zoomapplogin bilan qayta boshlang.')

    async def done(self,update,context):
        if not ZoomAuthBot.allowed(update):
            return
        async with self.lock:
            if (not self.active(update.effective_user.id) or not self.native or not self.native.link
                    or not self.native.link.url or (self.task and not self.task.done())):
                await update.message.reply_text('Avval /zoomapplogin yuboring yoki oyna ochilishini kuting.')
                return
            try:
                status=await asyncio.wait_for(self.native.login_status(),25)
            except Exception as exc:
                logger.warning('Desktop login verification failed (%s)',type(exc).__name__)
                await update.message.reply_text('Login tekshiruvi bajarilmadi. Qayta urinib ko‘ring yoki /zoomappcancel yuboring.')
                return
            if status!='signed-in':
                if status=='retry-required':
                    network=await self.native.network_status()
                    await update.message.reply_text('Zoom login sahifasi qayta urinish so‘rayapti. '+network+
                        '. /zoomappretry ilovani qayta ochadi; /zoomappcancel yopadi. '
                        'HTTPS ochilishi akkaunt loginini tasdiqlamaydi.')
                    return
                await update.message.reply_text('Akkauntga kirish hali tasdiqlanmadi. Login/2FA/SSO’ni yakunlang; '
                    'profil menyusida Sign out ko‘rinsin. /zoomappdone qayta tekshiradi; /zoomappcancel bekor qiladi.')
                return
            native=await self._cleanup(save=True)
            message='Desktop Home oynasida akkauntga kirish tasdiqlandi. Login shu serverda saqlandi. '
            message+=('Profil nusxasi MongoDB’da shifrlab saqlandi. ' if native.last_backup=='saved' else
                      'MongoDB nusxasi saqlanmadi; mahalliy profil ishlatiladi. ')
            await update.message.reply_text(message+'/zoommode app-auth bilan yozishingiz mumkin. '
                'Zoom keyinchalik qayta tasdiqlashni talab qilishi mumkin.')

    async def status(self,update,context):
        if not ZoomAuthBot.allowed(update):
            return
        if (not self.active(update.effective_user.id) or not self.native
                or (self.task and not self.task.done())):
            await update.message.reply_text('Desktop login oynasi yopiq. /zoomapplogin orqali ochib tekshiring. '
                'Diskdagi profil mavjudligi amaldagi loginni tasdiqlamaydi.')
            return
        try:
            status=await asyncio.wait_for(self.native.login_status(),25)
            network=(' '+await self.native.network_status()+'.') if status=='retry-required' else ''
            await update.message.reply_text('Desktop login holati: '+status+'.'+network+
                ' /zoomappretry — qayta ochish; /zoomappdone — saqlash; /zoomappcancel — yopish.')
        except Exception:
            await update.message.reply_text('Holat tekshiruvi bajarilmadi. /zoomappcancel bilan yopishingiz mumkin.')

    async def cancel(self,update,context):
        if not ZoomAuthBot.allowed(update):
            return
        await self.close()
        await update.message.reply_text('Desktop login yopildi; umumiy MongoDB nusxasi almashtirilmadi.')

    async def retry(self,update,context):
        if not ZoomAuthBot.allowed(update):
            return
        await self.close()
        await self.start(update,context)


zoom_app_auth=ZoomAppAuth()
