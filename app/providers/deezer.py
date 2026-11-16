"""Deezer: только чтение открытых плейлистов и открытого избранного через публичный API.

С 2025 года Deezer не выдаёт новые ключи API частным лицам, поэтому запись (лайки и плейлисты)
пока невозможна без неофициальных обходов. Чтение публичных данных работает без входа.
"""
from __future__ import annotations

import re

import httpx

from .base import Provider, ProviderError

API = "https://api.deezer.com"


class Deezer(Provider):
    id = "deezer"
    name = "Deezer"
    color = "#a238ff"
    can_read = True
    can_write = False
    likes_name = "Любимые треки"
    request_interval = 0.15   # публичный лимит Deezer: 50 запросов за 5 секунд
    link_hint = "https://www.deezer.com/ru/playlist/… или https://www.deezer.com/ru/profile/…"
    status_note = "Только чтение открытых плейлистов и профилей; запись закрыта Deezer"

    def is_connected(self) -> bool:
        return True   # вход не нужен

    def account(self) -> str | None:
        return "без входа"

    def _get(self, url: str, params: dict | None = None) -> dict:
        self.throttle(essential=True)
        with httpx.Client(proxy=self.proxy(), timeout=30, follow_redirects=True) as h:
            r = h.get(url if url.startswith("http") else API + url, params=params)
        try:
            data = r.json()
        except ValueError as e:
            raise ProviderError(f"Deezer ответил не JSON: {r.status_code}") from e
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise ProviderError(f"Deezer: {err.get('message') or err}. Проверьте, что плейлист/профиль открыт.")
        return data

    def _resolve_short(self, url: str) -> str:
        if re.search(r"(?:link\.deezer\.com|deezer\.page\.link)/", url):
            with httpx.Client(proxy=self.proxy(), timeout=30, follow_redirects=True) as h:
                return str(h.get(url).url)
        return url

    def parse_link(self, url: str) -> str | None:
        url = (url or "").strip()
        if re.search(r"(?:link\.deezer\.com|deezer\.page\.link)/", url):
            return f"short:{url}"
        m = re.search(r"deezer\.com/(?:\w{2}/)?playlist/(\d+)", url)
        if m:
            return f"playlist:{m.group(1)}"
        m = re.search(r"deezer\.com/(?:\w{2}/)?profile/(\d+)", url)
        if m:
            return f"user:{m.group(1)}"
        return None

    @staticmethod
    def _track(t: dict) -> dict | None:
        if not t.get("title") or t.get("type", "track") != "track":
            return None
        return {"id": str(t["id"]), "title": t.get("title_short") or t["title"],
                "version": t.get("title_version") or None,
                "artists": [(t.get("artist") or {}).get("name")] if (t.get("artist") or {}).get("name") else [],
                "album": (t.get("album") or {}).get("title"),
                "duration_ms": int(t["duration"]) * 1000 if t.get("duration") else None,
                "isrc": t.get("isrc"), "ref": str(t["id"]),
                "added_at": str(t.get("time_add")) if t.get("time_add") else None}

    def _paged(self, path: str, progress=None) -> list[dict]:
        data = self._get(path, {"limit": 100})
        items = list(data.get("data") or [])
        while data.get("next"):
            if progress:
                progress(f"Загружено {len(items)} из {data.get('total', '?')}")
            data = self._get(data["next"])
            items += data.get("data") or []
        return items

    def fetch(self, key: str, progress=None) -> tuple[str, list[dict]]:
        if key.startswith("short:"):
            real = self.parse_link(self._resolve_short(key.split(":", 1)[1]))
            if not real or real.startswith("short:"):
                raise ProviderError("Не удалось раскрыть короткую ссылку Deezer")
            key = real
        if key.startswith("playlist:"):
            pid = key.split(":", 1)[1]
            meta = self._get(f"/playlist/{pid}")
            items = self._paged(f"/playlist/{pid}/tracks", progress)
            return meta.get("title") or "Плейлист", [t for t in map(self._track, items) if t], \
                {"description": meta.get("description") or "", "image": meta.get("picture_xl") or meta.get("picture_big")}
        if key.startswith("user:"):
            uid = key.split(":", 1)[1]
            meta = self._get(f"/user/{uid}")
            items = self._paged(f"/user/{uid}/tracks", progress)
            return f"Любимые треки {meta.get('name') or uid}", [t for t in map(self._track, items) if t]
        raise ProviderError(f"Неизвестная коллекция Deezer: {key}")
