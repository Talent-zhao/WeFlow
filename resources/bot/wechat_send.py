"""Unified WeChat message sending module.

Provides two tiers:
  Tier 1 (primitives) — reusable window-management building blocks
  Tier 2 (complete)   — full send functions for bot_reply_server.py

Both clipboard paste and human-like SendInput typing follow the same
6-step protocol: find → activate → click input → clear → enter text → Enter.
"""

import ctypes
from ctypes import wintypes
import logging
import random
import time

import pyautogui
import pyperclip

logger = logging.getLogger("wechat-send")

# ── Tier 1: Primitives ──────────────────────────────────────────────

def find_wechat_window() -> int | None:
    """Find the WeChat main window and return its HWND, or None."""
    wechat_hwnd = None

    def _enum_callback(hwnd: int, _ctx):
        nonlocal wechat_hwnd
        try:
            import win32gui
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            if title == "微信" or cls == "Qt51514QWindowIcon":
                wechat_hwnd = hwnd
                return False
        except Exception:
            pass
        return True

    try:
        import win32gui
        import win32con
        win32gui.EnumWindows(_enum_callback, None)
    except Exception as e:
        logger.warning("Failed to enumerate windows: %s", e)

    return wechat_hwnd


def activate_window(hwnd: int) -> None:
    """Restore and bring a window to the foreground.

    Windows restricts SetForegroundWindow for background processes.  We use
    the Alt-key trick to get foreground activation rights before calling it.
    """
    import win32gui
    import win32con

    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    # Trick Windows into granting foreground rights to this process:
    # simulate a modifier key press, which the system treats as user input.
    ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)        # Alt down
    ctypes.windll.user32.keybd_event(0x12, 0, 0x0002, 0)   # Alt up
    time.sleep(0.03)

    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.15)


def click_input_area(hwnd: int) -> None:
    """Click the text input area of the WeChat window.

    WeChat 4.x layout (from window bottom):
      0-30px   — window frame
      30-80px  — toolbar (emoji, screenshot, file, etc.)
      80-220px — text input area
    Clicking at bottom-50 hits the toolbar (triggers screenshot!).
    bottom-130 lands in the middle of the text input area.
    """
    import win32gui
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    cx = (left + right) // 2
    cy = bottom - 130
    pyautogui.click(cx, cy)
    time.sleep(0.1)


def clear_input() -> None:
    """Select all text and delete it in the active input field."""
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.04)
    pyautogui.press("backspace")
    time.sleep(0.04)


def release_modifiers() -> None:
    """Release LCtrl, RCtrl, LAlt, RAlt to fix stuck modifier keys.

    When the user presses Ctrl+Enter in WeFlow, the Ctrl key may still be
    physically held when Python steals focus to WeChat.  This sends explicit
    KEYUP events so subsequent keyboard simulation starts from a clean state.
    """
    VK_LCONTROL = 0xA2
    VK_RCONTROL = 0xA3
    VK_LMENU = 0xA4
    VK_RMENU = 0xA5
    KEYEVENTF_KEYUP = 0x0002
    for vk in (VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU):
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)


def paste_text_at(text: str, x: int, y: int) -> bool:
    """Click at screen coords, clear, paste text, and press Enter.

    Used by bot_image.py which manages its own window activation and
    coordinate calculation.  Does NOT find or focus a window.
    """
    pyautogui.click(x, y)
    time.sleep(0.2)
    clear_input()
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)
    pyautogui.press("enter")
    return True


# ── Tier 1b: Chat navigation ────────────────────────────────────────

def navigate_to_chat(contact_name: str, hwnd: int) -> bool:
    """Open a contact's chat in WeChat by searching with Ctrl+F.

    Uses WeChat's built-in search to find and open the correct contact's chat.
    This ensures replies go to the intended recipient even when multiple people
    message at the same time.
    """
    import win32gui

    if not contact_name:
        return False

    # Save current clipboard to restore later
    try:
        old_clipboard = pyperclip.paste()
    except Exception:
        old_clipboard = ""

    try:
        # Ctrl+F to open WeChat's search box
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.35)

        # 文件传输助手的搜索关键词只取"文件"两字，全称可能匹配不到
        search_keyword = "文件" if contact_name == "文件传输助手" else contact_name
        pyperclip.copy(search_keyword)
        time.sleep(0.05)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.65)  # Wait for search results to populate

        # Press Enter to open the first search result
        pyautogui.press("enter")
        time.sleep(0.45)  # Wait for chat to load

        logger.info("Navigated to chat: %s", contact_name)
        return True
    except Exception as e:
        logger.warning("Failed to navigate to chat '%s': %s", contact_name, e)
        return False
    finally:
        # Restore original clipboard
        try:
            pyperclip.copy(old_clipboard)
        except Exception:
            pass


