# Secretary Bot

Секретарь для Telegram Business: отвечает клиентам в твоих личных чатах от твоего имени, а тебе пересылает все входящие.

## Как работает
- Пишет клиент → бот присылает тебе копию в личку и отвечает клиенту (ИИ или стандартным текстом).
- Если ты сам ответил в чате, бот на время замолкает.
- Управление в личке с ботом: `/business on|off`, `/business notify on|off`, `/business persona <текст>`, `/business status`.
- Логика перенесена из ForUs (`tgbot/business.py`): пауза при вашем вмешательстве (`BUSINESS_TAKEOVER_MINUTES`, 60), лимит ответов в час на чат (`BUSINESS_MAX_REPLIES_PER_HOUR`, 20), защита от инъекций в промпте.

## Подключение
1. @BotFather → `/newbot`; затем Bot Settings → Business Mode → включить.
2. Нужен Telegram Premium. Настройки → Telegram для бизнеса → Чат-боты → выбрать бота, оставить право отвечать на сообщения.
3. Задеплоить на Render (New → Blueprint → этот репозиторий).

## Переменные окружения
- `BOT_TOKEN` — токен от @BotFather
- `OWNER_ID` — твой Telegram ID (необязательно: определяется при подключении Business)
- `ANTHROPIC_API_KEY` — ключ для ИИ-ответов (без него бот шлёт стандартный текст)
- `MODEL` — модель Claude (по умолчанию `claude-haiku-5-5`)
- `GEMINI_API_KEY` — если нет ключа Claude, ответы идут через Gemini (`GEMINI_MODEL`, по умолчанию gemini-2.5-flash)
- `OWNER_NAME` — как зовут владельца в промпте (по умолчанию «владельца»)
- `BUSINESS_TAKEOVER_MINUTES`, `BUSINESS_MAX_REPLIES_PER_HOUR` — пауза и лимит
- `WEBHOOK_SECRET` — генерируется Render

Примечание: на бесплатном плане Render диск временный, настройки и история сбрасываются при перезапуске.
