import asyncio
import csv
import io
import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

print("[1/6] main.py yuklandi, env tekshirilmoqda...", flush=True)


def fail(msg):
    print(f"XATO: {msg}", flush=True)
    sys.exit(1)


def _env_int(name, default=0):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        fail(f"{name} faqat raqam bo'lishi kerak, lekin '{raw}' berildi. Masalan: -1001234567890")


def _env_admin_ids():
    raw = os.getenv("ADMIN_ID", "").strip()
    if not raw:
        fail("ADMIN_ID env kiritilmagan. Telegram ID ingizni yozing.")
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            fail(f"ADMIN_ID dagi '{part}' raqam emas. Masalan: 123456789")
        ids.append(int(part))
    if not ids:
        fail("ADMIN_ID bo'sh. Telegram ID ingizni yozing.")
    return ids


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    fail("BOT_TOKEN env kiritilmagan. BotFather dan olingan tokenni yozing.")
ADMIN_IDS = _env_admin_ids()
BACKUP_CHANNEL_ID = _env_int("BACKUP_CHANNEL_ID")
PORT = _env_int("PORT", 10000)
DATA_FILE = "data.json"
print(f"[2/6] Env OK. Adminlar: {len(ADMIN_IDS)} ta, port: {PORT}", flush=True)

from aiohttp import web


def _health_page(request):
    return web.Response(text="ok")


def _run_health():
    app = web.Application()
    app.router.add_get("/", _health_page)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


threading.Thread(target=_run_health, daemon=True).start()
print(f"[3/6] Port {PORT} ochildi (health-server)", flush=True)

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

CATS = {
    "kino": "🎬 Kino",
    "serial": "📺 Serial",
    "anime": "🎌 Anime",
    "multik": "🧸 Multik",
    "drama": "🎭 Drama",
}

DEFAULT_SETTINGS = {
    "baholash": True,
    "buyurtmalar": True,
    "himoya": False,
    "til": "uz",
    "support": "",
    "oy_narx": 9000,
    "umr_narx": 44440,
    "karta": "",
    "karta_egasi": "",
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def money(n):
    return f"{int(n):,}".replace(",", " ")


class DB:
    def __init__(self):
        self.users = {}
        self.movies = {}
        self.channels = []
        self.settings = dict(DEFAULT_SETTINGS)
        self.receipts = {}
        self.complaints = []
        self.banned = []
        self._rid = 0

    def to_dict(self):
        return {
            "users": self.users,
            "movies": self.movies,
            "channels": self.channels,
            "settings": self.settings,
            "receipts": self.receipts,
            "complaints": self.complaints,
            "banned": self.banned,
            "rid": self._rid,
            "saved_at": now_iso(),
        }

    def load_dict(self, d):
        self.users = d.get("users", {})
        self.movies = d.get("movies", {})
        self.channels = d.get("channels", [])
        self.settings = {**DEFAULT_SETTINGS, **d.get("settings", {})}
        self.receipts = d.get("receipts", {})
        self.complaints = d.get("complaints", [])
        self.banned = d.get("banned", [])
        self._rid = d.get("rid", 0)

    def save(self):
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False)
        except Exception:
            pass

    def load_local(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, encoding="utf-8") as f:
                    self.load_dict(json.load(f))
            except Exception:
                pass

    def user(self, uid, name="", username=""):
        u = self.users.get(str(uid))
        if u is None:
            u = {
                "name": name,
                "username": username,
                "joined": now_iso(),
                "vip_until": None,
                "vip_life": False,
            }
            self.users[str(uid)] = u
        else:
            if name:
                u["name"] = name
            if username:
                u["username"] = username
        return u

    def next_rid(self):
        self._rid += 1
        return self._rid


db = DB()


def is_vip(uid) -> bool:
    u = db.users.get(str(uid))
    if not u:
        return False
    if u.get("vip_life"):
        return True
    vu = u.get("vip_until")
    if vu:
        try:
            return datetime.fromisoformat(vu) > datetime.now()
        except Exception:
            return False
    return False


def rating_of(movie):
    r = movie.get("ratings", {})
    if not r:
        return "0.0", 0
    return f"{sum(r.values()) / len(r):.1f}", len(r)


def parse_part(caption):
    """Sarlavhadan (fasl, qism) o'qiydi.
    '5-qism' -> (1, 5); '3 fasl 5 qism' -> (3, 5); 's3e5' -> (3, 5); '3x5' -> (3, 5).
    Fasl ko'rsatilmasa 1 deb olinadi."""
    if not caption:
        return None
    c = caption.lower().replace("сезон", "fasl").replace("серия", "qism")
    season, episode = None, None
    m = re.search(r"(\d+)\s*[-.]?\s*fasl", c)
    if m:
        season = int(m.group(1))
    m = re.search(r"(\d+)\s*[-.]?\s*qism", c)
    if m:
        episode = int(m.group(1))
    if season is None and episode is None:
        m = re.search(r"s(\d+)[^0-9]*e(\d+)", c)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
    if season is None and episode is None:
        m = re.search(r"(\d+)\s*[xх]\s*(\d+)", c)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
    if episode is None:
        m = re.search(r"(\d+)", c)
        if m:
            episode = int(m.group(1))
    if episode is None:
        return None
    if season is None:
        season = 1
    return season, episode


def part_label(season, episode, multi_season):
    if multi_season:
        return f"{season} fasl {episode}-qism"
    return f"{episode}-qism"


def ch_link(ch):
    ch = str(ch)
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    if ch.startswith("-100"):
        return f"https://t.me/c/{ch[4:]}"
    return ch


# ---------------- klaviaturalar ----------------

