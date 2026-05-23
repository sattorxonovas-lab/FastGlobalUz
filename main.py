import logging
import asyncio
import sqlite3
import re
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware, Bot, Dispatcher, types as ad_types, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# TELETHON IMPORTLARI
from telethon import TelegramClient, functions
from telethon import tl
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.photos import DeletePhotosRequest

# ==================== SOZLAMALAR ====================
BOT_TOKEN = "8730248955:AAGn3QBpLJOvqY7yP2waQA2t8lJsO2XVdFM"
ADMIN_IDS = [8254560260]
ADMIN_USERNAME = "FastAdmin_Uz"
CHANNEL_USERNAME = "@FastGlobalUz"
FEEDBACK_CHANNEL = "@FastGlobalUz_otziv"
TANISHUV_SAYT_URL = "https://fastglobaluz.netlify.app"  # Agar tanishuv saytingiz bo'lsa, URL ni shu yerga qo'ying, aks holda None qoldiring

API_ID = 20429961
API_HASH = "4ad3a141f391112f26aa88ee88f2c7b0"

CARD_NUMBER = "9860 3566 1268 9014"
CARD_HOLDER = "S.S"

CHANNELS = [
    {"name": "1-Kanal 📢", "url": "https://t.me/FastGlobalUz", "id": "@FastGlobalUz"},
    {"name": "2-Kanal 📢", "url": "https://t.me/FastGlobalUz_otziv", "id": "@FastGlobalUz_otziv"},
]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==================== FSM HOLATLARI ====================
class AdminStates(StatesGroup):
    waiting_for_ad_content = State()
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()
    waiting_for_name = State()
    waiting_for_price = State()
    waiting_for_ban_id = State()

class UserStates(StatesGroup):
    waiting_for_receipt = State()

# ==================== MA'LUMOTLAR BAZASI ====================
def init_db():
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    # Foydalanuvchilar jadvali
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, status TEXT DEFAULT 'active')")
    # Bazada 'settings' degan jadval ochamiz (agar yo'q bo'lsa)
    cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('bot_status', 'active')")
    cursor.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('rating_week_start', ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),)
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS purchase_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            purchased_at TEXT NOT NULL
        )
    """)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'")
    except sqlite3.OperationalError:
        pass
    # Raqamlar jadvaliga 'country' va 'post_url' uchun ustunlar qo'shish kerak (agar yo'q bo'lsa)
    # Buning uchun bazani bir marta yangilash kifoya
    # Kodingizdagi init_db funksiyasini shunday qilib yangilang:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            name TEXT,
            price TEXT,
            password_2fa TEXT,
            status TEXT DEFAULT 'available',
            session_file TEXT,
            received_code TEXT DEFAULT 'kutilmoqda',
            sold_at TEXT DEFAULT NULL,
            user_id INTEGER  -- <--- SHUNI QO'SHASIZ
        )
    """)
    conn.commit()
    conn.close()

def add_number_to_db(phone, name, price, password_2fa, session_file):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO numbers (phone, name, price, password_2fa, session_file, received_code) VALUES (?, ?, ?, ?, ?, 'kutilmoqda')",
        (phone, name, price, password_2fa, session_file)
    )
    conn.commit()
    conn.close()

def update_received_code(phone, code):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE numbers SET received_code=? WHERE phone=?", (code, phone))
    conn.commit()
    conn.close()

# ⏱️ 30 SOATDAN O'TGAN SOTILGAN RAQAMLARNI BUTUNLAY BAZADAN O'CHIRISH
def auto_clean_sold_numbers():
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, sold_at FROM numbers WHERE status='sold' AND sold_at IS NOT NULL")
    sold_list = cursor.fetchall()
    
    for num_id, sold_time_str in sold_list:
        try:
            sold_time = datetime.strptime(sold_time_str, "%Y-%m-%d %H:%M:%S")
            if datetime.now() - sold_time > timedelta(hours=30):
                cursor.execute("DELETE FROM numbers WHERE id=?", (num_id,))
                logging.info(f"⏳ 30 soat bo'lgani uchun raqam (ID: {num_id}) butunlay o'chirildi.")
        except Exception as e:
            logging.error(f"Avto-tozalashda xatolik: {e}")
            
    conn.commit()
    conn.close()

init_db()


def backfill_week_purchases_from_numbers():
    """Eski sotuvlarni (shu hafta) reyting logiga bir marta ko'chiradi."""
    week_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect("bot_users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'rating_week_start'")
        res = cursor.fetchone()
        if res:
            week_start = res[0]
        cursor.execute("SELECT COUNT(*) FROM purchase_log")
        if cursor.fetchone()[0] > 0:
            conn.close()
            return
        cursor.execute(
            """
            SELECT user_id, sold_at FROM numbers
            WHERE status='sold' AND user_id IS NOT NULL AND sold_at >= ?
            """,
            (week_start,)
        )
        for user_id, sold_at in cursor.fetchall():
            cursor.execute(
                "INSERT INTO purchase_log (user_id, purchased_at) VALUES (?, ?)",
                (int(user_id), sold_at)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Reyting backfill xatosi: {e}")


backfill_week_purchases_from_numbers()


def get_user_status(user_id):
    try:
        conn = sqlite3.connect("bot_users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        conn.close()
        return res[0] if res else 'active'
    except Exception:
        return 'active'


BOT_STOP_MESSAGE = (
    '🙏 Ming bor uzur, bizning "FastGlobalUz" loyhamiz vaqtincha ish faoliyatida emas.\n\n'
    "Biz hali katta o'zgarishlar bilan qaytamiz."
)
BOT_RESUME_MESSAGE = "👋Assalomu alekom bot yana faol bemalol foydalanishingiz mumkin." \
    "\n\nHurmat bilan (FastGlobalUz) jamoasi ✅" \
    "\n\nQayta ishga tushurish uchun /start buyrug'ini yuboring."


def is_bot_stopped() -> bool:
    try:
        conn = sqlite3.connect("bot_users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'bot_status'")
        res = cursor.fetchone()
        conn.close()
        return res is not None and res[0] == 'stopped'
    except Exception:
        return False


def set_bot_status(status: str):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('bot_status', ?)",
        (status,)
    )
    conn.commit()
    conn.close()


def get_bot_stop_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        ad_types.InlineKeyboardButton(text="✅ Yoqish", callback_data="bot_stop_enable"),
        ad_types.InlineKeyboardButton(text="⛔ O'chirish", callback_data="bot_stop_disable")
    )
    builder.row(ad_types.InlineKeyboardButton(text="⬅️ Admin panel", callback_data="back_to_admin_panel"))
    return builder.as_markup()


