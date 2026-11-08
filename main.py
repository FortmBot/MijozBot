import asyncio
import csv
import io
import json
import os
import re
import sys
import threading
import traceback
import unicodedata
import zipfile
from datetime import datetime, timedelta
from html import escape as he

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

print("[1/7] main.py yuklandi, env tekshirilmoqda...", flush=True)


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
print(f"[2/7] Env OK. Adminlar: {len(ADMIN_IDS)} ta, port: {PORT}", flush=True)

from aiohttp import web


def _health_page(request):
    return web.Response(text="ok")


def _run_health():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    app.router.add_get("/", _health_page)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    loop.run_until_complete(site.start())
    loop.run_forever()


threading.Thread(target=_run_health, daemon=True).start()
print(f"[3/7] Port {PORT} ochildi (health-server)", flush=True)

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, ChatJoinRequest,
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
    "admin_contact": "",
    "texnik": False,
}

BTN_CONTINUE = "▶️ Davom ettirish"
BTN_SEARCH = "🔎 Qidiruv"
BTN_FAVS = "❤️ Sevimlilarim"
BTN_ACCOUNT = "👤 Hisobim"
BTN_MUNDARIJA = "📖 Mundarija"
BTN_ORDER = "🛒 Buyurtma qilish"
BTN_COMPLAINT = "📝 Shikoyat"
BTN_CONTACT = "📞 Admin bilan bog'lanish"
BTN_PREMIUM = "💎 Premium"
BTN_ADMIN = "👑 Admin panel"

MENU_TEXTS = {
    BTN_CONTINUE, BTN_SEARCH, BTN_FAVS, BTN_ACCOUNT, BTN_MUNDARIJA,
    BTN_ORDER, BTN_COMPLAINT, BTN_CONTACT, BTN_PREMIUM, BTN_ADMIN,
}

SUB_TEXT = ("❌ <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling.</b>\n\n"
            "Obuna bo'lgach «✅ Obunani tekshirish» tugmasini bosing.\n"
            "Yoki 💎 VIP oling va obunasiz foydalaning.")

TEXNIK_TEXT = ("🛠 <b>Texnik ishlar</b>\n\n"
               "Bot hozir yangilanmoqda. Iltimos, birozdan keyin urinib ko'ring.\n"
               "Noqulaylik uchun uzr so'raymiz. 🙏")

PURGE_TEXT = ("⚠️ <b>Diqqat!</b>\n\n"
              "Siz majburiy obunadagi kanaldan chiqib ketganingiz sababli "
              "chatdagi kinolar o'chirildi.\n\n"
              "Qayta foydalanish uchun kanallarga obuna bo'ling yoki 💎 VIP oling.")

EP_PER_PAGE = 30
LIST_PER_PAGE = 10


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def money(n):
    return f"{int(n):,}".replace(",", " ")


def blank_data():
    return {
        "users": {},
        "movies": {},
        "channels": [],
        "settings": dict(DEFAULT_SETTINGS),
        "receipts": {},
        "complaints": [],
        "banned": [],
        "admins": {},
        "orders": [],
        "join_requests": {},
        "groups": {},
        "rid": 0,
        "cid": 0,
        "oid": 0,
    }


class DB:
    def __init__(self):
        d = blank_data()
        self.users = d["users"]
        self.movies = d["movies"]
        self.channels = d["channels"]
        self.settings = d["settings"]
        self.receipts = d["receipts"]
        self.complaints = d["complaints"]
        self.banned = d["banned"]
        self.admins = d["admins"]
        self.orders = d["orders"]
        self.join_requests = d["join_requests"]
        self.groups = d["groups"]
        self._rid = 0
        self._cid = 0
        self._oid = 0

    def to_dict(self):
        return {
            "users": self.users,
            "movies": self.movies,
            "channels": self.channels,
            "settings": self.settings,
            "receipts": self.receipts,
            "complaints": self.complaints,
            "banned": self.banned,
            "admins": self.admins,
            "orders": self.orders,
            "join_requests": self.join_requests,
            "groups": self.groups,
            "rid": self._rid,
            "cid": self._cid,
            "oid": self._oid,
            "saved_at": now_iso(),
        }

    def load_dict(self, d):
        if not isinstance(d, dict):
            raise ValueError("JSON ildizi obyekt bo'lishi kerak")
        self.users = d.get("users", {}) or {}
        self.movies = d.get("movies", {}) or {}
        self.channels = d.get("channels", []) or []
        self.settings = {**DEFAULT_SETTINGS, **(d.get("settings", {}) or {})}
        self.receipts = d.get("receipts", {}) or {}
        self.complaints = d.get("complaints", []) or []
        self.banned = d.get("banned", []) or []
        self.admins = d.get("admins", {}) or {}
        self.orders = d.get("orders", []) or []
        self.join_requests = d.get("join_requests", {}) or {}
        self.groups = d.get("groups", {}) or {}
        self._rid = int(d.get("rid", 0) or 0)
        self._cid = int(d.get("cid", 0) or 0)
        self._oid = int(d.get("oid", 0) or 0)
        for c in self.complaints:
            if not isinstance(c, dict):
                continue
            if "cid" not in c:
                self._cid += 1
                c["cid"] = self._cid
            c.setdefault("status", "wait")
        for o in self.orders:
            if not isinstance(o, dict):
                continue
            if "oid" not in o:
                self._oid += 1
                o["oid"] = self._oid

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
                "favs": [],
                "last_watched": None,
            }
            self.users[str(uid)] = u
        else:
            if name:
                u["name"] = name
            if username:
                u["username"] = username
            u.setdefault("favs", [])
            u.setdefault("last_watched", None)
        return u

    def next_rid(self):
        self._rid += 1
        return self._rid

    def next_cid(self):
        self._cid += 1
        return self._cid

    def next_oid(self):
        self._oid += 1
        return self._oid


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


ADMIN_PERMS = {
    "stat": "📊 Statistika",
    "content": "🎬 Kontent boshqaruvi",
    "bcast": "📢 Xabar yuborish",
    "orders": "🧾 To'lovlar va buyurtmalar",
    "comp": "⚠️ Shikoyatlar",
    "chan": "📣 Kanallar",
    "texnik": "🛠 Texnik ishlar",
    "users": "👥 Foydalanuvchilar / Ban",
    "anal": "📈 Analitika / Import-eksport",
    "prem": "💎 Premium",
    "sett": "⚙️ Sozlamalar",
    "back": "💾 Zaxira",
}


def is_owner(uid) -> bool:
    try:
        return int(uid) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


def is_admin(uid) -> bool:
    return is_owner(uid) or str(uid) in db.admins


def admin_perms(uid):
    if is_owner(uid):
        return set(ADMIN_PERMS) | {"admins"}
    rec = db.admins.get(str(uid)) or {}
    return {p for p in rec.get("perms", []) if p in ADMIN_PERMS}


def has_perm(uid, perm) -> bool:
    return perm in admin_perms(uid)


def all_admin_ids():
    ids = set(ADMIN_IDS)
    for uid in db.admins:
        if str(uid).lstrip("-").isdigit():
            ids.add(int(uid))
    return sorted(ids)


def vip_status(uid):
    u = db.users.get(str(uid), {})
    if u.get("vip_life"):
        return "♾ Umrlik"
    vu = u.get("vip_until")
    if vu:
        try:
            dt = datetime.fromisoformat(vu)
            if dt > datetime.now():
                return f"✅ {dt:%d.%m.%Y %H:%M} gacha"
        except Exception:
            pass
    return "❌ Yo'q"


# ---------------- obunadan chiqqanda media tozalash ----------------

def track_media_msg(uid, message_id):
    """Foydalanuvchiga yuborilgan kino (media) xabarini eslab qoladi."""
    u = db.users.get(str(uid))
    if not u or not message_id:
        return
    ids = [int(x) for x in (u.get("media_msgs") or [])]
    ids.append(int(message_id))
    u["media_msgs"] = ids[-300:]
    db.save()


async def purge_user_media(bot, uid, notify=True):
    """Obunadan chiqqan foydalanuvchi chatidan bot yuborgan kinolarni o'chiradi.
    Matnlar va tugmalar tegilmaydi. VIP foydalanuvchilar ozod."""
    if is_vip(uid):
        return 0
    u = db.users.get(str(uid))
    if not u:
        return 0
    ids = [int(x) for x in (u.get("media_msgs") or [])]
    if not ids:
        return 0
    deleted = 0
    for mid in ids:
        try:
            await bot.delete_message(uid, mid)
            deleted += 1
        except Exception:
            pass
        await asyncio.sleep(0.04)
    u["media_msgs"] = []
    db.save()
    if notify and deleted:
        try:
            await bot.send_message(uid, PURGE_TEXT)
        except Exception:
            pass
    return deleted


async def media_monitor():
    """Har 10 daqiqada obunadan chiqqanlarni tekshirib, kinolarini o'chiradi."""
    await asyncio.sleep(90)
    while True:
        try:
            if db.channels:
                for uid_str, u in list(db.users.items()):
                    if not u.get("media_msgs"):
                        continue
                    try:
                        uid = int(uid_str)
                    except Exception:
                        continue
                    if is_vip(uid):
                        continue
                    ok, _bad = await check_subscription(bot, uid)
                    if not ok:
                        await purge_user_media(bot, uid)
                    await asyncio.sleep(0.05)
        except Exception:
            pass
        await asyncio.sleep(600)


def rating_of(movie):
    r = movie.get("ratings", {}) or {}
    if not r:
        return "0.0", 0
    return f"{sum(r.values()) / len(r):.1f}", len(r)


_EP_KEYWORDS = (
    r"qism|qismi|episode|epizod|seriya|seria|part|серия|часть",
    r"bolum|bolumu|bolim|bolimi|chapter",
)
_NUM_WORDS = {
    "nol": 0, "bir": 1, "ikki": 2, "uch": 3, "tort": 4, "besh": 5, "olti": 6,
    "yetti": 7, "sakkiz": 8, "toqqiz": 9, "on": 10, "yigirma": 20, "ottiz": 30,
    "qirq": 40, "ellik": 50, "oltmish": 60, "yetmish": 70, "sakson": 80,
    "toqson": 90, "yuz": 100,
    # turkcha
    "iki": 2, "uc": 3, "dort": 4, "bes": 5, "alti": 6, "yedi": 7, "dokuz": 9,
    "yirmi": 20, "otuz": 30, "kirk": 40, "elli": 50, "altmis": 60,
    "yetmis": 70, "seksen": 80, "doksan": 90,
}
_ORD_WORDS = {
    "birinchi": 1, "ikkinchi": 2, "uchinchi": 3, "tortinchi": 4, "beshinchi": 5,
    "oltinchi": 6, "yettinchi": 7, "sakkizinchi": 8, "toqqizinchi": 9,
    "oninchi": 10, "yigirmanchi": 20, "ottizinchi": 30, "qirqinchi": 40,
    "elliginchi": 50, "oltmishinchi": 60, "yetmishinchi": 70,
    "saksoninchi": 80, "toqsoninchi": 90, "yuzinchi": 100,
    "birinci": 1, "ikinci": 2, "ucuncu": 3, "dorduncu": 4, "besinci": 5,
    "altinci": 6, "yedinci": 7, "sekizinci": 8, "dokuzuncu": 9, "onuncu": 10,
}
_TRANS = {
    ord("ö"): "o", ord("ü"): "u", ord("ş"): "s", ord("ç"): "c", ord("ğ"): "g",
    ord("ı"): "i", ord("ə"): "e", ord("’"): "", ord("‘"): "", ord("`"): "",
    ord("'"): "", ord("ʻ"): "", ord("ʼ"): "",
    ord(chr(0x307)): "", ord("–"): "-", ord("—"): "-", ord("−"): "-", ord("‐"): "-", ord("‑"): "-",
}
for _digits in ("٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹"):
    for _idx, _ch in enumerate(_digits):
        _TRANS[ord(_ch)] = str(_idx)


def _norm_caption(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.casefold().translate(_TRANS).split())


def _num_words_sum(tokens):
    total, found = 0, False
    for token in tokens:
        value = _NUM_WORDS.get(token, _ORD_WORDS.get(token))
        if value is None:
            return None
        found = True
        if value == 100:
            total = (total or 1) * 100
        else:
            total += value
    return total if found else None


def _words_phrase_number(phrase):
    tokens = [t for t in phrase.split() if t]
    for start in range(len(tokens)):
        value = _num_words_sum(tokens[start:])
        if value:
            return value
    return None


def _detect_part_number(text):
    """Qism raqamini turli yozuvlarda topadi: '5-qism', '5. BÖLÜM', 'Qism: 5', 'beshinchi qism'."""
    norm = _norm_caption(text)
    if not norm:
        return None
    for keywords in _EP_KEYWORDS:
        for pattern in (r"(\d{1,4})\s*[.\-–—_:#№)\]]*\s*(?:%s)" % keywords,
                        r"(?:%s)\s*[.\-–—_:#№]*\s*(\d{1,4})" % keywords):
            m = re.search(pattern, norm)
            if m and int(m.group(1)) > 0:
                return int(m.group(1))
        for pattern in (r"([a-z]+(?:\s+[a-z]+){0,3})\s*(?:%s)" % keywords,
                        r"(?:%s)\s*([a-z]+(?:\s+[a-z]+){0,3})" % keywords):
            m = re.search(pattern, norm)
            if m:
                value = _words_phrase_number(m.group(1))
                if value:
                    return value
    m = re.search(r"\bs\s*(\d{1,2})\s*[._ -]?\s*e\s*(\d{1,4})\b", norm)
    if m:
        return int(m.group(2))
    m = re.search(r"\b(\d{1,2})\s*[xх]\s*(\d{1,4})\b", norm)
    if m:
        return int(m.group(2))
    cleaned = re.sub(r"\d+\s*[.\-]?\s*(?:fasl|mavsum|season|sezon|saison|сезон)", " ", norm)
    cleaned = re.sub(r"\b(?:19|20)\d{2}\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{3,4}\s*p\b", " ", cleaned)
    m = re.search(r"\b(\d{1,3})\b", cleaned)
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def _detect_season_number(text):
    norm = _norm_caption(text)
    if not norm:
        return None
    m = re.search(r"(\d{1,2})\s*[.\-–—_]*\s*(?:fasl|mavsum|season|sezon|saison|сезон)", norm)
    if m and int(m.group(1)) > 0:
        return int(m.group(1))
    m = re.search(r"\bs\s*(\d{1,2})\s*[._ -]?\s*e\s*\d{1,4}\b", norm)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{1,2})\s*[xх]\s*\d{1,4}\b", norm)
    if m and int(m.group(1)) > 0:
        return int(m.group(1))
    return None


def parse_part(caption):
    """Sarlavhadan (fasl, qism) o'qiydi.
    '5-qism', '5. BÖLÜM', 'Qism: 5', 'beshinchi qism', '3 fasl 5 qism', 's3e5', '3x5'."""
    if not caption:
        return None
    episode = _detect_part_number(caption)
    if not episode:
        return None
    return (_detect_season_number(caption) or 1), episode


def part_label(season, episode, multi_season):
    if multi_season:
        return f"{season} fasl {episode}-qism"
    return f"{episode}-qism"


def ch_link(ch):
    ch = str(ch).strip()
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    if ch.startswith("-100"):
        return f"https://t.me/c/{ch[4:]}"
    return ch


def parse_channel_entry(entry):
    raw = str(entry).strip()
    if "|" in raw:
        cid, link = raw.split("|", 1)
        return cid.strip(), link.strip()
    return raw, ch_link(raw)


def channel_aliases(cid):
    cid = str(cid).strip()
    out = [cid]
    if cid.lstrip("-").isdigit():
        out.append(str(int(cid)))
    return list(dict.fromkeys(out))