def main_kb(uid):
    rows = [
        [KeyboardButton(text="🎬 Kinolar"), KeyboardButton(text="🔎 Qidiruv")],
        [KeyboardButton(text="💎 Premium"), KeyboardButton(text="📝 Shikoyat")],
        [KeyboardButton(text="⚙️ Sozlamalar")],
    ]
    if uid in ADMIN_IDS:
        rows.append([KeyboardButton(text="👑 Admin panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_kb():
    b = [
        [InlineKeyboardButton(text="📊 Statistika", callback_data="a:stat"),
         InlineKeyboardButton(text="🎬 Kinolar", callback_data="a:movies")],
        [InlineKeyboardButton(text="➕ Kino qo'shish", callback_data="a:addm"),
         InlineKeyboardButton(text="✂️ Qism qo'shish", callback_data="a:parts")],
        [InlineKeyboardButton(text="📢 Xabar yuborish", callback_data="a:bcast"),
         InlineKeyboardButton(text="📣 Homiy kanallar", callback_data="a:chan")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="a:users"),
         InlineKeyboardButton(text="🚫 Ban/Unban", callback_data="a:ban")],
        [InlineKeyboardButton(text="🛒 Buyurtmalar", callback_data="a:orders"),
         InlineKeyboardButton(text="⚠️ Shikoyatlar", callback_data="a:comp")],
        [InlineKeyboardButton(text="📈 Analitika", callback_data="a:anal"),
         InlineKeyboardButton(text="📤 Excel eksport", callback_data="a:expe")],
        [InlineKeyboardButton(text="📥 Excel import", callback_data="a:expi"),
         InlineKeyboardButton(text="💎 Premium", callback_data="a:prem")],
        [InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="a:sett"),
         InlineKeyboardButton(text="💾 Zaxira nuska", callback_data="a:back")],
        [InlineKeyboardButton(text="« Bosh menyu", callback_data="a:home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=b)


def sub_kb():
    rows = [[InlineKeyboardButton(text=f"📢 {c}", url=ch_link(c))] for c in db.channels]
    rows.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="checksub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def premium_kb():
    if not db.settings["buyurtmalar"]:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 1 oylik sotib olish", callback_data="pay:oy")],
        [InlineKeyboardButton(text="♾ Umrlik sotib olish", callback_data="pay:umr")],
    ])


def premium_text():
    s = db.settings
    t = (f"💎 <b>Premium obuna</b>\n\n"
         f"🧾 1 oylik: <b>{money(s['oy_narx'])} so'm</b>\n"
         f"♾ Umrlik: <b>{money(s['umr_narx'])} so'm</b>\n\n"
         f"💳 Karta: <code>{s['karta']}</code>\n"
         f"👤 Karta egasi: {s['karta_egasi']}\n\n"
         f"To'lov qiling va chekni (rasm/fayl) shu yerga yuboring. "
         f"Admin tasdiqlagach VIP ochiladi.")
    if not s["buyurtmalar"]:
        t += "\n\n<i>⚠️ Hozircha buyurtmalar o'chirilgan.</i>"
    return t


def movies_kb(page=0, per=10):
    items = list(db.movies.values())
    total = len(items)
    chunk = items[page * per:(page + 1) * per]
    rows = []
    for mv in chunk:
        label = f"{CATS.get(mv['category'], '🎬')} {mv['name']}"[:60]
        rows.append([InlineKeyboardButton(text=label, callback_data=f"mv:{mv['code']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"ml:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"ml:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb():
    s = db.settings

    def mark(x):
        return "✅" if x else "❌"

    rows = [
        [InlineKeyboardButton(text=f"📝 Baholash: {mark(s['baholash'])}", callback_data="stg:baholash"),
         InlineKeyboardButton(text=f"🛒 Buyurtmalar: {mark(s['buyurtmalar'])}", callback_data="stg:buyurtmalar")],
        [InlineKeyboardButton(text=f"🔒 Himoya: {mark(s['himoya'])}", callback_data="stg:himoya"),
         InlineKeyboardButton(text=f"🌐 Til: {s.get('til', 'uz')}", callback_data="ste:til")],
        [InlineKeyboardButton(text="📞 Support", callback_data="ste:support"),
         InlineKeyboardButton(text=f"📣 Homiy kanallar: {len(db.channels)}", callback_data="a:chan")],
        [InlineKeyboardButton(text="💳 Karta", callback_data="ste:karta"),
         InlineKeyboardButton(text="👤 Karta egasi", callback_data="ste:karta_egasi")],
        [InlineKeyboardButton(text=f"💎 1 oy: {money(s['oy_narx'])}", callback_data="ste:oy_narx"),
         InlineKeyboardButton(text=f"♾ Umrlik: {money(s['umr_narx'])}", callback_data="ste:umr_narx")],
        [InlineKeyboardButton(text="« Admin panel", callback_data="a:panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text():
    s = db.settings
    return ("⚙️ <b>Sozlamalar</b>\n\n"
            f"📝 Baholash: {'✅' if s['baholash'] else '❌'}\n"
            f"🛒 Buyurtmalar: {'✅' if s['buyurtmalar'] else '❌'}\n"
            f"🔒 Kontentni himoyalash: {'✅' if s['himoya'] else '❌'}\n"
            f"🌐 Standart til: {s.get('til', 'uz')}\n"
            f"📞 Support: {s['support'] or 'kiritilmagan'}\n"
            f"💳 Karta raqami: {s['karta'] or 'kiritilmagan'}\n"
            f"👤 Karta egasi: {s['karta_egasi'] or 'kiritilmagan'}\n"
            f"💎 1 oylik narx: {money(s['oy_narx'])} so'm\n"
            f"💎 Umrlik narx: {money(s['umr_narx'])} so'm\n"
            f"📣 Kanal havolasi: {len(db.channels)} ta kanal")


# ---------------- middleware ----------------

class GuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id in ADMIN_IDS:
            return await handler(event, data)

        db.user(user.id, user.full_name, user.username or "")
        u = db.users[str(user.id)]
        if u.get("banned") or user.id in db.banned:
            if isinstance(event, Message):
                await event.answer("🚫 Siz bloklangansiz.")
            else:
                await event.answer("🚫 Siz bloklangansiz.", show_alert=True)
            return

        st = data.get("state")
        state_name = ""
        if st is not None:
            state_name = (await st.get_state()) or ""

        need_check = False
        if isinstance(event, CallbackQuery):
            if event.data != "checksub":
                need_check = True
        elif isinstance(event, Message) and not state_name:
            need_check = True

        if need_check and db.channels:
            bot_ = data["bot"]
            bad = []
            for ch in db.channels:
                try:
                    member = await bot_.get_chat_member(ch, user.id)
                    if member.status in ("left", "kicked"):
                        bad.append(ch)
                except Exception:
                    pass
            if bad:
                if isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Avval kanallarga obuna bo'ling!", show_alert=True)
                    await bot_.send_message(
                        user.id,
                        "❌ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
                        reply_markup=sub_kb(),
                    )
                else:
                    await event.answer(
                        "❌ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
                        reply_markup=sub_kb(),
                    )
                return
        return await handler(event, data)


# ---------------- FSM ----------------

class AddMovie(StatesGroup):
    code = State()
    name = State()
    photo = State()
    desc = State()


class AddParts(StatesGroup):
    adding = State()


class Bcast(StatesGroup):
    content = State()


class ChanAdd(StatesGroup):
    text = State()


class BanFlow(StatesGroup):
    uid = State()


class SetFlow(StatesGroup):
    value = State()


class ImpFlow(StatesGroup):
    wait = State()


class UserSearch(StatesGroup):
    q = State()


class UserComplaint(StatesGroup):
    text = State()


class UserPay(StatesGroup):
    wait = State()


class AdminFilter(Filter):
    async def __call__(self, obj):
        u = getattr(obj, "from_user", None)
        return u is not None and u.id in ADMIN_IDS


bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())
dp.message.outer_middleware(GuardMiddleware())
dp.callback_query.outer_middleware(GuardMiddleware())
print(f"[3/6] Bot va routerlar tayyor. Zaxira kanal: {BACKUP_CHANNEL_ID}", flush=True)

router_user = Router()
router_admin = Router()
router_admin.message.filter(AdminFilter())
router_admin.callback_query.filter(AdminFilter())
dp.include_router(router_admin)
dp.include_router(router_user)


# ================= FOYDALANUVCHI =================

@router_user.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    db.user(m.from_user.id, m.from_user.full_name, m.from_user.username or "")
    db.save()
    await m.answer(
        "🎬 <b>Kino botiga xush kelibsiz!</b>\n\n"
        "Kinolarni ko'rish uchun menyudan foydalaning.",
        reply_markup=main_kb(m.from_user.id),
    )


@router_user.message(F.text == "🎬 Kinolar", StateFilter(None))
async def list_movies(m: Message):
    if not db.movies:
        await m.answer("🎬 Hozircha kino qo'shilmagan.")
        return
    await m.answer("🎬 <b>Kinolar ro'yxati:</b>", reply_markup=movies_kb(0))


@router_user.callback_query(F.data.startswith("ml:"))
async def movies_page(cq: CallbackQuery):
    page = int(cq.data.split(":")[1])
    try:
        await cq.message.edit_text("🎬 <b>Kinolar ro'yxati:</b>", reply_markup=movies_kb(page))
    except TelegramBadRequest:
        await cq.message.answer("🎬 <b>Kinolar ro'yxati:</b>", reply_markup=movies_kb(page))
    await cq.answer()


@router_user.callback_query(F.data.startswith("mv:"))
async def movie_info(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    code = cq.data.split(":", 1)[1]
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("❌ Kino topilmadi.")
        return
    mv["views"] = mv.get("views", 0) + 1
    db.save()
    avg, cnt = rating_of(mv)
    vip_only = mv.get("tariff") == "vip"
    locked = vip_only and not is_vip(cq.from_user.id)
    text = (f"{CATS.get(mv['category'], '🎬')} <b>{mv['name']}</b>\n"
            f"⭐ {avg} ({cnt})   👁 {mv['views']}   📁 {len(mv['parts'])}\n"
            f"{'🔒 VIP kontent' if vip_only else '🆓 Bepul'}\n\n{mv.get('desc', '')}")
    kb = []
    if locked:
        kb.append([InlineKeyboardButton(text="💎 Premium sotib olish", callback_data="pr")])
    else:
        pairs = sorted((int(k.split(":")[0]), int(k.split(":")[1])) for k in mv["parts"].keys())
        multi = any(s > 1 for s, _ in pairs)
        for i in range(0, len(pairs), 5):
            kb.append([InlineKeyboardButton(
                text=part_label(s, e, multi),
                callback_data=f"pt:{code}:{s}:{e}") for s, e in pairs[i:i + 5]])
    if cq.from_user.id in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"del:{code}")])
    kb.append([InlineKeyboardButton(text="« Orqaga", callback_data="ml:0")])
    try:
        await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except TelegramBadRequest:
        await cq.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cq.answer()


@router_user.callback_query(F.data.startswith("pt:"))
async def watch_part(cq: CallbackQuery):
    _, code, season, ep = cq.data.split(":")
    key = f"{season}:{ep}"
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("❌ Kino topilmadi.")
        return
    if mv.get("tariff") == "vip" and not is_vip(cq.from_user.id):
        await cq.answer("🔒 Bu kontent faqat VIP uchun!", show_alert=True)
        return
    fid = mv["parts"].get(key)
    if not fid:
        await cq.answer("❌ Qism topilmadi.")
        return
    label = part_label(int(season), int(ep), int(season) > 1)
    await cq.message.answer_video(
        video=fid,
        caption=f"{mv['name']} — {label}",
        protect_content=db.settings["himoya"],
    )
    mv = db.movies[code]
    if db.settings["baholash"] and str(cq.from_user.id) not in mv.get("ratings", {}):
        stars = [InlineKeyboardButton(text=s, callback_data=f"rt:{code}:{i}")
                 for i, s in enumerate(["1⭐", "2⭐", "3⭐", "4⭐", "5⭐"], start=1)]
        await cq.message.answer("🎬 Kino yoqdimi? Baho bering:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[stars]))
    await cq.answer()


@router_user.callback_query(F.data.startswith("rt:"))
async def rate_movie(cq: CallbackQuery):
    _, code, score = cq.data.split(":")
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("❌ Kino topilmadi.")
        return
    mv.setdefault("ratings", {})[str(cq.from_user.id)] = int(score)
    db.save()
    await cq.message.edit_text("✅ Bahongiz qabul qilindi. Rahmat!")
    await cq.answer()


@router_user.message(F.text == "🔎 Qidiruv", StateFilter(None))
async def search_start(m: Message, state: FSMContext):
    await state.set_state(UserSearch.q)
    await m.answer("🔎 Kino nomi yoki kodini yozing:")


@router_user.message(UserSearch.q, F.text)
async def search_run(m: Message, state: FSMContext):
    q = m.text.strip().lower()
    found = [mv for mv in db.movies.values()
             if q in mv["name"].lower() or q == mv["code"].lower()]
    await state.clear()
    if not found:
        await m.answer("❌ Hech narsa topilmadi.")
        return
    rows = [[InlineKeyboardButton(text=f"{CATS.get(mv['category'], '🎬')} {mv['name']}"[:60],
                                  callback_data=f"mv:{mv['code']}")] for mv in found[:20]]
    await m.answer("🔎 Topildi:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router_user.message(F.text == "💎 Premium", StateFilter(None))
@router_user.callback_query(F.data == "pr")
async def premium_menu(event):
    kb = premium_kb()
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(premium_text(), reply_markup=kb)
        except TelegramBadRequest:
            await event.message.answer(premium_text(), reply_markup=kb)
        await event.answer()
    else:
        await event.answer(premium_text(), reply_markup=kb)


@router_user.callback_query(F.data.startswith("pay:"))
async def pay_start(cq: CallbackQuery, state: FSMContext):
    if not db.settings["buyurtmalar"]:
        await cq.answer("⚠️ Hozircha buyurtmalar o'chirilgan.", show_alert=True)
        return
    tariff = cq.data.split(":")[1]
    await state.set_state(UserPay.wait)
    await state.update_data(tariff=tariff)
    name = "1 oylik" if tariff == "oy" else "Umrlik"
    await cq.message.answer(
        f"🧾 <b>{name}</b> tarifi tanlandi.\n\n"
        "To'lov qiling va chekni (rasm yoki fayl) shu yerga yuboring.\n"
        "Bekor qilish: /bekor",
    )
    await cq.answer()


@router_user.message(UserPay.wait, F.photo | F.document)
async def pay_receipt(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    tariff = data.get("tariff", "oy")
    rid = db.next_rid()
    db.receipts[str(rid)] = {
        "uid": m.from_user.id,
        "tariff": tariff,
        "status": "wait",
        "at": now_iso(),
    }
    db.save()
    name = "1 oylik" if tariff == "oy" else "Umrlik"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"apr:{rid}"),
         InlineKeyboardButton(text="❌ Rad etish", callback_data=f"rej:{rid}")],
    ])
    for admin_id in ADMIN_IDS:
        try:
            await m.copy_to(admin_id)
            await bot.send_message(
                admin_id,
                f"🧾 <b>Yangi to'lov</b>\n🆔 {rid}\n👤 {m.from_user.full_name} "
                f"(<code>{m.from_user.id}</code>)\n📦 Tarif: {name}",
                reply_markup=kb,
            )
        except Exception:
            pass
    await state.clear()
    await m.answer("🧾 Chek yuborildi. Admin tasdiqlagach VIP ochiladi. Kuting...")


@router_user.message(F.text == "📝 Shikoyat", StateFilter(None))
async def complaint_start(m: Message, state: FSMContext):
    await state.set_state(UserComplaint.text)
    await m.answer("📝 Shikoyatingizni yozing (yoki /bekor):")


@router_user.message(UserComplaint.text, F.text)
async def complaint_send(m: Message, state: FSMContext, bot: Bot):
    db.complaints.append({"uid": m.from_user.id, "text": m.text, "at": now_iso()})
    db.save()
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"⚠️ <b>Yangi shikoyat</b>\n👤 {m.from_user.full_name} "
                f"(<code>{m.from_user.id}</code>)\n💬 {m.text}",
            )
        except Exception:
            pass
    await state.clear()
    await m.answer("✅ Shikoyatingiz yuborildi.")


@router_user.message(F.text == "⚙️ Sozlamalar", StateFilter(None))
async def user_settings(m: Message):
    s = db.settings
    vip = "♾ Umrlik" if db.users.get(str(m.from_user.id), {}).get("vip_life") else \
        ("🧾 VIP" if is_vip(m.from_user.id) else "❌ yo'q")
    text = (f"⚙️ <b>Sozlamalar</b>\n\n🌐 Til: {s.get('til', 'uz')}\n"
            f"💎 VIP holati: {vip}\n"
            f"📞 Support: {s['support'] or 'kiritilmagan'}")
    await m.answer(text)


@router_user.callback_query(F.data == "checksub")
async def check_sub(cq: CallbackQuery, bot: Bot):
    bad = []
    for ch in db.channels:
        try:
            member = await bot.get_chat_member(ch, cq.from_user.id)
            if member.status in ("left", "kicked"):
                bad.append(ch)
        except Exception:
            pass
    if bad:
        await cq.answer("❌ Hali obuna bo'lmagansiz!", show_alert=True)
        return
    await cq.answer("✅ Rahmat!")
    await cq.message.answer("🏠 Bosh menyu", reply_markup=main_kb(cq.from_user.id))


# ================= ADMIN =================

@router_admin.message(F.text == "👑 Admin panel", StateFilter(None))
@router_admin.message(Command("admin"))
async def admin_panel(m: Message):
    await m.answer("👑 <b>Admin panel</b>", reply_markup=admin_kb())


@router_admin.callback_query(F.data == "a:home")
async def admin_home(cq: CallbackQuery):
    await cq.message.delete()
    await cq.message.answer("🏠 Bosh menyu", reply_markup=main_kb(cq.from_user.id))
    await cq.answer()


@router_admin.callback_query(F.data == "a:panel")
async def admin_back(cq: CallbackQuery):
    await cq.message.edit_text("👑 <b>Admin panel</b>", reply_markup=admin_kb())
    await cq.answer()


@router_admin.callback_query(F.data == "a:stat")
async def a_stat(cq: CallbackQuery):
    today = datetime.now().date().isoformat()
    new_today = sum(1 for u in db.users.values() if u.get("joined", "").startswith(today))
    vip_count = sum(1 for uid in db.users if is_vip(int(uid)))
    parts_total = sum(len(mv["parts"]) for mv in db.movies.values())
    pending = sum(1 for r in db.receipts.values() if r["status"] == "wait")
    text = ("📊 <b>Statistika</b>\n\n"
            f"👥 Jami foydalanuvchilar: {len(db.users)}\n"
            f"🆕 Bugun qo'shilgan: {new_today}\n"
            f"💎 VIP foydalanuvchilar: {vip_count}\n"
            f"🎬 Kinolar: {len(db.movies)}\n"
            f"📁 Jami qismlar: {parts_total}\n"
            f"⏳ Kutilayotgan to'lovlar: {pending}\n"
            f"📣 Kanallar: {len(db.channels)}\n"
            f"🚫 Bloklangan: {len(db.banned) + sum(1 for u in db.users.values() if u.get('banned'))}")
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer()


@router_admin.callback_query(F.data == "a:movies")
async def a_movies(cq: CallbackQuery):
    if not db.movies:
        await cq.answer("Kino yo'q.")
        return
    await cq.message.edit_text("🎬 <b>Barcha kinolar:</b>", reply_markup=movies_kb(0))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("del:"))
async def a_del(cq: CallbackQuery):
    code = cq.data.split(":")[1]
    if code in db.movies:
        del db.movies[code]
        db.save()
        await cq.answer("🗑 O'chirildi.")
        try:
            await cq.message.edit_text("🎬 <b>Barcha kinolar:</b>", reply_markup=movies_kb(0))
        except TelegramBadRequest:
            pass
    else:
        await cq.answer("Topilmadi.")


# ---- Kino qo'shish ----

@router_admin.callback_query(F.data == "a:addm")
async def a_addm(cq: CallbackQuery, state: FSMContext):
    rows = [[InlineKeyboardButton(text=t, callback_data=f"cat:{k}")] for k, t in CATS.items()]
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
    await cq.message.edit_text("🎬 Qaysi turda qo'shamiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.set_state(AddMovie.code)
    await cq.answer()


@router_admin.callback_query(F.data.startswith("cat:"))
async def a_cat(cq: CallbackQuery, state: FSMContext):
    await state.update_data(category=cq.data.split(":")[1])
    await cq.message.edit_text("🔢 Kino kodi (unikal, masalan: 101):")
    await state.set_state(AddMovie.code)
    await cq.answer()


@router_admin.message(AddMovie.code, F.text)
async def a_code(m: Message, state: FSMContext):
    code = m.text.strip()
    if not code or code in db.movies:
        await m.answer("❌ Kod band yoki noto'g'ri. Boshqa kod kiriting:")
        return
    await state.update_data(code=code)
    await m.answer("📝 Kino nomini kiriting:")
    await state.set_state(AddMovie.name)


@router_admin.message(AddMovie.name, F.text)
async def a_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await m.answer("🖼 Rasm yuboring:")
    await state.set_state(AddMovie.photo)


@router_admin.message(AddMovie.photo, F.photo)
async def a_photo(m: Message, state: FSMContext):
    await state.update_data(photo=m.photo[-1].file_id)
    await m.answer("📄 Tavfsif yozing:")
    await state.set_state(AddMovie.desc)


@router_admin.message(AddMovie.desc, F.text)
async def a_desc(m: Message, state: FSMContext):
    data = await state.get_data()
    db.movies[data["code"]] = {
        "code": data["code"],
        "name": data["name"],
        "category": data.get("category", "kino"),
        "desc": m.text.strip(),
        "photo": data.get("photo", ""),
        "tariff": "oddiy",
        "parts": {},
        "views": 0,
        "ratings": {},
    }
    db.save()
    await state.clear()
    await m.answer(
        f"✅ <b>{data['name']}</b> qo'shildi!\n\n"
        f"Qismlar qo'shish: Admin panel → ✂️ Qism qo'shish",
        reply_markup=admin_kb(),
    )


# ---- Qism qo'shish ----

@router_admin.callback_query(F.data == "a:parts")
async def a_parts(cq: CallbackQuery, state: FSMContext):
    await cq.message.edit_text("✂️ Qism qo'shiladigan kino kodini yozing:")
    await state.set_state(AddParts.adding)
    await state.update_data(step="code")
    await cq.answer()


@router_admin.message(AddParts.adding, F.text)
async def a_parts_text(m: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("step") == "code":
        code = m.text.strip()
        if code not in db.movies:
            await m.answer("❌ Bunday kodli kino yo'q. Qayta kiriting:")
            return
        mv = db.movies[code]
        await state.update_data(step="tariff", code=code)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🆓 Oddiy (bepul)", callback_data="trf:oddiy"),
             InlineKeyboardButton(text="💎 VIP uchun", callback_data="trf:vip")],
        ])
        await m.answer(f"🎬 <b>{mv['name']}</b> — qismlar qaysi tarif uchun?", reply_markup=kb)
    elif m.text.strip() == "✅ Tayyor":
        code = data.get("code", "")
        mv = db.movies.get(code)
        await state.clear()
        n = len(mv["parts"]) if mv else 0
        await m.answer(f"✅ Tayyor! Jami {n} qism saqlandi.", reply_markup=main_kb(m.from_user.id))


@router_admin.callback_query(F.data.startswith("trf:"))
async def a_parts_tariff(cq: CallbackQuery, state: FSMContext):
    tariff = cq.data.split(":")[1]
    await state.update_data(tariff=tariff, step="adding")
    mv = db.movies[(await state.get_data())["code"]]
    mv["tariff"] = tariff
    db.save()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Tayyor")]], resize_keyboard=True)
    await cq.message.answer(
        "📤 Endi videolarni yuboring.\n"
        "Har bir media sarlavhasida qism raqami bo'lsin — bot o'zi o'qib taxlaydi.\n"
        "Masalan: <b>1-qism</b>, <b>2-qism</b> ...\n"
        "Fasl bo'lsa: <b>3 fasl 5 qism</b> (yoki s3e5, 3x5)\n\n"
        "Tugatgach «✅ Tayyor» tugmasini bosing.",
        reply_markup=kb,
    )
    await cq.answer()


@router_admin.message(AddParts.adding, F.video | F.document)
async def a_parts_media(m: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("step") != "adding":
        await m.answer("❌ Avval kino kodini kiriting.")
        return
    code = data["code"]
    mv = db.movies.get(code)
    if not mv:
        await m.answer("❌ Kino topilmadi.")
        await state.clear()
        return
    parsed = parse_part(m.caption)
    if parsed is None:
        await m.answer("❌ Sarlavhasida qism raqami topilmadi.\n"
                       "Masalan: <b>5-qism</b> yoki <b>3 fasl 5 qism</b>")
        return
    season, ep = parsed
    fid = m.video.file_id if m.video else m.document.file_id
    mv["parts"][f"{season}:{ep}"] = fid
    db.save()
    await m.answer(f"✅ {part_label(season, ep, season > 1)} qo'shildi (jami: {len(mv['parts'])}). "
                   f"Davom eting yoki «✅ Tayyor» bosing.")


# ---- Xabar yuborish ----

@router_admin.callback_query(F.data == "a:bcast")
async def a_bcast(cq: CallbackQuery, state: FSMContext):
    await cq.message.edit_text("📢 Yuboriladigan xabarni yozing (barcha foydalanuvchilarga):")
    await state.set_state(Bcast.content)
    await cq.answer()


@router_admin.message(Bcast.content)
async def a_bcast_run(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ok, fail = 0, 0
    for uid in list(db.users.keys()):
        try:
            await m.copy_to(int(uid))
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)
    await m.answer(f"✅ Yuborildi: {ok}\n❌ Xatolik: {fail}", reply_markup=admin_kb())


# ---- Homiy kanallar ----

@router_admin.callback_query(F.data == "a:chan")
async def a_chan(cq: CallbackQuery):
    rows = []
    for i, ch in enumerate(db.channels):
        rows.append([InlineKeyboardButton(text=f"❌ {ch}", callback_data=f"chdel:{i}")])
    rows.append([InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="chadd")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
    text = ("📣 <b>Majburiy obuna kanallari</b>\n\n" +
            ("\n".join(f"• {c}" for c in db.channels) if db.channels else "Hozircha kanal qo'shilmagan."))
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_admin.callback_query(F.data == "chadd")
async def a_chan_add(cq: CallbackQuery, state: FSMContext):
    await cq.message.edit_text("Kanal username yoki ID yuboring (masalan: @Ruhshunos_PanjaraOrtida yoki -100123...):")
    await state.set_state(ChanAdd.text)
    await cq.answer()


@router_admin.message(ChanAdd.text, F.text)
async def a_chan_save(m: Message, state: FSMContext):
    ch = m.text.strip()
    if not (ch.startswith("@") or ch.startswith("-100") or ch.lstrip("-").isdigit()):
        await m.answer("❌ Noto'g'ri format. Masalan: @kanal yoki -1001234567890")
        return
    if ch in db.channels:
        await m.answer("❌ Bu kanal allaqachon qo'shilgan.")
        return
    db.channels.append(ch)
    db.save()
    await state.clear()
    await m.answer(f"✅ {ch} qo'shildi.", reply_markup=admin_kb())


@router_admin.callback_query(F.data.startswith("chdel:"))
async def a_chan_del(cq: CallbackQuery):
    i = int(cq.data.split(":")[1])
    if 0 <= i < len(db.channels):
        removed = db.channels.pop(i)
        db.save()
        await cq.answer(f"❌ {removed} o'chirildi.")
        rows = [[InlineKeyboardButton(text=f"❌ {c}", callback_data=f"chdel:{j}")] for j, c in enumerate(db.channels)]
        rows.append([InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="chadd")])
        rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
        try:
            await cq.message.edit_text("📣 <b>Majburiy obuna kanallari</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except TelegramBadRequest:
            pass
    else:
        await cq.answer("Topilmadi.")


# ---- Foydalanuvchilar ----

def users_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "name", "username", "joined", "vip"])
    for uid, u in db.users.items():
        w.writerow([uid, u.get("name", ""), u.get("username", ""), u.get("joined", ""),
                    "VIP" if is_vip(int(uid)) else ""])
    return buf.getvalue().encode("utf-8-sig")


def movies_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code", "name", "category", "tariff", "parts", "views"])
    for c, mv in db.movies.items():
        w.writerow([c, mv["name"], mv["category"], mv["tariff"], len(mv["parts"]), mv["views"]])
    return buf.getvalue().encode("utf-8-sig")


@router_admin.callback_query(F.data == "a:users")
async def a_users(cq: CallbackQuery):
    vip_count = sum(1 for uid in db.users if is_vip(int(uid)))
    last = list(db.users.items())[-5:]
    text = (f"👥 <b>Foydalanuvchilar</b>\n\nJami: {len(db.users)}\nVIP: {vip_count}\n\n"
            f"Oxirgi 5 ta:\n" + "\n".join(f"• {u.get('name','?')} (<code>{uid}</code>)" for uid, u in last))
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 CSV yuklab olish", callback_data="a:ucsv")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer()


@router_admin.callback_query(F.data == "a:ucsv")
async def a_ucsv(cq: CallbackQuery):
    await cq.message.answer_document(
        BufferedInputFile(users_csv(), filename="foydalanuvchilar.csv"),
        caption="👥 Foydalanuvchilar ro'yxati (Excel'da ochiladi)",
    )
    await cq.answer()


# ---- Ban / Unban ----

@router_admin.callback_query(F.data == "a:ban")
async def a_ban(cq: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Banlash", callback_data="bn:1"),
         InlineKeyboardButton(text="✅ Bandan chiqarish", callback_data="bn:0")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")],
    ])
    await cq.message.edit_text("🚫 <b>Ban / Unban</b>\nTanlang:", reply_markup=kb)
    await cq.answer()


