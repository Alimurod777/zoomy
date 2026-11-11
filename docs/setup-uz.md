# Zoomy — Alwaysdata’da ishlatish

## Yozuv ko‘rinishi

`ZOOM_CLEAN_RECORDING=true` standart holat. Ekran ulashilganda Zoom’ning
ulashilgan media qatlami to‘liq o‘lchami bilan olinib, yozuvga nisbatini
saqlagan holda sig‘diriladi (`contain`). Profil tasvirlari, pastki panel va
ulashish boshqaruvlari bu qatlamga qo‘shilmaydi. Standart
`ZOOM_RECORDING_SOURCE=shared-only`: ekran ulashilmaganida yoki to‘xtaganida
kamera o‘rniga qora kutish kadri chiqadi, ovoz yozilishi davom etadi.
Kameraga avtomatik o‘tish faqat `shared-and-speaker` bilan alohida yoqiladi.
Bu manba tanlash Web App rendereriga tegishli; native Desktop butun kadrni oladi.

`ZOOM_RECORDING_ASPECT=auto` yozuv boshidagi media nisbatiga mos fayl yaratadi:
tik ekran tik videoga, keng ekran keng videoga tushadi. Faqat renderer yaratgan
ishlatilmayotgan tashqi bo‘sh joy chiqariladi; kontentdan bo‘lak kesilmaydi va
tasvir cho‘zilmaydi. Yozuv davomida manba nisbati o‘zgarsa yangi kontent shu
faylga to‘liq sig‘diriladi, shunda bo‘sh joy qolishi mumkin. Pauza/davom etish
bir xil chiqish o‘lchamini saqlaydi. `/peek` ham yozuvdagi ko‘rinishni yuboradi.
Doim keng kadr uchun `ZOOM_RECORDING_ASPECT=original` qo‘ying.
FFmpeg botning sichqoncha
kursorini chizmaydi. Brauzer paneli chiqarib tashlanadi; toq balandlikni juft
qilish uchun pastki pikselni kesish o‘rniga qora qator qo‘shiladi.

Zoom brauzer interfeysi o‘zgarsa yoki media qatlami aniqlanmasa, kontentni
yo‘qotmaslik uchun to‘liq odatiy ko‘rinish saqlanadi. Yangi ko‘rinish keyingi
yozuvlarga qo‘llanadi. Avvalgi yozuv fayllari o‘zgarmaydi.

Zoomy Telegram buyrug‘i bilan Zoom uchrashuviga kiradi, ekran va ovozni MP4’ga
yozadi. Playwright/Chromium Zoom oynasini ochadi, Xvfb virtual ekran beradi,
PulseAudio ovozni ajratadi va FFmpeg yozadi. faster-whisper matn va subtitr
yaratadi; AI xulosa, YouTube va FileBrowser qo‘shimcha imkoniyatlardir.

## Avtomatik kirish va kutish

Standart `AUTO_RECORD_SETUP=true`: `/record TO‘LIQ_ZOOM_HAVOLA` yuborilganda
ism va fayl nomi haqida qo‘shimcha savolsiz boshlaydi. Parolli konferensiya
uchun `?pwd=...` bor to‘liq taklif havolasini yuboring. Saqlangan profilning
display name qiymati ishlatiladi; fayl nomi konferensiya sarlavhasidan, u
aniqlanmasa uchrashuv ID’si va vaqtdan olinadi.

Brauzerga kirishning har bir urinishi 25 soniya bilan cheklangan. Konferensiya
boshlanishi yoki host ruxsatini kutish alohida: `ZOOM_HOST_WAIT_SECONDS=7200`
(2 soat). `/stop` kutishni ham bekor qiladi. Hali boshlanmagan konferensiya
`ZOOM_JOIN_RETRY_SECONDS=30` oralig‘ida qayta tekshiriladi. Waiting room’da
sahifa qayta yuklanmaydi, navbat saqlanadi.

