"""CSV-файлы: импорт списка треков из файла и выгрузка в файл.

Импорт понимает выгрузки TuneMyMusic, Soundiiz, Exportify и самодельные таблицы: колонки
ищутся по названию (Track name / Title / Название, Artist / Исполнитель, Album, ISRC, Duration).
"""
from __future__ import annotations

import csv
import hashlib
import io
import re

from .base import Provider, ProviderError

ALIASES = {
    "title": ("track name", "track", "title", "song", "song name", "name", "название", "трек", "песня"),
    "artist": ("artist name", "artist name(s)", "artist", "artists", "artist(s)", "исполнитель", "артист",
               "артисты", "исполнители"),
    "album": ("album", "album name", "album title", "альбом"),
    "isrc": ("isrc",),
    "duration": ("duration", "duration (ms)", "track duration (ms)", "length", "time", "длительность"),
    "version": ("version", "версия"),
}


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _duration_ms(v: str, header: str) -> int | None:
    v = (v or "").strip()
    if not v:
        return None
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}", v):
        total = 0
        for part in v.split(":"):
            total = total * 60 + int(part)
        return total * 1000
    if re.fullmatch(r"\d+(\.\d+)?", v):
        n = float(v)
        return int(n) if "ms" in header or n > 10000 else int(n * 1000)
    return None


def parse_csv(raw: bytes) -> list[dict]:
    text = _decode(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        raise ProviderError("Файл пустой")
    header = [h.strip().lower() for h in rows[0]]
    cols: dict[str, int] = {}
    for field, names in ALIASES.items():
        for name in names:
            if name in header:
                cols[field] = header.index(name)
                break
    if "title" not in cols:
        raise ProviderError("Не нашёл колонку с названием трека. Нужна колонка «Track name», «Title» или "
                            "«Название» (первая строка файла — заголовки).")
    out = []
    for i, row in enumerate(rows[1:]):
        def cell(field: str) -> str:
            idx = cols.get(field)
            return row[idx].strip() if idx is not None and idx < len(row) else ""
        title = cell("title")
        if not title:
            continue
        artists = [a.strip() for a in re.split(r"\s*[,;&]\s*|\s+feat\.?\s+", cell("artist")) if a.strip()]
        out.append({
            "id": str(i + 1), "title": title, "version": cell("version") or None, "artists": artists,
            "album": cell("album") or None, "isrc": cell("isrc") or None,
            "duration_ms": _duration_ms(cell("duration"), header[cols["duration"]] if "duration" in cols else ""),
        })
    if not out:
        raise ProviderError("В файле нет ни одной строки с треком")
    return out


def file_key(filename: str, raw: bytes) -> str:
    return f"file:{hashlib.sha1(raw).hexdigest()[:10]}"


EXPORT_HEADER = ["Track name", "Artist name", "Album", "ISRC", "Duration (ms)", "Playlist name"]


class CsvFile(Provider):
    id = "csv"
    name = "CSV-файл"
    color = "#64748b"
    can_read = True
    can_write = False   # выгрузка в CSV делается кнопкой «Скачать CSV», без поиска
    status_note = "Загрузка из файла и выгрузка в файл (подходит для TuneMyMusic, Soundiiz)"

    def is_connected(self) -> bool:
        return True

    def account(self) -> str | None:
        return None
