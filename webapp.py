"""HTTP-роуты Telegram Mini App для secretary-.

Подключаются к ТЕМ ЖЕ aiohttp-приложению, что и webhook aiogram:
  GET  /             -> "ok" (health, регистрируется в main.py)
  GET  /health       -> "ok" для внешнего мониторинга (без БД и авторизации)
  POST /webhook      -> aiogram (регистрируется в main.py)
  GET  /app          -> HTML Mini App
  GET  /static/{name}-> app.css / app.js
  POST /api/state    -> текущие настройки + статус + схема
  POST /api/settings -> атомарное сохранение patch
  POST /api/test-ai  -> тестовый запрос к каскаду (опционально)

Безопасность:
  * авторизация `initData` выполняется ДО любого чтения/записи БД;
  * доступ только у OWNER_ID;
  * read-only системные значения никогда не принимаются от клиента;
  * BOT_TOKEN, ключ Gemini и initData не логируются и не уходят в JSON;
  * ответы всегда с Cache-Control: no-store, тело запроса ограничено 64 КБ.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from app_settings import (
    READONLY_KEYS,
    canonical,
    load_state,
    public_schema,
    validate_patch,
)
from miniapp_auth import AuthError, require_owner, verify_init_data

log = logging.getLogger("secretary.webapp")

WEB_DIR = Path(__file__).resolve().parent / "web"
ALLOWED_ASSETS = {"app.css", "app.js"}
MAX_BODY = 64 * 1024
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
CONTENT_TYPES = {
    "app.css": "text/css",
    "app.js": "application/javascript",
}


def _json(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers=NO_STORE)


def _fail(status: int, message: str, **extra) -> web.Response:
    body = {"ok": False, "error": message}
    body.update(extra)
    return _json(body, status)


def _mask(value: Any) -> str:
    """Последний рубеж: даже если вызывающий передал секрет целиком."""
    text = str(value or "")
    if not text:
        return "—"
    if ":" in text:  # BOT_TOKEN вида 123456789:AAxxxx
        head, tail = text.split(":", 1)
        return f"{head}:{tail[:2]}••••••••••••"
    if len(text) > 24 or all(c in "0123456789abcdef" for c in text.lower()):
        return text[:3] + "••••••••" + text[-2:]
    return text


def _safe_readonly(readonly: dict | None) -> dict:
    """Только белый список ключей и только замаскированные значения."""
    src = readonly or {}
    out: dict[str, Any] = {}
    for key in READONLY_KEYS:
        if key not in src:
            continue
        value = src[key]
        if key == "api_key_set":
            out[key] = bool(value)
        elif key in ("PORT", "OWNER_ID"):
            out[key] = value
        elif key in ("BASE_URL", "APP_URL", "DB_PATH", "STORAGE_BACKEND"):
            # STORAGE_BACKEND — готовая безопасная строка, а не DSN.
            out[key] = str(value)
        else:
            out[key] = _mask(value)
    return out


def _safe_status(provider) -> dict:
    try:
        result = provider() if callable(provider) else {}
        if inspect.isawaitable(result):
            return {}  # sync-контекст; main.py передаёт sync-функцию
        if not isinstance(result, dict):
            return {}
        # На всякий случай не отдаём ничего похожего на секреты.
        return {
            k: v for k, v in result.items()
            if not any(word in k.lower() for word in ("token", "secret", "initdata"))
        }
    except Exception as e:
        log.warning("не удалось получить статус для Mini App: %s", type(e).__name__)
        return {}


async def _body(request: web.Request) -> dict:
    ctype = (request.content_type or "").split(";")[0].strip().lower()
    if ctype and ctype != "application/json":
        raise AuthError("ожидается application/json", 415)
    if request.content_length and request.content_length > MAX_BODY:
        raise AuthError("тело запроса слишком большое", 413)
    raw = await request.content.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise AuthError("тело запроса слишком большое", 413)
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AuthError("некорректный JSON", 400)
    if not isinstance(data, dict):
        raise AuthError("ожидался объект JSON", 400)
    return data


def _authorize(request: web.Request, data: dict, bot_token: str, owner_id: int) -> dict:
    init_data = data.get("initData") or request.headers.get("X-Telegram-Init-Data") or ""
    parsed = verify_init_data(init_data, bot_token)
    require_owner(parsed["user"]["id"], owner_id)
    return parsed


def register_webapp_routes(
    app: web.Application,
    *,
    bot_token: str,
    owner_id: int,
    storage,
    defaults: dict,
    status_provider: Callable[[], dict] | None = None,
    readonly: dict | None = None,
    test_ai_provider=None,
    web_dir: Path | None = None,
) -> None:
    """Регистрирует роуты Mini App. Ничего не знает про модуль `config`."""
    base = Path(web_dir) if web_dir is not None else WEB_DIR
    safe_readonly = _safe_readonly(readonly)

    async def serve_app(_: web.Request) -> web.Response:
        path = base / "index.html"
        if not path.is_file():
            return _fail(404, "Mini App не собран: нет web/index.html")
        return web.Response(
            body=path.read_bytes(),
            content_type="text/html",
            charset="utf-8",
            headers=NO_STORE,
        )

    async def serve_static(request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        if name not in ALLOWED_ASSETS:
            return _fail(404, "нет такого ресурса")
        path = base / name
        if not path.is_file():
            return _fail(404, "нет такого ресурса")
        return web.Response(
            body=path.read_bytes(),
            content_type=CONTENT_TYPES[name],
            charset="utf-8",
            headers=NO_STORE,
        )

    async def api_state(request: web.Request) -> web.Response:
        try:
            data = await _body(request)
            _authorize(request, data, bot_token, owner_id)
        except AuthError as e:
            return _fail(e.status, e.message)
        rows = await asyncio.to_thread(storage.get_all_settings)
        state = load_state(rows, defaults)
        return _json({
            "ok": True,
            "settings": state,
            "readonly": safe_readonly,
            "status": _safe_status(status_provider),
            "schema": public_schema(defaults),
        })

    async def api_settings(request: web.Request) -> web.Response:
        try:
            data = await _body(request)
            _authorize(request, data, bot_token, owner_id)
        except AuthError as e:
            return _fail(e.status, e.message)

        patch = data.get("settings")
        errors = validate_patch(patch, defaults)
        if errors:
            return _fail(400, "Некоторые поля заполнены неверно", errors=errors)

        pairs = canonical(patch, defaults)
        if pairs:
            await asyncio.to_thread(storage.set_settings_many, pairs)
            log.info("Mini App: сохранено настроек: %s", len(pairs))
        rows = await asyncio.to_thread(storage.get_all_settings)
        return _json({
            "ok": True,
            "settings": load_state(rows, defaults),
            "schema": public_schema(defaults),
            "status": _safe_status(status_provider),
        })

    async def api_test_ai(request: web.Request) -> web.Response:
        try:
            data = await _body(request)
            _authorize(request, data, bot_token, owner_id)
        except AuthError as e:
            return _fail(e.status, e.message)
        if test_ai_provider is None:
            return _fail(503, "Тест ИИ не настроен на сервере")
        try:
            result = test_ai_provider()
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:
            # В текст ошибки может попасть ответ API — не прокидываем его клиенту.
            log.warning("тест ИИ не удался: %s", type(e).__name__)
            return _fail(502, "Каскад моделей не ответил. Проверьте ключ и список моделей.")
        return _json({"ok": True, "reply": str(result)[:1000]})

    async def health_probe(_: web.Request) -> web.Response:
        # Ни БД, ни авторизации: внешний мониторинг должен получать 200 даже
        # когда Postgres недоступен, иначе он решит, что сервис упал.
        return web.Response(text="ok", headers=NO_STORE)

    app.router.add_get("/health", health_probe)
    app.router.add_get("/app", serve_app)
    app.router.add_get("/static/{name}", serve_static)
    app.router.add_post("/api/state", api_state)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_post("/api/test-ai", api_test_ai)
