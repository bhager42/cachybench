"""Telling you how the session is going when you are not at the machine.

``notify-send`` only reaches the desktop the benchmark is running on, which is
the one place you are not looking during an unattended session. ``notify_url``
adds a push to your phone through any ntfy-compatible endpoint (ntfy.sh or a
self-hosted server): a plain-text POST with the title in a header.

A notification must never be the thing that breaks a benchmark, so every
failure here is swallowed.
"""

from __future__ import annotations

import shutil
import subprocess
import urllib.error
import urllib.request


def send(title: str, message: str, url: str = "", urgent: bool = False) -> None:
    """Notify the local desktop, and ``url`` if one is configured."""
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", *(["-u", "critical"] if urgent else []), title, message],
                check=False,
                timeout=10,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    if url:
        push(url, title, message, urgent)


def push(url: str, title: str, message: str, urgent: bool = False) -> bool:
    """POST to an ntfy-style endpoint. Returns False if it could not be delivered."""
    try:
        request = urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            method="POST",
            headers={
                # Header values must be latin-1; drop anything else rather than fail.
                "Title": title.encode("latin-1", errors="ignore").decode("latin-1"),
                "Priority": "high" if urgent else "default",
                "Tags": "warning" if urgent else "bar_chart",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False
