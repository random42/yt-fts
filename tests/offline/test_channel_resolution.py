import click
import pytest
import requests
from click.testing import CliRunner

from yt_fts.download.download_handler import DownloadHandler
from yt_fts.utils import handle_reject_consent_cookie
from yt_fts.yt_fts import cli


CHANNEL_ID = "UC" + "a" * 22


@pytest.fixture
def handler():
    return DownloadHandler()


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/channel",
        "https://www.youtube.com/channel/",
        "https://www.youtube.com/channel/not-a-channel-id",
        "https://www.youtube.com/@",
        "https://www.youtube.com/@/videos",
        "https://notyoutube.com/@creator",
        "https://youtube.com.evil.example/@creator",
        "https://youtube.com:8443/@creator",
        "ftp://youtube.com/@creator",
    ],
)
def test_invalid_channel_urls_are_rejected_before_resolution(handler, monkeypatch, url):
    def unexpected_resolution(*args, **kwargs):
        raise AssertionError("invalid URL must not reach yt-dlp")

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", unexpected_resolution)

    with pytest.raises(ValueError, match="Invalid YouTube channel URL"):
        normalized = handler.validate_channel_url(url)
        handler.get_channel_id(normalized)


@pytest.mark.parametrize(
    ("host", "path"),
    [
        ("youtube.com", "/@creator"),
        ("www.youtube.com", "/@creator/"),
        ("m.youtube.com", "/channel/" + CHANNEL_ID + "/videos/"),
        ("music.youtube.com", "/channel/" + CHANNEL_ID),
    ],
)
def test_normalizes_supported_hosts_and_trailing_paths(handler, host, path):
    assert handler.validate_channel_url(f"https://{host}{path}?feature=shared") == (
        f"https://www.youtube.com/{path.strip('/').removesuffix('/videos')}/videos"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@creator/",
        "https://www.youtube.com/@creator/videos?view=0",
        f"https://www.youtube.com/channel/{CHANNEL_ID}?feature=shared",
    ],
)
def test_resolution_uses_normalized_url_and_validates_metadata(handler, monkeypatch, url):
    seen = []

    class FakeYoutubeDL:
        def __init__(self, options):
            assert options["extract_flat"] is True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, normalized_url, download, process):
            assert process is False
            seen.append((normalized_url, download))
            return {"channel_id": CHANNEL_ID, "channel": "Creator"}

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", FakeYoutubeDL)
    normalized = handler.validate_channel_url(url)

    assert handler.get_channel_id(normalized) == CHANNEL_ID
    assert handler.channel_name == "Creator"
    assert seen == [(normalized, False)]


def test_download_channel_passes_normalized_url_to_resolution_and_session(handler, monkeypatch):
    calls = []
    handler.channel_name = "Creator"
    monkeypatch.setattr(handler, "get_channel_id", lambda url: calls.append(("resolve", url)) or CHANNEL_ID)
    monkeypatch.setattr(handler, "init_session", lambda url: calls.append(("session", url)))
    monkeypatch.setattr("yt_fts.download.download_handler.check_if_channel_exists", lambda channel_id: True)
    monkeypatch.setattr(handler, "update_channel", lambda channel_id: calls.append(("update", channel_id)))

    handler.download_channel("https://m.youtube.com/@creator/?feature=shared")

    normalized = "https://www.youtube.com/@creator/videos"
    assert calls == [("resolve", normalized), ("update", CHANNEL_ID)]


def test_resolution_uses_browser_cookies_when_requested(handler, monkeypatch):
    handler.cookies_from_browser = "chrome"

    class FakeYoutubeDL:
        def __init__(self, options):
            assert options["cookiesfrombrowser"] == ("chrome",)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download, process):
            return {"channel_id": CHANNEL_ID, "channel": "Creator"}

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", FakeYoutubeDL)

    assert handler.get_channel_id("https://www.youtube.com/@creator/videos") == CHANNEL_ID


def test_unresolved_or_handle_metadata_fails_without_followup_requests(handler, monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download, process):
            return {"channel_id": "@creator", "channel": "Creator"}

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", FakeYoutubeDL)

    with pytest.raises(SystemExit) as error:
        handler.get_channel_id("https://www.youtube.com/@creator/videos")

    assert error.value.code == 1
    assert handler.channel_id is None


