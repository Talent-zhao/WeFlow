"""Low-level human-like character typing engine using Windows SendInput API.

Uses KEYEVENTF_UNICODE to type CJK characters directly — no IME, no clipboard.
With variable-speed typing, bursts, sentence pauses, and jitter, the output
rhythm is statistically indistinguishable from a human typing.

This module is a dependency of wechat_send.py (which handles window management).
Do not use this module directly for sending messages — use wechat_send.py instead.
"""

import ctypes
from ctypes import wintypes
import random
import time
import logging

logger = logging.getLogger("human-send")

# ── Windows API constants ──
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002

# ── Delay ranges (milliseconds) ──
CHAR_DELAY_CJK_MIN = 50
CHAR_DELAY_CJK_MAX = 180
CHAR_DELAY_ASCII_MIN = 25
CHAR_DELAY_ASCII_MAX = 100
SENTENCE_PAUSE_MIN = 350
SENTENCE_PAUSE_MAX = 850
PARAGRAPH_PAUSE_MIN = 700
PARAGRAPH_PAUSE_MAX = 1600
BURST_PROBABILITY = 0.15
BURST_SPEED_MULTIPLIER = 0.35
THINKING_PAUSE_PROBABILITY = 0.05
THINKING_PAUSE_MIN = 400
THINKING_PAUSE_MAX = 1400
WARMUP_DELAY_MIN = 200
WARMUP_DELAY_MAX = 800
ENTER_DELAY_MIN = 80
ENTER_DELAY_MAX = 350

# ── Anti-pattern: delay history to prevent recognizable sequences ──
_last_delays: list[float] = []
_MAX_DELAY_HISTORY = 14

# ── Windows API structs (sizeof(INPUT) == 40 on x64) ──

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    ]


def _add_jitter(base_ms: float) -> float:
    """Return a jittered delay in seconds, avoiding consecutive identical delays."""
    jitter = random.uniform(-0.30, 0.30) * base_ms
    delay = base_ms + jitter
    # Prevent identical consecutive delays
    if _last_delays and abs(delay - _last_delays[-1]) < 6:
        delay += random.uniform(8, 25)
    _last_delays.append(delay)
    if len(_last_delays) > _MAX_DELAY_HISTORY:
        _last_delays.pop(0)
    return max(6, delay) / 1000.0


def send_unicode_char(ch: str) -> bool:
    """Send a single Unicode character via SendInput(KEYEVENTF_UNICODE).

    Returns True on success, False if SendInput reports 0 inputs inserted.
    """
    code = ord(ch)

    inp_down = INPUT()
    inp_down.type = INPUT_KEYBOARD
    inp_down.u.ki.wVk = 0
    inp_down.u.ki.wScan = code
    inp_down.u.ki.dwFlags = KEYEVENTF_UNICODE
    inp_down.u.ki.time = 0
    inp_down.u.ki.dwExtraInfo = 0

    inp_up = INPUT()
    inp_up.type = INPUT_KEYBOARD
    inp_up.u.ki.wVk = 0
    inp_up.u.ki.wScan = code
    inp_up.u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
    inp_up.u.ki.time = 0
    inp_up.u.ki.dwExtraInfo = 0

    inputs = (INPUT * 2)(inp_down, inp_up)
    inserted = ctypes.windll.user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    return inserted == 2


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
            0x20000 <= cp <= 0x2A6DF or 0xF900 <= cp <= 0xFAFF)


def _is_sentence_end(ch: str) -> bool:
    return ch in '。！？!?.\n'


def _is_punctuation(ch: str) -> bool:
    return ch in '。！？，、；：""''（）…—…、,.:;!?-()[]{}""''《》【】'


def type_message_human(text: str) -> float:
    """Type a message character-by-character with human-like variable rhythm.

    Returns total time spent typing (seconds), useful for UI estimates.
    """
    total_elapsed = 0.0
    chars = list(text)
    n = len(chars)
    i = 0

    # Burst state
    burst_remaining = 0

    while i < n:
        ch = chars[i]

        # ── Maybe start a burst ──
        if burst_remaining <= 0 and random.random() < BURST_PROBABILITY:
            burst_remaining = random.randint(2, 5)

        # ── Determine base delay ──
        if _is_cjk(ch):
            base = random.uniform(CHAR_DELAY_CJK_MIN, CHAR_DELAY_CJK_MAX)
        else:
            base = random.uniform(CHAR_DELAY_ASCII_MIN, CHAR_DELAY_ASCII_MAX)

        if _is_punctuation(ch):
            base *= 1.6

        if burst_remaining > 0:
            base *= BURST_SPEED_MULTIPLIER
            burst_remaining -= 1

        # ── Send the character ──
        if not send_unicode_char(ch):
            logger.warning("SendInput failed for U+%04X (%s)", ord(ch), ch)

        # ── Apply delay ──
        delay = _add_jitter(base)
        time.sleep(delay)
        total_elapsed += delay

        # ── Sentence-end pause ──
        if _is_sentence_end(ch):
            pause = random.uniform(SENTENCE_PAUSE_MIN, SENTENCE_PAUSE_MAX) / 1000.0
            time.sleep(pause)
            total_elapsed += pause

        # ── Paragraph pause ──
        if ch == '\n' and i + 1 < n and chars[i + 1] == '\n':
            pause = random.uniform(PARAGRAPH_PAUSE_MIN, PARAGRAPH_PAUSE_MAX) / 1000.0
            time.sleep(pause)
            total_elapsed += pause

        # ── Thinking pause ──
        if _is_sentence_end(ch) and random.random() < THINKING_PAUSE_PROBABILITY:
            pause = random.uniform(THINKING_PAUSE_MIN, THINKING_PAUSE_MAX) / 1000.0
            logger.debug("Thinking pause: %.0fms", pause * 1000)
            time.sleep(pause)
            total_elapsed += pause

        i += 1

    return total_elapsed






def estimate_typing_duration(text: str) -> float:
    """Estimate how many seconds this message will take to type (for UI display)."""
    total = 0.0
    for ch in text:
        if _is_cjk(ch):
            base = (CHAR_DELAY_CJK_MIN + CHAR_DELAY_CJK_MAX) / 2
        else:
            base = (CHAR_DELAY_ASCII_MIN + CHAR_DELAY_ASCII_MAX) / 2
        if _is_punctuation(ch):
            base *= 1.6
        total += base
    sentence_count = sum(1 for ch in text if _is_sentence_end(ch))
    total += sentence_count * (SENTENCE_PAUSE_MIN + SENTENCE_PAUSE_MAX) / 2
    total += (WARMUP_DELAY_MIN + WARMUP_DELAY_MAX) / 2
    total += (ENTER_DELAY_MIN + ENTER_DELAY_MAX) / 2
    return round(total / 1000.0, 1)
