"""YouTube Music через неофициальную библиотеку ytmusicapi (заголовки запроса из браузера)."""
from __future__ import annotations

import json
import re

from .base import NotConnected, Provider, ProviderError, split_fielded


def _artists(item: dict) -> list[str]:
    return [a.get("name") for a in item.get("artists") or [] if a and a.get("name")]


def _dur(item: dict) -> int | None:
    sec = item.get("duration_seconds")
    if sec:
        return int(sec) * 1000
    d = item.get("duration") or ""
    if re.fullmatch(r"\d+(:\d+){1,2}", d):
        total = 0
        for part in d.split(":"):
            total = total * 60 + int(part)
        return total * 1000
    return None


class YouTubeMusic(Provider):
    id = "ytmusic"
    name = "YouTube Music"
    color = "#ff0033"
    can_read = True
    can_write = True
    likes_name = "Понравившиеся"
    like_batch = 1          # лайк ставится по одному треку
    playlist_batch = 50
    request_interval = 0.6
    link_hint = "https://music.youtube.com/playlist?list=PL…"
    status_note = "Вход через заголовки запроса из Brave (инструкция внутри)"

    def _api(self):
        from ytmusicapi import YTMusic
        headers = self.cfg().get("headers")
        if not headers:
            raise NotConnected("YouTube Music не подключён")
        proxies = {"http": self.proxy(), "https": self.proxy()} if self.proxy() else None
        return YTMusic(auth=headers if isinstance(headers, str) else json.dumps(headers), proxies=proxies)

    def connect(self, headers_raw: str, proxy: str = "") -> str:
        import ytmusicapi
        if "cookie" not in (headers_raw or "").lower():
            raise ProviderError("В скопированных заголовках нет cookie. Скопируйте заголовки запроса "
                                "к music.youtube.com целиком (см. инструкцию).")
        try:
            headers = json.loads(ytmusicapi.setup(headers_raw=headers_raw.replace("\r\n", "\n")))
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Не удалось разобрать заголовки: {e}") from e
        self.save_cfg(headers=headers, proxy=proxy.strip())
        try:
            info = self._api().get_account_info() or {}
        except Exception as e:  # noqa: BLE001
            self.save_cfg(headers=None)
            raise ProviderError(f"YouTube Music не принял вход: {e}") from e
        name = info.get("accountName") or info.get("channelHandle") or "YouTube Music"
        self.save_cfg(account=name)
        return name

    def is_connected(self) -> bool:
        return bool(self.cfg().get("headers"))

    # --- чтение -----------------------------------------------------------------

    def list_collections(self) -> list[dict]:
        out = [{"key": "likes", "title": "Понравившиеся", "count": None, "kind": "likes"}]
        for pl in self._api().get_library_playlists(limit=None) or []:
            pid = pl.get("playlistId")
            if not pid or pid in ("LM", "SE"):
                continue
            cnt = pl.get("count")
            out.append({"key": f"playlist:{pid}", "title": pl.get("title") or pid,
                        "count": int(re.sub(r"\D", "", str(cnt)) or 0) if cnt else None, "kind": "playlist"})
        return out

    def parse_link(self, url: str) -> str | None:
        m = re.search(r"(?:music\.)?youtube\.com/.*[?&]list=([A-Za-z0-9_\-]+)", url or "")
        return f"playlist:{m.group(1)}" if m else None

    @staticmethod
    def _track(it: dict) -> dict | None:
        vid = it.get("videoId")
        if not vid or not it.get("title"):
            return None
        return {"id": vid, "title": it["title"], "artists": _artists(it),
                "album": (it.get("album") or {}).get("name"), "duration_ms": _dur(it), "ref": vid}

    def fetch(self, key: str, progress=None) -> tuple[str, list[dict]]:
        api = self._api()
        if progress:
            progress("Загружаю список из YouTube Music…")
        if key == "likes":
            data = api.get_liked_songs(limit=100000) or {}
            title = "Понравившиеся"
        elif key.startswith("playlist:"):
            data = api.get_playlist(key.split(":", 1)[1], limit=None) or {}
            title = data.get("title") or "Плейлист"
        else:
            raise ProviderError(f"Неизвестная коллекция YouTube Music: {key}")
        return title, [t for t in map(self._track, data.get("tracks") or []) if t]

    # --- запись -----------------------------------------------------------------

    def search(self, kind: str, text: str) -> list[dict]:
        if kind == "isrc":
            return []
        if kind == "fielded":
            artist, title = split_fielded(text)
            text = f"{artist} {title}"
        self.throttle()
        out = []
        for it in (self._api().search(text, filter="songs", limit=10) or [])[:10]:
            if not it.get("videoId"):
                continue
            out.append({"id": it["videoId"], "name": it.get("title"), "artists": _artists(it),
                        "album": (it.get("album") or {}).get("name"), "duration_ms": _dur(it), "isrc": None,
                        "url": self.track_url(it["videoId"])})
        return out

    def save_likes(self, ids: list[str]) -> None:
        from ytmusicapi.models.content.enums import LikeStatus
        api = self._api()
        for vid in ids:
            self.throttle(essential=True)
            api.rate_song(vid, LikeStatus.LIKE)

    def create_playlist(self, name: str, description: str) -> str:
        self.throttle(essential=True)
        res = self._api().create_playlist(name, description or "", privacy_status="PRIVATE")
        if not isinstance(res, str):
            raise ProviderError(f"YouTube Music не создал плейлист: {str(res)[:200]}")
        return res

    def add_to_playlist(self, playlist_id: str, ids: list[str]) -> None:
        api = self._api()
        for i in range(0, len(ids), self.playlist_batch):
            self.throttle(essential=True)
            res = api.add_playlist_items(playlist_id, ids[i:i + self.playlist_batch], duplicates=False)
            if isinstance(res, dict) and res.get("status") not in (None, "STATUS_SUCCEEDED"):
                raise ProviderError(f"YouTube Music не добавил треки: {str(res)[:200]}")

    def parse_track_ref(self, text: str) -> str | None:
        m = re.search(r"[?&]v=([A-Za-z0-9_\-]{11})", text or "") or re.search(r"youtu\.be/([A-Za-z0-9_\-]{11})", text or "")
        if m:
            return m.group(1)
        return text.strip() if re.fullmatch(r"\s*[A-Za-z0-9_\-]{11}\s*", text or "") else None

    def track_url(self, track_id: str) -> str | None:
        return f"https://music.youtube.com/watch?v={track_id}" if track_id else None
