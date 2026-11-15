"""Обработчики всех экранов бота управления, FSM-состояний и кнопок под карточками лидов."""
from __future__ import annotations

import html
import io
import logging
import re
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from bot import keyboards as kb
from database.db import DEDUP_CHOICES, DELAY_CHOICES, Database
from userbot.monitor import Monitor, parse_chat_refs
from utils import exporter

log = logging.getLogger("tgleadmonitor.bot")
router = Router()
router.message.filter(F.chat.type == "private")

e = html.escape

STATUS_TEXT = {
    "active": "🟢 Активен",
    "paused": "⏸ На паузе",
    "queued": "⏳ В очереди на вступление",
    "error": "❌ Ошибка доступа",
}


# ============================================================ FSM-состояния
class AddChat(StatesGroup):
    waiting_input = State()


class AddCategory(StatesGroup):
    waiting_name = State()


class AddKeyword(StatesGroup):
    waiting_words = State()


class AddStopword(StatesGroup):
    waiting_words = State()


class AddBlacklist(StatesGroup):
    waiting_ids = State()


class FilterLen(StatesGroup):
    waiting_range = State()


class RestoreCfg(StatesGroup):
    waiting_file = State()


# ============================================================ Middleware
class AdminMiddleware(BaseMiddleware):
    """Пропускает только ADMIN_ID, всех остальных молча игнорирует."""

    def __init__(self, admin_id: int):
        self.admin_id = admin_id

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.id != self.admin_id:
            return None
        return await handler(event, data)


class ClearStateMiddleware(BaseMiddleware):
    """Любое нажатие кнопки меню сбрасывает FSM (кроме карточек лидов и noop).

    Благодаря этому каждая кнопка «Назад» гарантированно делает state.clear().
    """

    async def __call__(self, handler, event, data):
        payload = getattr(event, "data", "") or ""
        if not payload.startswith(("ld:", "noop")):
            state = data.get("state")
            if state is not None:
                await state.clear()
        return await handler(event, data)


router.callback_query.middleware(ClearStateMiddleware())


