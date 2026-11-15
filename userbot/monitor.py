"""Юзербот на Telethon: очередь вступления в чаты, прослушка, фильтрация, антидубли, отправка лидов."""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from telethon import TelegramClient, errors, events, utils
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, DeleteChatUserRequest, ImportChatInviteRequest
from telethon.tl.types import Channel, Chat, InputUserSelf, PeerChannel, User

from bot.keyboards import lead_kb
from database.db import Database

log = logging.getLogger("tgleadmonitor.monitor")

# ---------------------------------------------------------------------------
# Разбор ссылок на чаты
# ---------------------------------------------------------------------------
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
INVITE_RE = re.compile(r"^(?:\+|joinchat/)([A-Za-z0-9_-]{5,64})$")
URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(.+)$", re.I)
TG_INVITE_RE = re.compile(r"^tg://join\?invite=([A-Za-z0-9_-]{5,64})", re.I)
TG_RESOLVE_RE = re.compile(r"^tg://resolve\?domain=([A-Za-z0-9_]+)", re.I)
RESERVED_PATHS = {"c", "addlist", "proxy", "share", "socks", "login", "addstickers", "addemoji", "setlanguage"}


def parse_chat_ref(token: str) -> tuple[str, str | None] | None:
    """Возвращает (key, username) либо None. key: 'username' (нижний регистр) или '+invite_hash'."""
    token = token.strip().strip("()<>[]\"'«»,;")
    if not token:
        return None
    m = TG_INVITE_RE.match(token)
    if m:
        return "+" + m.group(1), None
    m = TG_RESOLVE_RE.match(token)
    if m and USERNAME_RE.match(m.group(1)):
        return m.group(1).lower(), m.group(1)
    m = URL_RE.match(token)
    if m:
        path = m.group(1).split("?")[0].split("#")[0].strip("/")
        if path.startswith("s/"):
            path = path[2:]
        inv = INVITE_RE.match(path)
        if inv:
            return "+" + inv.group(1), None
        first = path.split("/")[0]
        if first.lower() in RESERVED_PATHS:
            return None
        if USERNAME_RE.match(first):
            return first.lower(), first
        return None
    if token.startswith("@"):
        token = token[1:]
    if USERNAME_RE.match(token):
        return token.lower(), token
    return None


def parse_chat_refs(text: str) -> tuple[list[tuple[str, str | None]], int]:
    """Разбирает произвольный текст на ссылки. Возвращает (уникальные ссылки, число нераспознанных)."""
    found: dict[str, str | None] = {}
    invalid = 0
    for token in re.split(r"[\s,;]+", text):
        if not token:
            continue
        parsed = parse_chat_ref(token)
        if parsed is None:
            invalid += 1
        else:
            found.setdefault(parsed[0], parsed[1])
    return list(found.items()), invalid


ERROR_TEXTS = {
    "UsernameNotOccupiedError": "Юзернейм не существует",
    "UsernameInvalidError": "Некорректный юзернейм",
    "InviteHashExpiredError": "Ссылка-приглашение истекла",
    "InviteHashInvalidError": "Ссылка-приглашение недействительна",
    "InviteRequestSentError": "Отправлена заявка на вступление — дождитесь одобрения админом и повторите",
    "ChannelPrivateError": "Чат приватный, либо аккаунт в нём забанен",
    "UserBannedInChannelError": "Аккаунт забанен в этом чате",
    "ChannelsTooMuchError": "Достигнут лимит чатов на аккаунте",
    "UserAlreadyParticipantError": "Аккаунт уже состоит в чате",
    "ChatAdminRequiredError": "Нужны права администратора",
}


def describe_error(exc: Exception) -> str:
    name = type(exc).__name__
    if name in ERROR_TEXTS:
        return ERROR_TEXTS[name]
    if isinstance(exc, ValueError):
        return str(exc)[:200] or "Не удалось найти чат"
    return f"{name}: {exc}"[:300]


NOT_MEMBER_TEXT = "Аккаунт не состоит в этом чате (или чат недоступен). Вступите вручную и нажмите «Проверить доступ»"


