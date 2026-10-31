"""Update the exe from the latest GitHub release.

At start (the exe only, not from source), we ask GitHub for the latest
release of the project. When its version is newer, its SC-hauling.exe is
downloaded next to ours, checked against the SHA-256 GitHub gives for it
(or a SC-hauling.exe.sha256 file of the release), and swapped in: Windows
lets a running exe be renamed, not replaced, so ours becomes .old (deleted
at the next start) and the new one takes its name. It runs from the next
start; the menu restarts at once.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .api import USER_AGENT

REPO = "alexisdematos/SC-hauling"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "SC-hauling.exe"
TIMEOUT_S = 10
DOWNLOAD_TIMEOUT_S = 120


class UpdateError(Exception):
    pass


def _get(url, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise UpdateError(f"HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise UpdateError(f"no answer ({getattr(e, 'reason', None) or e})") from None


def version_tuple(text):
    """"v0.2.10" -> (0, 2, 10); None when it is no version."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(n) for n in m.group(1).split(".")) if m else None


def running_exe():
    """Path of our exe, or None when run from source."""
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def latest(get=_get, current=__version__):
    """(version, exe url, sha256) of a newer release, or None when we are up
    to date. Raises UpdateError."""
    try:
        release = json.loads(get(LATEST_URL))
    except ValueError:
        raise UpdateError("bad answer") from None
    version = release.get("tag_name")
    if not version_tuple(version) or not version_tuple(current) or version_tuple(version) <= version_tuple(current):
        return None
    assets = {a.get("name"): a for a in release.get("assets") or []}
    exe = assets.get(ASSET)
    if not exe:
        raise UpdateError(f"release {version} has no {ASSET}")
    digest = (exe.get("digest") or "").removeprefix("sha256:")
    if not digest and f"{ASSET}.sha256" in assets:
        digest = get(assets[f"{ASSET}.sha256"]["browser_download_url"]).decode("ascii", "replace").split()[0]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest or ""):
        raise UpdateError(f"release {version} gives no SHA-256 for {ASSET}")
    return version.lstrip("v"), exe["browser_download_url"], digest.lower()


def install(exe, url, sha256, get=_get):
    """Download the new exe next to `exe`, check it, swap it in."""
    exe = Path(exe)
    data = get(url, DOWNLOAD_TIMEOUT_S)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise UpdateError("download does not match its SHA-256: not installed")
    new, old = exe.with_name(exe.name + ".new"), exe.with_name(exe.name + ".old")
    new.write_bytes(data)
    try:
        old.unlink(missing_ok=True)
        exe.replace(old)  # a running exe can be renamed
        new.replace(exe)
    except OSError as e:
        if not exe.exists() and old.exists():
            old.replace(exe)  # put ours back
        raise UpdateError(f"could not swap the exe ({e})") from None


def cleanup(exe):
    """Delete the exe an update left (it ran until then). We run the new
    one now: the install is no longer pending."""
    try:
        Path(exe).with_name(Path(exe).name + ".old").unlink(missing_ok=True)
        _marker(exe).with_suffix(".pending").unlink(missing_ok=True)
    except OSError:
        pass  # still running: next time


def _marker(exe):
    return Path(exe).with_name(Path(exe).name + ".installed")


def update(exe, get=_get):
    """Check and install. Status text for the overlay, or None when up to
    date. Network: on a worker thread."""
    try:
        found = latest(get)
        if not found:
            return None
        version, url, sha256 = found
        try:
            done = _marker(exe).read_text(encoding="ascii").split()
        except OSError:
            done = []
        if done == [version, sha256] and not _marker(exe).with_suffix(".pending").exists():
            # Installed and started, yet we are older: the release's exe has
            # another version inside than its tag. Not again at each start.
            return f"Release {version} holds an older version: not downloaded again"
        install(exe, url, sha256, get)
        _marker(exe).write_text(f"{version} {sha256}", encoding="ascii")
        _marker(exe).with_suffix(".pending").write_text("", encoding="ascii")
    except (UpdateError, OSError) as e:
        return f"Update not installed: {e}"
    return f"Updated to {version}: right-click → Restart to use it"


def restart(exe):
    """Start the exe again a moment after we quit (the local server port
    must be free first)."""
    subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & start "" "{exe}"',
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True, cwd=os.path.dirname(exe))
