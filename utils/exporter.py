"""Экспорт лидов (CSV / TXT) и экспорт/импорт настроек (JSON)."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

CSV_HEADER = ["Дата", "Чат", "Автор", "Юзернейм", "Категория", "Ключ", "Текст", "Ссылка"]


def _safe_cell(value: Any) -> str:
    """Защита от CSV-инъекций: ячейки, начинающиеся с = + - @, экранируются."""
    s = "" if value is None else str(value)
    if s and s[0] in "=+-@\t\r":
        s = "'" + s
    return s


def leads_to_csv(leads: list[dict]) -> bytes:
    """CSV в UTF-8-SIG, разделитель «;» — корректно открывается в русском Excel."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writerow(CSV_HEADER)
    for lead in leads:
        username = lead.get("author_username")
        writer.writerow([
            _safe_cell(lead.get("created_at")),
            _safe_cell(lead.get("chat_title")),
            _safe_cell(lead.get("author_name")),
            _safe_cell(f"@{username}" if username else ""),
            _safe_cell(lead.get("category")),
            _safe_cell(lead.get("keywords")),
            _safe_cell(lead.get("text")),
            _safe_cell(lead.get("link")),
        ])
    return buf.getvalue().encode("utf-8-sig")


def leads_to_txt(leads: list[dict]) -> bytes:
    parts: list[str] = []
    for lead in leads:
        username = lead.get("author_username")
        author = lead.get("author_name") or "—"
        if username:
            author += f" (@{username})"
        if lead.get("author_id"):
            author += f" [ID: {lead['author_id']}]"
        parts.append(
            f"=== Лид #{lead.get('id')} · {lead.get('created_at')} ===\n"
            f"Чат: {lead.get('chat_title') or '—'}\n"
            f"Автор: {author}\n"
            f"Категория: {lead.get('category') or '—'}\n"
            f"Ключ: {lead.get('keywords') or '—'}\n"
            f"Ссылка: {lead.get('link') or '—'}\n"
            f"Текст:\n{lead.get('text') or ''}\n"
        )
    return "\n".join(parts).encode("utf-8")


def config_to_json(config: dict) -> bytes:
    return json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")


def parse_config(raw: bytes) -> dict:
    """Разбирает и проверяет файл бэкапа. Бросает ValueError при некорректном формате."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Файл не является корректным JSON") from exc
    if not isinstance(data, dict) or data.get("app") != "TGLeadMonitor":
        raise ValueError("Это не файл бэкапа TGLeadMonitor")
    for field in ("chats", "categories", "stopwords", "blacklist"):
        if field in data and not isinstance(data[field], list):
            raise ValueError(f"Поле «{field}» должно быть списком")
    if "settings" in data and not isinstance(data["settings"], dict):
        raise ValueError("Поле «settings» должно быть объектом")
    return data
