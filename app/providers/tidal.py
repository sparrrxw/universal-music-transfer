"""TIDAL через неофициальную библиотеку tidalapi: вход по ссылке (как на телевизоре), без ключей разработчика."""
from __future__ import annotations

import re
import threading
from datetime import datetime

from .base import NotConnected, Provider, ProviderError, split_fielded

_login_lock = threading.Lock()
_pending: dict = {}   # текущая попытка входа: {"url", "code", "error", "done"}


def _track(t) -> dict | None:
    if t is None or not getattr(t, "name", None):
        return None
    return {
        "id": str(t.id), "title": t.name, "version": getattr(t, "version", None),
        "artists": [a.name for a in (getattr(t, "artists", None) or []) if getattr(a, "name", None)],
        "album": getattr(getattr(t, "album", None), "name", None),
        "duration_ms": (t.duration or 0) * 1000 or None, "isrc": (getattr(t, "isrc", None) or "").upper() or None,
        "ref": str(t.id),
    }


def _cand(t) -> dict | None:
    d = _track(t)
    if not d:
        return None
    name = f"{d['title']} ({d['version']})" if d["version"] else d["title"]
    return {"id": d["id"], "name": name, "artists": d["artists"], "album": d["album"],
            "duration_ms": d["duration_ms"], "isrc": d["isrc"], "url": f"https://tidal.com/track/{d['id']}"}