class BotStopMiddleware(BaseMiddleware):
    """Adminlardan tashqari foydalanuvchilarni bot to'xtatilgan paytda bloklaydi."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        if user and user.id not in ADMIN_IDS and is_bot_stopped():
            if isinstance(event, Message):
                await event.answer(BOT_STOP_MESSAGE)
            elif isinstance(event, CallbackQuery):
                await event.answer()
                if event.message:
                    await event.message.answer(BOT_STOP_MESSAGE)
            return None

        return await handler(event, data)


dp.message.middleware(BotStopMiddleware())
dp.callback_query.middleware(BotStopMiddleware())


def get_all_users():
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]


# ==================== HAFTALIK REYTING ====================
def get_rating_week_start() -> datetime:
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'rating_week_start'")
    res = cursor.fetchone()
    conn.close()
    if res:
        try:
            return datetime.strptime(res[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    now = datetime.now()
    set_rating_week_start(now)
    return now


def set_rating_week_start(dt: datetime):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('rating_week_start', ?)",
        (dt.strftime("%Y-%m-%d %H:%M:%S"),)
    )
    conn.commit()
    conn.close()


def record_weekly_purchase(user_id: int):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO purchase_log (user_id, purchased_at) VALUES (?, ?)",
        (int(user_id), now_str)
    )
    conn.commit()
    conn.close()


def get_weekly_ranking(limit: int = 10) -> list[tuple[int, int]]:
    week_start = get_rating_week_start().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT user_id, COUNT(*) AS cnt
        FROM purchase_log
        WHERE purchased_at >= ?
        GROUP BY user_id
        ORDER BY cnt DESC, user_id ASC
        LIMIT ?
        """,
        (week_start, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return [(int(r[0]), int(r[1])) for r in rows]


def format_rating_week_header() -> str:
    ws = get_rating_week_start()
    return f"📊 Bu hafta: {ws.strftime('%d.%m.%Y')} || {ws.strftime('%H:%M:%S')}"


def build_weekly_rating_text(for_broadcast: bool = False) -> str:
    ranking = get_weekly_ranking(limit=10)
    lines = [
        format_rating_week_header(),
        "",
        'Bu haftada eng zo\'r natija qilgan "Top-10 Mijoz" Ro\'yxat:',
        ""
    ]
    if not ranking:
        lines.append("Hozircha reytingda hech kim yo'q.")
    else:
        for place, (uid, count) in enumerate(ranking, 1):
            lines.append(f"{place}) --> {uid} --> {count} ta")
    if not for_broadcast:
        days_left = 7 - (datetime.now() - get_rating_week_start()).days
        if days_left < 0:
            days_left = 0
        lines.append(f"\n⏳ Hafta yakuni: {days_left} kun qoldi")
    return "\n".join(lines)


async def broadcast_weekly_rating():
    text = build_weekly_rating_text(for_broadcast=True)
    text = f"🏆 <b>HAFTALIK REYTING YAKUNLANDI</b>\n\n{text}"

    try:
        await bot.send_message(chat_id=CHANNEL_USERNAME, text=text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Reyting kanalga yuborilmadi: {e}")

    users = get_all_users()
    for u_id in users:
        try:
            await bot.send_message(chat_id=u_id, text=text, parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(0.03)


async def check_and_reset_weekly_rating():
    if is_bot_stopped():
        return
    week_start = get_rating_week_start()
    if datetime.now() - week_start < timedelta(days=7):
        return
    await broadcast_weekly_rating()
    set_rating_week_start(datetime.now())


async def check_sub_status(user_id: int) -> bool:
    for channel in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel["id"], user_id=user_id)
            if member.status in ['left', 'kicked']: return False
        except Exception:
            return False
    return True

# ==================== KLAVIATURALAR BO'LIMI ====================

def get_main_menu(user_id: int):
    builder = ReplyKeyboardBuilder()
    builder.row(ad_types.KeyboardButton(text="🛍️ Bozor"), ad_types.KeyboardButton(text="❓ Yordam"))
    builder.row(ad_types.KeyboardButton(text="💬 Isbotlar"), ad_types.KeyboardButton(text="🛍 Mening xaridlarim"))
    builder.row(
        ad_types.KeyboardButton(text="❤️ Tanishuv Sayt"),
        ad_types.KeyboardButton(text="🏆 Haftalik reyting")
    )

    # Sening yangi ADMIN_IDS ro'yxating uchun tekshiruv
    if user_id in ADMIN_IDS:
        builder.row(ad_types.KeyboardButton(text="⚡ Admin Panel"))
    return builder.as_markup(resize_keyboard=True)


def get_user_inline_keyboard():
    builder = InlineKeyboardBuilder()
    if TANISHUV_SAYT_URL is None:
        builder.row(
            ad_types.InlineKeyboardButton(text="❤️ Tanishuv Sayt", callback_data="tanishuv_yopiq"),
        )
    else:
        builder.row(
            ad_types.InlineKeyboardButton(text="❤️ Tanishuv Sayt", web_app=ad_types.WebAppInfo(url=TANISHUV_SAYT_URL)),
        )
    return builder.as_markup()


def get_admin_inline_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        ad_types.InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats"),
        ad_types.InlineKeyboardButton(text="📢 Xabar Yuborish", callback_data="admin_send_ad")
    )
    builder.row(ad_types.InlineKeyboardButton(text="➕ Raqam Qo'shish", callback_data="admin_add_number"))
    builder.row(ad_types.InlineKeyboardButton(text="📋 Raqamlar Ro'yxati", callback_data="admin_list_numbers"))
    builder.row(ad_types.InlineKeyboardButton(text="💰 Sotilgan raqamlar soni", callback_data="admin_sold_count"))
    builder.row(ad_types.InlineKeyboardButton(text="🛡 Anti-Ban", callback_data="admin_anti_ban"))
    builder.row(ad_types.InlineKeyboardButton(text="⏸ Bot Stop", callback_data="admin_bot_stop"))
    return builder.as_markup()


# MANA SHU YERGA (157-QATOR ATROFIGA) IKKALA YANGI FUNKSIYANI JOYLASHTIRASAN:
async def send_night_message():
    if is_bot_stopped():
        return
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users") 
    all_users = cursor.fetchall()
    conn.close()

    matn = "🌃 Hayrli tun yaxshi dam olinglar tuningiz osuda o'tisin\n\n\"Hurmat bilan (FastGlobalUz) jamoasi\" ✅"
    
    for user in all_users:
        try:
            await bot.send_message(chat_id=user[0], text=matn)
        except Exception:
            pass

async def send_morning_message():
    if is_bot_stopped():
        return
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    all_users = cursor.fetchall()
    conn.close()

    matn = "🌇 Assalomu alekum yaxshi dam oldingizlarmi kuningiz a'lo o'tsin\n\n\"Hurmat bilan (FastGlobalUz) jamoasi\" ✅"
    
    for user in all_users:
        try:
            await bot.send_message(chat_id=user[0], text=matn)
        except Exception:
            pass


# ==================== HANDLERLAR ====================
@dp.message(CommandStart())
async def start_command(message: ad_types.Message):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    user_id = message.from_user.id
    is_subscribed = await check_sub_status(user_id)
    
    if not is_subscribed:
        builder = InlineKeyboardBuilder()
        for channel in CHANNELS:
            builder.row(ad_types.InlineKeyboardButton(text=channel["name"], url=channel["url"]))
        bot_info = await bot.get_me()
        builder.row(ad_types.InlineKeyboardButton(text="✅ A'zolikni tekshirish", url=f"https://t.me/{bot_info.username}?start=true"))
        
        await message.answer(
            "❌ <b>Botdan foydalanish uchun homiy kanallarga a'zo bo'lishingiz majburiy!</b>\n\n"
            "Iltimos, pastdagi kanallarga ulaning va so'ng A'zolikni tekshirish tugmasini bosing.", 
            parse_mode="HTML", reply_markup=builder.as_markup()
        )
        return

    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    await message.answer(f"👋 Salom, {message.from_user.first_name}!\n⚡ Menyuni tanlang:", reply_markup=get_main_menu(user_id))

@dp.message(F.text == "💬 Isbotlar")
async def isbotlar_handler(message: ad_types.Message):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if not await check_sub_status(message.from_user.id):
        builder = InlineKeyboardBuilder()
        await message.answer(
            "⚠️ Botdan foydalanish uchun avval kanalimizga obuna bo'ling!", 
            reply_markup=builder.as_markup()
        )
        return
    builder = InlineKeyboardBuilder()
    builder.row(ad_types.InlineKeyboardButton(text="🔥 Isbotlar Kanali", url=f"https://t.me/{FEEDBACK_CHANNEL.replace('@', '')}"))
    await message.answer("✅ Mijozlarimiz tomonidan qoldirilgan sharhlar va isbotlar:", reply_markup=builder.as_markup())

@dp.message(F.text == "❓ Yordam")
async def help_handler(message: ad_types.Message):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if not await check_sub_status(message.from_user.id):
        builder = InlineKeyboardBuilder()
        await message.answer(
            "⚠️ Botdan foydalanish uchun avval kanalimizga obuna bo'ling!", 
            reply_markup=builder.as_markup()
        )
        return
    builder = InlineKeyboardBuilder()
    builder.row(ad_types.InlineKeyboardButton(text="👤 Admin", url=f"https://t.me/{ADMIN_USERNAME}"))
    await message.answer("🛠️ Yordam markazi. Adminga yozishingiz mumkin:", reply_markup=builder.as_markup())


@dp.message(F.text == "⚡ Admin Panel")
async def admin_panel(message: ad_types.Message):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    # Sening yangi ADMIN_IDS ro'yxating uchun tekshiruv
    if message.from_user.id in ADMIN_IDS:
        try:
            auto_clean_sold_numbers()
        except Exception:
            pass
            
        # MANA BU YERDA get_admin_inline_keyboard() FUNKSIYASINI ULADIK!
        await message.answer(
            text="<b>💻 ADMIN PANEL</b>\n\nBoshqarish uchun quyidagi tugmalardan birini tanlang:", 
            parse_mode="HTML",
            reply_markup=get_admin_inline_keyboard() # Tugmalar endi chiqadi!
        )
    else:
        await message.answer("❌ Bu bo'lim faqat adminlar uchun!")

@dp.message(F.text == "🛍 Mening xaridlarim")
async def show_my_purchases(message: ad_types.Message):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if not await check_sub_status(message.from_user.id):
        builder = InlineKeyboardBuilder()
        await message.answer(
            "⚠️ Botdan foydalanish uchun avval kanalimizga obuna bo'ling!", 
            reply_markup=builder.as_markup()
        )
        return
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    # Foydalanuvchi sotib olgan raqamlarni olamiz
    cursor.execute("SELECT name, phone FROM numbers WHERE user_id = ? AND status='sold'", (message.from_user.id,))
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        await message.answer("❌ Sizda hali sotib olingan raqamlar yo'q.")
        return
        
    text = f"🛍 <b>Sizning sotib olgan raqamlaringiz soni:</b> {len(rows)} ta\n\n<b>Ro'yxat:</b>\n"
    for i, row in enumerate(rows, 1):
        text += f"{i:02}. {row[0]} - <code>{row[1]}</code>\n"
        
    await message.answer(text, parse_mode="HTML")        

# ==================== ULTRA DEMONTAJ TIZIMI (TELETHON) ====================
active_clients = {}

@dp.callback_query(F.data == "admin_add_number")
async def admin_add_number_start(callback: ad_types.CallbackQuery, state: FSMContext):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_phone)
    await callback.message.answer("📞 Iltimos, Telegram raqamni kiriting (Masalan: +998901234567):")