def test_resolution_follows_handle_redirect_to_canonical_channel(handler, monkeypatch):
    seen = []

    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download, process):
            assert download is False
            assert process is False
            seen.append(url)
            if url.endswith("/@creator/videos"):
                return {"_type": "url", "url": f"https://www.youtube.com/channel/{CHANNEL_ID}/videos"}
            return {"channel_id": CHANNEL_ID, "channel": "Creator"}

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", FakeYoutubeDL)

    assert handler.get_channel_id("https://www.youtube.com/@creator/videos") == CHANNEL_ID
    assert handler.channel_name == "Creator"
    assert seen == [
        "https://www.youtube.com/@creator/videos",
        f"https://www.youtube.com/channel/{CHANNEL_ID}/videos",
    ]


def test_resolution_bounds_url_redirects(handler, monkeypatch):
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download, process):
            calls.append(url)
            return {"_type": "url", "url": url}

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", FakeYoutubeDL)

    with pytest.raises(SystemExit) as error:
        handler.get_channel_id("https://www.youtube.com/@creator/videos")

    assert error.value.code == 1
    assert len(calls) == 6  # Initial lookup plus five bounded redirects.


def test_resolution_does_not_process_playlist_entries(handler, monkeypatch):
    consumed = []

    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download, process):
            assert process is False
            return {"channel_id": CHANNEL_ID, "channel": "Creator", "entries": iter(consumed.append("entry") for _ in range(10))}

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", FakeYoutubeDL)

    assert handler.get_channel_id("https://www.youtube.com/@creator/videos") == CHANNEL_ID
    assert consumed == []


@pytest.mark.parametrize("url", ["https://notyoutube.com/@creator", "https://www.youtube.com/channel"])
def test_invalid_channel_url_is_clean_cli_error_without_network(monkeypatch, url):
    def unexpected_network(*args, **kwargs):
        raise AssertionError("invalid channel URL must not make a network request")

    monkeypatch.setattr(DownloadHandler, "init_session", unexpected_network)
    monkeypatch.setattr(DownloadHandler, "get_channel_id", unexpected_network)

    result = CliRunner().invoke(cli, ["download", url])

    assert result.exit_code == 1
    assert "Error: Invalid YouTube channel URL" in result.output
    assert "Traceback" not in result.output


def test_channel_name_fallback_uses_bounded_request_timeout(handler):
    class FeedResponse:
        status_code = 200
        content = b"<feed><author><name>Creator</name></author></feed>"

    class Session:
        def get(self, url, timeout):
            assert url.endswith(f"channel_id={CHANNEL_ID}")
            assert timeout == 10
            return FeedResponse()

    handler.session = Session()

    assert handler.get_channel_name(CHANNEL_ID) == "Creator"


@pytest.mark.parametrize("content", [b"<feed>", b"<feed><author /></feed>"])
def test_invalid_channel_feed_fails_with_clear_error(handler, content):
    class FeedResponse:
        status_code = 200
        def __init__(self, content):
            self.content = content

    class Session:
        def get(self, url, timeout):
            return FeedResponse(content)

    handler.session = Session()

    with pytest.raises(click.ClickException, match="invalid channel feed|does not contain a channel name"):
        handler.get_channel_name(CHANNEL_ID)


def test_consent_requests_use_bounded_timeouts():
    class Response:
        url = "https://consent.youtube.com/m"
        text = '<input type="hidden" name="bl" value="token">'

    class Session:
        def get(self, url, timeout):
            assert timeout == 10
            return Response()

        def post(self, url, data, timeout):
            assert url == "https://consent.youtube.com/save"
            assert data["bl"] == "token"
            assert timeout == 10

    handle_reject_consent_cookie("https://www.youtube.com/@creator", Session())


def test_channel_name_fallback_request_error_is_a_clear_cli_error(handler, monkeypatch):
    monkeypatch.setattr(handler, "get_channel_id", lambda url: CHANNEL_ID)
    monkeypatch.setattr(handler, "init_session", lambda url: (_ for _ in ()).throw(
        requests.ConnectionError("offline")
    ))

    with pytest.raises(click.ClickException, match="Could not get channel name: offline"):
        handler.download_channel("https://www.youtube.com/@creator")


def test_missing_metadata_fails_clearly(handler, monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download, process):
            return None

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", FakeYoutubeDL)

    with pytest.raises(SystemExit) as error:
        handler.get_channel_id("https://www.youtube.com/@creator/videos")

    assert error.value.code == 1


def test_metadata_request_error_exits_before_followup_requests(handler, monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download, process):
            raise RuntimeError("HTTP Error 403: Forbidden")

    monkeypatch.setattr("yt_fts.download.download_handler.yt_dlp.YoutubeDL", FakeYoutubeDL)

    with pytest.raises(SystemExit) as error:
        handler.get_channel_id("https://www.youtube.com/@creator/videos")

    assert error.value.code == 1
