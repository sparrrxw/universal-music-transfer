"""Общий интерфейс сервиса: источник (откуда читаем треки) и/или приёмник (куда пишем).

Трек источника — словарь:
    {"id", "title", "version", "artists": [..], "album", "year", "duration_ms", "isrc", "added_at", "ref"}
Кандидат поиска в приёмнике — словарь:
    {"id", "name", "artists": [..], "album", "duration_ms", "isrc", "url"}
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable

from .. import db

Progress = Callable[[str], None] | None


class ProviderError(Exception):
    pass


class NotConnected(ProviderError):
    pass


class QuotaPaused(ProviderError):
    """Сервис ограничил запросы надолго: задачу ставим на паузу, потом продолжаем с того же места."""

    def __init__(self, provider: str, until: datetime, reason: str):
        super().__init__(reason)
        self.provider = provider
        self.until = until
        self.reason = reason


class Provider:
    id: str = ""
    name: str = ""
    color: str = "#888"
    can_read = False          # можно брать треки
    can_write = False         # можно переносить сюда
    can_write_likes = True    # есть «лайки»/«избранное»
    likes_name = "Избранное"  # как называется коллекция лайков в этом сервисе
    supports_isrc = False     # поиск по ISRC
    like_batch = 50
    playlist_batch = 100
    request_interval = 0.3    # пауза между запросами по умолчанию, сек
    daily_budget: int | None = None  # свой дневной лимит запросов поиска (None — без лимита)
    link_hint = ""            # пример ссылки на плейлист для импорта
    status_note = ""          # короткое пояснение в списке сервисов

    # --- настройки и подключение ------------------------------------------------

    def cfg(self) -> dict:
        return db.get(f"cfg:{self.id}", {}) or {}

    def save_cfg(self, **kw) -> None:
        c = self.cfg()
        c.update(kw)
        db.put(f"cfg:{self.id}", c)

    def is_connected(self) -> bool:
        return False

    def account(self) -> str | None:
        return self.cfg().get("account")

    def disconnect(self) -> None:
        db.delete(f"cfg:{self.id}")

    def proxy(self) -> str | None:
        return (self.cfg().get("proxy") or "").strip() or None

    # --- чтение -----------------------------------------------------------------

    def list_collections(self) -> list[dict]:
        """Лайки и плейлисты аккаунта: [{"key", "title", "count", "kind": "likes"|"playlist"}]."""
        return []

    def fetch(self, key: str, progress: Progress = None) -> tuple[str, list[dict]]:
        """Загружает коллекцию (ключ из list_collections или parse_link). Возвращает (название, треки)."""
        raise ProviderError(f"{self.name}: чтение не поддерживается")

    def parse_link(self, url: str) -> str | None:
        """Ссылка на плейлист этого сервиса → ключ коллекции, иначе None."""
        return None

    # --- запись -----------------------------------------------------------------

    def search(self, kind: str, text: str) -> list[dict]:
        """kind: "isrc" (text = ISRC), "fielded" (text = "артист\\tназвание"), "free"."""
        raise ProviderError(f"{self.name}: поиск не поддерживается")

    def save_likes(self, ids: list[str]) -> None:
        raise ProviderError(f"{self.name}: запись не поддерживается")

    def create_playlist(self, name: str, description: str) -> str:
        raise ProviderError(f"{self.name}: запись не поддерживается")

    def add_to_playlist(self, playlist_id: str, ids: list[str]) -> None:
        raise ProviderError(f"{self.name}: запись не поддерживается")

    def parse_track_ref(self, text: str) -> str | None:
        """Ссылка на трек в этом сервисе (для ручного выбора) → id трека."""
        return None

    def track_url(self, track_id: str) -> str | None:
        return None

    # --- учёт запросов ----------------------------------------------------------

    _req_lock = threading.Lock()

    def requests_today(self) -> int:
        return int(db.get(f"requests:{self.id}:{date.today().isoformat()}", 0))

    def budget(self) -> int | None:
        v = self.cfg().get("daily_budget")
        return int(v) if v else self.daily_budget

    def paused_until(self) -> datetime | None:
        v = db.get(f"paused:{self.id}")
        if not v:
            return None
        dt = datetime.fromisoformat(v)
        if dt <= datetime.now():
            db.delete(f"paused:{self.id}")
            return None
        return dt

    def pause(self, seconds: float, reason: str) -> QuotaPaused:
        until = datetime.now() + timedelta(seconds=seconds)
        db.put(f"paused:{self.id}", until.isoformat(timespec="seconds"))
        return QuotaPaused(self.id, until, f"{self.name}: {reason}")

    def unpause(self) -> None:
        db.delete(f"paused:{self.id}")

    def throttle(self, essential: bool = False) -> None:
        """Вызывать перед каждым запросом к сервису.

        essential=True для записи (лайки/плейлист): таких запросов мало, поэтому дневной лимит и
        паузу после поиска к ним не применяем. Если сервис сам откажет, пауза поставится заново.
        """
        if not essential:
            until = self.paused_until()
            if until:
                raise QuotaPaused(self.id, until, f"{self.name} временно ограничил запросы")
            b = self.budget()
            if b and self.requests_today() >= b:
                tomorrow = datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
                raise QuotaPaused(self.id, tomorrow, f"{self.name}: достигнут дневной лимит запросов ({b})")
        interval = float(self.cfg().get("request_interval") or self.request_interval)
        with Provider._req_lock:
            last = _last.get(self.id, 0.0)
            wait = last + interval - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[self.id] = time.time()
            key = f"requests:{self.id}:{date.today().isoformat()}"
            db.put(key, int(db.get(key, 0)) + 1)


_last: dict[str, float] = {}


def split_fielded(text: str) -> tuple[str, str]:
    artist, _, title = text.partition("\t")
    return artist, title


def label(cand: dict) -> str:
    return f"{', '.join(cand.get('artists') or [])} — {cand.get('name') or ''}"
