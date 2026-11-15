import asyncio
import json
import logging
import os
import re
import unicodedata
import hashlib
import difflib
import time
import zipfile
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html import escape as _esc
from urllib.parse import quote
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton as TelegramInlineKeyboardButton, ReplyKeyboardRemove, BufferedInputFile, BotCommand
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest


# Tugma yozuvlari uchun foydalanuvchi tanlagan small-caps uslubi.
_SMALL_CAPS = str.maketrans({
    "a":"ᴀ", "b":"ʙ", "c":"ᴄ", "d":"ᴅ", "e":"ᴇ", "f":"ғ", "g":"ɢ",
    "h":"ʜ", "i":"ɪ", "j":"ᴊ", "k":"ᴋ", "l":"ʟ", "m":"ᴍ", "n":"ɴ",
    "o":"ᴏ", "p":"ᴘ", "q":"ǫ", "r":"ʀ", "s":"s", "t":"ᴛ", "u":"ᴜ",
    "v":"ᴠ", "w":"ᴡ", "x":"x", "y":"ʏ", "z":"ᴢ"
})
def small_caps(value):
    return str(value).translate(_SMALL_CAPS)

# Premium (custom) emoji ID larni Telegram faqat Fragment'da username sotib olgan
# botlarda qabul qiladi. Bir marta aniqlangach holat butun jarayonga qo'llanadi.
_CUSTOM_EMOJI_STATE = {"allowed": True}


def _button_emoji_value(callback_data):
    """Owner panelida tugmaga biriktirilgan premium emoji ID yoki oddiy emoji."""
    try:
        saved = load_data().get("settings", {}).get("button_emojis", {})
        return str(saved.get(str(callback_data)) or "").strip()
    except Exception:
        return ""


def _apply_button_emoji(button, value):
    if not value or not _CUSTOM_EMOJI_STATE["allowed"]:
        return button
    if value.isdigit():
        # icon_custom_emoji_id premium emojini tugma oldiga qo'yadi.
        try:
            button.icon_custom_emoji_id = value
        except Exception:
            pass
        return button
    if value not in button.text:
        button.text = (value + " " + button.text).strip()
    return button


def _strip_custom_emoji_icons(markup):
    """Telegram premium emojini rad etsa, tugmalardan ikonkani olib tashlaydi."""
    try:
        for row in markup.inline_keyboard:
            for button in row:
                if getattr(button, "icon_custom_emoji_id", None):
                    button.icon_custom_emoji_id = None
    except Exception:
        pass
    return markup


def _is_custom_emoji_error(exc):
    text = str(exc).lower()
    return "emoji" in text or "button_icon" in text


def InlineKeyboardButton(*, text, **kwargs):
    """Tugma sozlamalarini har tugmada diskdan o'qimasdan, xotiradagi ma'lumotdan oladi."""
    _cb = str(kwargs.get("callback_data", ""))
    try:
        _cfg = load_data().get("settings", {})
        _custom_text = _cfg.get("button_texts", {}).get(_cb)
        if _custom_text:
            text = _custom_text
        _colors = _cfg.get("button_colors", {})
        _group = _button_color_group(_cb, str(text))
        # Aniq guruh sozlamasi doim "barcha tugmalar" sozlamasidan ustun.
        # Guruh uchun "default" tanlansa ham, umumiy rangga qaytib ketmasin.
        if _group in _colors:
            _color = _colors[_group]
        else:
            _color = _colors.get("all")
        if _color in ("primary", "success", "danger"):
            kwargs["style"] = _color
        elif _color == "default":
            kwargs.pop("style", None)
    except Exception:
        pass
    button = TelegramInlineKeyboardButton(text=small_caps(text), **kwargs)
    return _apply_button_emoji(button, _button_emoji_value(_cb))

def _button_color_group(callback_data, text=""):
    cb = str(callback_data or "")
    # Katalog sahifalash tugmalari alohida sozlanadi; episode raqamlari bundan mustasno.
    if cb == "page_info":
        return "page_indicator"
    if cb.startswith(("season_page:", "media_page:")):
        if "⬅" in str(text): return "page_left"
        if "➡" in str(text): return "page_right"
    # Also catch pager buttons if their callback data changes in a future menu.
    if cb == "page_info" or re.fullmatch(r"\d+\s*/\s*\d+", str(text).strip()):
        return "page_indicator"
    if cb in ("home", "admin_home") or "Orqaga" in text or "Asosiy menyu" in text:
        return "back"
    if cb.startswith("episode:") and "Keyingi qism" in str(text):
        return "next_episode"
    if cb.startswith(("episode:", "show:", "season_show:", "catalog_show:")) or cb in ("list", "movie_list", "continue_watching"):
        return "content"
    if cb in ("search", "search_name", "search_menu") or "qidirish" in text.casefold():
        return "search"
    return "other"

# ================== SOZLAMALAR ==================
# Render Environment Variables: BOT_TOKEN, OWNER_ID, ADMIN_ID.
# Qiymatlarda bosh/oxir bo'sh joy bo'lsa tozalanadi.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")

# ADMIN_ID bitta ID yoki vergul bilan ajratilgan bir nechta ID bo'lishi mumkin.
ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_IDS = []
for _admin_value in ADMIN_IDS_RAW.split(","):
    _admin_value = _admin_value.strip()
    if _admin_value.isascii() and _admin_value.isdecimal():
        _parsed_admin = int(_admin_value)
        if _parsed_admin > 0 and _parsed_admin not in ADMIN_IDS:
            ADMIN_IDS.append(_parsed_admin)

OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
if OWNER_ID_RAW.isascii() and OWNER_ID_RAW.isdecimal() and int(OWNER_ID_RAW) > 0:
    OWNER_ID = int(OWNER_ID_RAW)
else:
    # OWNER_ID bo'sh qolgan bo'lsa, ADMIN_ID dagi birinchi ID egasi sifatida olinadi.
    OWNER_ID = ADMIN_IDS[0] if ADMIN_IDS else 0
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
BACKUP_CHANNEL_ID = os.getenv("BACKUP_CHANNEL_ID", "").strip()
ADMIN_CONTACT = "@AnimBot_Admin"
# media postlari saqlanadigan kanal ID sini STORAGE_CHANNEL_ID environment variable orqali berish mumkin.
CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID", "").strip()

def get_backup_channel_id():
    """Owner panelidagi kanal sozlamasi ENV qiymatidan ustun turadi."""
    return str(load_data().get("settings", {}).get("backup_channel_id") or BACKUP_CHANNEL_ID or "").strip()
# =================================================

logging.basicConfig(level=logging.INFO)
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi. Render > Service > Environment bo'limida "
        "BOT_TOKEN nomli variable yarating va BotFather tokenini kiriting."
    )
def small_caps_html(value):
    """HTML teglarini, entitylarni va code/pre ichidagi texnik matnni buzmasdan bezaydi."""
    value = str(value)
    tokens = re.split(r"(<[^>]+>|&(?:amp|lt|gt|quot|#39|#\d+|#x[0-9a-fA-F]+);)", value)
    output = []
    in_code = False
    for token in tokens:
        if not token:
            continue
        if token.startswith("<") and token.endswith(">"):
            tag = re.match(r"</?([a-zA-Z0-9]+)", token)
            if tag and tag.group(1).lower() in {"code", "pre"}:
                if token.startswith("</"):
                    in_code = False
                elif not token.endswith("/>"):
                    in_code = True
            output.append(token)
        elif token.startswith("&") and token.endswith(";"):
            output.append(token)
        elif in_code:
            output.append(token)
        else:
            # Slash commands (/start, /done) va @username oddiy shrift bo'lib qoladi.
            pieces = re.split(r"(@[A-Za-z0-9_]{3,}|/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?)", token)
            for piece in pieces:
                if not piece:
                    continue
                if re.fullmatch(r"(?:@[A-Za-z0-9_]{3,}|/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?)", piece):
                    output.append(piece)
                else:
                    output.append(small_caps(piece))
    return "".join(output)