@router_admin.callback_query(F.data.startswith("bn:"))
async def a_ban_pick(cq: CallbackQuery, state: FSMContext):
    await state.update_data(ban=cq.data.split(":")[1] == "1")
    await cq.message.edit_text("Foydalanuvchi ID sini yozing:")
    await state.set_state(BanFlow.uid)
    await cq.answer()


@router_admin.message(BanFlow.uid, F.text)
async def a_ban_apply(m: Message, state: FSMContext):
    if not m.text.strip().lstrip("-").isdigit():
        await m.answer("❌ Faqat raqam kiriting.")
        return
    uid = int(m.text.strip())
    data = await state.get_data()
    if data.get("ban"):
        u = db.user(uid)
        u["banned"] = True
        if uid in db.banned:
            db.banned.remove(uid)
        msg = f"🚫 {uid} bloklandi."
    else:
        u = db.users.get(str(uid))
        if u:
            u["banned"] = False
        if uid in db.banned:
            db.banned.remove(uid)
        msg = f"✅ {uid} bandan chiqarildi."
    db.save()
    await state.clear()
    await m.answer(msg, reply_markup=admin_kb())


# ---- Buyurtmalar ----

@router_admin.callback_query(F.data == "a:orders")
async def a_orders(cq: CallbackQuery):
    pending = [(int(rid), r) for rid, r in db.receipts.items() if r["status"] == "wait"]
    if not pending:
        await cq.answer("🛒 Kutilayotgan to'lov yo'q.")
        return
    rows = []
    for rid, r in pending:
        name = "1 oylik" if r["tariff"] == "oy" else "Umrlik"
        rows.append([InlineKeyboardButton(text=f"🆔{rid} 👤{r['uid']} 📦{name}", callback_data="noop")])
        rows.append([InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"apr:{rid}"),
                     InlineKeyboardButton(text="❌ Rad etish", callback_data=f"rej:{rid}")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
    await cq.message.edit_text(f"🛒 <b>Kutilayotgan to'lovlar: {len(pending)}</b>",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("apr:"))
async def a_approve(cq: CallbackQuery, bot: Bot):
    rid = cq.data.split(":")[1]
    r = db.receipts.get(rid)
    if not r or r["status"] != "wait":
        await cq.answer("Allaqachon ko'rilgan.")
        return
    uid = r["uid"]
    u = db.user(uid)
    if r["tariff"] == "oy":
        u["vip_until"] = (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds")
        u["vip_life"] = False
        label = "1 oylik"
    else:
        u["vip_life"] = True
        label = "umrlik"
    r["status"] = "ok"
    db.save()
    try:
        await bot.send_message(uid, f"🎉 Tabriklaymiz! <b>{label}</b> premium obunangiz faollashtirildi. 🎬")
    except Exception:
        pass
    await cq.message.edit_text(cq.message.text + f"\n\n✅ Tasdiqlandi → <code>{uid}</code>")
    await cq.answer("✅ Tasdiqlandi.")


@router_admin.callback_query(F.data.startswith("rej:"))
async def a_reject(cq: CallbackQuery, bot: Bot):
    rid = cq.data.split(":")[1]
    r = db.receipts.get(rid)
    if not r or r["status"] != "wait":
        await cq.answer("Allaqachon ko'rilgan.")
        return
    r["status"] = "rej"
    db.save()
    try:
        await bot.send_message(r["uid"], "❌ To'lovingiz rad etildi. Qayta urinib ko'ring yoki supportga yozing.")
    except Exception:
        pass
    await cq.message.edit_text(cq.message.text + f"\n\n❌ Rad etildi → <code>{r['uid']}</code>")
    await cq.answer("❌ Rad etildi.")


# ---- Shikoyatlar ----

@router_admin.callback_query(F.data == "a:comp")
async def a_comp(cq: CallbackQuery):
    items = db.complaints[-10:]
    text = "⚠️ <b>Shikoyatlar</b>\n\n"
    text += "\n\n".join(f"👤 <code>{c['uid']}</code> ({c['at']})\n💬 {c['text']}" for c in items) if items \
        else "Shikoyatlar yo'q."
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Tozalash", callback_data="comp:clr")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer()


@router_admin.callback_query(F.data == "comp:clr")
async def a_comp_clr(cq: CallbackQuery):
    db.complaints = []
    db.save()
    await cq.message.edit_text("⚠️ <b>Shikoyatlar</b>\n\nTozalandi.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                   [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer("🗑 Tozalandi.")


# ---- Analitika ----

@router_admin.callback_query(F.data == "a:anal")
async def a_anal(cq: CallbackQuery):
    lines = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).date().isoformat()
        cnt = sum(1 for u in db.users.values() if u.get("joined", "").startswith(day))
        lines.append(f"{day}: {cnt} ta")
    parts_total = sum(len(mv["parts"]) for mv in db.movies.values())
    views_total = sum(mv["views"] for mv in db.movies.values())
    text = ("📈 <b>Analitika (oxirgi 7 kun)</b>\n\n" + "\n".join(lines) +
            f"\n\n🎬 Kinolar: {len(db.movies)}\n📁 Qismlar: {parts_total}\n👁 Ko'rilgan: {views_total}")
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer()


# ---- Excel eksport / import ----

@router_admin.callback_query(F.data == "a:expe")
async def a_expe(cq: CallbackQuery):
    await cq.message.answer_document(
        BufferedInputFile(users_csv(), filename="foydalanuvchilar.csv"))
    await cq.message.answer_document(
        BufferedInputFile(movies_csv(), filename="kinolar.csv"))
    await cq.answer("📤 Yuklab olindi.")


@router_admin.callback_query(F.data == "a:expi")
async def a_expi(cq: CallbackQuery, state: FSMContext):
    await cq.message.edit_text(
        "📥 CSV fayl yuboring (Excel'dan saqlangan).\n"
        "Birinchi ustun — foydalanuvchi ID lar bo'lishi kerak.")
    await state.set_state(ImpFlow.wait)
    await cq.answer()


@router_admin.message(ImpFlow.wait, F.document)
async def a_impi_run(m: Message, state: FSMContext, bot: Bot):
    try:
        raw = await bot.download(m.document.file_id)
        text = raw.read().decode("utf-8-sig", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        added = 0
        for i, row in enumerate(reader):
            if not row:
                continue
            cell = row[0].strip()
            if i == 0 and not cell.lstrip("-").isdigit():
                continue
            if cell.lstrip("-").isdigit():
                db.user(int(cell))
                added += 1
        db.save()
        await m.answer(f"✅ {added} ta foydalanuvchi import qilindi.", reply_markup=admin_kb())
    except Exception:
        await m.answer("❌ Faylni o'qib bo'lmadi. Qayta yuboring.")
        return
    await state.clear()


# ---- Premium panel ----

@router_admin.callback_query(F.data == "a:prem")
async def a_prem(cq: CallbackQuery):
    vips = [(uid, u) for uid, u in db.users.items() if is_vip(int(uid))]
    text = (f"💎 <b>Premium</b>\n\nFaol VIP foydalanuvchilar: {len(vips)}\n"
            f"1 oylik: {money(db.settings['oy_narx'])} so'm\n"
            f"Umrlik: {money(db.settings['umr_narx'])} so'm\n\n"
            "Narxni o'zgartirish: ⚙️ Sozlamalar")
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 VIP ro'yxati (CSV)", callback_data="a:vcsv")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer()


@router_admin.callback_query(F.data == "a:vcsv")
async def a_vcsv(cq: CallbackQuery):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "name", "username", "vip"])
    for uid, u in db.users.items():
        if is_vip(int(uid)):
            kind = "umrlik" if u.get("vip_life") else (u.get("vip_until") or "")
            w.writerow([uid, u.get("name", ""), u.get("username", ""), kind])
    await cq.message.answer_document(
        BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="vip_foydalanuvchilar.csv"))
    await cq.answer()