# ---------------------------------------------------------------------------
# Фильтры и карточка лида
# ---------------------------------------------------------------------------
def is_bot_or_channel_message(msg, sender) -> bool:
    """True — сообщение отправлено ботом / от имени канала либо является постом/пересылкой из канала.

    Используется при включённом фильтре «Игнор ботов и каналов». Все обращения к атрибутам
    через getattr, поэтому функция не бросает исключений на нетипичных сообщениях.
    """
    # сообщение отправлено через инлайн-бота
    if getattr(msg, "via_bot_id", None) is not None:
        return True
    # пересланные посты из каналов
    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None:
        if getattr(fwd, "channel_post", None):
            return True
        if isinstance(getattr(fwd, "from_id", None), PeerChannel):
            return True
        if isinstance(getattr(fwd, "saved_from_peer", None), PeerChannel):
            return True
    # пост в канале (broadcast)
    if getattr(msg, "post", False):
        return True
    # отправитель — канал или бот
    if isinstance(sender, Channel):
        return True
    if getattr(sender, "bot", False):
        return True
    return False


def text_hash(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compile_keyword(word: str, mode: str) -> re.Pattern:
    escaped = re.escape(word)
    if mode == "exact":
        return re.compile(r"(?<!\w)" + escaped + r"(?!\w)", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def highlight(text: str, patterns: list[re.Pattern]) -> str:
    """Экранирует текст и оборачивает найденные ключи в <b><u>...</u></b>."""
    spans: list[list[int]] = []
    for rx in patterns:
        spans.extend([m.start(), m.end()] for m in rx.finditer(text) if m.end() > m.start())
    spans.sort()
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out: list[str] = []
    pos = 0
    for a, b in merged:
        out.append(html.escape(text[pos:a]))
        out.append("<b><u>" + html.escape(text[a:b]) + "</u></b>")
        pos = b
    out.append(html.escape(text[pos:]))
    return "".join(out)


def build_card(category: str, keys: list[str], chat_title: str, chat_username: str | None,
               author_name: str, author_username: str | None, author_id: int | None,
               text: str, patterns: list[re.Pattern]) -> str:
    esc = html.escape
    chat_line = esc(chat_title) + (f" (@{esc(chat_username)})" if chat_username else "")
    author_bits = []
    if author_username:
        author_bits.append(f"@{esc(author_username)}")
    if author_id:
        author_bits.append(f"ID: {author_id}")
    author_line = esc(author_name) + (f" ({' | '.join(author_bits)})" if author_bits else "")
    head = (f"🏷 Категория: {esc(category)} | Ключ: {esc(', '.join(keys))}\n"
            f"💬 Чат: {chat_line}\n"
            f"👤 Автор: {author_line}\n"
            f"📝 Текст сообщения:\n")
    body = text if len(text) <= 2500 else text[:2500] + "…"
    card = head + highlight(body, patterns)
    if len(card) > 4096:  # спецсимволы раздули экранирование — упрощённый вариант
        card = head + esc(text[:1200]) + "…"
    return card[:4096]


def message_link(chat_username: str | None, chat_id: int, msg_id: int) -> str | None:
    if chat_username:
        return f"https://t.me/{chat_username}/{msg_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{msg_id}"
    return None


# ---------------------------------------------------------------------------
# Монитор
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, client: TelegramClient, db: Database, bot: Bot, admin_id: int):
        self.client = client
        self.db = db
        self.bot = bot
        self.admin_id = admin_id
        self.me_name = "—"
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._cache: dict | None = None
        self._cache_ver = -1

    # ----- запуск -----
    async def setup(self) -> None:
        me = await self.client.get_me()
        self.me_name = f"@{me.username}" if me.username else (me.first_name or str(me.id))
        for chat_id in await self.db.queued_chat_ids():
            self.queue.put_nowait(chat_id)
        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True))

    def enqueue(self, chat_ids: list[int]) -> None:
        for chat_id in chat_ids:
            self.queue.put_nowait(chat_id)

    async def _notify(self, text: str) -> None:
        try:
            await self.bot.send_message(self.admin_id, text, disable_notification=True)
        except TelegramAPIError:
            log.warning("Не удалось отправить служебное уведомление админу")

    # ----- кэш настроек/ключей (обновляется при любом изменении в БД) -----
    async def _get_cache(self) -> dict:
        if self._cache is None or self._cache_ver != self.db.version:
            ver = self.db.version
            categories = []
            for cat in await self.db.categories_with_words():
                patterns = [(w, compile_keyword(w, cat["match_mode"])) for w in cat["words"]]
                if patterns:
                    categories.append((cat["name"], patterns))
            self._cache = {
                "settings": await self.db.all_settings(),
                "active_ids": await self.db.active_chat_ids(),
                "categories": categories,
                "stopwords": await self.db.all_stopwords(),
                "blacklist": await self.db.blacklist_ids(),
            }
            self._cache_ver = ver
        return self._cache

    # ----- прослушка сообщений -----
    async def on_message(self, event: events.NewMessage.Event) -> None:
        try:
            await self._process(event)
        except Exception:  # noqa: BLE001 — слушатель не должен падать из-за одного сообщения
            log.exception("Ошибка обработки сообщения")

    async def _process(self, event: events.NewMessage.Event) -> None:
        cache = await self._get_cache()
        s = cache["settings"]
        if s["monitoring"] != "1":
            return
        if not (event.is_group or event.is_channel):
            return
        if event.chat_id not in cache["active_ids"]:
            return

        msg = event.message
        text = (msg.raw_text or "").strip()
        if not text:
            return
        if not (int(s["min_len"]) <= len(text) <= int(s["max_len"])):
            return

        ignore_bots = s["ignore_bots"] == "1"
        # дешёвая проверка до запроса отправителя (инлайн-боты, пересылки, посты каналов)
        if ignore_bots and is_bot_or_channel_message(msg, None):
            return

        sender = await event.get_sender()
        # полная проверка с учётом отправителя (бот или канал)
        if ignore_bots and is_bot_or_channel_message(msg, sender):
            return
        author_id = sender.id if isinstance(sender, User) else None
        if author_id is not None and author_id in cache["blacklist"]:
            return

        lowered = text.lower()
        if any(sw in lowered for sw in cache["stopwords"]):
            return

        matched_categories: list[str] = []
        matched: list[tuple[str, re.Pattern]] = []
        seen_words: set[str] = set()
        for name, patterns in cache["categories"]:
            found = [(w, rx) for w, rx in patterns if rx.search(text)]
            if found:
                matched_categories.append(name)
                for w, rx in found:
                    if w not in seen_words:
                        seen_words.add(w)
                        matched.append((w, rx))
        if not matched:
            return
        matched_category = ", ".join(matched_categories)

        digest = text_hash(text)
        if await self.db.seen_check_and_mark(digest, int(s["dedup_hours"])):
            return

        chat = await event.get_chat()
        chat_title = getattr(chat, "title", None) or str(event.chat_id)
        chat_username = getattr(chat, "username", None)
        if isinstance(sender, User):
            author_name = " ".join(p for p in (sender.first_name, sender.last_name) if p) or "Без имени"
            author_username = sender.username
        elif sender is not None:
            author_name = getattr(sender, "title", None) or "Канал / аноним"
            author_username = getattr(sender, "username", None)
        else:
            author_name, author_username = "Неизвестно", None

        link = message_link(chat_username, event.chat_id, msg.id)
        keys = [w for w, _ in matched]
        await self.db.add_lead(
            chat_tg_id=event.chat_id, chat_title=chat_title, chat_username=chat_username,
            author_id=author_id, author_name=author_name, author_username=author_username,
            category=matched_category, keywords=keys, text=text, link=link, text_hash=digest)

        card = build_card(matched_category, keys, chat_title, chat_username, author_name,
                          author_username, author_id, text, [rx for _, rx in matched])
        silent = s["sound"] != "1"
        try:
            await self.bot.send_message(self.admin_id, card, reply_markup=lead_kb(link, author_username, author_id),
                                        disable_notification=silent)
        except TelegramBadRequest:
            # например, tg://user?id=... отклонён из-за приватности — повторяем без кнопки «Написать»
            await self.bot.send_message(self.admin_id, card, disable_notification=silent,
                                        reply_markup=lead_kb(link, author_username, author_id, with_write=False))

    # ----- очередь вступления -----
    async def join_worker(self) -> None:
        while True:
            chat_id = await self.queue.get()
            try:
                attempted = await self._join_one(chat_id)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в очереди вступления")
                attempted = False
            if attempted:
                delay = int(await self.db.get_setting("join_delay"))
                await asyncio.sleep(delay)

    async def _join_one(self, chat_id: int) -> bool:
        """True — была попытка вступления (после неё нужна пауза)."""
        chat = await self.db.get_chat(chat_id)
        if not chat or chat["status"] != "queued":
            return False
        try:
            entity = await self._join(chat["key"])
        except errors.FloodWaitError as exc:
            wait = int(exc.seconds) + 2
            log.warning("FloodWait %s сек. при вступлении в %s", wait, chat["key"])
            await self._notify(f"⏳ Telegram ограничил вступления (FloodWait): пауза {wait} сек. "
                               f"Очередь продолжится автоматически.")
            await asyncio.sleep(wait)
            self.queue.put_nowait(chat_id)
            return False
        except Exception as exc:  # noqa: BLE001
            await self.db.update_chat(chat_id, status="error", error=describe_error(exc))
            return True

        tg_id = utils.get_peer_id(entity)
        other = await self.db.get_chat_by_tg(tg_id)
        if other and other["id"] != chat_id:
            await self.db.delete_chat(chat_id)  # этот чат уже есть в базе под другим ключом
            return True
        await self.db.update_chat(chat_id, tg_id=tg_id, title=getattr(entity, "title", None),
                                  username=getattr(entity, "username", None), status="active", error=None)
        return True

    async def _join(self, key: str):
        if key.startswith("id:"):
            raise ValueError("У чата нет публичной ссылки — автоматически вступить нельзя. "
                             "Вступите вручную и нажмите «Проверить доступ» или «Импорт из аккаунта»")
        if key.startswith("+"):
            invite_hash = key[1:]
            try:
                updates = await self.client(ImportChatInviteRequest(invite_hash))
                return updates.chats[0]
            except errors.UserAlreadyParticipantError:
                info = await self.client(CheckChatInviteRequest(invite_hash))
                chat = getattr(info, "chat", None)
                if chat is None:
                    raise
                return chat
        entity = await self.client.get_entity(key)
        if isinstance(entity, User):
            raise ValueError("Это пользователь или бот, а не группа/канал")
        if isinstance(entity, Channel) and getattr(entity, "left", True):
            await self.client(JoinChannelRequest(entity))
        return entity

    # ----- импорт из аккаунта и выход -----
    async def sync_dialogs(self) -> tuple[int, int]:
        """Добавляет группы/каналы, в которых уже состоит аккаунт. Возвращает (добавлено, найдено всего)."""
        added = total = 0
        async for dialog in self.client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue
            total += 1
            tg_id = dialog.id
            username = getattr(dialog.entity, "username", None)
            key = username.lower() if username else f"id:{tg_id}"
            existing = await self.db.get_chat_by_tg(tg_id) or await self.db.get_chat_by_key(key)
            if existing:
                unverified = existing["status"] == "paused" and bool(existing["error"])
                if existing["tg_id"] is None or existing["status"] in ("queued", "error") or unverified:
                    await self.db.update_chat(existing["id"], tg_id=tg_id, title=dialog.name,
                                              username=username, status="active", error=None)
                continue
            if await self.db.add_chat(key, username=username, title=dialog.name, status="active", tg_id=tg_id):
                added += 1
        return added, total

    async def verify_chats(self, chat_ids: list[int]) -> tuple[int, int]:
        """Проверяет, что аккаунт состоит в чатах (по списку его диалогов).

        Состоит -> статус 🟢 active, не состоит / чат недоступен -> статус ❌ error.
        Возвращает (подтверждено, недоступно).
        """
        members: dict[int, object] = {}
        async for dialog in self.client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                members[dialog.id] = dialog
        ok = failed = 0
        for chat_id in chat_ids:
            chat = await self.db.get_chat(chat_id)
            if not chat:
                continue
            dialog = members.get(chat["tg_id"]) if chat["tg_id"] is not None else None
            if dialog is None:
                await self.db.update_chat(chat_id, status="error", error=NOT_MEMBER_TEXT)
                failed += 1
            else:
                await self.db.update_chat(chat_id, title=dialog.name,
                                          username=getattr(dialog.entity, "username", None),
                                          status="active", error=None)
                ok += 1
        return ok, failed

    async def leave(self, chat: dict) -> str:
        """Выходит из группы / супергруппы / канала. Историю диалога НЕ удаляет и не очищает.

        Каналы и супергруппы — LeaveChannelRequest, обычные (небольшие) группы — DeleteChatUserRequest
        без revoke_history. Возвращает "left" (вышли) или "not_member" (аккаунт уже не состоял в чате).
        """
        tg_id = chat.get("tg_id")
        if tg_id is None:
            return "not_member"  # в чат ни разу не вступали
        try:
            entity = await self.client.get_entity(tg_id)
        except ValueError as exc:
            raise ValueError("Чат не найден среди доступных аккаунту. Если вы уже вышли из него, "
                             "используйте «Удалить из базы»") from exc
        except (errors.ChannelPrivateError, errors.UserNotParticipantError):
            return "not_member"
        try:
            if isinstance(entity, Channel):
                await self.client(LeaveChannelRequest(entity))
            elif isinstance(entity, Chat):
                await self.client(DeleteChatUserRequest(chat_id=entity.id, user_id=InputUserSelf()))
            else:
                raise ValueError("Это не группа и не канал")
        except (errors.ChannelPrivateError, errors.UserNotParticipantError):
            return "not_member"
        return "left"