Vaqtinchalik aloqa uzilsa `ZOOM_RECONNECT_ATTEMPTS=5` gacha tiklashga
urinadi. Oldingi yozuv bo‘laklari saqlanadi va birlashtiriladi; tiklanish
oralig‘i yozuvga kiritilmaydi. Host efirni tugatsa yoki botni chiqarsa yozuv
to‘xtaydi. Qo‘lda ism tanlash va rejalashtirish uchun `AUTO_RECORD_SETUP=false`.

`ZOOM_JOIN_MODE=auto` avval asl taklif havolasini tekshiradi. Sahifa
**Join from Zoom Workplace app** desa yoki joriy meeting uchun `zoommtg://`
so‘rovini yuborsa, Web App URL’iga almashtirmasdan haqiqiy Linux Zoom
Desktop’ga o‘tadi. **Join from Your Browser** varianti bo‘lsa Web App tanlanadi. Cheklov sahifada
aniqlanmayotgan bo‘lsa `ZOOM_JOIN_MODE=desktop` bilan to‘g‘ridan-to‘g‘ri
ilovadan kirish mumkin. `web` faqat brauzer yo‘lini ishlatadi.

Desktop’ni tayyorlash (bu serverda bajarildi):

```bash
bash scripts/setup-zoom-desktop.sh
source scripts/runtime-env.sh
python scripts/check-zoom-desktop.py
```

Rasmiy Zoom 7.2.5.6661 paketi checksum bilan tekshirib, loyiha ichiga ochiladi.
`xdotool`, `openbox`, `tesseract` va Pillow talab qilinadi. Desktop yo‘li
sinov bosqichida: oyna ochilishi yozuv boshlangani hisoblanmaydi, haqiqiy
konferensiya boshqaruvlari tekshiriladi. Haqiqiy ilovani ochish va ataylab noto‘g‘ri Meeting ID’ni aniqlab,
yozuvni boshlamaslik tekshirildi. Hali real hostli efirda tekshirilmagan.
Native ilovaning sovuq ishga tushishi server yukiga qarab 30 soniyadan oshishi
mumkin; brauzer uchun belgilangan 25 soniyalik urinish chegarasi unga tatbiq
qilinmaydi.

Desktop logini brauzer cookie’laridan alohida. Akkauntga muvaffaqiyatli
kirish barcha uchrashuvlar uchun shart emas: mehmonlarga ochiq taklif Desktop
orqali login qilmasdan ishlaydi. Home oynasidagi oddiy **Sign in** tugmasi
akkaunt talab qilinganini bildirmaydi; host yoki qabulni kutayotganda login
havolasi yuborilmaydi. Akkauntga muvaffaqiyatli
kirgach, Zoom o‘zining mahalliy profilida (`~/.zoom/data`, sozlamalari
`~/.config/zoomus.conf`) autentifikatsiya holatini saqlaydi. Login oynasida
**Keep me signed in** varianti bo‘lsa uni belgilang. Bot bu kataloglarni har
bir yozuvdan keyin o‘chirmaydi va akkauntdan Sign out qilmaydi. Sessiya Zoom
tomonidan bekor qilinsa yoki muddati tugasa qayta login kerak bo‘lishi mumkin.
`ZOOM_DESKTOP_SYNC=true` (standart) va MongoDB sozlangan bo‘lsa, Zoom odatiy
yopilgach asosiy profil bazalari va Zoom sozlamalarining shifrlangan nusxasi
alohida `desktop_auth` kolleksiyasiga saqlanadi. Mavjud `MONGODB_AUTH_KEY` va
`ZOOM_AUTH_ID` ishlatiladi; yangi kalit talab qilinmaydi. Keyingi ochilishdan
oldin mahalliy profil bo‘sh bo‘lsa nusxa tiklanadi. Mavjud mahalliy profil
almashtirilmaydi; boshqa serverning farqli profili umumiy nusxani bosib
yozolmaydi. Revision tekshiruvi parallel o‘zgarishlarni ham himoya qiladi.
Loglar, chat bazalari va keshlar olinmaydi; nusxa chegarasi 8 MiB. Ishlayotgan
Zoom profilidan yoki majburiy `SIGKILL`dan keyin nusxa olinmaydi. Xato bo‘lsa
mahalliy profil saqlanadi, jurnalga ogohlantirish yoziladi. Bu davriy jonli
sinxronlash emas: nusxa har bir Desktop yozuvi tugagach yangilanadi.
**Profil tiklangani login tasdiqlandi degani emas**: boshqa serverda Zoom
qayta kirishni talab qilishi mumkin. MongoDB’dagi brauzer cookie’lari Native
login hisoblanmaydi. Login yoki
qo‘shimcha ruxsat kerak bo‘lsa faqat `OWNER_TELEGRAM_ID` egasiga 15 daqiqalik
boshqaruv havolasi yuboriladi. Desktop bir server foydalanuvchisida bir vaqtda
bitta yozuvga xizmat qiladi; boshqa ochiq Zoom nusxasiga ulanmaydi.

