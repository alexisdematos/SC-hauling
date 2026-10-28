"""Keep a copy of the SC-map data in step with the API.

At start we ask the API for /data/version (a few bytes). When it differs
from the copy in %APPDATA%\\SC-hauling\\data, we download the files again
(~26 KB compressed). The copy then holds the very names the API knows, so
the routes we ask for always resolve. Offline, the last copy (or the data
bundled in the exe) is used.
"""
import json
import urllib.error
import urllib.request
from pathlib import Path

from .api import API_URL, USER_AGENT

FILES = ["locations.csv", "bodies.csv", "quantum.json"]
VERSION_FILE = "version.txt"
# Start-up must not wait long for the network.
TIMEOUT_S = 4


def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def complete(folder):
    folder = Path(folder)
    return all((folder / name).is_file() for name in FILES)


def local_version(folder):
    try:
        return (Path(folder) / VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def sync(folder, api_url=API_URL, timeout=TIMEOUT_S, get=_get):
    """Bring `folder` up to the API's data. Returns (folder or None, status text).

    `status` is "up to date", "updated to <version>", or why it could not
    check ("offline: ..."), for the overlay's status line.
    """
    folder = Path(folder)
    try:
        info = json.loads(get(f"{api_url}/data/version", timeout))
        version = info["version"]
    except urllib.error.HTTPError as e:  # online, but the API has no /data/version
        return (folder if complete(folder) else None), f"API without data version (HTTP {e.code})"
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        return (folder if complete(folder) else None), f"offline: {e}"
    if version == local_version(folder) and complete(folder):
        return folder, "up to date"
    try:
        contents = {name: get(f"{api_url}/data/{name}", timeout) for name in FILES}
    except (urllib.error.URLError, OSError) as e:
        return (folder if complete(folder) else None), f"download failed: {e}"
    # Write every file, then the version: a cut in the middle leaves the old
    # version, so the next start downloads again.
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        tmp = folder / f"{name}.tmp"
        tmp.write_bytes(data)
        tmp.replace(folder / name)
    (folder / VERSION_FILE).write_text(version, encoding="utf-8")
    return folder, f"updated to {version}"
