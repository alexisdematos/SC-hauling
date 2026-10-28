"""Type a chat command in the game, the way a player would.

Opt-in only (overlay menu). It sends key presses with Windows SendInput,
as scan codes, at a human typing speed, and only when the Star Citizen
window is in front. It is still automated input into the game: the player
chooses to turn it on.
"""
import ctypes
import random
import time
from ctypes import wintypes

from .windows import available, game_in_front  # noqa: F401  (available: used by the overlay)

ENTER_SCAN = 0x1C  # opens the chat, then sends the line
ESCAPE_SCAN = 0x01  # closes the chat, dropping what was typed
SHIFT_SCAN = 0x2A

# Wait for the player's hands off the keyboard (no key down for QUIET_S),
# at most WAIT_QUIET_S, before typing anything.
QUIET_S = 1.0
WAIT_QUIET_S = 15.0
POLL_S = 0.05
# Virtual-key codes we watch: every key, not the mouse buttons (1-6).
_WATCHED_VK = range(0x08, 0xFF)

# Human-like pace: 80-160 ms between keys, keys held 30-60 ms.
GAP_S = (0.08, 0.16)
HOLD_S = (0.03, 0.06)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member; pad so the size matches on 64-bit.
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]




def _send(scan, up):
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(0, scan, flags, 0, 0)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _tap(scan, shift=False):
    if shift:
        _send(SHIFT_SCAN, False)
        time.sleep(random.uniform(*HOLD_S))
    _send(scan, False)
    time.sleep(random.uniform(*HOLD_S))
    _send(scan, True)
    if shift:
        time.sleep(random.uniform(*HOLD_S))
        _send(SHIFT_SCAN, True)
    time.sleep(random.uniform(*GAP_S))


def _char_scan(ch):
    """(scan code, shift) for a character on the current keyboard layout."""
    user32 = ctypes.windll.user32
    user32.VkKeyScanW.restype = ctypes.c_short
    vk = user32.VkKeyScanW(ord(ch))
    if vk == -1:
        raise ValueError(f"no key for {ch!r} on this layout")
    shift = bool((vk >> 8) & 1)
    scan = user32.MapVirtualKeyW(vk & 0xFF, MAPVK_VK_TO_VSC)
    return scan, shift


def player_pressing():
    """True while the player holds any key (Z to walk, Shift to run...)."""
    state = ctypes.windll.user32.GetAsyncKeyState
    return any(state(vk) & 0x8000 for vk in _WATCHED_VK)


def wait_quiet(quiet_s=QUIET_S, timeout_s=WAIT_QUIET_S, pressing=None, clock=time.monotonic, sleep=time.sleep):
    """Wait until no key has been down for quiet_s. False after timeout_s."""
    pressing = pressing or player_pressing
    start = last_busy = clock()
    while clock() - start < timeout_s:
        if pressing():
            last_busy = clock()
        elif clock() - last_busy >= quiet_s:
            return True
        sleep(POLL_S)
    return False


class Interrupted(Exception):
    """The player pressed a key while we were typing."""


def chat_command(command):
    """Open the chat (Enter), type the command, send it (Enter).

    /showlocation works in the chat, not in the console: only the chat
    copies the coordinates. Waits for the player to let go of the keyboard
    first; if the player presses a key while we type, closes the chat
    (Escape) without sending. Returns True when sent.
    """
    if not available() or not game_in_front():
        return False
    keys = [_char_scan(ch) for ch in command]  # fail before typing anything
    if not wait_quiet() or not game_in_front():
        return False

    def tap(scan, shift=False):
        _tap(scan, shift)
        # Our own key is released by now: a key down is the player's.
        if player_pressing():
            raise Interrupted()

    try:
        tap(ENTER_SCAN)
        time.sleep(random.uniform(0.25, 0.4))  # the chat field takes focus
        for scan, shift in keys:
            tap(scan, shift)
    except Interrupted:
        _tap(ESCAPE_SCAN)
        return False
    _tap(ENTER_SCAN)
    return True
