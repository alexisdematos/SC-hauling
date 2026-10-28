"""Read Game.log, and the previous sessions kept in logbackups/.

Contracts survive a game restart, but the new Game.log does not repeat
them: we replay the recent backups first. When the game restarts it
starts a new Game.log, smaller than what we read: we start over.
"""
import os
import time
from pathlib import Path

DEFAULT_LOGS = [
    rf"{drive}:\{folder}\StarCitizen\LIVE\Game.log"
    for drive in "CDEFG"
    for folder in (r"Program Files\Roberts Space Industries", "Roberts Space Industries", "Star Citizen",
                   r"Games\Roberts Space Industries", r"Games\Star Citizen")
]

# Older sessions than this cannot hold a running contract.
BACKUP_MAX_AGE_S = 3 * 24 * 3600


def find_log(path=None):
    for p in [path] if path else DEFAULT_LOGS:
        if p and Path(p).is_file():
            return Path(p)
    return Path(path) if path else None


def _lines(path, start=0):
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    # Keep a partial last line for the next read.
    end = data.rfind(b"\n") + 1
    text = data[:end].decode("utf-8", "replace")
    return text.splitlines(), start + end


class LogSource:
    def __init__(self, path, backups=True, now=None):
        self.path = Path(path)
        self.backups = backups
        self.offset = 0
        self._now = now or time.time

    def backup_files(self):
        folder = self.path.parent / "logbackups"
        if not self.backups or not folder.is_dir():
            return []
        limit = self._now() - BACKUP_MAX_AGE_S
        files = [p for p in folder.glob("*.log") if p.stat().st_mtime >= limit]
        return sorted(files, key=lambda p: p.stat().st_mtime)

    def initial(self):
        """Every line of the recent backups, then of the current log."""
        for backup in self.backup_files():
            yield from _lines(backup)[0]
        self.offset = 0
        yield from self.poll()

    def poll(self):
        """New lines since the last call. Empty list when nothing changed."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.offset:
            # New game session: the old log is now the newest backup.
            self.offset = 0
            raise LogRestarted()
        if size == self.offset:
            return []
        lines, self.offset = _lines(self.path, self.offset)
        return lines


class LogRestarted(Exception):
    """The game started a new Game.log: rebuild the state from scratch."""