# ── Message splitting ────────────────────────────────────────────────

def _split_long_sentence(text: str, max_len: int) -> list[str]:
    """Split a single paragraph that exceeds max_len into smaller pieces."""
    # Split by sentence-ending punctuation
    sentences: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？!?":
            sentences.append(buf)
            buf = ""
    if buf.strip():
        sentences.append(buf)

    # For any sentence still too long, split by clause punctuation
    result: list[str] = []
    for sent in sentences:
        if len(sent) <= max_len:
            if sent.strip():
                result.append(sent)
            continue

        pieces: list[str] = []
        buf2 = ""
        for ch in sent:
            buf2 += ch
            if ch in "，,；;—…":
                pieces.append(buf2)
                buf2 = ""
        if buf2.strip():
            pieces.append(buf2)

        merged = ""
        for p in pieces:
            if len(merged) + len(p) <= max_len:
                merged += p
            else:
                if merged.strip():
                    result.append(merged)
                merged = p
        if merged.strip():
            result.append(merged)

    # Merge very short trailing pieces into previous
    final: list[str] = []
    for seg in result:
        seg = seg.strip()
        if not seg:
            continue
        if len(seg) <= 6 and final:
            final[-1] = final[-1] + seg
        else:
            final.append(seg)

    return final if final else [text]


def split_for_wechat(text: str, max_len: int = 50) -> list[str]:
    """Split text into natural segments for human-like WeChat sending.

    Paragraph breaks (\\n) always start a new message.  Long paragraphs
    are further split at sentence/clause boundaries.
    """
    text = text.strip()
    if not text:
        return [text]

    # Step 0: paragraph breaks always start new messages
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l]

    if len(lines) <= 1:
        # Single paragraph — split only if too long
        if len(text) <= max_len:
            return [text]
        return _split_long_sentence(text, max_len)

    # Multiple paragraphs — each is a separate message
    result: list[str] = []
    for line in lines:
        if len(line) <= max_len:
            result.append(line)
        else:
            result.extend(_split_long_sentence(line, max_len))

    return result if result else [text]


# ── Tier 2: Complete send functions ─────────────────────────────────

def send_clipboard(text: str, hwnd: int | None = None,
                   contact: str | None = None) -> bool:
    """Send text into WeChat via clipboard paste (fast, for auto-reply).

    If contact is provided, navigates to that contact's chat first.
    Otherwise sends to the currently open chat.
    """
    if hwnd is None:
        hwnd = find_wechat_window()
    if not hwnd:
        logger.error("WeChat window not found")
        return False

    activate_window(hwnd)

    if contact:
        navigate_to_chat(contact, hwnd)

    click_input_area(hwnd)
    clear_input()
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)
    pyautogui.press("enter")
    logger.info("Clipboard sent to %s: %s", contact or "(current)", text[:80])
    return True


def send_typing(text: str, hwnd: int | None = None,
                contact: str | None = None) -> bool:
    """Send text into WeChat with human-like timing (for manual reply).

    If contact is provided, navigates to that contact's chat first.
    Calls release_modifiers() after activation to fix stuck Ctrl from
    the frontend Ctrl+Enter hotkey.
    """
    if hwnd is None:
        hwnd = find_wechat_window()
    if not hwnd:
        logger.error("WeChat window not found")
        return False

    activate_window(hwnd)
    release_modifiers()

    if contact:
        navigate_to_chat(contact, hwnd)

    # Brief warm-up (simulates reading before replying)
    warmup = random.uniform(0.3, 0.9)
    time.sleep(warmup)

    click_input_area(hwnd)
    clear_input()

    text_stripped = text.strip()
    pyperclip.copy(text_stripped)
    time.sleep(0.06)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.25)
    logger.info("Sent (typing mode) to %s: %s", contact or "(current)",
                text_stripped[:80])

    enter_delay = random.uniform(0.08, 0.35)
    time.sleep(enter_delay)
    pyautogui.press("enter")
    return True
