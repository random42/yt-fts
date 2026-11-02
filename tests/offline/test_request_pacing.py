"""Exercise pacing at yt-dlp's transport boundary, not only batch entry."""

import pytest

from yt_fts.download.download_handler import DownloadHandler


def test_active_download_and_discovery_share_transport_cooldown(monkeypatch):
    now = [200.0]
    requests = []
    monkeypatch.setattr(DownloadHandler, "_next_request_at", 0.0)
    monkeypatch.setattr(DownloadHandler, "_cooldown_until", 0.0)
    monkeypatch.setattr(DownloadHandler, "_request_interval", 1.0)
    monkeypatch.setattr("yt_fts.download.download_handler.time.monotonic", lambda: now[0])
    monkeypatch.setattr("yt_fts.download.download_handler.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    class YDL:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def urlopen(self, url):
            requests.append((url, now[0]))
            if url.endswith("/streams"):
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            return {"entries": [{"id": "video"}]}
        def extract_info(self, url, **kwargs):
            return self.urlopen(url)

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", YDL)
    handler = DownloadHandler()
    # This instance is already active when discovery encounters a rate limit.
    active_worker = handler._youtube_dl({})
    assert handler.get_videos_list("https://youtube.com/channel/id/videos") == ["video"]
    deadline = DownloadHandler._cooldown_until
    assert deadline > now[0]
    active_worker.urlopen("subtitle")
    active_worker.urlopen("next subtitle")
    assert requests[-2][1] >= deadline
    assert requests[-1][1] >= requests[-2][1] + 1


def test_transport_propagates_permanent_error_without_cooldown(monkeypatch):
    class YDL:
        def __init__(self, options): pass
        def urlopen(self, request): raise ValueError("invalid request")

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", YDL)
    handler = DownloadHandler()
    monkeypatch.setattr(handler, "_pace_request", lambda: None)
    monkeypatch.setattr(handler, "_rate_limit_cooldown", lambda seconds: pytest.fail("unexpected cooldown"))
    with pytest.raises(ValueError, match="invalid request"):
        handler._youtube_dl({}).urlopen("bad")