class StyledBot(Bot):
    async def _call_with_emoji_fallback(self, method, *args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except TelegramBadRequest as exc:
            if _CUSTOM_EMOJI_STATE["allowed"] and _is_custom_emoji_error(exc):
                _CUSTOM_EMOJI_STATE["allowed"] = False
                logging.warning("Premium emoji tugmalarda ishlamadi, emojisiz qayta yuborilmoqda: %s", exc)
                _strip_custom_emoji_icons(kwargs.get("reply_markup"))
                return await method(*args, **kwargs)
            raise

    async def send_message(self, chat_id, text, *args, **kwargs):
        return await self._call_with_emoji_fallback(super().send_message, chat_id, small_caps_html(text), *args, **kwargs)

    async def send_photo(self, chat_id, photo, *args, **kwargs):
        if kwargs.get("caption") is not None:
            kwargs["caption"] = small_caps_html(kwargs["caption"])
        return await self._call_with_emoji_fallback(super().send_photo, chat_id, photo, *args, **kwargs)

    async def edit_message_text(self, text, *args, **kwargs):
        return await self._call_with_emoji_fallback(super().edit_message_text, small_caps_html(text), *args, **kwargs)

    async def edit_message_caption(self, *args, **kwargs):
        if kwargs.get("caption") is not None:
            kwargs["caption"] = small_caps_html(kwargs["caption"])
        return await self._call_with_emoji_fallback(super().edit_message_caption, *args, **kwargs)

bot = StyledBot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class MaintenanceMiddleware:
    """Texnik tanaffusda ownerdan boshqalarning so'rovlarini to'xtatadi."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not is_owner(user.id):
            cfg = load_data().get("settings", {})
            if cfg.get("maintenance", False):
                message = getattr(event, "message", None) or event
                try:
                    if isinstance(event, CallbackQuery):
                        await event.answer("🛠 Botda texnik ishlar ketmoqda. Keyinroq urinib ko'ring.", show_alert=True)
                    elif isinstance(event, Message):
                        await event.answer("🛠 <b>Texnik tanaffus</b>\nBot yangilanmoqda. Iltimos, keyinroq urinib ko'ring.")
                except Exception:
                    pass
                return None
        return await handler(event, data)

dp.message.outer_middleware(MaintenanceMiddleware())
dp.callback_query.outer_middleware(MaintenanceMiddleware())


def default_data():
    return {
        "admins": [OWNER_ID],
        "start_text": "🎬 <b>Assalomu alaykum, {first}!</b>\n\n🍿 <b>Bugun nima ko'ramiz?</b>",
        "channels": [],
        "medias": {},
        "channel_catalog": {},
        "users": {},
        "banned_users": [],
        "orders": [],
        "complaints": [],
        "order_seq": 0,
        "complaint_seq": 0,
        "settings": {"catalog_channel_id": "", "button_colors": {}, "button_texts": {}, "button_emojis": {}, "maintenance": False, "vip_enabled": False, "vip_save_protection": True, "vip_month_price": 15000, "vip_max_months": 6, "vip_duration_days": 30, "early_view_days": 1, "referral_reward": 5000, "referral_need": 3, "referral_vip_days": 3, "notify_new_content": True, "admin_contact": "obidov_xz", "ordinary_base_status": "sozlanmagan", "vip_base_status": "sozlanmagan"}
    }


# Bitta Render jarayonida JSON faylni har bir tugma/handlerda qayta parse qilmaslik uchun kesh.
_DATA_CACHE = None
_DATA_CACHE_MTIME = None

def load_data():
    global _DATA_CACHE, _DATA_CACHE_MTIME
    try:
        current_mtime = DATA_FILE.stat().st_mtime_ns if DATA_FILE.exists() else None
    except OSError:
        current_mtime = None
    if _DATA_CACHE is not None and current_mtime == _DATA_CACHE_MTIME:
        return _DATA_CACHE
    if not DATA_FILE.exists():
        save_data(default_data())
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = default_data()
    for key, value in default_data().items():
        data.setdefault(key, value)
    data.setdefault("channel_catalog", {})
    data.setdefault("settings", {})
    for setting_key, setting_value in default_data()["settings"].items():
        data["settings"].setdefault(setting_key, setting_value)
    if "medias" not in data:
        data["medias"] = data.pop("ani" + "mes", {})
    for media in data.get("medias", {}).values():
        media.setdefault("seasons", {"1": {"episodes": media.get("episodes", {})}})
        media.setdefault("genres", "")
        media.setdefault("status", "")
        media.setdefault("quality", "")
        media.setdefault("voice", "")
    # Avvaldan saqlangan medialarni yangi qoida bo'yicha darhol ochiq qoldiramiz.
    for media in data.get("medias", {}).values():
        media.setdefault("created_at", 0)
    for user_id, user in data.get("users", {}).items():
        user.setdefault("favorites", [])
        user.setdefault("last_watched", None)
        user.setdefault("last_seen", user.get("joined_at", 0))
    _DATA_CACHE = data
    try:
        _DATA_CACHE_MTIME = DATA_FILE.stat().st_mtime_ns
    except OSError:
        _DATA_CACHE_MTIME = None
    return data


def get_setting(name: str):
    return load_data().get("settings", {}).get(name, default_data()["settings"].get(name))


def save_data(data):
    global _DATA_CACHE, _DATA_CACHE_MTIME
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    _DATA_CACHE = data
    try:
        _DATA_CACHE_MTIME = DATA_FILE.stat().st_mtime_ns
    except OSError:
        _DATA_CACHE_MTIME = None


def is_owner(user_id: int) -> bool:
    return bool(OWNER_ID) and user_id == OWNER_ID


def get_admin_permissions(user_id: int):
    if is_owner(user_id):
        return {"add_content", "add_episode", "edit_content", "delete_content"}
    data = load_data()
    if user_id in ADMIN_IDS and str(user_id) not in data.get("admin_permissions", {}):
        return {"add_content", "add_episode"}
    return set(data.get("admin_permissions", {}).get(str(user_id), ["add_content", "add_episode"]))


def has_permission(user_id: int, permission: str) -> bool:
    return is_owner(user_id) or (is_admin(user_id) and permission in get_admin_permissions(user_id))


def is_admin(user_id: int) -> bool:
    # Owner va ruxsati bor adminlar.
    if is_owner(user_id):
        return True
    data = load_data()
    return user_id in ADMIN_IDS or (user_id in data.get("admins", []) and bool(get_admin_permissions(user_id)))


def now_ts() -> int:
    return int(time.time())




def ensure_user(data, tg_user):
    uid = str(tg_user.id)
    is_new = uid not in data.setdefault("users", {})
    if is_new:
        data["users"][uid] = {
            "id": tg_user.id, "username": getattr(tg_user, "username", None),
            "first_name": getattr(tg_user, "first_name", "") or "",
            "favorites": [], "last_watched": None, "joined_at": now_ts(), "last_seen": now_ts()
        }
    else:
        user = data["users"][uid]
        user["username"] = getattr(tg_user, "username", None)
        user["first_name"] = getattr(tg_user, "first_name", "") or user.get("first_name", "")
        user["last_seen"] = now_ts()
    return data["users"][uid], is_new


def fmt_date(timestamp: int) -> str:
    if not timestamp:
        return "Faol emas"
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")












async def notify_new_user(tg_user):
    data = load_data()
    user, is_new = ensure_user(data, tg_user)
    if not is_new:
        save_data(data)
        return
    save_data(data)
    username = f"@{tg_user.username}" if getattr(tg_user, "username", None) else "username yo'q"
    total = len(data.get("users", {}))
    text = (f"🆕 <b>Yangi obunachi</b>\n👤 Ism: {user.get('first_name') or '-'}\n"
            f"🔗 Username: {username}\n🆔 ID: <code>{tg_user.id}</code>\n"
            f"👥 Jami obunachilar: {total}")
    recipients = set([OWNER_ID] + ADMIN_IDS + [x for x in data.get("admins", []) if isinstance(x, int)])
    for admin_id in recipients:
        if not admin_id:
            continue
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logging.info("Yangi obunachi xabari %s ga yuborilmadi: %s", admin_id, exc)


async def send_start(message: Message, user_id=None):
    data = load_data()
    menu_user_id = int(user_id or message.from_user.id)
    # Eski pastki ReplyKeyboard'ni olib tashlab, faqat inline menyudan foydalanamiz.
    remove_msg = await message.answer("⁣", reply_markup=ReplyKeyboardRemove())
    try:
        await remove_msg.delete()
    except Exception:
        pass
    tg_user = message.from_user
    first = (f"@{tg_user.username}" if tg_user.username else (tg_user.first_name or "do'stim"))
    start_text = data.get("start_text", "🎬 Assalomu alaykum, {first}!\n\n🍿 Bugun nima ko'ramiz?").replace("{first}", first)
    await message.answer(start_text, reply_markup=main_menu(menu_user_id))


def main_menu(user_id=None):
    # Foydalanuvchi yuborgan namunadagi joylashuv; mavjud funksiyalar olib tashlanmaydi.
    rows = [
        [InlineKeyboardButton(text="🔎 Anime va Kino izlash", callback_data="search_menu")],
        [InlineKeyboardButton(text="👤 Hisobim", callback_data="account"), InlineKeyboardButton(text="👥 Referal", callback_data="referral")],
        [InlineKeyboardButton(text="💰 Balans", callback_data="account"), InlineKeyboardButton(text="💎 VIP olish", callback_data="vip_buy")],
        [InlineKeyboardButton(text="❓ VIP o‘zi nima?", callback_data="vip_info")],
        [InlineKeyboardButton(text="🛒 Buyurtma berish", callback_data="order_new"), InlineKeyboardButton(text="📝 Shikoyat", callback_data="complaint_new")],
        [InlineKeyboardButton(text="📖 Qo‘llanma", callback_data="guide")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_reply_keyboard():
    """Eski pastki klaviatura endi ishlatilmaydi."""
    return ReplyKeyboardRemove()


def search_menu(user_id=None):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Kod bo'yicha izlash", callback_data="search"), InlineKeyboardButton(text="🔠 Nom bo'yicha izlash", callback_data="search_name")],
        [InlineKeyboardButton(text="🎞 Kinolar ro'yxati", callback_data="category_list:movie"), InlineKeyboardButton(text="📺 Seriallar ro'yxati", callback_data="category_list:series")],
        [InlineKeyboardButton(text="🍥 Anime ro'yxati", callback_data="category_list:anime"), InlineKeyboardButton(text="🎨 Multfilmlar ro'yxati", callback_data="category_list:cartoon")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="home")],
    ])


def admin_menu(user_id=None):
    uid = user_id
    if uid is not None and not is_owner(uid):
        perms = get_admin_permissions(uid)
    else:
        perms = {"add_content", "add_episode", "edit_content", "delete_content"}
    rows = []
    if "add_content" in perms:
        rows.append([InlineKeyboardButton(text="➕ Kino qo'shish", callback_data="newtype:movie"), InlineKeyboardButton(text="➕ Serial qo'shish", callback_data="newtype:series")])
        rows.append([InlineKeyboardButton(text="🍥 Anime qo'shish", callback_data="newtype:anime"), InlineKeyboardButton(text="🎨 Multfilm qo'shish", callback_data="newtype:cartoon")])
    # Admin panelida qism/fasl qo'shish tugmasi alohida ko'rsatiladi.
    if "add_episode" in perms:
        rows.append([InlineKeyboardButton(text="➕ Qism / yangi fasl qo'shish", callback_data="add_episode")])
    if "edit_content" in perms:
        rows.append([InlineKeyboardButton(text="✏️ Nomini o'zgartirish", callback_data="rename_media"), InlineKeyboardButton(text="🖼 Posterni o'zgartirish", callback_data="edit_poster")])
        rows.append([InlineKeyboardButton(text="🔢 Kodini o'zgartirish", callback_data="rename_code")])
    if "delete_content" in perms:
        rows.append([InlineKeyboardButton(text="🗑 Kontent o'chirish", callback_data="del_media")])
        rows.append([InlineKeyboardButton(text="🗑 Qism o'chirish", callback_data="del_episode"), InlineKeyboardButton(text="🗑 Fasl o'chirish", callback_data="del_season")])
    return InlineKeyboardMarkup(inline_keyboard=rows or [[InlineKeyboardButton(text="⛔ Sizga huquq berilmagan", callback_data="admin_home")]])


def owner_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Kino qo'shish", callback_data="add_movie"), InlineKeyboardButton(text="➕ Serial qo'shish", callback_data="add_series")],
        [InlineKeyboardButton(text="🍥 Anime qo'shish", callback_data="newtype:anime"), InlineKeyboardButton(text="🎨 Multfilm qo'shish", callback_data="newtype:cartoon")],
        [InlineKeyboardButton(text="➕ Qism yoki yangi sezon", callback_data="add_episode"), InlineKeyboardButton(text="🗑 Kontent o'chirish", callback_data="del_media")],
        [InlineKeyboardButton(text="🗑 Fasl o'chirish", callback_data="del_season")],
        [InlineKeyboardButton(text="✏️ Nom / kodni tahrirlash", callback_data="rename_media"), InlineKeyboardButton(text="🖼 Posterni tahrirlash", callback_data="edit_poster")],
        [InlineKeyboardButton(text="👤 Admin qo'shish", callback_data="add_admin"), InlineKeyboardButton(text="➖ Admin o'chirish", callback_data="del_admin")],
        [InlineKeyboardButton(text="🔐 Admin huquqlarini sozlash", callback_data="admin_permissions")],
        [InlineKeyboardButton(text="📢 Kanal qo'shish", callback_data="add_channel"), InlineKeyboardButton(text="➖ Kanal o'chirish", callback_data="del_channel")],
        [InlineKeyboardButton(text="🎨 Tugma ranglari", callback_data="appearance")],
        [InlineKeyboardButton(text="😀 Tugmalarga emoji qo'shish", callback_data="button_emoji_menu")],
        [InlineKeyboardButton(text="💾 Zaxira olish", callback_data="owner_backup"), InlineKeyboardButton(text="♻️ Zaxirani tiklash", callback_data="owner_restore_help")],
        [InlineKeyboardButton(text="🗄 Zaxira kanalini o'zgartirish", callback_data="backup_channel_change")],
        [InlineKeyboardButton(text="🛠 Texnik tanaffus: holat", callback_data="owner_maintenance")],
        [InlineKeyboardButton(text=("💎 VIP: O'CHIRISH" if load_data().get("settings", {}).get("vip_enabled", False) else "💎 VIP: YOQISH"), callback_data="owner_vip")],
        [InlineKeyboardButton(text=("🔐 Saqlash himoyasi: YOQILGAN" if load_data().get("settings", {}).get("vip_save_protection", True) else "🔓 Saqlash himoyasi: O'CHIRILGAN"), callback_data="owner_save_protection")],
        [InlineKeyboardButton(text="📣 Hammaga xabar yuborish", callback_data="broadcast")],
        [InlineKeyboardButton(text="🛒 Buyurtmalar", callback_data="owner_orders"), InlineKeyboardButton(text="📝 Shikoyatlar", callback_data="owner_complaints")],
        [InlineKeyboardButton(text="⚙️ Bot sozlamalari", callback_data="owner_settings")],
        [InlineKeyboardButton(text="👥 Obunachilarni boshqarish", callback_data="manage_users")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="stats")],
        [InlineKeyboardButton(text=("📣 Yangi kontent xabari: YOQILGAN" if load_data().get("settings", {}).get("notify_new_content", True) else "📣 Yangi kontent xabari: O‘CHIRILGAN"), callback_data="owner_notify_toggle")],
    ])


def normalize_media_title(value: str) -> str:
    """Sarlavhani kanal captioni va botdagi media nomi bilan solishtirish uchun normallashtiradi."""
    value = re.sub(r"#\w+", " ", value or "", flags=re.UNICODE)
    value = re.sub(r"^(nom|title|kino|media)\s*:\s*", "", value.strip(), flags=re.I)
    return " ".join(value.casefold().split()).strip(" .-–—:")


def catalog_item_for_media(data: dict, media: dict):
    target = normalize_media_title(media.get("name", ""))
    if not target:
        return None, None
    for key, item in data.get("channel_catalog", {}).items():
        if normalize_media_title(item.get("name", "")) == target:
            return key, item
    return None, None


def build_pager_row(left_button, middle_button, right_button):
    """Standart sahifalash: ⬅️ 1/3 ➡️. Episode raqamlari bundan mustasno."""
    buttons = [b for b in (left_button, middle_button, right_button) if b is not None]
    return [buttons]


def episode_keyboard(code: str, episodes: dict, catalog_key=None, catalog_item=None, user_id=None, season="1", media=None, page=0):
    """Qismlar faqat 1, 2, 3 ko‘rinishida; ko‘p faslda avval fasl tanlanadi."""
    rows, row = [], []
    media = media or {}
    seasons = media.get("seasons") or {"1": {"episodes": episodes}}
    def sort_key(value):
        return (0, int(value)) if str(value).isdigit() else (1, str(value))
    season_keys = sorted(seasons, key=sort_key)
    tier = "regular"
    if len(season_keys) > 1 and season == "choose":
        visible_seasons = []
        for sn in season_keys:
            eps = seasons.get(sn, {}).get("episodes", {})
            if any((isinstance(rec, dict) and (tier in rec or "chat_id" in rec or "file_id" in rec)) for rec in eps.values()):
                visible_seasons.append(sn)
        # Fasllar oldindan kiritilgan, ammo postlar hali kelmagan bo‘lsa, fasllarni ham ko‘rsatamiz.
        if not visible_seasons:
            visible_seasons = season_keys
        for sn in visible_seasons:
            row.append(InlineKeyboardButton(text=f"📺 {sn}-fasl", callback_data=f"season_show:{code}:{sn}"))
            if len(row) == 3:
                rows.append(row); row=[]
        if row: rows.append(row)
    else:
        season_data = seasons.get(str(season), {"episodes": episodes})
        season_episodes = season_data.get("episodes", episodes)
        available_numbers = []
        for number, record in season_episodes.items():
            if isinstance(record, dict) and "regular" in record and isinstance(record["regular"], dict):
                available_numbers.append(number)
            elif isinstance(record, dict) and "file_id" in record:
                # Ruchnoy qo'shilgan fayllar kanal sozlamalaridan mustaqil.
                available_numbers.append(number)
            elif isinstance(record, dict) and "chat_id" in record:
                # Eski bitta-bazali format faqat ikki baza sozlanmagan holatda ishlaydi.
                available_numbers.append(number)
        sorted_numbers = sorted(available_numbers, key=sort_key)
        # Har sahifada 15 ta qism; keyingi sahifalar uchun oldinga/orqaga tugmalari.
        page_size = 15
        total_pages = max(1, (len(sorted_numbers) + page_size - 1) // page_size)
        page = max(0, min(int(page), total_pages - 1))
        page_numbers = sorted_numbers[page * page_size:(page + 1) * page_size]
        # Kino va serial qismlari faqat raqam bilan ko'rsatiladi: 1, 2, 3...
        for number in page_numbers:
            row.append(InlineKeyboardButton(text=str(number), callback_data=f"episode:{code}:{season}:{number}"))
            if len(row) == 5:
                rows.append(row); row=[]
        if row: rows.append(row)
        if total_pages > 1:
            left_btn = InlineKeyboardButton(text="⬅️", callback_data=f"season_page:{code}:{season}:{page-1}") if page > 0 else None
            middle_btn = InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="page_info")
            right_btn = InlineKeyboardButton(text="➡️", callback_data=f"season_page:{code}:{season}:{page+1}") if page < total_pages - 1 else None
            rows.extend(build_pager_row(left_btn, middle_btn, right_btn))
        if len(season_keys) > 1:
            rows.append([InlineKeyboardButton(text="⬅️ Fasllar", callback_data=f"season_show:{code}:choose")])
    rows.append([InlineKeyboardButton(text="♥️ Sevimlilarga qo‘shish", callback_data=f"favorite_add:{code}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="search_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def media_caption(code: str, media: dict) -> str:
    description = (media.get("description") or "").strip()
    kind = {"movie":"🎞 Kino", "series":"📺 Serial", "drama":"🎭 Drama", "anime":"🍥 Anime", "cartoon":"🎨 Multfilm"}.get(media.get("media_type"), "🎬 Kontent")
    seasons = media.get("seasons") or {"1": {"episodes": media.get("episodes", {})}}
    total_episodes = sum(len(value.get("episodes", {})) for value in seasons.values())
    base = f"{kind}: <b>{media['name']}</b>\n\n🔢 Kod: <code>{code}</code>\n📺 Fasllar: {len(seasons)}\n🎞 Qismlar: {total_episodes}"
    if media.get("status"):
        base += f"\n📊 Holati: {media['status']}"
    if media.get("quality"):
        base += f"\n🎥 Sifati: {media['quality']}"
    if media.get("genres"):
        base += f"\n🎭 Janri: {media['genres']}"
    if media.get("voice"):
        base += f"\n🎙 Ovoz: {media['voice']}"
    if description:
        base += "\n\n" + description
    return base[:1024]


def is_vip_user(user_id: int, data=None) -> bool:
    """Foydalanuvchining VIP muddati hali tugamaganini tekshiradi."""
    data = data or load_data()
    user = data.get("users", {}).get(str(user_id), {})
    return int(user.get("vip_until", 0) or 0) > now_ts()


def should_protect_content(user_id: int, data=None) -> bool:
    """Himoya yoqilgan bo‘lsa, faqat VIP bo‘lmaganlarga saqlash/forwardni cheklaydi."""
    data = data or load_data()
    enabled = bool(data.get("settings", {}).get("vip_save_protection", True))
    return enabled and not is_vip_user(user_id, data)


def media_available_for_user(user_id: int, media: dict, data=None) -> bool:
    """Barcha qo‘shilgan kino va seriallar darhol hamma foydalanuvchiga ochiq."""
    return True


async def send_media_card(chat_id: int, code: str, media: dict):
    data = load_data()
    caption = media_caption(code, media)
    seasons = media.get("seasons") or {"1": {"episodes": media.get("episodes", {})}}
    season_keys = sorted(seasons, key=lambda v: int(v) if str(v).isdigit() else 999999)
    first_season = season_keys[0] if season_keys else "1"
    season_data = seasons.get(first_season, {"episodes": media.get("episodes", {})})
    markup = episode_keyboard(code, season_data.get("episodes", {}), user_id=chat_id, season=("choose" if len(season_keys) > 1 else first_season), media=media)
    poster = media.get("poster_file_id")
    if poster:
        await bot.send_photo(chat_id, poster, caption=caption, reply_markup=markup)
    else:
        await bot.send_message(chat_id, caption, reply_markup=markup)


class Form(StatesGroup):
    owner_restore_file = State()
    search_code = State()
    search_name = State()
    media_code = State()
    media_name = State()
    media_info = State()
    media_details = State()
    media_season_count = State()
    episode_code = State()
    episode_season = State()
    episode_batch = State()
    delete_code = State()
    delete_episode_code = State()
    delete_episode_number = State()
    delete_season_code = State()
    delete_season_number = State()
    rename_code = State()
    rename_name = State()
    recode_old = State()
    recode_new = State()
    admin_id_add = State()
    admin_id_del = State()
    channel_add = State()
    channel_del = State()
    start_text = State()
    manage_user_id = State()
    owner_setting_value = State()
    owner_backup_channel = State()
    broadcast_message = State()
    button_text_value = State()
    button_emoji_value = State()
    edit_poster_code = State()
    edit_poster_file = State()
    admin_permissions_id = State()
    manage_vip_days = State()
    manage_balance_amount = State()
    manage_mute_duration = State()
    manage_vip_amount = State()
    manage_tx_desc = State()
    manage_tx_media = State()
    manage_tx_confirm = State()
    request_text = State()
    request_confirm = State()
    request_reply = State()


def parse_channel_entry(entry):
    """Eski format (@username yoki -100... ID) va yangi ID|invite formatini o'qiydi."""
    raw = str(entry).strip()
    if "|" in raw:
        chat_ref, invite_url = (part.strip() for part in raw.split("|", 1))
    else:
        chat_ref, invite_url = raw, ""
    if chat_ref.lstrip("-").isdigit():
        chat_ref = int(chat_ref)
    elif chat_ref.startswith("https://t.me/"):
        # Kanal username URL ko'rinishida berilgan bo'lsa, username'ga aylantiramiz.
        tail = chat_ref.removeprefix("https://t.me/").strip("/")
        chat_ref = "@" + tail if tail and not tail.startswith("+") else chat_ref
    if not invite_url and isinstance(chat_ref, str) and chat_ref.startswith("@"):
        invite_url = "https://t.me/" + chat_ref[1:]
    return chat_ref, invite_url, raw


def channel_aliases(chat_ref, invite_url="") -> set:
    """Kanalni ID, @username va taklif havolasi ko'rinishlarida solishtirish uchun kalitlar."""
    aliases = set()
    def add(value):
        value = str(value or "").strip()
        if value:
            aliases.add(value)
            aliases.add(value.casefold())
    add(chat_ref)
    add(invite_url)
    add(str(invite_url or "").strip().rstrip("/"))
    ref = str(chat_ref or "").strip()
    if ref.startswith("@"):
        add(ref[1:])
    elif "t.me/" in ref:
        tail = ref.split("t.me/", 1)[1].strip("/")
        if tail and not tail.startswith("+"):
            add(tail)
            add("@" + tail)
    return aliases


async def check_subscription(user_id: int, ignore_vip: bool = False) -> bool:
    data = load_data()
    # VIP foydalanuvchi barcha majburiy obuna talablaridan ozod.
    # Referal esa faqat haqiqiy obuna bo'lganda hisoblanishi uchun ignore_vip bilan tekshiriladi.
    if not ignore_vip and is_vip_user(user_id, data):
        return True
    channels = data.get("channels", [])
    pending_requests = data.setdefault("join_requests", {})
    requested_channel_ids = {str(x) for x in pending_requests.get(str(user_id), [])}
    for entry in channels:
        chat_ref, _invite_url, raw = parse_channel_entry(entry)
        # Maxfiy kanalga qo'shilish so'rovi yuborilgan bo'lsa, admin tasdig'ini kutmaymiz.
        if channel_aliases(chat_ref, _invite_url) & requested_channel_ids:
            continue
        try:
            member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
            # Restricted a'zolar ham is_member=True bo'lsa obunachi hisoblanadi.
            status = str(member.status)
            status = status.split(".")[-1].lower()
            is_member = status in ("creator", "administrator", "member")
            if status == "restricted":
                is_member = bool(getattr(member, "is_member", False))
            if not is_member:
                logging.info("Subscription missing: user=%s channel=%s status=%s", user_id, raw, status)
                return False
        except Exception as exc:
            logging.exception("get_chat_member failed for channel=%s: %s", raw, exc)
            return False
    return True


PURGE_TEXT = ("⚠️ <b>Diqqat!</b>\n\n"
              "Siz majburiy obunadagi kanaldan chiqib ketganingiz sababli "
              "chatdagi kinolar o'chirildi.\n\n"
              "Qayta foydalanish uchun kanallarga obuna bo'ling yoki 💎 VIP oling.")


def track_media_msg(user_id: int, message_id, vip=None):
    """Foydalanuvchiga yuborilgan kino (media) xabarini eslab qoladi.

    VIP faol paytda yuborilganlar alohida saqlanadi — VIP tugagach o'shalar o'chiriladi.
    """
    if not message_id:
        return
    data = load_data()
    user = data.setdefault("users", {}).setdefault(str(user_id), {})
    ids = [int(x) for x in (user.get("media_msgs") or [])]
    ids.append(int(message_id))
    user["media_msgs"] = ids[-300:]
    if vip is None:
        vip = is_vip_user(user_id, data)
    if vip:
        vip_ids = [int(x) for x in (user.get("vip_media_msgs") or [])]
        vip_ids.append(int(message_id))
        user["vip_media_msgs"] = vip_ids[-300:]
    save_data(data)


async def purge_user_media(user_id: int, notify: bool = True) -> int:
    """Obunadan chiqqan foydalanuvchi chatidan bot yuborgan kinolarni o'chiradi.
    Matnlar va tugmalar tegilmaydi. VIP foydalanuvchilar ozod."""
    data = load_data()
    if is_vip_user(user_id, data):
        return 0
    user = data.get("users", {}).get(str(user_id))
    if not user:
        return 0
    ids = [int(x) for x in (user.get("media_msgs") or [])]
    if not ids:
        return 0
    deleted = 0
    for mid in ids:
        try:
            await bot.delete_message(user_id, mid)
            deleted += 1
        except Exception:
            pass
        await asyncio.sleep(0.04)
    user["media_msgs"] = []
    user["vip_media_msgs"] = []
    save_data(data)
    if notify and deleted:
        try:
            await bot.send_message(user_id, PURGE_TEXT)
        except Exception:
            pass
    return deleted


async def enforce_subscription(user_id: int) -> bool:
    """Obunani tekshiradi.

    Kinolar faqat kanaldagi «chiqib ketdi» xabaridan (chat_member update) aniqlangan
    foydalanuvchilardan o'chiriladi — kanal qo'shilganda hammani jazolab bo'lmaydi.
    Obuna tasdiqlanganda referal ham bir marta hisoblanadi.
    """
    allowed = await check_subscription(user_id)
    if allowed:
        await credit_referral(user_id)
    return allowed


async def purge_vip_media(user_id: int, user: dict) -> int:
    """VIP faol paytda yuborilgan kinolarni chatdan o'chiradi."""
    ids = [int(x) for x in (user.get("vip_media_msgs") or [])]
    if not ids:
        return 0
    removed = set(ids)
    deleted = 0
    for mid in ids:
        try:
            await bot.delete_message(user_id, mid)
            deleted += 1
        except Exception:
            pass
        await asyncio.sleep(0.04)
    user["vip_media_msgs"] = []
    user["media_msgs"] = [x for x in (user.get("media_msgs") or []) if int(x) not in removed]
    return deleted


# ================= REFERAL TIZIMI =================
REF_PREFIX = "ref_"
REFERRAL_RULES = (
    "• Har <b>{need} ta</b> do‘stingiz havola orqali botga kirsa — "
    "<b>{vip_days} kunlik VIP</b> olasiz.\n"
    "• Referal faqat do‘stingiz <b>barcha majburiy kanallarga obuna bo‘lgandan keyin</b> hisoblanadi.\n"
    "• VIP muddati tugagach, VIP orqali ochilgan kinolar chatdan o‘chiriladi."
)


def ref_settings(data=None):
    data = data or load_data()
    cfg = data.get("settings", {})
    need = max(1, int(cfg.get("referral_need", 3) or 3))
    vip_days = max(1, int(cfg.get("referral_vip_days", 3) or 3))
    return need, vip_days


def parse_ref_payload(payload: str):
    """«ref_123» yoki eski «123» ko‘rinishidagi start parametrini o‘qiydi."""
    payload = str(payload or "").strip()
    if payload.casefold().startswith(REF_PREFIX):
        payload = payload[len(REF_PREFIX):]
    if not payload.isdigit():
        return None
    value = int(payload)
    return value if value > 0 else None


def record_referrer(data, user_id: int, payload: str) -> bool:
    """Yangi foydalanuvchiga taklif qiluvchini biriktiradi (faqat bir marta)."""
    referrer = parse_ref_payload(payload)
    if not referrer or referrer == int(user_id):
        return False
    users = data.setdefault("users", {})
    if str(referrer) not in users:
        return False
    user = users.setdefault(str(user_id), {})
    if user.get("referrer"):
        return False
    user["referrer"] = str(referrer)
    user["referral_qualified"] = False
    return True


_REFERRAL_INFLIGHT = set()


async def credit_referral(user_id: int) -> bool:
    """Do‘st barcha kanallarga obuna bo‘lgach referalni hisoblaydi va VIP beradi."""
    if int(user_id) in _REFERRAL_INFLIGHT:
        return False
    _REFERRAL_INFLIGHT.add(int(user_id))
    try:
        return await _credit_referral(user_id)
    finally:
        _REFERRAL_INFLIGHT.discard(int(user_id))


async def _credit_referral(user_id: int) -> bool:
    data = load_data()
    users = data.get("users", {})
    user = users.get(str(user_id))
    if not user:
        return False
    referrer_key = str(user.get("referrer") or "")
    if not referrer_key or user.get("referral_qualified") or user.get("referral_invalid"):
        return False
    inviter = users.get(referrer_key)
    if not inviter:
        user["referral_invalid"] = True
        save_data(data)
        return False
    # VIP bo‘lishning o‘zi yetarli emas — barcha majburiy kanallarga a’zolik shart.
    if not await check_subscription(user_id, ignore_vip=True):
        return False
    user["referral_qualified"] = True
    user["referral_qualified_at"] = now_ts()
    need, vip_days = ref_settings(data)
    count = int(inviter.get("referrals_count", 0) or 0) + 1
    inviter["referrals_count"] = count
    reward_given = count % need == 0
    if reward_given:
        base = max(now_ts(), int(inviter.get("vip_until", 0) or 0))
        inviter["vip_until"] = base + vip_days * 86400
        inviter["referral_rewards"] = int(inviter.get("referral_rewards", 0) or 0) + 1
        inviter.pop("vip_warned_until", None)
    save_data(data)
    logging.info("Referal hisoblandi: user=%s referrer=%s count=%s", user_id, referrer_key, count)
    if reward_given:
        try:
            await bot.send_message(
                int(referrer_key),
                "🎉 <b>Tabriklaymiz!</b>\n\n"
                f"👥 Taklif qilgan do‘stlaringiz: <b>{count}</b> ta\n"
                f"💎 Mukofot: <b>{vip_days} kunlik VIP</b>\n"
                f"⏳ VIP tugash vaqti: <b>{fmt_date(inviter['vip_until'])}</b>",
            )
        except Exception:
            logging.info("Referal mukofoti xabari yuborilmadi: %s", referrer_key)
    return True


async def referral_text_and_link(user_id: int):
    data = load_data()
    user = data.get("users", {}).get(str(user_id), {})
    count = int(user.get("referrals_count", 0) or 0)
    need, vip_days = ref_settings(data)
    left = need - (count % need)
    rewards = int(user.get("referral_rewards", 0) or 0)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={REF_PREFIX}{user_id}"
    text = (
        "👥 <b>Referal tizimi</b>\n\n"
        "🔗 Sizning taklif havolangiz:\n"
        f"<code>{link}</code>\n\n"
        "📋 <b>Qoidalar:</b>\n"
        + REFERRAL_RULES.format(need=need, vip_days=vip_days)
        + "\n\n"
        f"📊 Taklif qilgan do‘stlar: <b>{count}</b> ta\n"
        f"🎁 Keyingi mukofotgacha: <b>{left}</b> ta do‘st\n"
        f"👑 Olingan VIP mukofotlar: <b>{rewards}</b> marta"
    )
    return text, link


def referral_keyboard(link: str):
    share = "https://t.me/share/url?url=" + quote(link) + "&text=" + quote(
        "Do‘stim, bu botda kino va seriallar bor. Havola orqali kiring:"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Do‘stlarga ulashish", url=share)],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="home")],
    ])


def subscription_keyboard():
    rows = []
    for entry in load_data().get("channels", []):
        chat_ref, invite_url, raw = parse_channel_entry(entry)
        # Private kanal uchun taklif havolasi shart; uni admin ID|https://t.me/+... ko'rinishida beradi.
        if invite_url:
            label = str(chat_ref) if str(chat_ref).startswith("@") else "Maxfiy kanal"
            rows.append([InlineKeyboardButton(text=f"📢 {label}", url=invite_url)])
    rows.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")])
    # VIP sotib olgan foydalanuvchi majburiy obunasiz foydalanadi.
    rows.append([InlineKeyboardButton(text="💎 VIP olish", callback_data="vip_buy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def require_subscription_message(message: Message) -> bool:
    data = load_data()
    if int(message.from_user.id) in [int(x) for x in data.get("banned_users", [])]:
        await message.answer("⛔ Siz botdan foydalanishdan chetlatilgansiz.")
        return False
    user_record = data.get("users", {}).get(str(message.from_user.id), {})
    mute_until = int(user_record.get("mute_until", 0) or 0)
    if mute_until > now_ts():
        await message.answer(f"🔇 Botdan foydalanish vaqtincha cheklangan.\nQayta foydalanish: {fmt_date(mute_until)}")
        return False
    if await enforce_subscription(message.from_user.id):
        return True
    await message.answer(
        "🔒 <b>Botdan foydalanishdan oldin majburiy kanallarga obuna bo'ling.</b>\n\n"
        "📢 Kanallarga qo'shiling yoki 💎 VIP oling — VIP obunasida majburiy obuna talab qilinmaydi.",
        reply_markup=subscription_keyboard()
    )
    return False


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    # Avval ro'yxatdan o'tkazish va yangi obunachi xabarini yuborishni kutamiz;
    # create_task bilan parallel ishlatish yangi userni oldindan saqlab, xabarni o'tkazib yuborardi.
    await notify_new_user(message.from_user)
    data = load_data()
    _user, is_new = ensure_user(data, message.from_user)
    # Referal havolasi: /start ref_123 (eski /start 123 ham qo'llanadi).
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if is_new and payload:
        record_referrer(data, message.from_user.id, payload)
    save_data(data)
    if int(message.from_user.id) in [int(x) for x in data.get("banned_users", [])]:
        return await message.answer("⛔ Siz botdan foydalanishdan chetlatilgansiz.")
    _u = data.get("users", {}).get(str(message.from_user.id), {})
    if int(_u.get("mute_until", 0) or 0) > now_ts():
        return await message.answer(f"🔇 Botdan foydalanish vaqtincha cheklangan.\nQayta foydalanish: {fmt_date(int(_u['mute_until']))}")
    if not await enforce_subscription(message.from_user.id):
        return await message.answer("🔒 <b>Botdan foydalanishdan oldin majburiy kanallarga obuna bo‘ling.</b>\n\n📢 Kanallarga qo'shiling yoki 💎 VIP oling.", reply_markup=subscription_keyboard())
    await send_start(message)


@dp.message(Command("ref"))
async def ref_command(message: Message, state: FSMContext):
    """Obunachiga referal havolasi va qoidalarini ko'rsatadi."""
    await state.clear()
    if not await require_subscription_message(message):
        return
    text, link = await referral_text_and_link(message.from_user.id)
    await message.answer(text, reply_markup=referral_keyboard(link))


@dp.callback_query(F.data == "owner_settings")
async def owner_settings_callback(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    cfg = load_data().get("settings", {})
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"👥 Referal: {cfg.get('referral_need', 3)} do‘st", callback_data="setcfg:referral_need")],
        [InlineKeyboardButton(text=f"🎁 Referal mukofoti: {cfg.get('referral_vip_days', 3)} kunlik VIP", callback_data="setcfg:referral_vip_days")],
        [InlineKeyboardButton(text=f"👑 VIP 1 oy narxi: {cfg.get('vip_month_price', 15000)} so‘m", callback_data="setcfg:vip_month_price")],
        [InlineKeyboardButton(text=f"📅 VIP maksimal muddati: {cfg.get('vip_max_months', 6)} oy", callback_data="setcfg:vip_max_months")],
        [InlineKeyboardButton(text=f"⏳ VIP oy davomiyligi: {cfg.get('vip_duration_days', 30)} kun", callback_data="setcfg:vip_duration_days")],
        [InlineKeyboardButton(text=f"🎬 Erta ko‘rish: {cfg.get('early_view_days', 1)} kun", callback_data="setcfg:early_view_days")],
        [InlineKeyboardButton(text=f"💬 To‘lov admini: @{str(cfg.get('admin_contact', 'obidov_xz')).lstrip('@')}", callback_data="setcfg:admin_contact")],
        [InlineKeyboardButton(text="⬅️ Owner paneli", callback_data="admin_home")]
    ])
    try:
        await call.message.edit_text("⚙️ <b>Bot sozlamalari</b>", reply_markup=kb)
    except Exception:
        await call.message.answer("⚙️ <b>Bot sozlamalari</b>", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("setcfg:"))
