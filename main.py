import asyncio
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, PreCheckoutQuery
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram import BaseMiddleware

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "8595238605"))
DEVELOPER_ID = int(os.getenv("DEVELOPER_ID", "7041471070"))
DB_PATH = Path(os.getenv("DB_PATH", "data/bot.sqlite3"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "data/backups"))
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_SEND_TO_ADMINS = os.getenv("BACKUP_SEND_TO_ADMINS", "true").lower() == "true"
BOT_PUBLIC_NAME = os.getenv("BOT_PUBLIC_NAME", "Kino bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN ENV kiritilmagan.")

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("kino-bot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# =========================
# DATABASE
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT NOT NULL,
            vip_until TEXT,
            referrer INTEGER
        );
        CREATE TABLE IF NOT EXISTS titles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'serial',
            description TEXT DEFAULT '',
            poster_file_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS episodes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
            episode_no INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            caption TEXT DEFAULT '',
            access TEXT NOT NULL DEFAULT 'free',
            created_at TEXT NOT NULL,
            UNIQUE(title_id, episode_no)
        );
        CREATE TABLE IF NOT EXISTS mandatory_channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL UNIQUE,
            invite_url TEXT NOT NULL,
            title TEXT DEFAULT 'Kanal'
        );
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            plan_name TEXT NOT NULL,
            amount INTEGER NOT NULL,
            duration_days INTEGER NOT NULL,
            receipt_file_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vip_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            payment_id INTEGER,
            days INTEGER NOT NULL,
            granted_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        defaults = {
            "card_number": "KARTA_RAQAMINI_SOZLAMALARDAN_KIRITING",
            "card_owner": "Karta egasi",
            "plan_30_price": "8888",
            "plan_90_price": "20000",
            "plan_180_price": "35000",
            "plan_30_name": "1 oylik",
            "plan_90_name": "3 oylik",
            "plan_180_name": "6 oylik",
        }
        for k, v in defaults.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

def setting(key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with db() as con:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def log_action(actor, action, details=""):
    with db() as con:
        con.execute(
            "INSERT INTO audit_logs(actor_id,action,details,created_at) VALUES(?,?,?,?)",
            (actor, action, details, now_iso())
        )

def now_utc():
    return datetime.now(timezone.utc)

def now_iso():
    return now_utc().isoformat()

def is_admin(uid: int) -> bool:
    return uid in (OWNER_ID, DEVELOPER_ID)

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def get_user(uid):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def is_vip(uid):
    user = get_user(uid)
    if not user or not user["vip_until"]:
        return False
    try:
        return datetime.fromisoformat(user["vip_until"]) > now_utc()
    except ValueError:
        return False

def register_user(user, referrer=None):
    with db() as con:
        con.execute("""
        INSERT INTO users(user_id,username,first_name,joined_at,referrer)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
        """, (user.id, user.username or "", user.first_name or "", now_iso(), referrer))

def grant_vip(uid, days, granted_by, payment_id=None):
    user = get_user(uid)
    start = now_utc()
    if user and user["vip_until"]:
        try:
            old = datetime.fromisoformat(user["vip_until"])
            if old > start:
                start = old
        except ValueError:
            pass
    until = (start + timedelta(days=days)).isoformat()
    with db() as con:
        con.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until, uid))
        con.execute(
            "INSERT INTO vip_history(user_id,payment_id,days,granted_by,created_at) VALUES(?,?,?,?,?)",
            (uid, payment_id, days, granted_by, now_iso())
        )
    return until

# =========================
# KEYBOARDS
# =========================
def ikb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

def main_kb():
    return ikb([
        [InlineKeyboardButton(text="🎬 Kino qidirish", callback_data="search"),
         InlineKeyboardButton(text="📚 Mundarija", callback_data="catalog")],
        [InlineKeyboardButton(text="💎 VIP obuna", callback_data="vip"),
         InlineKeyboardButton(text="👤 Profilim", callback_data="profile")],
        [InlineKeyboardButton(text="ℹ️ Qo‘llanma", callback_data="help")]
    ])

def back_kb(dest="home"):
    return ikb([[InlineKeyboardButton(text="⬅️ Orqaga", callback_data=dest)]])

def admin_kb():
    return ikb([
        [InlineKeyboardButton(text="➕ Kino/serial qo‘shish", callback_data="adm_add_title")],
        [InlineKeyboardButton(text="➕ Qism qo‘shish", callback_data="adm_add_episode"),
         InlineKeyboardButton(text="🗑 O‘chirish", callback_data="adm_delete")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats"),
         InlineKeyboardButton(text="📣 Kanallar", callback_data="adm_channels")],
        [InlineKeyboardButton(text="💳 VIP sozlamalari", callback_data="adm_vip_settings")],
        [InlineKeyboardButton(text="📦 Zaxira yaratish", callback_data="adm_backup")]
    ])

