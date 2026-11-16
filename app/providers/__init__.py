"""Реестр сервисов. Чтобы добавить сервис, напишите класс-наследник Provider и добавьте его в ALL."""
from __future__ import annotations

from .base import NotConnected, Provider, ProviderError, QuotaPaused  # noqa: F401
from .csvfile import CsvFile
from .deezer import Deezer
from .spotify import Spotify
from .tidal import Tidal
from .yandex import Yandex
from .ytmusic import YouTubeMusic

ALL: dict[str, Provider] = {p.id: p for p in (Yandex(), Spotify(), YouTubeMusic(), Tidal(), Deezer(), CsvFile())}

# Сервисы, которые пока не подключены, и почему (показываются в интерфейсе и README).
PLANNED = [
    {"name": "Apple Music",
     "why": "Официальный API требует платного аккаунта Apple Developer (99 $ в год) и подписи токена "
            "своим ключом. Обходной путь: скачать CSV и загрузить его в Apple Music через TuneMyMusic."},
    {"name": "VK Музыка",
     "why": "У ВКонтакте нет открытого API для музыки: работают только неофициальные токены, а с сентября 2026 "
            "VK ещё и ввёл лимиты и платный доступ к API. Высокий риск блокировки аккаунта."},
    {"name": "SoundCloud",
     "why": "Ключ API выдают только владельцам подписки Artist Pro. Добавлю, если такой ключ у вас есть."},
    {"name": "Deezer (запись)",
     "why": "С 2025 года Deezer не выдаёт новые ключи API частным лицам. Чтение открытых плейлистов работает."},
]


def get(pid: str | None) -> Provider | None:
    return ALL.get(pid or "")


def readers() -> list[Provider]:
    return [p for p in ALL.values() if p.can_read]


def writers() -> list[Provider]:
    return [p for p in ALL.values() if p.can_write]


def provider_of(source_key: str) -> Provider | None:
    return ALL.get((source_key or "").split(":", 1)[0])


def by_link(url: str) -> tuple[Provider, str] | None:
    for p in ALL.values():
        key = p.parse_link(url)
        if key:
            return p, key
    return None
