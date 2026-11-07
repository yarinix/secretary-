"""Дедупликация ``update_id`` для webhook-обновлений Telegram.

Telegram повторяет POST вебхука, если не получил 2XX, и «сдаётся после
разумного числа попыток». На Render Free сервис поднимается ≈1 минуту, поэтому
повторы вокруг холодного старта — обычное дело: один и тот же update может
прийти дважды и вызвать два автоответа / два расхода квоты Gemini.

``DedupGuard`` — чистая, потокобезопасная логика без зависимостей от aiogram:
middleware-обёртка живёт в main.py, а этот класс легко тестировать отдельно.
Состояние держится в памяти процесса (после рестарта пусто — это нормально,
Telegram не шлёт старые update_id бесконечно).
"""

from __future__ import annotations

import threading
import time
from typing import Any


class DedupGuard:
    def __init__(self, ttl: float = 3600.0, max_size: int = 5000) -> None:
        self.ttl = float(ttl)
        self.max_size = int(max_size)
        self._seen: dict[int, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, update_id: Any, *, now: float | None = None) -> bool:
        """Вернуть True, если этот update_id уже обрабатывался.

        Первый вызов запоминает id и возвращает False. ``now`` пробрасывается
        в тестах (по умолчанию ``time.monotonic()``). Некорректные id просто
        пропускаются дальше — лучше обработать лишний раз, чем потерять.
        """
        try:
            uid = int(update_id)
        except (TypeError, ValueError, OverflowError):
            return False
        t = time.monotonic() if now is None else float(now)

        with self._lock:
            if len(self._seen) >= self.max_size:
                cutoff = t - self.ttl
                self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
                if len(self._seen) >= self.max_size:  # всё ещё тесно — выкинуть старое
                    overflow = len(self._seen) - self.max_size + 1
                    for key in sorted(self._seen, key=self._seen.get)[:overflow]:
                        self._seen.pop(key, None)

            prev = self._seen.get(uid)
            if prev is not None and t - prev < self.ttl:
                return True
            self._seen[uid] = t
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)