async def owner_setting_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    key = call.data.split(":", 1)[1]
    prompts = {
        "admin_contact": "To‘lov adminining username sini yuboring, masalan @obidov_xz:",
        "vip_month_price": "VIP obunasining 1 oylik narxini so‘mda yuboring:",
        "referral_need": "Nechta do‘st taklif qilinganda mukofot berilsin (masalan, 3):",
        "referral_vip_days": "Referal mukofoti necha kunlik VIP bo‘lsin (masalan, 3):",
        "vip_max_months": "VIP uchun ruxsat etilgan maksimal oy sonini yuboring:",
        "vip_duration_days": "VIP obunasining bir oyi necha kun bo‘lishini yuboring:",
        "early_view_days": "VIP erta ko‘rish necha kun oldin bo‘lishini yuboring:",
    }
    if key not in prompts:
        return await call.answer("⚠️ Noma'lum sozlama", show_alert=True)
    await state.set_state(Form.owner_setting_value)
    await state.update_data(setting_key=key)
    await call.message.answer("⚙️ " + prompts[key] + "\nBekor qilish: /cancel")
    await call.answer()


@dp.message(Form.owner_setting_value)
async def owner_setting_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Faqat owner uchun.")
    if (message.text or "").strip() == "/cancel":
        await state.clear()
        return await message.answer("✅ Bekor qilindi.", reply_markup=owner_menu())
    saved = await state.get_data()
    key = saved.get("setting_key")
    raw = (message.text or "").strip()
    limits = {}
    if key in limits:
        try:
            value = int(raw)
        except ValueError:
            return await message.answer("🔢 Faqat raqam yuboring:")
        low, high = limits[key]
        if not low <= value <= high:
            return await message.answer(f"⚠️ Qiymat {low} dan {high} gacha bo'lishi kerak. Qayta yuboring:")
    elif key == "admin_contact":
        value = raw
        if not value.startswith("@") or len(value) < 2 or " " in value:
            return await message.answer("⚠️ Username @ bilan boshlansin, masalan @AnimBot_Admin. Qayta yuboring:")
    elif key in {"vip_max_months", "vip_duration_days", "early_view_days", "vip_month_price", "referral_need", "referral_vip_days"}:
        try: value = int(raw)
        except ValueError: return await message.answer("🔢 Faqat raqam yuboring:")
        if value < 0: return await message.answer("⚠️ Qiymat manfiy bo‘lishi mumkin emas:")
        if key in {"vip_max_months", "vip_duration_days", "referral_need", "referral_vip_days"} and value < 1:
            return await message.answer("⚠️ Bu qiymat kamida 1 bo‘lishi kerak:")
    else:
        await state.clear()
        return await message.answer("❌ Sozlama topilmadi.")
    data = load_data()
    data.setdefault("settings", {})[key] = value
    save_data(data)
    await state.clear()
    labels = {"admin_contact": "To‘lov admini", "vip_month_price": "VIP oylik narxi", "referral_need": "Referal uchun kerakli do‘stlar soni", "referral_vip_days": "Referal mukofoti (kun)", "vip_max_months": "VIP maksimal muddati", "vip_duration_days": "VIP oy davomiyligi", "early_view_days": "Erta ko‘rish"}
    await message.answer(f"✅ {labels.get(key, key)} yangilandi: <b>{value}</b>\nO‘zgarish darhol kuchga kirdi.", reply_markup=owner_menu())


@dp.callback_query(F.data == "manage_users")
async def manage_users_callback(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Adminlar uchun", show_alert=True)
    await state.set_state(Form.manage_user_id)
    await call.message.answer("👥 <b>Obunachilarni boshqarish</b>\n🆔 Boshqariladigan foydalanuvchi ID sini yuboring:")
    await call.answer()


@dp.callback_query(F.data == "backup_channel_change")
async def backup_channel_change_callback(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    current = get_backup_channel_id() or "sozlanmagan"
    await state.set_state(Form.owner_backup_channel)
    await call.message.answer(
        "🗄 <b>Zaxira kanalini sozlash</b>\n"
        f"Hozirgi kanal ID: <code>{current}</code>\n\n"
        "Yangi kanal ID sini yuboring (masalan, <code>-1001234567890</code>).\n"
        "O‘chirish uchun <code>off</code> yuboring. Bot o‘sha kanalga admin bo‘lishi kerak.\n"
        "Bekor qilish: /cancel"
    )
    await call.answer()


@dp.message(Form.owner_backup_channel)
async def backup_channel_change_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Faqat owner uchun.")
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await state.clear()
        return await message.answer("✅ Bekor qilindi.", reply_markup=owner_menu())
    data = load_data()
    if raw.casefold() in {"off", "o‘chirish", "ochirish"}:
        data.setdefault("settings", {}).pop("backup_channel_id", None)
        save_data(data)
        await state.clear()
        return await message.answer("✅ Zaxira kanal sozlamasi olib tashlandi. ENV dagi BACKUP_CHANNEL_ID ishlatiladi; u ham bo‘sh bo‘lsa, zaxira shaxsiy xabarga yuboriladi.", reply_markup=owner_menu())
    try:
        channel_id = int(raw)
        if channel_id >= 0:
            raise ValueError
    except ValueError:
        return await message.answer("❌ Kanal ID manfiy raqam bo‘lishi kerak, masalan -1001234567890. Qayta yuboring:")
    data.setdefault("settings", {})["backup_channel_id"] = str(channel_id)
    save_data(data)
    await state.clear()
    await message.answer(f"✅ Zaxira kanali saqlandi: <code>{channel_id}</code>\nBot kanalga admin qilinganini tekshiring.", reply_markup=owner_menu())


@dp.callback_query(F.data == "owner_backup")
async def owner_backup_callback(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    data = load_data()
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    doc = BufferedInputFile(payload, filename=f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        backup_channel_id = get_backup_channel_id()
        if backup_channel_id:
            await bot.send_document(backup_channel_id, doc, caption="🤖 Bot avtomatik zaxirasi")
            await call.message.answer("✅ Zaxira JSON fayli BACKUP_CHANNEL_ID kanaliga yuborildi.", reply_markup=owner_menu())
        else:
            await bot.send_document(call.from_user.id, doc, caption="💾 Bot zaxirasi. Xavfsiz joyda saqlang.")
            await call.message.answer("✅ Zaxira sizga shaxsiy xabarda yuborildi. Renderda BACKUP_CHANNEL_ID kiritsangiz, kanalga ham yuboriladi.", reply_markup=owner_menu())
    except Exception as exc:
        await call.message.answer(f"❌ Zaxira yuborilmadi. Kanal IDsi va bot adminligini tekshiring. ({type(exc).__name__})")
    await call.answer()

@dp.callback_query(F.data == "owner_restore_help")
async def owner_restore_help(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    await state.set_state(Form.owner_restore_file)
    await state.update_data(restore_files=0)
    await call.message.answer(
        "♻️ <b>Zaxirani tiklash</b>\n"
        "Bitta JSON fayl yoki ichida bir nechta .json bo'lgan ZIP arxiv yuboring. "
        "Bir nechta JSONni alohida yubormoqchi bo'lsangiz, ketma-ket yuboring va oxirida /done yozing.\n"
        "Yuborilgan JSONlar birlashtiriladi: medias kodlari qo'shiladi/yangilanadi, qolgan sozlamalar oxirgi fayldan olinadi.\n"
        "Bekor qilish: /cancel"
    )
    await call.answer()


def _merge_backup_objects(base, incoming):
    if not isinstance(incoming, dict):
        raise ValueError("JSON ildizi obyekt bo'lishi kerak")
    if not isinstance(incoming.get("medias", {}), dict):
        raise ValueError("medias bo'limi noto'g'ri")
    # Har bir zaxiradagi media kodlarini saqlab qolamiz; bir xil kod bo'lsa yangi qiymat ustun.
    base.setdefault("medias", {}).update(incoming.get("medias", {}))
    for key, value in incoming.items():
        if key == "medias":
            continue
        if key == "users" and isinstance(value, dict):
            base.setdefault("users", {}).update(value)
        elif key == "settings" and isinstance(value, dict):
            base.setdefault("settings", {}).update(value)
        elif key in ("admins", "channels", "banned_users") and isinstance(value, list):
            current = base.setdefault(key, [])
            base[key] = list(dict.fromkeys(current + value))
        else:
            base[key] = value
    return base


@dp.message(Form.owner_restore_file)
async def owner_restore_file(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    if message.text and message.text.strip().lower() in ("/done", "done", "tayyor"):
        saved = await state.get_data()
        files_count = int(saved.get("restore_files", 0))
        if not files_count:
            return await message.answer("❌ Hali JSON yuborilmadi. JSON yoki ZIP yuboring:")
        restored = saved.get("restore_data")
        if not isinstance(restored, dict):
            return await message.answer("❌ Tiklash ma'lumotlari topilmadi. Qaytadan boshlang: /cancel")
        base = default_data()
        for key, value in base.items():
            restored.setdefault(key, value)
        restored.setdefault("settings", {})
        for key, value in base["settings"].items():
            restored["settings"].setdefault(key, value)
        save_data(restored)
        await state.clear()
        return await message.answer(f"✅ Tiklandi. {files_count} ta JSON fayl birlashtirildi; jami {len(restored.get('medias', {}))} ta kontent kodi mavjud.", reply_markup=owner_menu())
    if not message.document:
        return await message.answer("📄 JSON yoki ichida JSON fayllari bo'lgan ZIP arxiv yuboring. Bir nechta fayl uchun oxirida /done yozing.")
    fname = (message.document.file_name or "").lower()
    if not (fname.endswith(".json") or fname.endswith(".zip")):
        return await message.answer("❌ Faqat .json yoki .zip fayl qabul qilinadi.")
    try:
        file = await bot.get_file(message.document.file_id)
        stream = await bot.download_file(file.file_path)
        raw = stream.read() if hasattr(stream, "read") else bytes(stream)
        json_blobs = []
        if fname.endswith(".json"):
            json_blobs = [raw]
        else:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                json_blobs = [archive.read(n) for n in archive.namelist() if n.lower().endswith(".json") and not n.endswith("/")]
            if not json_blobs:
                return await message.answer("❌ ZIP ichidan .json fayl topilmadi.")
        saved = await state.get_data()
        base = saved.get("restore_data") or default_data()
        for blob in json_blobs:
            incoming = json.loads(blob.decode("utf-8-sig"))
            base = _merge_backup_objects(base, incoming)
        count = int(saved.get("restore_files", 0)) + len(json_blobs)
        await state.update_data(restore_data=base, restore_files=count)
        await message.answer(f"✅ {len(json_blobs)} ta JSON o'qildi. Jami: {count} ta. Yana fayl yuboring yoki tiklashni yakunlash uchun /done yozing.")
    except Exception as exc:
        logging.exception("Backup restore failed")
        await message.answer(f"❌ Zaxirani o'qib bo'lmadi: {type(exc).__name__}: {str(exc)[:180]}. JSON fayl bot yaratgan zaxira ekanini tekshiring.")

@dp.callback_query(F.data == "owner_maintenance")
async def owner_maintenance_callback(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    data = load_data(); cfg = data.setdefault("settings", {})
    cfg["maintenance"] = not bool(cfg.get("maintenance", False)); save_data(data)
    status = "YOQILDI" if cfg["maintenance"] else "O'CHIRILDI"
    await call.message.answer(f"🛠 Texnik tanaffus {status}.", reply_markup=owner_menu())
    await call.answer()

@dp.callback_query(F.data == "owner_vip")
async def owner_vip_callback(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    data = load_data(); cfg = data.setdefault("settings", {})
    cfg["vip_enabled"] = not bool(cfg.get("vip_enabled", False)); save_data(data)
    status = "YOQILDI" if cfg["vip_enabled"] else "O'CHIRILDI"
    detail = "Foydalanuvchilarga VIP olish va VIP nima tugmalari ko'rsatiladi." if cfg["vip_enabled"] else "Foydalanuvchilardan VIP tugmalari yashiriladi."
    await call.message.answer(f"💎 VIP rejimi {status}. {detail}", reply_markup=owner_menu())
    await call.answer()

@dp.callback_query(F.data == "owner_save_protection")
async def owner_save_protection_callback(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    data = load_data()
    cfg = data.setdefault("settings", {})
    cfg["vip_save_protection"] = not bool(cfg.get("vip_save_protection", True))
    save_data(data)
    status = "YOQILDI" if cfg["vip_save_protection"] else "O‘CHIRILDI"
    detail = (
        "VIP bo‘lmaganlar kontentni saqlay/forward qila olmaydi; VIP foydalanuvchilar erkin foydalanadi."
        if cfg["vip_save_protection"] else
        "Barcha foydalanuvchilar kontentni saqlash va forward qilish imkoniga ega."
    )
    try:
        await call.message.edit_reply_markup(reply_markup=owner_menu())
    except Exception:
        pass
    await call.message.answer(f"🔐 Saqlash himoyasi {status}.\n{detail}", reply_markup=owner_menu())
    await call.answer()


@dp.callback_query(F.data == "vip_info")
async def vip_info_callback(call: CallbackQuery):
    cfg = load_data().get("settings", {})
    if not cfg.get("vip_enabled", False) and not is_owner(call.from_user.id):
        return await call.answer("💎 VIP hozircha o‘chirilgan.", show_alert=True)
    price = int(cfg.get("vip_month_price", 15000) or 15000)
    text = (
        "👑 <b>VIP — tomoshadan ko‘proq zavq oling!</b>\n\n"
        "VIP obunasi orqali sevimli anime, serial va filmlaringizni yanada qulayroq tomosha qiling! 💎\n\n"
        "✨ <b>VIP imkoniyatlari:</b>\n\n"
        "🚀 3 kun oldin tomosha qilish\n"
        "🎬 720p–1080p sifatdagi videolar (manbada mavjud bo‘lsa)\n"
        "🔓 Majburiy kanallarga obunasiz kirish\n"
        "📤 Kontentni ulashish imkoniyati\n"
        "♥️ Sevimlilar va ▶️ Davom ettirish\n\n"
        f"💎 VIP narxi: oyiga {price:,} so‘m".replace(",", " ")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 VIP olish", callback_data="vip_buy")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="home")],
    ])
    try: await call.message.edit_text(text, reply_markup=kb)
    except Exception: await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "vip_buy")
async def vip_buy_callback(call: CallbackQuery):
    data = load_data(); cfg = data.get("settings", {})
    if not cfg.get("vip_enabled", False) and not is_owner(call.from_user.id):
        return await call.answer("💎 VIP hozircha o'chirilgan.", show_alert=True)
    price = int(cfg.get("vip_month_price", 15000) or 15000)
    max_months = max(1, min(24, int(cfg.get("vip_max_months", 6) or 6)))
    rows = [[InlineKeyboardButton(text=f"{m} oy — {price*m:,} so'm".replace(',', ' '), callback_data=f"vip_plan:{m}")] for m in range(1, max_months+1)]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="vip_info")])
    text = f"💎 <b>VIP sotib olish</b>\n\n1 oy: {price:,} so'm\nKerakli muddatni tanlang. Xarid ikki marta tasdiqlanadi va pul balansingizdan yechiladi.".replace(',', ' ')
    try: await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception: await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()

@dp.callback_query(F.data.startswith("vip_plan:"))
async def vip_plan_callback(call: CallbackQuery):
    data=load_data(); cfg=data.get("settings", {})
    if not cfg.get("vip_enabled", False) and not is_owner(call.from_user.id):
        return await call.answer("💎 VIP hozircha o'chirilgan.", show_alert=True)
    try: months=int(call.data.split(':',1)[1])
    except ValueError: return await call.answer("⚠️ Muddat noto'g'ri", show_alert=True)
    max_months=max(1,min(24,int(cfg.get('vip_max_months',6) or 6)))
    if not 1 <= months <= max_months: return await call.answer("⚠️ Bu muddat mavjud emas", show_alert=True)
    price=int(cfg.get('vip_month_price',15000) or 15000)*months
    user=data.setdefault('users',{}).setdefault(str(call.from_user.id),{})
    balance=float(user.get('balance',0) or 0)
    if balance < price:
        return await call.answer(f"💰 Balansingiz yetarli emas. Kerak: {price:,} so'm; balans: {balance:g} so'm".replace(',', ' '), show_alert=True)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, sotib olaman", callback_data=f"vip_confirm:{months}")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="vip_buy")]
    ])
    text=f"⚠️ <b>Xaridni tasdiqlash</b>\n\nVIP muddati: {months} oy\nNarxi: {price:,} so'm\nBalansingiz: {balance:g} so'm\nXaridni davom ettirish uchun yana bir marta tasdiqlang.".replace(',', ' ')
    try: await call.message.edit_text(text,reply_markup=kb)
    except Exception: await call.message.answer(text,reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("vip_confirm:"))
async def vip_confirm_callback(call: CallbackQuery):
    try: months=int(call.data.split(':',1)[1])
    except ValueError: return await call.answer("⚠️ Muddat noto'g'ri",show_alert=True)
    data=load_data(); cfg=data.get('settings',{})
    if not cfg.get('vip_enabled',False) and not is_owner(call.from_user.id): return await call.answer("💎 VIP hozircha o'chirilgan.",show_alert=True)
    max_months=max(1,min(24,int(cfg.get('vip_max_months',6) or 6)))
    if not 1 <= months <= max_months: return await call.answer("⚠️ Muddat noto'g'ri",show_alert=True)
    user=data.setdefault('users',{}).setdefault(str(call.from_user.id),{})
    price=int(cfg.get('vip_month_price',15000) or 15000)*months
    balance=float(user.get('balance',0) or 0)
    if balance < price: return await call.answer("💰 Balansingiz yetarli emas.",show_alert=True)
    # Takroriy callbackdan ikki marta pul yechilmasligi uchun balans tekshiruvi va yozish bir amalda.
    user['balance']=balance-price
    base=max(now_ts(),int(user.get('vip_until',0) or 0))
    user['vip_until']=base+months*int(cfg.get('vip_duration_days',30) or 30)*86400
    user.pop('vip_warned_until',None)
    save_data(data)
    text=f"✅ <b>VIP faollashtirildi!</b>\nMuddat: {months} oy\nTo'landi: {price:,} so'm\nQolgan balans: {user['balance']:g} so'm\nTugash vaqti: {fmt_date(user['vip_until'])}".replace(',', ' ')
    try: await call.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Asosiy menyu",callback_data="home")]]))
    except Exception: await call.message.answer(text)
    await call.answer("✅ VIP sotib olindi")


@dp.callback_query(F.data == "admin_home")
async def admin_home_callback(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Adminlar uchun", show_alert=True)
    if is_owner(call.from_user.id):
        await call.message.answer("👑 <b>Owner paneli</b>", reply_markup=owner_menu())
    else:
        await call.message.answer("🛠 <b>Admin paneli</b>", reply_markup=admin_menu(call.from_user.id))
    await call.answer()


@dp.message(F.text.regexp(r"^/manage(?:\s+\d+)?$"))
async def manage_user_command(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return await message.answer("⛔ Adminlar uchun.")
    parts = (message.text or "").split()
    if len(parts) == 1:
        await state.set_state(Form.manage_user_id)
        return await message.answer("🆔 Boshqariladigan foydalanuvchi Telegram ID sini yuboring:")
    await show_user_management(message, int(parts[1]))


async def show_user_management(message: Message, user_id: int):
    data = load_data()
    user = data.get("users", {}).get(str(user_id))
    if not user:
        return await message.answer("❌ Bu foydalanuvchi bazada topilmadi. U botda /start bosgan bo‘lishi kerak.")
    banned = user_id in [int(x) for x in data.get("banned_users", [])]
    vip_until = int(user.get("vip_until", 0) or 0)
    mute_until = int(user.get("mute_until", 0) or 0)
    balance = float(user.get("balance", 0) or 0)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 VIP berish / muddatini uzaytirish", callback_data=f"uservip:{user_id}"), InlineKeyboardButton(text="❌ VIPni olish", callback_data=f"uservipremove:{user_id}")],
        [InlineKeyboardButton(text="💰 Balansga pul qo‘shish", callback_data=f"userbalance:{user_id}"), InlineKeyboardButton(text="💸 Balansdan ayirish", callback_data=f"userbalanceout:{user_id}")],
        [InlineKeyboardButton(text="🔇 Mute qilish", callback_data=f"usermute:{user_id}"), InlineKeyboardButton(text="🔊 Mute bekor qilish", callback_data=f"usermuteoff:{user_id}")],
        [InlineKeyboardButton(text="🚫 Ban qilish" if not banned else "✅ Unban qilish", callback_data=f"userban:{user_id}")],
        [InlineKeyboardButton(text="⬅️ Owner paneli", callback_data="admin_home")]
    ])
    await message.answer(
        f"👤 <b>Foydalanuvchi</b>\nID: <code>{user_id}</code>\nUsername: @{user.get('username') or 'yo‘q'}"
        f"\nVIP: {'Faol (' + fmt_date(vip_until) + ')' if vip_until > now_ts() else 'Faol emas'}"
        f"\nBalans: {balance:g} so‘m\nMute: {fmt_date(mute_until) if mute_until > now_ts() else 'Yo‘q'}"
        f"\nBan: {'Ha' if banned else 'Yo‘q'}", reply_markup=kb)


@dp.message(Form.manage_user_id)
async def manage_user_id_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Adminlar uchun.")
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        return await message.answer("🆔 Faqat Telegram ID raqamini yuboring:")
    await state.clear()
    await show_user_management(message, user_id)


@dp.callback_query(F.data.startswith("userban:"))
async def user_ban_callback(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Adminlar uchun", show_alert=True)
    user_id = int(call.data.split(":", 1)[1])
    data = load_data()
    banned = [int(x) for x in data.get("banned_users", [])]
    if user_id in banned:
        data["banned_users"] = [x for x in banned if x != user_id]
        result = "unban qilindi"
    else:
        data.setdefault("banned_users", []).append(user_id)
        result = "ban qilindi"
    save_data(data)
    await call.answer(f"👤 Foydalanuvchi {result}", show_alert=True)
    await show_user_management(call.message, user_id)












# ---- Qo'lda pul o'tkazish / VIP berish ----

TX_MEDIA_LABELS = {"photo": "🖼 Rasm", "video": "🎬 Video", "sticker": "🎨 Stiker",
                   "document": "📄 Fayl", "animation": "🎞 GIF"}


def _tx_money(value) -> str:
    return f"{float(value or 0):,.0f}".replace(",", " ")


def _tx_media_label(tx: dict) -> str:
    return TX_MEDIA_LABELS.get(str(tx.get("media_type") or ""), "yo‘q")


def _tx_user_text(tx: dict, balance: float, vip_until: int = 0) -> str:
    kind = tx.get("kind")
    lines = []
    if kind == "vip":
        days = int(tx.get("days") or 0)
        lines += ["💎 <b>VIP obunangiz faollashtirildi!</b>", ""]
        lines.append("📅 Muddat: <b>cheksiz</b>" if days == 0 else f"📅 Muddat: <b>{days} kun</b>")
        if vip_until:
            lines.append(f"⏳ Tugash vaqti: <b>{fmt_date(vip_until)}</b>")
        amount = float(tx.get("amount") or 0)
        if amount > 0:
            lines.append(f"💰 To‘lov: <b>{_tx_money(amount)} so‘m</b> — balansingizga qo‘shildi")
    elif kind == "out":
        lines += ["💰 <b>Balansingizdan pul yechildi</b>", "",
                  f"➖ Yechildi: <b>{_tx_money(tx.get('charged'))} so‘m</b>"]
    else:
        lines += ["💰 <b>Balansingiz to‘ldirildi</b>", "",
                  f"➕ Qo‘shildi: <b>{_tx_money(tx.get('amount'))} so‘m</b>"]
    lines.append(f"💳 Balansingiz: <b>{_tx_money(balance)} so‘m</b>")
    if tx.get("desc"):
        lines += ["", f"📝 Izoh: {_esc(str(tx['desc']))}"]
    lines += ["", f"🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}"]
    return "\n".join(lines)


def _tx_confirm_text(tx: dict) -> str:
    user = load_data().get("users", {}).get(str(tx["uid"]), {})
    balance = float(user.get("balance", 0) or 0)
    name = user.get("first_name") or user.get("username") or "-"
    lines = ["🧾 <b>Amalni tasdiqlang</b>", "",
             f"👤 Foydalanuvchi: <b>{_esc(str(name))}</b>",
             f"🆔 ID: <code>{tx['uid']}</code>"]
    if tx.get("kind") == "vip":
        days = int(tx.get("days") or 0)
        lines.append("💎 VIP: <b>cheksiz</b>" if days == 0 else f"💎 VIP: <b>{days} kun</b>")
    if float(tx.get("amount") or 0) > 0:
        shown = min(float(tx["amount"]), balance) if tx.get("kind") == "out" else float(tx["amount"])
        verb = "Balansdan yechiladi" if tx.get("kind") == "out" else "Balansga qo‘shiladi"
        lines.append(f"💰 {verb}: <b>{_tx_money(shown)} so‘m</b>")
    else:
        lines.append("💰 Summa: <b>yo‘q</b>")
    if tx.get("kind") in {"add", "out"}:
        lines.append(f"💳 Hozirgi balans: <b>{_tx_money(balance)} so‘m</b>")
    lines.append(f"🖼 Ilova: <b>{_tx_media_label(tx)}</b>")
    if tx.get("desc"):
        lines += ["", f"📝 Izoh: {_esc(str(tx['desc']))}"]
    lines += ["", "Foydalanuvchiga xabar yuboriladi."]
    return "\n".join(lines)


def _tx_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="txok"),
         InlineKeyboardButton(text="❌ Bekor qilish", callback_data="txcancel")],
    ])