# ---- Sozlamalar ----

@router_admin.callback_query(F.data == "a:sett")
async def a_sett(cq: CallbackQuery):
    await cq.message.edit_text(settings_text(), reply_markup=settings_kb())
    await cq.answer()


@router_admin.callback_query(F.data.startswith("stg:"))
async def a_stg_toggle(cq: CallbackQuery):
    key = cq.data.split(":")[1]
    db.settings[key] = not db.settings.get(key, False)
    db.save()
    await cq.message.edit_text(settings_text(), reply_markup=settings_kb())
    await cq.answer()


STE_PROMPTS = {
    "til": "🌐 Standart tilni kiriting (masalan: uz):",
    "support": "📞 Support username kiriting (masalan: @SupportBot):",
    "karta": "💳 Karta raqamini kiriting:",
    "karta_egasi": "👤 Karta egasini kiriting:",
    "oy_narx": "💎 1 oylik narxni kiriting (faqat raqam):",
    "umr_narx": "💎 Umrlik narxni kiriting (faqat raqam):",
}


@router_admin.callback_query(F.data.startswith("ste:"))
async def a_ste_edit(cq: CallbackQuery, state: FSMContext):
    key = cq.data.split(":")[1]
    await state.update_data(key=key)
    await cq.message.edit_text(STE_PROMPTS[key])
    await state.set_state(SetFlow.value)
    await cq.answer()


