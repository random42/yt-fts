import json
import sqlite3
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from yt_fts import db_utils
from yt_fts.download.download_handler import DownloadHandler, DownloadResult
from yt_fts.yt_fts import cli

CHANNEL = "UC" + "a" * 22
ROUTES = ["channel", "playlist", "update", "existing"]


def command(route):
    if route == "playlist":
        return ["download", "--playlist", "https://youtube.com/playlist?list=PL1"]
    if route == "update":
        return ["update", "--channel", CHANNEL]
    return ["download", f"https://youtube.com/channel/{CHANNEL}/videos"]


@pytest.fixture
def library(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    db_utils.make_db(str(path))
    monkeypatch.setattr(db_utils, "get_db_path", lambda: str(path))
    monkeypatch.setattr("yt_fts.download.download_handler.get_db_path", lambda: str(path))
    monkeypatch.setattr(DownloadHandler, "_pace_request", lambda self: None)
    monkeypatch.setattr(DownloadHandler, "_rate_limit_cooldown", lambda self, seconds: None)
    monkeypatch.setattr(DownloadHandler, "get_channel_id", lambda self, url: CHANNEL)
    monkeypatch.setattr(DownloadHandler, "init_session", lambda self, url: object())
    monkeypatch.setattr(DownloadHandler, "get_channel_name", lambda self, cid: "Test channel")
    return path


def counts(path):
    with sqlite3.connect(path) as con:
        return tuple(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("Channels", "Videos", "Subtitles"))


def discovery(monkeypatch, ids):
    monkeypatch.setattr(DownloadHandler, "get_videos_list", lambda self, url: ids)
    monkeypatch.setattr(DownloadHandler, "get_playlist_data", lambda self, url: [
        {"channel_name": "Test channel", "channel_id": CHANNEL, "video_id": vid,
         "channel_url": f"https://youtube.com/channel/{CHANNEL}/videos"} for vid in ids])


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("outcomes,expected_exit,expected_imported", [
    ([], 0, 0), (["none"], 0, 0), (["ready", "failure"], 2, 1),
    (["failure"], 1, 0), (["missing"], 1, 0), (["ready", "bad-import"], 2, 1),
])
def test_cli_outcome_matrix(library, monkeypatch, route, outcomes, expected_exit, expected_imported):
    if route in ("update", "existing"):
        db_utils.add_channel_info(CHANNEL, "Test channel", "https://youtube.com/channel/" + CHANNEL)
    ids = [f"v{i}" for i in range(len(outcomes))]
    discovery(monkeypatch, ids)
    behavior = dict(zip(ids, outcomes))

    class YDL:
        def __init__(self, options): self.options = options
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, download):
            assert download is True
            vid = url.split("v=")[-1]
            outcome = behavior[vid]
            if outcome == "failure":
                raise RuntimeError("Video unavailable")
            if outcome == "none":
                return {"requested_subtitles": {}}
            if outcome != "missing":
                directory = Path(self.options["outtmpl"]).parent
                (directory / f"{vid}.en.vtt").write_text(
                    "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello world\n")
                metadata = {"title": vid, "upload_date": "20240101", "channel_id": CHANNEL}
                (directory / f"{vid}.info.json").write_text(
                    "invalid json" if outcome == "bad-import" else json.dumps(metadata))
            return {"requested_subtitles": {"en": {"ext": "vtt"}}}

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", YDL)
    result = CliRunner().invoke(cli, command(route))
    assert result.exit_code == expected_exit, (result.output, result.exception)
    assert counts(library)[1:] == (expected_imported, expected_imported)
    assert f"{expected_imported} imported" in result.output
    if "none" in outcomes:
        assert "1 skipped" in result.output and "no captions" in result.output
    if expected_exit:
        assert "1 failed" in result.output and "Retry" in result.output
    if "failure" in outcomes:
        assert "unavailable" in result.output


@pytest.mark.parametrize("route", ROUTES)
def test_discovery_failure_is_not_empty_success(library, monkeypatch, route):
    if route in ("update", "existing"):
        db_utils.add_channel_info(CHANNEL, "Test channel", "url")
    def fail(self, url): raise RuntimeError("HTTP 429: discovery blocked")
    monkeypatch.setattr(DownloadHandler, "get_videos_list", fail)
    monkeypatch.setattr(DownloadHandler, "get_playlist_data", fail)
    result = CliRunner().invoke(cli, command(route))
    assert result.exit_code == 1
    assert counts(library) == (int(route in ("update", "existing")), 0, 0)
    assert "discovery failed" in result.output.lower()
    assert "0 imported" not in result.output
    assert "do not have subtitles" not in result.output


@pytest.mark.parametrize("message,category,attempts", [
    ("HTTP Error 429: Too Many Requests", "rate limited", 3),
    ("Private video", "unavailable", 1),
    ("HTTP Error 403: Forbidden", "other failure", 1),
    ("Unexpected transport failure", "other failure", 1),
])
def test_failure_categories_are_visible(library, monkeypatch, tmp_path, capsys, message, category, attempts):
    calls = []
    class YDL:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, download):
            calls.append(url)
            raise RuntimeError(message)
    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", YDL)
    handler = DownloadHandler()
    handler.tmp_dir, handler.video_ids = str(tmp_path), ["failed"]
    result = handler.download_vtts()
    assert (result.imported, result.failed, result.exit_code) == (0, 1, 1)
    assert len(calls) == attempts
    assert f"Failed failed: {category}" in capsys.readouterr().out


def test_update_all_continues_after_discovery_failure(library, monkeypatch):
    db_utils.add_channel_info(CHANNEL, "First", "url")
    db_utils.add_channel_info("UC" + "b" * 22, "Second", "url")
    called = []
    def update(self, target, raise_failure=True):
        called.append(target)
        assert raise_failure is False
        if len(called) == 1:
            raise click.ClickException("Discovery failed")
        return DownloadResult(imported=1)
    monkeypatch.setattr(DownloadHandler, "update_channel", update)
    result = CliRunner().invoke(cli, ["update"])
    assert result.exit_code == 2
    assert len(called) == 2
    assert "1 imported" in result.output and "1 failed" in result.output
    assert "Retry" in result.output


@pytest.mark.parametrize("error,allowed", [
    ("This channel does not have a streams tab", True),
    ("HTTP 500: Internal Server Error", False),
    ("Extractor returned invalid data", False),
])
def test_only_missing_stream_tab_is_optional(library, monkeypatch, error, allowed):
    class YDL:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, **kwargs):
            if url.endswith("/streams"):
                raise RuntimeError(error)
            return {"entries": []}
    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", YDL)
    handler = DownloadHandler()
    if allowed:
        assert handler.get_videos_list("https://youtube.com/channel/id/videos") == []
    else:
        with pytest.raises(RuntimeError, match="Could not discover channel streams"):
            handler.get_videos_list("https://youtube.com/channel/id/videos")


def test_repeated_video_ids_are_counted_once(library, monkeypatch, tmp_path):
    called = []
    def absent(self, *args):
        called.append(args)
        return "no captions"
    monkeypatch.setattr(DownloadHandler, "get_vtt", absent)
    handler = DownloadHandler()
    handler.tmp_dir, handler.video_ids = str(tmp_path), ["same", "same"]
    result = handler.download_vtts()
    assert len(called) == 1
    assert (result.imported, result.skipped, result.failed) == (0, 1, 0)