async def _tx_send_to_user(tx: dict, text: str) -> bool:
    uid = int(tx["uid"])
    media_type = str(tx.get("media_type") or "")
    chat_id = tx.get("media_chat_id")
    msg_id = tx.get("media_message_id")
    kb = main_menu(uid)
    if media_type and chat_id and msg_id:
        if media_type != "sticker" and len(text) <= 1000:
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_id,
                                       caption=small_caps_html(text), reply_markup=kb)
                return True
            except Exception:
                logging.exception("Qo'lda to'lov: media caption bilan yuborilmadi")
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_id)
        except Exception:
            logging.exception("Qo'lda to'lov: ilova yuborilmadi")
    await bot.send_message(uid, text, reply_markup=kb)
    return True


@dp.callback_query(F.data.startswith("uservip:"))
async def user_vip_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id): return await call.answer("Faqat owner uchun", show_alert=True)
    uid=int(call.data.split(":",1)[1]); await state.update_data(manage_uid=uid); await state.set_state(Form.manage_vip_days)
    await call.message.answer("💎 VIP muddatini kunlarda yuboring (masalan, 30). 0 yuborsangiz cheksiz VIP beriladi:")
    await call.answer()

@dp.message(Form.manage_vip_days)
async def user_vip_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id): return await state.clear()
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=owner_menu())
    try: days=int(text); assert days >= 0
    except Exception: return await message.answer("💎 0 yoki musbat kun sonini yuboring:")
    ctx=await state.get_data(); uid=int(ctx["manage_uid"])
    if not load_data().get("users",{}).get(str(uid)):
        await state.clear(); return await message.answer("❌ Foydalanuvchi topilmadi.")
    await state.update_data(tx={"kind": "vip", "uid": uid, "days": days, "amount": 0, "desc": ""})
    await state.set_state(Form.manage_vip_amount)
    await message.answer(
        "💰 VIP uchun to‘lov summasini so‘mda yuboring (masalan, 50000).\n"
        "Summa foydalanuvchi balansiga ham qo‘shiladi.\n\n"
        "Kerak bo‘lmasa: /skipp\nBekor qilish: /cancel")

@dp.message(Form.manage_vip_amount)
async def user_vip_amount_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id): return await state.clear()
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=owner_menu())
    if text.lower() in {"/skipp", "/skip"}:
        amount = 0.0
    else:
        try: amount=float(text.replace(" ", "").replace(",", ".")); assert amount >= 0
        except Exception: return await message.answer("💰 Summani raqamda yuboring (masalan, 50000) yoki /skipp bosing:")
    ctx=await state.get_data(); tx=ctx.get("tx") or {}
    tx["amount"]=amount
    await state.update_data(tx=tx)
    await state.set_state(Form.manage_tx_desc)
    await message.answer(
        "📝 Izoh (tavsif) yozing — foydalanuvchi xabarida ko‘rinadi.\n\n"
        "Kerak bo‘lmasa: /skipp\nBekor qilish: /cancel")

@dp.callback_query(F.data.startswith("uservipremove:"))
async def user_vip_remove(call: CallbackQuery):
    if not is_owner(call.from_user.id): return await call.answer("Faqat owner uchun", show_alert=True)
    uid=int(call.data.split(":",1)[1]); data=load_data(); user=data.get("users",{}).get(str(uid),{})
    await purge_vip_media(uid, user)
    user["vip_until"]=0; save_data(data); await call.answer("❌ VIP bekor qilindi",show_alert=True); await show_user_management(call.message,uid)

@dp.callback_query(F.data.startswith(("userbalance:","userbalanceout:")))
async def user_balance_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id): return await call.answer("Faqat owner uchun", show_alert=True)
    mode,uid=call.data.split(":",1); await state.update_data(manage_uid=int(uid), balance_mode=mode); await state.set_state(Form.manage_balance_amount)
    await call.message.answer("💰 Summani so‘mda yuboring (faqat musbat son):"); await call.answer()

@dp.message(Form.manage_balance_amount)
async def user_balance_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id): return await state.clear()
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=owner_menu())
    try: amount=float(text.replace(" ", "").replace(",",".")); assert amount > 0
    except Exception: return await message.answer("💰 Musbat summa kiriting:")
    ctx=await state.get_data(); uid=int(ctx["manage_uid"])
    if not load_data().get("users",{}).get(str(uid)):
        await state.clear(); return await message.answer("❌ Foydalanuvchi topilmadi.")
    kind = "add" if ctx.get("balance_mode") == "userbalance" else "out"
    await state.update_data(tx={"kind": kind, "uid": uid, "amount": amount, "desc": ""})
    await state.set_state(Form.manage_tx_desc)
    await message.answer(
        "📝 Izoh (tavsif) yozing — foydalanuvchi xabarida ko‘rinadi.\n\n"
        "Kerak bo‘lmasa: /skipp\nBekor qilish: /cancel")

@dp.message(Form.manage_tx_desc)
async def manage_tx_desc_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id): return await state.clear()
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=owner_menu())
    has_media = bool(message.photo or message.video or message.document
                     or message.sticker or message.animation)
    if has_media:
        return await message.answer("📝 Avval izohni yozing yoki /skipp bosing — ilovani keyingi qadamda yuborasiz.")
    desc = "" if text.lower() in {"/skipp", "/skip"} else text[:900]
    ctx=await state.get_data(); tx=ctx.get("tx") or {}
    tx["desc"]=desc
    await state.update_data(tx=tx)
    await state.set_state(Form.manage_tx_media)
    await message.answer(
        "🖼 Rasm, video, fayl (txt) yoki stiker yuboring — u foydalanuvchiga shu xabar bilan boradi.\n\n"
        "Kerak bo‘lmasa: /skipp\nBekor qilish: /cancel")

@dp.message(Form.manage_tx_media)
async def manage_tx_media_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id): return await state.clear()
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=owner_menu())
    ctx=await state.get_data(); tx=ctx.get("tx") or {}
    if text.lower() in {"/skipp", "/skip"}:
        tx.pop("media_type", None); tx.pop("media_chat_id", None); tx.pop("media_message_id", None)
    else:
        media_type = ("photo" if message.photo else "video" if message.video
                      else "sticker" if message.sticker else "document" if message.document
                      else "animation" if message.animation else "")
        if not media_type:
            return await message.answer("🖼 Rasm, video, fayl yoki stiker yuboring. Kerak bo‘lmasa: /skipp")
        tx["media_type"]=media_type
        tx["media_chat_id"]=message.chat.id
        tx["media_message_id"]=message.message_id
    await state.update_data(tx=tx)
    await state.set_state(Form.manage_tx_confirm)
    await message.answer(_tx_confirm_text(tx), reply_markup=_tx_confirm_kb())

@dp.message(Form.manage_tx_confirm)
async def manage_tx_confirm_wait(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id): return await state.clear()
    if (message.text or "").strip() == "/cancel":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=owner_menu())
    await message.answer("🧾 Tasdiqlash uchun ✅ Tasdiqlash yoki ❌ Bekor qilish tugmasini bosing.")

@dp.callback_query(F.data.in_({"txok", "txcancel"}))
async def manage_tx_confirm(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("Faqat owner uchun", show_alert=True)
    ctx = await state.get_data(); tx = ctx.get("tx")
    if not tx or "uid" not in tx:
        await state.clear()
        return await call.answer("Bu amal allaqachon yakunlangan.", show_alert=True)
    if call.data == "txcancel":
        await state.clear()
        try: await call.message.edit_text("❎ Amal bekor qilindi.")
        except Exception: pass
        return await call.answer("Bekor qilindi")
    data = load_data(); user = data.get("users", {}).get(str(tx["uid"]))
    if not user:
        await state.clear()
        return await call.answer("❌ Foydalanuvchi topilmadi", show_alert=True)
    kind = tx.get("kind"); amount = float(tx.get("amount") or 0)
    balance = float(user.get("balance", 0) or 0)
    vip_until = 0; charged = amount
    if kind == "add":
        balance += amount
    elif kind == "out":
        charged = min(amount, balance)
        balance = max(0, balance - amount)
    elif kind == "vip":
        days = int(tx.get("days") or 0)
        vip_until = now_ts() + (days * 86400 if days > 0 else 100 * 365 * 86400)
        user["vip_until"] = vip_until
        user.pop("vip_warned_until", None)
        balance += amount
    user["balance"] = balance
    tx["charged"] = charged
    save_data(data)
    delivered = True
    try:
        await _tx_send_to_user(tx, _tx_user_text(tx, balance, vip_until))
    except Exception:
        delivered = False
        logging.exception("Qo'lda to'lov: foydalanuvchiga xabar yuborilmadi")
    await state.clear()
    tail = ("\n\n✅ Bajarildi, foydalanuvchiga xabar yuborildi." if delivered
            else "\n\n⚠️ Bajarildi, lekin foydalanuvchiga xabar yetkazilmadi (botni bloklagan bo‘lishi mumkin).")
    try: await call.message.edit_text(_tx_confirm_text(tx) + tail)
    except Exception: pass
    await call.answer("✅ Bajarildi." if delivered else "⚠️ Xabar yetkazilmadi", show_alert=not delivered)
    await show_user_management(call.message, int(tx["uid"]))

@dp.callback_query(F.data.startswith("usermute:"))
async def user_mute_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id): return await call.answer("Faqat owner uchun", show_alert=True)
    uid=int(call.data.split(":",1)[1]); await state.update_data(manage_uid=uid); await state.set_state(Form.manage_mute_duration)
    await call.message.answer("🔇 Mute muddatini daqiqalarda yuboring (masalan, 60 = 1 soat, 1440 = 1 kun):"); await call.answer()

@dp.message(Form.manage_mute_duration)
async def user_mute_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id): return await state.clear()
    try: minutes=int((message.text or "").strip()); assert minutes > 0
    except Exception: return await message.answer("🔇 Musbat daqiqa sonini yuboring:")
    ctx=await state.get_data(); uid=int(ctx["manage_uid"]); data=load_data(); user=data.get("users",{}).get(str(uid))
    if not user: await state.clear(); return await message.answer("❌ Foydalanuvchi topilmadi.")
    user["mute_until"] = now_ts()+minutes*60; save_data(data); await state.clear(); await message.answer("✅ Mute muddati saqlandi."); await show_user_management(message,uid)

@dp.callback_query(F.data.startswith("usermuteoff:"))
async def user_mute_off(call: CallbackQuery):
    if not is_owner(call.from_user.id): return await call.answer("Faqat owner uchun", show_alert=True)
    uid=int(call.data.split(":",1)[1]); data=load_data(); data.get("users",{}).setdefault(str(uid),{})["mute_until"]=0
    save_data(data); await call.answer("🔊 Mute bekor qilindi",show_alert=True); await show_user_management(call.message,uid)

@dp.message(Command("backup"))
async def backup_command(message: Message):
    if not is_owner(message.from_user.id):
        return await message.answer("⛔ Bu buyruq faqat owner uchun.")
    payload = json.dumps(load_data(), ensure_ascii=False, indent=2).encode("utf-8")
    doc = BufferedInputFile(payload, filename=f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    backup_channel_id = get_backup_channel_id()
    if backup_channel_id:
        await bot.send_document(backup_channel_id, doc, caption="🤖 Bot zaxirasi")
        await message.answer("✅ Zaxira kanalga yuborildi.")
    else:
        await message.answer_document(doc, caption="💾 Bot zaxirasi")

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Sizda admin huquqi yo'q.")
    try: await message.delete()
    except Exception: pass
    await message.answer("🎬 <b>Kino va serial boshqaruvi</b>", reply_markup=admin_menu(message.from_user.id))


@dp.message(Command("owner"))
async def owner_command(message: Message):
    if not is_owner(message.from_user.id):
        return await message.answer("⛔ Bu buyruq faqat bot egasi uchun.")
    try: await message.delete()
    except Exception: pass
    await message.answer("⚙️ <b>Owner paneli</b>", reply_markup=owner_menu())


@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    data = load_data()
    try:
        await call.message.edit_text(data["start_text"], reply_markup=main_menu(call.from_user.id))
    except Exception:
        await call.message.answer(data["start_text"], reply_markup=main_menu(call.from_user.id))
    await call.answer()


@dp.callback_query(F.data == "search_menu")
async def search_menu_open(call: CallbackQuery):
    try:
        await call.message.edit_text("🔎 <b>Kino va video izlash</b>", reply_markup=search_menu(call.from_user.id))
    except Exception:
        await call.message.answer("🔎 <b>Kino va video izlash</b>", reply_markup=search_menu(call.from_user.id))
    await call.answer()


@dp.callback_query(F.data == "guide")
async def guide(call: CallbackQuery):
    text = (
        "📖 <b>Botdan foydalanish qo‘llanmasi</b>\n\n"
        "1️⃣ <b>Kino qidirish</b> tugmasini bosing.\n"
        "2️⃣ Kerakli kontentni kod yoki nomi orqali qidiring.\n"
        "3️⃣ Chiqqan nomni tanlang — kino yoki serial ma’lumotlari ochiladi.\n"
        "4️⃣ Serial bo‘lsa, kerakli faslni, keyin qism raqamini bosing.\n"
        "5️⃣ Qism ochilgach, pastdagi ⬅️ va ➡️ tugmalari bilan oldingi yoki keyingi qismga o‘ting.\n\n"
        "🔒 Agar bot majburiy kanalga obuna bo‘lishni so‘rasa, kanallarga qo‘shiling va «Obunani tekshirish»ni bosing.\n"
        "👥 Do‘stlaringizni taklif qiling: har 3 ta do‘st uchun 3 kunlik VIP olasiz — /ref buyrug‘i orqali havolangizni oling.\n"
        "💎 Saqlash himoyasi yoqilganida VIP bo‘lmaganlar yuborilgan videoni Telegram orqali saqlay yoki boshqa joyga uzata olmaydi. VIP foydalanuvchilarga bu cheklov qo‘yilmaydi."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Kino qidirish", callback_data="search_menu")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="home")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


def _catalog_key(text: str, tags: list[str]) -> str:
    # #1, #2 kabi raqamli teglar qism raqami; qolgan teglar guruh identifikatori.
    identity = next((tag.casefold() for tag in tags if not tag.isdigit()), "")
    if not identity:
        identity = re.sub(r"#\w+", " ", text.splitlines()[0] if text else "")
        identity = re.sub(r"^(nom|title|kino|media)\s*:\s*", "", identity, flags=re.I)
        identity = " ".join(identity.casefold().split())
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12] if identity else ""


@dp.channel_post()
async def index_storage_channel_post(message: Message):
    """Bitta kontent bazasi kanalidagi postlarni kod, fasl va qism bo‘yicha indekslaydi.

    Caption misoli: #kino_25 #season_2 #3 yoki #serial_25 #season_2 #ep_3.
    Bot ushbu bazaviy kanalda administrator bo‘lishi kerak.
    """
    data = load_data()
    settings = data.get("settings", {})
    channel_id = str(message.chat.id)
    catalog_ref = str(settings.get("catalog_channel_id") or CHANNEL_ID or "").strip()
    def matches(ref):
        if not ref:
            return False
        if ref.startswith("@"):
            return (message.chat.username or "").casefold() == ref[1:].casefold()
        return channel_id == ref
    tier = "regular" if matches(catalog_ref) else ""
    shared_base = bool(matches(catalog_ref))
    if not tier:
        return
    text = (message.caption or message.text or "").strip()
    tags = re.findall(r"#([\w]+)", text, flags=re.UNICODE)
    code_tag = next((tag.split("_", 1)[1] for tag in tags if tag.casefold().startswith(("kino_", "serial_", "movie_")) and tag.split("_", 1)[1].isdigit()), None)
    season_tag = next((tag.split("_", 1)[1] for tag in tags if tag.casefold().startswith(("season_", "fasl_")) and tag.split("_", 1)[1].isdigit()), "1")
    part_tag = next((tag.split("_", 1)[1] for tag in tags if tag.casefold().startswith(("ep_", "qism_")) and tag.split("_", 1)[1].isdigit()), None)
    if not part_tag:
        part_tag = next((tag for tag in tags if tag.isdigit()), None)
    if code_tag and code_tag in data.get("medias", {}) and part_tag:
        media = data["medias"][code_tag]
        season = media.setdefault("seasons", {}).setdefault(str(int(season_tag)), {"episodes": {}})
        season.setdefault("episodes", {}).setdefault(str(int(part_tag)), {})[tier] = {"chat_id": message.chat.id, "message_id": message.message_id}
        # Legacy `episodes` mapping retained for old content and old callbacks.
        if str(int(season_tag)) == "1":
            media.setdefault("episodes", {}).setdefault(str(int(part_tag)), {})[tier] = {"chat_id": message.chat.id, "message_id": message.message_id}
        save_data(data)
        logging.info("Indexed %s: code=%s season=%s episode=%s", tier, code_tag, season_tag, part_tag)
        return
    # Backward compatibility for the old title #1 format; do not mix tiers.
    if not text:
        return
    old_tags = [tag for tag in tags if "_" not in tag]
    old_part = next((tag for tag in old_tags if tag.isdigit()), None)
    title_lines = [re.sub(r"#([\w]+)", "", line).strip(" \t-|:") for line in text.splitlines()]
    title_lines = [line for line in title_lines if line]
    title = title_lines[0] if title_lines else ""
    matched_code = next((code for code, item in data.get("medias", {}).items() if normalize_media_title(item.get("name", "")) == normalize_media_title(title)), None)
    if matched_code and old_part:
        media = data["medias"][matched_code]
        season = media.setdefault("seasons", {}).setdefault("1", {"episodes": {}})
        season.setdefault("episodes", {}).setdefault(str(int(old_part)), {})[tier] = {"chat_id": message.chat.id, "message_id": message.message_id}
        save_data(data)
        return
    # Eski umumiy katalog indekslashni saqlab qolamiz.
    key = _catalog_key(text, tags)
    if not key:
        return
    item = data.setdefault("channel_catalog", {}).setdefault(key, {"name": "", "description": "", "search_text": "", "parts": {}, "updated_at": now_ts()})
    if old_part:
        item["name"] = item.get("name") or title or "Nomsiz"
        item.setdefault("parts", {})[str(int(old_part))] = {"chat_id": message.chat.id, "message_id": message.message_id, "tier": tier}
    elif title:
        item["name"] = title
        item["description"] = "\n".join(title_lines[1:]).strip()
        item["info_chat_id"] = message.chat.id
        item["info_message_id"] = message.message_id
    item["search_text"] = (item.get("search_text", "") + " " + text).casefold()
    item["updated_at"] = now_ts()
    save_data(data)


@dp.callback_query(F.data.startswith("catalog_show:"))
async def catalog_show(call: CallbackQuery):
    data = load_data()
    key = call.data.split(":", 1)[1]
    item = data.get("channel_catalog", {}).get(key)
    if not item:
        return await call.answer("❌ Bu kino bazada topilmadi.", show_alert=True)
    if int(call.from_user.id) in [int(x) for x in data.get("banned_users", [])]:
        return await call.answer("⛔ Botdan foydalanish taqiqlangan.", show_alert=True)
    if not await enforce_subscription(call.from_user.id):
        await call.message.answer("🔒 Avval majburiy kanallarga obuna bo'ling yoki 💎 VIP oling.", reply_markup=subscription_keyboard())
        return await call.answer()
    parts = item.get("parts", {})
    rows, row = [], []
    tier = "regular"
    visible_parts = {number: part for number, part in parts.items() if part.get("tier") in (None, "", tier)}
    for number in sorted(visible_parts, key=lambda value: int(value) if str(value).isdigit() else 999999):
        row.append(InlineKeyboardButton(text=str(number), callback_data=f"catalog_part:{key}:{number}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Qidiruvga qaytish", callback_data="search_menu")])
    description = item.get("description", "")
    caption = f"🎬 <b>{item.get('name', 'Nomsiz kino')}</b>\n\n{description}\n\n📼 Qismlar: {len(parts)} ta"[:3900]
    await call.message.answer(caption, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("catalog_part:"))
async def catalog_send_part(call: CallbackQuery):
    data = load_data()
    try:
        _, key, number = call.data.split(":", 2)
        item = data["channel_catalog"][key]
        part = item["parts"][number]
        tier = "regular"
        if part.get("tier") not in (None, "", tier):
            return await call.answer("⚠️ Bu qism noto'g'ri bazaga biriktirilgan.", show_alert=True)
    except (KeyError, ValueError):
        return await call.answer("❌ Qism topilmadi.", show_alert=True)
    if int(call.from_user.id) in [int(x) for x in data.get("banned_users", [])]:
        return await call.answer("⛔ Botdan foydalanish taqiqlangan.", show_alert=True)
    if not await enforce_subscription(call.from_user.id):
        await call.message.answer("🔒 Avval majburiy kanallarga obuna bo'ling yoki 💎 VIP oling.", reply_markup=subscription_keyboard())
        return await call.answer()
    user = data.setdefault("users", {}).setdefault(str(call.from_user.id), {})
    matched_code = next((code for code, media in data.get("medias", {}).items() if normalize_media_title(media.get("name", "")) == normalize_media_title(item.get("name", ""))), None)
    user["last_watched"] = {"catalog_key": key, "code": matched_code, "number": number, "media_type": "movie", "updated_at": now_ts()}
    save_data(data)
    try:
        protect = should_protect_content(call.from_user.id, data)
        sent = await bot.copy_message(chat_id=call.from_user.id, from_chat_id=part["chat_id"], message_id=part["message_id"], protect_content=protect)
        track_media_msg(call.from_user.id, getattr(sent, "message_id", None))
        await call.answer(f"{number}-qism yuborildi")
    except Exception:
        logging.exception("Catalog part copy failed")
        await call.answer("❌ Qism yuborilmadi. Botning kanal adminligi va postni tekshiring.", show_alert=True)


@dp.callback_query(F.data == "search_name")
async def search_name_start(call: CallbackQuery, state: FSMContext):
    await state.update_data(search_filter="all")
    await state.set_state(Form.search_name)
    await call.message.answer("🔤 Qidirilayotgan kino yoki serial nomini yozing. Har safar yuborganingizda mos nomlar tanlash uchun chiqadi:")
    await call.answer()


@dp.callback_query(F.data.startswith("category:"))
async def category_search_start(call: CallbackQuery, state: FSMContext):
    media_type = call.data.split(":", 1)[1]
    labels = {"movie":"kino", "series":"serial", "drama":"drama", "anime":"anime", "cartoon":"multfilm"}
    await state.update_data(search_filter=media_type)
    await state.set_state(Form.search_name)
    await call.message.answer(f"🔎 {labels.get(media_type, 'kontent').capitalize()} bo'limi. Nomini yozing — mos kelgan nomlar tanlash uchun chiqadi:")
    await call.answer()


