"""Spotify Web API под правила 2026 года (Development Mode).

Запись: PUT /me/library (≤ 40 URI), POST /me/playlists, POST /playlists/{id}/items.
Чтение: GET /me/tracks, GET /me/playlists, GET /playlists/{id}/items (только свои плейлисты).
Поиск: GET /search, limit ≤ 10. Дневная квота → 429 QUOTA_EXCEEDED, задача ставится на паузу.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from .. import db
from .base import NotConnected, Provider, ProviderError, split_fielded

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = ("user-library-read user-library-modify playlist-read-private playlist-read-collaborative "
          "playlist-modify-private playlist-modify-public")


def _slim(t: dict) -> dict:
    return {
        "id": t.get("uri"),
        "name": t.get("name"),
        "artists": [a.get("name") for a in t.get("artists", []) if a.get("name")],
        "album": (t.get("album") or {}).get("name"),
        "duration_ms": t.get("duration_ms"),
        "isrc": ((t.get("external_ids") or {}).get("isrc") or "").upper() or None,
        "url": (t.get("external_urls") or {}).get("spotify"),
    }


class Spotify(Provider):
    id = "spotify"
    name = "Spotify"
    color = "#1db954"
    can_read = True
    can_write = True
    likes_name = "Любимые треки"
    supports_isrc = True
    like_batch = 40
    playlist_batch = 100
    request_interval = 0.35
    daily_budget = 1500
    link_hint = "https://open.spotify.com/playlist/…"
    status_note = "Нужен свой Client ID (бесплатно, но владельцу приложения нужен Premium)"

    # --- OAuth (Authorization Code + PKCE) -------------------------------------

    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{db.settings().get('port', 8890)}/callback/spotify"

    def login_url(self) -> str:
        client_id = self.cfg().get("client_id")
        if not client_id:
            raise NotConnected("Сначала укажите Spotify Client ID")
        verifier = secrets.token_urlsafe(64)[:128]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        db.put("spotify_pkce", {"verifier": verifier, "state": state})
        return AUTH_URL + "?" + urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": self.redirect_uri(),
            "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state,
        })

    def _http(self) -> httpx.Client:
        return httpx.Client(proxy=self.proxy(), timeout=30)

    def _store_token(self, data: dict) -> None:
        tok = self.cfg().get("token") or {}
        tok["access_token"] = data["access_token"]
        tok["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
        if data.get("refresh_token"):
            tok["refresh_token"] = data["refresh_token"]
        self.save_cfg(token=tok)

    def handle_callback(self, code: str, state: str) -> str:
        pkce = db.get("spotify_pkce") or {}
        if not pkce or pkce.get("state") != state:
            raise ProviderError("Неверный state в ответе Spotify. Попробуйте войти ещё раз.")
        with self._http() as h:
            r = h.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri(),
                "client_id": self.cfg().get("client_id"), "code_verifier": pkce["verifier"],
            })
        if r.status_code != 200:
            raise ProviderError(f"Spotify не выдал токен: {r.status_code} {r.text[:300]}")
        self._store_token(r.json())
        db.delete("spotify_pkce")
        user = self.request("GET", "/me", essential=True) or {}
        name = user.get("display_name") or user.get("id") or "Spotify"
        self.save_cfg(account=name, user_id=user.get("id"))
        return name

    def _refresh(self) -> None:
        tok = self.cfg().get("token") or {}
        if not tok.get("refresh_token"):
            raise NotConnected("Нужно войти в Spotify")
        with self._http() as h:
            r = h.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                        "client_id": self.cfg().get("client_id")})
        if r.status_code != 200:
            self.save_cfg(token=None)
            raise NotConnected("Сессия Spotify истекла, войдите заново")
        self._store_token(r.json())

    def is_connected(self) -> bool:
        return bool((self.cfg().get("token") or {}).get("refresh_token"))

    def disconnect(self) -> None:
        self.save_cfg(token=None, account=None)

    def _access_token(self) -> str:
        tok = self.cfg().get("token") or {}
        if not tok:
            raise NotConnected("Нужно войти в Spotify")
        if time.time() >= tok.get("expires_at", 0):
            self._refresh()
            tok = self.cfg().get("token")
        return tok["access_token"]

    # --- запросы ----------------------------------------------------------------

    def request(self, method: str, path: str, *, params=None, json=None, essential=False, _retry=0):
        self.throttle(essential)
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        url = path if path.startswith("http") else API + path
        with self._http() as h:
            r = h.request(method, url, params=params, json=json, headers=headers)
        if r.status_code == 401 and _retry < 1:
            self._refresh()
            return self.request(method, path, params=params, json=json, essential=essential, _retry=_retry + 1)
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "30") or 30)
            reason = ""
            try:
                reason = (r.json() or {}).get("reason", "") or ""
            except Exception:  # noqa: BLE001
                pass
            if "QUOTA" in reason.upper() or retry_after > 120:
                raise self.pause(max(retry_after, 60), "исчерпана дневная квота (QUOTA_EXCEEDED)")
            if _retry >= 5:
                raise self.pause(retry_after, "постоянно отвечает 429")
            time.sleep(retry_after + 1)
            return self.request(method, path, params=params, json=json, essential=essential, _retry=_retry + 1)
        if r.status_code >= 500 and _retry < 3:
            time.sleep(2 * (_retry + 1))
            return self.request(method, path, params=params, json=json, essential=essential, _retry=_retry + 1)
        if r.status_code == 403:
            raise ProviderError("Spotify ответил 403. Проверьте, что ваш аккаунт добавлен в User Management "
                                f"приложения и что у владельца приложения есть Premium. Ответ: {r.text[:300]}")
        if r.status_code >= 400:
            raise ProviderError(f"Spotify {method} {path}: {r.status_code} {r.text[:300]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def _paged(self, path: str, params: dict | None = None, progress=None, what="треков"):
        data = self.request("GET", path, params=params, essential=True) or {}
        items = list(data.get("items") or [])
        total = data.get("total")
        while data.get("next"):
            if progress:
                progress(f"Загружено {len(items)} из {total or '?'} {what}")
            data = self.request("GET", data["next"], essential=True) or {}
            items += data.get("items") or []
        return items

    # --- чтение -----------------------------------------------------------------

    def list_collections(self) -> list[dict]:
        out = [{"key": "likes", "title": "Любимые треки", "count": None, "kind": "likes"}]
        me = self.cfg().get("user_id")
        for pl in self._paged("/me/playlists", {"limit": 50}, what="плейлистов"):
            if not pl:
                continue
            own = (pl.get("owner") or {}).get("id") == me
            out.append({"key": f"playlist:{pl['id']}", "title": pl.get("name") or pl["id"],
                        "count": ((pl.get("items") or pl.get("tracks")) or {}).get("total"),
                        "kind": "playlist", "note": None if own else "чужой: Spotify может не отдать треки"})
        return out

    def parse_link(self, url: str) -> str | None:
        m = re.search(r"open\.spotify\.com/(?:intl-\w+/)?playlist/([A-Za-z0-9]+)", url or "") or \
            re.search(r"spotify:playlist:([A-Za-z0-9]+)", url or "")
        return f"playlist:{m.group(1)}" if m else None

    @staticmethod
    def _track(item: dict) -> dict | None:
        t = item.get("track") or item.get("item") or {}
        if not t or t.get("type", "track") != "track" or not t.get("name"):
            return None
        s = _slim(t)
        return {"id": t.get("id") or t.get("uri"), "title": s["name"], "artists": s["artists"],
                "album": s["album"], "duration_ms": s["duration_ms"], "isrc": s["isrc"],
                "added_at": item.get("added_at"), "ref": t.get("uri"),
                "year": int(((t.get("album") or {}).get("release_date") or "0")[:4] or 0) or None}

    def fetch(self, key: str, progress=None) -> tuple[str, list[dict]]:
        if key == "likes":
            items = self._paged("/me/tracks", {"limit": 50}, progress)
            return "Любимые треки", [t for t in map(self._track, items) if t]
        if key.startswith("playlist:"):
            pid = key.split(":", 1)[1]
            meta = self.request("GET", f"/playlists/{pid}", params={"fields": "name"}, essential=True) or {}
            items = self._paged(f"/playlists/{pid}/items", {"limit": 50}, progress)
            return meta.get("name") or "Плейлист", [t for t in map(self._track, items) if t]
        raise ProviderError(f"Неизвестная коллекция Spotify: {key}")

    # --- запись -----------------------------------------------------------------

    def search(self, kind: str, text: str) -> list[dict]:
        if kind == "isrc":
            q = f"isrc:{text}"
        elif kind == "fielded":
            artist, title = split_fielded(text)
            q = f'track:"{title}" artist:"{artist}"'
        else:
            q = text
        data = self.request("GET", "/search", params={"q": q, "type": "track", "limit": 10}) or {}
        return [_slim(t) for t in ((data.get("tracks") or {}).get("items") or []) if t]

    def save_likes(self, ids: list[str]) -> None:
        for i in range(0, len(ids), self.like_batch):
            self.request("PUT", "/me/library", params={"uris": ",".join(ids[i:i + self.like_batch])},
                         essential=True)

    def create_playlist(self, name: str, description: str) -> str:
        data = self.request("POST", "/me/playlists", json={"name": name, "public": False,
                                                           "description": description}, essential=True) or {}
        if not data.get("id"):
            raise ProviderError("Spotify не вернул id созданного плейлиста")
        return data["id"]

    def add_to_playlist(self, playlist_id: str, ids: list[str]) -> None:
        for i in range(0, len(ids), self.playlist_batch):
            self.request("POST", f"/playlists/{playlist_id}/items",
                         json={"uris": ids[i:i + self.playlist_batch]}, essential=True)

    def parse_track_ref(self, text: str) -> str | None:
        text = (text or "").strip()
        m = re.search(r"track[/:]([A-Za-z0-9]{22})", text)
        if m:
            return f"spotify:track:{m.group(1)}"
        if re.fullmatch(r"[A-Za-z0-9]{22}", text):
            return f"spotify:track:{text}"
        return None

    def track_url(self, track_id: str) -> str | None:
        return f"https://open.spotify.com/track/{track_id.rsplit(':', 1)[-1]}" if track_id else None
