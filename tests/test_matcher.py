import pytest

from app import matcher
from app.matcher import Source


def cand(name, artists, album="", dur=None, isrc=None):
    return {"id": f"id-{abs(hash(name + str(artists))) % 10**9}", "name": name, "artists": artists,
            "album": album, "duration_ms": dur, "isrc": isrc}


def test_exact_match_is_auto():
    src = Source("Bohemian Rhapsody", None, ["Queen"], "A Night at the Opera", 354000)
    c = cand("Bohemian Rhapsody - Remastered 2011", ["Queen"], "A Night At The Opera (2011 Remaster)", 355000)
    assert matcher.score(src, c) >= matcher.AUTO_THRESHOLD


def test_cyrillic_vs_latin_artist():
    src = Source("Группа крови", None, ["Кино"], "Группа крови", 286000)
    c = cand("Группа крови", ["KINO"], "Gruppa krovi", 285000)
    assert matcher.score(src, c) >= matcher.AUTO_THRESHOLD


def test_remix_vs_original_penalized():
    src = Source("Blinding Lights", None, ["The Weeknd"], "After Hours", 200000)
    orig = cand("Blinding Lights", ["The Weeknd"], "After Hours", 200040)
    remix = cand("Blinding Lights - Chromatics Remix", ["The Weeknd"], "Blinding Lights (Remixes)", 210000)
    assert matcher.score(src, orig) > matcher.score(src, remix) + 15
    assert matcher.classify(matcher.score(src, remix)) != "auto"


def test_wrong_artist_not_auto():
    src = Source("Hello", None, ["Adele"], "25", 295000)
    c = cand("Hello", ["Lionel Richie"], "Can't Slow Down", 247000)
    assert matcher.score(src, c) < matcher.AUTO_THRESHOLD


def test_missing_metadata_from_csv_still_matches():
    # в CSV часто нет альбома и длительности
    src = Source("Звезда по имени Солнце", None, ["Кино"], None, None)
    c = cand("Zvezda po imeni Solntse", ["Kino"], "Zvezda po imeni Solntse", 225000)
    assert matcher.score(src, c) >= matcher.REVIEW_THRESHOLD


def test_queries_order_and_isrc_support():
    src = Source("Звезда по имени Солнце", None, ["Кино"], None, None, isrc="SUA018800200")
    qs = matcher.build_queries(src)
    assert qs[0] == ("isrc", "SUA018800200")
    assert qs[1] == ("fielded", "Кино\tЗвезда по имени Солнце")
    assert any(k == "free" and "kino" in t and "zvezda" in t for k, t in qs)
    assert all(k != "isrc" for k, _ in matcher.build_queries(src, supports_isrc=False))


def test_isrc_match():
    src = Source("Группа крови", None, ["Кино"], "Группа крови", 286000, isrc="SUA018800100")
    c = cand("Gruppa krovi (Remastered)", ["Kino"], "Legenda", 290000, isrc="sua018800100")
    assert matcher.score(src, c) >= 97


def test_link_parsing_all_services():
    from app import providers
    cases = {
        "https://music.yandex.ru/users/friend.login/playlists/3?x=1": ("yandex", "playlist:friend.login:3"),
        "music.yandex.com/users/vasya/tracks": ("yandex", "userlikes:vasya"),
        "https://music.yandex.ru/playlists/lk.1a2b-3c4d?ref=share": ("yandex", "uuid:lk.1a2b-3c4d"),
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=1": ("spotify", "playlist:37i9dQZF1DXcBWIGoYBM5M"),
        "https://open.spotify.com/intl-de/playlist/abc123": ("spotify", "playlist:abc123"),
        "https://music.youtube.com/playlist?list=PLabc_-123": ("ytmusic", "playlist:PLabc_-123"),
        "https://tidal.com/browse/playlist/0b2a3c4d-1111-2222-3333-444455556666": (
            "tidal", "playlist:0b2a3c4d-1111-2222-3333-444455556666"),
        "https://www.deezer.com/ru/playlist/908622995": ("deezer", "playlist:908622995"),
        "https://www.deezer.com/en/profile/12345": ("deezer", "user:12345"),
    }
    for url, (pid, key) in cases.items():
        p, k = providers.by_link(url)
        assert (p.id, k) == (pid, key), url
    assert providers.by_link("https://example.com/x") is None


