"""Read the contract page of the mobiGlas from the screen (Windows OCR).

The log never says where each cargo is picked up; the contract page does
("Deliver 0/12 SCU of Party Favors to Grim HEX…" then "Pick up at Seraphim
Station"). Right after "Contract Accepted" that page is on screen: we take a
few screenshots of the game window and read them with the OCR engine built
into Windows 10/11, through PowerShell, so nothing needs installing.

Passive: it reads pixels like a screen recorder. Nothing is sent to the game.
"""
import base64
import ctypes
import json
import re
import struct
import subprocess
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

# OCR server: one PowerShell process kept open (its start costs about a
# second), reading image paths on stdin and printing, for each, one JSON list
# of lines. English first: the game text is English whatever the Windows
# language.
_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
[Console]::InputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $task.Wait(-1)
    $task.Result
}
$lang = [Windows.Globalization.Language]::new('en-US')
if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
} else {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
while ($null -ne ($path = [Console]::In.ReadLine())) {
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $stream.Dispose()
        [Console]::Out.WriteLine((ConvertTo-Json -Compress @(@($result.Lines | ForEach-Object { $_.Text }))))
    } catch {
        [Console]::Out.WriteLine((ConvertTo-Json -Compress @{ error = "$_" }))
    }
    [Console]::Out.Flush()
}
"""

CREATE_NO_WINDOW = 0x08000000


class _Server:
    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    def _start(self):
        encoded = base64.b64encode(_PS.encode("utf-16-le")).decode("ascii")
        self._proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace")

    def read(self, path):
        """OCR lines of one image file. Raises OSError on failure."""
        with self._lock:
            for attempt in (1, 2):  # once more with a fresh process if it died
                if self._proc is None or self._proc.poll() is not None:
                    self._start()
                try:
                    self._proc.stdin.write(str(Path(path).resolve()) + "\n")
                    self._proc.stdin.flush()
                    answer = self._proc.stdout.readline()
                except OSError:
                    answer = ""
                if answer.strip():
                    break
                self._proc = None
            else:
                raise OSError("the OCR process stopped")
        data = json.loads(answer)
        if isinstance(data, dict):
            raise OSError(data.get("error", "OCR failed")[:300])
        return [data] if isinstance(data, str) else data

    def warm_up(self):
        """Start the process ahead (PowerShell takes about a second)."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()


_server = _Server()
read_image = _server.read
warm_up = _server.warm_up


def read_images(paths):
    """OCR lines of each image file, in order. Raises OSError on failure."""
    return [read_image(p) for p in paths]


# --- what the page says ---------------------------------------------------

# The page's diamonds come out as "O", "o" or "◇" before each objective.
_BULLET = re.compile(r"^\s*(?:[Oo0◇◆•·○●]\s+)+")
# "SCU" comes out as "SCIJ" or "5CU" at times: any short word there.
# The destination (in blue) may come out as a line of its own, elsewhere in
# the OCR text: "Deliver 0/8 SCU of Gold to" alone is a whole objective.
_DELIVER = re.compile(r"(?i)^Deliver\s+(\d+)\s*/\s*(\d+)\s*\S{2,4}\s+of\s+(.+?)\s+to\b\s*(.*)$")
# "Pick up at Levski" (Smart Citizen / our user.ini), "Collect … from Levski" (stock)
_PICKUP = re.compile(r"(?i)^(?:Pick\s*up\s+(?:at|from)|Collect\s+(?:.+?\s+)?from)\s+(.+)$")
# Longer than this is OCR text from elsewhere on screen glued to a line.
MAX_WRAPS = 2
MAX_ITEM = 40
MAX_PLACE = 80


# A delivery and its pickup read as one line, or a diamond read as a stray
# "<>" or "C)" in front: cut before each verb.
_VERB = re.compile(r"(?i)(?=\b(?:Deliver\s+\d|Pick\s*up\s+(?:at|from)\b|Collect\b))")
MAX_JUNK = 4


def _clean(line):
    line = _BULLET.sub("", line.strip())
    return line.replace("•", "'").replace("’", "'").strip()


def _split(lines):
    out = []
    for line in lines:
        parts = [p.strip() for p in _VERB.split(_clean(line)) if p.strip()]
        # A short scrap before the first verb is a misread diamond.
        if len(parts) > 1 and len(parts[0]) <= MAX_JUNK and not _VERB.match(parts[0]):
            parts = parts[1:]
        # The next objective's diamond, left at the end of a cut part.
        out += [re.sub(r"\s+[Oo0◇◆•·○●]$", "", _clean(p)) for p in parts]
    return out