@router_admin.message(SetFlow.value, F.text)
async def a_ste_save(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data["key"]
    val = m.text.strip()
    if key in ("oy_narx", "umr_narx"):
        if not val.isdigit():
            await m.answer("❌ Faqat raqam kiriting:")
            return
        db.settings[key] = int(val)
    else:
        db.settings[key] = val
    db.save()
    await state.clear()
    await m.answer("✅ Saqlandi.", reply_markup=admin_kb())


# ---- Zaxira nuska ----

@router_admin.callback_query(F.data == "a:back")
async def a_backup_menu(cq: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Zahira olish", callback_data="back:send")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")],
    ])
    text = ("💾 <b>Zaxira nuska</b>\n\n"
            "• <b>Zaxira olish</b> — barcha ma'lumot JSON ko'rinishida zaxira kanalga yuboriladi.\n"
            "• <b>Tiklash</b> — zaxira kanaldagi .json faylni shu botga yuboring, "
            "ma'lumotlar avtomatik tiklanadi.")
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


@router_admin.callback_query(F.data == "back:send")
async def a_backup_send(cq: CallbackQuery, bot: Bot):
    data = json.dumps(db.to_dict(), ensure_ascii=False, indent=1).encode("utf-8")
    fname = f"zahira_{datetime.now():%Y%m%d_%H%M%S}.json"
    doc = BufferedInputFile(data, filename=fname)
    sent = False
    if BACKUP_CHANNEL_ID:
        try:
            await bot.send_document(BACKUP_CHANNEL_ID, doc, caption=f"💾 Zaxira nuska\n🕒 {now_iso()}")
            sent = True
        except Exception:
            sent = False
    if not sent:
        doc2 = BufferedInputFile(json.dumps(db.to_dict(), ensure_ascii=False, indent=1).encode("utf-8"),
                                 filename=fname)
        await cq.message.answer_document(doc2, caption="💾 Zaxira nuska (kanalga yuborilmadi, env tekshiring)")
    await cq.answer("💾 Zaxira kanalga yuborildi." if sent else "⚠️ Zaxira kanalga yuborilmadi.")
    db.save()


