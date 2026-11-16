"""Веб-интерфейс (FastAPI). Запуск: python -m app"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import db, jobs, matcher, providers
from .providers import ProviderError, QuotaPaused
from .providers.csvfile import EXPORT_HEADER
from .providers.yandex import OAUTH_URL as YANDEX_OAUTH_URL

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="Universal Music Transfer")


def _flash(text: str, kind: str = "ok", to: str = "/") -> RedirectResponse:
    db.put("flash", {"text": text, "kind": kind})
    return RedirectResponse(to, status_code=303)


def _back(to: str = "/") -> RedirectResponse:
    return RedirectResponse(to, status_code=303)


def _dest():
    p = providers.get(db.dest())
    if p and p.can_write:
        return p
    for w in providers.writers():
        if w.is_connected():
            return w
    return None


def _service_view(p) -> dict:
    paused = p.paused_until()
    return {"id": p.id, "name": p.name, "color": p.color, "can_read": p.can_read, "can_write": p.can_write,
            "connected": p.is_connected(), "account": p.account(), "note": p.status_note,
            "likes_name": p.likes_name, "requests_today": p.requests_today(), "budget": p.budget(),
            "paused_until": paused.strftime("%d.%m %H:%M") if paused else None}


def _ctx(request: Request, **extra) -> dict:
    flash = db.get("flash")
    if flash:
        db.delete("flash")
    dest = _dest()
    active = db.active()
    srcs = []
    for k, m in db.sources().items():
        sp = providers.get(m.get("provider"))
        srcs.append({"key": k, **m, "provider_name": sp.name if sp else m.get("provider"),
                     "counts": db.counts(k, dest.id if dest else None)})
    link = db.link(active, dest.id) if (active and dest) else {}
    src_meta = db.source(active) if active else {}
    src_p = providers.provider_of(active or "")
    default_pl = f"{src_p.name if src_p else 'Перенос'}: {src_meta.get('title', '')}" if src_meta else ""
    return {
        "request": request, "flash": flash, "job": jobs.state(),
        "services": [_service_view(p) for p in providers.ALL.values()],
        "planned": providers.PLANNED,
        "writers": [_service_view(p) for p in providers.writers()],
        "dest": _service_view(dest) if dest else None,
        "active": active, "source": src_meta or None, "sources": srcs,
        "counts": db.counts(active, dest.id if dest else None),
        "link": link, "default_playlist": link.get("playlist_name") or default_pl,
        "notifications": db.settings().get("notifications", True),
        "has_likes_sources": any(m.get("kind") == "likes" for m in db.sources().values()),
        **extra,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


@app.get("/api/status")
def api_status():
    dest = _dest()
    return JSONResponse({"job": jobs.state(), "counts": db.counts(db.active(), dest.id if dest else None)})


# --- подключение сервисов ------------------------------------------------------

@app.get("/services/{pid}", response_class=HTMLResponse)
def service_page(request: Request, pid: str):
    p = providers.get(pid)
    if not p:
        return _back()
    extra = {"p": _service_view(p), "cfg": {k: v for k, v in p.cfg().items()
                                            if k in ("client_id", "proxy", "daily_budget", "request_interval")}}
    if pid == "spotify":
        extra["redirect_uri"] = p.redirect_uri()
    if pid == "yandex":
        extra["yandex_oauth"] = YANDEX_OAUTH_URL
    if pid == "tidal":
        extra["tidal_login"] = p.login_state()
    return templates.TemplateResponse(request, f"service_{pid}.html" if pid in
                                      ("spotify", "yandex", "ytmusic", "tidal") else "service_simple.html",
                                      _ctx(request, **extra))


@app.post("/services/{pid}/disconnect")
def service_disconnect(pid: str):
    p = providers.get(pid)
    if p:
        p.disconnect()
    return _flash(f"{p.name if p else pid}: вход сброшен", to=f"/services/{pid}")


@app.post("/services/{pid}/limits")
def service_limits(pid: str, daily_budget: int = Form(0), request_interval: float = Form(0)):
    p = providers.get(pid)
    if not p:
        return _back()
    p.save_cfg(daily_budget=max(50, daily_budget) if daily_budget else None,
               request_interval=max(0.1, request_interval) if request_interval else None)
    return _flash("Лимиты сохранены", to=f"/services/{pid}")


@app.post("/services/{pid}/unpause")
def service_unpause(pid: str):
    p = providers.get(pid)
    if p:
        p.unpause()
    return _flash("Пауза снята. Если сервис всё ещё ограничивает запросы, пауза вернётся.")


@app.post("/services/yandex/connect")
def yandex_connect(token: str = Form(""), proxy: str = Form("")):
    try:
        name = providers.ALL["yandex"].connect(token, proxy)
    except Exception as e:  # noqa: BLE001
        return _flash(f"Яндекс не принял токен: {e}", "error", "/services/yandex")
    return _flash(f"Яндекс Музыка подключена: {name}")


@app.post("/services/ytmusic/connect")
def ytmusic_connect(headers: str = Form(""), proxy: str = Form("")):
    try:
        name = providers.ALL["ytmusic"].connect(headers, proxy)
    except Exception as e:  # noqa: BLE001
        return _flash(str(e), "error", "/services/ytmusic")
    return _flash(f"YouTube Music подключён: {name}")


@app.post("/services/spotify/settings")
def spotify_settings(client_id: str = Form(""), proxy: str = Form("")):
    providers.ALL["spotify"].save_cfg(client_id=client_id.strip(), proxy=proxy.strip())
    return _flash("Настройки Spotify сохранены", to="/services/spotify")


@app.get("/services/spotify/login")
def spotify_login():
    try:
        return RedirectResponse(providers.ALL["spotify"].login_url())
    except ProviderError as e:
        return _flash(str(e), "error", "/services/spotify")


@app.get("/callback/spotify")
def spotify_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return _flash(f"Spotify отказал во входе: {error}", "error", "/services/spotify")
    try:
        name = providers.ALL["spotify"].handle_callback(code, state)
    except ProviderError as e:
        return _flash(str(e), "error", "/services/spotify")
    return _flash(f"Spotify подключён: {name}")


@app.post("/services/tidal/login")
def tidal_login(proxy: str = Form("")):
    t = providers.ALL["tidal"]
    t.save_cfg(proxy=proxy.strip())
    try:
        t.start_login()
    except Exception as e:  # noqa: BLE001
        return _flash(f"TIDAL: не удалось начать вход: {e}", "error", "/services/tidal")
    return _back("/services/tidal")


@app.get("/api/tidal-login")
def tidal_login_state():
    t = providers.ALL["tidal"]
    return JSONResponse({**t.login_state(), "connected": t.is_connected(), "account": t.account()})


@app.post("/settings/notifications")
def settings_notifications(enabled: str = Form(""), action: str = Form("save")):
    from . import notify
    db.update_settings(notifications=bool(enabled))
    if action == "test":
        notify.send("Перенос музыки", "Так будет выглядеть уведомление об окончании переноса")
        return _flash("Уведомление отправлено. Если оно не появилось, проверьте, что уведомления Windows "
                      "для PowerShell не отключены (Параметры → Система → Уведомления).")
    return _flash("Настройка уведомлений сохранена")


# --- источники -----------------------------------------------------------------

@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request, pid: str = ""):
    p = providers.get(pid)
    colls, error = [], None
    if p and p.can_read and p.is_connected() and p.id not in ("csv", "deezer"):
        try:
            colls = p.list_collections()
        except Exception as e:  # noqa: BLE001
            error = f"Не удалось получить список: {e}"
    loaded = db.sources()
    for c in colls:
        c["loaded"] = f"{p.id}:{c['key']}" in loaded
    return templates.TemplateResponse(request, "add.html", _ctx(
        request, p=_service_view(p) if p else None, colls=colls, error=error,
        readers=[_service_view(r) for r in providers.readers()]))


@app.post("/add/{pid}")
async def add_collections(request: Request, pid: str):
    p = providers.get(pid)
    form = await request.form()
    keys = [str(k) for k in form.getlist("key")]
    if not p or not keys:
        return _flash("Отметьте хотя бы одну коллекцию", "error", f"/add?pid={pid}")
    colls = [(k, "likes" if k == "likes" else "playlist") for k in keys]
    return _back() if jobs.start_import(pid, colls) else _flash("Уже выполняется другая задача", "error")


@app.post("/add-link")
def add_link(url: str = Form("")):
    try:
        ok = jobs.start_import_link(url)
    except ValueError as e:
        return _flash(str(e), "error", "/add")
    return _back() if ok else _flash("Уже выполняется другая задача", "error")


@app.post("/add-csv")
async def add_csv(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        key, n = jobs.import_csv(file.filename or "file.csv", raw)
    except ProviderError as e:
        return _flash(str(e), "error", "/add?pid=csv")
    return _flash(f"Из файла загружено треков: {n}")


@app.post("/sources/select")
def source_select(key: str = Form(...)):
    if key in db.sources():
        db.set_active(key)
    return _back()


@app.post("/sources/remove")
def source_remove(key: str = Form(...), confirm: str = Form("")):
    if confirm.strip().lower() != "да":
        return _flash("Чтобы убрать источник, впишите «да»", "error")
    title = db.source(key).get("title", key)
    db.remove_source(key)
    return _flash(f"Источник «{title}» убран из списка. В сервисах ничего не удалено.")


@app.post("/dest")
def dest_select(pid: str = Form(...)):
    p = providers.get(pid)
    if p and p.can_write:
        db.set_dest(pid)
    return _back()


# --- задачи --------------------------------------------------------------------

def _busy():
    return _flash("Уже выполняется другая задача", "error")


def _need_dest():
    dest = _dest()
    if not dest or not dest.is_connected():
        return None, _flash("Сначала подключите сервис, куда переносить, и выберите его", "error")
    return dest, None


@app.post("/jobs/match")
def job_match(retry_not_found: str = Form("")):
    dest, err = _need_dest()
    if err:
        return err
    return _back() if jobs.start_match(dest.id, bool(retry_not_found)) else _busy()


@app.post("/jobs/transfer")
def job_transfer(to_library: str = Form(""), to_playlist: str = Form(""), playlist_name: str = Form("")):
    dest, err = _need_dest()
    if err:
        return err
    if not to_library and not to_playlist:
        return _flash("Выберите, куда переносить: в лайки и/или в плейлист", "error")
    ok = jobs.start_transfer(dest.id, bool(to_library), bool(to_playlist), playlist_name.strip() or None)
    return _back() if ok else _busy()


@app.post("/jobs/transfer-all")
def job_transfer_all():
    dest, err = _need_dest()
    if err:
        return err
    return _back() if jobs.start_transfer_all_playlists(dest.id) else _busy()


@app.post("/jobs/sync")
def job_sync():
    return _back() if jobs.start_sync() else _busy()


@app.post("/jobs/stop")
def job_stop():
    jobs.stop()
    return _back()


# --- проверка спорных ----------------------------------------------------------

def _track_view(r) -> dict:
    d = dict(r)
    d["artists"] = ", ".join(json.loads(r["artists"] or "[]"))
    d["candidates"] = json.loads(r["candidates"] or "[]")
    return d


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, status: str = "review"):
    if status not in {"review", "not_found", "auto", "manual", "skipped"}:
        status = "review"
    dest = _dest()
    rows = db.track_rows(db.active(), dest.id, (status,), limit=300) if (dest and db.active()) else []
    return templates.TemplateResponse(request, "review.html", _ctx(
        request, rows=[_track_view(r) for r in rows], status=status))


def _apply_everywhere(row, dest: str, **fields) -> None:
    """Решение по треку применяем и к тому же треку в других источниках, где он ещё не решён."""
    db.set_match(row["source_key"], row["track_id"], dest, **fields)
    for other in db.query(
            """SELECT t.source_key, t.track_id FROM tracks t LEFT JOIN matches m
                 ON m.source_key=t.source_key AND m.track_id=t.track_id AND m.dest=?
               WHERE t.source_key<>? AND COALESCE(m.status,'new') IN ('new','review','not_found')
                 AND ((t.isrc IS NOT NULL AND t.isrc=?) OR (t.title=? AND t.artists=?))""",
            (dest, row["source_key"], row["isrc"], row["title"], row["artists"])):
        db.set_match(other["source_key"], other["track_id"], dest, **fields)


def _row(track_id: str, dest: str):
    rows = db.query("""SELECT t.*, m.candidates FROM tracks t LEFT JOIN matches m
                         ON m.source_key=t.source_key AND m.track_id=t.track_id AND m.dest=?
                       WHERE t.source_key=? AND t.track_id=?""", (dest, db.active(), track_id))
    return rows[0] if rows else None


@app.post("/review/{track_id}")
def review_save(track_id: str, action: str = Form(...), uri: str = Form(""), link: str = Form(""),
                status: str = Form("review")):
    dest = _dest()
    row = _row(track_id, dest.id) if dest else None
    if not row:
        return _back(f"/review?status={status}")
    if action == "skip":
        _apply_everywhere(row, dest.id, status="skipped", dest_id=None, dest_label=None)
    elif action == "pick":
        chosen = dest.parse_track_ref(link) if link.strip() else uri
        if link.strip() and not chosen:
            return _flash(f"Не понимаю ссылку на трек {dest.name}", "error", f"/review?status={status}")
        if chosen:
            lbl = next((f"{c['artists']} — {c['name']}" for c in json.loads(row["candidates"] or "[]")
                        if c["id"] == chosen), None)
            _apply_everywhere(row, dest.id, status="manual", dest_id=chosen, dest_label=lbl or link.strip() or chosen,
                              saved_library=0, in_playlist=0)
    elif action == "reset":
        db.set_match(row["source_key"], row["track_id"], dest.id, status="new", dest_id=None, dest_label=None)
    return _back(f"/review?status={status}#t-{row['position']}")


@app.post("/review/{track_id}/search")
def review_search(track_id: str, q: str = Form(""), status: str = Form("review")):
    """Ручной поиск: свой запрос, результаты добавляются к вариантам трека."""
    dest = _dest()
    row = _row(track_id, dest.id) if dest else None
    if not row or not q.strip():
        return _back(f"/review?status={status}")
    to = f"/review?status={status}#t-{row['position']}"
    try:
        found = jobs.search_cached(dest, "free", q.strip())
    except QuotaPaused as e:
        return _flash(f"{e.reason}. Ручной поиск будет доступен после {e.until:%d.%m %H:%M}. "
                      "Пока можно вставить ссылку на трек.", "error", to)
    except ProviderError as e:
        return _flash(str(e), "error", to)
    src = jobs._source(row)
    merged = {c["id"]: c for c in json.loads(row["candidates"] or "[]")}
    for cand in found:
        if cand.get("id"):
            merged[cand["id"]] = matcher.candidate_view(cand, matcher.score(src, cand), src)
    ranked = sorted(merged.values(), key=lambda c: c["score"] or 0, reverse=True)[:10]
    db.set_match(row["source_key"], row["track_id"], dest.id, candidates=json.dumps(ranked, ensure_ascii=False))
    return _flash(f"Найдено вариантов: {len(found)}" if found else f"{dest.name} ничего не нашёл по этому запросу",
                  "ok" if found else "error", to)


@app.post("/review-bulk")
def review_bulk(action: str = Form(...)):
    dest = _dest()
    key = db.active()
    if not dest or not key:
        return _back("/review")
    if action == "accept_best":
        for r in db.track_rows(key, dest.id, ("review",)):
            c = json.loads(r["candidates"] or "[]")
            if c:
                db.set_match(key, r["track_id"], dest.id, status="manual", dest_id=c[0]["id"],
                             dest_label=f"{c[0]['artists']} — {c[0]['name']}")
    elif action == "skip_not_found":
        db.execute("UPDATE matches SET status='skipped' WHERE source_key=? AND dest=? AND status='not_found'",
                   (key, dest.id))
    return _back("/review")


# --- выгрузки ------------------------------------------------------------------

def _csv_response(header: list, rows: list, filename: str, delimiter: str = ",") -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter)
    w.writerow(header)
    w.writerows(rows)
    data = "﻿" + buf.getvalue()  # BOM, чтобы Excel правильно открыл кириллицу
    return StreamingResponse(iter([data.encode("utf-8")]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


def _full_title(r) -> str:
    return f"{r['title']} ({r['version']})" if r["version"] else r["title"]


@app.get("/export.csv")
def export_csv(all: int = 0):
    """Все треки источника (или всех источников) в универсальном CSV: для TuneMyMusic, Soundiiz, Apple Music."""
    allsrc = db.sources()
    keys = list(allsrc) if all else [db.active()]
    out = []
    for key in keys:
        for r in db.query("SELECT * FROM tracks WHERE source_key=? ORDER BY position", (key,)):
            out.append([_full_title(r), ", ".join(json.loads(r["artists"] or "[]")), r["album"] or "",
                        r["isrc"] or "", r["duration_ms"] or "", (allsrc.get(key) or {}).get("title", "")])
    return _csv_response(EXPORT_HEADER, out, "all_tracks.csv" if all else "tracks.csv")


@app.get("/report.csv")
def report():
    dest = _dest()
    if not dest or not db.active():
        return _back()
    out = []
    for r in db.track_rows(db.active(), dest.id):
        dur = r["duration_ms"] or 0
        out.append([r["position"] + 1, r["status"], ", ".join(json.loads(r["artists"] or "[]")), r["title"],
                    r["version"] or "", r["album"] or "", f"{dur // 60000}:{dur // 1000 % 60:02d}",
                    r["dest_id"] or "", r["dest_label"] or "", r["score"] or "",
                    "да" if r["saved_library"] else "", "да" if r["in_playlist"] else ""])
    return _csv_response(["№", "Статус", "Артисты", "Название", "Версия", "Альбом", "Длительность",
                          f"ID в {dest.name}", "Найдено", "Балл", "В лайках", "В плейлисте"],
                         out, "transfer_report.csv", delimiter=";")


STATUS_RU = {"review": "не подтверждён", "not_found": "не найден", "skipped": "пропущен"}


@app.get("/not_found.csv")
def not_found_csv(all: int = 0):
    """Ненайденные/пропущенные/неподтверждённые. Колонки подходят для импорта файлом в TuneMyMusic."""
    dest = _dest()
    if not dest:
        return _back()
    allsrc = db.sources()
    keys = list(allsrc) if all else [db.active()]
    out = []
    for key in keys:
        for r in db.track_rows(key, dest.id, ("not_found", "skipped", "review")):
            out.append([_full_title(r), ", ".join(json.loads(r["artists"] or "[]")), r["album"] or "",
                        r["isrc"] or "", (allsrc.get(key) or {}).get("title", ""),
                        STATUS_RU.get(r["status"], r["status"])])
    return _csv_response(["Track name", "Artist name", "Album", "ISRC", "Playlist name", "Статус"], out,
                         "not_found_all.csv" if all else "not_found.csv")