# =========================
# SUBSCRIPTION CHECK
# =========================
async def missing_channels(uid):
    with db() as con:
        channels = con.execute("SELECT * FROM mandatory_channels ORDER BY id").fetchall()
    missing = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["chat_id"], uid)
            status = getattr(member, "status", "")
            if status in ("left", "kicked"):
                missing.append(ch)
            elif status == "restricted" and not getattr(member, "is_member", False):
                missing.append(ch)
        except Exception as e:
            log.warning("Kanal tekshiruvi xatosi %s: %s", ch["chat_id"], e)
            # Bot kanal admini bo'lmasa tekshiruv ishonchli bo'lmaydi.
            missing.append(ch)
    return missing

async def send_join_prompt(message_or_call, missing):
    rows = []
    for ch in missing:
        rows.append([InlineKeyboardButton(text=f"➕ {ch['title']}", url=ch["invite_url"])])
    rows.append([InlineKeyboardButton(text="🔄 Obunani tekshirish", callback_data="check_subs")])
    text = "Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling:"
    if isinstance(message_or_call, Message):
        await message_or_call.answer(text, reply_markup=ikb(rows))
    else:
        try:
            await message_or_call.message.edit_text(text, reply_markup=ikb(rows))
        except TelegramBadRequest:
            await message_or_call.message.answer(text, reply_markup=ikb(rows))
        await message_or_call.answer()

class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        uid = None
        if isinstance(event, Message) and event.from_user:
            uid = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            uid = event.from_user.id
        if not uid or is_admin(uid) or is_vip(uid):
            return await handler(event, data)
        # Allow start, subscription checker, and join-related callbacks.
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_subs":
            return await handler(event, data)
        missing = await missing_channels(uid)
        if missing:
            if isinstance(event, Message):
                await send_join_prompt(event, missing)
            else:
                await send_join_prompt(event, missing)
            return
        return await handler(event, data)

dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())

# =========================
# FSM
# =========================
class AddTitle(StatesGroup):
    title = State()
    kind = State()
    description = State()
    poster = State()
    first_episode = State()

class AddEpisodes(StatesGroup):
    choose_title = State()
    media = State()

class SearchState(StatesGroup):
    query = State()

class AddChannel(StatesGroup):
    chat_id = State()
    invite = State()
    title = State()

class SettingState(StatesGroup):
    value = State()

class DeleteState(StatesGroup):
    choose = State()

