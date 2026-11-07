"""Каскад моделей Gemini — та же логика, что в ForUs/tgbot/llm.py.

ForUs использует google-genai SDK и PostgreSQL для учёта квот. Здесь каскад
реализован поверх уже имеющегося aiohttp REST (без новых зависимостей),
а учёт запросов сведён к логам: у secretary нет PostgreSQL.

Порядок моделей и поведение сохранены:
  * gemini-3.5-flash-lite -> gemini-3.1-flash-lite;
  * 429 / quota / RESOURCE_EXHAUSTED -> сразу следующая модель, без ретраев;
  * 500/502/503/504, таймауты, обрывы связи -> до 3 попыток на той же модели;
  * пустой ответ модели = неудача, переходим дальше.
"""

import asyncio
import logging

import aiohttp

from helpers import is_quota_error, is_transient, merged

log = logging.getLogger("secretary.llm")

ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEFAULT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


def build_contents(history: list[dict], text: str, media: dict | None = None) -> list[dict]:
    """Собирает `contents` для Gemini. Медиа крепится к последней реплике user."""
    msgs = merged(history, text, "model")
    contents = []
    for i, m in enumerate(msgs):
        parts = [{"text": m["text"]}]
        if media and i == len(msgs) - 1 and m["role"] == "user":
            parts.append(
                {"inline_data": {"mime_type": media["mime_type"], "data": media["data"]}}
            )
        contents.append({"role": m["role"], "parts": parts})
    return contents


def extract_text(data: dict) -> str:
    candidates = (data or {}).get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini вернул пустой candidates (возможно, сработал safety-фильтр)")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


class GeminiCascade:
    def __init__(
        self,
        api_key: str,
        models: list[str] | None = None,
        timeout: float = 40,
        max_attempts: int = 3,
    ):
        self.api_key = (api_key or "").strip()
        self.models = [m for m in (models or DEFAULT_MODELS) if m]
        self.timeout = timeout
        self.max_attempts = max_attempts

    def describe(self) -> str:
        return ", ".join(self.models) if self.api_key else "не настроен"

    async def generate(
        self,
        system_prompt: str,
        history: list[dict],
        text: str,
        media: dict | None = None,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("нет ключа Gemini (ANTHROPIC_API_KEY / GEMINI_API_KEY)")
        if not self.models:
            raise RuntimeError("GEMINI_MODELS не содержит ни одной модели")

        contents = build_contents(history, text, media)
        last_error: BaseException | None = None

        for model in self.models:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    reply = await self._call(model, contents, system_prompt)
                    if not reply:
                        raise RuntimeError("пустой ответ модели")
                    log.info("Gemini %s: ответ получен", model)
                    return reply
                except Exception as e:
                    last_error = e
                    if is_quota_error(e):
                        log.warning("Gemini %s: квота исчерпана, пробуем следующую модель", model)
                        break
                    if is_transient(e) and attempt < self.max_attempts:
                        log.warning(
                            "Gemini %s: временный сбой (попытка %s/%s): %s",
                            model, attempt, self.max_attempts, e,
                        )
                        await asyncio.sleep(min(2 ** (attempt - 1), 4))
                        continue
                    log.warning("Gemini %s: недоступна (%s), пробуем следующую", model, e)
                    break

        raise RuntimeError(f"Все модели каскада временно недоступны: {last_error}")

    async def _call(self, model: str, contents: list[dict], system_prompt: str) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": 500},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ENDPOINT.format(model=model),
                json=payload,
                headers={"x-goog-api-key": self.api_key},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    raise RuntimeError(
                        f"Gemini {model} HTTP {response.status}: {str(data)[:300]}"
                    )
                return extract_text(data)