def is_instagram_channel(cid, link=""):
    return str(cid).startswith("ig:") or "instagram.com" in str(link)


def instagram_name(cid):
    cid = str(cid)
    return cid[3:] if cid.startswith("ig:") else cid


def seasons_of(mv):
    ss = set()
    for k in (mv.get("parts", {}) or {}):
        try:
            ss.add(int(str(k).split(":")[0]))
        except Exception:
            continue
    return sorted(ss)


def episodes_of(mv, season):
    out = []
    for k in (mv.get("parts", {}) or {}):
        try:
            s, e = str(k).split(":")
            if int(s) == int(season):
                out.append(int(e))
        except Exception:
            continue
    return sorted(set(out))


def user_favs(uid):
    return db.user(uid).setdefault("favs", [])


def admin_label(user):
    if getattr(user, "username", None):
        return f"@{user.username}"
    return f"{getattr(user, 'full_name', '?')} ({getattr(user, 'id', '?')})"


def user_link(user):
    if getattr(user, "username", None):
        return f"@{user.username}"
    return "—"


# ---------------- klaviaturalar ----------------

def main_kb(uid):
    rows = [
        [KeyboardButton(text=BTN_CONTINUE), KeyboardButton(text=BTN_SEARCH)],
        [KeyboardButton(text=BTN_FAVS), KeyboardButton(text=BTN_ACCOUNT)],
        [KeyboardButton(text=BTN_MUNDARIJA)],
        [KeyboardButton(text=BTN_ORDER), KeyboardButton(text=BTN_COMPLAINT)],
        [KeyboardButton(text=BTN_CONTACT), KeyboardButton(text=BTN_PREMIUM)],
    ]
    if is_admin(uid):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_kb():
    b = [
        [InlineKeyboardButton(text="📊 Statistika", callback_data="a:stat"),
         InlineKeyboardButton(text="🎬 Kontent", callback_data="a:content")],
        [InlineKeyboardButton(text="➕ Kontent qo'shish", callback_data="a:addm"),
         InlineKeyboardButton(text="✂️ Qism qo'shish", callback_data="a:parts")],
        [InlineKeyboardButton(text="🗑 O'chirish", callback_data="a:del"),
         InlineKeyboardButton(text="📢 Xabar yuborish", callback_data="a:bcast")],
        [InlineKeyboardButton(text="🧾 To'lovlar", callback_data="a:orders"),
         InlineKeyboardButton(text="🛒 Buyurtmalar", callback_data="a:uorders")],
        [InlineKeyboardButton(text="⚠️ Shikoyatlar", callback_data="a:comp"),
         InlineKeyboardButton(text="📣 Kanallar", callback_data="a:chan")],
        [InlineKeyboardButton(text="🛠 Texnik ishlar", callback_data="a:texnik"),
         InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="a:users")],
        [InlineKeyboardButton(text="🚫 Ban/Unban", callback_data="a:ban"),
         InlineKeyboardButton(text="📈 Analitika", callback_data="a:anal")],
        [InlineKeyboardButton(text="📥 Import", callback_data="a:expi"),
         InlineKeyboardButton(text="💎 Premium", callback_data="a:prem")],
        [InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="a:sett"),
         InlineKeyboardButton(text="💾 Zaxira", callback_data="a:back")],
        [InlineKeyboardButton(text="👑 Adminlar", callback_data="a:admins")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="a:home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=b)


def sub_kb(uid=None):
    rows = []
    ig_ok = (db.users.get(str(uid), {}).get("ig_ok") or []) if uid is not None else []
    for i, entry in enumerate(db.channels):
        cid, link = parse_channel_entry(entry)
        if is_instagram_channel(cid, link):
            mark = "✅" if cid in ig_ok else "📸"
            rows.append([InlineKeyboardButton(text=f"{mark} Instagram: {instagram_name(cid)}",
                                              callback_data=f"igopen:{i}")])
        else:
            rows.append([InlineKeyboardButton(text=f"📢 {cid}", url=link)])
    rows.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="checksub")])
    rows.append([InlineKeyboardButton(text="💎 VIP olish (obunasiz)", callback_data="pr")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def premium_kb():
    if not db.settings["buyurtmalar"]:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Orqaga", callback_data="home")]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 1 oylik sotib olish", callback_data="pay:oy")],
        [InlineKeyboardButton(text="♾ Umrlik sotib olish", callback_data="pay:umr")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="home")],
    ])


def premium_text():
    s = db.settings
    t = (f"💎 <b>Premium obuna</b>\n\n"
         f"🧾 1 oylik: <b>{money(s['oy_narx'])} so'm</b>\n"
         f"♾ Umrlik: <b>{money(s['umr_narx'])} so'm</b>\n\n"
         f"💳 Karta: <code>{s['karta'] or 'kiritilmagan'}</code>\n"
         f"👤 Karta egasi: {s['karta_egasi'] or 'kiritilmagan'}\n\n"
         f"To'lov qiling va chekni (rasm/fayl) shu yerga yuboring. "
         f"Admin tasdiqlagach VIP ochiladi.\n"
         f"💡 VIP foydalanuvchilar majburiy obunasiz ham foydalanadi.")
    if not s["buyurtmalar"]:
        t += "\n\n<i>⚠️ Hozircha buyurtmalar o'chirilgan.</i>"
    return t


def settings_kb():
    s = db.settings

    def mark(x):
        return "✅" if x else "❌"

    contact = s.get("admin_contact") or "kiritilmagan"
    rows = [
        [InlineKeyboardButton(text=f"📝 Baholash: {mark(s['baholash'])}", callback_data="stg:baholash"),
         InlineKeyboardButton(text=f"🛒 Buyurtmalar: {mark(s['buyurtmalar'])}", callback_data="stg:buyurtmalar")],
        [InlineKeyboardButton(text=f"🔒 Himoya: {mark(s['himoya'])}", callback_data="stg:himoya"),
         InlineKeyboardButton(text=f"🌐 Til: {s.get('til', 'uz')}", callback_data="ste:til")],
        [InlineKeyboardButton(text=f"📞 Admin: {contact}"[:60], callback_data="ste:admin_contact"),
         InlineKeyboardButton(text="📞 Support", callback_data="ste:support")],
        [InlineKeyboardButton(text=f"📣 Homiy kanallar: {len(db.channels)}", callback_data="a:chan")],
        [InlineKeyboardButton(text="💳 Karta", callback_data="ste:karta"),
         InlineKeyboardButton(text="👤 Karta egasi", callback_data="ste:karta_egasi")],
        [InlineKeyboardButton(text=f"💎 1 oy: {money(s['oy_narx'])}", callback_data="ste:oy_narx"),
         InlineKeyboardButton(text=f"♾ Umrlik: {money(s['umr_narx'])}", callback_data="ste:umr_narx")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text():
    s = db.settings
    return ("⚙️ <b>Sozlamalar</b>\n\n"
            f"📝 Baholash: {'✅' if s['baholash'] else '❌'}\n"
            f"🛒 Buyurtmalar: {'✅' if s['buyurtmalar'] else '❌'}\n"
            f"🔒 Kontentni himoyalash: {'✅' if s['himoya'] else '❌'}\n"
            f"🌐 Standart til: {s.get('til', 'uz')}\n"
            f"📞 Admin bilan bog'lanish: {s.get('admin_contact') or 'kiritilmagan'}\n"
            f"📞 Support: {s['support'] or 'kiritilmagan'}\n"
            f"💳 Karta raqami: {s['karta'] or 'kiritilmagan'}\n"
            f"👤 Karta egasi: {s['karta_egasi'] or 'kiritilmagan'}\n"
            f"💎 1 oylik narx: {money(s['oy_narx'])} so'm\n"
            f"💎 Umrlik narx: {money(s['umr_narx'])} so'm\n"
            f"📣 Kanal havolasi: {len(db.channels)} ta kanal")


def search_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 Nomi bo'yicha", callback_data="srch:name"),
         InlineKeyboardButton(text="🔢 Kodi bo'yicha", callback_data="srch:code")],
        [InlineKeyboardButton(text="🎬 Kinolar", callback_data="list:kino:0"),
         InlineKeyboardButton(text="📺 Seriallar", callback_data="list:serial:0")],
        [InlineKeyboardButton(text="🎌 Animelar", callback_data="list:anime:0"),
         InlineKeyboardButton(text="🧸 Multfilmlar", callback_data="list:multik:0")],
        [InlineKeyboardButton(text="🎭 Dramalar", callback_data="list:drama:0")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="home")],
    ])


def card_caption(mv):
    avg, cnt = rating_of(mv)
    tariff = "💎 VIP kontent" if mv.get("tariff") == "vip" else "🆓 Bepul"
    year = f"📅 {he(str(mv['year']))}\n" if mv.get("year") else ""
    desc = he(mv.get("desc") or "")
    return (f"{CATS.get(mv.get('category'), '🎬')} <b>{he(str(mv.get('name', '')))}</b>\n"
            f"⭐ {avg} ({cnt})   👁 {mv.get('views', 0)}   📁 {len(mv.get('parts', {}))}\n"
            f"{tariff}\n{year}\n{desc}").strip()


def actions_row(uid, code, season=0, page=0):
    mv = db.movies.get(code) or {}
    fav = code in user_favs(uid)
    sub = str(uid) in mv.get("subs", [])
    return [
        InlineKeyboardButton(
            text="💔 Sevimlilardan olish" if fav else "❤️ Sevimlilarga qo'shish",
            callback_data=f"fav:{code}:{season}:{page}"),
        InlineKeyboardButton(
            text="🔕 Yangi qismlar" if sub else "🔔 Yangi qismlar",
            callback_data=f"sub:{code}:{season}:{page}"),
    ]


def actions_row2(uid, code):
    row = [InlineKeyboardButton(text="⭐ Baholash", callback_data=f"rate:{code}"),
           InlineKeyboardButton(text="⚠️ Xatolik", callback_data=f"report:{code}")]
    if has_perm(uid, "content"):
        row.append(InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"del_media:{code}"))
    return row


