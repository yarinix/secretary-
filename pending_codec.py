"""Кодек строк отложенных пачек: один формат для SQLite и Postgres.

Оба бэкенда возвращают «сырые» строки (tuple) в одном и том же порядке
колонок, поэтому десериализация живёт здесь, а не дублируется в storage.py и
pg_storage.py. Чистый модуль: без БД, сети и глобального состояния.

В БД `parts` и `seen_ids` лежат JSON-TEXT — так схема одинакова для SQLite и
Postgres и не требует их JSONB-типов (важно для Supabase pooler).
"""

from __future__ import annotations

import json

# Порядок обязан совпадать с ORDER BY в SELECT/RETURNING обоих бэкендов.
PENDING_COLUMNS = (
    "chat_id",
    "parts",
    "due_at",
    "created_at",
    "last_at",
    "mode",
    "seen_ids",
    "notify_message_id",
    "owner_chat_id",
    "conn_id",
    "file_id",
    "file_mime",
    "claimed_at",
    "claim_token",
    "attempts",
)

SELECT_COLUMNS = ", ".join(PENDING_COLUMNS)


def encode_parts(parts: list) -> str:
    return json.dumps([str(p) for p in parts], ensure_ascii=False)


def encode_ids(ids: list) -> str:
    return json.dumps([int(x) for x in ids], separators=(",", ":"))


def _json_list(raw, cast=str) -> list:
    try:
        value = json.loads(raw) if raw else []
        if not isinstance(value, list):
            return []
        return [cast(x) for x in value]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _float(raw):
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def _int(raw):
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


def row_to_pending(row) -> dict:
    """tuple из БД -> dict с расшифрованными JSON-полями."""
    raw = dict(zip(PENDING_COLUMNS, row))
    return {
        "chat_id": int(raw["chat_id"]),
        "parts": _json_list(raw["parts"]),
        "due_at": _float(raw["due_at"]) or 0.0,
        "created_at": _float(raw["created_at"]) or 0.0,
        "last_at": _float(raw["last_at"]) or 0.0,
        "mode": str(raw["mode"] or "wait"),
        "seen_ids": _json_list(raw["seen_ids"], int),
        "notify_message_id": _int(raw["notify_message_id"]),
        "owner_chat_id": _int(raw["owner_chat_id"]) or 0,
        "conn_id": str(raw["conn_id"] or ""),
        "file_id": str(raw["file_id"]) if raw["file_id"] else None,
        "file_mime": str(raw["file_mime"]) if raw["file_mime"] else None,
        "claimed_at": _float(raw["claimed_at"]),
        "claim_token": str(raw["claim_token"]) if raw["claim_token"] else None,
        "attempts": _int(raw["attempts"]) or 0,
    }
