"""Яндекс Музыка через неофициальную библиотеку yandex-music (токен из адресной строки)."""
from __future__ import annotations

import json
import re

from .. import db  # noqa: F401  (для единообразия с другими сервисами)
from .base import NotConnected, Provider, ProviderError, split_fielded

# Публичный client_id официального приложения Яндекс Музыки: после входа токен будет в адресной строке.
OAUTH_URL = "https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd418abb4b39c32c41195d"
_DOMAIN = r"(?:https?://)?(?:www\.)?music\.yandex\.(?:ru|com|by|kz|uz|com\.tr)"


def parse_token(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"access_token=([^&#\s]+)", text)
    return m.group(1) if m else text


def _ytrack(tr, pos_ts=None) -> dict | None:
    if tr is None or not tr.title:
        return None
    album = tr.albums[0] if tr.albums else None
    return {
        "id": str(tr.id), "title": tr.title, "version": tr.version,
        "artists": [a.name for a in (tr.artists or []) if a.name],
        "album": album.title if album else None, "year": album.year if album else None,
        "duration_ms": tr.duration_ms, "isrc": (getattr(tr, "isrc", None) or "").strip().upper() or None,
        "added_at": pos_ts, "ref": f"{tr.id}:{album.id}" if album else str(tr.id),
    }


def _cover_url(pl) -> str | None:
    uri = getattr(getattr(pl, "cover", None), "uri", None) or getattr(pl, "og_image", None)
    if not uri:
        return None
    uri = uri.replace("%%", "400x400")
    return uri if uri.startswith("http") else "https://" + uri


