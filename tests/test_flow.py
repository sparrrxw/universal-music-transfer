"""Сквозные тесты: загрузка → поиск → перенос с подменёнными сервисами."""
import json
from datetime import datetime

import pytest

from app.providers.base import Provider, QuotaPaused


class FakeSource(Provider):
    id = "yandex"
    name = "FakeYandex"
    can_read = True
    can_write = True
    request_interval = 0

    def __init__(self):
        self.likes = []

    def is_connected(self):
        return True

    def fetch(self, key, progress=None):
        return "Мне нравится", list(self.likes)

    def search(self, kind, text):
        return []


class FakeDest(Provider):
    id = "spotify"
    name = "FakeSpotify"
    can_write = True
    supports_isrc = True
    like_batch = 2
    request_interval = 0

    def __init__(self):
        self.catalog = []
        self.calls = []
        self.fail_after = None

    def is_connected(self):
        return True

    def search(self, kind, text):
        self.throttle()
        self.calls.append(("search", kind, text))
        if self.fail_after is not None and len([c for c in self.calls if c[0] == "search"]) > self.fail_after:
            raise self.pause(3600, "квота")
        t = text.lower()
        if kind == "isrc":
            return [c for c in self.catalog if (c.get("isrc") or "").lower() == t]
        return [c for c in self.catalog if c["_key"] in t.replace("\t", " ")]

    def save_likes(self, ids):
        self.calls.append(("like", list(ids)))

    def create_playlist(self, name, description):
        self.calls.append(("create", name))
        return f"PL{len([c for c in self.calls if c[0] == 'create'])}"

    def add_to_playlist(self, pid, ids):
        self.calls.append(("add", pid, list(ids)))


