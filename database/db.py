"""Работа с SQLite (aiosqlite): схема, CRUD, стартовый пресет, бэкап/восстановление."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

TS_FMT = "%Y-%m-%d %H:%M:%S"

DEFAULT_SETTINGS = {
    "monitoring": "1",
    "dedup_hours": "24",
    "min_len": "15",
    "max_len": "1500",
    "ignore_bots": "1",
    "join_delay": "20",
    "sound": "1",
}
BACKUP_SETTINGS = ("dedup_hours", "min_len", "max_len", "ignore_bots", "join_delay", "sound")
DEDUP_CHOICES = (0, 6, 24, 72)
DELAY_CHOICES = (10, 20, 45, 60)

# Чаты без публичной ссылки, восстановленные из бэкапа, ждут проверки доступа аккаунта
UNVERIFIED_ERROR = "Восстановлен из бэкапа: доступ аккаунта к чату не проверен"

STARTER_CATEGORY = "💻 Заказы (Python / Боты)"
STARTER_KEYWORDS = ["нужен бот", "ищу кодера", "написать скрипт", "парсер", "aiogram", "python"]

# Допустимые ключи чатов: username / +invite_hash / id:<числовой id>
KEY_RE = re.compile(r"^(?:[a-z][a-z0-9_]{3,31}|\+[A-Za-z0-9_-]{5,64}|id:-?\d+)$")

CHAT_FIELDS = {"tg_id", "title", "username", "status", "error"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    tg_id INTEGER,
    title TEXT,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chats_tg ON chats(tg_id);
CREATE TABLE IF NOT EXISTS categories(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    match_mode TEXT NOT NULL DEFAULT 'sub'
);
CREATE TABLE IF NOT EXISTS keywords(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    word TEXT NOT NULL,
    UNIQUE(category_id, word)
);
CREATE TABLE IF NOT EXISTS stopwords(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS blacklist(
    user_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    chat_tg_id INTEGER,
    chat_title TEXT,
    chat_username TEXT,
    author_id INTEGER,
    author_name TEXT,
    author_username TEXT,
    category TEXT,
    keywords TEXT,
    text TEXT,
    link TEXT,
    text_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_chat ON leads(chat_tg_id);
CREATE TABLE IF NOT EXISTS lead_keys(
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lead_keys_kw ON lead_keys(keyword);
CREATE TABLE IF NOT EXISTS seen_hashes(
    hash TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_str() -> str:
    return datetime.now().strftime(TS_FMT)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn: aiosqlite.Connection | None = None
        # Счётчик изменений конфигурации: по нему монитор понимает, что пора обновить кэш
        self.version = 0
        self._seen_checks = 0

    # ---------- служебное ----------
    async def init(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            await self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
        # Стартовый пресет создаётся только при самом первом запуске
        async with self.conn.execute("SELECT value FROM settings WHERE key='initialized'") as cur:
            first_run = await cur.fetchone() is None
        if first_run:
            cur = await self.conn.execute("INSERT INTO categories(name, match_mode) VALUES(?, 'sub')", (STARTER_CATEGORY,))
            cat_id = cur.lastrowid
            await cur.close()
            for word in STARTER_KEYWORDS:
                await self.conn.execute("INSERT OR IGNORE INTO keywords(category_id, word) VALUES(?, ?)", (cat_id, word))
            await self.conn.execute("INSERT INTO settings(key, value) VALUES('initialized', '1')")
        await self.conn.execute("DELETE FROM seen_hashes WHERE ts < ?", (int(time.time()) - 80 * 3600,))
        await self.conn.commit()
        self._touch()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    def _touch(self) -> None:
        self.version += 1

    async def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _one(self, sql: str, params: tuple = ()) -> dict | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _scalar(self, sql: str, params: tuple = ()) -> int:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    async def _run(self, sql: str, params: tuple = ()) -> tuple[int | None, int]:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        result = (cur.lastrowid, cur.rowcount)
        await cur.close()
        self._touch()
        return result

    # ---------- настройки ----------
    async def all_settings(self) -> dict[str, str]:
        rows = await self._all("SELECT key, value FROM settings")
        data = dict(DEFAULT_SETTINGS)
        data.update({r["key"]: r["value"] for r in rows})
        return data

    async def get_setting(self, key: str) -> str:
        return (await self.all_settings())[key]

    async def set_setting(self, key: str, value: Any) -> None:
        await self._run("INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # ---------- чаты ----------
    async def add_chat(self, key: str, username: str | None = None, title: str | None = None,
                       status: str = "queued", tg_id: int | None = None, error: str | None = None) -> int | None:
        rid, rc = await self._run(
            "INSERT OR IGNORE INTO chats(key, tg_id, title, username, status, error, added_at) VALUES(?,?,?,?,?,?,?)",
            (key, tg_id, title, username, status, error, now_str()))
        return rid if rc > 0 else None

    async def get_chat(self, chat_id: int) -> dict | None:
        return await self._one(
            "SELECT c.*, (SELECT COUNT(*) FROM leads l WHERE c.tg_id IS NOT NULL AND l.chat_tg_id = c.tg_id) AS leads "
            "FROM chats c WHERE c.id=?", (chat_id,))

    async def get_chat_by_key(self, key: str) -> dict | None:
        return await self._one("SELECT * FROM chats WHERE key=?", (key,))

    async def get_chat_by_tg(self, tg_id: int) -> dict | None:
        return await self._one("SELECT * FROM chats WHERE tg_id=?", (tg_id,))

    async def list_chats(self, offset: int, limit: int) -> list[dict]:
        return await self._all(
            "SELECT c.*, (SELECT COUNT(*) FROM leads l WHERE c.tg_id IS NOT NULL AND l.chat_tg_id = c.tg_id) AS leads "
            "FROM chats c ORDER BY c.id LIMIT ? OFFSET ?", (limit, offset))

    async def count_chats(self, status: str | None = None) -> int:
        if status:
            return await self._scalar("SELECT COUNT(*) FROM chats WHERE status=?", (status,))
        return await self._scalar("SELECT COUNT(*) FROM chats")

    async def update_chat(self, chat_id: int, **fields: Any) -> None:
        bad = set(fields) - CHAT_FIELDS
        if bad or not fields:
            raise ValueError(f"Недопустимые поля чата: {bad}")
        sets = ", ".join(f"{k}=?" for k in fields)
        await self._run(f"UPDATE chats SET {sets} WHERE id=?", (*fields.values(), chat_id))

    async def delete_chat(self, chat_id: int) -> None:
        await self._run("DELETE FROM chats WHERE id=?", (chat_id,))

    async def queued_chat_ids(self) -> list[int]:
        rows = await self._all("SELECT id FROM chats WHERE status='queued' ORDER BY id")
        return [r["id"] for r in rows]

    async def active_chat_ids(self) -> set[int]:
        rows = await self._all("SELECT tg_id FROM chats WHERE status='active' AND tg_id IS NOT NULL")
        return {r["tg_id"] for r in rows}

    # ---------- категории и ключи ----------
    async def count_categories(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM categories")

    async def list_categories(self, offset: int, limit: int) -> list[dict]:
        return await self._all(
            "SELECT c.*, (SELECT COUNT(*) FROM keywords k WHERE k.category_id=c.id) AS n "
            "FROM categories c ORDER BY c.id LIMIT ? OFFSET ?", (limit, offset))

    async def get_category(self, cat_id: int) -> dict | None:
        return await self._one(
            "SELECT c.*, (SELECT COUNT(*) FROM keywords k WHERE k.category_id=c.id) AS n "
            "FROM categories c WHERE c.id=?", (cat_id,))

    async def add_category(self, name: str) -> int:
        rid, _ = await self._run("INSERT INTO categories(name, match_mode) VALUES(?, 'sub')", (name,))
        return int(rid)

    async def toggle_category_mode(self, cat_id: int) -> None:
        await self._run("UPDATE categories SET match_mode = CASE match_mode WHEN 'sub' THEN 'exact' ELSE 'sub' END "
                        "WHERE id=?", (cat_id,))

    async def delete_category(self, cat_id: int) -> None:
        await self._run("DELETE FROM categories WHERE id=?", (cat_id,))

    async def add_keywords(self, cat_id: int, words: list[str]) -> int:
        added = 0
        for word in words:
            _, rc = await self._run("INSERT OR IGNORE INTO keywords(category_id, word) VALUES(?, ?)",
                                    (cat_id, word.lower()))
            added += 1 if rc > 0 else 0
        return added

    async def list_keywords(self, cat_id: int, offset: int = 0, limit: int = -1) -> list[dict]:
        return await self._all("SELECT id, word FROM keywords WHERE category_id=? ORDER BY id LIMIT ? OFFSET ?",
                               (cat_id, limit, offset))

    async def count_keywords(self, cat_id: int) -> int:
        return await self._scalar("SELECT COUNT(*) FROM keywords WHERE category_id=?", (cat_id,))

    async def count_all_keywords(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM keywords")

    async def delete_keyword(self, kw_id: int) -> None:
        await self._run("DELETE FROM keywords WHERE id=?", (kw_id,))

    async def categories_with_words(self) -> list[dict]:
        cats = await self._all("SELECT id, name, match_mode FROM categories ORDER BY id")
        for cat in cats:
            rows = await self._all("SELECT word FROM keywords WHERE category_id=? ORDER BY id", (cat["id"],))
            cat["words"] = [r["word"] for r in rows]
        return cats

    # ---------- стоп-слова ----------
    async def count_stopwords(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM stopwords")

    async def list_stopwords(self, offset: int = 0, limit: int = -1) -> list[dict]:
        return await self._all("SELECT id, word FROM stopwords ORDER BY id LIMIT ? OFFSET ?", (limit, offset))

    async def add_stopwords(self, words: list[str]) -> int:
        added = 0
        for word in words:
            _, rc = await self._run("INSERT OR IGNORE INTO stopwords(word) VALUES(?)", (word.lower(),))
            added += 1 if rc > 0 else 0
        return added

    async def delete_stopword(self, sw_id: int) -> None:
        await self._run("DELETE FROM stopwords WHERE id=?", (sw_id,))

    async def all_stopwords(self) -> list[str]:
        return [r["word"] for r in await self.list_stopwords()]

    # ---------- чёрный список авторов ----------
    async def count_blacklist(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM blacklist")

    async def list_blacklist(self, offset: int = 0, limit: int = -1) -> list[dict]:
        return await self._all("SELECT user_id FROM blacklist ORDER BY added_at, user_id LIMIT ? OFFSET ?",
                               (limit, offset))

    async def add_blacklist(self, user_ids: list[int]) -> int:
        added = 0
        for uid in user_ids:
            _, rc = await self._run("INSERT OR IGNORE INTO blacklist(user_id, added_at) VALUES(?, ?)",
                                    (uid, now_str()))
            added += 1 if rc > 0 else 0
        return added

    async def remove_blacklist(self, user_id: int) -> None:
        await self._run("DELETE FROM blacklist WHERE user_id=?", (user_id,))

    async def blacklist_ids(self) -> set[int]:
        return {r["user_id"] for r in await self.list_blacklist()}

    # ---------- лиды ----------
    async def add_lead(self, *, chat_tg_id: int | None, chat_title: str, chat_username: str | None,
                       author_id: int | None, author_name: str, author_username: str | None,
                       category: str, keywords: list[str], text: str, link: str | None, text_hash: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO leads(created_at, chat_tg_id, chat_title, chat_username, author_id, author_name, "
            "author_username, category, keywords, text, link, text_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_str(), chat_tg_id, chat_title, chat_username, author_id, author_name, author_username,
             category, ", ".join(keywords), text, link, text_hash))
        lead_id = cur.lastrowid
        await cur.close()
        await self.conn.executemany("INSERT INTO lead_keys(lead_id, keyword) VALUES(?, ?)",
                                    [(lead_id, k) for k in keywords])
        await self.conn.commit()
        return int(lead_id)

    async def leads_total(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM leads")

    async def leads_since(self, since: datetime) -> int:
        return await self._scalar("SELECT COUNT(*) FROM leads WHERE created_at >= ?", (since.strftime(TS_FMT),))

    async def top_chats(self, limit: int = 5) -> list[dict]:
        return await self._all(
            "SELECT MAX(chat_title) AS title, COUNT(*) AS n FROM leads "
            "GROUP BY chat_tg_id ORDER BY n DESC LIMIT ?", (limit,))

    async def top_keys(self, limit: int = 5) -> list[dict]:
        return await self._all("SELECT keyword, COUNT(*) AS n FROM lead_keys GROUP BY keyword "
                               "ORDER BY n DESC LIMIT ?", (limit,))

    async def all_leads(self) -> list[dict]:
        return await self._all("SELECT * FROM leads ORDER BY id DESC")

    async def clear_leads(self) -> None:
        await self.conn.execute("DELETE FROM lead_keys")
        await self.conn.execute("DELETE FROM leads")
        await self.conn.execute("DELETE FROM seen_hashes")
        await self.conn.commit()

    # ---------- антидубли ----------
    async def seen_check_and_mark(self, text_hash: str, hours: int) -> bool:
        """True — такой текст уже встречался в пределах окна (дубль). Иначе запоминает хеш и возвращает False."""
        if hours <= 0:
            return False
        now = int(time.time())
        row = await self._one("SELECT ts FROM seen_hashes WHERE hash=?", (text_hash,))
        if row and now - row["ts"] < hours * 3600:
            return True
        await self.conn.execute("INSERT INTO seen_hashes(hash, ts) VALUES(?, ?) "
                                "ON CONFLICT(hash) DO UPDATE SET ts=excluded.ts", (text_hash, now))
        self._seen_checks += 1
        if self._seen_checks % 200 == 0:
            await self.conn.execute("DELETE FROM seen_hashes WHERE ts < ?", (now - 80 * 3600,))
        await self.conn.commit()
        return False

    # ---------- бэкап / восстановление ----------
    async def export_config(self) -> dict:
        settings = await self.all_settings()
        chats = await self._all("SELECT key, username, title, tg_id, status FROM chats ORDER BY id")
        cats = await self.categories_with_words()
        return {
            "app": "TGLeadMonitor",
            "version": 1,
            "exported_at": now_str(),
            "settings": {k: settings[k] for k in BACKUP_SETTINGS},
            "chats": chats,
            "categories": [{"name": c["name"], "match_mode": c["match_mode"], "keywords": c["words"]} for c in cats],
            "stopwords": await self.all_stopwords(),
            "blacklist": sorted(await self.blacklist_ids()),
        }

    @staticmethod
    def _valid_setting(key: str, value: Any) -> str | None:
        try:
            if key == "dedup_hours" and int(value) in DEDUP_CHOICES:
                return str(int(value))
            if key == "join_delay" and int(value) in DELAY_CHOICES:
                return str(int(value))
            if key in ("min_len", "max_len") and 1 <= int(value) <= 4096:
                return str(int(value))
            if key in ("ignore_bots", "sound") and str(value) in ("0", "1"):
                return str(value)
        except (TypeError, ValueError):
            pass
        return None

    async def import_config(self, data: dict) -> dict:
        """Добавляет данные из бэкапа к текущим (ничего не удаляя). Возвращает счётчики.

        Чаты с публичной ссылкой (@username / +invite) ставятся в очередь вступления (queued_ids).
        Чаты без публичной ссылки (id:...) НЕ становятся активными: они добавляются на паузу (⏸)
        и возвращаются в check_ids — их нужно проверить через Monitor.verify_chats().
        """
        res = {"chats": 0, "keywords": 0, "categories": 0, "stopwords": 0, "blacklist": 0,
               "settings": 0, "queued_ids": [], "check_ids": []}

        for key, value in (data.get("settings") or {}).items():
            if key in BACKUP_SETTINGS:
                valid = self._valid_setting(key, value)
                if valid is not None:
                    await self.set_setting(key, valid)
                    res["settings"] += 1
        # мин/макс длина должны быть согласованы
        s = await self.all_settings()
        if int(s["min_len"]) > int(s["max_len"]):
            await self.set_setting("min_len", DEFAULT_SETTINGS["min_len"])
            await self.set_setting("max_len", DEFAULT_SETTINGS["max_len"])

        for item in data.get("chats") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", ""))
            if not KEY_RE.match(key):
                continue
            tg_id = item.get("tg_id") if isinstance(item.get("tg_id"), int) else None
            if key.startswith("id:"):
                if tg_id is None:
                    continue
                status, error = "paused", UNVERIFIED_ERROR
            else:
                status, error = "queued", None
            if await self.get_chat_by_key(key) or (tg_id is not None and await self.get_chat_by_tg(tg_id)):
                continue
            title = item.get("title") if isinstance(item.get("title"), str) else None
            username = item.get("username") if isinstance(item.get("username"), str) else None
            rid = await self.add_chat(key, username=username, title=title, status=status, tg_id=tg_id, error=error)
            if rid:
                res["chats"] += 1
                if status == "queued":
                    res["queued_ids"].append(rid)
                else:
                    res["check_ids"].append(rid)

        existing = {c["name"]: c["id"] for c in await self._all("SELECT id, name FROM categories")}
        for item in data.get("categories") or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                continue
            name = item["name"].strip()[:100]
            if name in existing:
                cat_id = existing[name]
            else:
                cat_id = await self.add_category(name)
                existing[name] = cat_id
                res["categories"] += 1
                if item.get("match_mode") == "exact":
                    await self.toggle_category_mode(cat_id)
            words = [str(w).strip() for w in item.get("keywords") or [] if str(w).strip()]
            res["keywords"] += await self.add_keywords(cat_id, [w[:100] for w in words])

        words = [str(w).strip()[:100] for w in data.get("stopwords") or [] if str(w).strip()]
        res["stopwords"] = await self.add_stopwords(words)
        ids = [i for i in data.get("blacklist") or [] if isinstance(i, int)]
        res["blacklist"] = await self.add_blacklist(ids)
        return res