class Tidal(Provider):
    id = "tidal"
    name = "TIDAL"
    color = "#00c2c7"
    can_read = True
    can_write = True
    likes_name = "Избранные треки (My Collection)"
    supports_isrc = True
    like_batch = 50
    playlist_batch = 50
    request_interval = 0.4
    link_hint = "https://tidal.com/playlist/…"
    status_note = "Вход по ссылке, нужна подписка TIDAL"

    # --- вход -------------------------------------------------------------------

    def _session(self):
        import tidalapi
        s = tidalapi.Session()
        if self.proxy():
            s.request_session.proxies.update({"http": self.proxy(), "https": self.proxy()})
        return s

    def start_login(self) -> dict:
        """Начинает вход: возвращает ссылку, которую надо открыть. Ожидание идёт в фоне."""
        with _login_lock:
            if _pending.get("url") and not _pending.get("done"):
                return dict(_pending)
            s = self._session()
            link, future = s.login_oauth()
            url = link.verification_uri_complete
            if not url.startswith("http"):
                url = "https://" + url
            _pending.clear()
            _pending.update(url=url, code=link.user_code, error=None, done=False)

        def wait():
            try:
                future.result()
                if not s.check_login():
                    raise ProviderError("TIDAL не подтвердил вход")
                self.save_cfg(token_type=s.token_type, access_token=s.access_token,
                              refresh_token=s.refresh_token,
                              expiry_time=s.expiry_time.isoformat() if s.expiry_time else None,
                              account=getattr(s.user, "username", None) or getattr(s.user, "email", None)
                              or f"id {s.user.id}")
            except Exception as e:  # noqa: BLE001
                _pending["error"] = str(e) or e.__class__.__name__
            finally:
                _pending["done"] = True

        threading.Thread(target=wait, daemon=True, name="tidal-login").start()
        return dict(_pending)

    @staticmethod
    def login_state() -> dict:
        return dict(_pending)

    def is_connected(self) -> bool:
        return bool(self.cfg().get("refresh_token") or self.cfg().get("access_token"))

    def _logged(self):
        c = self.cfg()
        if not c.get("access_token"):
            raise NotConnected("TIDAL не подключён")
        s = self._session()
        exp = datetime.fromisoformat(c["expiry_time"]) if c.get("expiry_time") else None
        ok = s.load_oauth_session(c.get("token_type") or "Bearer", c["access_token"], c.get("refresh_token"), exp)
        if not ok or not s.check_login():
            raise NotConnected("Сессия TIDAL истекла, войдите заново")
        if s.access_token != c["access_token"]:
            self.save_cfg(access_token=s.access_token,
                          expiry_time=s.expiry_time.isoformat() if s.expiry_time else None)
        return s

    # --- чтение -----------------------------------------------------------------

    def list_collections(self) -> list[dict]:
        s = self._logged()
        out = [{"key": "likes", "title": "Избранные треки", "count": None, "kind": "likes"}]
        for pl in s.user.playlists() or []:
            out.append({"key": f"playlist:{pl.id}", "title": pl.name, "count": getattr(pl, "num_tracks", None),
                        "kind": "playlist"})
        return out

    def parse_link(self, url: str) -> str | None:
        m = re.search(r"tidal\.com/(?:browse/)?playlist/([0-9a-f\-]{36})", url or "")
        return f"playlist:{m.group(1)}" if m else None

    def fetch(self, key: str, progress=None) -> tuple[str, list[dict]]:
        s = self._logged()
        if key == "likes":
            from tidalapi.types import ItemOrder, OrderDirection
            items, offset = [], 0
            while True:
                # «новые сверху», как в остальных сервисах
                chunk = s.user.favorites.tracks(limit=100, offset=offset, order=ItemOrder.Date,
                                                order_direction=OrderDirection.Descending)
                items += chunk
                if progress:
                    progress(f"Загружено {len(items)} избранных треков")
                if len(chunk) < 100:
                    break
                offset += 100
            return "Избранные треки", [t for t in map(_track, items) if t]
        if key.startswith("playlist:"):
            pl = s.playlist(key.split(":", 1)[1])
            items, offset = [], 0
            while True:
                chunk = pl.tracks(limit=100, offset=offset)
                items += chunk
                if len(chunk) < 100:
                    break
                offset += 100
            image = None
            try:
                image = pl.image(640)
            except Exception:  # noqa: BLE001  (у плейлиста может не быть картинки)
                pass
            return pl.name or "Плейлист", [t for t in map(_track, items) if t], \
                {"description": getattr(pl, "description", None) or "", "image": image}
        raise ProviderError(f"Неизвестная коллекция TIDAL: {key}")

    # --- запись -----------------------------------------------------------------

    def search(self, kind: str, text: str) -> list[dict]:
        import tidalapi
        s = self._logged()
        self.throttle()
        if kind == "isrc":
            try:
                found = s.get_tracks_by_isrc(text)
            except Exception:  # noqa: BLE001  (TIDAL отвечает 404, если ISRC неизвестен)
                found = []
        else:
            if kind == "fielded":
                artist, title = split_fielded(text)
                text = f"{artist} {title}"
            found = (s.search(text, models=[tidalapi.Track], limit=10) or {}).get("tracks") or []
        return [c for c in map(_cand, found[:10]) if c]

    def save_likes(self, ids: list[str]) -> None:
        s = self._logged()
        for i in range(0, len(ids), self.like_batch):
            self.throttle(essential=True)
            s.user.favorites.add_track(ids[i:i + self.like_batch])

    def create_playlist(self, name: str, description: str) -> str:
        self.throttle(essential=True)
        return str(self._logged().user.create_playlist(name, description or "").id)

    description_limit = 500

    def playlist_item_ids(self, playlist_id: str) -> set[str] | None:
        pl = self._logged().playlist(playlist_id)
        out, offset = set(), 0
        while True:
            chunk = pl.tracks(limit=100, offset=offset)
            out |= {str(t.id) for t in chunk}
            if len(chunk) < 100:
                return out
            offset += 100

    def add_to_playlist(self, playlist_id: str, ids: list[str]) -> None:
        pl = self._logged().playlist(playlist_id)
        for i in range(0, len(ids), self.playlist_batch):
            self.throttle(essential=True)
            pl.add(ids[i:i + self.playlist_batch], allow_duplicates=False)

    def parse_track_ref(self, text: str) -> str | None:
        m = re.search(r"tidal\.com/(?:browse/)?track/(\d+)", text or "")
        return m.group(1) if m else (text.strip() if re.fullmatch(r"\s*\d+\s*", text or "") else None)

    def track_url(self, track_id: str) -> str | None:
        return f"https://tidal.com/track/{track_id}" if track_id else None