Desktop’da alohida ovozsiz mikrofon va yozuvning o‘z audio kanali ishlatiladi.
Asosiy tasvir butun kadr bilan saqlanadi. Ushbu serverdagi haqiqiy Zoom
Settings oynasida quyidagilar sozlandi va ilovani qayta ochganda saqlangani
tekshirildi (yangi server yoki profil uchun Zoom ichida yana sozlang):

- **Meetings & webinars → My video → Hide** — botning o‘z profil/video bloki yashirin.
- **Show non-video participants → off** — kamerasiz qatnashchilar bloklari yashirin.
- **Keep meeting controls visible → off**, **Keep participant names visible → off**.
- **Enter full screen automatically → on**.
- **Share screen → Enter into fullscreen**, **Scale-to-fit content → on**,
  **See shared content in side-by-side mode → off**.

Bot sichqonchani pastki panelni ochadigan burchakda qoldirmaydi. Yozuvdan
oldin boshqaruvlar yashirinishi uchun 5 soniyagacha kutadi; `/stop` bu kutishni
bekor qiladi. To‘liq ekran holati `wmctrl` bilan aniq o‘rnatiladi, klaviatura
tugmasi bilan qayta-qayta almashtirilmaydi. Ekran ulashish menyusi mavjud
bo‘lganda **View Options → Hide Video Panel** buyrug‘i ishlatiladi; panel
allaqachon yashirilgan bo‘lsa qayta ko‘rsatilmaydi. Bu menyu yo‘li avtomatik
testlarda tekshirildi; haqiqiy host ekran ulashishi bilan hali tekshirilmagan.
Zoom muayyan xabar/panelni baribir ko‘rsatsa, uning o‘rnini yopish yoki
kontentni kesish ishlatilmaydi. Brauzerdagi canvas tozalash usuli Web App’ga
tegishli. Aloqani ilovaning o‘zi tiklashga urinadi; 30 soniyada tiklanmasa
oldingi yozuv saqlanib jarayon to‘xtaydi.

## Alwaysdata resurslari va ishga tushirish

SSH loginidagi cheklovlarga amal qiling: doimiy botni SSH’da `nohup`, `screen`
yoki `tmux` bilan qoldirish bu cheklovni bartaraf etmaydi. Pullik tarifdagi
**Advanced → Services** uchun foreground command:

```text
/bin/bash /home/telegramdata4/zoomy/scripts/run.sh
```

Working directory maydoni bo‘lsa `/home/telegramdata4/zoomy`. Bot inbound port
talab qilmaydi, Telegram’dan polling bilan xabar oladi. Services’ni yoqishdan
oldin SSH’dagi nusxani to‘xtating; bitta bot tokeni bilan ikkita polling
nusxasini ochmang. SIGHUP restart signali yozuvlarni yakunlashga va Whisper
ishchisini to‘xtatishga yo‘naltiriladi. Rasmiy ko‘rsatma:
https://help.alwaysdata.com/en/docs/web-hosting/services/

