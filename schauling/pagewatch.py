"""Screenshots of the contract page the instant a contract is accepted.

The overlay reads Game.log once a second: too late if the player clicks on
to another contract right away. This thread watches only the end of the log,
every 100 ms, and takes the screenshots as soon as "Contract Accepted"
shows, then reads them (OCR) and hands the pages over, one per screenshot,
so the overlay can keep the ones that are this contract's.
"""
import os
import threading
import time

from . import ocr, windows
from .logparser import parse_line
from .logsource import _lines

WATCH_S = 0.1
# Screenshots after the accept line (seconds): not at once, the page text is
# still being drawn then, and over a few seconds, as the page may come back
# late. Screenshots of another contract's page are dropped afterwards.
# Seen in game: the objectives type themselves out for about 1.5 s after
# the accept line (empty at 0.5 s, cut words at 0.9 s, whole at 1.5 s).
SHOTS_S = (1.0, 1.3, 1.7, 2.3, 3.0)


class PageWatcher:
    def __init__(self, log_path, overlay_hwnd, enabled, on_pages):
        """overlay_hwnd(): our window, left out of the screenshots;
        enabled(): read the option each time; on_pages(mission, pages, error,
        final) is called on this thread, pages being one objectives list per
        shot: once per screenshot, then once more with final True."""
        self.path = log_path
        self.overlay_hwnd = overlay_hwnd
        self.enabled = enabled
        self.on_pages = on_pages
        try:
            self.offset = os.path.getsize(log_path)  # only what comes from now on
        except OSError:
            self.offset = 0

    def start(self):
        ocr.warm_up()  # the OCR process is ready before the first accept
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            time.sleep(WATCH_S)
            try:
                size = os.path.getsize(self.path)
            except OSError:
                continue
            if size < self.offset:  # new game session
                self.offset = 0
            if size == self.offset:
                continue
            try:
                lines, self.offset = _lines(self.path, self.offset)
            except OSError:
                continue
            for line in lines:
                if "Contract Accepted" not in line:
                    continue
                ev = parse_line(line)
                if ev and ev["type"] == "accepted" and ev.get("mission") and self.enabled():
                    try:
                        self._read(ev["mission"])
                    except Exception as e:  # never let the thread die
                        ocr.debug(f"accept {ev['mission'][:8]}: failed: {e!r}")
                        self.on_pages(ev["mission"], [], str(e)[:80], True)

    def _read(self, mission):
        if not windows.game_in_front():
            ocr.debug(f"accept {mission[:8]}: game not in front, no screenshot")
            return
        t0, hwnd, found = time.monotonic(), self.overlay_hwnd(), False
        for n, at in enumerate(SHOTS_S):
            time.sleep(max(0.0, at - (time.monotonic() - t0)))
            if not windows.game_in_front():
                ocr.debug(f"accept {mission[:8]}: game left the front, stopped")
                break
            path = ocr.temp_image(10 + n)
            windows.exclude_from_capture(hwnd, True)
            try:
                taken = ocr.capture_foreground(path)
            finally:
                windows.exclude_from_capture(hwnd, False)
            if not taken:
                continue
            try:
                lines = ocr.read_image(path)
            finally:
                path.unlink(missing_ok=True)
            page = ocr.parse_objectives(lines)
            found = found or bool(page)
            ocr.debug(f"accept {mission[:8]}: shot {n} at {time.monotonic() - t0:.1f}s: {ocr.describe(page)}"
                      f"  [raw: {ocr.raw_objectives(lines) or ' | '.join(lines[:8])}]")
            # Each page as soon as read: the overlay fills in at once.
            self.on_pages(mission, [page], None, False)
        self.on_pages(mission, [], None if found else "no objectives on screen", True)