def parse_objectives(lines):
    """Deliveries on a contract page: [{item, total, dest, origin}], in page
    order. A delivery may wrap over a few lines, up to its full stop; its
    pickup is the line after it."""
    lines = _split(lines)
    for i, line in enumerate(lines):
        if re.match(r"(?i)^primary\s+objectives", line):
            lines = lines[i + 1:]
            break
    out, current, i = [], None, 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"(?i)^Deliver\b", line):
            # Join the wrapped lines of this delivery, up to its full stop: a
            # place name wraps over two lines at most. It is whole when it
            # ends with its full stop, or when its pickup line follows (the
            # OCR often misses a full stop). A page still drawing
            # ("…Audio-Visual Equipm", then other text) is neither: drop it.
            wraps = 0
            while not line.endswith(".") and wraps < MAX_WRAPS and i + 1 < len(lines) and not re.match(
                    r"(?i)^(Deliver|Pick\s*up|Collect)\b", lines[i + 1]):
                i += 1
                wraps += 1
                line += " " + lines[i]
            whole = line.endswith(".") or (i + 1 < len(lines) and _PICKUP.match(lines[i + 1]))
            d = _DELIVER.match(line) if whole else None
            current = None
            if d and len(d.group(3)) <= MAX_ITEM and len(d.group(4)) <= MAX_PLACE and int(d.group(2)) > 0:
                current = {"item": d.group(3).strip(" ."), "total": int(d.group(2)),
                           "dest": d.group(4).strip(" .") or None, "origin": None}
                out.append(current)
        elif current is not None and current["origin"] is None:
            p = _PICKUP.match(line)
            if p:
                current["origin"] = p.group(1).strip(" .")
            else:
                # A scrap of the destination may sit between the delivery and
                # its pickup ("NB Int.", "Spaceport on"); more is the list's end.
                current["strays"] = current.get("strays", 0) + 1
                if current["strays"] > MAX_WRAPS:
                    current = None
        i += 1
    for e in out:
        e.pop("strays", None)
    return out


def merge_pages(pages):
    """One list from the objectives of several screenshots of the same page:
    the fullest one, plus what only the others read (a line the OCR missed
    once, or one still fading in), each cargo once."""
    pages = sorted(pages, key=len, reverse=True)
    out = list(pages[0]) if pages else []
    seen = {(e["item"].lower(), e["total"]) for e in out}

    def cut_short(e):
        # "Audio-Visual Equipm": a line read while the page was drawing.
        item = e["item"].lower()
        return any(o["total"] == e["total"] and o["item"].lower() != item and o["item"].lower().startswith(item)
                   for p in pages for o in p)

    out = [e for e in out if not cut_short(e)]
    for page in pages[1:]:
        for e in page:
            key = (e["item"].lower(), e["total"])
            if cut_short(e):
                continue
            if key not in seen:
                seen.add(key)
                out.append(e)
            elif not e.get("origin"):
                continue
            else:  # fill an origin the fullest read missed
                for o in out:
                    if (o["item"].lower(), o["total"]) == key and not o.get("origin"):
                        o["origin"] = e["origin"]
    return out


# --- screenshots ----------------------------------------------------------

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


# The objectives sit on the right of the contract page (from 64 % of the
# width at 16:9, the mobiGlas stays centred in ultrawide): the right half is
# enough, and reads twice as fast as the whole window.
PAGE_LEFT = 0.5


def capture_foreground(path, left=PAGE_LEFT):
    """Save the foreground window (the game, borderless) as a .bmp file."""
    return capture_window(ctypes.windll.user32.GetForegroundWindow(), path, left)


def capture_window(hwnd, path, left=PAGE_LEFT):
    """Save what is on screen inside a window as a .bmp file, from `left`
    (a share of its width) to its right edge.

    A borderless game is composed by Windows, so the screen holds its
    pixels, whichever window has the focus (windows over it show too: leave
    ours out with windows.exclude_from_capture). Returns False when there
    is no window to capture."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    # Real pixels, not the DPI-scaled ones, for this thread only.
    try:
        old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        old = None
    try:
        rect = wintypes.RECT()
        if not hwnd or not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return False
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 200 or h < 200:
            return False
        skip = int(w * left)
        origin.x += skip
        w -= skip
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
        gdi32.SelectObject(mem, bmp)
        gdi32.BitBlt(mem, 0, 0, w, h, screen, origin.x, origin.y, 0x00CC0020)  # SRCCOPY
        header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, pixels, ctypes.byref(header), 0)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
    finally:
        if old is not None:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
    size = 14 + 40 + len(pixels.raw)
    with open(path, "wb") as f:
        f.write(b"BM" + struct.pack("<IHHI", size, 0, 0, 54))
        f.write(bytes(header))
        f.write(pixels.raw)
    return True


def temp_image(n):
    return Path(tempfile.gettempdir()) / f"sc-hauling-page-{n}.bmp"


DEBUG_MAX_BYTES = 200_000


def debug(message):
    """One line in ocr-debug.log (in %APPDATA%/SC-hauling): what each page read
    found and what was done with it, to see why a read did not help."""
    from . import config
    path = config.folder() / "ocr-debug.log"
    try:
        if path.is_file() and path.stat().st_size > DEBUG_MAX_BYTES:
            path.replace(path.with_suffix(".old.log"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


def describe(entries):
    return "; ".join(f"{e['total']} {e['item']} <- {e.get('origin')}" for e in entries) or "nothing"


def raw_objectives(lines):
    """The OCR lines that look like objectives, as read, for the debug log."""
    return " | ".join(l for l in lines if re.search(r"(?i)deliver|pick\s*up|collect|primary", l))[:1500]