Servicesiz boshqa ruxsat etilgan serverdan yoki Web → Sites → User program
orqali webhook arxitekturasidan foydalanish mumkin. Hozirgi kod polling
rejimida; webhook transporti hali qo‘shilmagan. Sayt variantida Idle time `0`
bo‘lishi kerak, biroq platforma sayt jarayonining uzluksiz ishlashini
kafolatlamaydi: https://help.alwaysdata.com/en/docs/web-hosting/sites/misc/
Zoom/FFmpeg uchun yozuv davomida uzluksiz jarayon zarur. Resurslarni kamaytirish
hosting limitlariga sig‘ishni kafolatlamaydi.

Terminaldagi `/stop` va **Yes, Stop** faqat tanlangan yozuvni yakunlaydi;
bot keyingi buyruqlarni kutishda davom etadi. Botni to‘liq yopish — `Ctrl+C`.
Terminal ochiq turishi tashqi SIGKILL’dan himoya qilmaydi. Shell’dagi `Killed`
odatiy `/stop` natijasi emas; aniq sabab uchun server loglari kerak.

Yengil standart sozlamalar: bitta yozuv, `RECORD_FPS=15`, `FFMPEG_THREADS=2`,
`WHISPER_THREADS=2`, `WHISPER_MODEL=small`, `AUTO_TRANSCRIBE=false`.
`large-v3` uchun `WHISPER_ALLOW_LARGE=true` talab qilinadi. Transkripsiya
navbati faol yozuvlar tugashini kutadi. Har bir ish alohida, pastroq CPU
ustuvorligidagi jarayonda bajariladi; tugaganda model RAM’dan chiqadi.
Bekor qilish, shutdown va Linux’da ota jarayon o‘lishi ishchini to‘xtatadi.
`WHISPER_JOB_TIMEOUT=1800` ish muddatini cheklaydi.

Kichik Silero VAD tekshiruvi nutqni Whisper modelidan oldin qidiradi. Nutq
bo‘lmasa model yuklanmaydi, til taxmin qilinmaydi va bo‘sh TXT/SRT muvaffaqiyatli
transkripsiya deb ko‘rsatilmaydi. Video saqlanadi. `AUDIO_WAIT_TIMEOUT` endi
haqiqiy soniyalik chegara: muddat tugasa video ovoz jim bo‘lsa ham boshlanadi.
Noma’lum kirish oynasining har bir urinishi `ZOOM_CONNECT_TIMEOUT=30` bilan
chegaralanadi. `ZOOM_JOIN_ATTEMPTS=3` bo‘yicha avtomatik qayta urinadi;
waiting room va hostni kutish alohida davom etadi. “Host has joined. We’ve let
them know you’re here” ham waiting room deb taniladi, sahifa yangilanmaydi.
“Launch Meeting / Open Zoom” tugmalari yoki joriy meeting uchun `zoommtg://`
protokoliga o‘tish aniqlansa `auto` rejimi haqiqiy Desktop’ni ishga tushiradi.
Bu brauzerning tashqi ilova dialogini o‘zi yopishini kutishga bog‘liq emas.

Atlas URI tanlanganda lokal MongoDB qayta ishga tushirilmaydi. 2026-11-11:
keraksiz lokal MongoDB va `Killed`dan qolgan loyiha `:99` Xvfb jarayoni
to‘xtatildi. Yangi ekran ajratishda mavjud X serverning lock fayli o‘chirilmaydi.

## `venv` va root huquqisiz dasturlar