@dp.message(Form.search_name)
async def search_name_result(message: Message, state: FSMContext):
    if not await require_subscription_message(message):
        return
    query = (message.text or "").strip().casefold()
    if not query:
        return await message.answer("✍️ Nomdan kamida bir necha harf yozing:")
    saved = await state.get_data()
    media_filter = saved.get("search_filter", "all")
    data = load_data()
    candidates = [(str(code), item) for code, item in data.get("medias", {}).items()
                  if item.get("media_type", "series") in {"movie", "series", "drama", "anime", "cartoon"}
                  and (media_filter == "all" or item.get("media_type", "series") == media_filter)
                  and media_available_for_user(message.from_user.id, item, data)]
    # Avval aniq qism/substring, keyin imlo xatosiga o'xshash nomlar.
    matches = []
    normalized_query = re.sub(r"[^a-z0-9а-яё]", "", query)
    for code, item in candidates:
        name = str(item.get("name", ""))
        folded = name.casefold()
        normalized_name = re.sub(r"[^a-z0-9а-яё]", "", folded)
        ratio = difflib.SequenceMatcher(None, normalized_query, normalized_name).ratio()
        token_scores = [difflib.SequenceMatcher(None, normalized_query, token).ratio() for token in re.findall(r"[a-z0-9а-яё]+", folded)]
        score = max([ratio] + token_scores)
        if query in folded or (len(normalized_query) >= 3 and score >= (0.48 if len(normalized_query) <= 5 else 0.42)):
            matches.append((code, item, score))
    matches.sort(key=lambda pair: (query not in pair[1].get("name", "").casefold(), -pair[2], pair[1].get("name", "").casefold()))
    if not matches:
        return await message.answer("❌ Mos nom topilmadi. Boshqa nom yoki qismini yozib ko'ring:", reply_markup=search_menu(message.from_user.id))
    rows = []
    for code, item, _score in matches[:40]:
        icon = {"movie":"🎞", "series":"📺", "drama":"🎭", "anime":"🍥", "cartoon":"🎨"}.get(item.get("media_type"), "🎬")
        rows.append([InlineKeyboardButton(text=f"{icon} {item.get('name','Nomsiz')} · {code}", callback_data=f"show:{code}")])
    rows.append([InlineKeyboardButton(text="🔎 Yana qidirish", callback_data="search_name")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="search_menu")])
    await message.answer(f"🔎 <b>Topilgan nomlar: {len(matches)}</b>\nKeraklisini tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.clear()


@dp.callback_query(F.data == "search")
async def search_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.search_code)
    await call.message.answer("🔢 Kino yoki serial kodini yuboring:")
    await call.answer()


@dp.message(Form.search_code)
async def search_code(message: Message, state: FSMContext):
    await state.clear()
    if not await require_subscription_message(message):
        return
    data = load_data()
    code = message.text.strip() if message.text else ""
    media = data["medias"].get(code)
    if not media:
        return await message.answer("❌ Bu kod bo‘yicha kino yoki serial topilmadi.")
    await send_media_card(message.chat.id, code, media)


@dp.callback_query(F.data == "page_info")
async def page_info(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("season_page:"))
async def season_page(call: CallbackQuery):
    try:
        _, code, season_no, page = call.data.split(":")
        data = load_data(); media = data["medias"][code]
        seasons = media.get("seasons") or {"1": {"episodes": media.get("episodes", {})}}
        episodes = seasons.get(season_no, {}).get("episodes", {})
        markup = episode_keyboard(code, episodes, user_id=call.from_user.id, season=season_no, media=media, page=int(page))
        await call.message.edit_reply_markup(reply_markup=markup)
    except Exception:
        await call.answer("⚠️ Sahifani ochib bo‘lmadi.", show_alert=True)
    else:
        await call.answer()

@dp.callback_query(F.data.startswith("season_show:"))
async def season_show(call: CallbackQuery):
    try:
        _, code, season = call.data.split(":", 2)
        data = load_data()
        media = data["medias"][code]
        season_data = (media.get("seasons") or {}).get(season, {"episodes": media.get("episodes", {})})
        kb = episode_keyboard(code, season_data.get("episodes", {}), user_id=call.from_user.id, season=season, media=media)
        title = f"🎬 <b>{media['name']}</b>\n\n📺 {season}-fasl — qismni tanlang:" if season != "choose" else f"🎬 <b>{media['name']}</b>\n\n📺 Faslni tanlang:"
        await call.message.answer(title, reply_markup=kb)
        await call.answer()
    except (KeyError, ValueError):
        await call.answer("❌ Fasl topilmadi.", show_alert=True)


def episode_navigation_keyboard(code, season_no, current_no, available_numbers):
    def sk(v): return (0, int(v)) if str(v).isdigit() else (1, str(v))
    nums = sorted([str(n) for n in available_numbers], key=sk)
    try: idx = nums.index(str(current_no))
    except ValueError: idx = -1
    row = []
    if idx > 0: row.append(InlineKeyboardButton(text="⬅️ Oldingi qism", callback_data=f"episode:{code}:{season_no}:{nums[idx-1]}"))
    if idx >= 0 and idx < len(nums)-1: row.append(InlineKeyboardButton(text="Keyingi qism ➡️", callback_data=f"episode:{code}:{season_no}:{nums[idx+1]}"))
    rows = [row] if row else []
    rows.append([InlineKeyboardButton(text="📋 Qismlar ro'yxati", callback_data=f"season_show:{code}:{season_no}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("episode:"))
async def send_episode(call: CallbackQuery):
    data = load_data()
    if int(call.from_user.id) in [int(x) for x in data.get("banned_users", [])]:
        return await call.answer("⛔ Botdan foydalanish taqiqlangan.", show_alert=True)
    if not await enforce_subscription(call.from_user.id):
        await call.message.answer(
            "🔒 Avval kanallarga obuna bo'ling yoki 💎 VIP oling.", reply_markup=subscription_keyboard()
        )
        return await call.answer()
    try:
        parts = call.data.split(":")
        if len(parts) == 3:
            _, code, number = parts
            season_number = "1"
        else:
            _, code, season_number, number = parts
        current_data = load_data()
        media = current_data["medias"][code]
        season_data = (media.get("seasons") or {}).get(season_number, {"episodes": media.get("episodes", {})})
        episode_record = season_data.get("episodes", {}).get(number) or media.get("episodes", {}).get(number)
        if not episode_record:
            await call.answer("⏳ Bu qism hali joylanmagan.", show_alert=True)
            return
        tier = "regular"
        if isinstance(episode_record, dict) and "regular" in episode_record:
            episode = episode_record["regular"]
        elif isinstance(episode_record, dict) and "file_id" in episode_record:
            # Yangi, ruchnoy qo'shilgan qism.
            episode = episode_record
        elif isinstance(episode_record, dict) and "chat_id" in episode_record:
            # Eski kanalga bog'langan qismlar bilan moslik.
            episode = episode_record
        else:
            episode = None
        if not episode or not isinstance(episode, dict):
            await call.answer("⏳ Bu qism hali qo'shilmagan.", show_alert=True)
            return
        user = current_data.setdefault("users", {}).setdefault(str(call.from_user.id), {})
        user["last_watched"] = {"code": code, "season": season_number, "number": number, "media_type": media.get("media_type", "series"), "updated_at": now_ts()}
        save_data(current_data)
        protect = should_protect_content(call.from_user.id, current_data)
        available_numbers = []
        for ep_no, ep_rec in season_data.get("episodes", {}).items():
            if isinstance(ep_rec, dict) and ("file_id" in ep_rec or "chat_id" in ep_rec or "regular" in ep_rec):
                available_numbers.append(str(ep_no))
        nav_markup = episode_navigation_keyboard(code, season_number, number, available_numbers)
        if episode.get("file_id"):
            media_type = episode.get("media_type", "video")
            file_id = episode["file_id"]
            caption = episode.get("caption") or f"🎬 <b>{media.get('name', 'Kontent')}</b> — {number}-qism"
            send_method = {
                "video": bot.send_video,
                "document": bot.send_document,
                "audio": bot.send_audio,
                "animation": bot.send_animation,
                "photo": bot.send_photo,
                "voice": bot.send_voice,
            }.get(media_type, bot.send_video)
            send_kwargs = {
                "chat_id": call.from_user.id,
                media_type: file_id,
                "caption": caption,
                "protect_content": protect,
                "reply_markup": nav_markup,
            }
            # Bot API 8.3+: video cover is separate from the video file_id.
            # Old records may not contain cover_id, so keep them compatible.
            if media_type == "video" and episode.get("cover_id"):
                send_kwargs["cover"] = episode["cover_id"]
            if media_type == "video" and episode.get("start_timestamp") is not None:
                send_kwargs["start_timestamp"] = episode["start_timestamp"]
            sent = await send_method(**send_kwargs)
            track_media_msg(call.from_user.id, getattr(sent, "message_id", None))
        elif "chat_id" in episode and "message_id" in episode:
            sent = await bot.copy_message(chat_id=call.from_user.id, from_chat_id=episode["chat_id"], message_id=episode["message_id"], protect_content=protect, reply_markup=nav_markup)
            track_media_msg(call.from_user.id, getattr(sent, "message_id", None))
        else:
            await call.answer("⚠️ Qism ma'lumoti noto'g'ri saqlangan.", show_alert=True)
            return
        await call.answer("✅ Qism yuborildi")
    except Exception:
        await call.answer("❌ Qism yuborilmadi. Kanal va xabarni tekshiring.", show_alert=True)


@dp.callback_query(F.data.startswith("category_list:"))
async def category_list_callback(call: CallbackQuery):
    media_type = call.data.split(":", 1)[1]
    labels = {"movie":("🎞 Kinolar", "📭 Kino hali qo'shilmagan"), "series":("📺 Seriallar", "📭 Serial hali qo'shilmagan"), "drama":("🎭 Dramalar", "📭 Drama hali qo'shilmagan"), "anime":("🍥 Anime", "📭 Anime hali qo'shilmagan"), "cartoon":("🎨 Multfilmlar", "📭 Multfilm hali qo'shilmagan")}
    title, empty = labels.get(media_type, ("🎬 Kontent", "📭 Hozircha bo'sh"))
    await media_category_list(call, media_type, title, empty)


@dp.callback_query(F.data == "movie_list")
async def movie_list(call: CallbackQuery):
    data = load_data()
    if not await enforce_subscription(call.from_user.id):
        await call.message.answer("🔒 Avval majburiy kanallarga obuna bo‘ling yoki 💎 VIP oling.", reply_markup=subscription_keyboard())
        return await call.answer()
    items = [(code, item) for code, item in data.get("medias", {}).items() if item.get("media_type") == "movie" and media_available_for_user(call.from_user.id, item, data)]
    rows = [[InlineKeyboardButton(text=f"🎞 {item['name']} · {code}", callback_data=f"show:{code}")] for code, item in items[:80]]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="search_menu")])
    await call.message.answer("🎞 <b>Kinolar ro‘yxati</b>" if items else "📭 Hozircha kino qo‘shilmagan.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data == "series_list")
async def series_list(call: CallbackQuery):
    await media_category_list(call, "series", "📺 Seriallar ro‘yxati", "📭 Hozircha serial qo‘shilmagan.")


async def media_category_list(call: CallbackQuery, media_type: str, title: str, empty_text: str, page: int = 0):
    data = load_data()
    if not await enforce_subscription(call.from_user.id):
        await call.message.answer("🔒 Avval majburiy kanallarga obuna bo‘ling yoki 💎 VIP oling.", reply_markup=subscription_keyboard())
        return await call.answer()
    items = [(str(code), item) for code, item in data.get("medias", {}).items()
             if item.get("media_type", "series") == media_type
             and media_available_for_user(call.from_user.id, item, data)]
    items.sort(key=lambda pair: pair[1].get("name", "").casefold())
    page_size = 10
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(int(page), total_pages - 1))
    visible = items[page * page_size:(page + 1) * page_size]
    rows = [[InlineKeyboardButton(text=f"🎬 {item.get('name', 'Nomsiz')} · {code}", callback_data=f"show:{code}")]
            for code, item in visible]
    if total_pages > 1:
        left_btn = InlineKeyboardButton(text="⬅️", callback_data=f"media_page:{media_type}:{page-1}") if page > 0 else None
        middle_btn = InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="page_info")
        right_btn = InlineKeyboardButton(text="➡️", callback_data=f"media_page:{media_type}:{page+1}") if page < total_pages - 1 else None
        rows.extend(build_pager_row(left_btn, middle_btn, right_btn))
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="search_menu")])
    text = f"<b>{title}</b>\n\nJami: {len(items)} ta" if items else empty_text
    try:
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("media_page:"))
async def media_page_callback(call: CallbackQuery):
    try:
        _, media_type, page = call.data.split(":")
    except ValueError:
        return await call.answer()
    labels = {"movie":("🎞 Kinolar ro'yxati", "📭 Hozircha kino qo'shilmagan."),
              "anime":("🍥 Anime ro'yxati", "📭 Hozircha anime qo'shilmagan."),
              "cartoon":("🎨 Multiklar ro'yxati", "📭 Hozircha multik qo'shilmagan."),
              "series":("📺 Seriallar ro'yxati", "📭 Hozircha serial qo'shilmagan.")}
    title, empty = labels.get(media_type, ("🎬 Kontent ro'yxati", "📭 Hozircha bo'sh."))
    await media_category_list(call, media_type, title, empty, int(page))


@dp.callback_query(F.data.startswith("favorite_add:"))
async def favorite_add(call: CallbackQuery):
    data = load_data()
    code = call.data.split(":", 1)[1]
    if code not in data.get("medias", {}):
        return await call.answer("❌ Topilmadi", show_alert=True)
    user = data.setdefault("users", {}).setdefault(str(call.from_user.id), {"id": call.from_user.id, "favorites": []})
    favs = user.setdefault("favorites", [])
    if code in favs:
        favs.remove(code)
        msg = "Sevimlilardan olib tashlandi"
    else:
        favs.append(code)
        msg = "Yoqtirganlaringizga saqlandi ⭐"
    save_data(data)
    await call.answer(msg, show_alert=True)


@dp.callback_query(F.data == "favorites")
async def favorites_list(call: CallbackQuery):
    data = load_data()
    user = data.get("users", {}).get(str(call.from_user.id), {})
    codes = user.get("favorites", [])
    rows = []
    for code in codes:
        item = data.get("medias", {}).get(str(code))
        if item:
            icon = "🎞" if item.get("media_type") == "movie" else "🍥"
            rows.append([InlineKeyboardButton(text=f"{icon} {item['name']} · {code}", callback_data=f"show:{code}")])
    rows.append([InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")])
    await call.message.answer("⭐ <b>Yoqtirganlarim</b>" if len(rows) > 1 else "⭐ Hozircha saqlangan narsa yo‘q.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data == "continue_watching")
async def continue_watching(call: CallbackQuery):
    data = load_data()
    user = data.get("users", {}).get(str(call.from_user.id), {})
    last = user.get("last_watched") or {}
    code = str(last.get("code", ""))
    item = data.get("medias", {}).get(code)
    if item:
        await send_media_card(call.from_user.id, code, item)
        await call.answer("▶️ Davom ettirish uchun kerakli qismni bosing")
    elif last.get("catalog_key") in data.get("channel_catalog", {}):
        key = last["catalog_key"]
        catalog_item = data["channel_catalog"][key]
        parts = catalog_item.get("parts", {})
        rows = [[InlineKeyboardButton(text=f"▶️ {number}-qism", callback_data=f"catalog_part:{key}:{number}")] for number in sorted(parts, key=lambda value: int(value) if str(value).isdigit() else 999999)]
        await call.message.answer(f"🎬 <b>{catalog_item.get('name', 'Nomsiz')}</b>\n\n{catalog_item.get('description', '')}", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await call.answer("✅ Oxirgi ko‘rilgan kino topildi")
    else:
        await call.answer("ℹ️ Hali tomosha qilgan narsangiz yo‘q.", show_alert=True)


@dp.callback_query(F.data.startswith("show:"))
async def show_media(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    data = load_data()
    if code not in data["medias"]:
        return await call.answer("❌ Kontent topilmadi", show_alert=True)
    if int(call.from_user.id) in [int(x) for x in data.get("banned_users", [])]:
        return await call.answer("⛔ Botdan foydalanish taqiqlangan.", show_alert=True)
    if not await enforce_subscription(call.from_user.id):
        await call.message.answer("🔒 Avval majburiy kanallarga obuna bo'ling yoki 💎 VIP oling.", reply_markup=subscription_keyboard())
        return await call.answer()
    try:
        await call.message.edit_text("🎬 <b>Kontent ochildi.</b>")
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    item = data["medias"][code]
    await send_media_card(call.from_user.id, code, item)
    # Faqat poster va mavjud qismlar tugmalari ko'rsatiladi; qism avtomatik yuborilmaydi.
    await call.answer()


async def ask(call: CallbackQuery, state: FSMContext, new_state: State, prompt: str):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Adminlar uchun.", show_alert=True)
    await state.set_state(new_state)
    await call.message.answer(prompt)
    await call.answer()


@dp.callback_query(F.data == "admin_add_content")
async def admin_add_content_callback(call: CallbackQuery, state: FSMContext):
    if not has_permission(call.from_user.id, "add_content"):
        return await call.answer("⛔ Sizda kontent qo'shish huquqi yo'q.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎞 Kino", callback_data="newtype:movie"), InlineKeyboardButton(text="🍥 Anime", callback_data="newtype:anime")],
        [InlineKeyboardButton(text="🎨 Multfilm", callback_data="newtype:cartoon")],
    ])
    await call.message.answer("🎬 Qaysi turdagi kontent qo'shamiz?", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "add_movie")
async def add_movie(call: CallbackQuery, state: FSMContext):
    if not has_permission(call.from_user.id, "add_content"):
        return await call.answer("⛔ Sizda kontent qo'shish huquqi yo'q.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎞 Kino", callback_data="newtype:movie"), InlineKeyboardButton(text="🍥 Anime", callback_data="newtype:anime")],
        [InlineKeyboardButton(text="🎨 Multfilm", callback_data="newtype:cartoon")],
    ])
    await call.message.answer("🎬 Qaysi turdagi kontent qo'shamiz?", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "add_series")
async def add_series(call: CallbackQuery, state: FSMContext):
    if not has_permission(call.from_user.id, "add_content"):
        return await call.answer("⛔ Sizda kontent qo'shish huquqi yo'q.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📺 Serial", callback_data="newtype:series")],
        [InlineKeyboardButton(text="🍥 Anime", callback_data="newtype:anime"), InlineKeyboardButton(text="🎨 Multfilm", callback_data="newtype:cartoon")],
    ])
    await call.message.answer("🎬 Qaysi turdagi kontent qo'shamiz?", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("newtype:"))
async def choose_new_media_type(call: CallbackQuery, state: FSMContext):
    if not has_permission(call.from_user.id, "add_content"):
        return await call.answer("⛔ Sizda kontent qo'shish huquqi yo'q.", show_alert=True)
    media_type = call.data.split(":", 1)[1]
    await state.update_data(media_type=media_type)
    await state.set_state(Form.media_code)
    await call.message.answer("🔢 Yangi kontent uchun takrorlanmaydigan kod yuboring:")
    await call.answer()


@dp.message(Form.media_code)
async def media_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    code = (message.text or "").strip()
    data = load_data()
    if not code or code in data["medias"]:
        return await message.answer("❌ Kod bo'sh yoki band. Boshqa kod yuboring:")
    await state.update_data(new_code=code)
    await state.set_state(Form.media_name)
    saved = await state.get_data()
    await message.answer({"movie":"🎞 Kino nomini", "series":"📺 Serial nomini", "drama":"🎭 Drama nomini", "anime":"🍥 Anime nomini", "cartoon":"🎨 Multfilm nomini"}.get(saved.get("media_type"), "🎬 Kontent nomini") + " yuboring:")


@dp.message(Form.media_name)
async def media_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    name = (message.text or "").strip()
    if not name:
        return await message.answer("✍️ Kontent nomini matn ko'rinishida yuboring:")
    await state.update_data(new_name=name)
    await state.set_state(Form.media_info)
    await message.answer(
        "🖼 Endi poster rasmi va ma'lumotini yuboring.\n"
        "• Rasmni caption bilan yuboring; yoki\n"
        "• Faqat tavsif matnini yuboring.\n"
        "Rasm/tavsif kerak bo'lmasa /skip yuboring."
    )


