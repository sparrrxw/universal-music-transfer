"""SQLite-хранилище: треки источников, сопоставления с приёмниками, кэш поиска, настройки."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("UMT_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
DB_PATH = DATA_DIR / "transfer.db"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

STATUSES = ("new", "auto", "review", "manual", "not_found", "skipped")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    source_key   TEXT NOT NULL,             -- откуда: "yandex:likes", "spotify:playlist:…", "csv:…"
    track_id     TEXT NOT NULL,             -- id трека в сервисе-источнике (или номер строки CSV)
    position     INTEGER NOT NULL,          -- 0 = самый верхний (самый свежий лайк)
    added_at     TEXT,
    title        TEXT NOT NULL,
    version      TEXT,
    artists      TEXT NOT NULL,             -- JSON-список
    album        TEXT,
    year         INTEGER,
    duration_ms  INTEGER,
    isrc         TEXT,
    ref          TEXT,                      -- id для записи в тот же сервис (yandex: "трек:альбом")
    PRIMARY KEY (source_key, track_id)
);
CREATE TABLE IF NOT EXISTS matches (
    source_key    TEXT NOT NULL,
    track_id      TEXT NOT NULL,
    dest          TEXT NOT NULL,            -- id сервиса-приёмника
    status        TEXT NOT NULL DEFAULT 'new',  -- new | auto | review | manual | not_found | skipped
    dest_id       TEXT,                     -- id найденного трека в приёмнике
    dest_label    TEXT,                     -- «Артист — Название»
    score         REAL,
    candidates    TEXT,                     -- JSON-список лучших кандидатов
    saved_library INTEGER NOT NULL DEFAULT 0,
    in_playlist   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_key, track_id, dest)
);
CREATE INDEX IF NOT EXISTS idx_matches_dest ON matches(dest, status);
CREATE TABLE IF NOT EXISTS search_cache (
    provider TEXT NOT NULL,
    query    TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY (provider, query)
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        return _conn


def reset_connection() -> None:
    """Для тестов: закрыть соединение, чтобы открыть базу по новому пути."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    with _lock:
        return conn().execute(sql, params)


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


# --- key/value ---------------------------------------------------------------

def get(key: str, default: Any = None) -> Any:
    rows = query("SELECT value FROM kv WHERE key=?", (key,))
    if not rows or rows[0]["value"] is None:
        return default
    return json.loads(rows[0]["value"])