Loyiha ildizidan (ushbu serverda allaqachon bajarilgan):

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r bot/requirements.txt
python -m playwright install chromium
bash scripts/setup-runtime.sh
bash scripts/setup-mongodb.sh
bash scripts/setup-login-link.sh
```

Setup skripti Debian 12 amd64 paketlarini rasmiy ombordan `.runtime/root`
ichiga ochadi. Root, sudo va Docker kerak emas. FFmpeg/ffprobe, Xvfb,
PulseAudio, pactl/parec, x11vnc va noVNC shu yerdan ishlaydi. Paketlar shu
serverdagi mavjud kutubxonalarga mos; boshqa tizimda qayta tekshirish kerak.

Xvfb ichidagi `xkbcomp` yo‘li faqat loyiha nusxasida o‘zgartiriladi; wrapper
uni o‘z katalogidan ishga tushiradi. PulseAudio loyiha konfiguratsiyasi va
`/tmp/zoomy-UID/pulse` Unix soketi bilan ishlaydi. Tizim kataloglariga yozilmaydi.

## `.env` ni to‘ldirish

Mahalliy yo‘llar bilan maxfiy `.env` namunasi tayyor. `scripts/run.sh`
faylni `python-dotenv` bilan o‘qiydi, shuning uchun MongoDB URI ichidagi `&`
belgisi ham to‘liq saqlanadi. `.env` ni shell’da `source` qilish shart emas.
Quyidagilarni kiriting:

```env
TELEGRAM_TOKEN=BOTFATHER_TOKEN
AUTHORIZED_USER_IDS=OZ_TELEGRAM_USER_ID
OWNER_TELEGRAM_ID=OZ_TELEGRAM_USER_ID
TELEGRAM_UPLOAD_ENABLED=true
TELEGRAM_API_ID=MY_TELEGRAM_ORG_API_ID
TELEGRAM_API_HASH=MY_TELEGRAM_ORG_API_HASH
TELEGRAM_SPLIT_MB=1900
```

API ID va hash https://my.telegram.org → API development tools’dan olinadi.
Pyrofork bot tokenidan foydalanadi: shaxsiy Telegram akkauntingizning telefon
kodi yoki user-session kerak emas. Botni Telegram’da avval `/start` bilan oching.
Guruhga yuborish uchun botni qo‘shing va yuborish huquqini bering.
Bot uchun ham API kalitlari kerak:
https://pyrofork.mayuri.my.id/staging/start/auth.html

## Zoom loginini Telegram ichidan bajarish

Botni `bash scripts/run.sh` bilan ishga tushirib, **OWNER_TELEGRAM_ID** egasining
shaxsiy chatida `/zoomlogin` yuboring. Standart `ZOOM_LOGIN_MODE=link` rejimida
bot vaqtinchalik HTTPS havola beradi. Havolani o‘z brauzeringizda ochib, Zoom
login, parol va 2FA’ni o‘zingiz bajarasiz. Sahifadagi **Loginni saqlash** yoki
botdagi shu nomli tugmani bosing. Bot profil sahifasini tekshirib, sessiyani
MongoDB’ga shifrlab saqlaydi va vaqtinchalik ulanishni yopadi. SSH kerak emas.

Havola 256-bit tasodifiy token bilan himoyalangan va vaqt bilan cheklangan.
noVNC/VNC faqat localhost’da ishlaydi; Cloudflare Quick Tunnel vaqtinchalik
HTTPS ulanish beradi. Saqlashdan yoki bekor qilishdan keyin link o‘chadi.
Maydonga matnni joylash tugmasi bor; uning oldidan Zoom’dagi kerakli maydonni
bosing. Sessiya ma’lumotlari HTTP loglarga yozilmaydi.

Brauzer va Desktop boshqaruv sahifasida **⌫ Bitta belgi**, **Hammasini
tanlash**, **Zoom maydonini tozalash** bor. Avval masofadagi Zoom oynasida
kerakli email/parol/kod maydonini bosing, keyin tegishli tugmani bosing.
**Oldingi matnni almashtirish** yoqilgan bo‘lsa kiritilgan matn butun maydonni
almashtiradi; o‘chirilgan bo‘lsa mavjud matnga qo‘shiladi. Tozalash faqat
joriy tanlangan matn maydoniga qaratilgan, akkaunt sessiyasini o‘chirmaydi.
Yangi boshqaruvlar uchun yangilangan botdan yangi havola oching.

Kiritish maydoni odatda matnni ko‘rsatadi; **Matnni yashirish** bilan berkitish
mumkin. **Kiritish + Enter** matnni tanlangan Zoom maydoniga bir yo‘la joylab,
keyingi qadamga o‘tadi. Unicode matn ham qo‘llanadi. Pastdagi yordam email,
parol yoki tasdiqlash kodi bosqichiga qarab o‘zgaradi; kiritilgan qiymatni o‘qimaydi.

Login brauzeri 1280×960 chegarasida ishlaydi va telefon/kompyuter oynasiga
mos ravishda qayta o‘lchamlanadi. Bu o‘zgarish sessiyani yangidan boshlamaydi.
**100% kattalashtirish**, **Oynani surish**, **To‘liq ekran** va **Kiritishni
yashirish** tugmalari kichik ekranlarda qulayroq ishlash uchun mavjud.

Botni qayta ishga tushirmasdan bir martalik link ochish:

```bash
source scripts/runtime-env.sh
python scripts/zoom-login-link.py
```

Havola `.runtime/zoom-login-link.json` ichida; sahifadagi saqlash tugmasi aynan
shu oynadagi loginni saqlaydi. Bu fayl va URL’ni faqat o‘zingiz ishlating.

Muqobil `ZOOM_LOGIN_MODE=telegram` rejimida bot Zoom oynasini rasm
va tugmalar bilan yuboradi. Email, keyin parol, keyin Zoom so‘ragan
email/SMS/Authenticator kodini shu chatga yozing. Login tekshirilgach brauzer
sessiyasi MongoDB’da shifrlab saqlanadi. Parol va bir martalik kod bazaga
saqlanmaydi; ularning Telegram xabarlarini bot o‘chirishga urinadi.

Maydonlar yuboriladigan rasmlarda yopiladi. Login faqat bot egasining shaxsiy
chatida ishlaydi; guruhlar va boshqa foydalanuvchilar kira olmaydi. Tugmalar
joriy oynaga bog‘langan. 15 daqiqadan keyin tugallanmagan login yopiladi.

- `/zoomlogin` — brauzer loginini boshlash yoki joriy oynani ko‘rish.
- `/zoomdone` — profil sahifasi orqali tekshirish va sessiyani saqlash.
- `/zoomclick x y` — 1280×900 rasmdagi joyni bosish, masalan CAPTCHA uchun.
- `/zoomcancel` yoki `/cancel` — vaqtinchalik loginni yopish.
- `/zoomsync`, `/zoomstatus` — saqlangan sessiyani sinxronlash va holatini ko‘rish.

Zoom ko‘rsatgan tugmalar botda ham chiqadi; kod yuborish variantini o‘sha
tugmalardan tanlash mumkin. Kodni Zoom beradi, bot uni avtomatik aylanib o‘tmaydi.
CAPTCHA va SSO oqimlari o‘zgarishi mumkin; Telegram oynasida davom etib bo‘lmasa,
quyidagi noVNC usulidan foydalaning. Login tasdiqlanmasdan sessiya saqlanmaydi.

## MongoDB va sinxronlash

Ushbu serverda MongoDB 8.0.30 root huquqisiz o‘rnatildi, parolli ulanish
`127.0.0.1:27018` da tayyor. `.env` dagi URI va shifrlash kaliti yaratilgan.
`scripts/run.sh` mahalliy bazani kerak bo‘lsa ishga tushiradi.

```bash
venv/bin/python scripts/local-mongo.py status
venv/bin/python scripts/local-mongo.py start
```

MongoDB fayllari `.runtime/mongodb/data`, boshqaruv parollari
`secrets/local-mongo.json` ichida. Ushbu katalogni oddiy cache deb o‘chirmang.
Mahalliy bazadan tashqari boshqa serverlar o‘rtasida sinxronlash uchun umumiy
MongoDB/Atlas manzilini ishlating:

```env
MONGODB_URI='mongodb+srv://USER:PASSWORD@YOUR_CLUSTER/'
MONGODB_LOCAL_ENABLED=false
MONGODB_DATABASE=zoomy
MONGODB_AUTH_KEY=SAME_FERNET_KEY_ON_ALL_SERVERS
ZOOM_AUTH_ID=default
ZOOM_AUTH_SYNC_SECONDS=60
```

Har bir serverda URI, database, auth ID va shifrlash kaliti bir xil bo‘lsin.
Masofaviy baza ushbu serverlardan ulanishga ruxsat berishi kerak. Kalit MongoDB’da
saqlanmaydi; uni yo‘qotish saqlangan sessiyani ochib bo‘lmasligiga olib keladi.
Bazadagi hujjat faqat shifrlangan `sealed_state`, revision va vaqtni saqlaydi.

Bot boshlanishida va har 60 soniyada MongoDB’dagi sessiya mahalliy cache bilan
sinxronlanadi. Har bir yangi yozuv oldidan recorder bazadan oxirgi sessiyani
bevosita oladi. Amaldagi yozuv sessiyasi o‘zgarmaydi. MongoDB sozlangan, ammo
ulanish yoki kalit xato bo‘lsa eski cookie’ga jimgina qaytilmaydi — xato bildiriladi.
Bo‘sh baza mavjud mahalliy login faylidan bir marta atomik to‘ldiriladi;
bazadagi yangi sessiya mahalliy eski fayldan ustun turadi.

Haqiqiy baza bilan ikki klient orqali shifrlash va sinxronlashni tekshirish:

```bash
source scripts/runtime-env.sh
set -a; source .env; set +a
python scripts/check-auth-sync.py
```

Tekshiruv faqat tasodifiy ID’li sinov hujjatini yaratib, oxirida o‘chiradi;
asosiy Zoom loginini o‘zgartirmaydi.

Mahalliy `RECORDINGS_DIR`, `ZOOM_STATE_PATH`, `YOUTUBE_TOKEN_PATH` ushbu
loyihadagi absolyut yo‘llardir. `.env.example` esa Docker’dagi `/recordings`
va `/secrets` yo‘llarini ishlatadi. `.env` shell orqali o‘qiladi: bo‘sh joyli
qiymatlarni qo‘shtirnoqqa oling. Kalitlarni Git’ga qo‘shmang.

## Muqobil: noVNC orqali grafik monitorsiz Zoom login

Serverda:

```bash
cd ~/zoomy
source scripts/runtime-env.sh
set -a; source .env; set +a
python bot/zoom_login.py --remote
```

Buyruq virtual ekran, Chromium, parolli VNC va faqat `127.0.0.1` da tinglaydigan
noVNC’ni vaqtincha ochadi. Terminal vaqtinchalik VNC parolini chiqaradi.
Kompyuteringizdagi boshqa terminalda:

```bash
ssh -N -L 6080:127.0.0.1:6080 YOUR_ALWAYS_DATA_SSH_LOGIN
```

Oxirgi argument o‘rniga Alwaysdata panelidagi SSH `foydalanuvchi@host` manzilini
yozing. Tunnel ochiq paytda kompyuteringizda http://127.0.0.1:6080/vnc.html ni
oching, Connect tugmasini bosing va vaqtinchalik VNC parolini kiriting.

Shu oynada Zoom akkauntingizga kiring. 2FA, email tasdig‘i yoki SSO’ni odatdagidek
bajaring. Keyin server terminalida Enter bosing. Yordamchi profil sahifasini
tekshiradi va `secrets/zoom_state.json` ni `0600` huquqi bilan saqlaydi;
brauzer va masofaviy ekran yopiladi. Bu fayl jonli login sessiyasidir.

SSO uchun `--url https://YOUR_ORG.zoom.us/signin` qo‘shish mumkin.
Band port/ekran uchun `--display`, `--web-port`, `--vnc-port` mavjud;
SSH tunnel portini ham moslang. Internetga ochiq port kerak emas.