def extract_episode_no(text: str) -> Optional[int]:
    if not text:
        return None
    patterns = [
        r"(?:qism|episode|ep)\s*[:._\- ]\s*(\d+)",
        r"(\d+)\s*[-._ ]?\s*(?:qism|episode|ep)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None

def get_titles(query=""):
    with db() as con:
        if query:
            return con.execute("SELECT * FROM titles WHERE title LIKE ? ORDER BY title LIMIT 30", (f"%{query}%",)).fetchall()
        return con.execute("SELECT * FROM titles ORDER BY title").fetchall()

def title_keyboard(titles, prefix="title"):
    rows = []
    for t in titles:
        rows.append([InlineKeyboardButton(text=f"{'🎬' if t['kind']=='kino' else '📺'} {t['title']}", callback_data=f"{prefix}:{t['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="home")])
    return ikb(rows)

async def show_title(call, title_id):
    with db() as con:
        title = con.execute("SELECT * FROM titles WHERE id=?", (title_id,)).fetchone()
        eps = con.execute("SELECT * FROM episodes WHERE title_id=? ORDER BY episode_no", (title_id,)).fetchall()
    if not title:
        await call.answer("Topilmadi", show_alert=True)
        return
    lines = [f"<b>{title['title']}</b>"]
    if title["description"]:
        lines += ["", title["description"]]
    lines += ["", f"📚 Qismlar: {len(eps)}"]
    rows = []
    if eps:
        for ep in eps:
            label = f"{ep['episode_no']}-qism"
            if ep["access"] == "vip":
                label = "💎 " + label
            else:
                label = "▶️ " + label
            rows.append([InlineKeyboardButton(text=label, callback_data=f"episode:{ep['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Mundarija", callback_data="catalog")])
    rows.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")])
    if title["poster_file_id"]:
        try:
            await call.message.answer_photo(title["poster_file_id"], caption="\n".join(lines), reply_markup=ikb(rows))
            await call.answer()
            return
        except Exception:
            pass
    await call.message.edit_text("\n".join(lines), reply_markup=ikb(rows))
    await call.answer()

# =========================
# START / USER FLOW
# =========================
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    referrer = None
    if len(parts) > 1 and parts[1].isdigit():
        candidate = int(parts[1])
        if candidate != message.from_user.id and get_user(candidate):
            referrer = candidate
    register_user(message.from_user, referrer)
    if is_admin(message.from_user.id):
        await message.answer(f"Assalomu alaykum {message.from_user.first_name}!\n\n{BOT_PUBLIC_NAME} boshqaruv paneli.", reply_markup=main_kb())
        return
    missing = await missing_channels(message.from_user.id)
    if missing:
        await send_join_prompt(message, missing)
        return
    await message.answer(f"Assalomu alaykum {message.from_user.first_name}!\n\nBugun nima ko‘ramiz?", reply_markup=main_kb())

@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("🏠 <b>Asosiy menyu</b>\n\nBugun nima ko‘ramiz?", reply_markup=main_kb())
    await call.answer()

@dp.callback_query(F.data == "check_subs")
async def check_subs(call: CallbackQuery):
    missing = await missing_channels(call.from_user.id)
    if missing:
        await call.answer("Hali barcha kanallarga obuna bo‘lmagansiz.", show_alert=True)
        return
    await call.answer("Obuna tasdiqlandi!")
    await call.message.edit_text("Assalomu alaykum!\n\nBugun nima ko‘ramiz?", reply_markup=main_kb())

@dp.callback_query(F.data == "help")
async def help_page(call: CallbackQuery):
    await call.message.edit_text(
        "ℹ️ <b>Qo‘llanma</b>\n\n"
        "• Kino qidirish orqali nom bo‘yicha izlang.\n"
        "• Mundarijadan barcha kinolarni ko‘ring.\n"
        "• 💎 belgili qismlar VIP tarifida.\n"
        "• VIP olish uchun to‘lov chekini yuboring.",
        reply_markup=back_kb()
    )
    await call.answer()

@dp.callback_query(F.data == "catalog")
async def catalog(call: CallbackQuery):
    titles = get_titles()
    if not titles:
        await call.message.edit_text("Hozircha katalog bo‘sh.", reply_markup=back_kb())
    else:
        await call.message.edit_text("📚 <b>Kinolar va seriallar mundarijasi</b>", reply_markup=title_keyboard(titles))
    await call.answer()

@dp.callback_query(F.data == "search")
async def search_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.query)
    await call.message.edit_text("🔎 Kino yoki serial nomini yozing:", reply_markup=back_kb())
    await call.answer()

@dp.message(SearchState.query)
async def search_result(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    titles = get_titles(query)
    await state.clear()
    if not titles:
        await message.answer("Bunday nom topilmadi. Boshqa nom bilan urinib ko‘ring.", reply_markup=main_kb())
        return
    await message.answer("🔎 <b>Qidiruv natijalari:</b>", reply_markup=title_keyboard(titles))

@dp.callback_query(F.data.startswith("title:"))
async def title_open(call: CallbackQuery):
    await show_title(call, int(call.data.split(":")[1]))

@dp.callback_query(F.data.startswith("episode:"))
async def episode_open(call: CallbackQuery):
    ep_id = int(call.data.split(":")[1])
    with db() as con:
        ep = con.execute("SELECT e.*,t.title FROM episodes e JOIN titles t ON t.id=e.title_id WHERE e.id=?", (ep_id,)).fetchone()
    if not ep:
        await call.answer("Qism topilmadi.", show_alert=True)
        return
    if ep["access"] == "vip" and not is_vip(call.from_user.id):
        await call.answer("Bu qism VIP uchun. VIP obuna oling.", show_alert=True)
        await call.message.answer("💎 Bu qismni ko‘rish uchun VIP obuna kerak.", reply_markup=ikb([[InlineKeyboardButton(text="💎 VIP olish", callback_data="vip")]]))
        return
    try:
        if ep["media_type"] == "video":
            await call.message.answer_video(ep["file_id"], caption=f"<b>{ep['title']} — {ep['episode_no']}-qism</b>")
        elif ep["media_type"] == "document":
            await call.message.answer_document(ep["file_id"], caption=f"<b>{ep['title']} — {ep['episode_no']}-qism</b>")
        elif ep["media_type"] == "animation":
            await call.message.answer_animation(ep["file_id"], caption=f"<b>{ep['title']} — {ep['episode_no']}-qism</b>")
        else:
            await call.message.answer_video(ep["file_id"], caption=f"<b>{ep['title']} — {ep['episode_no']}-qism</b>")
        await call.answer()
    except Exception as e:
        log.exception("Media yuborilmadi: %s", e)
        await call.answer("Videoni yuborishda xatolik.", show_alert=True)

@dp.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    user = get_user(call.from_user.id)
    vip_text = "Faol 💎" if is_vip(call.from_user.id) else "Oddiy tarif"
    until = ""
    if user and user["vip_until"] and is_vip(call.from_user.id):
        until = "\nVIP tugash vaqti: " + datetime.fromisoformat(user["vip_until"]).strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(f"👤 <b>Profil</b>\nID: <code>{call.from_user.id}</code>\nTarif: {vip_text}{until}", reply_markup=back_kb())
    await call.answer()

# =========================
# VIP PURCHASE / RECEIPTS
# =========================
@dp.callback_query(F.data == "vip")
async def vip_page(call: CallbackQuery):
    rows = []
    for days in (30, 90, 180):
        price = setting(f"plan_{days}_price", "0")
        name = setting(f"plan_{days}_name", f"{days} kun")
        rows.append([InlineKeyboardButton(text=f"💎 {name} — {price} so‘m", callback_data=f"buy:{days}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="home")])
    await call.message.edit_text(
        "💎 <b>VIP obuna</b>\n\nTarifni tanlang. To‘lovdan so‘ng chek rasmini yuboring.",
        reply_markup=ikb(rows)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def buy_plan(call: CallbackQuery, state: FSMContext):
    days = int(call.data.split(":")[1])
    price = int(setting(f"plan_{days}_price", "0"))
    name = setting(f"plan_{days}_name", f"{days} kun")
    if price <= 0:
        await call.answer("Bu tarif narxi sozlanmagan.", show_alert=True)
        return
    card = setting("card_number")
    owner = setting("card_owner")
    await state.update_data(plan_days=days, plan_price=price, plan_name=name)
    await state.set_state(ReceiptState.photo)
    await call.message.answer(
        f"💳 <b>To‘lov ma’lumotlari</b>\n\n"
        f"Tarif: {name}\nNarx: <b>{price:,} so‘m</b>\n"
        f"Karta: <code>{card}</code>\nKarta egasi: {owner}\n\n"
        "To‘lovni amalga oshirib, chek rasmini shu yerga yuboring."
    )
    await call.answer()

class ReceiptState(StatesGroup):
    photo = State()

@dp.message(ReceiptState.photo, F.photo)
async def receipt_received(message: Message, state: FSMContext):
    data = await state.get_data()
    file_id = message.photo[-1].file_id
    with db() as con:
        cur = con.execute("""
        INSERT INTO payments(user_id,username,plan_name,amount,duration_days,receipt_file_id,status,created_at)
        VALUES(?,?,?,?,?,?,?,?)
        """, (message.from_user.id, message.from_user.username or "", data["plan_name"], data["plan_price"], data["plan_days"], file_id, "pending", now_iso()))
        payment_id = cur.lastrowid
    await state.clear()
    username = f"@{message.from_user.username}" if message.from_user.username else "username yo‘q"
    caption = (
        f"💎 <b>Yangi Premium obuna — sotib olish jarayoni</b>\n\n"
        f"#{payment_id} · {data['plan_name']}\n"
        f"Narx: {data['plan_price']:,} so‘m\n"
        f"Foydalanuvchi: {username} ({message.from_user.id})\n"
        f"Chek yuborilgan vaqt: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        "Kartaga to‘lov tushganini tekshiring, keyin tasdiqlang yoki rad eting."
    )
    markup = ikb([[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"pay_ok:{payment_id}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"pay_no:{payment_id}")
    ]])
    for admin_id in (OWNER_ID, DEVELOPER_ID):
        try:
            await bot.send_photo(admin_id, file_id, caption=caption, reply_markup=markup)
        except Exception as e:
            log.warning("Admin %s ga chek yuborilmadi: %s", admin_id, e)
    await message.answer("✅ Chekingiz adminlarga yuborildi. Tekshiruv natijasi bot orqali xabar qilinadi.", reply_markup=main_kb())

async def payment_decision(call: CallbackQuery, payment_id: int, approved: bool):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return
    with db() as con:
        payment = con.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not payment:
            await call.answer("To‘lov topilmadi.", show_alert=True)
            return
        if payment["status"] != "pending":
            await call.answer("Bu to‘lov avval ko‘rib chiqilgan.", show_alert=True)
            return
        status = "approved" if approved else "rejected"
        con.execute("UPDATE payments SET status=?,decided_at=?,decided_by=? WHERE id=?", (status, now_iso(), call.from_user.id, payment_id))
    if approved:
        until = grant_vip(payment["user_id"], payment["duration_days"], call.from_user.id, payment_id)
        await bot.send_message(payment["user_id"], f"✅ To‘lov tasdiqlandi!\n💎 VIP {payment['duration_days']} kunga faollashtirildi.")
        log_action(call.from_user.id, "VIP payment approved", f"payment={payment_id}, user={payment['user_id']}, days={payment['duration_days']}")
        result = f"✅ Tasdiqlandi — {call.from_user.full_name}, {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    else:
        await bot.send_message(payment["user_id"], "❌ To‘lov chekingiz rad etildi. Iltimos, to‘lov ma’lumotlarini tekshirib qayta yuboring.")
        log_action(call.from_user.id, "VIP payment rejected", f"payment={payment_id}, user={payment['user_id']}")
        result = f"❌ Rad etildi — {call.from_user.full_name}, {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    try:
        await call.message.edit_caption((call.message.caption or "") + "\n\n" + result, reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.answer("Bajarildi.")

@dp.callback_query(F.data.startswith("pay_ok:"))
async def pay_ok(call: CallbackQuery):
    await payment_decision(call, int(call.data.split(":")[1]), True)

@dp.callback_query(F.data.startswith("pay_no:"))
async def pay_no(call: CallbackQuery):
    await payment_decision(call, int(call.data.split(":")[1]), False)

# =========================
# ADMIN PANEL
# =========================
@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb())

@dp.callback_query(F.data == "adm_add_title")
async def add_title_begin(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.clear()
    await state.set_state(AddTitle.title)
    await call.message.answer("Kino yoki serial nomini yuboring:")
    await call.answer()

@dp.message(AddTitle.title)
async def add_title_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(title=message.text.strip())
    await state.set_state(AddTitle.kind)
    await message.answer("Turini tanlang:", reply_markup=ikb([
        [InlineKeyboardButton(text="🎬 Kino", callback_data="kind:kino"),
         InlineKeyboardButton(text="📺 Serial", callback_data="kind:serial")]
    ]))

@dp.callback_query(F.data.startswith("kind:"))
async def add_title_kind(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.update_data(kind=call.data.split(":")[1])
    await state.set_state(AddTitle.description)
    await call.message.answer("Kino/serial tavsifini yuboring. Kerak bo‘lmasa - yozing:")
    await call.answer()

@dp.message(AddTitle.description)
async def add_title_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(description="" if message.text.strip() == "-" else message.text.strip())
    await state.set_state(AddTitle.poster)
    await message.answer("Poster rasmini yuboring yoki postersiz davom etish uchun /skip yozing.")

@dp.message(AddTitle.poster, F.photo)
async def add_title_poster(message: Message, state: FSMContext):
    await state.update_data(poster_file_id=message.photo[-1].file_id)
    await save_new_title(message, state)

@dp.message(AddTitle.poster, Command("skip"))
async def add_title_no_poster(message: Message, state: FSMContext):
    await state.update_data(poster_file_id=None)
    await save_new_title(message, state)

async def save_new_title(message, state):
    data = await state.get_data()
    with db() as con:
        cur = con.execute(
            "INSERT INTO titles(title,kind,description,poster_file_id,created_at) VALUES(?,?,?,?,?)",
            (data["title"], data["kind"], data["description"], data.get("poster_file_id"), now_iso())
        )
        title_id = cur.lastrowid
    await state.clear()
    log_action(message.from_user.id, "Title added", f"id={title_id}, title={data['title']}")
    await message.answer(
        f"✅ <b>{data['title']}</b> qo‘shildi.\nEndi qismlarini ketma-ket yuborishingiz mumkin. Har bir video captionida qism raqami bo‘lsa, avtomatik aniqlanadi. Tugatgach /done yozing.",
        reply_markup=ikb([[InlineKeyboardButton(text="➕ Qismlarini qo‘shish", callback_data=f"add_eps:{title_id}")],
                          [InlineKeyboardButton(text="🛠 Admin panel", callback_data="admin_panel")]])
    )

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(call: CallbackQuery):
    if is_admin(call.from_user.id):
        await call.message.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("add_eps:"))
async def add_eps_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    title_id = int(call.data.split(":")[1])
    with db() as con:
        title = con.execute("SELECT * FROM titles WHERE id=?", (title_id,)).fetchone()
    if not title:
        return await call.answer("Topilmadi", show_alert=True)
    await state.clear()
    await state.update_data(title_id=title_id, title_name=title["title"])
    await state.set_state(AddEpisodes.media)
    await call.message.answer(
        f"<b>{title['title']}</b> uchun videolarni yuboring.\n"
        "Captionga `1-qism`, `Qism: 2` kabi raqam yozing. Raqam bo‘lmasa navbatdagi bo‘sh raqam beriladi.\n"
        "Har bir yuborilgan qism uchun tarifni tanlang. Tugatgach /done."
    )
    await call.answer()

@dp.callback_query(F.data == "adm_add_episode")
async def add_episode_choose(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    titles = get_titles()
    if not titles:
        await call.message.answer("Avval kino yoki serial qo‘shing.")
    else:
        await call.message.answer("Qaysi kino/serialga qism qo‘shamiz?", reply_markup=title_keyboard(titles, "add_existing"))
    await call.answer()

@dp.callback_query(F.data.startswith("add_existing:"))
async def add_existing_choose(call: CallbackQuery, state: FSMContext):
    title_id = int(call.data.split(":")[1])
    with db() as con:
        title = con.execute("SELECT * FROM titles WHERE id=?", (title_id,)).fetchone()
    await state.clear()
    await state.update_data(title_id=title_id, title_name=title["title"])
    await state.set_state(AddEpisodes.media)
    await call.message.answer(f"{title['title']} uchun qismlarni yuboring. Tugatgach /done.")
    await call.answer()

@dp.message(AddEpisodes.media, Command("done"))
async def episodes_done(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await message.answer(f"✅ {data.get('title_name','')} qismlari saqlandi.", reply_markup=admin_kb())

@dp.message(AddEpisodes.media, Command("cancel"))
async def episodes_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Amal bekor qilindi.", reply_markup=admin_kb())

@dp.message(AddEpisodes.media)
async def episode_media_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    media_type = None
    file_id = None
    if message.video:
        media_type, file_id = "video", message.video.file_id
    elif message.document:
        media_type, file_id = "document", message.document.file_id
    elif message.animation:
        media_type, file_id = "animation", message.animation.file_id
    else:
        await message.answer("Video yoki video fayl yuboring. To‘xtatish: /done")
        return
    data = await state.get_data()
    title_id = data["title_id"]
    caption = message.caption or ""
    number = extract_episode_no(caption)
    with db() as con:
        if number is None:
            row = con.execute("SELECT COALESCE(MAX(episode_no),0)+1 AS n FROM episodes WHERE title_id=?", (title_id,)).fetchone()
            number = row["n"]
        exists = con.execute("SELECT id FROM episodes WHERE title_id=? AND episode_no=?", (title_id, number)).fetchone()
        if exists:
            await message.answer(f"⚠️ {number}-qism allaqachon mavjud. Caption raqamini tekshiring.")
            return
        con.execute("""
        INSERT INTO episodes(title_id,episode_no,file_id,media_type,caption,access,created_at)
        VALUES(?,?,?,?,?,?,?)
        """, (title_id, number, file_id, media_type, caption, "free", now_iso()))
        ep_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    await message.answer(
        f"✅ {number}-qism saqlandi. Endi uning tarifini tanlang:",
        reply_markup=ikb([[
            InlineKeyboardButton(text="🟢 Oddiy", callback_data=f"access:free:{ep_id}"),
            InlineKeyboardButton(text="💎 VIP", callback_data=f"access:vip:{ep_id}")
        ]])
    )

@dp.callback_query(F.data.startswith("access:"))
async def set_episode_access(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    _, access, ep_id = call.data.split(":")
    with db() as con:
        con.execute("UPDATE episodes SET access=? WHERE id=?", (access, int(ep_id)))
    await call.answer("Tarif saqlandi.")
    try:
        await call.message.edit_text(f"✅ Qism tarifi: {'VIP' if access=='vip' else 'Oddiy'}")
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data == "adm_delete")
async def delete_begin(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    titles = get_titles()
    if not titles:
        await call.message.answer("Katalog bo‘sh.")
    else:
        await call.message.answer("O‘chirish uchun kino/serialni tanlang:", reply_markup=title_keyboard(titles, "del_title"))
    await call.answer()

@dp.callback_query(F.data.startswith("del_title:"))
async def delete_title(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    title_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT title FROM titles WHERE id=?", (title_id,)).fetchone()
        con.execute("DELETE FROM titles WHERE id=?", (title_id,))
    log_action(call.from_user.id, "Title deleted", f"id={title_id}")
    await call.message.edit_text(f"🗑 {row['title'] if row else 'Kino'} va uning qismlari o‘chirildi.")
    await call.answer()

# =========================
# ADMIN CHANNEL MANAGEMENT
# =========================
@dp.callback_query(F.data == "adm_channels")
async def channels_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    with db() as con:
        channels = con.execute("SELECT * FROM mandatory_channels ORDER BY id").fetchall()
    rows = [[InlineKeyboardButton(text=f"❌ {c['title']}", callback_data=f"ch_del:{c['id']}")] for c in channels]
    rows.append([InlineKeyboardButton(text="➕ Kanal qo‘shish", callback_data="ch_add")])
    await call.message.answer("📣 <b>Majburiy obuna kanallari</b>\nO‘chirish uchun kanal tugmasini bosing.", reply_markup=ikb(rows))
    await call.answer()

@dp.callback_query(F.data == "ch_add")
async def channel_add_begin(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.set_state(AddChannel.chat_id)
    await call.message.answer("Kanal ID yoki @username yuboring:")
    await call.answer()

@dp.message(AddChannel.chat_id)
async def channel_id_got(message: Message, state: FSMContext):
    await state.update_data(chat_id=message.text.strip())
    await state.set_state(AddChannel.invite)
    await message.answer("Kanalga obuna bo‘lish havolasini yuboring:")

@dp.message(AddChannel.invite)
async def channel_invite_got(message: Message, state: FSMContext):
    await state.update_data(invite_url=message.text.strip())
    await state.set_state(AddChannel.title)
    await message.answer("Tugmada ko‘rinadigan kanal nomini yuboring:")

@dp.message(AddChannel.title)
async def channel_title_got(message: Message, state: FSMContext):
    data = await state.get_data()
    with db() as con:
        con.execute("INSERT OR REPLACE INTO mandatory_channels(chat_id,invite_url,title) VALUES(?,?,?)",
                    (data["chat_id"], data["invite_url"], message.text.strip()))
    await state.clear()
    log_action(message.from_user.id, "Mandatory channel added", data["chat_id"])
    await message.answer("✅ Kanal qo‘shildi.", reply_markup=admin_kb())

@dp.callback_query(F.data.startswith("ch_del:"))
async def channel_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    ch_id = int(call.data.split(":")[1])
    with db() as con:
        con.execute("DELETE FROM mandatory_channels WHERE id=?", (ch_id,))
    await call.answer("Kanal o‘chirildi.")
    await channels_menu(call)

# =========================
# SETTINGS / STATS / DEVELOPER
# =========================
@dp.callback_query(F.data == "adm_vip_settings")
async def vip_settings(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    text = (
        "💳 <b>VIP sozlamalari</b>\n\n"
        f"Karta: <code>{setting('card_number')}</code>\n"
        f"Karta egasi: {setting('card_owner')}\n"
        f"30 kun: {setting('plan_30_price')} so‘m\n"
        f"90 kun: {setting('plan_90_price')} so‘m\n"
        f"180 kun: {setting('plan_180_price')} so‘m\n\n"
        "O‘zgartirish uchun tugmani tanlang."
    )
    rows = [
        [InlineKeyboardButton(text="Karta raqami", callback_data="set:card_number"),
         InlineKeyboardButton(text="Karta egasi", callback_data="set:card_owner")],
        [InlineKeyboardButton(text="30 kun narxi", callback_data="set:plan_30_price"),
         InlineKeyboardButton(text="90 kun narxi", callback_data="set:plan_90_price")],
        [InlineKeyboardButton(text="180 kun narxi", callback_data="set:plan_180_price")],
    ]
    await call.message.answer(text, reply_markup=ikb(rows))
    await call.answer()

@dp.callback_query(F.data.startswith("set:"))
async def setting_begin(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    key = call.data.split(":", 1)[1]
    await state.update_data(setting_key=key)
    await state.set_state(SettingState.value)
    await call.message.answer(f"{key} uchun yangi qiymatni yuboring:")
    await call.answer()

@dp.message(SettingState.value)
async def setting_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data["setting_key"]
    old = setting(key)
    value = message.text.strip()
    if key.startswith("plan_") and not value.isdigit():
        await message.answer("Narx faqat raqam bo‘lishi kerak. Qayta yuboring.")
        return
    set_setting(key, value)
    await state.clear()
    log_action(message.from_user.id, "Setting changed", f"{key}: {old} -> {value}")
    # Muhim sozlama o'zgarishi haqida ikkinchi boshqaruvchiga xabar.
    for target in (OWNER_ID, DEVELOPER_ID):
        if target != message.from_user.id:
            try:
                await bot.send_message(target, f"⚙️ Muhim sozlama o‘zgardi.\nKim: {message.from_user.id}\n{key}: {old} → {value}")
            except Exception:
                pass
    await message.answer("✅ Sozlama saqlandi.", reply_markup=admin_kb())

@dp.callback_query(F.data == "adm_stats")
async def stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    with db() as con:
        users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        titles = con.execute("SELECT COUNT(*) n FROM titles").fetchone()["n"]
        episodes = con.execute("SELECT COUNT(*) n FROM episodes").fetchone()["n"]
        pending = con.execute("SELECT COUNT(*) n FROM payments WHERE status='pending'").fetchone()["n"]
        approved = con.execute("SELECT COUNT(*) n FROM payments WHERE status='approved'").fetchone()["n"]
        vip = con.execute("SELECT COUNT(*) n FROM users WHERE vip_until>?", (now_iso(),)).fetchone()["n"]
        refs = con.execute("SELECT COUNT(*) n FROM users WHERE referrer IS NOT NULL").fetchone()["n"]
    await call.message.answer(
        f"📊 <b>Statistika</b>\n\nObunachilar: {users}\nKino/serial: {titles}\nQismlar: {episodes}\nFaol VIP: {vip}\nTasdiqlangan to‘lovlar: {approved}\nKutilayotgan chek: {pending}\nTaklif orqali kelganlar: {refs}"
    )
    await call.answer()

@dp.message(Command("developer"))
async def developer_panel(message: Message):
    if message.from_user.id != DEVELOPER_ID:
        return  # Not developer: intentionally no response.
    await message.answer(
        "🧰 <b>Developer texnik paneli</b>\n\n"
        "Bu panelda bot holati va zaxiralar boshqariladi.\n"
        "Buyruqlar: /devstats, /backup, /botstatus\n"
        "Owner boshqaruv huquqlari o‘zgarishsiz qoladi."
    )

@dp.message(Command("devstats"))
async def dev_stats(message: Message):
    if message.from_user.id != DEVELOPER_ID:
        return
    with db() as con:
        logs = con.execute("SELECT actor_id,action,details,created_at FROM audit_logs ORDER BY id DESC LIMIT 10").fetchall()
    lines = ["🧾 <b>Oxirgi muhim amallar</b>"]
    for row in logs:
        lines.append(f"\n{row['created_at'][:16]} | {row['actor_id']} | {row['action']} | {row['details']}")
    await message.answer("\n".join(lines))

@dp.message(Command("botstatus"))
async def bot_status(message: Message):
    if message.from_user.id not in (OWNER_ID, DEVELOPER_ID):
        return
    await message.answer("🟢 Bot ishlayapti.")

# =========================
# BACKUPS
# =========================
async def create_backup(send_to_admins=False):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"backup_{stamp}.sqlite3"
    # SQLite backup API safely snapshots a live database.
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    # Keep the latest 14 backups.
    backups = sorted(BACKUP_DIR.glob("backup_*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[14:]:
        try:
            old.unlink()
        except OSError:
            pass
    if send_to_admins and BACKUP_SEND_TO_ADMINS:
        for admin_id in (OWNER_ID, DEVELOPER_ID):
            try:
                await bot.send_document(admin_id, target.open("rb"), caption=f"📦 Avtomatik zaxira: {stamp}")
            except Exception as e:
                log.warning("Backup yuborilmadi %s: %s", admin_id, e)
    return target

@dp.callback_query(F.data == "adm_backup")
async def manual_backup(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    path = await create_backup(False)
    await call.message.answer_document(path.open("rb"), caption="📦 Ma’lumotlar bazasi zaxirasi")
    await call.answer("Zaxira yaratildi.")

@dp.message(Command("backup"))
async def backup_command(message: Message):
    if not is_admin(message.from_user.id):
        return
    path = await create_backup(False)
    await message.answer_document(path.open("rb"), caption="📦 Ma’lumotlar bazasi zaxirasi")

async def backup_loop():
    while True:
        try:
            await asyncio.sleep(max(1, BACKUP_INTERVAL_HOURS) * 3600)
            path = await create_backup(BACKUP_SEND_TO_ADMINS)
            log.info("Backup created: %s", path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Backup loop error")

# =========================
# ERROR HANDLING / MAIN
# =========================
@dp.errors()
async def errors_handler(event):
    log.exception("Update error: %s", event.exception)
    return True

async def main():
    init_db()
    # First backup on startup, then periodic backups.
    try:
        await create_backup(False)
    except Exception:
        log.exception("Initial backup failed")
    task = asyncio.create_task(backup_loop())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