class Yandex(Provider):
    id = "yandex"
    name = "Яндекс Музыка"
    color = "#ffcc00"
    can_read = True
    can_write = True
    likes_name = "Мне нравится"
    like_batch = 100
    playlist_batch = 100
    request_interval = 0.3
    link_hint = "https://music.yandex.ru/users/ЛОГИН/playlists/3"
    status_note = "Нужен токен из адресной строки (инструкция внутри)"

    def _client(self, anonymous: bool = False):
        from yandex_music import Client
        from yandex_music.utils.request import Request
        token = None if anonymous else self.cfg().get("token")
        if not token and not anonymous:
            raise NotConnected("Не указан токен Яндекс Музыки")
        request = Request(proxy_url=self.proxy()) if self.proxy() else None
        return Client(token, request=request).init()

    def connect(self, token: str, proxy: str = "") -> str:
        self.save_cfg(token=parse_token(token), proxy=proxy.strip())
        client = self._client()
        acc = client.me.account if client.me else None
        if not acc or not acc.uid:
            raise ProviderError("Яндекс не вернул данные аккаунта, токен неверный?")
        name = acc.display_name or acc.login or str(acc.uid)
        self.save_cfg(account=name, uid=acc.uid)
        return name

    def is_connected(self) -> bool:
        return bool(self.cfg().get("token"))

    # --- чтение -----------------------------------------------------------------

    def list_collections(self) -> list[dict]:
        client = self._client()
        uid = client.me.account.uid
        out = [{"key": "likes", "title": "Мне нравится", "count": None, "kind": "likes"}]
        for pl in client.users_playlists_list(user_id=uid) or []:
            if pl.kind == 3:
                continue
            out.append({"key": f"playlist:{uid}:{pl.kind}", "title": pl.title or f"Плейлист {pl.kind}",
                        "count": pl.track_count or 0, "kind": "playlist"})
        return out

    def parse_link(self, url: str) -> str | None:
        url = (url or "").strip()
        m = re.search(_DOMAIN + r"/users/([^/?#]+)/playlists/(\d+)", url)
        if m:
            return f"playlist:{m.group(1)}:{m.group(2)}"
        m = re.search(_DOMAIN + r"/users/([^/?#]+)/tracks", url)
        if m:
            return f"userlikes:{m.group(1)}"
        m = re.search(_DOMAIN + r"/playlists/([A-Za-z0-9.\-_]+)", url)
        if m:
            return f"uuid:{m.group(1)}"
        return None

    def _load(self, client, key: str) -> tuple[str, list]:
        if key == "likes" or key.startswith("userlikes:"):
            user = None if key == "likes" else key.split(":", 1)[1]
            likes = client.users_likes_tracks(user_id=user) if user else client.users_likes_tracks()
            shorts = sorted(list(likes.tracks) if likes else [], key=lambda t: t.timestamp or "", reverse=True)
            self._last_meta = {}
            return ("Мне нравится" if not user else f"Лайки {user}"), shorts
        if key.startswith("playlist:"):
            _, user, kind = key.split(":", 2)
            pl = client.users_playlists(int(kind), user_id=user)
        elif key.startswith("uuid:"):
            pl = client.playlist(key.split(":", 1)[1])
        else:
            raise ProviderError(f"Неизвестная коллекция Яндекса: {key}")
        if isinstance(pl, list):
            pl = pl[0] if pl else None
        if pl is None:
            raise ProviderError("Яндекс не вернул плейлист")
        shorts = list(pl.tracks or [])
        if not shorts and (pl.track_count or 0) > 0 and pl.uid and pl.kind is not None:
            full = client.users_playlists(pl.kind, user_id=pl.uid)
            full = full[0] if isinstance(full, list) and full else full
            shorts = list(full.tracks or []) if full else []
        title = pl.title or "Плейлист"
        if key.startswith("playlist:") and key.endswith(":3"):
            title = f"Мне нравится ({key.split(':')[1]})"
        self._last_meta = {"description": pl.description or "", "image": _cover_url(pl)}
        return title, shorts

    def _details(self, client, shorts, progress) -> list[dict]:
        if progress:
            progress(f"Найдено треков: {len(shorts)}. Загружаю подробности…")
        by_id = {str(s.id): s.track for s in shorts if getattr(s, "track", None) is not None and s.track.title}
        missing = [s.track_id for s in shorts if str(s.id) not in by_id]
        for i in range(0, len(missing), 100):
            for tr in client.tracks(missing[i:i + 100]):
                by_id[str(tr.id)] = tr
            if progress:
                progress(f"Загружено {min(i + 100, len(missing))} из {len(missing)}")
        out = []
        for s in shorts:
            t = _ytrack(by_id.get(str(s.id)), getattr(s, "timestamp", None))
            if t:
                out.append(t)
        return out

    def fetch(self, key: str, progress=None) -> tuple[str, list[dict]]:
        own = key == "likes" or (key.startswith("playlist:") and key.split(":")[1] == str(self.cfg().get("uid")))
        attempts = [False] if own else ([True, False] if self.is_connected() else [True])
        errors = []
        for anonymous in attempts:
            try:
                client = self._client(anonymous=anonymous)
                title, shorts = self._load(client, key)
                if not shorts:
                    raise ProviderError("в плейлисте нет треков или он закрыт")
                return title, self._details(client, shorts, progress), getattr(self, "_last_meta", {})
            except NotConnected:
                raise
            except Exception as e:  # noqa: BLE001
                errors.append(f"{'без токена' if anonymous else 'с токеном'}: {e}")
        raise ProviderError("Не удалось загрузить из Яндекса. " + "; ".join(errors) +
                            ". Проверьте, что плейлист открыт (публичный).")

    # --- запись -----------------------------------------------------------------

    def search(self, kind: str, text: str) -> list[dict]:
        if kind == "isrc":
            return []
        if kind == "fielded":
            artist, title = split_fielded(text)
            text = f"{artist} {title}"
        self.throttle()
        res = self._client().search(text, type_="track", nocorrect=False)
        items = (res.tracks.results if res and res.tracks else []) or []
        out = []
        for tr in items[:10]:
            t = _ytrack(tr)
            if t:
                out.append({"id": t["ref"], "name": f"{t['title']} ({t['version']})" if t["version"] else t["title"],
                            "artists": t["artists"], "album": t["album"], "duration_ms": t["duration_ms"],
                            "isrc": t["isrc"], "url": self.track_url(t["ref"])})
        return out

    def save_likes(self, ids: list[str]) -> None:
        client = self._client()
        for i in range(0, len(ids), self.like_batch):
            self.throttle(essential=True)
            client.users_likes_tracks_add([x.split(":")[0] for x in ids[i:i + self.like_batch]])

    def create_playlist(self, name: str, description: str) -> str:
        self.throttle(essential=True)
        client = self._client()
        pl = client.users_playlists_create(name, visibility="private")
        if pl is None or pl.kind is None:
            raise ProviderError("Яндекс не создал плейлист")
        if description:
            try:
                client.users_playlists_description(pl.kind, description)
            except Exception:  # noqa: BLE001  (описание — не главное)
                pass
        return str(pl.kind)

    def playlist_item_ids(self, playlist_id: str) -> set[str] | None:
        pl = self._client().users_playlists(int(playlist_id))
        pl = pl[0] if isinstance(pl, list) else pl
        return {str(t.id).split(":")[0] for t in (pl.tracks or [])} if pl else set()

    def same_key(self, track_id: str) -> str:
        return str(track_id).split(":")[0]

    def add_to_playlist(self, playlist_id: str, ids: list[str]) -> None:
        client = self._client()
        for i in range(0, len(ids), self.playlist_batch):
            self.throttle(essential=True)
            pl = client.users_playlists(int(playlist_id))
            pl = pl[0] if isinstance(pl, list) else pl
            tracks = []
            for x in ids[i:i + self.playlist_batch]:
                tid, _, aid = x.partition(":")
                tracks.append({"id": tid, "albumId": aid} if aid else {"id": tid})
            diff = json.dumps([{"op": "insert", "at": pl.track_count or 0, "tracks": tracks}])
            client.users_playlists_change(int(playlist_id), diff, revision=pl.revision or 1)

    def parse_track_ref(self, text: str) -> str | None:
        m = re.search(r"album/(\d+)/track/(\d+)", text or "")
        if m:
            return f"{m.group(2)}:{m.group(1)}"
        m = re.fullmatch(r"\s*(\d+)(?::(\d+))?\s*", text or "")
        return f"{m.group(1)}:{m.group(2)}" if m and m.group(2) else (m.group(1) if m else None)

    def track_url(self, track_id: str) -> str | None:
        tid, _, aid = (track_id or "").partition(":")
        return f"https://music.yandex.ru/album/{aid}/track/{tid}" if aid else None