# ============================================================ Утилиты
def pages_of(total: int, per: int) -> int:
    return max(1, -(-total // per))


def clamp(page: int, pages: int) -> int:
    return min(max(page, 1), pages)


def args(call: CallbackQuery) -> list[int]:
    """Числовые аргументы callback_data после первых двух сегментов: 'c:v:12:1' -> [12, 1]."""
    return [int(x) for x in call.data.split(":")[2:]]


def split_words(text: str, limit: int = 100) -> list[str]:
    """Слова/фразы через запятую или с новой строки."""
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,\n]+", text):
        word = " ".join(part.split()).lower()[:limit]
        if word and word not in seen:
            seen.add(word)
            out.append(word)
    return out


async def edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def edit_menu(bot: Bot, data: dict, text: str, markup: InlineKeyboardMarkup) -> None:
    """Редактирует «меню-сообщение», сохранённое в FSM (после ввода текста пользователем)."""
    try:
        await bot.edit_message_text(text, chat_id=data["cid"], message_id=data["mid"], reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        await bot.send_message(data["cid"], text, reply_markup=markup)


async def drop(message: Message) -> None:
    """Удаляет сообщение пользователя, чтобы не засорять диалог."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def begin_input(call: CallbackQuery, state: FSMContext, new_state: State, text: str,
                      markup: InlineKeyboardMarkup, **extra) -> None:
    await state.set_state(new_state)
    await state.update_data(mid=call.message.message_id, cid=call.message.chat.id, **extra)
    await edit(call, text, markup)
    await call.answer()


# ============================================================ Рендеры экранов
async def render_main(db: Database, monitor: Monitor) -> tuple[str, InlineKeyboardMarkup]:
    s = await db.all_settings()
    on = s["monitoring"] == "1"
    chats_total = await db.count_chats()
    chats_active = await db.count_chats("active")
    cats = await db.count_categories()
    kws = await db.count_all_keywords()
    today = await db.leads_since(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
    total = await db.leads_total()
    text = (
        "<b>📡 TGLeadMonitor</b>\n\n"
        f"Статус: {'🟢 Активен' if on else '⏸ На паузе'}\n"
        f"Аккаунт юзербота: {e(monitor.me_name)}\n"
        f"Активных чатов: {chats_active} из {chats_total}\n"
        f"Категорий: {cats} · ключей: {kws}\n"
        f"Лидов сегодня: {today}\n"
        f"Лидов за всё время: {total}"
    )
    return text, kb.main_kb(on, chats_total, kws)


async def render_chats(db: Database, page: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_chats()
    pages = pages_of(total, kb.CHATS_PER_PAGE)
    page = clamp(page, pages)
    rows = await db.list_chats((page - 1) * kb.CHATS_PER_PAGE, kb.CHATS_PER_PAGE)
    text = "<b>💬 Чаты и каналы</b>\n"
    if note:
        text += f"\n{note}\n"
    text += "\n🟢 активен · ⏸ пауза · ⏳ в очереди · ❌ ошибка доступа\n"
    if not rows:
        text += "\nСписок пуст. Добавьте чаты вручную или импортируйте из аккаунта."
    return text, kb.chats_kb(rows, page, pages)


async def render_chat(db: Database, chat_id: int, page: int, note: str = "") -> tuple[str, InlineKeyboardMarkup] | None:
    chat = await db.get_chat(chat_id)
    if not chat:
        return None
    ref = f"@{chat['username']}" if chat["username"] else (f"https://t.me/{chat['key']}" if chat["key"].startswith("+") else "—")
    text = (
        f"<b>💬 {e(kb.chat_title(chat))}</b>\n"
        + (f"\n{note}\n" if note else "")
        + f"\nСсылка: {e(ref)}\n"
        f"ID: {chat['tg_id'] if chat['tg_id'] is not None else '—'}\n"
        f"Статус: {STATUS_TEXT.get(chat['status'], chat['status'])}\n"
        f"Добавлен: {e(chat['added_at'])}\n"
        f"Поймано лидов: {chat['leads']}"
    )
    if chat["error"]:
        text += f"\n\n⚠️ Последняя ошибка: {e(chat['error'])}"
    return text, kb.chat_card_kb(chat, page)


async def render_cats(db: Database, page: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_categories()
    pages = pages_of(total, kb.CATS_PER_PAGE)
    page = clamp(page, pages)
    rows = await db.list_categories((page - 1) * kb.CATS_PER_PAGE, kb.CATS_PER_PAGE)
    text = "<b>🔑 Ключевые слова</b>\n"
    if note:
        text += f"\n{note}\n"
    text += "\nКатегории — тематические папки с ключевыми фразами. Сообщение попадает в лиды, если в нём найден ключ из любой категории."
    if not rows:
        text += "\n\nКатегорий пока нет."
    return text, kb.cats_kb(rows, page, pages)


async def render_cat(db: Database, cat_id: int, page: int, note: str = "") -> tuple[str, InlineKeyboardMarkup] | None:
    cat = await db.get_category(cat_id)
    if not cat:
        return None
    if cat["match_mode"] == "sub":
        mode = "По вхождению (найдёт «парсеры», «парсера» по корню «парсер»)"
    else:
        mode = "Точное слово (строго по границам слова)"
    words = await db.list_keywords(cat_id)
    text = f"<b>📁 {e(cat['name'])}</b>\n"
    if note:
        text += f"\n{note}\n"
    text += f"\nРежим поиска: <b>{e(mode)}</b>\nФраз: {len(words)}\n\n"
    shown = 0
    for i, w in enumerate(words, 1):
        line = f"{i}. {e(w['word'])}\n"
        if len(text) + len(line) > 3600:
            text += f"…и ещё {len(words) - shown}"
            break
        text += line
        shown += 1
    if not words:
        text += "Пока пусто — добавьте слова."
    return text, kb.cat_view_kb(cat, page)


async def render_kw_del(db: Database, cat_id: int, page: int, kw_page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    cat = await db.get_category(cat_id)
    if not cat:
        return None
    total = await db.count_keywords(cat_id)
    pages = pages_of(total, kb.KW_PER_PAGE)
    kw_page = clamp(kw_page, pages)
    words = await db.list_keywords(cat_id, (kw_page - 1) * kb.KW_PER_PAGE, kb.KW_PER_PAGE)
    text = f"<b>➖ Удаление слов — {e(cat['name'])}</b>\n\nНажмите на слово, чтобы удалить его."
    if not words:
        text += "\n\nСлов в категории не осталось."
    return text, kb.kw_del_kb(words, cat_id, page, kw_page, pages)


async def render_filters(db: Database, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    s = await db.all_settings()
    sw_n = await db.count_stopwords()
    bl_n = await db.count_blacklist()
    hours = int(s["dedup_hours"])
    text = (
        "<b>🚫 Стоп-слова и фильтры</b>\n"
        + (f"\n{note}\n" if note else "")
        + f"\nСтоп-слов: {sw_n}\n"
        f"Чёрный список авторов: {bl_n}\n"
        f"Антидубли: {'выключены' if hours == 0 else f'{hours} ч.'}\n"
        f"Длина сообщения: {s['min_len']} – {s['max_len']} симв.\n"
        f"Игнор ботов и пересланных постов из каналов: {'вкл' if s['ignore_bots'] == '1' else 'выкл'}"
    )
    return text, kb.filters_kb(s, sw_n, bl_n)


async def render_sw(db: Database, page: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_stopwords()
    pages = pages_of(total, kb.SW_PER_PAGE)
    page = clamp(page, pages)
    words = await db.list_stopwords((page - 1) * kb.SW_PER_PAGE, kb.SW_PER_PAGE)
    text = ("<b>🚫 Стоп-слова</b>\n" + (f"\n{note}\n" if note else "")
            + "\nСообщения, содержащие любое из этих слов, игнорируются. Нажмите на слово, чтобы удалить.")
    if not words:
        text += "\n\nСписок пуст."
    return text, kb.sw_list_kb(words, page, pages)


async def render_bl(db: Database, page: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_blacklist()
    pages = pages_of(total, kb.BL_PER_PAGE)
    page = clamp(page, pages)
    users = await db.list_blacklist((page - 1) * kb.BL_PER_PAGE, kb.BL_PER_PAGE)
    text = ("<b>⛔ Чёрный список авторов</b>\n" + (f"\n{note}\n" if note else "")
            + "\nСообщения этих пользователей игнорируются. Нажмите на ID, чтобы убрать из списка.")
    if not users:
        text += "\n\nСписок пуст."
    return text, kb.bl_list_kb(users, page, pages)


async def render_stats(db: Database, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    now = datetime.now()
    today = await db.leads_since(now.replace(hour=0, minute=0, second=0, microsecond=0))
    week = await db.leads_since(now - timedelta(days=7))
    total = await db.leads_total()
    chats = await db.top_chats(5)
    keys = await db.top_keys(5)
    text = (
        "<b>📊 Статистика</b>\n"
        + (f"\n{note}\n" if note else "")
        + f"\nЛидов сегодня: {today}\nЗа 7 дней: {week}\nЗа всё время: {total}\n"
        "\n<b>Топ-5 чатов:</b>\n"
    )
    text += "\n".join(f"{i}. {e(c['title'] or '—')} — {c['n']}" for i, c in enumerate(chats, 1)) or "—"
    text += "\n\n<b>Топ-5 ключевых слов:</b>\n"
    text += "\n".join(f"{i}. {e(k['keyword'])} — {k['n']}" for i, k in enumerate(keys, 1)) or "—"
    return text, kb.stats_kb()


async def render_cfg(db: Database, monitor: Monitor, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    s = await db.all_settings()
    text = (
        "<b>⚙️ Настройки парсера</b>\n"
        + (f"\n{note}\n" if note else "")
        + f"\nАккаунт юзербота: {e(monitor.me_name)}\n"
        f"Задержка между вступлениями: {s['join_delay']} сек.\n"
        f"Звук уведомлений: {'включён' if s['sound'] == '1' else 'выключен (тихий режим)'}"
    )
    return text, kb.cfg_kb(s)


def cycle(current: str, choices: tuple[int, ...]) -> int:
    try:
        idx = choices.index(int(current))
    except ValueError:
        return choices[0]
    return choices[(idx + 1) % len(choices)]


# ============================================================ Команды
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext, db: Database, monitor: Monitor) -> None:
    await state.clear()
    await drop(message)
    text, markup = await render_main(db, monitor)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery) -> None:
    await call.answer()


# ============================================================ Экран 0: главное меню
@router.callback_query(F.data == "m:main")
async def m_main(call: CallbackQuery, db: Database, monitor: Monitor) -> None:
    text, markup = await render_main(db, monitor)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "m:toggle")
async def m_toggle(call: CallbackQuery, db: Database, monitor: Monitor) -> None:
    on = await db.get_setting("monitoring") == "1"
    await db.set_setting("monitoring", "0" if on else "1")
    text, markup = await render_main(db, monitor)
    await edit(call, text, markup)
    await call.answer("⏸ Мониторинг на паузе" if on else "▶️ Мониторинг запущен")


# ============================================================ Экран 1: чаты
@router.callback_query(F.data.startswith("c:l:"))
async def c_list(call: CallbackQuery, db: Database) -> None:
    (page,) = args(call)
    text, markup = await render_chats(db, page)
    await edit(call, text, markup)
    await call.answer()


async def show_chat(call: CallbackQuery, db: Database, chat_id: int, page: int, note: str = "") -> None:
    result = await render_chat(db, chat_id, page, note)
    if result is None:
        result = await render_chats(db, page, "⚠️ Чат не найден (возможно, уже удалён).")
    await edit(call, *result)


@router.callback_query(F.data.startswith("c:v:"))
async def c_view(call: CallbackQuery, db: Database) -> None:
    chat_id, page = args(call)
    await show_chat(call, db, chat_id, page)
    await call.answer()


@router.callback_query(F.data.startswith("c:tg:"))
async def c_toggle(call: CallbackQuery, db: Database, monitor: Monitor) -> None:
    chat_id, page = args(call)
    chat = await db.get_chat(chat_id)
    if chat and chat["key"].startswith("id:") and chat["error"]:
        # чат без публичной ссылки с невыясненным доступом: вместо включения — проверка членства аккаунта
        await call.answer("🔍 Проверяю доступ…")
        try:
            ok, _failed = await monitor.verify_chats([chat_id])
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось проверить чат %s: %s", chat_id, exc)
            note = f"⚠️ Не удалось проверить доступ: {e(str(exc)[:150])}"
        else:
            note = ("✅ Аккаунт состоит в чате — мониторинг включён." if ok
                    else "❌ Аккаунт не состоит в этом чате или чат недоступен.")
        await show_chat(call, db, chat_id, page, note)
        return
    if chat:
        if chat["status"] == "active":
            await db.update_chat(chat_id, status="paused")
        elif chat["status"] == "paused":
            await db.update_chat(chat_id, status="active")
        elif chat["status"] == "error":
            await db.update_chat(chat_id, status="queued", error=None)
            monitor.enqueue([chat_id])
    await show_chat(call, db, chat_id, page)
    await call.answer()


@router.callback_query(F.data.startswith("c:rm:"))
async def c_remove(call: CallbackQuery, db: Database) -> None:
    chat_id, page = args(call)
    await db.delete_chat(chat_id)
    text, markup = await render_chats(db, page, "🗑 Чат удалён из базы (аккаунт остался в чате).")
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("c:lv_ask:"))
async def c_leave_ask(call: CallbackQuery, db: Database) -> None:
    chat_id, page = args(call)
    chat = await db.get_chat(chat_id)
    if not chat:
        await show_chat(call, db, chat_id, page)
    else:
        await edit(
            call,
            f"🚪 Выйти из «{e(kb.chat_title(chat))}» и удалить его из базы?\n\n"
            "Аккаунт юзербота покинет чат (группу, супергруппу или канал). Бот выполняет только выход: "
            "переписка не удаляется и не очищается.\n"
            "Запись о чате исчезнет из TGLeadMonitor; уже найденные лиды останутся в статистике и экспорте.",
            kb.chat_leave_confirm_kb(chat_id, page))
    await call.answer()


@router.callback_query(F.data.startswith("c:lv:"))
async def c_leave(call: CallbackQuery, db: Database, monitor: Monitor) -> None:
    chat_id, page = args(call)
    chat = await db.get_chat(chat_id)
    note = "🗑 Запись удалена."
    if chat:
        try:
            outcome = await monitor.leave(chat)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось выйти из чата %s: %s", chat_id, exc)
            await show_chat(call, db, chat_id, page, f"⚠️ Не удалось выйти из чата: {e(str(exc)[:150])}")
            await call.answer("Не удалось выйти из чата", show_alert=True)
            return
        await db.delete_chat(chat_id)
        if outcome == "left":
            note = "🚪 Аккаунт вышел из чата (переписка не удалялась), запись удалена."
        else:
            note = "ℹ️ Аккаунт уже не состоял в этом чате. Запись удалена."
    text, markup = await render_chats(db, page, note)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("c:add:"))
async def c_add(call: CallbackQuery, state: FSMContext) -> None:
    (page,) = args(call)
    text = (
        "<b>➕ Добавление чатов</b>\n\n"
        "Пришлите одним сообщением:\n"
        "• <code>@username</code> или ссылку <code>https://t.me/...</code>\n"
        "• приватную ссылку-приглашение <code>t.me/+...</code>\n"
        "• список ссылок (по одной на строку)\n"
        "• или файл <b>.txt</b> со ссылками\n\n"
        "Дубликаты отсеются, чаты встанут в очередь на безопасное вступление."
    )
    await begin_input(call, state, AddChat.waiting_input, text, kb.chat_add_kb(page), page=page)


@router.message(AddChat.waiting_input)
async def add_chat_input(message: Message, state: FSMContext, bot: Bot, db: Database, monitor: Monitor) -> None:
    data = await state.get_data()
    page = data.get("page", 1)
    text = message.text or message.caption or ""
    if message.document:
        doc = message.document
        if (doc.file_name or "").lower().endswith(".txt") and (doc.file_size or 0) <= 1_000_000:
            buf = io.BytesIO()
            await bot.download(doc, destination=buf)
            text = buf.getvalue().decode("utf-8-sig", errors="ignore")
        else:
            await drop(message)
            await edit_menu(bot, data, "⚠️ Нужен текстовый файл .txt размером до 1 МБ. Попробуйте ещё раз.",
                            kb.chat_add_kb(page))
            return
    await drop(message)
    refs, invalid = parse_chat_refs(text)
    if not refs:
        await edit_menu(bot, data, "⚠️ Не нашёл ни одной корректной ссылки или @username. Попробуйте ещё раз.",
                        kb.chat_add_kb(page))
        return
    new_ids: list[int] = []
    duplicates = 0
    for key, username in refs[:500]:
        chat_id = await db.add_chat(key, username=username, status="queued")
        if chat_id is None:
            duplicates += 1
        else:
            new_ids.append(chat_id)
    monitor.enqueue(new_ids)
    delay = await db.get_setting("join_delay")
    await state.clear()
    report = (
        "<b>✅ Готово</b>\n\n"
        f"Поставлено в очередь вступления: {len(new_ids)}\n"
        f"Уже были в базе: {duplicates}\n"
        f"Не распознано: {invalid}\n\n"
        f"Вступление идёт в фоне с паузой {delay} сек. Статусы смотрите в списке чатов."
    )
    await edit_menu(bot, data, report, kb.chat_add_kb(page))


@router.callback_query(F.data.startswith("c:sync:"))
async def c_sync(call: CallbackQuery, db: Database, monitor: Monitor) -> None:
    (page,) = args(call)
    await call.answer()
    await edit(call, "🔄 Сканирую диалоги аккаунта…", kb.chat_add_kb(page))
    try:
        added, total = await monitor.sync_dialogs()
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка импорта диалогов")
        text, markup = await render_chats(db, page, f"⚠️ Не удалось импортировать: {e(str(exc)[:150])}")
    else:
        text, markup = await render_chats(
            db, page, f"🔄 Найдено групп и каналов в аккаунте: {total}\nДобавлено новых: {added}")
    await edit(call, text, markup)


# ============================================================ Экран 2: категории
@router.callback_query(F.data.startswith("k:l:"))
async def k_list(call: CallbackQuery, db: Database) -> None:
    (page,) = args(call)
    text, markup = await render_cats(db, page)
    await edit(call, text, markup)
    await call.answer()


async def show_cat(call: CallbackQuery, db: Database, cat_id: int, page: int, note: str = "") -> None:
    result = await render_cat(db, cat_id, page, note)
    if result is None:
        result = await render_cats(db, page, "⚠️ Категория не найдена.")
    await edit(call, *result)


@router.callback_query(F.data.startswith("k:v:"))
async def k_view(call: CallbackQuery, db: Database) -> None:
    cat_id, page = args(call)
    await show_cat(call, db, cat_id, page)
    await call.answer()


@router.callback_query(F.data.startswith("k:add:"))
async def k_add(call: CallbackQuery, state: FSMContext) -> None:
    (page,) = args(call)
    await begin_input(call, state, AddCategory.waiting_name,
                      "<b>➕ Новая категория</b>\n\nПришлите название категории (до 50 символов).",
                      kb.cat_add_kb(page), page=page)


@router.message(AddCategory.waiting_name)
async def k_add_input(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    data = await state.get_data()
    page = data.get("page", 1)
    name = " ".join((message.text or "").split())[:50]
    await drop(message)
    if not name:
        await edit_menu(bot, data, "⚠️ Название не может быть пустым. Пришлите текстом.", kb.cat_add_kb(page))
        return
    cat_id = await db.add_category(name)
    await state.clear()
    text, markup = await render_cat(db, cat_id, page, "✅ Категория создана. Добавьте в неё слова.")
    await edit_menu(bot, data, text, markup)


@router.callback_query(F.data.startswith("k:mode:"))
async def k_mode(call: CallbackQuery, db: Database) -> None:
    cat_id, page = args(call)
    await db.toggle_category_mode(cat_id)
    await show_cat(call, db, cat_id, page)
    await call.answer("Режим поиска изменён")


@router.callback_query(F.data.startswith("k:del_ask:"))
async def k_del_ask(call: CallbackQuery, db: Database) -> None:
    cat_id, page = args(call)
    cat = await db.get_category(cat_id)
    if not cat:
        await show_cat(call, db, cat_id, page)
    else:
        await edit(call, f"🗑 Удалить категорию «{e(cat['name'])}» вместе со всеми её словами ({cat['n']})?",
                   kb.cat_del_confirm_kb(cat_id, page))
    await call.answer()


@router.callback_query(F.data.startswith("k:del:"))
async def k_del(call: CallbackQuery, db: Database) -> None:
    cat_id, page = args(call)
    await db.delete_category(cat_id)
    text, markup = await render_cats(db, page, "🗑 Категория удалена.")
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("kw:add:"))
async def kw_add(call: CallbackQuery, state: FSMContext) -> None:
    cat_id, page = args(call)
    await begin_input(call, state, AddKeyword.waiting_words,
                      "<b>➕ Добавление слов</b>\n\nПришлите слова или фразы через запятую или с новой строки.",
                      kb.kw_add_kb(cat_id, page), cat_id=cat_id, page=page)


@router.message(AddKeyword.waiting_words)
async def kw_add_input(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    data = await state.get_data()
    cat_id, page = data["cat_id"], data.get("page", 1)
    words = split_words(message.text or "")
    await drop(message)
    if not words:
        await edit_menu(bot, data, "⚠️ Не нашёл слов. Пришлите текстом через запятую или с новой строки.",
                        kb.kw_add_kb(cat_id, page))
        return
    added = await db.add_keywords(cat_id, words)
    await state.clear()
    note = f"✅ Добавлено слов: {added}" + (f" (дублей пропущено: {len(words) - added})" if added < len(words) else "")
    result = await render_cat(db, cat_id, page, note)
    if result is None:
        result = await render_cats(db, page, "⚠️ Категория не найдена.")
    await edit_menu(bot, data, *result)


@router.callback_query(F.data.startswith("kw:del_l:"))
async def kw_del_list(call: CallbackQuery, db: Database) -> None:
    cat_id, page, kw_page = args(call)
    result = await render_kw_del(db, cat_id, page, kw_page)
    if result is None:
        result = await render_cats(db, page, "⚠️ Категория не найдена.")
    await edit(call, *result)
    await call.answer()


@router.callback_query(F.data.startswith("kw:rm:"))
async def kw_remove(call: CallbackQuery, db: Database) -> None:
    kw_id, cat_id, page, kw_page = args(call)
    await db.delete_keyword(kw_id)
    result = await render_kw_del(db, cat_id, page, kw_page)
    if result is None:
        result = await render_cats(db, page, "⚠️ Категория не найдена.")
    await edit(call, *result)
    await call.answer("Слово удалено")


# ============================================================ Экран 3: стоп-слова и фильтры
@router.callback_query(F.data == "f:main")
async def f_main(call: CallbackQuery, db: Database) -> None:
    text, markup = await render_filters(db)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "f:dedup")
async def f_dedup(call: CallbackQuery, db: Database) -> None:
    await db.set_setting("dedup_hours", cycle(await db.get_setting("dedup_hours"), DEDUP_CHOICES))
    text, markup = await render_filters(db)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "f:bots")
async def f_bots(call: CallbackQuery, db: Database) -> None:
    on = await db.get_setting("ignore_bots") == "1"
    await db.set_setting("ignore_bots", "0" if on else "1")
    text, markup = await render_filters(db)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "f:len")
async def f_len(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    s = await db.all_settings()
    text = (f"<b>📏 Длина текста</b>\n\nСейчас: {s['min_len']} – {s['max_len']} симв.\n\n"
            "Пришлите два числа через пробел: минимум и максимум (например <code>20 2000</code>). "
            "Максимум — до 4096.")
    await begin_input(call, state, FilterLen.waiting_range, text, kb.filter_len_kb())


@router.message(FilterLen.waiting_range)
async def f_len_input(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    data = await state.get_data()
    nums = re.findall(r"\d+", message.text or "")
    await drop(message)
    if len(nums) != 2 or not (1 <= int(nums[0]) <= int(nums[1]) <= 4096):
        await edit_menu(bot, data, "⚠️ Нужно два числа через пробел: минимум ≤ максимум ≤ 4096 "
                                   "(например <code>20 2000</code>).", kb.filter_len_kb())
        return
    await db.set_setting("min_len", int(nums[0]))
    await db.set_setting("max_len", int(nums[1]))
    await state.clear()
    text, markup = await render_filters(db, "✅ Длина текста обновлена.")
    await edit_menu(bot, data, text, markup)


# --- стоп-слова
@router.callback_query(F.data.startswith("sw:l:"))
async def sw_list(call: CallbackQuery, db: Database) -> None:
    (page,) = args(call)
    text, markup = await render_sw(db, page)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("sw:rm:"))
async def sw_remove(call: CallbackQuery, db: Database) -> None:
    sw_id, page = args(call)
    await db.delete_stopword(sw_id)
    text, markup = await render_sw(db, page)
    await edit(call, text, markup)
    await call.answer("Стоп-слово удалено")


@router.callback_query(F.data.startswith("sw:add:"))
async def sw_add(call: CallbackQuery, state: FSMContext) -> None:
    (page,) = args(call)
    await begin_input(call, state, AddStopword.waiting_words,
                      "<b>➕ Стоп-слова</b>\n\nПришлите слова или фразы через запятую или с новой строки.",
                      kb.sw_add_kb(page), page=page)


@router.message(AddStopword.waiting_words)
async def sw_add_input(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    data = await state.get_data()
    page = data.get("page", 1)
    words = split_words(message.text or "")
    await drop(message)
    if not words:
        await edit_menu(bot, data, "⚠️ Не нашёл слов. Пришлите текстом.", kb.sw_add_kb(page))
        return
    added = await db.add_stopwords(words)
    await state.clear()
    text, markup = await render_sw(db, page, f"✅ Добавлено стоп-слов: {added}")
    await edit_menu(bot, data, text, markup)


# --- чёрный список авторов
@router.callback_query(F.data.startswith("bl:l:"))
async def bl_list(call: CallbackQuery, db: Database) -> None:
    (page,) = args(call)
    text, markup = await render_bl(db, page)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("bl:rm:"))
async def bl_remove(call: CallbackQuery, db: Database) -> None:
    user_id, page = args(call)
    await db.remove_blacklist(user_id)
    text, markup = await render_bl(db, page)
    await edit(call, text, markup)
    await call.answer("Автор убран из чёрного списка")


@router.callback_query(F.data.startswith("bl:add:"))
async def bl_add(call: CallbackQuery, state: FSMContext) -> None:
    (page,) = args(call)
    await begin_input(call, state, AddBlacklist.waiting_ids,
                      "<b>➕ Чёрный список</b>\n\nПришлите числовые ID авторов (через пробел, запятую или с новой строки).",
                      kb.bl_add_kb(page), page=page)


@router.message(AddBlacklist.waiting_ids)
async def bl_add_input(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    data = await state.get_data()
    page = data.get("page", 1)
    ids = [int(x) for x in re.findall(r"\d{3,15}", message.text or "")]
    await drop(message)
    if not ids:
        await edit_menu(bot, data, "⚠️ Не нашёл числовых ID. Пришлите ID цифрами.", kb.bl_add_kb(page))
        return
    added = await db.add_blacklist(ids)
    await state.clear()
    text, markup = await render_bl(db, page, f"✅ Добавлено в чёрный список: {added}")
    await edit_menu(bot, data, text, markup)


# ============================================================ Экран 4: статистика и экспорт
@router.callback_query(F.data == "s:main")
async def s_main(call: CallbackQuery, db: Database) -> None:
    text, markup = await render_stats(db)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.in_({"s:exp:csv", "s:exp:txt"}))
async def s_export(call: CallbackQuery, db: Database) -> None:
    leads = await db.all_leads()
    if not leads:
        await call.answer("Лидов пока нет — нечего выгружать", show_alert=True)
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if call.data.endswith("csv"):
        file = BufferedInputFile(exporter.leads_to_csv(leads), filename=f"leads_{stamp}.csv")
    else:
        file = BufferedInputFile(exporter.leads_to_txt(leads), filename=f"leads_{stamp}.txt")
    await call.message.answer_document(file, caption=f"📦 Лидов в файле: {len(leads)}")
    await call.answer("Файл отправлен")


@router.callback_query(F.data == "s:clr_ask")
async def s_clear_ask(call: CallbackQuery, db: Database) -> None:
    total = await db.leads_total()
    await edit(call, f"🧹 Удалить всю историю лидов ({total} шт.)? Действие необратимо.",
               kb.stats_clear_confirm_kb())
    await call.answer()


@router.callback_query(F.data == "s:clr")
async def s_clear(call: CallbackQuery, db: Database) -> None:
    await db.clear_leads()
    text, markup = await render_stats(db, "🧹 История лидов очищена.")
    await edit(call, text, markup)
    await call.answer()


# ============================================================ Экран 5: настройки
@router.callback_query(F.data == "cfg:main")
async def cfg_main(call: CallbackQuery, db: Database, monitor: Monitor) -> None:
    text, markup = await render_cfg(db, monitor)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "cfg:delay")
async def cfg_delay(call: CallbackQuery, db: Database, monitor: Monitor) -> None:
    await db.set_setting("join_delay", cycle(await db.get_setting("join_delay"), DELAY_CHOICES))
    text, markup = await render_cfg(db, monitor)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "cfg:sound")
async def cfg_sound(call: CallbackQuery, db: Database, monitor: Monitor) -> None:
    on = await db.get_setting("sound") == "1"
    await db.set_setting("sound", "0" if on else "1")
    text, markup = await render_cfg(db, monitor)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "cfg:bkp")
async def cfg_backup(call: CallbackQuery, db: Database) -> None:
    raw = exporter.config_to_json(await db.export_config())
    await call.message.answer_document(BufferedInputFile(raw, filename="backup_config.json"),
                                       caption="💾 Бэкап чатов, категорий, ключей и стоп-слов")
    await call.answer("Файл отправлен")


@router.callback_query(F.data == "cfg:rst")
async def cfg_restore(call: CallbackQuery, state: FSMContext) -> None:
    text = ("<b>📥 Восстановление из JSON</b>\n\nПришлите файл <code>backup_config.json</code>, "
            "полученный через «Бэкап настроек». Данные будут <b>добавлены</b> к текущим, ничего не удаляется.")
    await begin_input(call, state, RestoreCfg.waiting_file, text, kb.cfg_back_kb())


@router.message(RestoreCfg.waiting_file)
async def cfg_restore_input(message: Message, state: FSMContext, bot: Bot, db: Database, monitor: Monitor) -> None:
    data = await state.get_data()
    doc = message.document
    await drop(message)
    if doc is None or not (doc.file_name or "").lower().endswith(".json") or (doc.file_size or 0) > 5_000_000:
        await edit_menu(bot, data, "⚠️ Пришлите файл .json размером до 5 МБ.", kb.cfg_back_kb())
        return
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        config = exporter.parse_config(buf.getvalue())
    except ValueError as exc:
        await edit_menu(bot, data, f"⚠️ {e(str(exc))}. Пришлите корректный файл бэкапа.", kb.cfg_back_kb())
        return
    res = await db.import_config(config)
    monitor.enqueue(res["queued_ids"])
    await state.clear()
    check_line = ""
    if res["check_ids"]:
        # чаты без публичной ссылки не включаем вслепую: проверяем, состоит ли в них аккаунт
        await edit_menu(bot, data, "⏳ Проверяю доступ аккаунта к восстановленным чатам без публичной ссылки…",
                        kb.cfg_back_kb())
        try:
            ok, failed = await monitor.verify_chats(res["check_ids"])
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось проверить восстановленные чаты: %s", exc)
            check_line = (f"Чатов без публичной ссылки: {len(res['check_ids'])} — доступ проверить не удалось "
                          f"({e(str(exc)[:100])}), они оставлены на паузе ⏸; откройте карточку чата и "
                          "нажмите «Проверить доступ»\n")
        else:
            check_line = (f"Чатов без публичной ссылки: {len(res['check_ids'])} — аккаунт состоит в {ok} "
                          f"(🟢 включены), недоступны {failed} (❌ — вступите вручную и нажмите «Проверить доступ»)\n")
    text = (
        "<b>✅ Восстановление завершено</b>\n\n"
        f"Чатов добавлено: {res['chats']} (в очередь на вступление: {len(res['queued_ids'])})\n"
        f"{check_line}"
        f"Категорий создано: {res['categories']}\n"
        f"Ключевых слов добавлено: {res['keywords']}\n"
        f"Стоп-слов добавлено: {res['stopwords']}\n"
        f"В чёрный список добавлено: {res['blacklist']}\n"
        f"Настроек применено: {res['settings']}"
    )
    await edit_menu(bot, data, text, kb.cfg_back_kb())


# ============================================================ Кнопки под карточкой лида
@router.callback_query(F.data.startswith("ld:ban_u:"))
async def lead_ban(call: CallbackQuery, db: Database) -> None:
    user_id = int(call.data.split(":")[2])
    await db.add_blacklist([user_id])
    markup = call.message.reply_markup
    if markup:  # убираем кнопку ЧС из карточки
        rows = [[b for b in row if not (b.callback_data or "").startswith("ld:ban_u:")]
                for row in markup.inline_keyboard]
        rows = [r for r in rows if r]
        try:
            await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except TelegramBadRequest:
            pass
    await call.answer("🚷 Автор добавлен в чёрный список")


@router.callback_query(F.data == "ld:hide")
async def lead_hide(call: CallbackQuery) -> None:
    try:
        await call.message.delete()
    except TelegramAPIError:
        await call.answer("Не удалось удалить сообщение", show_alert=True)
        return
    await call.answer()


# ============================================================ Устаревшие кнопки
@router.callback_query()
async def stale(call: CallbackQuery) -> None:
    await call.answer("Кнопка устарела — откройте меню командой /start", show_alert=True)