Zoom OAuth/API tokeni brauzer login sessiyasining o‘rnini bosmaydi. Bu usul
odatiy login oynasini masofadan boshqaradi; parol/2FA’ni chetlab o‘tmaydi.
Sessiya eskirganda qayta login qiling. Host domenni cheklasa, mos tashkilot
akkaunti yoki host bergan authentication-exception havolasi kerak; `tk=` ni saqlang.
Rasmiy izoh: https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0063837

## Bot va MTProto upload

```bash
bash scripts/run.sh
```

Doimiy ishlatish uchun shu buyruqni Alwaysdata xizmatiga (Services) qo‘ying.
Polling ishlatiladi: HTTP sayt/webhook shart emas.

Telegram’da `/record ZOOM_LINK` yuboring. `/stop` yoki uchrashuv tugashidan
keyin, kalitlar sozlangan bo‘lsa, MP4 Pyrofork/MTProto orqali shu chatga yuklanadi.
Oldingi yozuv yoki qayta urinish: `/history` → yozuv → **📤 Telegram video**.

- Standart qism chegarasi 1900 MiB; `TELEGRAM_SPLIT_MB` 1–2000 oralig‘ida.
- Kichik video bitta fayl. Kattasi keyframe chegaralarida qayta kodlanmasdan
  alohida ochiladigan MP4 qismlarga bo‘linadi. Har bir haqiqiy hajm tekshiriladi;
  katta chiqsa kichikroq segment vaqti bilan takrorlanadi.