@dp.message(Form.media_info)
async def media_info(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    poster = None
    description = ""
    forwarded_record = None
    origin = getattr(message, "forward_origin", None)
    if origin and getattr(origin, "type", "") == "channel":
        source_chat = getattr(origin, "chat", None)
        source_message_id = getattr(origin, "message_id", None)
        if not source_chat or not source_message_id:
            return await message.answer("❌ Forward manbasini aniqlab bo'lmadi. Kanal postini to'g'ridan-to'g'ri forward qiling.")
        try:
            me = await bot.get_me()
            member = await bot.get_chat_member(source_chat.id, me.id)
            status = str(member.status).split(".")[-1].lower()
            if status not in ("administrator", "creator"):
                return await message.answer("❌ Bot bu manba kanal/guruhda admin emas. Botni har bir kontent kanaliga administrator qiling.")
        except Exception as exc:
            logging.warning("Forward source permission check failed: %s", exc)
            return await message.answer("❌ Manba kanalni tekshirib bo'lmadi. Bot shu kanal/guruhda administrator ekanini tekshiring.")
        forwarded_record = {"chat_id": source_chat.id, "message_id": source_message_id}
        description = (message.caption or "").strip()
        if message.photo:
            poster = message.photo[-1].file_id
    elif message.text and message.text.strip() == "/skip":
        pass
    elif message.photo:
        poster = message.photo[-1].file_id
        description = (message.caption or "").strip()
    elif message.text:
        description = message.text.strip()
    else:
        return await message.answer("🖼 Rasm yoki matn yuboring, yoki /skip bosing.")
    await state.update_data(pending_poster=poster, pending_description=description, pending_forward_record=forwarded_record)
    await state.set_state(Form.media_details)
    await message.answer(
        "📝 Qo‘shimcha ma’lumotlarni quyidagi ko‘rinishda yuboring:\n"
        "Janr: Ekshn, Fantastika\nHolati: Tugallangan\nSifati: 720p, 1080p\nOvoz: Anibla\n\n"
        "Kerak bo‘lmagan qatorlarni tashlab ketsangiz bo‘ladi. Hammasini o‘tkazish uchun /skip yuboring."
    )


@dp.message(Form.media_details)
async def media_details_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    saved = await state.get_data()
    raw = (message.text or "").strip()
    fields = {"genres": "", "status": "", "quality": "", "voice": ""}
    if raw and raw != "/skip":
        aliases = {"janr": "genres", "genre": "genres", "holati": "status", "status": "status", "sifati": "quality", "sifat": "quality", "quality": "quality", "ovoz": "voice", "voice": "voice", "studio": "voice"}
        for line in raw.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                field = aliases.get(key.strip().casefold())
                if field:
                    fields[field] = value.strip()
    code, name = str(saved["new_code"]), str(saved["new_name"])
    data = load_data()
    media_type = saved.get("media_type", "series")
    first_episode = saved.get("pending_forward_record")
    seasons = {"1": {"episodes": ({"1": first_episode} if first_episode else {})}}
    data["medias"][code] = {
        "name": name, "description": saved.get("pending_description", ""),
        "poster_file_id": saved.get("pending_poster"), "episodes": ({"1": first_episode} if first_episode else {}), "seasons": seasons,
        "created_at": now_ts(), "media_type": media_type, **fields
    }
    save_data(data)
    await state.set_state(Form.media_season_count)
    await message.answer("📺 Bu kontent nechta fasldan iborat? Faqat son yuboring. Kino uchun 1 deb yozing:")


async def notify_content_added(code: str, item: dict):
    """Yangi media/kino qo‘shilganda barcha ro‘yxatdan o‘tgan foydalanuvchilarga xabar."""
    data = load_data()
    is_movie = item.get("media_type") == "movie"
    label = "KINO" if is_movie else "SERIAL"
    years = item.get("year", "") or item.get("years", "") or "Yili ko'rsatilmagan"
    genre = item.get("genre", "") or item.get("genres", "") or "Janr ko'rsatilmagan"
    seasons = item.get("seasons") or {"1": {"episodes": item.get("episodes", {})}}
    episode_count = sum(len(v.get("episodes", {})) for v in seasons.values() if isinstance(v, dict))
    text = (f"🔥 <b>Premyera!</b>\n\n"
            f"🎬 Nomi: {item.get('name', 'Nomsiz')}\n"
            f"Kodi: <code>{code}</code>\n\n"
            f"📥 Barcha qismlar tayyor — hoziroq <code>{code}</code> kodi orqali yuklab oling!👇")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎬 Tomosha qilish", callback_data=f"show:{code}")]])
    if not data.get("settings", {}).get("notify_new_content", True):
        return
    for uid in list(data.get("users", {})):
        try:
            await bot.send_message(int(uid), text, reply_markup=kb)
        except Exception as exc:
            logging.info("New content notification failed for %s: %s", uid, exc)


@dp.message(Form.media_season_count)
async def media_season_count_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    try:
        count = int((message.text or "").strip())
    except ValueError:
        return await message.answer("🔢 Fasllar sonini raqam bilan yuboring, masalan 1 yoki 3:")
    if count < 1 or count > 30:
        return await message.answer("⚠️ Fasllar soni 1 dan 30 gacha bo‘lishi kerak:")
    saved = await state.get_data()
    code = str(saved.get("new_code"))
    data = load_data()
    item = data.get("medias", {}).get(code)
    if not item:
        await state.clear()
        return await message.answer("❌ Kontent topilmadi. Qaytadan qo‘shing.", reply_markup=admin_menu(message.from_user.id))
    existing_eps = item.get("seasons", {}).get("1", {}).get("episodes", {})
    item["seasons"] = {str(n): {"episodes": {}} for n in range(1, count + 1)}
    if existing_eps:
        item["seasons"].setdefault("1", {"episodes": {}})["episodes"].update(existing_eps)
    save_data(data)
    await state.clear()
    await notify_content_added(code, item)
    await message.answer(
        f"✅ <b>{item['name']}</b> (kod <code>{code}</code>) botga qo‘shildi.\n\n"
        f"📺 Fasllar: {count} ta\n\n"
        "Endi serial/anime qismlarini <b>Qism yoki yangi sezon</b> bo'limidan qo'shing.\n"
        "Kino qo'shayotganda esa bot admin bo'lgan kanal/guruh postini forward qilsangiz, u 1-qism sifatida saqlanadi.",
        reply_markup=admin_menu()
    )


@dp.callback_query(F.data == "add_episode")
async def add_episode(call: CallbackQuery, state: FSMContext):
    await ask(call, state, Form.episode_code, "🔢 Qismlar qo‘shiladigan kino yoki serial kodini yuboring:")


@dp.message(Form.episode_code)
async def episode_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    code = (message.text or "").strip()
    data = load_data()
    if code not in data["medias"]:
        return await message.answer("❌ Bunday kod topilmadi. Qayta yuboring:")
    item = data["medias"][code]
    seasons = item.get("seasons") or {"1": {"episodes": item.get("episodes", {})}}
    await state.update_data(batch_code=code)
    await state.set_state(Form.episode_season)
    await message.answer(
        f"🎬 <b>{item['name']}</b> tanlandi.\nMavjud fasllar: {', '.join(sorted(seasons, key=lambda x: int(x) if str(x).isdigit() else 999))}\n\n"
        "Fasl raqamini yuboring (masalan: 2). Yangi fasl bo‘lsa ham raqamini yozing."
    )


@dp.message(Form.episode_season)
async def episode_season_select(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 100:
        return await message.answer("🔢 Fasl raqamini 1 dan 100 gacha raqam bilan yuboring, masalan 2:")
    season_no = str(int(raw))
    saved = await state.get_data()
    code = str(saved.get("batch_code", ""))
    data = load_data()
    item = data.get("medias", {}).get(code)
    if not item:
        await state.clear()
        return await message.answer("❌ Kontent topilmadi. Qaytadan boshlang.", reply_markup=admin_menu(message.from_user.id))
    item.setdefault("seasons", {}).setdefault(season_no, {"episodes": {}})
    save_data(data)
    await state.update_data(batch_season=season_no, batch_count=0)
    await state.set_state(Form.episode_batch)
    prompt = await message.answer(
        f"🎬 <b>{item['name']}</b> · <b>{season_no}-fasl</b>\nQismlarni bittadan yoki bir nechtasini ketma-ket yuboring. Bot tavsifdagi 1-qism, Qism: 2 va shunga o'xshash raqamlarni taniydi; raqam topilmasa navbatdagi bo'sh raqamni beradi.\n/done — tugatish · /cancel — bekor qilish"
    )
    await state.update_data(progress_message_id=prompt.message_id)


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


@dp.message(Form.episode_batch)
async def episode_batch_message(message: Message, state: FSMContext):
    """Qism faqat bot administrator bo'lgan kanaldan forward qilinganda qabul qilinadi."""
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    text = (message.text or "").strip().lower()
    if text in ("/done", "done", "tayyor"):
        saved = await state.get_data()
        count = int(saved.get("batch_count", 0))
        await state.clear()
        return await message.answer(f"✅ Yakunlandi. {count} ta qism saqlandi.", reply_markup=admin_menu(message.from_user.id))
    if text == "/cancel":
        saved = await state.get_data()
        code = str(saved.get("batch_code", "")); season_no = str(saved.get("batch_season", "1"))
        added = saved.get("batch_added", [])
        data = load_data(); item = data.get("medias", {}).get(code)
        if item:
            eps = item.get("seasons", {}).get(season_no, {}).get("episodes", {})
            for n in added:
                eps.pop(str(n), None)
                if season_no == "1": item.get("episodes", {}).pop(str(n), None)
            save_data(data)
        await state.clear()
        return await message.answer("❎ Bekor qilindi. Shu jarayonda qo'shilgan qismlar olib tashlandi; avvalgi qismlar saqlandi.", reply_markup=admin_menu(message.from_user.id))

    # Media istalgan manbadan qabul qilinadi: kanal, mavzu, guruh yoki shaxsiy chat.
    # Faylni serverga yuklamaymiz; Telegram file_id yoki kerak bo'lsa source message saqlanadi.
    media_type = None
    file_id = None
    cover_id = None
    start_timestamp = None
    for attr in ("video", "document", "audio", "animation", "photo", "voice"):
        obj = getattr(message, attr, None)
        if obj:
            media_type = attr
            file_id = (obj[-1].file_id if attr == "photo" else obj.file_id)
            if attr == "video":
                covers = getattr(obj, "cover", None) or []
                if covers:
                    cover_id = covers[-1].file_id
                start_timestamp = getattr(obj, "start_timestamp", None)
            break
    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None) if origin else None
    origin_message_id = getattr(origin, "message_id", None) if origin else None
    if not file_id and not (origin_chat and origin_message_id):
        return await message.answer("❌ Video yoki media yuboring/forward qiling. Tugatish: /done")

    saved = await state.get_data()
    code = str(saved.get("batch_code", ""))
    data = load_data()
    item = data.get("medias", {}).get(code)
    if not item:
        await state.clear()
        return await message.answer("❌ Kino/serial topilmadi. Qaytadan boshlang.", reply_markup=admin_menu(message.from_user.id))
    season_no = str(saved.get("batch_season", "1"))
    season = item.setdefault("seasons", {}).setdefault(season_no, {"episodes": {}})
    episodes = season.setdefault("episodes", {})
    numeric = [int(n) for n in episodes if str(n).isdigit()]
    next_number = max(numeric, default=0) + 1
    caption_text = " ".join(filter(None, [message.caption or "", message.text or ""]))
    detected_number = _detect_part_number(caption_text)
    number = detected_number if detected_number and detected_number > 0 else next_number
    note = ""
    if str(number) in episodes:
        existing_record = episodes.get(str(number)) or {}
        if file_id and existing_record.get("file_id") == file_id:
            return await message.answer(f"⚠️ Bu media allaqachon {number}-qism sifatida saqlangan.")
        taken = number
        number = next_number
        note = f"\nℹ️ {taken}-qism band edi — {number}-qism sifatida saqlandi."
    if origin_chat and origin_message_id:
        # Asl postni nusxalash media xabari va mavjud coverni saqlash uchun afzal.
        record = {"chat_id": origin_chat.id, "message_id": origin_message_id, "media_type": media_type or "video"}
        if cover_id:
            record["cover_id"] = cover_id
        if start_timestamp is not None:
            record["start_timestamp"] = start_timestamp
    elif file_id:
        record = {"file_id": file_id, "media_type": media_type or "video", "caption": (message.caption or "")[:900]}
        if cover_id:
            record["cover_id"] = cover_id
        if start_timestamp is not None:
            record["start_timestamp"] = start_timestamp
    episodes[str(number)] = record
    if season_no == "1":
        item.setdefault("episodes", {})[str(number)] = record
    save_data(data)
    count = int(saved.get("batch_count", 0)) + 1
    added = list(saved.get("batch_added", [])); added.append(str(number))
    await state.update_data(batch_count=count, batch_added=added)
    progress_id = saved.get("progress_message_id")
    if progress_id:
        try:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=progress_id,
                text=f"🎬 <b>{item['name']}</b> · {season_no}-fasl\n✅ {count} ta media qabul qilindi. Oxirgisi: <b>{number}-qism</b>.{note}\n\nPostlarni yuborishda davom eting.\nTugatish: /done · Bekor qilish: /cancel")
        except Exception:
            await message.answer(f"✅ {number}-qism saqlandi.{note}\nKeyingi postni forward qiling yoki /done yozing.")
    else:
        await message.answer(f"✅ {number}-qism saqlandi.{note}\nKeyingi postni forward qiling yoki /done yozing.")

@dp.callback_query(F.data == "edit_poster")
async def edit_poster_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id) and not has_permission(call.from_user.id, "edit_content"):
        return await call.answer("⛔ Sizda tahrirlash huquqi yo'q.", show_alert=True)
    await state.set_state(Form.edit_poster_code)
    await call.message.answer("🖼 Posterini almashtirmoqchi bo'lgan kontent kodini yuboring:")
    await call.answer()


@dp.message(Form.edit_poster_code)
async def edit_poster_code_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id) and not has_permission(message.from_user.id, "edit_content"):
        await state.clear(); return await message.answer("⛔ Tahrirlash huquqi yo'q.")
    code = (message.text or "").strip()
    if code not in load_data().get("medias", {}):
        return await message.answer("Kod topilmadi. Qayta yuboring:")
    await state.update_data(edit_poster_code=code)
    await state.set_state(Form.edit_poster_file)
    await message.answer("🖼 Yangi poster rasmini yuboring. Posterni olib tashlash uchun /remove yozing:")


@dp.message(Form.edit_poster_file)
async def edit_poster_file_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id) and not has_permission(message.from_user.id, "edit_content"):
        await state.clear(); return await message.answer("⛔ Tahrirlash huquqi yo'q.")
    saved = await state.get_data(); code = str(saved.get("edit_poster_code", ""))
    data = load_data(); item = data.get("medias", {}).get(code)
    if not item:
        await state.clear(); return await message.answer("❌ Kontent topilmadi.")
    if message.photo:
        item["poster_file_id"] = message.photo[-1].file_id
    elif (message.text or "").strip() == "/remove":
        item["poster_file_id"] = None
    else:
        return await message.answer("🖼 Rasm yuboring yoki /remove yozing:")
    save_data(data); await state.clear()
    await message.answer(f"✅ <b>{item.get('name')}</b> posteri yangilandi.", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "admin_permissions")
async def admin_permissions_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("Faqat owner uchun.", show_alert=True)
    await state.set_state(Form.admin_permissions_id)
    await call.message.answer("🔐 Huquqlarini o'zgartirmoqchi bo'lgan admin Telegram ID sini yuboring:")
    await call.answer()