def episodes_rows(uid, code, season, page):
    mv = db.movies.get(code) or {}
    eps = episodes_of(mv, season)
    pages = max(1, (len(eps) + EP_PER_PAGE - 1) // EP_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = eps[page * EP_PER_PAGE:(page + 1) * EP_PER_PAGE]
    rows = []
    for i in range(0, len(chunk), 5):
        rows.append([InlineKeyboardButton(text=str(e), callback_data=f"ep:{code}:{season}:{e}")
                     for e in chunk[i:i + 5]])
    if not chunk:
        rows.append([InlineKeyboardButton(text="Qismlar hali qo'shilmagan", callback_data="noop")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"spage:{code}:{season}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"spage:{code}:{season}:{page + 1}"))
        rows.append(nav)
    rows.append(actions_row(uid, code, season, page))
    rows.append(actions_row2(uid, code))
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data=f"back:{code}"),
                 InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")])
    return rows


def card_kb(uid, code):
    mv = db.movies.get(code) or {}
    seasons = seasons_of(mv)
    if len(seasons) == 1:
        return InlineKeyboardMarkup(inline_keyboard=episodes_rows(uid, code, seasons[0], 0))
    rows = []
    for i in range(0, len(seasons), 3):
        rows.append([InlineKeyboardButton(text=f"📺 {s}-fasl", callback_data=f"season:{code}:{s}")
                     for s in seasons[i:i + 3]])
    if not seasons:
        rows.append([InlineKeyboardButton(text="Qismlar hali qo'shilmagan", callback_data="noop")])
    rows.append(actions_row(uid, code))
    rows.append(actions_row2(uid, code))
    rows.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def movie_label(mv, with_cat=False):
    """Kino tugmasi matni: nom yonida kodi ham ko'rinadi."""
    prefix = f"{CATS.get(mv.get('category'), '🎬')} " if with_cat else ""
    suffix = f" ({mv.get('code', '')})"
    name = str(mv.get("name", ""))
    room = max(1, 60 - len(prefix) - len(suffix))
    return f"{prefix}{name[:room]}{suffix}"


def mundarija_text():
    return ("📖 <b>Mundarija</b>\n\n"
            "Kinolar ikki tarifga bo'lingan:\n"
            "🆓 <b>Oddiy tarif</b> — bepul kinolar\n"
            "💎 <b>VIP tarif</b> — faqat VIP foydalanuvchilar uchun\n\n"
            "Har bir kino yonida uning <b>kodi</b> ko'rsatilgan.")


def mundarija_menu_kb():
    oddiy = sum(1 for mv in db.movies.values() if mv.get("tariff") != "vip")
    vip = sum(1 for mv in db.movies.values() if mv.get("tariff") == "vip")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🆓 Oddiy tarif ({oddiy})", callback_data="mdr:oddiy:0")],
        [InlineKeyboardButton(text=f"💎 VIP tarif ({vip})", callback_data="mdr:vip:0")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")],
    ])


def mundarija_list_kb(tariff, page=0, per=LIST_PER_PAGE):
    items = [mv for mv in db.movies.values()
             if ("vip" if mv.get("tariff") == "vip" else "oddiy") == tariff]
    total = len(items)
    pages = max(1, (total + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = items[page * per:(page + 1) * per]
    rows = [[InlineKeyboardButton(text=movie_label(mv, with_cat=True),
                                  callback_data=f"show:{mv['code']}")] for mv in chunk]
    if not chunk:
        rows.append([InlineKeyboardButton(text="Hozircha kino yo'q", callback_data="noop")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"mdr:{tariff}:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"mdr:{tariff}:{page + 1}"))
    if nav:
        rows.append(nav)
    if tariff == "vip":
        rows.append([InlineKeyboardButton(text="💎 VIP olish", callback_data="pr")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="mundarija")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def movies_list_kb(page=0, per=LIST_PER_PAGE):
    items = list(db.movies.values())
    total = len(items)
    chunk = items[page * per:(page + 1) * per]
    rows = []
    for mv in chunk:
        rows.append([InlineKeyboardButton(text=movie_label(mv, with_cat=True),
                                          callback_data=f"show:{mv['code']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"ml:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"ml:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------- obuna tekshirish ----------------

async def check_subscription(bot, uid):
    if is_vip(uid):
        return True, []
    if not db.channels:
        return True, []
    requested = {str(x) for x in db.join_requests.get(str(uid), [])}
    bad = []
    ig_ok = db.users.get(str(uid), {}).get("ig_ok") or []
    for entry in db.channels:
        cid, link = parse_channel_entry(entry)
        if is_instagram_channel(cid, link):
            # Instagram API orqali tekshirilmaydi: foydalanuvchi tugmani bosgani hisobga olinadi.
            if cid not in ig_ok:
                bad.append(cid)
            continue
        aliases = channel_aliases(cid)
        if requested & set(aliases):
            continue
        ok = False
        for alias in aliases:
            try:
                member = await bot.get_chat_member(alias, uid)
                if getattr(member, "status", "") not in ("left", "kicked"):
                    ok = True
                    break
            except Exception:
                ok = True
                break
        if not ok:
            bad.append(cid)
    return (not bad), bad


async def notify_admins(bot, text, exclude=None, kb=None):
    for aid in all_admin_ids():
        if exclude is not None and aid == exclude:
            continue
        try:
            await bot.send_message(aid, text, reply_markup=kb)
        except Exception:
            pass


async def edit_msg(cq, text, kb=None):
    msg = cq.message
    try:
        if msg.photo or msg.video or msg.document:
            await msg.edit_caption(caption=text, reply_markup=kb)
        else:
            await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await msg.answer(text, reply_markup=kb)


async def send_card(bot, chat_id, code, uid, inc=False):
    mv = db.movies.get(code)
    if not mv:
        return
    if inc:
        mv["views"] = int(mv.get("views", 0)) + 1
        db.save()
    caption = card_caption(mv)
    if mv.get("tariff") == "vip" and not is_vip(uid) and not is_admin(uid):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Premium sotib olish", callback_data="pr")],
            [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")]])
        txt = caption + "\n\n🔒 Bu kontent faqat VIP foydalanuvchilar uchun!"
        if mv.get("photo"):
            await bot.send_photo(chat_id, mv["photo"], caption=txt, reply_markup=kb)
        else:
            await bot.send_message(chat_id, txt, reply_markup=kb)
        return
    if mv.get("photo"):
        await bot.send_photo(chat_id, mv["photo"], caption=caption, reply_markup=card_kb(uid, code))
    else:
        await bot.send_message(chat_id, caption, reply_markup=card_kb(uid, code))


async def deliver_episode(bot, chat_id, mv, season, episode):
    key = f"{season}:{episode}"
    val = (mv.get("parts", {}) or {}).get(key)
    if val is None:
        return False
    multi = len(seasons_of(mv)) > 1
    cap = f"{mv['name']} — {part_label(season, episode, multi)}"
    prot = bool(db.settings.get("himoya"))
    if isinstance(val, dict) and val.get("from_chat_id"):
        sent = await bot.copy_message(chat_id, val["from_chat_id"], val["message_id"],
                                      caption=cap, protect_content=prot)
    elif isinstance(val, dict):
        senders = {"document": bot.send_document, "photo": bot.send_photo,
                   "animation": bot.send_animation, "audio": bot.send_audio}
        sender = senders.get(val.get("type"), bot.send_video)
        sent = await sender(chat_id, val["file_id"], caption=cap, protect_content=prot)
    else:
        sent = await bot.send_video(chat_id, val, caption=cap, protect_content=prot)
    if int(chat_id) > 0:
        track_media_msg(chat_id, getattr(sent, "message_id", None))
    return True


# ---------------- middleware ----------------

class GuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        chat = getattr(event, "chat", None) or data.get("event_chat")

        if chat is not None and getattr(chat, "type", "") in ("group", "supergroup"):
            raw = (getattr(event, "text", "") or "").strip()
            cmd = raw.split()[0].split("@")[0].lower() if raw.startswith("/") else ""
            if cmd == "/xabar":
                return await handler(event, data)
            if not db.groups.get(str(chat.id), {}).get("enabled"):
                return
            return await handler(event, data)

        if user is None:
            return await handler(event, data)
        if is_admin(user.id):
            return await handler(event, data)

        if db.settings.get("texnik"):
            if isinstance(event, Message):
                await event.answer(TEXNIK_TEXT)
            else:
                await event.answer("🛠 Botda texnik ishlar ketmoqda. Keyinroq urinib ko'ring.",
                                   show_alert=True)
            return

        db.user(user.id, user.full_name, user.username or "")
        u = db.users[str(user.id)]
        if u.get("banned") or user.id in db.banned:
            if isinstance(event, Message):
                await event.answer("🚫 Siz bloklangansiz.")
            else:
                await event.answer("🚫 Siz bloklangansiz.", show_alert=True)
            return

        need_check = False
        if isinstance(event, CallbackQuery):
            need_check = event.data != "checksub" and not event.data.startswith("igopen:")
        elif isinstance(event, Message):
            txt = (event.text or "").strip().lower()
            need_check = not (txt.startswith("/start") or txt.startswith("/bekor") or txt.startswith("/cancel"))

        if need_check and db.channels:
            bot_ = data["bot"]
            ok, _bad = await check_subscription(bot_, user.id)
            if not ok:
                await purge_user_media(bot_, user.id)
                if isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Avval kanallarga obuna bo'ling!", show_alert=True)
                    try:
                        await bot_.send_message(user.id, SUB_TEXT, reply_markup=sub_kb(user.id))
                    except Exception:
                        pass
                else:
                    await event.answer(SUB_TEXT, reply_markup=sub_kb(user.id))
                return
        return await handler(event, data)


# ---------------- FSM ----------------

class AddContent(StatesGroup):
    code = State()
    name = State()
    photo = State()
    desc = State()
    details = State()
    seasons = State()


class AddEpisodes(StatesGroup):
    code = State()
    season = State()
    batch = State()


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


class RestoreFlow(StatesGroup):
    file = State()


class AdminFlow(StatesGroup):
    add_id = State()


class SearchName(StatesGroup):
    q = State()


class SearchCode(StatesGroup):
    q = State()


class Complaint(StatesGroup):
    text = State()
    confirm = State()


class PayFlow(StatesGroup):
    wait = State()


class OrderFlow(StatesGroup):
    name = State()
    photo = State()
    year = State()


class ReportFlow(StatesGroup):
    text = State()


class AdminFilter(Filter):
    async def __call__(self, obj):
        u = getattr(obj, "from_user", None)
        return u is not None and is_admin(u.id)


_CB_PERM_EXACT = {
    "a:stat": "stat",
    "a:content": "content", "a:movies": "content", "a:addm": "content",
    "a:parts": "content", "a:del": "content",
    "delm_list": "content", "dels_list": "content", "dele_list": "content",
    "a:bcast": "bcast",
    "a:orders": "orders", "a:uorders": "orders",
    "a:comp": "comp", "comp:clr": "comp",
    "a:chan": "chan", "chadd": "chan",
    "a:texnik": "texnik",
    "a:users": "users", "a:ucsv": "users", "a:ban": "users",
    "a:anal": "anal", "a:expe": "anal", "a:expi": "anal",
    "a:prem": "prem", "a:vcsv": "prem",
    "a:sett": "sett",
    "a:back": "back", "back:send": "back", "a:restore": "back",
}

_CB_PERM_PREFIX = {
    "acat:": "content", "atr:": "content",
    "del_media:": "content", "del_media_ok:": "content",
    "delse:": "content", "delsok:": "content",
    "delee:": "content", "delees:": "content", "deleok:": "content",
    "apr:": "orders", "rej:": "orders", "ord_ok:": "orders",
    "cmp_ok:": "comp", "cmp_rej:": "comp",
    "chdel:": "chan",
    "bn:": "users",
    "stg:": "sett", "ste:": "sett",
    "texnik:": "texnik",
}

_STATE_PERM = {
    "AddContent": "content",
    "AddEpisodes": "content",
    "Bcast": "bcast",
    "ChanAdd": "chan",
    "BanFlow": "users",
    "ImpFlow": "anal",
    "SetFlow": "sett",
    "RestoreFlow": "back",
    "AdminFlow": "admins",
}


def _cb_perm(cb):
    if cb in _CB_PERM_EXACT:
        return _CB_PERM_EXACT[cb]
    for pref, perm in _CB_PERM_PREFIX.items():
        if cb.startswith(pref):
            return perm
    return None


class AdminPermMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or not is_admin(user.id):
            return await handler(event, data)
        uid = user.id
        if isinstance(event, CallbackQuery):
            cb = event.data or ""
            if cb == "a:admins" or cb.startswith("adm:"):
                if not is_owner(uid):
                    return await event.answer("⛔ Faqat owner uchun.", show_alert=True)
                return await handler(event, data)
            perm = _cb_perm(cb)
            if perm and not has_perm(uid, perm):
                return await event.answer("⛔ Bu bo'lim uchun ruxsatingiz yo'q.", show_alert=True)
            return await handler(event, data)
        if isinstance(event, Message):
            group = (data.get("raw_state") or "").split(":")[0]
            perm = _STATE_PERM.get(group)
            if perm == "admins":
                if not is_owner(uid):
                    state = data.get("state")
                    if state is not None:
                        await state.clear()
                    return await event.answer("⛔ Faqat owner uchun.")
            elif perm and not has_perm(uid, perm):
                return await event.answer("⛔ Bu bo'lim uchun ruxsatingiz yo'q.")
        return await handler(event, data)


bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
dp.message.outer_middleware(GuardMiddleware())
dp.callback_query.outer_middleware(GuardMiddleware())
print(f"[4/7] Bot va routerlar tayyor. Zaxira kanal: {BACKUP_CHANNEL_ID}", flush=True)


@dp.message(F.text.in_(MENU_TEXTS), F.chat.type == "private")
async def menu_intercept(m: Message, state: FSMContext):
    await state.clear()
    t = (m.text or "").strip()
    if t == BTN_CONTINUE:
        return await menu_continue(m, state)
    if t == BTN_SEARCH:
        return await menu_search(m, state)
    if t == BTN_FAVS:
        return await menu_favorites(m, state)
    if t == BTN_ACCOUNT:
        return await menu_account(m, state)
    if t == BTN_MUNDARIJA:
        return await menu_mundarija(m, state)
    if t == BTN_ORDER:
        return await menu_order(m, state)
    if t == BTN_COMPLAINT:
        return await menu_complaint(m, state)
    if t == BTN_CONTACT:
        return await menu_contact(m, state)
    if t == BTN_PREMIUM:
        return await menu_premium(m, state)
    if t == BTN_ADMIN:
        if is_admin(m.from_user.id):
            return await admin_panel(m)
        return await m.answer("⛔ Faqat admin uchun.", reply_markup=main_kb(m.from_user.id))
    return await m.answer("🏠 Bosh menyu", reply_markup=main_kb(m.from_user.id))


router_user = Router()
router_admin = Router()
router_admin.message.filter(AdminFilter())
router_admin.callback_query.filter(AdminFilter())
router_admin.message.outer_middleware(AdminPermMiddleware())
router_admin.callback_query.outer_middleware(AdminPermMiddleware())


@router_user.message(Command("bekor", "cancel"))
@router_admin.message(Command("bekor", "cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ Bekor qilindi.", reply_markup=main_kb(m.from_user.id))


# ================= FOYDALANUVCHI =================

@router_user.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    db.user(m.from_user.id, m.from_user.full_name, m.from_user.username or "")
    db.save()
    await m.answer(
        "🎬 <b>Kino botiga xush kelibsiz!</b>\n\n"
        "Kerakli bo'limni pastdagi menyudan tanlang.",
        reply_markup=main_kb(m.from_user.id),
    )


async def menu_continue(m: Message, state: FSMContext):
    uid = m.from_user.id
    u = db.user(uid)
    lw = u.get("last_watched") or {}
    code = lw.get("code")
    if not code or code not in db.movies:
        return await m.answer("▶️ Hozircha hech narsa ko'rmagansiz.\n"
                              "🔎 Qidiruv orqali kino toping.",
                              reply_markup=main_kb(uid))
    mv = db.movies[code]
    season = int(lw.get("season", 1) or 1)
    episode = int(lw.get("episode", 1) or 1)
    await m.answer(f"▶️ <b>Davom ettirish</b>\n"
                   f"🎬 {mv['name']}\n"
                   f"📥 Oxirgi ko'rgan: {part_label(season, episode, len(seasons_of(mv)) > 1)}")
    await send_card(m.bot, m.chat.id, code, uid)


async def menu_search(m: Message, state: FSMContext):
    await m.answer("🔎 <b>Qidiruv</b>\n\nNomi yoki kodi bo'yicha qidiring, "
                   "yoki bo'limni tanlang:", reply_markup=search_menu_kb())


async def menu_favorites(m: Message, state: FSMContext):
    favs = user_favs(m.from_user.id)
    items = [db.movies[c] for c in favs if c in db.movies]
    if not items:
        return await m.answer("❤️ Sevimlilar ro'yxati bo'sh.\n"
                              "Kino sahifasidagi «❤️ Sevimlilarga qo'shish» tugmasini bosing.",
                              reply_markup=main_kb(m.from_user.id))
    rows = [[InlineKeyboardButton(text=f"{CATS.get(mv['category'], '🎬')} {mv['name']}"[:60],
                                  callback_data=f"show:{mv['code']}")] for mv in items[:30]]
    rows.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")])
    await m.answer("❤️ <b>Sevimlilarim:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def menu_account(m: Message, state: FSMContext):
    uid = m.from_user.id
    u = db.user(uid)
    lw = u.get("last_watched") or {}
    last = "—"
    if lw.get("code") in db.movies:
        mv = db.movies[lw["code"]]
        last = f"{mv['name']} — {part_label(int(lw.get('season', 1)), int(lw.get('episode', 1)), len(seasons_of(mv)) > 1)}"
    text = (f"👤 <b>Hisobim</b>\n\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"👤 Ism: {he(m.from_user.full_name)}\n"
            f"🔗 Username: {he(user_link(m.from_user))}\n"
            f"💎 VIP: {vip_status(uid)}\n"
            f"▶️ Oxirgi ko'rgan: {he(last)}\n"
            f"📅 Qo'shilgan: {(u.get('joined') or '')[:10]}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Premium", callback_data="pr"),
         InlineKeyboardButton(text="🛒 Buyurtma qilish", callback_data="order_new")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")]])
    await m.answer(text, reply_markup=kb)


async def menu_mundarija(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(mundarija_text(), reply_markup=mundarija_menu_kb())


async def menu_order(m: Message, state: FSMContext):
    await state.set_state(OrderFlow.name)
    await state.update_data(order_name="", order_photo="", order_year="")
    await m.answer("🛒 <b>Buyurtma qilish</b>\n\n"
                   "🎬 Qaysi kino/serialni qo'shishimizni xohlaysiz? Nomini yozing:\n"
                   "Bekor qilish: /bekor")


async def menu_complaint(m: Message, state: FSMContext):
    await state.set_state(Complaint.text)
    await state.update_data(complaint_text="")
    await m.answer("📝 <b>Shikoyat</b>\n\n"
                   "Shikoyatingizni batafsil yozing (faqat matn).\n"
                   "Xabar yuborishdan oldin tasdiqlash so'raladi.\n"
                   "Bekor qilish: /bekor")


async def menu_contact(m: Message, state: FSMContext):
    contact = (db.settings.get("admin_contact") or "").strip()
    support = (db.settings.get("support") or "").strip()
    target = contact or support
    if not target:
        return await m.answer("📞 Admin bilan bog'lanish uchun kontakt hali kiritilmagan.",
                              reply_markup=main_kb(m.from_user.id))
    url = target if target.startswith("http") else f"https://t.me/{target.lstrip('@')}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Admin bilan bog'lanish", url=url)],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")]])
    await m.answer("📞 <b>Admin bilan bog'lanish</b>\n\n"
                   "Savol, taklif yoki muammo bo'lsa quyidagi tugma orqali yozing.",
                   reply_markup=kb)


async def menu_premium(m: Message, state: FSMContext):
    await m.answer(premium_text(), reply_markup=premium_kb())


# ---- kino kartasi ----

@router_user.callback_query(F.data.startswith("show:"))
async def cb_show(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    code = cq.data.split(":", 1)[1]
    if code not in db.movies:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    await send_card(cq.bot, cq.message.chat.id, code, cq.from_user.id, inc=True)
    await cq.answer()


@router_user.callback_query(F.data.startswith("back:"))
async def cb_back(cq: CallbackQuery):
    code = cq.data.split(":", 1)[1]
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    await edit_msg(cq, card_caption(mv), card_kb(cq.from_user.id, code))
    await cq.answer()


@router_user.callback_query(F.data.startswith("season:"))
async def cb_season(cq: CallbackQuery):
    _, code, season = cq.data.split(":")
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    rows = episodes_rows(cq.from_user.id, code, int(season), 0)
    await edit_msg(cq, f"📺 <b>{mv['name']}</b> — {season}-fasl\n👇 Qismni tanlang:",
                   InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_user.callback_query(F.data.startswith("spage:"))
async def cb_spage(cq: CallbackQuery):
    _, code, season, page = cq.data.split(":")
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    rows = episodes_rows(cq.from_user.id, code, int(season), int(page))
    await edit_msg(cq, f"📺 <b>{mv['name']}</b> — {season}-fasl\n👇 Qismni tanlang:",
                   InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_user.callback_query(F.data.startswith("ep:"))
async def cb_episode(cq: CallbackQuery, bot: Bot):
    try:
        _, code, season, episode = cq.data.split(":")
    except ValueError:
        await cq.answer("❌ Xato tugma.")
        return
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    if mv.get("tariff") == "vip" and not is_vip(cq.from_user.id) and not is_admin(cq.from_user.id):
        await cq.answer("🔒 Bu kontent faqat VIP uchun!", show_alert=True)
        return
    try:
        await deliver_episode(bot, cq.message.chat.id, mv, int(season), int(episode))
    except Exception as exc:
        await cq.answer(f"❌ Videoni yuborib bo'lmadi ({type(exc).__name__}).", show_alert=True)
        return
    u = db.user(cq.from_user.id)
    u["last_watched"] = {"code": code, "season": int(season),
                         "episode": int(episode), "at": now_iso()}
    db.save()
    if db.settings.get("baholash") and str(cq.from_user.id) not in (mv.get("ratings") or {}):
        stars = [InlineKeyboardButton(text=s, callback_data=f"rt:{code}:{i}")
                 for i, s in enumerate(["1⭐", "2⭐", "3⭐", "4⭐", "5⭐"], start=1)]
        await cq.message.answer("🎬 Kino yoqdimi? Baho bering:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[stars]))
    await cq.answer("🎬")


@router_user.callback_query(F.data.startswith("fav:"))
async def cb_fav(cq: CallbackQuery):
    parts = cq.data.split(":")
    code = parts[1]
    season = int(parts[2]) if len(parts) > 2 else 0
    page = int(parts[3]) if len(parts) > 3 else 0
    if code not in db.movies:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    favs = user_favs(cq.from_user.id)
    if code in favs:
        favs.remove(code)
        msg = "💔 Sevimlilardan olib tashlandi."
    else:
        favs.append(code)
        msg = "❤️ Sevimlilarga qo'shildi."
    db.save()
    try:
        if season > 0:
            kb = InlineKeyboardMarkup(inline_keyboard=episodes_rows(cq.from_user.id, code, season, page))
        else:
            kb = card_kb(cq.from_user.id, code)
        await cq.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cq.answer(msg)


@router_user.callback_query(F.data.startswith("sub:"))
async def cb_sub(cq: CallbackQuery):
    parts = cq.data.split(":")
    code = parts[1]
    season = int(parts[2]) if len(parts) > 2 else 0
    page = int(parts[3]) if len(parts) > 3 else 0
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    subs = mv.setdefault("subs", [])
    if str(cq.from_user.id) in subs:
        subs.remove(str(cq.from_user.id))
        msg = "🔕 Yangi qism xabarlari o'chirildi."
    else:
        subs.append(str(cq.from_user.id))
        msg = "🔔 Yangi qismlar chiqanda xabar beraman."
    db.save()
    try:
        if season > 0:
            kb = InlineKeyboardMarkup(inline_keyboard=episodes_rows(cq.from_user.id, code, season, page))
        else:
            kb = card_kb(cq.from_user.id, code)
        await cq.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cq.answer(msg)


@router_user.callback_query(F.data.startswith("rate:"))
async def cb_rate(cq: CallbackQuery):
    code = cq.data.split(":", 1)[1]
    if code not in db.movies:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    if not db.settings.get("baholash"):
        await cq.answer("📝 Baholash hozir o'chirilgan.", show_alert=True)
        return
    stars = [InlineKeyboardButton(text=s, callback_data=f"rt:{code}:{i}")
             for i, s in enumerate(["1⭐", "2⭐", "3⭐", "4⭐", "5⭐"], start=1)]
    await cq.message.answer("🎬 Baho bering:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[stars]))
    await cq.answer()


@router_user.callback_query(F.data.startswith("rt:"))
async def cb_rating(cq: CallbackQuery):
    _, code, score = cq.data.split(":")
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    mv.setdefault("ratings", {})[str(cq.from_user.id)] = int(score)
    db.save()
    try:
        await cq.message.edit_text("✅ Bahongiz qabul qilindi. Rahmat!")
    except TelegramBadRequest:
        pass
    await cq.answer()


@router_user.callback_query(F.data.startswith("report:"))
async def cb_report(cq: CallbackQuery, state: FSMContext):
    code = cq.data.split(":", 1)[1]
    if code not in db.movies:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    await state.set_state(ReportFlow.text)
    await state.update_data(report_code=code)
    await cq.message.answer("⚠️ Xatolikni yozing (qaysi qism, qanday muammo).\nBekor qilish: /bekor")
    await cq.answer()


@router_user.message(ReportFlow.text, F.text)
async def report_send(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    code = data.get("report_code", "")
    mv = db.movies.get(code) or {}
    text = m.text.strip()
    for aid in [a for a in all_admin_ids() if has_perm(a, "comp")]:
        try:
            await bot.send_message(
                aid,
                f"⚠️ <b>Xatolik xabari</b>\n"
                f"🎬 {he(mv.get('name', code))} (<code>{he(code)}</code>)\n"
                f"👤 {he(m.from_user.full_name)} (<code>{m.from_user.id}</code>)\n"
                f"💬 {he(text)}")
        except Exception:
            pass
    await state.clear()
    await m.answer("✅ Xabar yuborildi. Rahmat!", reply_markup=main_kb(m.from_user.id))


@router_user.callback_query(F.data == "home")
async def cb_home(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.answer("🏠 Bosh menyu", reply_markup=main_kb(cq.from_user.id))
    await cq.answer()


@router_user.callback_query(F.data == "pr")
async def cb_premium(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_msg(cq, premium_text(), premium_kb())
    await cq.answer()


@router_user.callback_query(F.data == "order_new")
async def cb_order_new(cq: CallbackQuery, state: FSMContext):
    await state.set_state(OrderFlow.name)
    await state.update_data(order_name="", order_photo="", order_year="")
    await cq.message.answer("🛒 <b>Buyurtma qilish</b>\n\n"
                            "🎬 Qaysi kino/serialni qo'shishimizni xohlaysiz? Nomini yozing:\n"
                            "Bekor qilish: /bekor")
    await cq.answer()


# ---- qidiruv ----

def category_kb(cat, page):
    items = [mv for mv in db.movies.values() if mv.get("category") == cat]
    per = LIST_PER_PAGE
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = items[page * per:(page + 1) * per]
    rows = [[InlineKeyboardButton(text=movie_label(mv), callback_data=f"show:{mv['code']}")]
            for mv in chunk]
    if not chunk:
        rows.append([InlineKeyboardButton(text="Bo'sh", callback_data="noop")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"list:{cat}:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"list:{cat}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="search")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router_user.callback_query(F.data == "search")
async def cb_search_menu(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_msg(cq, "🔎 <b>Qidiruv</b>\n\nNomi yoki kodi bo'yicha qidiring, "
                       "yoki bo'limni tanlang:", search_menu_kb())
    await cq.answer()


@router_user.callback_query(F.data == "srch:name")
async def cb_search_name(cq: CallbackQuery, state: FSMContext):
    await state.set_state(SearchName.q)
    await cq.message.answer("🔤 Kino nomini yozing:")
    await cq.answer()


@router_user.callback_query(F.data == "srch:code")
async def cb_search_code(cq: CallbackQuery, state: FSMContext):
    await state.set_state(SearchCode.q)
    await cq.message.answer("🔢 Kino kodini yozing (masalan: 101):")
    await cq.answer()


@router_user.callback_query(F.data.startswith("list:"))
async def cb_list(cq: CallbackQuery):
    try:
        _, cat, page = cq.data.split(":")
    except ValueError:
        await cq.answer("❌ Xato tugma.")
        return
    await edit_msg(cq, f"{CATS.get(cat, cat)} — ro'yxat:", category_kb(cat, int(page)))
    await cq.answer()


@router_user.callback_query(F.data.startswith("ml:"))
async def cb_ml(cq: CallbackQuery):
    page = int(cq.data.split(":")[1])
    await edit_msg(cq, "🎬 <b>Barcha kinolar:</b>", movies_list_kb(page))
    await cq.answer()


@router_user.callback_query(F.data == "mundarija")
async def cb_mundarija(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_msg(cq, mundarija_text(), mundarija_menu_kb())
    await cq.answer()


@router_user.callback_query(F.data.startswith("mdr:"))
async def cb_mundarija_list(cq: CallbackQuery):
    try:
        _, tariff, page = cq.data.split(":")
        page = int(page)
    except ValueError:
        await cq.answer("❌ Xato tugma.")
        return
    if tariff not in ("oddiy", "vip"):
        tariff = "oddiy"
    if tariff == "vip":
        title = ("💎 <b>VIP tarif kinolari</b>\n\n"
                 "🔒 Bu kinolar faqat VIP foydalanuvchilar uchun. "
                 "VIP oling va hammasini bemalol ko'ring!")
    else:
        title = "🆓 <b>Oddiy tarif kinolari</b>\n\nBepul kinolar — bemalol tomosha qiling."
    await edit_msg(cq, title, mundarija_list_kb(tariff, page))
    await cq.answer()


@router_user.callback_query(F.data.startswith("mv:"))
async def cb_legacy_mv(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    code = cq.data.split(":", 1)[1]
    if code not in db.movies:
        await cq.answer("❌ Kino topilmadi.", show_alert=True)
        return
    await send_card(cq.bot, cq.message.chat.id, code, cq.from_user.id, inc=True)
    await cq.answer()


@router_user.message(SearchName.q, F.text)
async def search_name_run(m: Message, state: FSMContext):
    q = m.text.strip().lower()
    await state.clear()
    if not q:
        return await m.answer("🔤 Nom bo'sh bo'lmasin. Qayta yozing:",
                              reply_markup=main_kb(m.from_user.id))
    found = [mv for mv in db.movies.values() if q in mv["name"].lower()]
    exact = [mv for mv in found if mv["name"].lower() == q]
    if len(exact) == 1:
        return await send_card(m.bot, m.chat.id, exact[0]["code"], m.from_user.id, inc=True)
    if not found:
        return await m.answer("❌ Hech narsa topilmadi.", reply_markup=main_kb(m.from_user.id))
    rows = [[InlineKeyboardButton(text=f"{CATS.get(mv['category'], '🎬')} {mv['name']}"[:60],
                                  callback_data=f"show:{mv['code']}")] for mv in found[:20]]
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="search")])
    await m.answer(f"🔎 Topildi: {len(found)} ta", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router_user.message(SearchCode.q, F.text)
async def search_code_run(m: Message, state: FSMContext):
    q = m.text.strip().lstrip("#")
    await state.clear()
    mv = db.movies.get(q)
    if not mv:
        ql = q.casefold()
        for code, item in db.movies.items():
            if code.casefold() == ql:
                mv = item
                break
    if not mv:
        return await m.answer("❌ Bunday kodli kino topilmadi.", reply_markup=main_kb(m.from_user.id))
    await send_card(m.bot, m.chat.id, mv["code"], m.from_user.id, inc=True)


# ---- shikoyat ----

@router_user.message(Complaint.text, F.text)
async def complaint_text(m: Message, state: FSMContext):
    text = m.text.strip()
    if len(text) < 3:
        return await m.answer("📝 Shikoyat juda qisqa. Batafsil yozing:")
    await state.update_data(complaint_text=text)
    await state.set_state(Complaint.confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yuborish", callback_data="cmp_send"),
         InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cmp_cancel")]])
    await m.answer(f"📝 <b>Shikoyat matni:</b>\n\n{he(text)}\n\nYuborishni tasdiqlaysizmi?",
                   reply_markup=kb)


@router_user.message(Complaint.confirm)
async def complaint_confirm_fallback(m: Message):
    await m.answer("Iltimos, «✅ Yuborish» yoki «❌ Bekor qilish» tugmasini bosing.\nBekor qilish: /bekor")


@router_user.callback_query(F.data == "cmp_cancel")
async def cb_complaint_cancel(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_msg(cq, "❌ Shikoyat bekor qilindi.")
    await cq.answer()


@router_user.callback_query(F.data == "cmp_send")
async def cb_complaint_send(cq: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    text = (data.get("complaint_text") or "").strip()
    if not text:
        await state.clear()
        await cq.answer("Shikoyat matni topilmadi.", show_alert=True)
        return
    cid = db.next_cid()
    db.complaints.append({"cid": cid, "uid": cq.from_user.id, "text": text,
                          "at": now_iso(), "status": "wait"})
    db.save()
    await state.clear()
    await edit_msg(cq, f"✅ Shikoyatingiz qabul qilindi (#{cid}).\nJavobini kuting.")
    await cq.answer("✅ Yuborildi.")


# ---- to'lov ----

@router_user.callback_query(F.data.startswith("pay:"))
async def pay_start(cq: CallbackQuery, state: FSMContext):
    if not db.settings["buyurtmalar"]:
        await cq.answer("⚠️ Hozircha buyurtmalar o'chirilgan.", show_alert=True)
        return
    tariff = cq.data.split(":")[1]
    await state.set_state(PayFlow.wait)
    await state.update_data(tariff=tariff)
    name = "1 oylik" if tariff == "oy" else "Umrlik"
    await cq.message.answer(
        f"🧾 <b>{name}</b> tarifi tanlandi.\n\n"
        "To'lov qiling va chekni (rasm yoki fayl) shu yerga yuboring.\n"
        "Bekor qilish: /bekor",
    )
    await cq.answer()


@router_user.message(PayFlow.wait, F.photo | F.document)
async def pay_receipt(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    tariff = data.get("tariff", "oy")
    rid = db.next_rid()
    db.receipts[str(rid)] = {"uid": m.from_user.id, "tariff": tariff,
                             "status": "wait", "at": now_iso()}
    db.save()
    name = "1 oylik" if tariff == "oy" else "Umrlik"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"apr:{rid}"),
         InlineKeyboardButton(text="❌ Rad etish", callback_data=f"rej:{rid}")],
    ])
    for aid in [a for a in all_admin_ids() if has_perm(a, "orders")]:
        try:
            await m.copy_to(aid)
            await bot.send_message(
                aid,
                f"🧾 <b>Yangi to'lov</b>\n🆔 {rid}\n👤 {he(m.from_user.full_name)} "
                f"(<code>{m.from_user.id}</code>)\n📦 Tarif: {name}",
                reply_markup=kb,
            )
        except Exception:
            pass
    await state.clear()
    await m.answer("🧾 Chek yuborildi. Admin tasdiqlagach VIP ochiladi. Kuting...",
                   reply_markup=main_kb(m.from_user.id))


# ---- buyurtma ----

@router_user.message(OrderFlow.name, F.text)
async def order_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if len(name) < 2:
        return await m.answer("🎬 Kino nomini to'liqroq yozing:")
    await state.update_data(order_name=name)
    await state.set_state(OrderFlow.photo)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data="order_skip_photo")]])
    await m.answer(f"🖼 <b>{he(name)}</b> uchun poster (rasm) yuboring.\n"
                   "Poster bo'lmasa «⏭ O'tkazib yuborish» tugmasini bosing.", reply_markup=kb)


@router_user.message(OrderFlow.photo, F.photo)
async def order_photo(m: Message, state: FSMContext):
    await state.update_data(order_photo=m.photo[-1].file_id)
    await state.set_state(OrderFlow.year)
    await m.answer("📅 Kino yilini kiriting (masalan: 2024). Bilmasangiz /skip yozing.")


@router_user.message(OrderFlow.photo, F.text)
async def order_photo_text(m: Message, state: FSMContext):
    if m.text.strip().lower() in ("/skip", "skip", "yo'q", "yoq"):
        await state.update_data(order_photo="")
        await state.set_state(OrderFlow.year)
        return await m.answer("📅 Kino yilini kiriting (masalan: 2024). Bilmasangiz /skip yozing.")
    await m.answer("🖼 Iltimos, rasm yuboring yoki «⏭ O'tkazib yuborish» tugmasini bosing.")


@router_user.callback_query(F.data == "order_skip_photo")
async def cb_order_skip_photo(cq: CallbackQuery, state: FSMContext):
    await state.update_data(order_photo="")
    await state.set_state(OrderFlow.year)
    await edit_msg(cq, "📅 Kino yilini kiriting (masalan: 2024). Bilmasangiz /skip yozing.")
    await cq.answer()


@router_user.message(OrderFlow.year, F.text)
async def order_year(m: Message, state: FSMContext, bot: Bot):
    raw = m.text.strip()
    year = "" if raw.lower() in ("/skip", "skip", "yo'q", "yoq") else raw
    data = await state.get_data()
    name = data.get("order_name", "")
    photo = data.get("order_photo", "")
    oid = db.next_oid()
    db.orders.append({"oid": oid, "uid": m.from_user.id, "name": name, "year": year,
                      "photo": photo, "at": now_iso(), "status": "new"})
    db.save()
    await state.clear()
    await m.answer("✅ Buyurtmangiz qabul qilindi. Tez orada javob beramiz.",
                   reply_markup=main_kb(m.from_user.id))


# ---- obuna ----

@router_user.callback_query(F.data.startswith("igopen:"))
async def cb_igopen(cq: CallbackQuery):
    try:
        i = int(cq.data.split(":")[1])
    except ValueError:
        await cq.answer("❌ Xato tugma.")
        return
    if not (0 <= i < len(db.channels)):
        await cq.answer("Kanal topilmadi.", show_alert=True)
        return
    cid, link = parse_channel_entry(db.channels[i])
    if not is_instagram_channel(cid, link):
        await cq.answer()
        return
    u = db.user(cq.from_user.id)
    oks = u.setdefault("ig_ok", [])
    if cid not in oks:
        oks.append(cid)
        db.save()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Instagramga o'tish", url=link)],
        [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="checksub")],
    ])
    await cq.message.answer(
        f"📸 <b>Instagram: @{he(instagram_name(cid))}</b>\n\n"
        "Sahifamizga obuna bo'ling va «✅ Obunani tekshirish» tugmasini bosing.",
        reply_markup=kb)
    await cq.answer("✅ Qabul qilindi.")


@router_user.callback_query(F.data == "checksub")
async def check_sub(cq: CallbackQuery, bot: Bot):
    ok, _bad = await check_subscription(bot, cq.from_user.id)
    if not ok:
        await cq.answer("❌ Hali obuna bo'lmagansiz!", show_alert=True)
        return
    await cq.answer("✅ Rahmat!")
    await cq.message.answer("🏠 Bosh menyu", reply_markup=main_kb(cq.from_user.id))


@router_user.callback_query(F.data == "noop")
async def noop(cq: CallbackQuery):
    await cq.answer()


# ================= ADMIN =================

@router_admin.message(Command("admin"))
async def admin_panel(m: Message, state: FSMContext = None):
    if state is not None:
        await state.clear()
    await m.answer("👑 <b>Admin panel</b>", reply_markup=admin_kb())


@router_admin.callback_query(F.data == "a:home")
async def admin_home(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cq.message.delete()
    except Exception:
        pass
    await cq.message.answer("🏠 Bosh menyu", reply_markup=main_kb(cq.from_user.id))
    await cq.answer()


@router_admin.callback_query(F.data == "a:panel")
async def admin_back(cq: CallbackQuery):
    await edit_msg(cq, "👑 <b>Admin panel</b>", admin_kb())
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
            f"🎬 Kontentlar: {len(db.movies)}\n"
            f"📁 Jami qismlar: {parts_total}\n"
            f"⏳ Kutilayotgan to'lovlar: {pending}\n"
            f"🛒 Buyurtmalar: {len(db.orders)}\n"
            f"📣 Kanallar: {len(db.channels)}\n"
            f"🚫 Bloklangan: {sum(1 for u in db.users.values() if u.get('banned'))}")
    await edit_msg(cq, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer()


@router_admin.callback_query(F.data.in_({"a:content", "a:movies"}))
async def a_content(cq: CallbackQuery):
    if not db.movies:
        await cq.answer("Kontent yo'q.", show_alert=True)
        return
    await edit_msg(cq, "🎬 <b>Barcha kontentlar:</b>", movies_list_kb(0))
    await cq.answer()


# ---- kontent qo'shish ----

@router_admin.callback_query(F.data == "a:addm")
async def a_addm(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[InlineKeyboardButton(text=t, callback_data=f"acat:{k}")] for k, t in CATS.items()]
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
    await edit_msg(cq, "🎬 Qaysi turda kontent qo'shamiz?",
                   InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("acat:"))
async def a_acat(cq: CallbackQuery, state: FSMContext):
    cat = cq.data.split(":")[1]
    await state.update_data(category=cat, photo="", year="", desc="")
    await state.set_state(AddContent.code)
    await edit_msg(cq, f"{CATS.get(cat, cat)}\n\n🔢 Kod kiriting (unikal, masalan: 101):")
    await cq.answer()


@router_admin.message(AddContent.code, F.text)
async def a_code(m: Message, state: FSMContext):
    code = m.text.strip()
    if not code or code in db.movies:
        await m.answer("❌ Kod band yoki noto'g'ri. Boshqa kod kiriting:")
        return
    await state.update_data(code=code)
    await m.answer("📝 Nomi (kino/anime/serial nomi):")
    await state.set_state(AddContent.name)


@router_admin.message(AddContent.name, F.text)
async def a_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(AddContent.photo)
    await m.answer("🖼 Poster (rasm) yuboring yoki /skip yozing:")


@router_admin.message(AddContent.photo, F.photo)
async def a_photo(m: Message, state: FSMContext):
    await state.update_data(photo=m.photo[-1].file_id)
    await state.set_state(AddContent.desc)
    await m.answer("📄 Tavsif yozing yoki /skip:")


@router_admin.message(AddContent.photo, F.text)
async def a_photo_skip(m: Message, state: FSMContext):
    if m.text.strip().lower() in ("/skip", "skip"):
        await state.set_state(AddContent.desc)
        return await m.answer("📄 Tavsif yozing yoki /skip:")
    await m.answer("🖼 Rasm yuboring yoki /skip yozing:")


@router_admin.message(AddContent.desc, F.text)
async def a_desc(m: Message, state: FSMContext):
    val = m.text.strip()
    await state.update_data(desc="" if val.lower() in ("/skip", "skip") else val)
    await state.set_state(AddContent.details)
    await m.answer("📅 Yil va janr (masalan: 2024, jangari) yoki /skip:")


@router_admin.message(AddContent.details, F.text)
async def a_details(m: Message, state: FSMContext):
    raw = m.text.strip()
    year = ""
    if raw.lower() not in ("/skip", "skip"):
        mm = re.search(r"(19|20)\d{2}", raw)
        if mm:
            year = mm.group(0)
        data = await state.get_data()
        desc = data.get("desc", "")
        extra = raw if not year else raw.replace(year, "").strip(" ,.-")
        if extra:
            desc = (desc + "\n" + extra).strip()
        await state.update_data(desc=desc, year=year)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆓 Oddiy (bepul)", callback_data="atr:oddiy"),
         InlineKeyboardButton(text="💎 VIP uchun", callback_data="atr:vip")]])
    await m.answer("💳 Tarifni tanlang:", reply_markup=kb)


@router_admin.callback_query(F.data.startswith("atr:"))
async def a_tariff(cq: CallbackQuery, state: FSMContext):
    tariff = cq.data.split(":")[1]
    await state.update_data(tariff=tariff)
    await state.set_state(AddContent.seasons)
    await edit_msg(cq, "📺 Fasllar sonini kiriting (masalan: 1). Bilmasangiz /skip:")
    await cq.answer()


@router_admin.message(AddContent.seasons, F.text)
async def a_seasons_count(m: Message, state: FSMContext):
    raw = m.text.strip()
    data = await state.get_data()
    total = 0
    if raw.isdigit():
        total = int(raw)
    code = data.get("code")
    db.movies[code] = {
        "code": code,
        "name": data.get("name", code),
        "category": data.get("category", "kino"),
        "desc": data.get("desc", ""),
        "photo": data.get("photo", ""),
        "year": data.get("year", ""),
        "tariff": data.get("tariff", "oddiy"),
        "parts": {},
        "season_total": total,
        "views": 0,
        "ratings": {},
        "subs": [],
        "created": now_iso(),
    }
    db.save()
    await state.clear()
    await m.answer(f"✅ <b>{data.get('name', code)}</b> qo'shildi!\n\n"
                   f"Endi qismlarni qo'shing: Admin panel → ✂️ Qism qo'shish.",
                   reply_markup=admin_kb())


# ---- qism qo'shish ----

@router_admin.callback_query(F.data == "a:parts")
async def a_parts(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddEpisodes.code)
    await edit_msg(cq, "✂️ Qism qo'shiladigan kino kodini yozing:")
    await cq.answer()


@router_admin.message(AddEpisodes.code, F.text)
async def a_ep_code(m: Message, state: FSMContext):
    code = m.text.strip()
    if code not in db.movies:
        await m.answer("❌ Bunday kodli kino yo'q. Qayta kiriting:")
        return
    await state.update_data(code=code)
    await state.set_state(AddEpisodes.season)
    await m.answer("📺 Fasl raqamini kiriting (1, 2, 3 ...).\n"
                   "0 = har bir video sarlavhasidagi fasl/qism avtomatik o'qilsin:")


@router_admin.message(AddEpisodes.season, F.text)
async def a_ep_season(m: Message, state: FSMContext):
    raw = m.text.strip()
    if not raw.isdigit():
        await m.answer("❌ Faqat raqam kiriting (0, 1, 2 ...):")
        return
    season = int(raw)
    await state.update_data(season=season, added=0)
    await state.set_state(AddEpisodes.batch)
    mv = db.movies.get((await state.get_data()).get("code"))
    s_text = "sarlavhadan avtomatik" if season == 0 else f"{season}-fasl"
    await m.answer(
        f"📤 <b>{mv['name'] if mv else ''}</b> — {s_text}.\n\n"
        "Videolarni yuboring. Qism raqami sarlavhada bo'lsin:\n"
        "• <b>5-qism</b>, <b>5. BÖLÜM</b>, <b>Qism: 5</b>, <b>beshinchi qism</b>\n"
        "• <b>3 fasl 5 qism</b>, <b>s3e5</b>, <b>3x5</b>\n\n"
        "Tugatgach /done yozing. Bekor qilish: /bekor")


@router_admin.message(AddEpisodes.batch,
                      F.video | F.document | F.photo | F.animation | F.audio)
async def a_ep_media(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    code = data.get("code", "")
    fixed_season = int(data.get("season", 0) or 0)
    mv = db.movies.get(code)
    if not mv:
        await state.clear()
        return await m.answer("❌ Kino topilmadi.", reply_markup=admin_kb())
    parsed = parse_part(m.caption)
    mtype = ("video" if m.video else "document" if m.document else "photo" if m.photo
             else "animation" if m.animation else "audio")
    fid = (m.photo[-1].file_id if m.photo else
           m.video.file_id if m.video else m.document.file_id if m.document
           else m.animation.file_id if m.animation else m.audio.file_id)
    if parsed:
        season, episode = parsed
        if fixed_season > 0:
            season = fixed_season
    else:
        if fixed_season <= 0:
            return await m.answer("❌ Sarlavhada qism raqami topilmadi.\n"
                                  "Masalan: <b>5-qism</b>, <b>5. BÖLÜM</b>, <b>3 fasl 5 qism</b>")
        season = fixed_season
        existing = episodes_of(mv, season)
        episode = (max(existing) + 1) if existing else 1
    note = ""
    if f"{season}:{episode}" in mv.get("parts", {}):
        current = mv["parts"][f"{season}:{episode}"]
        if fid and isinstance(current, dict) and current.get("file_id") == fid:
            return await m.answer(f"⚠️ Bu media allaqachon {episode}-qism sifatida qo'shilgan.")
        taken = episode
        existing = episodes_of(mv, season)
        episode = (max(existing) + 1) if existing else episode + 1
        note = f"\nℹ️ {taken}-qism band edi — {episode}-qism sifatida saqlandi."
    mv.setdefault("parts", {})[f"{season}:{episode}"] = {"file_id": fid, "type": mtype}
    total_seasons = max(seasons_of(mv) or [0])
    if int(mv.get("season_total", 0) or 0) < total_seasons:
        mv["season_total"] = total_seasons
    db.save()
    await state.update_data(added=int(data.get("added", 0)) + 1)
    label = part_label(season, episode, len(seasons_of(mv)) > 1)
    await m.answer(f"✅ {label} qo'shildi (jami: {len(mv['parts'])}).{note}\n"
                   "Davom eting yoki /done yozing.")
    for suid in mv.get("subs", []):
        try:
            await bot.send_message(int(suid),
                                   f"🔔 <b>{mv['name']}</b> — yangi qism!\n📥 {label} qo'shildi.")
        except Exception:
            pass


@router_admin.message(AddEpisodes.batch, Command("done"))
async def a_ep_done(m: Message, state: FSMContext):
    data = await state.get_data()
    mv = db.movies.get(data.get("code", ""))
    await state.clear()
    await m.answer(f"✅ Tayyor! Qo'shildi: {data.get('added', 0)} ta.\n"
                   f"Jami qismlar: {len(mv['parts']) if mv else 0}.",
                   reply_markup=admin_kb())


@router_admin.message(AddEpisodes.batch, F.text)
async def a_ep_text_hint(m: Message, state: FSMContext):
    await m.answer("📤 Qism sifatida video, rasm, GIF yoki fayl yuboring.\n"
                   "Qism raqami sarlavhada bo'lsin: <b>5-qism</b>, <b>5. BÖLÜM</b>.\n\n"
                   "Tugatish: /done · Bekor qilish: /bekor")


# ---- o'chirish ----

@router_admin.callback_query(F.data == "a:del")
async def a_del_menu(cq: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Kontentni o'chirish", callback_data="delm_list"),
         InlineKeyboardButton(text="📺 Faslni o'chirish", callback_data="dels_list")],
        [InlineKeyboardButton(text="🎞 Qismni o'chirish", callback_data="dele_list")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]])
    await edit_msg(cq, "🗑 <b>O'chirish</b>\n\nNimani o'chirmoqchisiz?", kb)
    await cq.answer()


@router_admin.callback_query(F.data == "delm_list")
async def a_delm_list(cq: CallbackQuery):
    rows = [[InlineKeyboardButton(text=f"{mv['name']} ({code})"[:60],
                                  callback_data=f"del_media:{code}")]
            for code, mv in list(db.movies.items())[:40]]
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:del")])
    await edit_msg(cq, "🎬 Qaysi kontentni o'chiramiz?", InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("del_media:"))
async def a_del_media_confirm(cq: CallbackQuery):
    code = cq.data.split(":", 1)[1]
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("Topilmadi.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Ha, o'chirish", callback_data=f"del_media_ok:{code}")],
        [InlineKeyboardButton(text="« Bekor qilish", callback_data="a:del")]])
    await edit_msg(cq, f"🗑 <b>{mv['name']}</b> butunlay o'chiriladi.\nTasdiqlaysizmi?", kb)
    await cq.answer()


@router_admin.callback_query(F.data.startswith("del_media_ok:"))
async def a_del_media(cq: CallbackQuery):
    code = cq.data.split(":", 1)[1]
    mv = db.movies.pop(code, None)
    db.save()
    await cq.answer(f"🗑 {mv['name'] if mv else code} o'chirildi.")
    await edit_msg(cq, "🗑 O'chirildi.", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:del")]]))


@router_admin.callback_query(F.data == "dels_list")
async def a_dels_list(cq: CallbackQuery):
    rows = [[InlineKeyboardButton(text=f"{mv['name']} ({code})"[:60],
                                  callback_data=f"delse:{code}")]
            for code, mv in list(db.movies.items())[:40]]
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:del")])
    await edit_msg(cq, "📺 Qaysi kontentning faslini o'chiramiz?",
                   InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("delse:"))
async def a_delseasons(cq: CallbackQuery):
    code = cq.data.split(":", 1)[1]
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("Topilmadi.")
        return
    seasons = seasons_of(mv)
    rows = [[InlineKeyboardButton(text=f"🗑 {s}-fasl ({len(episodes_of(mv, s))} qism)",
                                  callback_data=f"delsok:{code}:{s}")] for s in seasons]
    if not rows:
        rows.append([InlineKeyboardButton(text="Fasllar yo'q", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="dels_list")])
    await edit_msg(cq, f"📺 <b>{mv['name']}</b> — faslni tanlang:",
                   InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("delsok:"))
async def a_delseason(cq: CallbackQuery):
    _, code, season = cq.data.split(":")
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("Topilmadi.")
        return
    keys = [k for k in mv["parts"] if k.split(":")[0] == str(season)]
    for k in keys:
        mv["parts"].pop(k, None)
    db.save()
    await cq.answer(f"🗑 {season}-fasl o'chirildi ({len(keys)} qism).")
    await edit_msg(cq, f"🗑 {season}-fasl o'chirildi.", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:del")]]))


@router_admin.callback_query(F.data == "dele_list")
async def a_dele_list(cq: CallbackQuery):
    rows = [[InlineKeyboardButton(text=f"{mv['name']} ({code})"[:60],
                                  callback_data=f"delee:{code}")]
            for code, mv in list(db.movies.items())[:40]]
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:del")])
    await edit_msg(cq, "🎞 Qaysi kontentdan qism o'chiramiz?",
                   InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("delee:"))
async def a_dele_seasons(cq: CallbackQuery):
    code = cq.data.split(":", 1)[1]
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("Topilmadi.")
        return
    seasons = seasons_of(mv)
    rows = [[InlineKeyboardButton(text=f"📺 {s}-fasl", callback_data=f"delees:{code}:{s}")] for s in seasons]
    if not rows:
        rows.append([InlineKeyboardButton(text="Fasllar yo'q", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="dele_list")])
    await edit_msg(cq, f"🎞 <b>{mv['name']}</b> — faslni tanlang:",
                   InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("delees:"))
async def a_dele_episodes(cq: CallbackQuery):
    _, code, season = cq.data.split(":")
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("Topilmadi.")
        return
    eps = episodes_of(mv, int(season))
    rows = []
    for i in range(0, len(eps), 6):
        rows.append([InlineKeyboardButton(text=f"🗑 {e}", callback_data=f"deleok:{code}:{season}:{e}")
                     for e in eps[i:i + 6]])
    if not rows:
        rows.append([InlineKeyboardButton(text="Qismlar yo'q", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="delee:" + code)])
    await edit_msg(cq, f"🎞 <b>{mv['name']}</b> — {season}-fasl qismini tanlang:",
                   InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("deleok:"))
async def a_dele_episode(cq: CallbackQuery):
    _, code, season, episode = cq.data.split(":")
    mv = db.movies.get(code)
    if not mv:
        await cq.answer("Topilmadi.")
        return
    mv["parts"].pop(f"{season}:{episode}", None)
    db.save()
    await cq.answer(f"🗑 {episode}-qism o'chirildi.")
    await edit_msg(cq, f"🗑 {season}-fasl {episode}-qism o'chirildi.",
                   InlineKeyboardMarkup(inline_keyboard=[
                       [InlineKeyboardButton(text="« Orqaga", callback_data="delee:" + code)]]))


# ---- xabar yuborish ----

@router_admin.callback_query(F.data == "a:bcast")
async def a_bcast(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Bcast.content)
    await edit_msg(cq, "📢 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring.\n"
                       "Bekor qilish: /bekor")
    await cq.answer()


@router_admin.message(Bcast.content)
async def a_bcast_run(m: Message, state: FSMContext):
    await state.clear()
    ok, fail = 0, 0
    for uid in list(db.users.keys()):
        try:
            await m.copy_to(int(uid))
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)
    gok = 0
    for gid, rec in list(db.groups.items()):
        if not rec.get("enabled"):
            continue
        try:
            await m.copy_to(int(gid))
            gok += 1
        except Exception:
            pass
        await asyncio.sleep(0.04)
    await m.answer(f"✅ Yuborildi: {ok}\n❌ Xatolik: {fail}\n👥 Guruhlar: {gok}",
                   reply_markup=admin_kb())


# ---- homiy kanallar ----

def channels_kb():
    rows = []
    for i, ch in enumerate(db.channels):
        cid, link = parse_channel_entry(ch)
        if is_instagram_channel(cid, link):
            label = f"❌ 📸 {instagram_name(cid)}"
        else:
            label = f"❌ {cid}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"chdel:{i}")])
    rows.append([InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="chadd")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channels_text():
    lines = []
    for ch in db.channels:
        cid, link = parse_channel_entry(ch)
        if is_instagram_channel(cid, link):
            lines.append(f"• 📸 @{he(instagram_name(cid))} → {he(link)}")
        else:
            lines.append(f"• <code>{he(cid)}</code> → {he(link)}")
    body = "\n".join(lines) if lines else "Hozircha kanal qo'shilmagan."
    return (f"📣 <b>Majburiy obuna kanallari</b>\n\n{body}\n\n"
            "Qo'shish formatlari:\n"
            "• <code>@kanal_username</code>\n"
            "• <code>-1001234567890</code>\n"
            "• <code>-1001234567890|https://t.me/+taklif_havolasi</code> (yopiq kanal)\n"
            "• <code>instagram:@sahifa</code> yoki <code>https://instagram.com/sahifa</code>\n\n"
            "Yopiq kanalda bot admin bo'lsa, qo'shilish so'rovlari ham obuna deb hisoblanadi.\n"
            "ℹ️ Instagram obunasi API orqali tekshirilmaydi — foydalanuvchi tugmani bosgani hisobga olinadi.")


@router_admin.callback_query(F.data == "a:chan")
async def a_chan(cq: CallbackQuery):
    await edit_msg(cq, channels_text(), channels_kb())
    await cq.answer()


@router_admin.callback_query(F.data == "chadd")
async def a_chan_add(cq: CallbackQuery, state: FSMContext):
    await state.set_state(ChanAdd.text)
    await edit_msg(cq, "Kanal yoki Instagram yuboring:\n"
                       "• <code>@kanal_username</code>\n"
                       "• <code>-1001234567890</code>\n"
                       "• <code>-1001234567890|https://t.me/+havola</code>\n"
                       "• <code>instagram:@sahifa</code> yoki <code>https://instagram.com/sahifa</code>")
    await cq.answer()


@router_admin.message(ChanAdd.text, F.text)
async def a_chan_save(m: Message, state: FSMContext):
    raw = m.text.strip()
    ig_user = None
    if raw.lower().startswith(("instagram:", "ig:")):
        ig_user = raw.split(":", 1)[1].strip().lstrip("@")
    elif "instagram.com/" in raw.lower():
        mm = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", raw, re.I)
        if mm:
            ig_user = mm.group(1)
    if ig_user is not None:
        ig_user = ig_user.strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", ig_user):
            await m.answer("❌ Instagram username noto'g'ri. Masalan: instagram:@mening_sahifam")
            return
        cid = f"ig:{ig_user}"
        if any(parse_channel_entry(x)[0] == cid for x in db.channels):
            await m.answer("❌ Bu Instagram allaqachon qo'shilgan.")
            return
        db.channels.append(f"{cid}|https://www.instagram.com/{ig_user}/")
        db.save()
        await state.clear()
        await m.answer(f"✅ Instagram @{ig_user} qo'shildi.\n"
                       "ℹ️ Foydalanuvchi Instagram tugmasini bosganda obuna hisoblanadi.",
                       reply_markup=admin_kb())
        return
    cid = raw.split("|", 1)[0].strip()
    valid = cid.startswith("@") or cid.lstrip("-").isdigit()
    if not valid:
        await m.answer("❌ Noto'g'ri format. Masalan: @kanal, -1001234567890 yoki instagram:@sahifa")
        return
    if any(parse_channel_entry(x)[0] == cid for x in db.channels):
        await m.answer("❌ Bu kanal allaqachon qo'shilgan.")
        return
    db.channels.append(raw)
    db.save()
    await state.clear()
    await m.answer(f"✅ {cid} qo'shildi.", reply_markup=admin_kb())


@router_admin.callback_query(F.data.startswith("chdel:"))
async def a_chan_del(cq: CallbackQuery):
    i = int(cq.data.split(":")[1])
    if 0 <= i < len(db.channels):
        removed = db.channels.pop(i)
        db.save()
        cid, _ = parse_channel_entry(removed)
        await cq.answer(f"❌ {cid} o'chirildi.")
        await edit_msg(cq, channels_text(), channels_kb())
    else:
        await cq.answer("Topilmadi.")


# ---- foydalanuvchilar ----

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
        w.writerow([c, mv["name"], mv.get("category", ""), mv.get("tariff", ""),
                    len(mv.get("parts", {})), mv.get("views", 0)])
    return buf.getvalue().encode("utf-8-sig")


@router_admin.callback_query(F.data == "a:users")
async def a_users(cq: CallbackQuery):
    vip_count = sum(1 for uid in db.users if is_vip(int(uid)))
    last = list(db.users.items())[-5:]
    text = (f"👥 <b>Foydalanuvchilar</b>\n\nJami: {len(db.users)}\nVIP: {vip_count}\n\n"
            f"Oxirgi 5 ta:\n" + "\n".join(f"• {he(u.get('name', '?'))} (<code>{uid}</code>)"
                                          for uid, u in last))
    await edit_msg(cq, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 CSV yuklab olish", callback_data="a:ucsv")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer()


@router_admin.callback_query(F.data == "a:ucsv")
async def a_ucsv(cq: CallbackQuery):
    await cq.message.answer_document(
        BufferedInputFile(users_csv(), filename="foydalanuvchilar.csv"),
        caption="👥 Foydalanuvchilar ro'yxati (Excel'da ochiladi)")
    await cq.answer()


# ---- ban ----

@router_admin.callback_query(F.data == "a:ban")
async def a_ban(cq: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Banlash", callback_data="bn:1"),
         InlineKeyboardButton(text="✅ Bandan chiqarish", callback_data="bn:0")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")],
    ])
    await edit_msg(cq, "🚫 <b>Ban / Unban</b>\nTanlang:", kb)
    await cq.answer()


@router_admin.callback_query(F.data.startswith("bn:"))
async def a_ban_pick(cq: CallbackQuery, state: FSMContext):
    await state.update_data(ban=cq.data.split(":")[1] == "1")
    await state.set_state(BanFlow.uid)
    await edit_msg(cq, "Foydalanuvchi ID sini yozing:")
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
        msg = f"🚫 {uid} bloklandi."
    else:
        u = db.users.get(str(uid))
        if u:
            u["banned"] = False
        msg = f"✅ {uid} bandan chiqarildi."
    db.save()
    await state.clear()
    await m.answer(msg, reply_markup=admin_kb())


# ---- to'lovlar (cheklar) ----

@router_admin.callback_query(F.data == "a:orders")
async def a_orders(cq: CallbackQuery):
    pending = [(rid, r) for rid, r in db.receipts.items() if r.get("status") == "wait"]
    rows = []
    for rid, r in pending:
        name = "1 oylik" if r.get("tariff") == "oy" else "Umrlik"
        rows.append([InlineKeyboardButton(text=f"🆔 {rid} 👤 {r.get('uid')} 📦 {name}",
                                          callback_data="noop")])
        rows.append([InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"apr:{rid}"),
                     InlineKeyboardButton(text="❌ Rad etish", callback_data=f"rej:{rid}")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
    await edit_msg(cq, f"🧾 <b>Kutilayotgan to'lovlar: {len(pending)}</b>",
                   InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


def _admin_msg_base(msg):
    return (getattr(msg, "html_text", None) or getattr(msg, "text", None)
            or getattr(msg, "caption", None) or "")


@router_admin.callback_query(F.data.startswith("apr:"))
async def a_approve(cq: CallbackQuery, bot: Bot):
    rid = cq.data.split(":")[1]
    r = db.receipts.get(rid)
    if not r or r.get("status") != "wait":
        await cq.answer("Allaqachon ko'rilgan.")
        return
    uid = r["uid"]
    u = db.user(uid)
    if r.get("tariff") == "oy":
        u["vip_until"] = (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds")
        u["vip_life"] = False
        label = "1 oylik"
    else:
        u["vip_life"] = True
        label = "umrlik"
    r["status"] = "ok"
    r["handled_by"] = cq.from_user.id
    r["handled_at"] = now_iso()
    db.save()
    try:
        await bot.send_message(uid, f"🎉 Tabriklaymiz! <b>{label}</b> premium obunangiz faollashtirildi. 🎬",
                               reply_markup=main_kb(uid))
    except Exception:
        pass
    base = _admin_msg_base(cq.message)
    try:
        await cq.message.edit_text(base + f"\n\n✅ Tasdiqlandi\n👮 Admin: {admin_label(cq.from_user)}")
    except TelegramBadRequest:
        pass
    await notify_admins(bot, f"🧾 To'lov #{rid} tasdiqlandi.\n"
                             f"👤 <code>{uid}</code>\n"
                             f"👮 Admin: {admin_label(cq.from_user)}",
                        exclude=cq.from_user.id)
    await cq.answer("✅ Tasdiqlandi.")


@router_admin.callback_query(F.data.startswith("rej:"))
async def a_reject(cq: CallbackQuery, bot: Bot):
    rid = cq.data.split(":")[1]
    r = db.receipts.get(rid)
    if not r or r.get("status") != "wait":
        await cq.answer("Allaqachon ko'rilgan.")
        return
    r["status"] = "rej"
    r["handled_by"] = cq.from_user.id
    r["handled_at"] = now_iso()
    db.save()
    try:
        await bot.send_message(
            r["uid"],
            "❌ Afsuski, to'lovingiz tasdiqlanmadi.\n\n"
            "Sababi: chek o'qilmadi yoki to'lov topilmadi. "
            "Qayta yuborishingiz yoki admin bilan bog'lanishingiz mumkin. 🙏",
            reply_markup=main_kb(r["uid"]))
    except Exception:
        pass
    base = _admin_msg_base(cq.message)
    try:
        await cq.message.edit_text(base + f"\n\n❌ Rad etildi\n👮 Admin: {admin_label(cq.from_user)}")
    except TelegramBadRequest:
        pass
    await notify_admins(bot, f"🧾 To'lov #{rid} rad etildi.\n"
                             f"👤 <code>{r['uid']}</code>\n"
                             f"👮 Admin: {admin_label(cq.from_user)}",
                        exclude=cq.from_user.id)
    await cq.answer("❌ Rad etildi.")


# ---- foydalanuvchi buyurtmalari ----

async def _show_uorders(cq: CallbackQuery):
    items = [o for o in db.orders if o.get("status") == "new"]
    if not items:
        await edit_msg(cq, "🛒 <b>Foydalanuvchi buyurtmalari</b>\n\nYangi buyurtmalar yo'q.",
                       InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
        return
    blocks = []
    rows = []
    for o in reversed(items[-15:]):
        blocks.append(f"#{o.get('oid')} 🎬 {he(str(o.get('name')))}"
                      + (f" ({he(str(o.get('year')))})" if o.get("year") else "")
                      + f"\n👤 <code>{o.get('uid')}</code> • {str(o.get('at', ''))[:16]}")
        rows.append([InlineKeyboardButton(text=f"#{o['oid']} ✅ Ko'rildi",
                                          callback_data=f"ord_ok:{o['oid']}")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
    await edit_msg(cq, f"🛒 <b>Yangi buyurtmalar: {len(items)}</b>\n\n" + "\n\n".join(blocks),
                   InlineKeyboardMarkup(inline_keyboard=rows))


@router_admin.callback_query(F.data == "a:uorders")
async def a_uorders(cq: CallbackQuery):
    await _show_uorders(cq)
    await cq.answer()


@router_admin.callback_query(F.data.startswith("ord_ok:"))
async def a_order_seen(cq: CallbackQuery):
    oid = int(cq.data.split(":")[1])
    rec = next((o for o in db.orders if o.get("oid") == oid), None)
    if not rec:
        await cq.answer("Topilmadi.")
        return
    db.orders = [o for o in db.orders if o.get("oid") != oid]
    db.save()
    await cq.answer(f"✅ #{oid} o'chirildi.")
    await _show_uorders(cq)


# ---- shikoyatlar (admin) ----

CMP_APPROVE_TEXT = ("✅ Shikoyatingiz qabul qilindi.\n\n"
                    "Murojaatingiz ko'rib chiqilmoqda. Tez orada xatolik to'g'irlanadi.\n"
                    "Noqulayliklar uchun uzr so'raymiz. 🙏")
CMP_REJECT_TEXT = ("Assalomu alaykum!\n\n"
                   "Murojaatingiz ko'rib chiqildi. Afsuski, bu safar shikoyatni tasdiqlab bo'lmadi.\n"
                   "Savollaringiz bo'lsa, admin bilan bog'lanishingiz mumkin.\n"
                   "Hurmat bilan. 🙏")


async def _show_comp(cq: CallbackQuery):
    items = [c for c in db.complaints if c.get("status") == "wait"]
    if not items:
        await edit_msg(cq, "⚠️ <b>Shikoyatlar</b>\n\nKutilayotgan shikoyatlar yo'q.",
                       InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
        return
    blocks = []
    rows = []
    for c in reversed(items[-10:]):
        blocks.append(f"#{c.get('cid')}\n"
                      f"👤 <code>{c.get('uid')}</code> • {str(c.get('at', ''))[:16]}\n"
                      f"💬 {he(str(c.get('text', '')))[:300]}")
        rows.append([InlineKeyboardButton(text=f"#{c['cid']} ✅ Tasdiqlash",
                                          callback_data=f"cmp_ok:{c['cid']}"),
                     InlineKeyboardButton(text=f"#{c['cid']} ❌ Rad etish",
                                          callback_data=f"cmp_rej:{c['cid']}")])
    rows.append([InlineKeyboardButton(text="🗑 Hammasini tozalash", callback_data="comp:clr")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
    text = f"⚠️ <b>Kutilayotgan shikoyatlar: {len(items)}</b>\n\n" + "\n\n".join(blocks)
    await edit_msg(cq, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router_admin.callback_query(F.data == "a:comp")
async def a_comp(cq: CallbackQuery):
    await _show_comp(cq)
    await cq.answer()


@router_admin.callback_query(F.data.startswith("cmp_ok:"))
async def a_cmp_ok(cq: CallbackQuery, bot: Bot):
    cid = int(cq.data.split(":")[1])
    rec = next((c for c in db.complaints if c.get("cid") == cid), None)
    if not rec or rec.get("status") != "wait":
        await cq.answer("Allaqachon ko'rilgan.")
        return
    uid = rec["uid"]
    db.complaints = [c for c in db.complaints if c.get("cid") != cid]
    db.save()
    try:
        await bot.send_message(uid, CMP_APPROVE_TEXT, reply_markup=main_kb(uid))
    except Exception:
        pass
    await cq.answer(f"✅ #{cid} tasdiqlandi va o'chirildi.")
    await _show_comp(cq)


@router_admin.callback_query(F.data.startswith("cmp_rej:"))
async def a_cmp_rej(cq: CallbackQuery, bot: Bot):
    cid = int(cq.data.split(":")[1])
    rec = next((c for c in db.complaints if c.get("cid") == cid), None)
    if not rec or rec.get("status") != "wait":
        await cq.answer("Allaqachon ko'rilgan.")
        return
    uid = rec["uid"]
    db.complaints = [c for c in db.complaints if c.get("cid") != cid]
    db.save()
    try:
        await bot.send_message(uid, CMP_REJECT_TEXT, reply_markup=main_kb(uid))
    except Exception:
        pass
    await cq.answer(f"❌ #{cid} rad etildi va o'chirildi.")
    await _show_comp(cq)


@router_admin.callback_query(F.data == "comp:clr")
async def a_comp_clr(cq: CallbackQuery):
    db.complaints = []
    db.save()
    await edit_msg(cq, "⚠️ <b>Shikoyatlar</b>\n\nTozalandi.",
                   InlineKeyboardMarkup(inline_keyboard=[
                       [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer("🗑 Tozalandi.")


# ---- texnik ishlar ----

@router_admin.callback_query(F.data == "a:texnik")
async def a_texnik(cq: CallbackQuery):
    # Bo'limga kirish holatni o'zgartirmaydi — faqat joriy holatni ko'rsatadi.
    await _show_texnik(cq)
    await cq.answer()


def _texnik_view():
    on = bool(db.settings.get("texnik", False))
    holat = "🔴 YOQILGAN" if on else "🟢 O'CHIRILGAN"
    text = ("🛠 <b>Texnik ishlar rejimi</b>\n\n"
            "Holat: " + holat + "\n\n"
            + ("Foydalanuvchilar botga yozganda «texnik ishlar ketmoqda» deb javob oladi. "
               "Adminlar va owner botdan normal foydalanadi."
               if on else
               "Bot hozir barcha foydalanuvchilar uchun ochiq."))
    if on:
        action = [InlineKeyboardButton(text="🟢 O'chirish", callback_data="texnik:off")]
    else:
        action = [InlineKeyboardButton(text="🔴 Yoqish", callback_data="texnik:on")]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        action,
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]])
    return text, kb


async def _show_texnik(cq: CallbackQuery):
    text, kb = _texnik_view()
    await edit_msg(cq, text, kb)


@router_admin.callback_query(F.data == "texnik:on")
async def texnik_on(cq: CallbackQuery):
    db.settings["texnik"] = True
    db.save()
    await _show_texnik(cq)
    await cq.answer("🛠 Texnik ishlar yoqildi.")


@router_admin.callback_query(F.data == "texnik:off")
async def texnik_off(cq: CallbackQuery):
    db.settings["texnik"] = False
    db.save()
    await _show_texnik(cq)
    await cq.answer("✅ Texnik ishlar o'chirildi.")


# ---- analitika ----

@router_admin.callback_query(F.data == "a:anal")
async def a_anal(cq: CallbackQuery):
    lines = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).date().isoformat()
        cnt = sum(1 for u in db.users.values() if u.get("joined", "").startswith(day))
        lines.append(f"{day}: {cnt} ta")
    parts_total = sum(len(mv["parts"]) for mv in db.movies.values())
    views_total = sum(mv.get("views", 0) for mv in db.movies.values())
    text = ("📈 <b>Analitika (oxirgi 7 kun)</b>\n\n" + "\n".join(lines) +
            f"\n\n🎬 Kontentlar: {len(db.movies)}\n📁 Qismlar: {parts_total}\n"
            f"👁 Ko'rilgan: {views_total}\n🛒 Buyurtmalar: {len(db.orders)}")
    await edit_msg(cq, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]]))
    await cq.answer()


# ---- eksport / import ----

@router_admin.callback_query(F.data == "a:expe")
async def a_expe(cq: CallbackQuery):
    await cq.message.answer_document(BufferedInputFile(users_csv(), filename="foydalanuvchilar.csv"))
    await cq.message.answer_document(BufferedInputFile(movies_csv(), filename="kinolar.csv"))
    await cq.answer("📤 Yuklab olindi.")


@router_admin.callback_query(F.data == "a:expi")
async def a_expi(cq: CallbackQuery, state: FSMContext):
    await state.set_state(ImpFlow.wait)
    await edit_msg(cq, "📥 CSV fayl yuboring (Excel'dan saqlangan).\n"
                       "Birinchi ustun — foydalanuvchi ID lar bo'lishi kerak.")
    await cq.answer()


@router_admin.message(ImpFlow.wait, F.document)
async def a_impi_run(m: Message, state: FSMContext, bot: Bot):
    try:
        raw = await bot.download(m.document.file_id)
        payload = raw.read() if hasattr(raw, "read") else bytes(raw)
        text = payload.decode("utf-8-sig", errors="ignore")
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


# ---- premium panel ----

@router_admin.callback_query(F.data == "a:prem")
async def a_prem(cq: CallbackQuery):
    vips = [(uid, u) for uid, u in db.users.items() if is_vip(int(uid))]
    text = (f"💎 <b>Premium</b>\n\nFaol VIP foydalanuvchilar: {len(vips)}\n"
            f"1 oylik: {money(db.settings['oy_narx'])} so'm\n"
            f"Umrlik: {money(db.settings['umr_narx'])} so'm\n\n"
            "Narxni o'zgartirish: ⚙️ Sozlamalar")
    await edit_msg(cq, text, InlineKeyboardMarkup(inline_keyboard=[
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


# ---- adminlar (owner) ----

def _admin_perms_text(uid):
    rec = db.admins.get(str(uid)) or {}
    perms = [p for p in rec.get("perms", []) if p in ADMIN_PERMS]
    nm = he(rec.get("name") or str(uid))
    un = f"@{he(rec['username'])}" if rec.get("username") else "—"
    return ("🛡 <b>Admin huquqlari</b>\n\n"
            f"👤 {nm}\n🔗 {un}\n🆔 <code>{uid}</code>\n\n"
            f"✅ Berilgan huquqlar: {len(perms)}/{len(ADMIN_PERMS)}\n"
            "Tugmalar bilan huquqlarni yoqing yoki o'chiring:")


def _admin_perms_kb(uid):
    rec = db.admins.get(str(uid)) or {}
    perms = set(rec.get("perms", []))
    rows = []
    for key, label in ADMIN_PERMS.items():
        mark = "✅" if key in perms else "❌"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}",
                                          callback_data=f"adm:perm:{uid}:{key}")])
    rows.append([InlineKeyboardButton(text="✅ Barchasi", callback_data=f"adm:all:{uid}"),
                 InlineKeyboardButton(text="❌ Hech biri", callback_data=f"adm:none:{uid}")])
    rows.append([InlineKeyboardButton(text="🗑 Adminni o'chirish", callback_data=f"adm:del:{uid}")])
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_admins(cq):
    lines = ["👑 <b>Adminlar boshqaruvi</b>\n", "👑 <b>Ownerlar (to'liq huquq)</b>"]
    for oid in ADMIN_IDS:
        lines.append(f"• <code>{oid}</code>")
    lines.append(f"\n🛡 <b>Adminlar: {len(db.admins)} ta</b>")
    rows = [[InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="adm:add")]]
    for uid, rec in db.admins.items():
        perms = [p for p in rec.get("perms", []) if p in ADMIN_PERMS]
        nm = rec.get("name") or uid
        lines.append(f"• {he(str(nm))} (<code>{uid}</code>) — {len(perms)} huquq")
        rows.append([InlineKeyboardButton(text=f"👤 {nm}"[:40], callback_data=f"adm:view:{uid}")])
    if not db.admins:
        lines.append("Hozircha adminlar yo'q.")
    rows.append([InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")])
    await edit_msg(cq, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router_admin.callback_query(F.data == "a:admins")
async def a_admins(cq: CallbackQuery):
    if not is_owner(cq.from_user.id):
        return await cq.answer("⛔ Faqat owner uchun.", show_alert=True)
    await _show_admins(cq)
    await cq.answer()


@router_admin.callback_query(F.data == "adm:add")
async def adm_add(cq: CallbackQuery, state: FSMContext):
    if not is_owner(cq.from_user.id):
        return await cq.answer("⛔ Faqat owner uchun.", show_alert=True)
    await state.set_state(AdminFlow.add_id)
    await edit_msg(cq, "➕ <b>Admin qo'shish</b>\n\n"
                       "Yangi adminning Telegram ID sini yuboring (faqat raqam).\n"
                       "Masalan: <code>123456789</code>\n\n"
                       "Bekor qilish: /bekor")
    await cq.answer()


@router_admin.message(AdminFlow.add_id, F.text)
async def adm_add_id(m: Message, state: FSMContext, bot: Bot):
    if not is_owner(m.from_user.id):
        await state.clear()
        return await m.answer("⛔ Faqat owner uchun.")
    raw = (m.text or "").strip()
    if raw in ("/bekor", "/cancel"):
        await state.clear()
        return await m.answer("❌ Bekor qilindi.", reply_markup=admin_kb())
    if not (raw.isascii() and raw.isdigit()) or int(raw) <= 0:
        return await m.answer("❌ ID faqat musbat raqam bo'lishi kerak. Masalan: 123456789")
    uid = int(raw)
    if uid in ADMIN_IDS:
        await state.clear()
        return await m.answer("ℹ️ Bu foydalanuvchi allaqachon owner (to'liq huquq).",
                              reply_markup=admin_kb())
    suid = str(uid)
    if suid in db.admins:
        await state.clear()
        return await m.answer("ℹ️ Bu foydalanuvchi allaqachon admin.", reply_markup=admin_kb())
    name, username = "", ""
    try:
        chat = await bot.get_chat(uid)
        name = getattr(chat, "full_name", "") or getattr(chat, "title", "") or ""
        username = getattr(chat, "username", "") or ""
    except Exception:
        pass
    db.admins[suid] = {"perms": [], "name": name, "username": username,
                       "added_by": m.from_user.id, "added_at": now_iso()}
    db.save()
    await state.clear()
    await m.answer(f"✅ Admin qo'shildi: <code>{uid}</code>\nEndi huquqlarni belgilang.",
                   reply_markup=admin_kb())
    await m.answer(_admin_perms_text(suid), reply_markup=_admin_perms_kb(suid))


@router_admin.callback_query(F.data.startswith("adm:view:"))
async def adm_view(cq: CallbackQuery):
    if not is_owner(cq.from_user.id):
        return await cq.answer("⛔ Faqat owner uchun.", show_alert=True)
    uid = cq.data.split(":")[2]
    if uid not in db.admins:
        return await cq.answer("Admin topilmadi.", show_alert=True)
    await edit_msg(cq, _admin_perms_text(uid), _admin_perms_kb(uid))
    await cq.answer()


@router_admin.callback_query(F.data.startswith("adm:perm:"))
async def adm_perm(cq: CallbackQuery):
    if not is_owner(cq.from_user.id):
        return await cq.answer("⛔ Faqat owner uchun.", show_alert=True)
    parts = cq.data.split(":")
    if len(parts) != 4:
        return await cq.answer("❌ Xato tugma.")
    _, _, uid, perm = parts
    rec = db.admins.get(uid)
    if rec is None or perm not in ADMIN_PERMS:
        return await cq.answer("Topilmadi.", show_alert=True)
    perms = rec.setdefault("perms", [])
    if perm in perms:
        perms.remove(perm)
    else:
        perms.append(perm)
    db.save()
    await edit_msg(cq, _admin_perms_text(uid), _admin_perms_kb(uid))
    await cq.answer("✅ O'zgartirildi")


@router_admin.callback_query(F.data.startswith("adm:all:"))
async def adm_all(cq: CallbackQuery):
    if not is_owner(cq.from_user.id):
        return await cq.answer("⛔ Faqat owner uchun.", show_alert=True)
    uid = cq.data.split(":")[2]
    rec = db.admins.get(uid)
    if rec is None:
        return await cq.answer("Admin topilmadi.", show_alert=True)
    rec["perms"] = list(ADMIN_PERMS)
    db.save()
    await edit_msg(cq, _admin_perms_text(uid), _admin_perms_kb(uid))
    await cq.answer("✅ Barcha huquqlar berildi")


@router_admin.callback_query(F.data.startswith("adm:none:"))
async def adm_none(cq: CallbackQuery):
    if not is_owner(cq.from_user.id):
        return await cq.answer("⛔ Faqat owner uchun.", show_alert=True)
    uid = cq.data.split(":")[2]
    rec = db.admins.get(uid)
    if rec is None:
        return await cq.answer("Admin topilmadi.", show_alert=True)
    rec["perms"] = []
    db.save()
    await edit_msg(cq, _admin_perms_text(uid), _admin_perms_kb(uid))
    await cq.answer("❌ Huquqlar olib tashlandi")


@router_admin.callback_query(F.data.startswith("adm:del:"))
async def adm_del(cq: CallbackQuery):
    if not is_owner(cq.from_user.id):
        return await cq.answer("⛔ Faqat owner uchun.", show_alert=True)
    uid = cq.data.split(":")[2]
    if uid in db.admins:
        del db.admins[uid]
        db.save()
    await cq.answer("🗑 Admin o'chirildi.")
    await _show_admins(cq)


# ---- sozlamalar ----

@router_admin.callback_query(F.data == "a:sett")
async def a_sett(cq: CallbackQuery):
    await edit_msg(cq, settings_text(), settings_kb())
    await cq.answer()


@router_admin.callback_query(F.data.startswith("stg:"))
async def a_stg_toggle(cq: CallbackQuery):
    key = cq.data.split(":")[1]
    db.settings[key] = not db.settings.get(key, False)
    db.save()
    await edit_msg(cq, settings_text(), settings_kb())
    await cq.answer()


STE_PROMPTS = {
    "til": "🌐 Standart tilni kiriting (masalan: uz):",
    "support": "📞 Support username kiriting (masalan: @SupportBot):",
    "karta": "💳 Karta raqamini kiriting:",
    "karta_egasi": "👤 Karta egasini kiriting:",
    "oy_narx": "💎 1 oylik narxni kiriting (faqat raqam):",
    "umr_narx": "💎 Umrlik narxni kiriting (faqat raqam):",
    "admin_contact": "📞 Admin bilan bog'lanish uchun username yoki havola kiriting:\n"
                     "Masalan: @admin_username yoki https://t.me/admin_username",
}


@router_admin.callback_query(F.data.startswith("ste:"))
async def a_ste_edit(cq: CallbackQuery, state: FSMContext):
    key = cq.data.split(":")[1]
    if key not in STE_PROMPTS:
        await cq.answer("❌")
        return
    await state.update_data(key=key)
    await state.set_state(SetFlow.value)
    await edit_msg(cq, STE_PROMPTS[key])
    await cq.answer()


@router_admin.message(SetFlow.value, F.text)
async def a_ste_save(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("key", "")
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


# ---- zaxira ----

@router_admin.callback_query(F.data == "a:back")
async def a_backup_menu(cq: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Zaxira olish", callback_data="back:send")],
        [InlineKeyboardButton(text="♻️ Zaxirani tiklash", callback_data="a:restore")],
        [InlineKeyboardButton(text="« Orqaga", callback_data="a:panel")]])
    text = ("💾 <b>Zaxira nuska</b>\n\n"
            "• <b>Zaxira olish</b> — bot <b>bitta JSON</b> fayl yasab zaxira kanalga yuboradi: "
            "foydalanuvchilar (VIP holati, hisobidagi pul), barcha kinolar (kodi, posteri, "
            "qismlari va boshqa ma'lumotlari) shu faylda bo'ladi.\n"
            "• <b>Tiklash</b> — JSON yoki ZIP yuborib, /done yozasiz — zaxiradagi kontentlar "
            "qo'shiladi/yangilanadi, mavjudlari saqlanib qoladi.")
    await edit_msg(cq, text, kb)
    await cq.answer()


@router_admin.callback_query(F.data == "back:send")
async def a_backup_send(cq: CallbackQuery, bot: Bot):
    payload = json.dumps(db.to_dict(), ensure_ascii=False, indent=1).encode("utf-8")
    fname = f"zahira_{datetime.now():%Y%m%d_%H%M%S}.json"
    sent = False
    if BACKUP_CHANNEL_ID:
        try:
            await bot.send_document(BACKUP_CHANNEL_ID,
                                    BufferedInputFile(payload, filename=fname),
                                    caption=f"💾 Zaxira nuska\n🕒 {now_iso()}")
            sent = True
        except Exception:
            sent = False
    if not sent:
        await cq.message.answer_document(
            BufferedInputFile(payload, filename=fname),
            caption="💾 Zaxira nuska (kanalga yuborilmadi, BACKUP_CHANNEL_ID ni tekshiring)")
    db.save()
    await cq.answer("💾 Zaxira kanalga yuborildi." if sent else "⚠️ Zaxira kanalga yuborilmadi.")


def _convert_medias_to_movies(medias):
    cat_map = {"movie": "kino", "series": "serial", "cartoon": "multik",
               "anime": "anime", "drama": "drama"}
    out = {}
    if not isinstance(medias, dict):
        return out
    for code, m in medias.items():
        if not isinstance(m, dict):
            continue
        parts = {}
        seasons = m.get("seasons") or {}
        if isinstance(seasons, dict):
            for s, sd in seasons.items():
                eps = (sd or {}).get("episodes") or {}
                for e, val in eps.items():
                    try:
                        key = f"{int(s)}:{int(e)}"
                    except Exception:
                        continue
                    if isinstance(val, dict):
                        parts[key] = val
                    else:
                        parts[key] = {"file_id": val, "type": "video"}
        cat = m.get("media_type") or m.get("category") or "kino"
        out[str(code)] = {
            "code": str(code),
            "name": m.get("name") or str(code),
            "category": cat_map.get(cat, cat),
            "desc": m.get("description") or m.get("desc") or "",
            "photo": m.get("poster_file_id") or m.get("photo") or "",
            "year": str(m.get("year") or ""),
            "tariff": "vip" if m.get("vip_only") else "oddiy",
            "parts": parts,
            "season_total": len(seasons) if isinstance(seasons, dict) else 0,
            "views": int(m.get("views") or 0),
            "ratings": m.get("ratings") or {},
            "subs": [],
            "created": m.get("created") or now_iso(),
        }
    return out


def _merge_backup_objects(base, incoming):
    if not isinstance(incoming, dict):
        raise ValueError("JSON ildizi obyekt bo'lishi kerak")
    incoming = dict(incoming)
    if "medias" in incoming and "movies" not in incoming:
        incoming["movies"] = _convert_medias_to_movies(incoming.get("medias"))
    if "movies" in incoming and not isinstance(incoming["movies"], dict):
        raise ValueError("movies bo'limi noto'g'ri")
    base.setdefault("movies", {})
    base["movies"].update(incoming.get("movies") or {})
    if isinstance(incoming.get("banned_users"), list):
        base.setdefault("banned", [])
        for item in incoming["banned_users"]:
            if item not in base["banned"]:
                base["banned"].append(item)
    if isinstance(incoming.get("channels"), list):
        base["channels"] = list(dict.fromkeys(
            [c for c in (base.get("channels") or []) if isinstance(c, str)]
            + [c for c in incoming["channels"] if isinstance(c, str)]))
    for key, value in incoming.items():
        if key in ("movies", "medias", "channels"):
            continue
        if key in ("users", "join_requests", "groups", "settings", "receipts") and isinstance(value, dict):
            base.setdefault(key, {}).update(value)
        elif key in ("banned", "banned_users", "complaints", "orders", "admins") and isinstance(value, list):
            base.setdefault(key, [])
            for item in value:
                if item not in base[key]:
                    base[key].append(item)
        else:
            base[key] = value
    return base


@router_admin.callback_query(F.data == "a:restore")
async def a_restore_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(RestoreFlow.file)
    await state.update_data(restore_files=0)
    await edit_msg(cq, "♻️ <b>Zaxirani tiklash</b>\n\n"
                       "Bitta JSON yoki ichida JSON bo'lgan ZIP arxiv yuboring.\n"
                       "Bir nechta faylni ketma-ket yuborib, oxirida /done yozing.\n"
                       "Tiklash mavjud ma'lumotni o'chirmaydi: zaxiradagi kontentlar "
                       "qo'shiladi/yangilanadi, qolganlari saqlanib qoladi.\n"
                       "Bekor qilish: /bekor")
    await cq.answer()


@router_admin.message(RestoreFlow.file, Command("done"))
async def a_restore_done(m: Message, state: FSMContext):
    data = await state.get_data()
    restored = data.get("restore_data")
    count = int(data.get("restore_files", 0) or 0)
    if not restored or not count:
        await m.answer("❌ Hali JSON yuborilmadi. JSON/ZIP yuboring yoki /bekor.")
        return
    before_movies = len(db.movies)
    merged = _merge_backup_objects(db.to_dict(), restored)
    db.load_dict(merged)
    numeric_rids = [int(rid) for rid in db.receipts.keys() if str(rid).isdigit()]
    if numeric_rids:
        db._rid = max(db._rid, max(numeric_rids))
    db.save()
    await state.clear()
    await m.answer(f"✅ Tiklandi. {count} ta fayl birlashtirildi.\n"
                   f"🎬 Kontentlar: {before_movies} → {len(db.movies)}\n"
                   f"👥 Foydalanuvchilar: {len(db.users)}\n"
                   f"📁 Qismlar: {sum(len(mv['parts']) for mv in db.movies.values())}",
                   reply_markup=admin_kb())


@router_admin.message(RestoreFlow.file, F.document)
async def a_restore_file(m: Message, state: FSMContext, bot: Bot):
    fname = (m.document.file_name or "").lower()
    if not (fname.endswith(".json") or fname.endswith(".zip")):
        await m.answer("❌ Faqat .json yoki .zip fayl qabul qilinadi.")
        return
    try:
        raw = await bot.download(m.document.file_id)
        payload = raw.read() if hasattr(raw, "read") else bytes(raw)
        blobs = []
        if fname.endswith(".json"):
            blobs = [payload]
        else:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                for name in archive.namelist():
                    if name.lower().endswith(".json") and not name.endswith("/"):
                        blobs.append(archive.read(name))
            if not blobs:
                await m.answer("❌ ZIP ichidan .json fayl topilmadi.")
                return
        data = await state.get_data()
        base = data.get("restore_data") or blank_data()
        for blob in blobs:
            incoming = json.loads(blob.decode("utf-8-sig"))
            base = _merge_backup_objects(base, incoming)
        count = int(data.get("restore_files", 0) or 0) + len(blobs)
        await state.update_data(restore_data=base, restore_files=count)
        await m.answer(f"✅ {len(blobs)} ta JSON o'qildi. Jami: {count} ta.\n"
                       "Yana fayl yuboring yoki /done yozib yakunlang.")
    except Exception as exc:
        await m.answer(f"❌ Zaxirani o'qib bo'lmadi: {type(exc).__name__}: {str(exc)[:160]}\n"
                       "Fayl bot yaratgan zaxira ekanini tekshiring.")


@router_admin.message(RestoreFlow.file, F.text)
async def a_restore_text(m: Message):
    await m.answer("📄 JSON yoki ZIP yuboring. Yakunlash: /done. Bekor qilish: /bekor.")


# ---- guruhlar ----

@dp.message(Command("xabar"), F.chat.type.in_({"group", "supergroup"}))
async def group_xabar(m: Message):
    if m.from_user is None or not is_admin(m.from_user.id):
        return await m.answer("⛔ Faqat admin uchun.")
    rec = db.groups.setdefault(str(m.chat.id), {"title": m.chat.title or "", "enabled": False})
    rec["enabled"] = not bool(rec.get("enabled"))
    rec["title"] = m.chat.title or rec.get("title", "")
    db.save()
    if rec["enabled"]:
        await m.answer("✅ Bu guruhda bot xabarlari YOQILDI.\nBot admin bo'lib qoladi.")
    else:
        await m.answer("🔕 Bu guruhda bot xabarlari O'CHIRILDI.\nBot admin bo'lib qoladi.")


@dp.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    aliases = db.join_requests.setdefault(str(req.from_user.id), [])
    candidates = [str(req.chat.id)]
    if req.chat.username:
        candidates.append(f"@{req.chat.username}")
    for alias in candidates:
        if alias not in aliases:
            aliases.append(alias)
    db.save()


# ---- zaxira: yakuniy handlerlar ----

def quick_find_movies(query, limit=20):
    q = (query or "").strip().lstrip("#").strip().casefold()
    if not q:
        return []
    for code, mv in db.movies.items():
        if code.casefold() == q:
            return [mv]
    exact = [mv for mv in db.movies.values()
             if (mv.get("name") or "").casefold() == q]
    if exact:
        return exact[:limit]
    return [mv for mv in db.movies.values()
            if q in (mv.get("name") or "").casefold()][:limit]


@router_user.message(F.chat.type == "private")
async def private_fallback(m: Message, state: FSMContext):
    st = await state.get_state()
    if st:
        return await m.answer("❌ Bu qadamda bunday xabar qabul qilinmaydi.\nBekor qilish: /bekor")
    text = (m.text or "").strip()
    if text and not text.startswith("/"):
        found = quick_find_movies(text)
        if len(found) == 1:
            return await send_card(m.bot, m.chat.id, found[0]["code"], m.from_user.id, inc=True)
        if found:
            rows = [[InlineKeyboardButton(text=f"{CATS.get(mv['category'], '🎬')} {mv['name']}"[:60],
                                          callback_data=f"show:{mv['code']}")] for mv in found]
            rows.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")])
            return await m.answer(f"🔎 Topildi: {len(found)} ta",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return await m.answer("❌ Hech narsa topilmadi.\n\nKino kodini yoki nomini qayta yozib ko'ring.",
                              reply_markup=main_kb(m.from_user.id))
    await m.answer("🏠 Bosh menyu", reply_markup=main_kb(m.from_user.id))


dp.include_router(router_admin)
dp.include_router(router_user)


# ---------------- ishga tushirish ----------------

async def main():
    print("[5/7] Local ma'lumot yuklanmoqda...", flush=True)
    db.load_local()
    asyncio.create_task(media_monitor())
    if BACKUP_CHANNEL_ID and not db.movies and not db.users:
        print("[!] Ma'lumot bo'sh — adminlarga zaxira eslatmasi yuboriladi.", flush=True)
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "⚠️ <b>Bot ma'lumoti topilmadi.</b>\n"
                    "Render qayta ishga tushgan bo'lishi mumkin.\n\n"
                    "Tiklash: zaxira kanalidagi eng yangi JSON faylni shu yerga "
                    "forward qiling → /done yozing.\n"
                    "(Admin panel → 💾 Zaxira → ♻️ Zaxirani tiklash)")
            except Exception:
                pass
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramUnauthorizedError:
        fail("BOT_TOKEN noto'g'ri. BotFather dan yangi token olib qo'ying.")
    print("[6/7] Telegram bilan bog'landik. Polling boshlanmoqda...", flush=True)
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