- Bitta keyframe oralig‘i ham sig‘masa xato qaytariladi va asl video saqlanadi.
- Yuklashlar navbat bilan bajariladi; qisqa `FloodWait` qayta uriniladi.
  Xatodan keyin `/history` dan davom ettiring.
- `.telegram.json` yuborilgan qism ID’larini saqlaydi. Fayl, chat va bo‘lish
  sozlamasi o‘zgarmagan bo‘lsa qayta urinish muvaffaqiyatli qismlarni tashlab o‘tadi.
- Vaqtinchalik bo‘laklar muvaffaqiyat/xato/bekor qilishda tozalanadi; asl video
  upload sabab o‘chirilmaydi. Upload paytida papkani bot orqali o‘chirish,
  qayta nomlash va YouTube retention orqali o‘chirish to‘siladi.

MTProto yo‘lida HTTP Bot API’dagi 50 MB upload chegarasi ishlatilmaydi;
Local Bot API serveri talab qilinmaydi. YouTube sozlangan bo‘lsa avvalgi
upload va retention ishlashi ham davom etadi.

## Tekshirish

```bash
source scripts/runtime-env.sh
python -m pip check
python -m unittest discover -s tests -v
python -m compileall -q bot tests scripts
python scripts/check-runtime.py
```

`check-runtime.py` haqiqiy uchrashuvga kirmaydi. Chromium sinov tasviri va
440 Hz ovozni `.runtime/smoke.mp4` ga yozadi, video/ovoz va ovoz jim emasligini
tekshiradi. Testlar Telegram so‘rovlarini taqlid qiladi; real bo‘lish testi
qismlarni FFmpeg bilan dekodlab tekshiradi.

2026-11-10: brauzer login havolasining HTTPS, token nazorati, WebSocket va
VNC ulanishi haqiqiy tunnel orqali tekshirildi. Testlar token, muddat, origin
va tasdiqlanmagan loginni saqlamaslikni ham qamraydi.
Haqiqiy mahalliy MongoDB’da shifrlangan sessiyani qayta ulanishda olish va ikki
klient o‘rtasida revision 2 gacha sinxronlash tekshirildi.
Haqiqiy Zoom login va Telegram upload uchun akkauntga kirish hamda `.env`
kalitlarini to‘ldirish kerak.

Jadval vaqti hozir `Asia/Jakarta` (UTC+7). Whisper standart kontekst lug‘ati
indonezcha; o‘zbekcha uchun `WHISPER_LANGUAGE=uz`, `WHISPER_INITIAL_PROMPT`
va `WHISPER_HOTWORDS` ni moslang.