def put(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def delete(key: str) -> None:
    execute("DELETE FROM kv WHERE key=?", (key,))


def settings() -> dict:
    return get("settings", {}) or {}


def update_settings(**kw) -> None:
    s = settings()
    s.update(kw)
    put("settings", s)


# --- кэш поиска --------------------------------------------------------------

def _cache_key(q: str) -> str:
    return " ".join((q or "").lower().split())


def cache_get(provider: str, q: str) -> Any | None:
    rows = query("SELECT response FROM search_cache WHERE provider=? AND query=?", (provider, _cache_key(q)))
    return json.loads(rows[0]["response"]) if rows else None


def cache_put(provider: str, q: str, response: Any) -> None:
    execute("INSERT OR REPLACE INTO search_cache(provider, query, response) VALUES(?, ?, ?)",
            (provider, _cache_key(q), json.dumps(response, ensure_ascii=False)))


# --- источники ---------------------------------------------------------------

def sources() -> dict[str, dict]:
    return get("sources", {}) or {}


def source(key: str | None = None) -> dict:
    return sources().get(key or active() or "", {})


def save_source(key: str, meta: dict) -> None:
    allsrc = sources()
    allsrc[key] = {**allsrc.get(key, {}), **meta}
    put("sources", allsrc)


def remove_source(key: str) -> None:
    allsrc = sources()
    allsrc.pop(key, None)
    put("sources", allsrc)
    execute("DELETE FROM tracks WHERE source_key=?", (key,))
    execute("DELETE FROM matches WHERE source_key=?", (key,))
    links = get("dest_links", {}) or {}
    put("dest_links", {k: v for k, v in links.items() if not k.startswith(key + "|")})
    if active() == key:
        put("active_source", next(iter(allsrc), None))


def active() -> str | None:
    key = get("active_source")
    return key if key in sources() else next(iter(sources()), None)


def set_active(key: str) -> None:
    put("active_source", key)


def dest() -> str | None:
    return get("active_dest")


def set_dest(provider_id: str) -> None:
    put("active_dest", provider_id)


# --- связь «источник → приёмник»: созданный плейлист и прошлые настройки переноса ---

def link(source_key: str, dest_id: str) -> dict:
    return (get("dest_links", {}) or {}).get(f"{source_key}|{dest_id}", {})


def save_link(source_key: str, dest_id: str, meta: dict) -> None:
    links = get("dest_links", {}) or {}
    k = f"{source_key}|{dest_id}"
    links[k] = {**links.get(k, {}), **meta}
    put("dest_links", links)


# --- статистика --------------------------------------------------------------

def counts(source_key: str | None, dest_id: str | None) -> dict[str, int]:
    """Статистика переноса источника в приёмник. source_key='*' — по всем источникам."""
    out = {s: 0 for s in STATUSES}
    out.update(total=0, matched=0, saved_library=0, in_playlist=0)
    if not source_key:
        return out
    where, params = ("", ()) if source_key == "*" else ("WHERE t.source_key=?", (source_key,))
    rows = query(
        f"""SELECT COALESCE(m.status,'new') AS st, COUNT(*) AS n,
                   SUM(COALESCE(m.saved_library,0)) AS lib, SUM(COALESCE(m.in_playlist,0)) AS pl
            FROM tracks t LEFT JOIN matches m
              ON m.source_key=t.source_key AND m.track_id=t.track_id AND m.dest=?
            {where} GROUP BY st""",
        (dest_id or "",) + params,
    )
    for r in rows:
        out[r["st"]] = r["n"]
        out["saved_library"] += r["lib"] or 0
        out["in_playlist"] += r["pl"] or 0
    out["total"] = sum(out[s] for s in STATUSES)
    out["matched"] = out["auto"] + out["manual"]
    return out


def track_rows(source_key: str, dest_id: str, statuses: tuple[str, ...] | None = None,
               order: str = "t.position", limit: int | None = None) -> list[sqlite3.Row]:
    """Треки источника вместе с их сопоставлением для приёмника."""
    sql = f"""SELECT t.*, COALESCE(m.status,'new') AS status, m.dest_id, m.dest_label, m.score, m.candidates,
                     COALESCE(m.saved_library,0) AS saved_library, COALESCE(m.in_playlist,0) AS in_playlist
              FROM tracks t LEFT JOIN matches m
                ON m.source_key=t.source_key AND m.track_id=t.track_id AND m.dest=?
              WHERE t.source_key=?"""
    params: tuple = (dest_id, source_key)
    if statuses:
        sql += f" AND COALESCE(m.status,'new') IN ({','.join('?' * len(statuses))})"
        params += tuple(statuses)
    sql += f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return query(sql, params)


def set_match(source_key: str, track_id: str, dest: str, /, **fields) -> None:
    """Создаёт или обновляет строку сопоставления."""
    execute("INSERT OR IGNORE INTO matches(source_key, track_id, dest) VALUES(?,?,?)",
            (source_key, track_id, dest))
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        execute(f"UPDATE matches SET {cols} WHERE source_key=? AND track_id=? AND dest=?",
                tuple(fields.values()) + (source_key, track_id, dest))


def upsert_track(source_key: str, pos: int, t: dict) -> None:
    execute(
        """INSERT INTO tracks(source_key, track_id, position, added_at, title, version, artists, album, year,
                              duration_ms, isrc, ref)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_key, track_id) DO UPDATE SET position=excluded.position,
               added_at=excluded.added_at, title=excluded.title, version=excluded.version,
               artists=excluded.artists, album=excluded.album, year=excluded.year,
               duration_ms=excluded.duration_ms, isrc=COALESCE(excluded.isrc, tracks.isrc),
               ref=COALESCE(excluded.ref, tracks.ref)""",
        (source_key, str(t["id"]), pos, t.get("added_at"), t["title"], t.get("version") or None,
         json.dumps([a for a in t.get("artists") or [] if a], ensure_ascii=False),
         t.get("album") or None, t.get("year"), t.get("duration_ms"),
         (t.get("isrc") or "").strip().upper() or None, t.get("ref")),
    )
