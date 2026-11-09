"""Regression coverage for named-store and season/episode web-search routing."""
from local_chat import server


def test_season_episode_request_uses_web_discovery_not_generic_media(monkeypatch):
    seen = {}

    def web(query, search_options=None):
        seen["query"] = query
        return {"results": [], "engine": "web"}

    def media(*args, **kwargs):
        raise AssertionError("episode requests must not use generic media lookup")

    monkeypatch.setattr(server, "_search_web_text", web)
    monkeypatch.setattr(server.media_search, "search_media", media)
    result = server._search_web("توی اینترنت فصل ۲ انیمه بلک کاور آخرین قسمتشو پیدا کن لینک تماشاشو بده")

    assert "قسمت کامل پخش آنلاین" in seen["query"]
    assert result["intent"]["kind"] == "movie_episode"


def test_regular_movie_search_keeps_specialized_media_path(monkeypatch):
    monkeypatch.setattr(server.media_search, "search_media", lambda *a, **k: [{"url": "https://example.com/watch"}])
    result = server._search_web("لینک تماشای فیلم Inception")
    assert result["engine"] == "media"
