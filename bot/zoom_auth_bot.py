"""Owner-only, private-chat UI for Zoom's browser authentication."""
import asyncio
import io
import logging
import os
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from auth_store import auth_store, AuthStoreError
from zoom_browser_auth import ZoomBrowserLogin, LoginInputError
from login_link import BrowserLoginLink

logger = logging.getLogger(__name__)


class ZoomAuthBot:
    def __init__(self):
        self.browser = None
        self.link = None
        self.user_id = None
        self.nonce = None
        self.timeout_task = None
        self.lock = asyncio.Lock()

    def active(self, user_id: int) -> bool:
        return self.browser is not None and self.user_id == user_id

    @staticmethod
    def allowed(update) -> bool:
        owner = int(os.getenv("OWNER_TELEGRAM_ID", "0"))
        return bool(owner > 0 and update.effective_user
            and update.effective_user.id == owner
            and update.effective_chat.type == "private"
            and update.effective_chat.id == owner)

    async def authorize(self, update) -> bool:
        if self.allowed(update):
            return True
        if update.callback_query:
            await update.callback_query.answer("Faqat bot egasining shaxsiy chatida.", show_alert=True)
        elif update.message:
            await update.message.reply_text("Zoom login faqat OWNER_TELEGRAM_ID egasining shaxsiy chatida ishlaydi.")
        return False

    async def _close(self):
        if self.timeout_task and self.timeout_task is not asyncio.current_task():
            self.timeout_task.cancel()
        self.timeout_task = None
        browser, self.browser = self.browser, None
        self.user_id = self.nonce = None
        if self.link:
            link, self.link = self.link, None
            await link.close()
        if browser:
            await browser.close()

    async def close(self):
        async with self.lock:
            await self._close()

    async def _timeout(self, bot, user_id):
        await asyncio.sleep(int(os.getenv("ZOOM_LOGIN_TIMEOUT", "900")))
        async with self.lock:
            await self._close()
            await bot.send_message(user_id, "⏱ Zoom login vaqti tugadi. /zoomlogin bilan qayta boshlang.")

    async def start(self, update, context):
        if not await self.authorize(update):
            return
        async with self.lock:
            if self.browser:
                try:
                    if self.link:
                        await self._link_view(context.bot)
                    else:
                        await self._view(context.bot)
                except Exception as exc:
                    await self._error(context.bot, update.effective_user.id, exc)
                return
            # Validate Mongo encryption/config before collecting credentials.
            try:
                if auth_store.configured:
                    await auth_store.load()
                self.user_id = update.effective_user.id
                self.browser = ZoomBrowserLogin()
                await update.message.reply_text("Zoom brauzer oynasi ochilyapti…")
                manual = os.getenv("ZOOM_LOGIN_MODE", "link") == "link"
                await self.browser.open(manual=manual)
                self.timeout_task = asyncio.create_task(self._timeout(context.bot, self.user_id))
                if manual:
                    self.link = BrowserLoginLink(self.browser,
                        ttl=int(os.getenv("ZOOM_LOGIN_TIMEOUT", "900")),
                        on_saved=lambda: self._link_saved(context.bot))
                    await self.link.open()
                    await self._link_view(context.bot)
                else:
                    await self._view(context.bot)
            except Exception as exc:
                await self._close()
                await self._error(context.bot, update.effective_user.id, exc)

    async def _error(self, bot, user_id, exc):
        # Playwright fill() exceptions can include the filled password in their
        # call log. Never log the exception, traceback, or incoming message.
        logger.warning("Zoom auth operation failed (%s)", type(exc).__name__)
        text = str(exc) if isinstance(exc, (AuthStoreError, LoginInputError)) else (
            "Zoom amalini bajarib bo‘lmadi. Oynani yangilang yoki /zoomcancel yuboring. "
            "CAPTCHA/SSO oynasida /zoomclick x y yoki masofaviy login usulidan foydalaning.")
        await bot.send_message(user_id, text)
        if self.browser and not self.link:
            try:
                await self._view(bot)
            except Exception:
                logger.warning("Could not refresh Zoom auth preview")
                await bot.send_message(user_id,
                    "/zoomcancel yuboring va /zoomlogin bilan qayta boshlang.")

    async def _link_view(self, bot):
        self.nonce = secrets.token_hex(6)
        prefix = f"zoomauth:{self.nonce}:"
        await bot.send_message(self.user_id,
            "Zoom akkauntingizga brauzerda o‘zingiz kiring. Parol va kodni Telegram’ga yuborish shart emas.\n"
            "Tayyor bo‘lgach ochilgan sahifadagi “Loginni saqlash” tugmasini bosing yoki /zoomdone yuboring.\n"
            "Havola 15 daqiqaga amal qiladi; faqat o‘zingiz foydalaning.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Zoom brauzerini ochish", url=self.link.url)],
                [InlineKeyboardButton("✅ Loginni saqlash", callback_data=prefix + "save"),
                 InlineKeyboardButton("❌ Bekor qilish", callback_data=prefix + "cancel")]]))

    async def _link_saved(self, bot):
        async with self.lock:
            if not self.link or self.link.saved is None:
                return
            user_id = self.user_id
            mongo = self.link.saved["mongo"]
            await self._close()
            await bot.send_message(user_id, "✅ Zoom sessiyasi " +
                ("MongoDB’da shifrlab saqlandi." if mongo else "mahalliy saqlandi.") +
                " Keyingi yozuv shu loginni ishlatadi.")

    async def _view(self, bot):
        if self.link:
            await self._link_view(bot)
            return
        phase, _ = await self.browser.inputs()
        prompt = {
            "email": "Zoom email/telefonini shu chatga yuboring.",
            "password": "Zoom parolini shu chatga yuboring.",
            "code": "Zoom email/SMS/Authenticator orqali bergan tasdiqlash kodini yuboring.",
            "manual": "Quyidagi Zoom tugmasini bosing. CAPTCHA bo‘lsa rasmga qarab /zoomclick x y ishlating.",
        }[phase]
        names = await self.browser.controls()
        self.nonce = secrets.token_hex(6)
        prefix = f"zoomauth:{self.nonce}:"
        rows = [[InlineKeyboardButton(name[:45], callback_data=prefix + f"b{i}")]
                for i, name in enumerate(names)]
        rows.append([InlineKeyboardButton("🔄 Yangilash", callback_data=prefix + "refresh"),
                     InlineKeyboardButton("✅ Loginni saqlash", callback_data=prefix + "save")])
        rows.append([InlineKeyboardButton("❌ Bekor qilish", callback_data=prefix + "cancel")])
        await bot.send_photo(self.user_id, photo=io.BytesIO(await self.browser.screenshot()),
            caption=f"{prompt}\n\nParol/kod xabari o‘chirishga uriniladi; bazaga faqat brauzer sessiyasi saqlanadi.\n"
                    "Rasm 1280×900. /zoomdone — tekshirish va saqlash; /zoomcancel — bekor qilish.",
            reply_markup=InlineKeyboardMarkup(rows))

    async def text(self, update, context):
        if not await self.authorize(update):
            return
        if self.link:
            await context.bot.send_message(update.effective_user.id,
                "Email/parol/kodni ochilgan brauzer sahifasida kiriting. Havolani yana olish uchun /zoomlogin.")
            return
        # Delete incoming credentials before browser actions, including failures.
        value = update.message.text
        try:
            await update.message.delete()
        except Exception:
            pass
        async with self.lock:
            if not self.active(update.effective_user.id):
                return
            self.nonce = None
            try:
                await self.browser.submit(value)
                # Login can redirect to a profile, meeting list, etc.
                if "signin" not in self.browser.page.url and "login" not in self.browser.page.url:
                    if await self._save(context.bot):
                        return
                if self.link:
                    await self._link_view(context.bot)
                else:
                    await self._view(context.bot)
            except Exception as exc:
                await self._error(context.bot, update.effective_user.id, exc)
            finally:
                value = None

    async def _save(self, bot) -> bool:
        async def persist():
            if self.link and self.link.saved is not None:
                return self.link.saved
            state = await self.browser.verified_state()
            if state is None:
                return None
            result = await auth_store.save(state)
            if self.link:
                self.link.saved = result
            return result
        if self.link:
            async with self.link.lock:
                saved = await persist()
        else:
            saved = await persist()
        if saved is None:
            await bot.send_message(self.user_id, "Zoom login hali tasdiqlanmadi. Oynadagi qadamlarni yakunlang.")
            return False
        destination = (f"MongoDB’da shifrlab saqlandi, revision {saved['revision']}."
                       if saved["mongo"] else "Mahalliy sessiya saqlandi. MongoDB hali sozlanmagan.")
        user_id = self.user_id
        await self._close()
        await bot.send_message(user_id, f"✅ Zoom brauzer logini tasdiqlandi. {destination}\n"
            "Keyingi yozuv yangi sessiyani avtomatik ishlatadi; botni qayta ishga tushirish shart emas.")
        return True

    async def done(self, update, context):
        if not await self.authorize(update):
            return
        async with self.lock:
            if not self.browser:
                await update.message.reply_text("Avval /zoomlogin yuboring.")
                return
            try:
                if not await self._save(context.bot):
                    await self._view(context.bot)
            except Exception as exc:
                await self._error(context.bot, update.effective_user.id, exc)

    async def cancel(self, update, context):
        if not await self.authorize(update):
            return
        await self.close()
        await update.message.reply_text("Zoom login bekor qilindi. Avval saqlangan sessiya o‘zgarmadi.")

    async def click(self, update, context):
        if not await self.authorize(update):
            return
        async with self.lock:
            if not self.browser:
                await update.message.reply_text("Avval /zoomlogin yuboring.")
                return
            try:
                if len(context.args) != 2:
                    raise LoginInputError("/zoomclick x y — rasm 1280×900.")
                try:
                    x, y = map(int, context.args)
                except ValueError:
                    raise LoginInputError("/zoomclick x y — ikkita butun son yuboring.") from None
                await self.browser.click(x, y)
                await self._view(context.bot)
            except Exception as exc:
                await self._error(context.bot, update.effective_user.id, exc)

    async def callback(self, update, context):
        if not await self.authorize(update):
            return
        query = update.callback_query
        async with self.lock:
            parts = query.data.split(":")
            if not self.browser or len(parts) != 3 or parts[1] != self.nonce:
                await query.answer("Bu oyna eskirgan. Oxirgi yuborilgan oynadan foydalaning.", show_alert=True)
                return
            await query.answer()
            self.nonce = None  # Invalidate before doing anything asynchronous.
            action = parts[2]
            try:
                if action == "cancel":
                    user_id = self.user_id
                    await self._close()
                    await context.bot.send_message(user_id, "Zoom login bekor qilindi.")
                    return
                if action == "save" and await self._save(context.bot):
                    return
                if action.startswith("b"):
                    await self.browser.click_control(int(action[1:]))
                await self._view(context.bot)
            except Exception as exc:
                await self._error(context.bot, update.effective_user.id, exc)

    async def sync(self, update, context):
        if not await self.authorize(update):
            return
        try:
            found = await auth_store.sync()
            source = "MongoDB" if auth_store.configured else "mahalliy fayl (MongoDB sozlanmagan)"
            await update.message.reply_text(f"Zoom auth: {source}; "
                f"{'sessiya mavjud' if found else 'sessiya yo‘q — /zoomlogin'}. "
                f"Revision: {auth_store.revision}. Bu cookie muddati yoki Zoom qabul qilishini kafolatlamaydi.")
        except Exception as exc:
            await self._error(context.bot, update.effective_user.id, exc)


zoom_auth_bot = ZoomAuthBot()