@dp.message(Form.admin_permissions_id)
async def admin_permissions_choose(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear(); return await message.answer("⛔ Faqat owner uchun.")
    try: uid = int((message.text or "").strip())
    except ValueError: return await message.answer("🆔 Telegram ID raqamini yuboring:")
    data = load_data()
    if uid not in data.get("admins", []) and uid not in ADMIN_IDS:
        return await message.answer("⚠️ Bu ID adminlar ro'yxatida yo'q. Avval admin qo'shing:")
    perms = set(get_admin_permissions(uid))
    labels = [("add_content", "Kontent qo'shish"), ("add_episode", "Qism/sezon qo'shish"), ("edit_content", "Nom/poster tahrirlash"), ("delete_content", "Kontent o'chirish")]
    rows = [[InlineKeyboardButton(text=("✅ " if key in perms else "⬜ ")+label, callback_data=f"perm_toggle:{uid}:{key}")] for key,label in labels]
    rows.append([InlineKeyboardButton(text="✅ Tayyor", callback_data="admin_home")])
    await state.clear()
    await message.answer(f"🔐 <b>{uid}</b> admin huquqlari. Bosib yoqing/o'chiring:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("perm_toggle:"))
async def admin_permission_toggle(call: CallbackQuery):
    if not is_owner(call.from_user.id): return await call.answer("Faqat owner uchun.", show_alert=True)
    _, uid_raw, perm = call.data.split(":", 2); uid = int(uid_raw)
    valid = {"add_content", "add_episode", "edit_content", "delete_content"}
    if perm not in valid: return await call.answer("Noto'g'ri huquq", show_alert=True)
    data = load_data(); permissions = set(data.setdefault("admin_permissions", {}).get(str(uid), ["add_content", "add_episode"]))
    if perm in permissions: permissions.remove(perm)
    else: permissions.add(perm)
    data["admin_permissions"][str(uid)] = sorted(permissions); save_data(data)
    labels = [("add_content", "Kontent qo'shish"), ("add_episode", "Qism/sezon qo'shish"), ("edit_content", "Nom/poster tahrirlash"), ("delete_content", "Kontent o'chirish")]
    rows = [[InlineKeyboardButton(text=("✅ " if key in permissions else "⬜ ")+label, callback_data=f"perm_toggle:{uid}:{key}")] for key,label in labels]
    rows.append([InlineKeyboardButton(text="✅ Tayyor", callback_data="admin_home")])
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer("✅ Huquq yangilandi")


@dp.callback_query(F.data == "del_media")
async def del_media(call: CallbackQuery, state: FSMContext):
    if not has_permission(call.from_user.id, "delete_content"):
        return await call.answer("⛔ Sizda o'chirish huquqi yo'q.", show_alert=True)
    await ask(call, state, Form.delete_code, "🗑 O'chiriladigan kino yoki serial kodini yuboring:")


@dp.message(Form.delete_code)
async def delete_media(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    code = (message.text or "").strip()
    data = load_data()
    if code not in data["medias"]:
        return await message.answer("❌ media kodi topilmadi:")
    name = data["medias"][code]["name"]
    del data["medias"][code]
    save_data(data)
    await state.clear()
    await message.answer(f"🗑 <b>{name}</b> o'chirildi.", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "del_episode")
async def del_episode(call: CallbackQuery, state: FSMContext):
    await ask(call, state, Form.delete_episode_code, "🗑 Qismi o'chiriladigan media kodini yuboring:")


@dp.message(Form.delete_episode_code)
async def delete_episode_ask_number(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    code = (message.text or "").strip()
    data = load_data()
    if code not in data["medias"]:
        return await message.answer("❌ media kodi topilmadi. Qayta yuboring:")
    await state.update_data(delete_episode_code=code)
    await state.set_state(Form.delete_episode_number)
    await message.answer("🗑 O'chiriladigan qism raqamini yuboring:")


@dp.message(Form.delete_episode_number)
async def delete_episode_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    number = (message.text or "").strip()
    saved = await state.get_data()
    data = load_data()
    code = saved["delete_episode_code"]
    if code not in data["medias"] or number not in data["medias"][code]["episodes"]:
        return await message.answer("❌ Bunday qism topilmadi. Qism raqamini qayta yuboring:")
    media = data["medias"][code]
    removed = False
    seasons = media.get("seasons", {})
    for season_no, season_data in seasons.items():
        eps = season_data.get("episodes", {})
        if number in eps:
            del eps[number]; removed = True
            if str(season_no) == "1": media.get("episodes", {}).pop(number, None)
            break
    if not removed and number in media.get("episodes", {}):
        del media["episodes"][number]; removed = True
    if not removed:
        return await message.answer("❌ Bunday qism topilmadi. Qism raqamini qayta yuboring:")
    save_data(data)
    await state.clear()
    await message.answer(f"🗑 {number}-qism o'chirildi.", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "del_season")
async def del_season_start(call: CallbackQuery, state: FSMContext):
    if not has_permission(call.from_user.id, "delete_content"):
        return await call.answer("⛔ Sizda o‘chirish huquqi yo‘q.", show_alert=True)
    await ask(call, state, Form.delete_season_code, "🗑 Faslini o‘chirmoqchi bo‘lgan kino/serial kodini yuboring:")

@dp.message(Form.delete_season_code)
async def del_season_code(message: Message, state: FSMContext):
    if not has_permission(message.from_user.id, "delete_content"):
        await state.clear(); return
    code = (message.text or "").strip(); data = load_data()
    if code not in data.get("medias", {}):
        return await message.answer("Kod topilmadi. Qayta yuboring:")
    await state.update_data(delete_season_code=code)
    await state.set_state(Form.delete_season_number)
    await message.answer("🗑 O‘chiriladigan fasl raqamini yuboring:")

@dp.message(Form.delete_season_number)
async def del_season_number(message: Message, state: FSMContext):
    if not has_permission(message.from_user.id, "delete_content"):
        await state.clear(); return
    raw = (message.text or "").strip()
    if not raw.isdigit(): return await message.answer("🔢 Fasl raqamini raqam bilan yuboring:")
    saved = await state.get_data(); code = str(saved.get("delete_season_code", "")); sn = str(int(raw))
    data = load_data(); media = data.get("medias", {}).get(code)
    if not media or sn not in media.get("seasons", {}):
        return await message.answer("❌ Bunday fasl topilmadi. Qayta yuboring:")
    count = len(media["seasons"][sn].get("episodes", {}))
    del media["seasons"][sn]
    if sn == "1": media["episodes"] = {}
    save_data(data); await state.clear()
    await message.answer(f"🗑 {sn}-fasl va undagi {count} ta qism o‘chirildi.", reply_markup=admin_menu(message.from_user.id))

@dp.callback_query(F.data == "rename_media")
async def rename_start(call: CallbackQuery, state: FSMContext):
    await ask(call, state, Form.rename_code, "✏️ Nomi o'zgaradigan media kodini yuboring:")


@dp.message(Form.rename_code)
async def rename_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    code = (message.text or "").strip()
    if code not in load_data()["medias"]:
        return await message.answer("❌ Kod topilmadi:")
    await state.update_data(rename_code=code)
    await state.set_state(Form.rename_name)
    await message.answer("✏️ Yangi media nomini yuboring:")


@dp.message(Form.rename_name)
async def rename_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    saved = await state.get_data()
    data = load_data()
    data["medias"][saved["rename_code"]]["name"] = (message.text or "").strip()
    save_data(data)
    await state.clear()
    await message.answer("✅ media nomi o'zgartirildi.", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "recode_media")
async def recode_start(call: CallbackQuery, state: FSMContext):
    await ask(call, state, Form.recode_old, "🔢 Eski media kodini yuboring:")


@dp.message(Form.recode_old)
async def recode_old(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    old = (message.text or "").strip()
    if old not in load_data()["medias"]:
        return await message.answer("❌ Eski kod topilmadi:")
    await state.update_data(recode_old=old)
    await state.set_state(Form.recode_new)
    await message.answer("🔢 Yangi, band bo'lmagan kodni yuboring:")


@dp.message(Form.recode_new)
async def recode_new(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    new = (message.text or "").strip()
    saved = await state.get_data()
    data = load_data()
    if not new or new in data["medias"]:
        return await message.answer("❌ Yangi kod bo'sh yoki band:")
    data["medias"][new] = data["medias"].pop(saved["recode_old"])
    save_data(data)
    await state.clear()
    await message.answer(f"✅ Kod {new} qilib o'zgartirildi.", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "add_admin")
async def add_admin_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Bu bo‘lim faqat bot egasi uchun.", show_alert=True)
    await ask(call, state, Form.admin_id_add, "👤 Admin qilinadigan foydalanuvchining Telegram ID raqamini yuboring:")


@dp.message(Form.admin_id_add)
async def add_admin_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        return await message.answer("🔢 Faqat raqam yuboring:")
    data = load_data()
    if user_id not in data["admins"]:
        data["admins"].append(user_id)
    data.setdefault("admin_permissions", {}).setdefault(str(user_id), ["add_content", "add_episode"])
    save_data(data)
    await state.clear()
    await message.answer(f"✅ Adminlar ro'yxatiga qo'shildi: <code>{user_id}</code>", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "del_admin")
async def del_admin_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Bu bo‘lim faqat bot egasi uchun.", show_alert=True)
    await ask(call, state, Form.admin_id_del, "👤 Adminlikdan olinadigan Telegram ID raqamini yuboring:")


@dp.message(Form.admin_id_del)
async def del_admin_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        return await message.answer("🔢 Faqat raqam yuboring:")
    data = load_data()
    if user_id == OWNER_ID:
        return await message.answer("❌ Asosiy adminni o'chirib bo'lmaydi.")
    if user_id in data["admins"]:
        data["admins"].remove(user_id)
        save_data(data)
    await state.clear()
    await message.answer("✅ Admin ro'yxati yangilandi.", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "add_channel")
async def add_channel_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Bu bo‘lim faqat bot egasi uchun.", show_alert=True)
    await ask(call, state, Form.channel_add,
        "Kanalni yuboring. Ochiq kanal: @username\n"
        "Maxfiy kanal: -100...ID | https://t.me/+taklif_havolasi\n"
        "Masalan: -1001234567890 | https://t.me/+AbCdEfGh\n"
        "Bot kanal ichida administrator bo'lishi kerak. Maxfiy kanal uchun ID va taklif havolasi ikkalasi ham kerak:")


@dp.message(Form.channel_add)
async def add_channel_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    channel = (message.text or "").strip()
    data = load_data()
    if not channel:
        return await message.answer("❌ Kanal ma'lumoti bo'sh. Qayta yuboring yoki /cancel bosing.")
    chat_ref, invite_url, raw = parse_channel_entry(channel)
    if isinstance(chat_ref, str) and chat_ref.startswith("https://t.me/+"):
        return await message.answer("❌ Maxfiy kanal uchun ID ham kerak: -100...ID | https://t.me/+...")
    if not (str(chat_ref).startswith("@") or (isinstance(chat_ref, int) and str(chat_ref).startswith("-100"))):
        return await message.answer("❌ Kanal formati noto'g'ri. @username yoki -100...ID | invite URL yuboring.")
    if isinstance(chat_ref, int) and not invite_url:
        return await message.answer("⚠️ Kanal ID qabul qilindi, lekin tugma chiqishi uchun taklif havolasi ham kerak.\n"
            "Quyidagi formatda yuboring: -100...ID | https://t.me/+taklif_havolasi\n"
            "Yoki /cancel qilib, to'g'ri formatda qayta boshlang.")
    if raw not in data["channels"]:
        data["channels"].append(raw)
        save_data(data)
    await state.clear()
    await message.answer("✅ Kanal ro'yxatga qo'shildi. Tugma va obunani tekshirish uchun bot o'sha kanalda administrator bo'lishi kerak.", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "del_channel")
async def del_channel_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Bu bo‘lim faqat bot egasi uchun.", show_alert=True)
    await ask(call, state, Form.channel_del, "📢 O'chiriladigan kanal @username yoki ID'sini yuboring:")


@dp.message(Form.channel_del)
async def del_channel_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    channel = (message.text or "").strip()
    data = load_data()
    # Kanalni ID, username yoki ID|invite formatida o'chirish mumkin.
    target_ref, _target_invite, target_raw = parse_channel_entry(channel)
    matches = []
    for entry in data.get("channels", []):
        ref, _invite, raw = parse_channel_entry(entry)
        if raw == target_raw or str(ref) == str(target_ref):
            matches.append(entry)
    for entry in matches:
        data["channels"].remove(entry)
    if matches:
        save_data(data)
    await state.clear()
    if matches:
        await message.answer("✅ Kanal ro'yxatdan o'chirildi.", reply_markup=admin_menu(message.from_user.id))
    else:
        await message.answer("❌ Bunday kanal topilmadi. Kanal ID/username'ni tekshiring.", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "start_text")
async def start_text_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Bu bo‘lim faqat bot egasi uchun.", show_alert=True)
    await ask(call, state, Form.start_text, "✏️ Yangi /start xabarini yuboring. HTML teglardan foydalanishingiz mumkin:")


@dp.message(Form.start_text)
async def start_text_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Bu amal faqat adminlar uchun.")
    data = load_data()
    data["start_text"] = message.html_text or message.text or ""
    save_data(data)
    await state.clear()
    await message.answer("✅ /start xabari o'zgartirildi.", reply_markup=admin_menu(message.from_user.id))


@dp.callback_query(F.data == "referral")
async def referral_callback(call: CallbackQuery):
    if not await enforce_subscription(call.from_user.id):
        return await call.answer("🔒 Avval majburiy kanallarga obuna bo‘ling yoki 💎 VIP oling.", show_alert=True)
    text, link = await referral_text_and_link(call.from_user.id)
    kb = referral_keyboard(link)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "account")
async def account_callback(call: CallbackQuery):
    data = load_data()
    user = data.setdefault("users", {}).get(str(call.from_user.id), {})
    balance = int(user.get("balance", 0) or 0)
    vip_until = int(user.get("vip_until", 0) or 0)
    vip_status = "✅ Ha" if vip_until > now_ts() else "❌ Yo‘q"
    vip_date = fmt_date(vip_until) if vip_until > now_ts() else "—"
    text = (f"👤 <b>Hisobim</b>\n\n🆔 ID: <code>{call.from_user.id}</code>\n"
            f"💰 Balans: <b>{balance:,} so‘m</b>\n👑 VIP obunachi: <b>{vip_status}</b>\n📅 VIP muddati: <b>{vip_date}</b>".replace(",", " "))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 VIP olish", callback_data="vip_buy")], [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="home")]])
    try: await call.message.edit_text(text, reply_markup=kb)
    except Exception: await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "owner_notify_toggle")
async def owner_notify_toggle(call: CallbackQuery):
    if not is_owner(call.from_user.id): return await call.answer("Faqat owner uchun", show_alert=True)
    data = load_data(); cfg = data.setdefault("settings", {})
    cfg["notify_new_content"] = not bool(cfg.get("notify_new_content", True)); save_data(data)
    await call.message.answer("📣 Yangi kontent xabarlari " + ("yoqildi." if cfg["notify_new_content"] else "o‘chirildi."), reply_markup=owner_menu())
    await call.answer()


@dp.callback_query(F.data == "stats")
async def stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Faqat adminlar uchun", show_alert=True)
    data = load_data()
    users = list(data.get("users", {}).values())
    now = now_ts()
    def seen_in(days):
        return sum(1 for u in users if int(u.get("last_seen", u.get("joined_at", 0)) or 0) >= now - days * 86400)
    def joined_in(days):
        return sum(1 for u in users if int(u.get("joined_at", 0) or 0) >= now - days * 86400)
    active30 = seen_in(30)
    inactive = max(0, len(users) - active30)
    medias = list(data.get("medias", {}).values())
    movie_count = sum(1 for m in medias if m.get("media_type") == "movie")
    anime_count = sum(1 for m in medias if m.get("media_type") == "anime")
    cartoon_count = sum(1 for m in medias if m.get("media_type") == "cartoon")
    series_count = sum(1 for m in medias if m.get("media_type") in {"series", "drama"})
    downloads = data.get("downloads", {})
    vip_ever = sum(1 for u in users if int(u.get("vip_until", 0) or 0) > 0)
    vip_active = sum(1 for u in users if int(u.get("vip_until", 0) or 0) > now)
    referral_total = sum(1 for u in users if u.get("referral_qualified"))
    referral_pending = sum(1 for u in users if u.get("referrer") and not u.get("referral_qualified"))
    referral_rewards = sum(int(u.get("referral_rewards", 0) or 0) for u in users)
    def downloads_in(days):
        return sum(1 for t in downloads.values() if isinstance(t, (int, float)) and t >= now - days * 86400) if isinstance(downloads, dict) else 0
    await call.message.answer(
        f"📊 <b>Statistika</b>\n• Obunachilar soni: {len(users)} ta\n• Faol obunachilar: {active30} ta\n• Tark etganlar: {inactive} ta\n\n"
        f"📈 <b>Obunachilar qo‘shilishi</b>\n• Oxirgi 24 soat: +{joined_in(1)} obunachi\n• Oxirgi 7 kun: +{joined_in(7)} obunachi\n• Oxirgi 30 kun: +{joined_in(30)} obunachi\n\n"
        f"💎 <b>VIP statistikasi</b>\n• VIP olganlar (qayd etilgan): {vip_ever} ta\n• Hozir VIP faol: {vip_active} ta\n\n"
        f"👥 <b>Referal statistikasi</b>\n• Hisoblangan referallar: {referral_total} ta\n• Obunani kutayotganlar: {referral_pending} ta\n• Berilgan VIP mukofotlar: {referral_rewards} ta\n\n"
        f"📊 <b>Faollik</b>\n• Oxirgi 24 soatda faol: {seen_in(1)} ta\n• Oxirgi 7 kun faol: {seen_in(7)} ta\n• Oxirgi 30 kun faol: {active30} ta\n\n"
        f"📥 <b>Yuklanishlar</b>\n• Oxirgi 24 soat: {downloads_in(1)} ta\n• Oxirgi 7 kun: {downloads_in(7)} ta\n• Oxirgi 30 kun: {downloads_in(30)} ta\n\n"
        f"🎬 Kinolar soni: {movie_count} ta\n🍥 Anime: {anime_count} ta\n🎨 Multfilm: {cartoon_count} ta\n📺 Serial/drama: {series_count} ta"
    )
    await call.answer()


@dp.callback_query(F.data == "appearance")
async def appearance_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Faqat admin va owner uchun", show_alert=True)
    data = load_data()
    colors = data.get("settings", {}).get("button_colors", {})
    rows = [
        [InlineKeyboardButton(text=f"🔎 Qidirish tugmalari: {colors.get('search', 'standart')}", callback_data="color_group:search")],
        [InlineKeyboardButton(text=f"🎬 Kontent tugmalari: {colors.get('content', 'standart')}", callback_data="color_group:content")],
        [InlineKeyboardButton(text=f"⏭ Keyingi qism tugmasi: {colors.get('next_episode', 'standart')}", callback_data="color_group:next_episode")],
        [InlineKeyboardButton(text=f"⬅️ Katalog chap strelkasi: {colors.get('page_left', 'standart')}", callback_data="color_group:page_left")],
        [InlineKeyboardButton(text=f"➡️ Katalog o‘ng strelkasi: {colors.get('page_right', 'standart')}", callback_data="color_group:page_right")],
        [InlineKeyboardButton(text=f"🔢 1/3 sahifa ko‘rsatkichi: {colors.get('page_indicator', 'standart')}", callback_data="color_group:page_indicator")],
        [InlineKeyboardButton(text=f"⬅️ Orqaga tugmalari: {colors.get('back', 'standart')}", callback_data="color_group:back")],
        [InlineKeyboardButton(text=f"🧩 Boshqa tugmalar: {colors.get('other', 'standart')}", callback_data="color_group:other")],
        [InlineKeyboardButton(text="🌈 Barcha tugmalar", callback_data="color_group:all")],
        [InlineKeyboardButton(text="♻️ Ranglarni tozalash", callback_data="color_reset")],
        [InlineKeyboardButton(text="✏️ Tugma nomlarini o‘zgartirish", callback_data="button_text_menu")],
    ]
    if is_owner(call.from_user.id):
        rows.append([InlineKeyboardButton(text="😀 Tugmalarga emoji qo‘shish", callback_data="button_emoji_menu")])
    rows.append([InlineKeyboardButton(text="⬅️ Owner paneli", callback_data="admin_home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await call.message.answer("🎨 <b>Chiroyli ko‘rinish</b>\nTugma guruhini tanlang, keyin rangini belgilang. Telegram inline tugmalarda faqat standart, ko‘k (primary), yashil (success) va qizil (danger) uslublarni beradi; boshqa rangni kod bilan majburlab bo‘lmaydi.", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("color_group:"))
async def color_group_select(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Faqat admin va owner uchun", show_alert=True)
    group = call.data.split(":", 1)[1]
    if group not in {"search", "content", "next_episode", "back", "other", "all", "page_left", "page_right", "page_indicator"}:
        return await call.answer("⚠️ Noto‘g‘ri guruh", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟥 Qizil", callback_data=f"color_set:{group}:danger")],
        [InlineKeyboardButton(text="🟩 Yashil", callback_data=f"color_set:{group}:success")],
        [InlineKeyboardButton(text="🟦 Ko‘k", callback_data=f"color_set:{group}:primary")],
        [InlineKeyboardButton(text="⚪ Standart rang", callback_data=f"color_set:{group}:default")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="appearance")]
    ])
    await call.message.answer("🎨 Rangni tanlang:", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("color_set:"))
async def color_set_save(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Faqat admin va owner uchun", show_alert=True)
    _, group, color = call.data.split(":", 2)
    if group not in {"search", "content", "next_episode", "back", "other", "all", "page_left", "page_right", "page_indicator"} or color not in {"danger", "success", "primary", "default"}:
        return await call.answer("⚠️ Noto‘g‘ri rang", show_alert=True)
    data = load_data()
    colors = data.setdefault("settings", {}).setdefault("button_colors", {})
    # "default" ham alohida qiymat sifatida saqlanadi: shu orqali "all" rangi
    # tanlangan bo‘lsa ham bitta guruhni standart rangda qoldirish mumkin.
    colors[group] = color
    save_data(data)
    await appearance_menu(call)
    await call.answer("✅ Rang saqlandi")


# Sahifalash joylashuvini o‘zgartirish funksiyasi olib tashlandi; standart ⬅️ 1/3 ➡️ saqlanadi.

@dp.callback_query(F.data == "color_reset")
async def color_reset(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Faqat admin va owner uchun", show_alert=True)
    data = load_data()
    data.setdefault("settings", {}).pop("button_colors", None)
    save_data(data)
    await appearance_menu(call)
    await call.answer("✅ Ranglar tozalandi")


# Tugma matnlarini owner panelidan almashtirish mumkin. Kalitlar callback_data bilan bog'langan.
EDITABLE_BUTTONS = {
    "search_menu": "🔎 Kino va serial izlash",
    "guide": "📖 Qo‘llanma",
    "search": "🔢 Kod bo‘yicha qidirish",
    "search_name": "🔠 Nom bo‘yicha qidirish",
    "list": "📚 Kino va seriallar ro‘yxati",
    "movie_list": "🎞 Kinolar ro‘yxati",
    "home": "⬅️ Orqaga / asosiy menyu",
    "add_media": "➕ Kino/serial qo‘shish",
    "add_movie": "➕ Kino qo‘shish",
    "add_series": "➕ Serial qo‘shish",
    "add_episode": "➕ Qism qo‘shish",
    "del_episode": "🗑 Qism o‘chirish",
    "rename_media": "✏️ Nomini o‘zgartirish",
    "recode_media": "🔢 Kodini o‘zgartirish",
    "del_media": "🗑 Kino/serial o‘chirish",
    "stats": "📊 Statistika",
    "broadcast": "📣 Hammaga xabar yuborish",
}

# Owner panelidagi "Tugmalarga emoji" bo'limi shu ro'yxatlar asosida ishlaydi.
# Kalit — tugmaning callback_data si, shu orqali emoji aniq tugmaga bog'lanadi.
BUTTON_EMOJI_GROUPS = [
    ("main", "🏠 Asosiy menyu", [
        ("search_menu", "🔎 Anime va Kino izlash"),
        ("account", "👤 Hisobim"),
        ("referral", "👥 Referal"),
        ("vip_buy", "💎 VIP olish"),
        ("vip_info", "❓ VIP o'zi nima?"),
        ("order_new", "🛒 Buyurtma berish"),
        ("complaint_new", "📝 Shikoyat"),
        ("guide", "📖 Qo'llanma"),
        ("home", "⬅️ Orqaga / asosiy menyu"),
    ]),
    ("search", "🔎 Qidiruv menyusi", [
        ("search", "🔢 Kod bo'yicha izlash"),
        ("search_name", "🔠 Nom bo'yicha izlash"),
        ("category_list:movie", "🎞 Kinolar ro'yxati"),
        ("category_list:series", "📺 Seriallar ro'yxati"),
        ("category_list:anime", "🍥 Anime ro'yxati"),
        ("category_list:cartoon", "🎨 Multfilmlar ro'yxati"),
        ("check_sub", "✅ Obunani tekshirish"),
    ]),
    ("admin", "🛠 Admin / Owner paneli", [
        ("add_movie", "➕ Kino qo'shish"),
        ("add_series", "➕ Serial qo'shish"),
        ("newtype:anime", "🍥 Anime qo'shish"),
        ("newtype:cartoon", "🎨 Multfilm qo'shish"),
        ("add_episode", "➕ Qism / yangi fasl"),
        ("del_media", "🗑 Kontent o'chirish"),
        ("del_season", "🗑 Fasl o'chirish"),
        ("rename_media", "✏️ Nom / kodni tahrirlash"),
        ("edit_poster", "🖼 Posterni tahrirlash"),
        ("stats", "📊 Statistika"),
        ("broadcast", "📣 Hammaga xabar yuborish"),
        ("appearance", "🎨 Tugma ranglari"),
        ("owner_settings", "⚙️ Bot sozlamalari"),
        ("manage_users", "👥 Obunachilarni boshqarish"),
        ("owner_vip", "💎 VIP yoqish/o'chirish"),
        ("owner_orders", "🛒 Buyurtmalar"),
        ("owner_complaints", "📝 Shikoyatlar"),
        ("owner_backup", "💾 Zaxira olish"),
    ]),
]
BUTTON_EMOJI_LABELS = {key: label for _, _, items in BUTTON_EMOJI_GROUPS for key, label in items}


def _button_emoji_mark(value):
    if not value:
        return "▫️"
    return "✅" if str(value).isdigit() else str(value)


def _extract_custom_emoji_id(message: Message):
    """Premium emoji yuborilgan bo'lsa, uning custom_emoji_id sini oladi."""
    for entity in (message.entities or []):
        if entity.type == "custom_emoji" and getattr(entity, "custom_emoji_id", None):
            return str(entity.custom_emoji_id)
    return ""


async def _probe_custom_emoji(emoji_id, chat_id) -> bool:
    """Premium emoji ID ni sinab ko'radi: botga ruxsat bor-yo'qligini aniqlaydi."""
    try:
        probe = TelegramInlineKeyboardButton(text="✅ Emoji test", callback_data="emoji_probe", icon_custom_emoji_id=str(emoji_id))
    except TypeError:
        _CUSTOM_EMOJI_STATE["allowed"] = False
        return False
    try:
        sent = await bot.send_message(chat_id, "🧪 Premium emoji tekshirilmoqda…", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[probe]]))
    except TelegramBadRequest as exc:
        logging.warning("Premium emoji tekshiruvi muvaffaqiyatsiz: %s", exc)
        if _is_custom_emoji_error(exc):
            _CUSTOM_EMOJI_STATE["allowed"] = False
            return False
        return True
    except Exception:
        logging.exception("Premium emoji tekshiruvida xatolik")
        return True
    _CUSTOM_EMOJI_STATE["allowed"] = True
    try:
        await bot.delete_message(sent.chat.id, sent.message_id)
    except Exception:
        pass
    return True


async def verify_custom_emoji_support():
    """Ishga tushishda saqlangan emojilardan birini sinab ko'radi."""
    emojis = load_data().get("settings", {}).get("button_emojis", {})
    emoji_id = next((str(v) for v in emojis.values() if str(v).isdigit()), "")
    if emoji_id and OWNER_ID:
        await _probe_custom_emoji(emoji_id, OWNER_ID)


@dp.callback_query(F.data == "button_emoji_menu")
async def button_emoji_menu(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    await state.clear()
    saved = load_data().get("settings", {}).get("button_emojis", {})
    rows = []
    for group_key, group_label, items in BUTTON_EMOJI_GROUPS:
        count = sum(1 for key, _ in items if saved.get(key))
        suffix = f" · {count} ta" if count else ""
        rows.append([InlineKeyboardButton(text=f"{group_label}{suffix}", callback_data=f"button_emoji_group:{group_key}")])
    rows.append([InlineKeyboardButton(text="♻️ Barcha emojilarni o'chirish", callback_data="button_emoji_reset")])
    rows.append([InlineKeyboardButton(text="⬅️ Owner paneli", callback_data="admin_home")])
    await call.message.answer(
        "😀 <b>Tugmalarga emoji qo'shish</b>\n"
        "1️⃣ Guruhni tanlang\n2️⃣ Tugmani tanlang\n3️⃣ Premium emoji ID sini yuboring\n\n"
        "Premium emoji ID ni maxsus emojini botga yuborish orqali ham olishingiz mumkin — bot ID ni o'zi aniqlaydi. "
        "Oddiy emoji (masalan 🔥) yuborsangiz, u tugma matni boshiga qo'shiladi.\n\n"
        "ℹ️ Telegram premium emojini faqat Fragment'da username sotib olgan botlarda yoki "
        "bot egasida Telegram Premium obunasi bo'lsa ko'rsatadi. Bot buni saqlash paytida avtomatik tekshiradi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("button_emoji_group:"))
async def button_emoji_group(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    await state.clear()
    group_key = call.data.split(":", 1)[1]
    group = next(((label, items) for key, label, items in BUTTON_EMOJI_GROUPS if key == group_key), None)
    if not group:
        return await call.answer("⚠️ Guruh topilmadi", show_alert=True)
    group_label, items = group
    saved = load_data().get("settings", {}).get("button_emojis", {})
    rows = []
    for key, label in items:
        mark = _button_emoji_mark(saved.get(key))
        shown = label if len(label) <= 30 else label[:29] + "…"
        rows.append([InlineKeyboardButton(text=f"{mark} {shown}", callback_data=f"button_emoji_pick:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="button_emoji_menu")])
    await call.message.answer(f"😀 <b>{group_label}</b>\nEmoji qo'shmoqchi bo'lgan tugmani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("button_emoji_pick:"))
async def button_emoji_pick(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    key = call.data.split(":", 1)[1]
    if key not in BUTTON_EMOJI_LABELS:
        return await call.answer("⚠️ Tugma topilmadi", show_alert=True)
    current = load_data().get("settings", {}).get("button_emojis", {}).get(key) or "yo'q"
    await state.update_data(button_emoji_key=key)
    await state.set_state(Form.button_emoji_value)
    rows = []
    if current != "yo'q":
        rows.append([InlineKeyboardButton(text="🗑 Emojini o'chirish", callback_data=f"button_emoji_del:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="button_emoji_menu")])
    await call.message.answer(
        f"😀 <b>{BUTTON_EMOJI_LABELS[key]}</b>\nHozirgi emoji: <code>{current}</code>\n\n"
        "Premium emoji ID sini yuboring (masalan <code>5368324170671202286</code>) "
        "yoki premium emojining o'zini yuboring.\n"
        "Oddiy emoji yuborsangiz, u tugma matni boshiga qo'shiladi.\n"
        "Bekor qilish: /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await call.answer()


@dp.message(Form.button_emoji_value)
async def button_emoji_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Faqat owner uchun.")
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await state.clear()
        return await message.answer("✅ Bekor qilindi.", reply_markup=owner_menu())
    ctx = await state.get_data()
    key = ctx.get("button_emoji_key")
    if key not in BUTTON_EMOJI_LABELS:
        await state.clear()
        return await message.answer("⚠️ Tugma topilmadi. Qaytadan urinib ko'ring.")
    custom_id = _extract_custom_emoji_id(message)
    value = custom_id or raw
    if not value:
        return await message.answer("⚠️ Premium emoji ID sini yoki emojining o'zini yuboring. Bekor qilish: /cancel")
    if custom_id or value.isdigit():
        if len(value) < 5 or not value.isdigit():
            return await message.answer("⚠️ Premium emoji ID faqat raqamlardan iborat bo'ladi. Qayta yuboring:")
        if not await _probe_custom_emoji(value, message.from_user.id):
            return await message.answer(
                "❌ Bu premium emoji bilan tugma yuborilmadi.\n"
                "Sabab: bot Fragment'da username sotib olmagan va bot egasida Telegram Premium yo'q, "
                "emoji ID noto'g'ri yoki aiogram versiyasi eski (3.31.0 kerak).\n"
                "Boshqa ID yuboring yoki /cancel bosing.",
            )
    elif len(value) > 8 or " " in value:
        return await message.answer("⚠️ Faqat premium emoji ID (raqam) yoki bitta oddiy emoji yuboring. Qayta yuboring:")
    data = load_data()
    data.setdefault("settings", {}).setdefault("button_emojis", {})[key] = value
    save_data(data)
    await state.clear()
    await message.answer(
        f"✅ <b>{BUTTON_EMOJI_LABELS[key]}</b> tugmasiga emoji saqlandi: <code>{value}</code>\n"
        "Menyular qayta ochilganda ko'rinadi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="😀 Yana emoji qo'shish", callback_data="button_emoji_menu")],
            [InlineKeyboardButton(text="👑 Owner paneli", callback_data="admin_home")],
        ])
    )


@dp.callback_query(F.data.startswith("button_emoji_del:"))
async def button_emoji_delete(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    key = call.data.split(":", 1)[1]
    if key not in BUTTON_EMOJI_LABELS:
        return await call.answer("⚠️ Tugma topilmadi", show_alert=True)
    data = load_data()
    data.setdefault("settings", {}).setdefault("button_emojis", {}).pop(key, None)
    save_data(data)
    await state.clear()
    await call.answer("🗑 Emoji o'chirildi")
    try:
        await call.message.edit_text(
            f"🗑 <b>{BUTTON_EMOJI_LABELS[key]}</b> tugmasidagi emoji o'chirildi.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Emoji menyusi", callback_data="button_emoji_menu")]])
        )
    except Exception:
        pass


@dp.callback_query(F.data == "button_emoji_reset")
async def button_emoji_reset(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    data = load_data()
    data.setdefault("settings", {}).pop("button_emojis", None)
    save_data(data)
    await button_emoji_menu(call, state)

@dp.callback_query(F.data == "button_text_menu")
async def button_text_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Faqat admin va owner uchun", show_alert=True)
    data = load_data()
    saved = data.get("settings", {}).get("button_texts", {})
    rows = []
    for key, default_label in EDITABLE_BUTTONS.items():
        label = saved.get(key, default_label)
        shown = label if len(label) <= 32 else label[:29] + "…"
        rows.append([InlineKeyboardButton(text=shown, callback_data=f"button_text_edit:{key}")])
    rows.append([InlineKeyboardButton(text="♻️ Nomlarni standartga qaytarish", callback_data="button_text_reset")])
    rows.append([InlineKeyboardButton(text="⬅️ Ko‘rinish sozlamalari", callback_data="appearance")])
    await call.message.answer(
        "✏️ <b>Tugma nomlarini o‘zgartirish</b>\nO‘zgartirmoqchi bo‘lgan tugmani tanlang. Matn va oddiy emoji yozishingiz mumkin.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("button_text_edit:"))
async def button_text_edit(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Faqat admin va owner uchun", show_alert=True)
    key = call.data.split(":", 1)[1]
    if key not in EDITABLE_BUTTONS:
        return await call.answer("❌ Tugma topilmadi", show_alert=True)
    await state.update_data(button_text_key=key)
    await state.set_state(Form.button_text_value)
    await call.message.answer(
        f"Tanlangan tugma: <b>{EDITABLE_BUTTONS[key]}</b>\nYangi nomini yuboring (1–64 belgi). /cancel — bekor qilish.\n\n"
        "Eslatma: premium emoji tugma nomi ichida emas, alohida ikonka sifatida qo'shiladi — "
        "buning uchun Owner panelidagi «😀 Tugmalarga emoji qo'shish» bo'limidan foydalaning."
    )
    await call.answer()

@dp.message(Form.button_text_value)
async def button_text_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    new_text = (message.text or "").strip()
    if not new_text:
        return await message.answer("⚠️ Matn bo‘sh bo‘lmasin. Yangi nom yuboring yoki /cancel bosing.")
    if len(new_text) > 64:
        return await message.answer("⚠️ Tugma nomi 64 belgidan oshmasin. Qisqaroq nom yuboring.")
    state_data = await state.get_data()
    key = state_data.get("button_text_key")
    if key not in EDITABLE_BUTTONS:
        await state.clear()
        return await message.answer("⚠️ Tugma topilmadi. Qaytadan urinib ko‘ring.")
    data = load_data()
    texts = data.setdefault("settings", {}).setdefault("button_texts", {})
    if new_text == EDITABLE_BUTTONS[key]:
        texts.pop(key, None)
    else:
        texts[key] = new_text
    save_data(data)
    await state.clear()
    await message.answer("✅ Tugma nomi saqlandi. Menyuni qayta ochganda yangi nom ko‘rinadi.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Yana nom o‘zgartirish", callback_data="button_text_menu")],
        [InlineKeyboardButton(text="🎨 Ko‘rinish sozlamalari", callback_data="appearance")],
    ]))

@dp.callback_query(F.data == "button_text_reset")
async def button_text_reset(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Faqat admin va owner uchun", show_alert=True)
    data = load_data()
    data.setdefault("settings", {}).pop("button_texts", None)
    save_data(data)
    await button_text_menu(call)
    await call.answer("✅ Tugma nomlari standartga qaytarildi")


@dp.callback_query(F.data == "broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    await state.set_state(Form.broadcast_message)
    await call.message.answer("📣 Barcha foydalanuvchilarga yuboriladigan xabarni matn, rasm yoki video sifatida yuboring. Bekor qilish: /cancel")
    await call.answer()


@dp.message(Form.broadcast_message)
async def broadcast_send(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("Faqat owner uchun.")
    if (message.text or "").strip() == "/cancel":
        await state.clear()
        return await message.answer("❎ Yuborish bekor qilindi.", reply_markup=owner_menu())
    data = load_data()
    recipients = list(data.get("users", {}).keys())
    sent = failed = 0
    await state.clear()
    for uid in recipients:
        try:
            await bot.copy_message(chat_id=int(uid), from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
        except Exception as exc:
            failed += 1
            logging.info("Broadcast %s ga yuborilmadi: %s", uid, exc)
        await asyncio.sleep(0.04)
    await message.answer(f"📣 Xabar yuborish tugadi.\n✅ Yetkazildi: {sent}\n❌ Yetkazilmadi: {failed}", reply_markup=owner_menu())


def _chat_status(value) -> str:
    return str(value).split(".")[-1].lower()


def is_mandatory_chat(chat, data=None) -> bool:
    data = data or load_data()
    aliases = set()
    chat_id = getattr(chat, "id", None)
    if chat_id is not None:
        aliases |= channel_aliases(str(chat_id))
    username = getattr(chat, "username", None)
    if username:
        aliases |= channel_aliases("@" + str(username))
    if not aliases:
        return False
    for entry in data.get("channels", []):
        chat_ref, invite_url, _raw = parse_channel_entry(entry)
        if channel_aliases(chat_ref, invite_url) & aliases:
            return True
    return False


@dp.chat_member()
async def member_left_mandatory_channel(event: ChatMemberUpdated):
    """Kanaldagi «foydalanuvchi chiqib ketdi» amali bo‘yicha faqat o‘shaning kinolarini o‘chiradi."""
    try:
        old_status = _chat_status(event.old_chat_member.status)
        new_status = _chat_status(event.new_chat_member.status)
        if old_status not in {"creator", "administrator", "member", "restricted"}:
            return
        if new_status not in {"left", "kicked"}:
            return
        data = load_data()
        if not is_mandatory_chat(event.chat, data):
            return
        uid = int(event.new_chat_member.user.id)
        if is_vip_user(uid, data):
            return
        deleted = await purge_user_media(uid)
        logging.info("Majburiy kanaldan chiqdi: user=%s chat=%s deleted=%s", uid, getattr(event.chat, "id", ""), deleted)
    except Exception:
        logging.exception("chat_member update xatosi")


@dp.chat_join_request()
async def remember_channel_join_request(event):
    """Maxfiy majburiy kanalga so'rov yuborgan foydalanuvchini qayd etadi.

    So'rov admin tomonidan hali tasdiqlanmagan bo'lsa ham, foydalanuvchi
    obuna bo'lgan deb hisoblanadi va qayta obunaga majburlanmaydi.
    """
    try:
        data = load_data()
        chat = event.chat
        channel_id = str(chat.id)
        username = getattr(chat, "username", None)
        invite_obj = getattr(event, "invite_link", None)
        invite_link = str(getattr(invite_obj, "invite_link", "") or "")
        event_aliases = channel_aliases(channel_id, invite_link)
        if username:
            event_aliases |= channel_aliases("@" + str(username))
        # Kanal ID, @username yoki taklif havolasi bo'yicha mos kelishini tekshiramiz.
        matched = any(
            channel_aliases(*parse_channel_entry(entry)[:2]) & event_aliases
            for entry in data.get("channels", [])
        )
        if not matched:
            return
        requests = data.setdefault("join_requests", {})
        user_key = str(event.from_user.id)
        user_channels = {str(x) for x in requests.get(user_key, [])}
        user_channels |= event_aliases
        requests[user_key] = sorted(user_channels)
        save_data(data)
        logging.info("Join request recorded: user=%s channel=%s", user_key, channel_id)
    except Exception:
        logging.exception("Join requestni saqlashda xatolik")


@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(call: CallbackQuery):
    data = load_data()
    if await enforce_subscription(call.from_user.id):
        await send_start(call.message, call.from_user.id)
        await call.answer("✅ Obuna tasdiqlandi")
    else:
        await call.answer("⚠️ Hamma kanallarga obuna bo'ling yoki 💎 VIP oling.", show_alert=True)


# ================= BUYURTMA VA SHIKOYAT =================

REQUEST_META = {
    "order": {
        "emoji": "🛒", "noun": "buyurtma", "plural": "Buyurtmalar",
        "store": "orders", "seq": "order_seq", "reply_title": "Buyurtmangizga javob",
        "prompt": "🛒 <b>Buyurtma berish</b>\n\nQaysi kino, serial yoki animeni qo'shishimizni xohlaysiz? Nomini va istaklaringizni yozing.\nBekor qilish: /cancel",
        "received": "✅ Buyurtmangiz qabul qilindi. Tez orada ko'rib chiqamiz.",
    },
    "complaint": {
        "emoji": "📝", "noun": "shikoyat", "plural": "Shikoyatlar",
        "store": "complaints", "seq": "complaint_seq", "reply_title": "Shikoyatingizga javob",
        "prompt": "📝 <b>Shikoyat</b>\n\nShikoyatingizni batafsil yozing (muammo, xatolik yoki taklif).\nBekor qilish: /cancel",
        "received": "✅ Shikoyatingiz qabul qilindi. Ko'rib chiqamiz.",
    },
}


def _request_recipients(data):
    ids = {OWNER_ID}
    ids.update(ADMIN_IDS)
    ids.update(x for x in data.get("admins", []) if isinstance(x, int))
    return {i for i in ids if i}


async def _request_start(call: CallbackQuery, state: FSMContext, kind: str):
    await state.clear()
    await state.update_data(request_kind=kind)
    await state.set_state(Form.request_text)
    await call.message.answer(REQUEST_META[kind]["prompt"])
    await call.answer()


@dp.callback_query(F.data == "order_new")
async def order_new_callback(call: CallbackQuery, state: FSMContext):
    await _request_start(call, state, "order")


@dp.callback_query(F.data == "complaint_new")
async def complaint_new_callback(call: CallbackQuery, state: FSMContext):
    await _request_start(call, state, "complaint")


@dp.message(Form.request_text)
async def request_text_save(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=main_menu(message.from_user.id))
    if not text:
        return await message.answer("✍️ Iltimos, matn yuboring. Bekor qilish: /cancel")
    if len(text) < 3:
        return await message.answer("✍️ Juda qisqa. Batafsilroq yozing:")
    ctx = await state.get_data()
    meta = REQUEST_META[ctx.get("request_kind", "order")]
    text = text[:1500]
    await state.update_data(request_text=text)
    await state.set_state(Form.request_confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yuborish", callback_data="req_send"),
         InlineKeyboardButton(text="❌ Bekor qilish", callback_data="req_cancel")],
    ])
    await message.answer(f"{meta['emoji']} <b>Tasdiqlang</b>\n\n{_esc(text)}\n\nYuborishni tasdiqlaysizmi?", reply_markup=kb)


@dp.message(Form.request_confirm)
async def request_confirm_wait(message: Message, state: FSMContext):
    if (message.text or "").strip() == "/cancel":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=main_menu(message.from_user.id))
    await message.answer("👆 «✅ Yuborish» yoki «❌ Bekor qilish» tugmasini bosing.")


@dp.callback_query(F.data == "req_cancel")
async def request_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❎ Bekor qilindi.")
    except Exception:
        pass
    await call.answer()


@dp.callback_query(F.data == "req_send")
async def request_send(call: CallbackQuery, state: FSMContext):
    ctx = await state.get_data()
    kind = ctx.get("request_kind", "order")
    text = (ctx.get("request_text") or "").strip()
    meta = REQUEST_META[kind]
    if not text:
        await state.clear()
        return await call.answer("Matn topilmadi. Qaytadan boshlang.", show_alert=True)
    data = load_data()
    data[meta["seq"]] = int(data.get(meta["seq"], 0) or 0) + 1
    rid = data[meta["seq"]]
    data.setdefault(meta["store"], []).append({
        "id": rid, "uid": call.from_user.id, "name": call.from_user.full_name,
        "username": call.from_user.username or "", "text": text,
        "at": now_ts(), "status": "new",
    })
    save_data(data)
    await state.clear()
    try:
        await call.message.edit_text(f"{meta['received']}\n\n🔖 Raqam: <b>#{rid}</b>")
    except Exception:
        await call.message.answer(f"{meta['received']} (#{rid})")
    await call.answer("✅ Yuborildi")
    username = f"@{call.from_user.username}" if call.from_user.username else "username yo'q"
    note = (f"{meta['emoji']} <b>Yangi {meta['noun']}</b> #{rid}\n"
            f"👤 {_esc(call.from_user.full_name)}\n🔗 {username}\n"
            f"🆔 <code>{call.from_user.id}</code>\n🕒 {fmt_date(now_ts())}\n\n💬 {_esc(text)}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Javob berish", callback_data=f"reqreply:{kind}:{rid}")],
    ])
    for admin_id in _request_recipients(data):
        try:
            await bot.send_message(admin_id, note, reply_markup=kb)
        except Exception as exc:
            logging.info("Murojaat xabari %s ga yuborilmadi: %s", admin_id, exc)


def _requests_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Owner paneli", callback_data="admin_home")],
    ])


async def _show_requests(call: CallbackQuery, kind: str):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    meta = REQUEST_META[kind]
    data = load_data()
    items = list(data.get(meta["store"], []))
    if not items:
        text = f"{meta['emoji']} <b>{meta['plural']}</b>\n\nHozircha murojaatlar yo'q."
        try:
            await call.message.edit_text(text, reply_markup=_requests_back_kb())
        except Exception:
            await call.message.answer(text, reply_markup=_requests_back_kb())
        return await call.answer()
    new_count = sum(1 for r in items if r.get("status") == "new")
    blocks, rows = [], []
    for r in reversed(items[-12:]):
        status = {"new": "🆕 yangi", "answered": "✍️ javob berilgan", "done": "✅ bajarilgan"}.get(r.get("status"), str(r.get("status")))
        uname = f"@{r['username']}" if r.get("username") else (r.get("name") or "?")
        blocks.append(f"#{r['id']} • {status}\n👤 {_esc(str(uname))} • 🆔 <code>{r['uid']}</code>\n💬 {_esc(str(r.get('text', '')))[:220]}")
        row = [InlineKeyboardButton(text=f"✍️ #{r['id']}", callback_data=f"reqreply:{kind}:{r['id']}")]
        if r.get("status") != "done":
            row.append(InlineKeyboardButton(text=f"✅ #{r['id']}", callback_data=f"reqdone:{kind}:{r['id']}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🗑 Tozalash", callback_data=f"reqclear:{kind}")])
    rows.append([InlineKeyboardButton(text="⬅️ Owner paneli", callback_data="admin_home")])
    text = f"{meta['emoji']} <b>{meta['plural']}</b> (yangi: {new_count})\n\n" + "\n\n".join(blocks)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "owner_orders")
async def owner_orders_callback(call: CallbackQuery):
    await _show_requests(call, "order")


@dp.callback_query(F.data == "owner_complaints")
async def owner_complaints_callback(call: CallbackQuery):
    await _show_requests(call, "complaint")


@dp.callback_query(F.data.startswith("reqreply:"))
async def request_reply_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    try:
        _, kind, rid = call.data.split(":")
        rid = int(rid)
    except ValueError:
        return await call.answer("⚠️ Xato tugma", show_alert=True)
    if kind not in REQUEST_META:
        return await call.answer("⚠️ Noma'lum tur", show_alert=True)
    await state.update_data(reply_kind=kind, reply_id=rid)
    await state.set_state(Form.request_reply)
    await call.message.answer(f"✍️ #{rid} {REQUEST_META[kind]['noun']}ga javob matnini yozing.\nBekor qilish: /cancel")
    await call.answer()


@dp.message(Form.request_reply)
async def request_reply_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Faqat owner uchun.")
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=owner_menu())
    if not text:
        return await message.answer("✍️ Javob matnini yuboring. Bekor qilish: /cancel")
    ctx = await state.get_data()
    kind = ctx.get("reply_kind")
    rid = int(ctx.get("reply_id", 0))
    meta = REQUEST_META.get(kind)
    data = load_data()
    record = next((r for r in data.get(meta["store"], []) if r.get("id") == rid), None) if meta else None
    if not record:
        await state.clear()
        return await message.answer("❌ Murojaat topilmadi.", reply_markup=owner_menu())
    uid = int(record["uid"])
    reply_text = f"{meta['emoji']} <b>{meta['reply_title']}</b> (#{rid})\n\n{_esc(text[:1500])}"
    delivered = True
    try:
        await bot.send_message(uid, reply_text, reply_markup=main_menu(uid))
    except Exception:
        delivered = False
    record["status"] = "answered"
    record["reply"] = text[:1500]
    record["replied_at"] = now_ts()
    save_data(data)
    await state.clear()
    tail = "✅ Javob yuborildi." if delivered else "⚠️ Javob saqlandi, lekin foydalanuvchiga yetkazilmadi (botni bloklagan bo'lishi mumkin)."
    await message.answer(tail, reply_markup=owner_menu())


@dp.callback_query(F.data.startswith("reqdone:"))
async def request_done(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    try:
        _, kind, rid = call.data.split(":")
        rid = int(rid)
    except ValueError:
        return await call.answer("⚠️ Xato tugma", show_alert=True)
    meta = REQUEST_META.get(kind)
    if not meta:
        return await call.answer("⚠️ Noma'lum tur", show_alert=True)
    data = load_data()
    record = next((r for r in data.get(meta["store"], []) if r.get("id") == rid), None)
    if record:
        record["status"] = "done"
        save_data(data)
    await call.answer(f"✅ #{rid} bajarildi")
    await _show_requests(call, kind)


@dp.callback_query(F.data.startswith("reqclear:"))
async def request_clear(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Faqat owner uchun", show_alert=True)
    kind = call.data.split(":")[1]
    meta = REQUEST_META.get(kind)
    if not meta:
        return await call.answer("⚠️ Noma'lum tur", show_alert=True)
    data = load_data()
    data[meta["store"]] = [r for r in data.get(meta["store"], []) if r.get("status") == "new"]
    save_data(data)
    await call.answer("🗑 Tozalandi")
    await _show_requests(call, kind)


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current:
        text = "✅ Joriy amal bekor qilindi."
        if is_admin(message.from_user.id):
            return await message.answer(text, reply_markup=admin_menu(message.from_user.id))
        return await message.answer(text, reply_markup=main_menu(message.from_user.id))
    await message.answer("ℹ️ Bekor qilinadigan amal yo'q.")


@dp.message(F.text.in_({"▶️ Davom ettirish", "⭐ Yoqtirganlarim"}))
async def main_reply_keyboard_routes(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    await state.clear()
    data = load_data()
    user = data.get("users", {}).get(str(message.from_user.id), {})
    if text == "▶️ Davom ettirish":
        last = user.get("last_watched") or {}
        code = str(last.get("code", ""))
        item = data.get("medias", {}).get(code)
        if item:
            await send_media_card(message.chat.id, code, item)
        else:
            await message.answer("ℹ️ Hali tomosha qilgan narsangiz yo‘q.")
    elif text == "⭐ Yoqtirganlarim":
        codes = user.get("favorites", [])
        rows = []
        for code in codes:
            item = data.get("medias", {}).get(str(code))
            if item:
                rows.append([InlineKeyboardButton(text=f"🎬 {item['name']} · {code}", callback_data=f"show:{code}")])
        rows.append([InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")])
        await message.answer("⭐ <b>Yoqtirganlarim</b>" if len(rows) > 1 else "⭐ Hozircha saqlangan narsa yo‘q.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message()
async def fallback(message: Message):
    if message.text and message.text.isdigit():
        if not await require_subscription_message(message):
            return
        data = load_data()
        code = message.text.strip()
        if code in data["medias"]:
            media = data["medias"][code]
            await send_media_card(message.chat.id, code, media)
        else:
            await message.answer("❌ Kod topilmadi. Qayta tekshiring.")
    elif message.text and message.text.strip() and not message.text.startswith("/"):
        query = message.text.strip().casefold()
        if query in {"🔎 kino va serial izlash", "kino va serial izlash", "🔎 kino izlash", "kino izlash"}:
            await message.answer("🔎 Kino yoki serial nomini yozing. Qismlar baza kanalidan avtomatik topiladi.", reply_markup=main_menu(message.from_user.id))
            return
        if not await require_subscription_message(message):
            return
        data = load_data()
        matches = [(code, item) for code, item in data.get("medias", {}).items() if query in item.get("name", "").casefold() and media_available_for_user(message.from_user.id, item, data)]
        catalog_matches = [(key, item) for key, item in data.get("channel_catalog", {}).items() if query in item.get("name", "").casefold() or query in item.get("search_text", "").casefold()]
        if matches or catalog_matches:
            rows = [[InlineKeyboardButton(text=f"{'🎞' if item.get('media_type') == 'movie' else '🍥'} {item.get('name', 'Nomsiz')} · {code}", callback_data=f"show:{code}")] for code, item in matches[:30]]
            rows += [[InlineKeyboardButton(text=f"🎬 {item.get('name', 'Nomsiz kino')} · baza kanal", callback_data=f"catalog_show:{key}")] for key, item in catalog_matches[:30]]
            rows.append([InlineKeyboardButton(text="🔎 Yana qidirish", callback_data="search_name")])
            await message.answer("🔎 <b>Topilgan kino va seriallar</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        else:
            await message.answer("❌ Bu nom bo'yicha hech narsa topilmadi. Nomning boshqa qismini yozib ko'ring.", reply_markup=main_menu(message.from_user.id))
    elif message.text:
        await message.answer("📋 Menyudan foydalaning.", reply_markup=main_menu(message.from_user.id))


async def health_check(request: web.Request) -> web.Response:
    """Render health check uchun oddiy HTTP endpoint."""
    return web.Response(text="KinoSerial bot is running", status=200)


async def start_web_server():
    # Render PORT qiymatini o'zi beradi; odatda 10000 bo'ladi.
    port = int(os.getenv("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("Health server 0.0.0.0:%s portida ishga tushdi", port)
    return runner




async def vip_expiry_monitor():
    """VIP muddati tugashini avtomatik to'xtatadi va 24 soat oldin bir marta ogohlantiradi."""
    while True:
        try:
            data=load_data(); users=data.setdefault("users",{}); changed=False; now=now_ts()
            for uid, user in users.items():
                until=int(user.get("vip_until",0) or 0)
                if until <= 0:
                    continue
                if until <= now:
                    user["vip_until"]=0
                    user.pop("vip_warned_until",None)
                    changed=True
                    purged = await purge_vip_media(int(uid), user)
                    text = "⏰ VIP obunangiz muddati tugadi. VIP imkoniyatlari avtomatik o'chirildi."
                    if purged:
                        text += f"\n\n🗑 VIP orqali ochilgan {purged} ta kino chatdan o'chirildi."
                    try: await bot.send_message(int(uid), text)
                    except Exception: pass
                elif until-now <= 86400 and int(user.get("vip_warned_until",0) or 0) != until:
                    try:
                        await bot.send_message(int(uid), f"⚠️ VIP obunangiz {fmt_date(until)} da tugaydi — 1 kundan kam vaqt qoldi. Balansingizni to'ldirib, VIPni uzaytirishingiz mumkin.")
                        user["vip_warned_until"]=until; changed=True
                    except Exception:
                        # Xabar yuborilmasa keyingi aylanishda qayta urinadi.
                        pass
            if changed: save_data(data)
        except Exception:
            logging.exception("VIP expiry monitor error")
        await asyncio.sleep(60)

async def main():
    if not OWNER_ID:
        raise RuntimeError(
            "OWNER_ID topilmadi yoki noto'g'ri. Render Environment bo'limida "
            "OWNER_ID ga Telegram raqamli user ID ni yozing (masalan, 123456789), "
            "yoki ADMIN_ID ga kamida bitta raqamli ID kiriting. @username yozmang."
        )
    data = load_data()
    if OWNER_ID not in data.get("admins", []):
        data.setdefault("admins", []).append(OWNER_ID)
        save_data(data)

    # ADMIN_ID va OWNER_ID ro'yxatini doimiy adminlar ro'yxatiga qo'shamiz.
    for admin_id in [OWNER_ID] + ADMIN_IDS:
        if admin_id and admin_id not in data.setdefault("admins", []):
            data["admins"].append(admin_id)
    save_data(data)
    # Saqlangan premium emoji tugmalari ishlashini ishga tushishda bir marta tekshiramiz.
    await verify_custom_emoji_support()
    runner = await start_web_server()
    vip_monitor_task = asyncio.create_task(vip_expiry_monitor())
    try:
        from aiogram.types import BotCommandScopeChat
        user_commands = [BotCommand(command="start", description="Botni boshlash"), BotCommand(command="ref", description="Referal havola va qoidalar")]
        await bot.set_my_commands(user_commands)
        owner_commands = user_commands + [BotCommand(command="owner", description="Owner paneli"), BotCommand(command="admin", description="Admin paneli"), BotCommand(command="backup", description="Zaxira olish")]
        await bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=OWNER_ID))
        for admin_id in set(ADMIN_IDS + [int(x) for x in data.get("admins", []) if str(x).isdigit()]):
            if admin_id and admin_id != OWNER_ID:
                await bot.set_my_commands(user_commands + [BotCommand(command="admin", description="Admin paneli")], scope=BotCommandScopeChat(chat_id=admin_id))
        await dp.start_polling(bot)
    finally:
        vip_monitor_task.cancel()
        try: await vip_monitor_task
        except asyncio.CancelledError: pass
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
