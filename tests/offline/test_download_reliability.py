import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from click.testing import CliRunner

from yt_fts.download.download_handler import DownloadHandler
from yt_fts.db_utils import make_db
from yt_fts.yt_fts import cli


@pytest.fixture
def handler():
    DownloadHandler._next_request_at = 0
    DownloadHandler._cooldown_until = 0
    DownloadHandler._request_interval = 1
    return DownloadHandler()


def test_default_jobs_and_cli_rejects_nonpositive_jobs(handler):
    assert handler.number_of_jobs == 2
    result = CliRunner().invoke(cli, ["download", "https://www.youtube.com/@creator", "--jobs", "0"])
    assert result.exit_code != 0
    assert "range x>=1" in result.output


def test_shared_pacing_reserves_separate_slots_and_rechecks_cooldown(handler, monkeypatch):
    now = [100.0]
    sleeps = []
    monkeypatch.setattr("yt_fts.download.download_handler.time.monotonic", lambda: now[0])

    def sleep(seconds):
        now[0] += seconds
        sleeps.append(seconds)
        if len(sleeps) == 1:
            DownloadHandler._rate_limit_cooldown(10)

    monkeypatch.setattr("yt_fts.download.download_handler.time.sleep", sleep)
    DownloadHandler._request_interval = 0
    DownloadHandler._pace_request()
    DownloadHandler._request_interval = 1
    DownloadHandler._rate_limit_cooldown(10)
    DownloadHandler._request_interval = 0
    DownloadHandler._pace_request()
    assert now[0] == 120
    assert sleeps == [10, 10]


def test_concurrent_gate_observes_shared_interval(handler, monkeypatch):
    now = [10.0]
    lock = threading.Lock()
    monkeypatch.setattr("yt_fts.download.download_handler.time.monotonic", lambda: now[0])

    def sleep(seconds):
        with lock:
            now[0] += seconds

    monkeypatch.setattr("yt_fts.download.download_handler.time.sleep", sleep)
    DownloadHandler._request_interval = 1
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: handler._pace_request(), range(2)))
    assert now[0] == 11.0
    assert DownloadHandler._next_request_at == 12.0


def test_get_vtt_uses_one_download_call_and_disables_internal_retries(handler, monkeypatch):
    seen = []

    class YDL:
        def __init__(self, options):
            seen.append(options)

        def __enter__(self): return self
        def __exit__(self, *args): return False
        def download(self, urls):
            seen.append(urls)
            return 0

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", YDL)
    monkeypatch.setattr(handler, "_pace_request", lambda: None)
    assert handler.get_vtt("/tmp", "https://youtu.be/vid", "en")
    assert seen[1] == ["https://youtu.be/vid"]
    assert {key: seen[0][key] for key in ("retries", "fragment_retries", "extractor_retries")} == {
        "retries": 0, "fragment_retries": 0, "extractor_retries": 0
    }
    assert seen[0]["sleep_interval_requests"] == seen[0]["sleep_interval_subtitles"] == 1


@pytest.mark.parametrize(("errors", "expected"), [
    ([RuntimeError("HTTP Error 429: Too Many Requests")] * 3, 3),
    ([RuntimeError("permanent failure")], 1),
])
def test_get_vtt_bounds_rate_limit_retries_and_stops_permanent_errors(handler, monkeypatch, errors, expected):
    calls = []

    class YDL:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def download(self, urls):
            calls.append(urls)
            error = errors.pop(0)
            raise error

    cooldowns = []
    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", YDL)
    monkeypatch.setattr(handler, "_pace_request", lambda: None)
    monkeypatch.setattr(handler, "_rate_limit_cooldown", cooldowns.append)
    assert handler.get_vtt("/tmp", "https://youtu.be/vid", "en") is False
    assert len(calls) == expected
    assert cooldowns == ([10, 20, 30] if expected == 3 else [])


def _write_vtt(tmp_path, video_id, channel="channel"):
    (tmp_path / f"{video_id}.info.json").write_text(json.dumps({"title": video_id, "upload_date": "20240101", "channel_id": channel}))
    (tmp_path / f"{video_id}.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello world\n")


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    make_db(str(path))
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO Channels VALUES (?, ?, ?)", ("channel", "Creator", "https://youtube.com"))
    monkeypatch.setattr("yt_fts.download.download_handler.get_db_path", lambda: str(path))
    return path


def test_incremental_import_keeps_first_video_when_later_import_fails(handler, tmp_path, db_path, monkeypatch):
    handler.tmp_dir = str(tmp_path)
    _write_vtt(tmp_path, "first")
    _write_vtt(tmp_path, "second")
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TRIGGER fail_second BEFORE INSERT ON Subtitles WHEN NEW.video_id = 'second' BEGIN SELECT RAISE(ABORT, 'simulated insert failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        handler.vtt_to_db({"first", "second"})
    with sqlite3.connect(db_path) as con:
        con.execute("DROP TRIGGER fail_second")
    with sqlite3.connect(db_path) as con:
        assert con.execute("SELECT video_id FROM Videos").fetchall() == [("first",)]
        assert con.execute("SELECT DISTINCT video_id FROM Subtitles").fetchall() == [("first",)]
    assert handler.vtt_to_db({"second"}) == 1
    assert handler.vtt_to_db({"first", "second"}) == 0


def test_import_transaction_rolls_back_video_when_subtitle_insert_fails(handler, tmp_path, db_path, monkeypatch):
    handler.tmp_dir = str(tmp_path)
    _write_vtt(tmp_path, "broken")
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TRIGGER fail_broken BEFORE INSERT ON Subtitles WHEN NEW.video_id = 'broken' BEGIN SELECT RAISE(ABORT, 'simulated subtitle failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        handler.vtt_to_db({"broken"})
    with sqlite3.connect(db_path) as con:
        assert con.execute("SELECT count(*) FROM Videos").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM Subtitles").fetchone()[0] == 0


def test_update_compares_ids_when_remote_and_local_counts_match(handler, monkeypatch):
    handler.init_session = lambda url: None
    handler.get_channel_name = lambda channel: "Creator"
    monkeypatch.setattr("yt_fts.download.download_handler.get_vid_ids_by_channel_id", lambda channel: [("old",)])
    monkeypatch.setattr(handler, "get_videos_list", lambda url: ["new"])
    seen = []
    monkeypatch.setattr(handler, "download_vtts", lambda: seen.extend(handler.video_ids))
    handler.update_channel("UC" + "a" * 22)
    assert seen == ["new"]


def test_all_discovery_request_paths_use_shared_gate(handler, monkeypatch):
    calls = []
    monkeypatch.setattr(handler, "_pace_request", lambda: calls.append("gate"))
    monkeypatch.setattr("yt_fts.download.download_handler.handle_reject_consent_cookie", lambda url, session: None)
    monkeypatch.setattr("yt_fts.download.download_handler.requests.session", lambda: object())
    handler.init_session("url")

    class YDL:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, **kwargs):
            if "@creator" in url:
                return {"channel_id": "UC" + "a" * 22, "entries": []}
            return {"entries": []}

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", YDL)
    handler.get_channel_id("https://www.youtube.com/@creator/videos")
    handler.get_videos_list("https://www.youtube.com/channel/UC" + "a" * 22 + "/videos")
    handler.get_playlist_data("https://www.youtube.com/playlist?id=x")
    assert len(calls) == 5