def test_track_refs():
    from app.providers import ALL
    tid = "4uLU6hMCjMI75M1A2tKUQC"
    assert ALL["spotify"].parse_track_ref(f"https://open.spotify.com/track/{tid}?si=abc") == f"spotify:track:{tid}"
    assert ALL["spotify"].parse_track_ref("nonsense") is None
    assert ALL["yandex"].parse_track_ref("https://music.yandex.ru/album/123/track/456") == "456:123"
    assert ALL["ytmusic"].parse_track_ref("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=x") == "dQw4w9WgXcQ"
    assert ALL["tidal"].parse_track_ref("https://tidal.com/browse/track/77646168") == "77646168"


def test_yandex_token():
    from app.providers.yandex import parse_token
    assert parse_token("https://music.yandex.ru/#access_token=y0_ABC&token_type=bearer") == "y0_ABC"


def test_csv_parsing_variants():
    from app.providers.csvfile import parse_csv
    from app.providers import ProviderError
    tmm = "Track name,Artist name,Album,Playlist name\nHello,Adele,25,x\n".encode("utf-8-sig")
    assert parse_csv(tmm)[0]["artists"] == ["Adele"]
    exportify = ('"Track URI","Track Name","Artist Name(s)","Album Name","Track Duration (ms)","ISRC"\n'
                 '"spotify:track:x","Stay","The Kid LAROI, Justin Bieber","Stay","141805","USSM12105970"\n').encode()
    t = parse_csv(exportify)[0]
    assert t["artists"] == ["The Kid LAROI", "Justin Bieber"] and t["duration_ms"] == 141805
    assert t["isrc"] == "USSM12105970"
    ru = "Название;Исполнитель;Длительность\nКукушка;Кино;6:41\n".encode("cp1251")
    t = parse_csv(ru)[0]
    assert t["title"] == "Кукушка" and t["duration_ms"] == 401000
    with pytest.raises(ProviderError):
        parse_csv(b"foo,bar\n1,2\n")


@pytest.mark.parametrize("cand_title,album,artists", [
    ("Blinding Lights - Chromatics Remix", "After Hours", ["The Weeknd"]),
    ("Blinding Lights (Live)", "After Hours", ["The Weeknd"]),
    ("Blinding Lights", "Live at SoFi Stadium", ["The Weeknd"]),
    ("Blinding Lights (Acoustic)", "After Hours", ["The Weeknd"]),
    ("Blinding Lights - Slowed + Reverb", "After Hours", ["The Weeknd"]),
    ("Blinding Lights (Karaoke Version)", "Karaoke Hits 2020", ["Sing2Music"]),
    ("Blinding Lights", "Pop Covers", ["Vitamin String Quartet"]),
    ("Blinding Lights (Cover)", "After Hours", ["The Weeknd"]),
])
def test_other_versions_never_auto_even_if_isrc_lookalike(cand_title, album, artists):
    src = Source("Blinding Lights", None, ["The Weeknd"], "After Hours", 200000)
    c = cand(cand_title, artists, album, 200000)
    sc = matcher.score(src, c)
    assert matcher.classify(sc) != "auto", (cand_title, sc)
    assert matcher.explain(src, c), cand_title


def test_live_requested_prefers_live():
    src = Source("Hello", "Live at the Church Studios", ["Adele"], None, 300000)
    live = cand("Hello - Live at the Church Studios", ["Adele"], "Hello (Live)", 300000)
    studio = cand("Hello", ["Adele"], "25", 295000)
    assert matcher.score(src, live) >= matcher.AUTO_THRESHOLD
    assert matcher.score(src, studio) < matcher.AUTO_THRESHOLD
    assert "в источнике живое исполнение" in matcher.explain(src, studio)[0]


def test_plain_titles_with_tag_words_are_not_versions():
    for title in ("Live Forever", "Cover Me", "Demons", "Live and Let Die"):
        assert matcher.version_kinds(title) == set(), title
    src = Source("Live Forever", None, ["Oasis"], "Definitely Maybe", 276000)
    assert matcher.score(src, cand("Live Forever - Remastered", ["Oasis"], "Definitely Maybe", 277000)) >= 85


def test_explain_reasons():
    src = Source("Hello", None, ["Adele"], "25", 295000)
    why = matcher.explain(src, cand("Hello", ["Lionel Richie"], "Can't Slow Down", 247000))
    assert "другой артист" in why and any("длительность" in w for w in why) and "другой альбом" in why
    assert matcher.explain(src, cand("Hello", ["Adele"], "25", 295500)) == []