TRACKS = [
    {"id": "1", "title": "Группа крови", "artists": ["Кино"], "duration_ms": 286000, "ref": "1:10"},
    {"id": "2", "title": "Bohemian Rhapsody", "artists": ["Queen"], "duration_ms": 354000, "isrc": "GBUM71029604",
     "ref": "2:20"},
    {"id": "3", "title": "Совсем неизвестная песня", "artists": ["Никто"], "duration_ms": 100000, "ref": "3:30"},
]
CATALOG = [
    {"_key": "kino", "id": "sp:kino", "name": "Gruppa krovi", "artists": ["KINO"], "album": "Gruppa krovi",
     "duration_ms": 286500},
    {"_key": "queen", "id": "sp:queen", "name": "Bohemian Rhapsody - Remastered 2011", "artists": ["Queen"],
     "album": "A Night at the Opera", "duration_ms": 354300, "isrc": "GBUM71029604"},
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app import db, providers
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_conn", None)
    src, dst = FakeSource(), FakeDest()
    src.likes = [dict(t) for t in TRACKS]
    dst.catalog = CATALOG
    monkeypatch.setattr(providers, "ALL", {"yandex": src, "spotify": dst})
    db.set_dest("spotify")
    yield db, src, dst
    db.reset_connection()


def test_full_flow(env):
    db, src, dst = env
    from app import jobs
    jobs.start_import("yandex", [("likes", "likes")], sync=True)
    assert jobs.state()["error"] is None, jobs.state()
    key = "yandex:likes"
    assert db.active() == key and db.counts(key, "spotify")["total"] == 3

    jobs.start_match("spotify", sync=True)
    assert jobs.state()["error"] is None, jobs.state()
    c = db.counts(key, "spotify")
    assert c["auto"] == 2 and c["not_found"] == 1, c
    # трек с ISRC нашёлся одним запросом по ISRC
    assert ("search", "isrc", "GBUM71029604") in dst.calls
    assert not any(k == "search" and kind != "isrc" and "Bohemian" in t for k, kind, t, *_ in
                   [c + (None,) for c in dst.calls if c[0] == "search"])

    jobs.start_transfer("spotify", True, True, "Test", sync=True)
    assert jobs.state()["error"] is None, jobs.state()
    likes = [c[1] for c in dst.calls if c[0] == "like"]
    # лайки от старого к новому: Queen (позиция 1) раньше Кино (позиция 0)
    assert likes == [["sp:queen", "sp:kino"]]
    adds = [c for c in dst.calls if c[0] == "add"]
    assert adds == [("add", "PL1", ["sp:kino", "sp:queen"])]

    # повторный перенос ничего не дублирует
    n = len(dst.calls)
    jobs.start_transfer("spotify", True, True, "Test", sync=True)
    assert len(dst.calls) == n


def test_same_service_needs_no_search(env):
    db, src, dst = env
    from app import jobs
    jobs.start_import("yandex", [("likes", "likes")], sync=True)
    jobs.start_match("yandex", sync=True)
    c = db.counts("yandex:likes", "yandex")
    assert c["auto"] == 3
    assert db.track_rows("yandex:likes", "yandex")[0]["dest_id"] == "1:10"


def test_quota_pause_and_resume(env):
    db, src, dst = env
    from app import jobs
    jobs.start_import("yandex", [("likes", "likes")], sync=True)
    dst.fail_after = 1
    jobs.start_match("spotify", sync=True)
    assert "квота" in (jobs.state()["error"] or "")
    assert dst.paused_until() is not None
    done_before = db.counts("yandex:likes", "spotify")
    assert done_before["new"] >= 1
    # квота вернулась: пауза снята, поиск продолжается с того же места, старые запросы из кэша
    dst.unpause()
    dst.fail_after = None
    first_calls = len([c for c in dst.calls if c[0] == "search"])
    jobs.start_match("spotify", sync=True)
    assert jobs.state()["error"] is None
    assert db.counts("yandex:likes", "spotify")["new"] == 0
    assert len([c for c in dst.calls if c[0] == "search"]) > first_calls


def test_sync_new_likes(env):
    db, src, dst = env
    from app import jobs
    src.likes = [dict(TRACKS[1])]
    jobs.start_import("yandex", [("likes", "likes")], sync=True)
    jobs.start_match("spotify", sync=True)
    jobs.start_transfer("spotify", True, False, None, sync=True)
    dst.calls.clear()
    src.likes = [dict(TRACKS[0]), dict(TRACKS[1])]   # новый лайк сверху
    jobs.start_sync(sync=True)
    assert jobs.state()["error"] is None, jobs.state()
    assert [c[1] for c in dst.calls if c[0] == "like"] == [["sp:kino"]]
    assert db.source("yandex:likes")["last_sync"]["new"] == 1


def test_duplicate_across_sources_reused(env):
    db, src, dst = env
    from app import jobs
    jobs.start_import("yandex", [("likes", "likes")], sync=True)
    jobs.start_match("spotify", sync=True)
    searches = len([c for c in dst.calls if c[0] == "search"])
    jobs.import_csv("list.csv", "Track name,Artist name\nГруппа крови,Кино\n".encode())
    jobs.start_match("spotify", sync=True)
    assert len([c for c in dst.calls if c[0] == "search"]) == searches
    rows = db.track_rows(db.active(), "spotify")
    assert rows[0]["status"] == "auto" and rows[0]["dest_id"] == "sp:kino"


def test_web_review_and_csv(env):
    db, src, dst = env
    from fastapi.testclient import TestClient
    from app import jobs
    from app.main import app
    jobs.start_import("yandex", [("likes", "likes")], sync=True)
    jobs.start_match("spotify", sync=True)
    c = TestClient(app)
    assert c.get("/").status_code == 200
    r = c.get("/review?status=not_found")
    assert "Совсем неизвестная" in r.text
    c.post("/review/3", data={"action": "pick", "uri": "", "link": "sp:manual", "status": "not_found"})
    # FakeDest не понимает ссылки → ошибка, статус не меняется
    assert db.counts("yandex:likes", "spotify")["not_found"] == 1
    c.post("/review/3", data={"action": "skip", "status": "not_found"})
    assert db.counts("yandex:likes", "spotify")["skipped"] == 1
    nf = c.get("/not_found.csv").text
    assert "Совсем неизвестная песня" in nf and "пропущен" in nf
    assert "Bohemian" in c.get("/export.csv").text
