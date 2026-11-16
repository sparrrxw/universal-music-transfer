"""Фоновые задачи: загрузка из источников, поиск в приёмнике, перенос.

Одновременно работает одна задача. Всё состояние лежит в SQLite, поэтому любую задачу можно
остановить и запустить снова: сделанное не повторяется. Если сервис исчерпал квоту, задача
встаёт на паузу и потом продолжается с того же места.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime

from . import db, matcher, notify, providers
from .providers import Provider, QuotaPaused
from .providers.base import label

_state_lock = threading.Lock()
_state = {"name": None, "running": False, "message": "", "error": None, "done": 0, "total": 0,
          "finished_at": None}
_stop = threading.Event()
_phase = {"start": 0.0, "done0": 0, "total": None, "started_at": 0.0}

JOB_TITLES = {"import": "Загрузка", "match": "Поиск", "transfer": "Перенос", "sync": "Синхронизация"}


def fmt_duration(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return "меньше минуты"
    m = sec // 60
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


def eta_seconds(done: int, total: int, elapsed: float, done0: int = 0) -> float | None:
    """Оценка оставшегося времени по средней скорости текущего этапа. None — пока рано судить."""
    progressed = done - done0
    if not total or progressed < 3 or elapsed < 3 or done >= total:
        return None
    return (total - done) * elapsed / progressed


def state() -> dict:
    with _state_lock:
        st = dict(_state)
        ph = dict(_phase)
    st["eta"] = None
    if st["running"] and st["total"]:
        sec = eta_seconds(st["done"], st["total"], time.time() - ph["start"], ph["done0"])
        if sec is not None:
            st["eta"] = fmt_duration(sec)
    return st


def _set(**kw) -> None:
    with _state_lock:
        # новый этап (поменялось общее число) — скорость считаем заново
        if "total" in kw and kw["total"] != _phase["total"]:
            _phase.update(start=time.time(), done0=kw.get("done", 0), total=kw["total"])
        _state.update(kw)


def stop() -> None:
    _stop.set()


def _run(name: str, fn, sync: bool = False) -> bool:
    with _state_lock:
        if _state["running"]:
            return False
        _state.update(name=name, running=True, message="Запуск…", error=None, done=0, total=0, finished_at=None)
        _phase.update(start=time.time(), done0=0, total=0, started_at=time.time())
    _stop.clear()

    def wrapper():
        try:
            fn()
        except QuotaPaused as e:
            _set(error=f"{e.reason}. Продолжить можно после {e.until:%d.%m %H:%M}. "
                       "Просто нажмите кнопку ещё раз в это время, работа продолжится с того же места.")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _set(error=str(e) or e.__class__.__name__)
        finally:
            _set(running=False, finished_at=datetime.now().strftime("%H:%M:%S"))
            _notify_finished(name)

    if sync:
        wrapper()
    else:
        threading.Thread(target=wrapper, daemon=True, name=f"job-{name}").start()
    return True


def _notify_finished(name: str) -> None:
    """Уведомление Windows, если задача шла дольше минуты или упёрлась в лимит/ошибку."""
    st = state()
    long_job = time.time() - _phase["started_at"] >= float(db.settings().get("notify_after", 60))
    if not (long_job or st["error"]) or not db.settings().get("notifications", True):
        return
    title = JOB_TITLES.get(name, "Задача")
    if st["error"]:
        notify.send(f"{title}: остановлено", st["error"])
    else:
        notify.send(f"{title}: готово", st["message"] or "Готово")


# --- 1. Загрузка из источника --------------------------------------------------

def store_collection(source_key: str, tracks: list[dict], meta: dict) -> int:
    for pos, t in enumerate(tracks):
        db.upsert_track(source_key, pos, t)
    db.save_source(source_key, {**meta, "count": len(tracks),
                                "loaded_at": datetime.now().strftime("%d.%m.%Y %H:%M")})
    return len(tracks)


def _default_playlist_name(p: Provider, title: str) -> str:
    return f"{p.name}: {title}"


def _import_one(p: Provider, coll: str, kind: str, extra: dict | None = None, prefix: str = "") -> tuple[str, int]:
    title, tracks = p.fetch(coll, progress=lambda m: _set(message=prefix + m))
    key = f"{p.id}:{coll}"
    n = store_collection(key, tracks, {"provider": p.id, "coll": coll, "title": title, "kind": kind,
                                       **(extra or {})})
    return key, n


def start_import(provider_id: str, colls: list[tuple[str, str]], sync: bool = False) -> bool:
    """colls: [(ключ коллекции, вид: likes|playlist|link)]."""
    p = providers.get(provider_id)

    def work():
        done = []
        _set(total=len(colls), done=0)
        last = None
        for i, (coll, kind) in enumerate(colls, 1):
            if _stop.is_set():
                break
            pre = f"{p.name}, {i} из {len(colls)}: " if len(colls) > 1 else f"{p.name}: "
            last, n = _import_one(p, coll, kind, prefix=pre)
            done.append(f"«{db.source(last).get('title')}» ({n})")
            _set(done=i)
        if last:
            db.set_active(last)
        _set(message="Загружено: " + ", ".join(done) if done else "Ничего не загружено")
    return _run("import", work, sync)


def start_import_link(url: str, sync: bool = False) -> bool:
    found = providers.by_link(url)
    if not found:
        raise ValueError("Не узнаю ссылку. Подходят ссылки на плейлисты Яндекс Музыки, Spotify, "
                         "YouTube Music, TIDAL и Deezer.")
    p, coll = found

    def work():
        key, n = _import_one(p, coll, "link", {"url": url.strip()}, prefix=f"{p.name}: ")
        db.set_active(key)
        _set(message=f"Готово: из «{db.source(key).get('title')}» загружено {n} треков", done=n, total=n)
    return _run("import", work, sync)


def import_csv(filename: str, raw: bytes) -> tuple[str, int]:
    from .providers.csvfile import file_key, parse_csv
    tracks = parse_csv(raw)
    coll = file_key(filename, raw)
    key = f"csv:{coll}"
    title = filename.rsplit(".", 1)[0] or "Файл"
    n = store_collection(key, tracks, {"provider": "csv", "coll": coll, "title": title, "kind": "file"})
    db.set_active(key)
    return key, n


# --- 2. Поиск в приёмнике ------------------------------------------------------

def search_cached(p: Provider, kind: str, text: str) -> list[dict]:
    q = f"{kind}:{text}"
    cached = db.cache_get(p.id, q)
    if cached is not None:
        return cached
    res = p.search(kind, text)
    db.cache_put(p.id, q, res)
    return res


def _source(row) -> matcher.Source:
    return matcher.Source(title=row["title"], version=row["version"], artists=json.loads(row["artists"] or "[]"),
                          album=row["album"], duration_ms=row["duration_ms"], isrc=row["isrc"])


def match_one(row, p: Provider) -> None:
    # Источник и приёмник — один сервис: трек уже есть, искать не нужно.
    if row["source_key"].split(":", 1)[0] == p.id and row["ref"]:
        db.set_match(row["source_key"], row["track_id"], p.id, status="auto", dest_id=row["ref"], score=100.0,
                     dest_label=f"{', '.join(json.loads(row['artists'] or '[]'))} — {row['title']}",
                     candidates="[]")
        return
    src = _source(row)
    best: dict[str, tuple[float, dict]] = {}
    # Экономия квоты: сначала самый точный запрос, запасные — только если он не дал уверенного совпадения.
    for i, (kind, text) in enumerate(matcher.build_queries(src, p.supports_isrc)):
        for cand in search_cached(p, kind, text):
            sc = matcher.score(src, cand)
            if cand.get("id") and (cand["id"] not in best or best[cand["id"]][0] < sc):
                best[cand["id"]] = (sc, cand)
        top = max((v[0] for v in best.values()), default=0.0)
        if top >= matcher.AUTO_THRESHOLD:
            break
        if i >= 1 and top >= matcher.GOOD_ENOUGH:
            break
    ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)[:5]
    top_score = ranked[0][0] if ranked else 0.0
    status = matcher.classify(top_score)
    top = ranked[0][1] if ranked else None
    db.set_match(
        row["source_key"], row["track_id"], p.id, status=status, score=top_score,
        candidates=json.dumps([matcher.candidate_view(c, s, src) for s, c in ranked], ensure_ascii=False),
        dest_id=top["id"] if status == "auto" else None,
        dest_label=label(top) if status == "auto" else None,
    )


def _match_key(row) -> str:
    """Один и тот же трек бывает под разными id (сингл, альбом, сборник) и в разных источниках."""
    if row["isrc"]:
        return "isrc:" + row["isrc"]
    artists = json.loads(row["artists"] or "[]")
    full = f"{row['title']} ({row['version']})" if row["version"] else row["title"]
    return matcher.normalize(artists[0] if artists else "") + "|" + matcher.normalize(full)


_COPY_COLS = ("status", "score", "candidates", "dest_id", "dest_label")


def _match_rows(rows, p: Provider) -> bool:
    """Ищет переданные треки в приёмнике p. Возвращает False, если остановлено."""
    known = {}
    for r in db.query("""SELECT t.*, m.status, m.score, m.candidates, m.dest_id, m.dest_label
                         FROM matches m JOIN tracks t ON t.source_key=m.source_key AND t.track_id=m.track_id
                         WHERE m.dest=? AND m.status IN ('auto','manual','review')
                         ORDER BY m.status='review'""", (p.id,)):
        known.setdefault(_match_key(r), r)
    _set(total=len(rows), done=0, message=f"Ищу в {p.name}: 0 из {len(rows)}")
    for i, row in enumerate(rows, 1):
        if _stop.is_set():
            _set(message=f"Остановлено. Обработано {i - 1} из {len(rows)}")
            return False
        key = _match_key(row)
        k = known.get(key)
        if k is not None and (k["source_key"], k["track_id"]) != (row["source_key"], row["track_id"]) \
                and row["source_key"].split(":", 1)[0] != p.id:
            db.set_match(row["source_key"], row["track_id"], p.id, **{c: k[c] for c in _COPY_COLS})
        else:
            match_one(row, p)
            fresh = db.query("SELECT * FROM matches WHERE source_key=? AND track_id=? AND dest=?",
                             (row["source_key"], row["track_id"], p.id))[0]
            if fresh["status"] in ("auto", "manual", "review"):
                known.setdefault(key, {**dict(row), **dict(fresh)})
        _set(done=i, message=f"Ищу в {p.name}: {i} из {len(rows)} (запросов сегодня: {p.requests_today()})")
    return True


def _pending_rows(dest: str, statuses: tuple[str, ...], only_source: str | None = None):
    where = "AND t.source_key=?" if only_source else ""
    params = (dest,) + statuses + ((only_source,) if only_source else ()) + (db.active() or "",)
    return db.query(
        f"""SELECT t.* FROM tracks t LEFT JOIN matches m
              ON m.source_key=t.source_key AND m.track_id=t.track_id AND m.dest=?
            WHERE COALESCE(m.status,'new') IN ({','.join('?' * len(statuses))}) {where}
            ORDER BY t.source_key<>?, t.source_key, t.position""", params)


def start_match(dest: str, rematch_not_found: bool = False, sync: bool = False) -> bool:
    p = providers.get(dest)

    def work():
        statuses = ("new", "not_found") if rematch_not_found else ("new",)
        if not _match_rows(_pending_rows(dest, statuses), p):
            return
        c = db.counts("*", dest)
        _set(message=f"Готово (все источники → {p.name}). Автоматически: {c['auto']}, "
                     f"на проверку: {c['review']}, не найдено: {c['not_found']}")
    return _run("match", work, sync)


# --- 3. Перенос ----------------------------------------------------------------

def _mark(col: str, key: str, dest: str, chunk) -> None:
    db.execute(
        f"UPDATE matches SET {col}=1 WHERE source_key=? AND dest=? "
        f"AND track_id IN ({','.join('?' * len(chunk))})",
        (key, dest) + tuple(r["track_id"] for r in chunk))


def _transfer_source(key: str, p: Provider, to_library: bool, to_playlist: bool, playlist_name: str | None) -> bool:
    meta = db.source(key)
    title = meta.get("title") or key
    src_p = providers.provider_of(key)
    base = """SELECT m.track_id, m.dest_id FROM matches m JOIN tracks t
                ON t.source_key=m.source_key AND t.track_id=m.track_id
              WHERE m.source_key=? AND m.dest=? AND m.status IN ('auto','manual') AND m.dest_id IS NOT NULL"""
    if to_library:
        # Лайки ставим от старых к новым, чтобы в приёмнике сохранился порядок «новые сверху».
        rows = db.query(base + " AND m.saved_library=0 ORDER BY t.position DESC", (key, p.id))
        _set(total=len(rows), done=0, message=f"«{title}» → {p.likes_name}: 0 из {len(rows)}")
        step = max(1, p.like_batch)
        for i in range(0, len(rows), step):
            if _stop.is_set():
                _set(message="Остановлено")
                return False
            chunk = rows[i:i + step]
            p.save_likes([r["dest_id"] for r in chunk])
            _mark("saved_library", key, p.id, chunk)
            _set(done=i + len(chunk), message=f"«{title}» → {p.likes_name}: {i + len(chunk)} из {len(rows)}")

    if to_playlist:
        link = db.link(key, p.id)
        name = (playlist_name or link.get("playlist_name")
                or _default_playlist_name(src_p or p, title)).strip()
        pl = link.get("playlist") or {}
        if not pl.get("id") or pl.get("name") != name:
            src_name = src_p.name if src_p else "другого сервиса"
            pl = {"id": p.create_playlist(name, f"Перенесено из {src_name}"), "name": name}
            db.save_link(key, p.id, {"playlist": pl, "playlist_name": name})
            db.execute("UPDATE matches SET in_playlist=0 WHERE source_key=? AND dest=?", (key, p.id))
        rows = db.query(base + " AND m.in_playlist=0 ORDER BY t.position ASC", (key, p.id))
        _set(total=len(rows), done=0, message=f"«{title}» → плейлист «{name}»: 0 из {len(rows)}")
        for i in range(0, len(rows), p.playlist_batch):
            if _stop.is_set():
                _set(message="Остановлено")
                return False
            chunk = rows[i:i + p.playlist_batch]
            added = {r["dest_id"] for r in db.query(
                "SELECT dest_id FROM matches WHERE source_key=? AND dest=? AND in_playlist=1 AND dest_id IS NOT NULL",
                (key, p.id))}
            ids = []
            for r in chunk:
                if r["dest_id"] not in added:
                    added.add(r["dest_id"])
                    ids.append(r["dest_id"])
            if ids:
                p.add_to_playlist(pl["id"], ids)
            _mark("in_playlist", key, p.id, chunk)
            _set(done=i + len(chunk), message=f"«{title}» → плейлист «{name}»: {i + len(chunk)} из {len(rows)}")
    return True


def start_transfer(dest: str, to_library: bool, to_playlist: bool, playlist_name: str | None,
                   key: str | None = None, sync: bool = False) -> bool:
    key = key or db.active()
    p = providers.get(dest)

    def work():
        db.save_link(key, dest, {"last_transfer": {"to_library": to_library, "to_playlist": to_playlist}})
        if not _transfer_source(key, p, to_library, to_playlist, playlist_name):
            return
        c = db.counts(key, dest)
        _set(message=f"Перенос в {p.name} завершён. В «{p.likes_name}»: {c['saved_library']}, "
                     f"в плейлисте: {c['in_playlist']}")
    return _run("transfer", work, sync)


def start_transfer_all_playlists(dest: str, sync: bool = False) -> bool:
    """Каждый загруженный источник, кроме лайков, — в свой плейлист в приёмнике."""
    p = providers.get(dest)

    def work():
        keys = [k for k, m in db.sources().items() if m.get("kind") != "likes"]
        for key in keys:
            if not _match_rows(_pending_rows(dest, ("new",), key), p):
                return
            if not _transfer_source(key, p, False, True, None):
                return
        _set(message=f"Перенесено плейлистов в {p.name}: {len(keys)}")
    return _run("transfer", work, sync)


def start_sync(sync: bool = False) -> bool:
    """Новые лайки с прошлого раза: для каждого источника-«лайков» загрузить заново, найти новые
    и перенести туда же и так же, как в прошлый раз."""

    def work():
        report = []
        for key, meta in db.sources().items():
            if meta.get("kind") != "likes":
                continue
            src = providers.get(meta.get("provider"))
            if not src:
                continue
            links = {d: db.link(key, d) for d in providers.ALL}
            targets = {d: ln["last_transfer"] for d, ln in links.items() if ln.get("last_transfer")}
            if not targets:
                continue
            before = {r["track_id"] for r in db.query("SELECT track_id FROM tracks WHERE source_key=?", (key,))}
            _set(message=f"Проверяю новые лайки в {src.name}…")
            _import_one(src, meta["coll"], "likes", prefix=f"{src.name}: ")
            after = {r["track_id"] for r in db.query("SELECT track_id FROM tracks WHERE source_key=?", (key,))}
            for dest, opts in targets.items():
                p = providers.get(dest)
                if not p or not p.is_connected():
                    continue
                if not _match_rows(_pending_rows(dest, ("new",), key), p):
                    return
                if not _transfer_source(key, p, opts.get("to_library", True), opts.get("to_playlist", False), None):
                    return
                c = db.counts(key, dest)
                report.append(f"{src.name} → {p.name}: новых {len(after - before)}, на проверку {c['review']}, "
                              f"не найдено {c['not_found']}")
            db.save_source(key, {"last_sync": {"at": datetime.now().strftime("%d.%m.%Y %H:%M"),
                                               "new": len(after - before)}})
        _set(message=("Синхронизация готова. " + "; ".join(report)) if report else
             "Нечего синхронизировать: сначала один раз перенесите лайки любого сервиса.")
    return _run("sync", work, sync)
