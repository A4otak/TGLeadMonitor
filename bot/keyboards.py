"""Генераторы inline-клавиатур.

callback_data — короткие строки вида ``c:v:12:1`` (строго <= 64 байт).
Каждая кнопка «Назад» хранит точный адрес родительского экрана и номер страницы.
"""
from __future__ import annotations

from typing import Callable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CHATS_PER_PAGE = 6
CATS_PER_PAGE = 6
KW_PER_PAGE = 8
SW_PER_PAGE = 8
BL_PER_PAGE = 8

STATUS_ICON = {"active": "🟢", "paused": "⏸", "queued": "⏳", "error": "❌"}

Rows = list[list[InlineKeyboardButton]]


def _btn(text: str, data: str) -> InlineKeyboardButton:
    if len(data.encode("utf-8")) > 64:
        raise ValueError(f"callback_data длиннее 64 байт: {data!r}")
    return InlineKeyboardButton(text=text, callback_data=data)


def _url(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def _kb(rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cut(text: str, n: int = 40) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _pager(page: int, pages: int, fmt: Callable[[int], str]) -> list[InlineKeyboardButton] | None:
    if pages <= 1:
        return None
    prev_p = (page - 2) % pages + 1
    next_p = page % pages + 1
    return [_btn("◀️", fmt(prev_p)), _btn(f"{page}/{pages}", "noop"), _btn("▶️", fmt(next_p))]


def _back(text: str, data: str) -> list[InlineKeyboardButton]:
    return [_btn(text, data)]


# ---------------- Экран 0: главное меню ----------------
def main_kb(monitoring: bool, chats_n: int, keywords_n: int) -> InlineKeyboardMarkup:
    toggle = "⏸ Поставить на паузу" if monitoring else "▶️ Запустить мониторинг"
    return _kb([
        [_btn(toggle, "m:toggle")],
        [_btn(f"💬 Чаты и каналы ({chats_n})", "c:l:1")],
        [_btn(f"🔑 Ключевые слова ({keywords_n})", "k:l:1")],
        [_btn("🚫 Стоп-слова и фильтры", "f:main")],
        [_btn("📊 Статистика и экспорт", "s:main")],
        [_btn("⚙️ Настройки парсера", "cfg:main")],
    ])


# ---------------- Экран 1: чаты ----------------
def chat_title(chat: dict) -> str:
    if chat.get("title"):
        return chat["title"]
    if chat.get("username"):
        return "@" + chat["username"]
    return chat.get("key", "?")


def chats_kb(chats: list[dict], page: int, pages: int) -> InlineKeyboardMarkup:
    rows: Rows = []
    for c in chats:
        icon = STATUS_ICON.get(c["status"], "❔")
        label = f"{icon} {_cut(chat_title(c), 30)} ({c.get('leads', 0)} лидов)"
        rows.append([_btn(label, f"c:v:{c['id']}:{page}")])
    pager = _pager(page, pages, lambda p: f"c:l:{p}")
    if pager:
        rows.append(pager)
    rows.append([_btn("➕ Добавить чат / список", f"c:add:{page}")])
    rows.append([_btn("🔄 Импорт из аккаунта", f"c:sync:{page}")])
    rows.append(_back("⬅️ В главное меню", "m:main"))
    return _kb(rows)


def chat_card_kb(chat: dict, page: int) -> InlineKeyboardMarkup:
    cid = chat["id"]
    rows: Rows = []
    # чат без публичной ссылки с невыясненным доступом (например, из бэкапа) — сначала проверка доступа
    needs_check = chat.get("key", "").startswith("id:") and bool(chat.get("error"))
    if needs_check:
        rows.append([_btn("🔍 Проверить доступ", f"c:tg:{cid}:{page}")])
    elif chat["status"] == "active":
        rows.append([_btn("⏸ Приостановить", f"c:tg:{cid}:{page}")])
    elif chat["status"] == "paused":
        rows.append([_btn("▶️ Включить", f"c:tg:{cid}:{page}")])
    elif chat["status"] == "error":
        rows.append([_btn("🔁 Повторить вступление", f"c:tg:{cid}:{page}")])
    rows.append([_btn("🗑 Удалить из базы", f"c:rm:{cid}:{page}")])
    rows.append([_btn("🚪 Выйти из чата и удалить", f"c:lv_ask:{cid}:{page}")])
    rows.append(_back("⬅️ Назад к чатам", f"c:l:{page}"))
    return _kb(rows)


def chat_leave_confirm_kb(chat_id: int, page: int) -> InlineKeyboardMarkup:
    return _kb([[_btn("✅ Да, выйти", f"c:lv:{chat_id}:{page}"),
                 _btn("❌ Отмена", f"c:v:{chat_id}:{page}")]])


def chat_add_kb(page: int) -> InlineKeyboardMarkup:
    return _kb([_back("⬅️ Назад к чатам", f"c:l:{page}")])


# ---------------- Экран 2: категории ----------------
def cats_kb(cats: list[dict], page: int, pages: int) -> InlineKeyboardMarkup:
    rows: Rows = []
    for c in cats:
        rows.append([_btn(f"📁 {_cut(c['name'], 34)} ({c['n']})", f"k:v:{c['id']}:{page}")])
    pager = _pager(page, pages, lambda p: f"k:l:{p}")
    if pager:
        rows.append(pager)
    rows.append([_btn("➕ Создать категорию", f"k:add:{page}")])
    rows.append(_back("⬅️ В главное меню", "m:main"))
    return _kb(rows)


def cat_add_kb(page: int) -> InlineKeyboardMarkup:
    return _kb([_back("⬅️ Назад к категориям", f"k:l:{page}")])


def cat_view_kb(cat: dict, page: int) -> InlineKeyboardMarkup:
    cid = cat["id"]
    mode = "По вхождению" if cat["match_mode"] == "sub" else "Точное слово"
    return _kb([
        [_btn("➕ Добавить слова", f"kw:add:{cid}:{page}")],
        [_btn("➖ Удалить слова", f"kw:del_l:{cid}:{page}:1")],
        [_btn(f"🎯 Режим: {mode}", f"k:mode:{cid}:{page}")],
        [_btn("🗑 Удалить категорию", f"k:del_ask:{cid}:{page}")],
        _back("⬅️ Назад к категориям", f"k:l:{page}"),
    ])


def cat_del_confirm_kb(cat_id: int, page: int) -> InlineKeyboardMarkup:
    return _kb([[_btn("✅ Удалить", f"k:del:{cat_id}:{page}"),
                 _btn("❌ Отмена", f"k:v:{cat_id}:{page}")]])


def kw_add_kb(cat_id: int, page: int) -> InlineKeyboardMarkup:
    return _kb([_back("⬅️ Назад в категорию", f"k:v:{cat_id}:{page}")])


def kw_del_kb(words: list[dict], cat_id: int, page: int, kw_page: int, kw_pages: int) -> InlineKeyboardMarkup:
    rows: Rows = []
    for w in words:
        rows.append([_btn(f"❌ {_cut(w['word'], 40)}", f"kw:rm:{w['id']}:{cat_id}:{page}:{kw_page}")])
    pager = _pager(kw_page, kw_pages, lambda p: f"kw:del_l:{cat_id}:{page}:{p}")
    if pager:
        rows.append(pager)
    rows.append(_back("⬅️ Назад в категорию", f"k:v:{cat_id}:{page}"))
    return _kb(rows)


# ---------------- Экран 3: стоп-слова и фильтры ----------------
def filters_kb(settings: dict, sw_n: int, bl_n: int) -> InlineKeyboardMarkup:
    hours = int(settings["dedup_hours"])
    dedup = "Выкл" if hours == 0 else f"{hours} ч."
    bots = "Вкл" if settings["ignore_bots"] == "1" else "Выкл"
    return _kb([
        [_btn(f"🚫 Стоп-слова ({sw_n})", "sw:l:1")],
        [_btn(f"⛔ Чёрный список авторов ({bl_n})", "bl:l:1")],
        [_btn(f"🔁 Антидубли: {dedup}", "f:dedup")],
        [_btn(f"📏 Длина текста: {settings['min_len']}–{settings['max_len']}", "f:len")],
        [_btn(f"🤖 Игнор ботов и каналов: {bots}", "f:bots")],
        _back("⬅️ В главное меню", "m:main"),
    ])


def filter_len_kb() -> InlineKeyboardMarkup:
    return _kb([_back("⬅️ Назад в фильтры", "f:main")])


def sw_list_kb(words: list[dict], page: int, pages: int) -> InlineKeyboardMarkup:
    rows: Rows = []
    for w in words:
        rows.append([_btn(f"❌ {_cut(w['word'], 40)}", f"sw:rm:{w['id']}:{page}")])
    pager = _pager(page, pages, lambda p: f"sw:l:{p}")
    if pager:
        rows.append(pager)
    rows.append([_btn("➕ Добавить стоп-слова", f"sw:add:{page}")])
    rows.append(_back("⬅️ Назад в фильтры", "f:main"))
    return _kb(rows)


def sw_add_kb(page: int) -> InlineKeyboardMarkup:
    return _kb([_back("⬅️ Назад к стоп-словам", f"sw:l:{page}")])


def bl_list_kb(users: list[dict], page: int, pages: int) -> InlineKeyboardMarkup:
    rows: Rows = []
    for u in users:
        rows.append([_btn(f"❌ ID {u['user_id']}", f"bl:rm:{u['user_id']}:{page}")])
    pager = _pager(page, pages, lambda p: f"bl:l:{p}")
    if pager:
        rows.append(pager)
    rows.append([_btn("➕ Добавить ID автора", f"bl:add:{page}")])
    rows.append(_back("⬅️ Назад в фильтры", "f:main"))
    return _kb(rows)


def bl_add_kb(page: int) -> InlineKeyboardMarkup:
    return _kb([_back("⬅️ Назад к чёрному списку", f"bl:l:{page}")])


# ---------------- Экран 4: статистика ----------------
def stats_kb() -> InlineKeyboardMarkup:
    return _kb([
        [_btn("📥 Экспорт в CSV (Excel)", "s:exp:csv")],
        [_btn("📄 Экспорт в TXT", "s:exp:txt")],
        [_btn("🧹 Очистить историю лидов", "s:clr_ask")],
        _back("⬅️ В главное меню", "m:main"),
    ])


def stats_clear_confirm_kb() -> InlineKeyboardMarkup:
    return _kb([[_btn("✅ Очистить", "s:clr"), _btn("❌ Отмена", "s:main")]])


# ---------------- Экран 5: настройки ----------------
def cfg_kb(settings: dict) -> InlineKeyboardMarkup:
    sound = "Вкл" if settings["sound"] == "1" else "Выкл"
    return _kb([
        [_btn(f"⏱ Задержка вступления: {settings['join_delay']} сек.", "cfg:delay")],
        [_btn(f"🔔 Звук уведомлений: {sound}", "cfg:sound")],
        [_btn("💾 Бэкап настроек (JSON)", "cfg:bkp")],
        [_btn("📥 Восстановить из JSON", "cfg:rst")],
        _back("⬅️ В главное меню", "m:main"),
    ])


def cfg_back_kb() -> InlineKeyboardMarkup:
    return _kb([_back("⬅️ Назад в настройки", "cfg:main")])


# ---------------- Карточка лида ----------------
def lead_kb(link: str | None, author_username: str | None, author_id: int | None,
            with_ban: bool = True, with_write: bool = True) -> InlineKeyboardMarkup:
    rows: Rows = []
    if link:
        rows.append([_url("🔗 Перейти к сообщению", link)])
    if with_write:
        if author_username:
            rows.append([_url("✉️ Написать автору", f"https://t.me/{author_username}")])
        elif author_id:
            rows.append([_url("✉️ Написать автору", f"tg://user?id={author_id}")])
    last: list[InlineKeyboardButton] = []
    if with_ban and author_id:
        last.append(_btn("🚷 ЧС автора", f"ld:ban_u:{author_id}"))
    last.append(_btn("🗑 Скрыть", "ld:hide"))
    rows.append(last)
    return _kb(rows)