@router_admin.message(F.document, StateFilter(None))
async def a_restore(m: Message, bot: Bot):
    if not (m.document.file_name or "").endswith(".json"):
        return
    try:
        raw = await bot.download(m.document.file_id)
        d = json.loads(raw.read().decode("utf-8"))
        db.load_dict(d)
        db.save()
        await m.answer(
            f"✅ Zaxira tiklandi!\n"
            f"👥 Foydalanuvchilar: {len(db.users)}\n"
            f"🎬 Kinolar: {len(db.movies)}\n"
            f"📁 Qismlar: {sum(len(mv['parts']) for mv in db.movies.values())}",
            reply_markup=admin_kb(),
        )
    except Exception:
        await m.answer("❌ Fayl o'qilmadi. To'g'ri zaxira faylini yuboring.")


# ---- Bekor ----

@router_user.message(Command("bekor"))
@router_admin.message(Command("bekor"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ Bekor qilindi.", reply_markup=main_kb(m.from_user.id))


@router_user.callback_query(F.data == "noop")
async def noop(cq: CallbackQuery):
    await cq.answer()


# ---------------- ishga tushirish ----------------

async def main():
    print("[4/6] Local ma'lumot yuklanmoqda...", flush=True)
    db.load_local()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramUnauthorizedError:
        fail("BOT_TOKEN noto'g'ri. BotFather dan yangi token olib qo'ying.")
    print("[5/6] Telegram bilan bog'landik. Polling boshlanmoqda...", flush=True)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "✅ Bot ishga tushdi.")
        except Exception:
            pass
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        tb = traceback.format_exc()
        print(f"Bot xato bilan to'xtadi:\n{tb}", flush=True)
        sys.exit(1)