@dp.message(AdminStates.waiting_for_phone)
async def process_admin_phone(message: ad_types.Message, state: FSMContext):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if message.from_user.id not in ADMIN_IDS: return
    phone = message.text.strip()
    if not phone.startswith("+"):
        await message.answer("❌ Xato! Qaytadan '+' bilan kiriting:")
        return
        
    await message.answer("⏳ Telegram serveriga ulanmoqda...")
    session_name = f"session_{phone.replace('+', '')}"
    client = TelegramClient(session_name, API_ID, API_HASH)
    await client.connect()
    
    try:
        send_code_req = await client.send_code_request(phone)
        active_clients[message.from_user.id] = {
            "client": client, "phone": phone,
            "phone_code_hash": send_code_req.phone_code_hash,
            "session_file": session_name, "has_2fa": False
        }
        await state.set_state(AdminStates.waiting_for_code)
        await message.answer("📩 Kod yuborildi! Kod ichiga nuqta (.) qo'yib yuboring (Masalan: <code>12.345</code>):", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Xato: {str(e)}")
        await client.disconnect()
        await state.clear()

@dp.message(AdminStates.waiting_for_code)
async def process_admin_code(message: ad_types.Message, state: FSMContext):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if message.from_user.id not in ADMIN_IDS: return
    raw_code = message.text.strip()
    if "." not in raw_code:
        await message.answer("⚠️ Havfsizlik qoidasi: Kod ichida nuqta bo'lishi shart. Qayta kiriting:")
        return
        
    tg_code = raw_code.replace(".", "")
    client_data = active_clients.get(message.from_user.id)
    client = client_data["client"]
    
    try:
        await client.sign_in(phone=client_data["phone"], code=tg_code, phone_code_hash=client_data["phone_code_hash"])
        await proceed_to_demontaj(message, state, client, client_data)
    except SessionPasswordNeededError:
        client_data["has_2fa"] = True
        await state.set_state(AdminStates.waiting_for_2fa)
        await message.answer("🔒 Akkauntda eski 2FA parol bor. Uni o'zgartirishimiz uchun parolni kiriting:")
    except PhoneCodeInvalidError:
        await message.answer("❌ Kod xato! Qayta kiriting:")
    except Exception as e:
        await message.answer(f"❌ Xato: {str(e)}")
        await state.clear()

@dp.message(AdminStates.waiting_for_2fa)
async def process_admin_2fa(message: ad_types.Message, state: FSMContext):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if message.from_user.id not in ADMIN_IDS: return
    old_password_2fa = message.text.strip()
    client_data = active_clients.get(message.from_user.id)
    client = client_data["client"]
    
    try:
        await client.sign_in(password=old_password_2fa)
        client_data["old_2fa_pass"] = old_password_2fa
        await proceed_to_demontaj(message, state, client, client_data)
    except PasswordHashInvalidError:
        await message.answer("❌ 2FA parol noto'g'ri. Qayta kiriting:")
    except Exception as e:
        await message.answer(f"❌ Xato: {str(e)}")

async def proceed_to_demontaj(message: ad_types.Message, state: FSMContext, client: TelegramClient, client_data: dict):
    progress_msg = await message.answer("⚙️ <b>Ultra Demontaj boshlandi: 0%</b>", parse_mode="HTML")
    
    try:
        await client(functions.account.UpdateProfileRequest(first_name="FastGlobalUz", last_name="", about=""))
    except Exception as e: logging.error(f"Ism/Bio xatosi: {e}")
    await progress_msg.edit_text("⚙️ <b>Demontaj: 20%</b>", parse_mode="HTML")
    
    try:
        photos = await client.get_profile_photos('me')
        if photos:
            await client(DeletePhotosRequest(id=[tl.types.InputPhoto(id=p.id, access_hash=p.access_hash) for p in photos]))
    except Exception as e: logging.error(f"Rasm o'chirish xatosi: {e}")
    await progress_msg.edit_text("⚙️ <b>Demontaj: 40%</b>", parse_mode="HTML")

    try:
        await client(functions.account.UpdateUsernameRequest(username=""))
    except Exception as e: logging.error(f"Username o'chirish xatosi: {e}")
    await progress_msg.edit_text("⚙️ <b>Demontaj: 50%</b>", parse_mode="HTML")

    try:
        rules = [tl.types.InputPrivacyValueAllowNone()]
        await client(functions.account.SetPrivacyRequest(key=tl.types.InputPrivacyKeyPhoneNumber(), rules=rules))
        await client(functions.account.SetPrivacyRequest(key=tl.types.InputPrivacyKeyStatusTimestamp(), rules=rules))
        await client(functions.account.SetPrivacyRequest(key=tl.types.InputPrivacyKeyPhoneCall(), rules=rules))
        await client(functions.account.SetPrivacyRequest(key=tl.types.InputPrivacyKeyProfilePhoto(), rules=rules))
    except Exception as e: logging.error(f"Xavfsizlik xatosi: {e}")
    await progress_msg.edit_text("⚙️ <b>Demontaj: 70%</b>", parse_mode="HTML")
    
    try:
        if client_data.get("has_2fa") and "old_2fa_pass" in client_data:
            await client.edit_2fa(current_password=client_data["old_2fa_pass"], new_password="FastGlobalUz", hint="###")
        else:
            await client.edit_2fa(new_password="FastGlobalUz", hint="###")
    except Exception as e: logging.error(f"2FA xatosi: {e}")
    await progress_msg.edit_text("⚙️ <b>Demontaj: 90%</b>", parse_mode="HTML")
    
    async for dialog in client.iter_dialogs():
        try:
            if dialog.is_user: await client.delete_dialog(dialog.id)
            elif dialog.is_group or dialog.is_channel: await client(LeaveChannelRequest(channel=dialog.entity))
        except Exception: continue
            
    await progress_msg.edit_text("✅ <b>Demontaj 100% yakunlandi!</b>", parse_mode="HTML")
    
    async def sms_code_listener(phone_num, cl):
        for _ in range(120):
            try:
                if not cl.is_connected(): await cl.connect()
                async for msg in cl.iter_messages(777000, limit=5):
                    if msg.text:
                        match = re.search(r'\b\d{5}\b', msg.text)
                        if match:
                            raw_code = match.group(0)
                            formatted_code = raw_code[:2] + "." + raw_code[2:]
                            update_received_code(phone_num, formatted_code)
                            break
            except Exception: pass
            await asyncio.sleep(5)
        await cl.disconnect()

    asyncio.create_task(sms_code_listener(client_data["phone"], client))
    
    await state.set_state(AdminStates.waiting_for_name)
    await message.answer("📝 Endi ushbu Raqam uchun biron NOM kiriting:")

@dp.message(AdminStates.waiting_for_name)
async def process_num_name(message: ad_types.Message, state: FSMContext):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if message.from_user.id not in ADMIN_IDS: return
    await state.update_data(num_name=message.text.strip())
    await state.set_state(AdminStates.waiting_for_price)
    await message.answer("💰 Raqam uchun NARX kiriting (Masalan: 50 000 so'm):")

@dp.message(AdminStates.waiting_for_price)
async def process_num_price(message: ad_types.Message, state: FSMContext):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if message.from_user.id not in ADMIN_IDS: return
    price = message.text.strip()
    data = await state.get_data()
    name = data.get("num_name")
    client_data = active_clients.get(message.from_user.id)
    phone = client_data["phone"]
    
    add_number_to_db(phone, name, price, "FastGlobalUz", client_data["session_file"])
    
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM numbers WHERE phone=?", (phone,))
    num_id = cursor.fetchone()[0]
    conn.close()
    
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.row(ad_types.InlineKeyboardButton(text="✅ Kanalga Chiqarish", callback_data=f"confirm_post_{num_id}"))
    
    hidden_phone = phone[:6] + "***" + phone[-3:]
    await message.answer(
        f"📋 <b>Raqam bazaga qo'shildi:</b>\n\n🏷️ Nomi: {name}\n📞 Raqam: <code>{hidden_phone}</code>\n🔐 2FA: <code>FastGlobalUz</code>\n💵 Narxi: {price}",
        parse_mode="HTML", reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("confirm_post_"))
async def channel_posting(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    num_id = int(callback.data.split("_")[2])
    
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, name, price FROM numbers WHERE id=?", (num_id,))
    res = cursor.fetchone()
    conn.close()
    if not res: return
    
    phone, name, price = res
    hidden_phone = phone[:6] + "***" + phone[-3:]
    bot_info = await bot.get_me()
    builder = InlineKeyboardBuilder()
    builder.row(ad_types.InlineKeyboardButton(text="🤖 Botga Kirish", url=f"https://t.me/{bot_info.username}"))
    
    try:
        await bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=f"📢 <b>Yangi Raqam Keldi!</b>\n\n🏷️ Nomi: {name}\n📞 Raqam: <code>{hidden_phone}</code>\n💵 Narxi: {price}",
            parse_mode="HTML", reply_markup=builder.as_markup()
        )
        await callback.message.edit_text("✨ Muvaffaqiyatli kanalga joylandi!")
    except Exception as e:
        await callback.message.edit_text(f"❌ Kanalga yuborishda xato: {e}")

# ==================== BOZOR MANTIG'I ====================
@dp.message(F.text == "🛍️ Bozor")
async def market_handler(message: ad_types.Message):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if not await check_sub_status(message.from_user.id):
        builder = InlineKeyboardBuilder()
        await message.answer(
            "⚠️ Botdan foydalanish uchun avval kanalimizga obuna bo'ling!", 
            reply_markup=builder.as_markup()
        )
        return
    
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM numbers WHERE status='available'")
    available_nums = cursor.fetchall()
    conn.close()
    
    if not available_nums:
        await message.answer("🛒 Hozirda sotuvda bo'sh raqamlar mavjud emas.")
        return
        
    builder = InlineKeyboardBuilder()
    for num_id, name in available_nums:
        builder.row(ad_types.InlineKeyboardButton(text=f"📱 {name}", callback_data=f"buy_num_{num_id}"))
    await message.answer("🛒 Raqamlar Ro'yxati:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("buy_num_"))
async def process_buy_selection(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    num_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, name, price, status FROM numbers WHERE id=?", (num_id,))
    res = cursor.fetchone()
    conn.close()
    
    if not res or res[3] != 'available':
        await callback.answer("❌ Bu raqam hozirgina band qilindi!", show_alert=True)
        return
        
    phone, name, price, _ = res
    hidden_phone = phone[:6] + "***" + phone[-3:]
    builder = InlineKeyboardBuilder()
    builder.row(ad_types.InlineKeyboardButton(text="🛍️ Sotib Olish", callback_data=f"checkout_{num_id}"))
    
    await callback.message.answer(f"🏷️ Nomi: {name}\n📞 Raqam: <code>{hidden_phone}</code>\n💵 Narxi: {price}", parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("checkout_"))
async def process_checkout(callback: ad_types.CallbackQuery, state: FSMContext):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    num_id = int(callback.data.split("_")[1])
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT price, status FROM numbers WHERE id=?", (num_id,))
    res = cursor.fetchone()
    
    if not res or res[1] != 'available':
        conn.close()
        await callback.answer("❌ Bu raqam band qilingan!", show_alert=True)
        return
        
    cursor.execute("UPDATE numbers SET status='pending' WHERE id=?", (num_id,))
    conn.commit()
    price = res[0]
    conn.close()
    
    await state.set_state(UserStates.waiting_for_receipt)
    await state.update_data(buy_num_id=num_id)
    
    await callback.message.answer(
        f"💳 <b>Karta:</b> <code>{CARD_NUMBER}</code>\n👤 <b>F.I.O:</b> {CARD_HOLDER}\n💰 <b>Narxi:</b> <code>{price}</code>\n\nChek skrinshotini yuboring:",
        parse_mode="HTML"
    )

@dp.message(UserStates.waiting_for_receipt, F.photo)
async def handle_receipt_photo(message: ad_types.Message, state: FSMContext):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    data = await state.get_data()
    num_id = data.get("buy_num_id")
    
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT price, name FROM numbers WHERE id=?", (num_id,))
    price, name = cursor.fetchone()
    conn.close()
    
    await state.clear()
    await message.answer("🔄 <b>Ushbu O'tkazma Adminga yuborildi ko'rib chiqib javob beriladi !</b>", parse_mode="HTML")
    
    # 🌟 ADMIN BUYRUTMA INLINE TUGMALARI (Mijoz tugmasi tepaga qo'shildi)
    admin_builder = InlineKeyboardBuilder()
    admin_builder.row(ad_types.InlineKeyboardButton(text="👤 Mijoz", url=f"tg://user?id={message.from_user.id}"))
    admin_builder.row(
        ad_types.InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"adm_confirm_{num_id}_{message.from_user.id}"),
        ad_types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"adm_cancel_{num_id}_{message.from_user.id}")
    )
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                chat_id=admin_id, photo=message.photo[-1].file_id,
                caption=f"💰 <b>Yangi to'lov cheki!</b>\n\n💵 Narxi: <code>{price}</code>\n📦 Mahsulot: {name}",
                parse_mode="HTML", reply_markup=admin_builder.as_markup()
            )
        except Exception:
            pass

