"""Check whether the installed yt-dlp release is behind PyPI's latest stable release."""

import http.client
import json
import os
import re
import shlex
import sys
import urllib.request

MIN_YT_DLP_VERSION = "2026.10.21"
_PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
_MAX_RESPONSE_BYTES = 2_000_000
_RELEASE_DATE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.|$)")


def _release_date(version: str) -> tuple[int, int, int] | None:
    if not isinstance(version, str):
        return None
    match = _RELEASE_DATE.match(version)
    return tuple(map(int, match.groups())) if match else None


def get_update_notice(installed_version: str, timeout: float = 2.0) -> str | None:
    """Return an actionable update message, or None when current or offline."""
    if os.environ.get("YT_FTS_SKIP_UPDATE_CHECK") == "1":
        return None

    try:
        request = urllib.request.Request(_PYPI_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            return None
        latest_info = json.loads(payload)["info"]
        latest_version = latest_info["version"]
        requires_python = latest_info["requires_python"]
    except (http.client.HTTPException, OSError, ValueError, KeyError, TypeError, RecursionError):
        return None

    if not isinstance(requires_python, str):
        return None

    latest_date = _release_date(latest_version)
    installed_date = _release_date(installed_version)
    if latest_date is None or installed_date is None or installed_date >= latest_date:
        return None

    required = re.search(r">=\s*(\d+)\.(\d+)", requires_python)
    if required and sys.version_info[:2] < tuple(map(int, required.groups())):
        return (
            f"yt-dlp {latest_version} requires Python {required.group(1)}.{required.group(2)} or newer. "
            "Upgrade Python before upgrading yt-dlp."
        )

    requirement = f"'yt-dlp[default]>={MIN_YT_DLP_VERSION}'"
    interpreter = shlex.quote(sys.executable)
    return (
        f"yt-dlp {installed_version} is older than the latest stable release ({latest_version}). "
        f"Upgrade with pip: {interpreter} -m pip install --upgrade {requirement}; "
        f"or with uv: uv pip install --python {interpreter} --upgrade {requirement}"
    )
