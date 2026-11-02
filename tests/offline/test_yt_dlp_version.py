import http.client
import json
import shlex
import sys

import pytest
from click.testing import CliRunner

from yt_fts import yt_fts
from yt_fts.yt_dlp_version import MIN_YT_DLP_VERSION, get_update_notice


class FakeResponse:
    def __init__(self, payload):
        payload.setdefault("info", {}).setdefault("requires_python", ">=3.10")
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        assert size == 2_000_001
        return self.payload


def test_warns_with_active_interpreter_upgrade_command(monkeypatch):
    def fake_urlopen(request, timeout):
        assert request.full_url == "https://pypi.org/pypi/yt-dlp/json"
        assert timeout == 2.0
        return FakeResponse({"info": {"version": "2026.10.26"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    notice = get_update_notice("2026.10.21")

    assert "2026.10.26" in notice
    assert f"{shlex.quote(sys.executable)} -m pip install --upgrade" in notice
    assert f"uv pip install --python {shlex.quote(sys.executable)} --upgrade" in notice
    assert f"yt-dlp[default]>={MIN_YT_DLP_VERSION}" in notice


def test_current_or_newer_release_needs_no_update(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: FakeResponse({"info": {"version": "2026.10.26"}}),
    )

    assert get_update_notice("2026.10.26") is None
    assert get_update_notice("2026.10.27") is None


def test_unavailable_network_is_nonfatal_and_bounded(monkeypatch):
    calls = []

    def offline(request, timeout):
        calls.append(timeout)
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", offline)

    assert get_update_notice("2026.10.21") is None
    assert calls == [2.0]


def test_cli_version_shows_application_interpreter_and_yt_dlp(monkeypatch):
    monkeypatch.setattr(yt_fts, "get_update_notice", lambda version: None)

    result = CliRunner().invoke(yt_fts.cli, ["--version"])

    assert result.exit_code == 0
    assert "yt-fts " in result.output
    assert f"Python interpreter: {sys.executable}" in result.output
    assert "yt-dlp " in result.output


def test_python_incompatible_latest_release_recommends_python_upgrade(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: FakeResponse({"info": {
            "version": "2026.10.26", "requires_python": ">=3.11"
        }}),
    )
    monkeypatch.setattr("yt_fts.yt_dlp_version.sys.version_info", (3, 10, 22))

    notice = get_update_notice("2026.10.21")

    assert "requires Python 3.11 or newer" in notice
    assert "Upgrade Python before upgrading yt-dlp" in notice
    assert "pip install" not in notice


def test_deeply_nested_response_is_nonfatal(monkeypatch):
    payload = b'{"info":{"version":"' + b"[" * 3000 + b'"2026.10.26"' + b"]" * 3000 + b'}}'

    class DeepResponse(FakeResponse):
        def read(self, size):
            return payload

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: DeepResponse({}))

    assert get_update_notice("2026.10.21") is None


def test_incomplete_http_response_is_nonfatal(monkeypatch):
    def incomplete_response(*args, **kwargs):
        raise http.client.IncompleteRead(b"{")

    monkeypatch.setattr("urllib.request.urlopen", incomplete_response)

    assert get_update_notice("2026.10.21") is None


def test_oversized_response_is_ignored(monkeypatch):
    class OversizedResponse(FakeResponse):
        def read(self, size):
            assert size == 2_000_001
            return b"x" * size

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: OversizedResponse({}),
    )

    assert get_update_notice("2026.10.21") is None


def test_update_check_can_be_skipped_without_network(monkeypatch):
    monkeypatch.setenv("YT_FTS_SKIP_UPDATE_CHECK", "1")

    def unexpected_network_call(*args, **kwargs):
        raise AssertionError("update check should be skipped")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_network_call)

    assert get_update_notice("2026.10.21") is None


@pytest.mark.parametrize("version", [None, 42, ["2026.10.26"]])
def test_invalid_release_response_is_nonfatal(monkeypatch, version):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: FakeResponse({"info": {"version": version}}),
    )

    assert get_update_notice("2026.10.21") is None


def test_missing_release_version_is_nonfatal(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: FakeResponse({"unexpected": "payload"}),
    )

    assert get_update_notice("2026.10.21") is None