@dp.callback_query(F.data.startswith("adm_cancel_"))
async def admin_cancel_order(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    _, _, num_id, user_id = callback.data.split("_")
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE numbers SET status='available' WHERE id=?", (int(num_id),))
    conn.commit()
    conn.close()
    
    await callback.message.answer("❌ Buyruq bekor qilindi, raqam bozorga qaytdi.")
    try: await bot.send_message(chat_id=int(user_id), text="❌ Sizning buyurtmangiz rad etildi.")
    except Exception: pass
    await callback.message.delete()

@dp.callback_query(F.data.startswith("adm_confirm_"))
async def admin_confirm_order(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    # Callbackdan user_id ni ajratib olamiz
    _, _, num_id, user_id = callback.data.split("_")
    num_id = int(num_id)
    
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    # user_id ni bazaga yozamiz
    cursor.execute("UPDATE numbers SET status='sold', sold_at=?, user_id=? WHERE id=?", (now_str, user_id, num_id))
    conn.commit()
    conn.close()

    record_weekly_purchase(user_id)
    
    # ... qolgan qismi (xabar yuborish) o'zgarishsiz qolaveradi
    
    user_builder = InlineKeyboardBuilder()
    user_builder.row(ad_types.InlineKeyboardButton(text="📱 Raqamni Olish", callback_data=f"get_my_num_{num_id}"))
    
    try:
        await bot.send_message(chat_id=int(user_id), text="🎉 <b>To'lovingiz tasdiqlandi!</b>\n\nPastdagi tugmani bosing:", parse_mode="HTML", reply_markup=user_builder.as_markup())
        await callback.message.edit_caption(caption="✅ Buyurtma muvaffaqiyatli tasdiqlandi!")
    except Exception: pass

@dp.callback_query(F.data.startswith("get_my_num_"))
async def user_claim_number(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    num_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM numbers WHERE id=?", (num_id,))
    phone = cursor.fetchone()[0]
    conn.close()
    
    builder = InlineKeyboardBuilder()
    builder.row(ad_types.InlineKeyboardButton(text="📩 Kod olish", callback_data=f"get_code_{num_id}"))
    builder.row(ad_types.InlineKeyboardButton(text="🔐 2-Bosqich", callback_data=f"get_2fa_{num_id}"))
    builder.row(ad_types.InlineKeyboardButton(text="🤝 Rahmat", callback_data=f"say_thanks_{num_id}"))
    
    await callback.message.answer(f"📱 Raqamingiz: <code>{phone}</code>\nUstiga bosib nusxa oling.", parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("get_code_"))
async def user_get_code(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    num_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT received_code FROM numbers WHERE id=?", (num_id,))
    res = cursor.fetchone()
    conn.close()
    
    current_code = res[0] if res else "kutilmoqda"
    if current_code == "kutilmoqda":
        await callback.answer("⏳ Kirish kodi kutilmoqda...", show_alert=True)
    else:
        await callback.message.answer(f"📩 Kod (Nusxa olish): <code>{current_code}</code>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("get_2fa_"))
async def user_get_2fa(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    await callback.answer()
    await callback.message.answer("🔐 2-Bosqich paroli: <code>FastGlobalUz</code>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("say_thanks_"))
async def user_finish_deal(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    num_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, name, price FROM numbers WHERE id=?", (num_id,))
    phone, name, price = cursor.fetchone()
    conn.close()
    
    hidden_phone = phone[:6] + "***" + phone[-3:]
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=f"💰 <b>Raqam sotildi !</b>\n\nSotilgan raqam: {phone}", parse_mode="HTML")
        except Exception:
            pass
    
    bot_info = await bot.get_me()
    chan_builder = InlineKeyboardBuilder()
    chan_builder.row(ad_types.InlineKeyboardButton(text="🤖 Botga Kirish", url=f"https://t.me/{bot_info.username}"))
    
    try:
        await bot.send_message(
            chat_id=FEEDBACK_CHANNEL,
            text=f"🤝 <b>Yana bir Raqam sotildi</b>\n\n📦 Nomi: {name}\n📱 Raqam: <code>{hidden_phone}</code>\n💰 Narxi: <code>{price}</code>",
            parse_mode="HTML", reply_markup=chan_builder.as_markup()
        )
    except Exception: pass
    await callback.message.delete()
    await callback.message.answer("🚀 Xaridingiz uchun rahmat!")


# ==================== 📋 AQLLI RAQAMLAR RO'YXATI (30 SOAT SAQLASH) ====================
@dp.callback_query(F.data == "admin_list_numbers")
async def admin_list_numbers_handler(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.answer()
    auto_clean_sold_numbers()
    
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, status, sold_at FROM numbers")
    all_nums = cursor.fetchall()
    conn.close()
    
    builder = InlineKeyboardBuilder()
    count = 0
    
    for num_id, name, status, sold_at_str in all_nums:
        if status == 'sold' and sold_at_str:
            try:
                sold_time = datetime.strptime(sold_at_str, "%Y-%m-%d %H:%M:%S")
                if datetime.now() - sold_time > timedelta(hours=30): continue
                status_tag = "❌ (Sotilgan)"
            except Exception: continue
        elif status == 'pending': status_tag = "⏳ (Kutilmoqda)"
        else: status_tag = "📱 (Sotuvda)"
            
        builder.row(ad_types.InlineKeyboardButton(text=f"{name} {status_tag}", callback_data=f"adm_view_num_{num_id}"))
        count += 1
        
    if count == 0:
        await callback.message.answer("📋 Hozirda ro'yxatda hech qanday raqam mavjud emas.")
        return
        
    builder.row(ad_types.InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_admin_panel"))
    await callback.message.answer("📋 Raqamlar ro'yxati:\n(Sotilgan raqamlar ro'yxatda 30 soat davomida saqlanadi)", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("adm_view_num_"))
async def admin_view_single_number(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.answer()
    num_id = int(callback.data.split("_")[3])
    
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, name, price, password_2fa FROM numbers WHERE id=?", (num_id,))
    res = cursor.fetchone()
    conn.close()
    if not res: return
    
    phone, name, price, password_2fa = res
    info_text = (
        f"📱 <b>Raqamlar ro'yxati:</b>\n\n"
        f"🏷️ {name}\n"
        f"📞 To'liq Raqam: <code>{phone}</code>\n"
        f"🔐 2FA Paroli: <code>{password_2fa}</code>\n"
        f"💵 Raqam narxi: {price}\n\n"
        f"<i>💡 Ma'lumotlarni ustiga bossangiz avtomatik nusxa oladi.</i>"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(ad_types.InlineKeyboardButton(text="🌐 Ulab Olish", callback_data=f"adm_connect_{num_id}"))
    builder.row(ad_types.InlineKeyboardButton(text="🗑️ O'chirib tashlash", callback_data=f"adm_delete_num_{num_id}"))
    builder.row(ad_types.InlineKeyboardButton(text="⬅️ Ro'yxatga qaytish", callback_data="admin_list_numbers"))
    await callback.message.answer(info_text, parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("adm_connect_"))
async def admin_connect_number_flow(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.answer()
    num_id = int(callback.data.split("_")[2])
    
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM numbers WHERE id=?", (num_id,))
    phone = cursor.fetchone()[0]
    conn.close()
    
    builder = InlineKeyboardBuilder()
    builder.row(ad_types.InlineKeyboardButton(text="📩 SMS olish", callback_data=f"adm_get_sms_{num_id}"))
    builder.row(ad_types.InlineKeyboardButton(text="🔐 2FA olish", callback_data=f"adm_get_2fa_{num_id}"))
    builder.row(ad_types.InlineKeyboardButton(text="✅ Kirdim", callback_data=f"adm_logged_in_{num_id}"))
    
    await callback.message.answer(f"🌐 <b>Ulab olish faol:</b>\n<code>{phone}</code>", parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("adm_get_sms_"))
async def admin_get_sms_callback(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    num_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT received_code FROM numbers WHERE id=?", (num_id,))
    res = cursor.fetchone()
    conn.close()
    
    current_code = res[0] if res else "kutilmoqda"
    if current_code == "kutilmoqda":
        await callback.answer("⏳ Kod hali kelmadi...", show_alert=True)
    else:
        await callback.message.answer(f"📩 SMS kod: <code>{current_code}</code>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_get_2fa_"))
async def admin_get_2fa_callback(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.answer()
    await callback.message.answer("🔐 2-Bosqich paroli: <code>FastGlobalUz</code>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_logged_in_"))
async def admin_logged_in_callback(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.answer()
    await callback.message.answer("Hayr sog' bo'lig !")

@dp.callback_query(F.data.startswith("adm_delete_num_"))
async def admin_delete_number_from_db(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    num_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM numbers WHERE id=?", (num_id,))
    conn.commit()
    conn.close()
    await callback.answer("🗑️ Raqam butunlay o'chirildi!", show_alert=True)
    await callback.message.edit_text("✅ Raqam o'chirildi.")

@dp.callback_query(F.data == "back_to_admin_panel")
async def back_to_admin_panel_callback(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.answer()
    await callback.message.edit_text("🖥️ <b>ADMIN PANELI</b>", parse_mode="HTML", reply_markup=get_admin_inline_keyboard())


# ==================== 📊 YANGILANGAN MUKAMMAL STATISTIKA ====================
@dp.callback_query(F.data == "admin_stats")
async def admin_stats_callback(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.answer("📊 Jonli hisob-kitob bajarilmoqda...")
    
    users = get_all_users()
    total_users = len(users)
    active_users = 0
    blocked_users = 0
    
    # Har bir foydalanuvchini faolligini tekshirish
    for u_id in users:
        try:
            await bot.send_chat_action(chat_id=u_id, action="typing")
            active_users += 1
        except Exception:
            blocked_users += 1
        await asyncio.sleep(0.01)  # Telegram limitiga tushmaslik uchun kichik kechikish
        
    stats_text = (
        f"📊 <b>BOT STATISTIKASI:</b>\n\n"
        f"👥 Jami Foydalanuvchilar: <code>{total_users}</code> ta\n"
        f"🟢 Aktiv odamlar soni: <code>{active_users}</code> ta\n"
        f"🔴 Botdan chiqib ketganlar (Bloklaganlar): <code>{blocked_users}</code> ta"
    )
    await callback.message.answer(stats_text, parse_mode="HTML")


# ==================== 📢 YANGILANGAN MUKAMMAL REKLAMA TIZIMI ====================
@dp.callback_query(F.data == "admin_send_ad")
async def admin_send_ad_callback(callback: ad_types.CallbackQuery, state: FSMContext):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_ad_content)
    await callback.message.answer("📢 Reklama xabarini (matn, rasm, video va h.k.) kiriting:")

@dp.message(AdminStates.waiting_for_ad_content)
async def process_ad_distribution(message: ad_types.Message, state: FSMContext):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    
    users = get_all_users()
    status_msg = await message.answer("⏳ Reklama tarqatilmoqda, iltimos kuting...")
    
    success_count = 0
    failed_count = 0
    
    for u_id in users:
        try:
            await message.copy_to(chat_id=u_id)
            success_count += 1
        except Exception:
            failed_count += 1
        await asyncio.sleep(0.03)  # Bot block bo'lib qolmasligi uchun optimal vaqt
        
    await status_msg.delete()
    
    report_text = (
        f"📢 <b>Reklama hisoboti muvaffaqiyatli yakunlandi:</b>\n\n"
        f"✅ Jami Yuborilgan odamlar soni: <code>{success_count}</code> ta\n"
        f"❌ Yuborilmagan odamlar soni: <code>{failed_count}</code> ta"
    )
    await message.answer(report_text, parse_mode="HTML")
# ==================== Sotilgan Raqamlar Soni ====================    
@dp.callback_query(F.data == "admin_sold_count")
async def admin_sold_count_callback(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if callback.from_user.id not in ADMIN_IDS:
        return
        
    await callback.answer()
    
    # Ma'lumotlar bazasiga ulanamiz
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    
    # Agar bazangda sotilgan raqamlar status='sold' deb yozilsa shunday qoladi.
    # Agar boshqacha so'z bo'lsa (masalan 'sotildi'), 'sold' o'rniga o'sha so'zni yoz.
    cursor.execute("SELECT COUNT(*) FROM numbers WHERE status = 'sold'")
    sotilgan_soni = cursor.fetchone()[0]
    conn.close()
    
    # Adminga ko'rinadigan matn
    matn = (
        "📊 <b>Sotuv statistikasi</b>\n\n"
        f"💰 Hozirgi vaqtgacha jami <code>{sotilgan_soni} ta</code> raqam muvaffaqiyatli sotildi."
    )
    
    await callback.message.edit_text(text=matn, parse_mode="HTML")
# ==================== Tanishuv Sayt ==================== 
# ... (Bu yerda sendagi eng oxirgi reklama tarqatish for sikli tugagan bo'ladi)
    # for u_id in users:
    #     ...


# FAYLNING ENG OXIRIGA KELIB, MANA SHU KODNI O'ZING QO'SHASAN:
# 1. Foydalanuvchi oddiy menyudagi tugmani bosganda inline tugmani yuborish
@dp.message(F.text == "❤️ Tanishuv Sayt")
async def tanishuv_sayt_handler(message: ad_types.Message):
    if get_user_status(message.from_user.id) == 'banned':
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if not await check_sub_status(message.from_user.id):
        builder = InlineKeyboardBuilder()
        await message.answer(
            "⚠️ Botdan foydalanish uchun avval kanalimizga obuna bo'ling!", 
            reply_markup=builder.as_markup()
        )
        return
    await message.answer(
        text="🌐 Tanishuv saytiga kirish uchun pastdagi tugmani bosing:",
        reply_markup=get_user_inline_keyboard() # Biz yaratgan inline tugma shu yerda chiqadi
    )

# 2. Agar silka None bo'lsa, o'sha inline tugma bosilganda "Vaqtincha yopiq!" deb javob berish
@dp.callback_query(F.data == "tanishuv_yopiq")
async def tanishuv_yopiq_callback(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    # Telegram tugmasidagi yuklanish (soat) aylanishini to'xtatamiz
    await callback.answer()
    
    # Foydalanuvchiga xabar ko'rinishida yuboramiz
    await callback.message.answer(text="❌ Vaqtincha yopiq!")


@dp.message(F.text == "🏆 Haftalik reyting")
async def haftalik_reyting_message(message: ad_types.Message):
    if not await check_sub_status(message.from_user.id):
        builder = InlineKeyboardBuilder()
        await message.answer(
            "⚠️ Botdan foydalanish uchun avval kanalimizga obuna bo'ling!", 
            reply_markup=builder.as_markup()
        )
        return

        await check_and_reset_weekly_rating()
        await message.answer(build_weekly_rating_text())
        await message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    if not await check_sub_status(message.from_user.id): return
    await check_and_reset_weekly_rating()
    await message.answer(build_weekly_rating_text())


@dp.callback_query(F.data == "haftalik_reyting")
async def haftalik_reyting_callback(callback: ad_types.CallbackQuery):
    if get_user_status(callback.from_user.id) == 'banned':
        if callback.message:
            await callback.message.answer("🚫 Siz adminlar tomonidan bloklandingiz.")
        return
    await callback.answer()
    await check_and_reset_weekly_rating()
    if callback.message:
        await callback.message.answer(build_weekly_rating_text())


# ==================== BOT STOP TIZIMI ====================
@dp.callback_query(F.data == "admin_bot_stop")
async def admin_bot_stop_menu(callback: ad_types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.answer()
    status_text = "🔴 TO'XTATILGAN" if is_bot_stopped() else "🟢 FAOL"
    await callback.message.answer(
        "⏸ <b>Bot Stop</b>\n\n"
        "Ushbu funksiya botni ish faoliyatini vaqtincha to'xtatadi.\n\n"
        f"Hozirgi holat: <b>{status_text}</b>\n\n"
        "✅ <b>Yoqish</b> — bot oddiy foydalanuvchilar uchun o'chadi (adminlar ishlaydi)\n"
        "⛔ <b>O'chirish</b> — bot barcha uchun qayta yoqiladi va xabar yuboriladi",
        parse_mode="HTML",
        reply_markup=get_bot_stop_keyboard()
    )


@dp.callback_query(F.data == "bot_stop_enable")
async def bot_stop_enable_handler(callback: ad_types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.answer()
    set_bot_status('stopped')
    await callback.message.answer(
        "✅ Funksiya yoqildi.\n\n"
        "Bot oddiy foydalanuvchilar uchun vaqtincha to'xtatildi. Adminlar ishlashda davom etadi."
    )


@dp.callback_query(F.data == "bot_stop_disable")
async def bot_stop_disable_handler(callback: ad_types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.answer()
    set_bot_status('active')

    users = get_all_users()
    success_count = 0
    failed_count = 0

    for u_id in users:
        if u_id in ADMIN_IDS:
            continue
        try:
            await bot.send_message(chat_id=u_id, text=BOT_RESUME_MESSAGE)
            success_count += 1
        except Exception:
            failed_count += 1
        await asyncio.sleep(0.03)

    await callback.message.answer(
        "✅ Bot qayta yoqildi! Barcha foydalanuvchilar uchun ishlayapti.\n\n"
        f"📨 Xabar yuborildi: <code>{success_count}</code> ta\n"
        f"❌ Yuborilmadi: <code>{failed_count}</code> ta",
        parse_mode="HTML",
        reply_markup=get_admin_inline_keyboard()
    )


# ==================== ANTI-BAN TIZIMI ====================
@dp.callback_query(F.data == "admin_anti_ban")
async def ask_for_ban_id(callback: ad_types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_ban_id)
    await callback.message.answer("🆔 Foydalanuvchi ID sini yuboring:")

@dp.message(AdminStates.waiting_for_ban_id)
async def process_ban_id(message: ad_types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID. Faqat raqam kiriting:")
        return
    status = get_user_status(user_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        ad_types.InlineKeyboardButton(text="🔓 Open", callback_data=f"status_Bot siz uchun faol 😁_{user_id}"),
        ad_types.InlineKeyboardButton(text="🔒 Close", callback_data=f"status_Botdan bloklangansiz 😔_{user_id}")
    )
    await message.answer(f"👤 Mijoz: {user_id}\nStatus: {status}", reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data.startswith("status_"))
async def update_status(callback: ad_types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    _, action, user_id = callback.data.split("_")
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, status) VALUES (?, ?)", (int(user_id), action))
    cursor.execute("UPDATE users SET status = ? WHERE user_id = ?", (action, int(user_id)))
    conn.commit()
    conn.close()
    await callback.answer(f"✅ Status: {action}")
    try:
        await bot.send_message(int(user_id), f"❗Sizning holatingiz: {action}")
    except Exception:
        pass


# ==================== RUN ====================
async def main():
    print("[+] Bot muvaffaqiyatli ishga tushdi !")
    await bot.delete_webhook(drop_pending_updates=True)
    # ... Senda bu yerda eski kodlaring bor (masalan: await bot.delete_webhook) ...
    # ... O'sha kodlaring o'z joyida turaversin ...

    # MANA SHU YERGA (start_polling DAN TEPAGA) SCHEDULER KODLARINI QO'SHASAN:
    # Schedulerni Toshkent vaqti bilan yaratamiz
    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")

    # Har kuni soat 21:00 da tungi xabarni yuborish
    scheduler.add_job(send_night_message, CronTrigger(hour=21, minute=0))

    # Har kuni soat 08:00 da ertalabgi xabarni yuborish
    scheduler.add_job(send_morning_message, CronTrigger(hour=8, minute=0))

    # Har soat haftalik reyting 7 kun tekshiruvi
    scheduler.add_job(check_and_reset_weekly_rating, CronTrigger(minute=0))

    # Schedulerni fonda ishga tushiramiz
    scheduler.start()

    # BOTNI ISHGA TUSHIRISH (Bu qator senda tayyor bor, unga tegmaysan, shunchaki eng tagida tursin):
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
