"""WeChat bot using screenshot + Windows OCR + keyboard automation.

Works with ANY WeChat version (3.x, 4.x, Qt5) since it doesn't rely on UIA.
Requires: WeChat window visible on screen, Windows 10/11.
"""

import logging
import time
import random
from typing import Optional
from collections import defaultdict

import numpy as np
import win32gui
import win32con
import win32ui
import pyautogui
import pyperclip
from PIL import Image

from config import (
    ANTHROPIC_API_KEY,
    REPLY_DELAY_MIN,
    REPLY_DELAY_MAX,
    REPLY_COOLDOWN,
    POSITION_CACHE_TTL,
    DEBUG,
    CHAT_SWITCH_COOLDOWN,
    RED_DOT_MIN_PIXELS,
    UNREAD_SCAN_INTERVAL,
    USER_IDLE_SECONDS,
    USER_MOUSE_THRESHOLD,
    USER_ACTIVITY_COOLDOWN,
    STALE_CONVERSATION_AGE,
    DEDUP_CLEANUP_INTERVAL,
)
from claude_client import ClaudeClient
from zhaoyoucai_prompt import SYSTEM_PROMPT
from contacts import lookup, describe, find_mentioned_contacts, describe_mentioned
from conversation_manager import normalize_contact
from display import ConsoleDisplay, GRAY, RESET, CYAN, MAGENTA, YELLOW, GREEN, RED, BLUE
from ocr_engine import (
    ocr_image,
    ocr_image_detailed,
    ensure_readable,
    active_backend,
    OCR_MIN_DIMENSION,
    OCR_SCALE_FACTOR,
)
from ws_server import BotWebSocketServer

logger = logging.getLogger("wechat-bot-img")

WECHAT_WINDOW_TITLE = "微信"
WECHAT_WINDOW_CLASS = "Qt51514QWindowIcon"


class ZhaoyoucaiImageBot:
    """赵有才微信机器人 — screenshot/OCR backend for WeChat 4.x Qt5."""

    def __init__(self, verbose: bool = True,
                 chat_list_rect: tuple | None = None,
                 conversation_rect: tuple | None = None,
                 ws_server: BotWebSocketServer | None = None):
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY not set")

        self.verbose = verbose
        self.disp = ConsoleDisplay(verbose=verbose)
        self.claude = ClaudeClient()
        self.ws = ws_server

        self._paused = False
        self._stop_requested = False
        self._start_time = time.time()

        # Warm up the OCR engine on startup
        backend = active_backend()
        logger.info(f"OCR backend: {backend}")

        self._hwnd = None
        self._last_messages: dict[str, set[str]] = defaultdict(set)
        self._last_sent: dict[str, str] = {}
        self._last_unread_scan: float = 0.0
        self._known_unread_positions: dict[int, float] = {}  # screen_y → timestamp
        self._divider_x: float = 0.55
        self._last_mouse_pos: tuple[int, int] = (0, 0)
        self._last_mouse_move_time: float = 0.0
        self._user_active_until: float = 0.0
        self._scan_cycle: int = 0  # for periodic maintenance scheduling

        # Manual sub-regions (set via --select).  When both are provided the
        # auto-calibration is skipped and divider_x becomes irrelevant.
        self._manual_chat_list_rect = chat_list_rect
        self._manual_conversation_rect = conversation_rect

        self._find_window()
        self._window_rect = win32gui.GetWindowRect(self._hwnd)

        if self._manual_chat_list_rect and self._manual_conversation_rect:
            # Use the union of both manual rects as the effective capture area
            cl = self._manual_chat_list_rect
            cv = self._manual_conversation_rect
            self._effective_rect = (
                min(cl[0], cv[0]), min(cl[1], cv[1]),
                max(cl[2], cv[2]), max(cl[3], cv[3]),
            )
            logger.info(
                f"Using manual regions: chat={cl[2]-cl[0]}x{cl[3]-cl[1]}, "
                f"conv={cv[2]-cv[0]}x{cv[3]-cv[1]}"
            )
        else:
            self._effective_rect = self._window_rect
            self._calibrate_layout()

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------

    def _find_window(self):
        """Find WeChat window via title/class, with fallback to WindowFromPoint.

        When manual regions are set, the window under the selection center takes
        priority — the user may have selected a window that doesn't match the
        standard WeChat title/class.
        """
        # When manual regions are set, try the window at the selection center first
        manual = self._manual_chat_list_rect or self._manual_conversation_rect
        if manual:
            cx = (manual[0] + manual[2]) // 2
            cy = (manual[1] + manual[3]) // 2
            hwnd = win32gui.WindowFromPoint((cx, cy))
            while hwnd:
                parent = win32gui.GetParent(hwnd)
                if not parent:
                    break
                hwnd = parent
            if hwnd and win32gui.IsWindowVisible(hwnd):
                self._hwnd = hwnd
                logger.info(
                    f"Using window under selection: HWND={self._hwnd}, "
                    f"title='{win32gui.GetWindowText(self._hwnd)}'"
                )
                return

        matches = []
        def enum_cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                cls = win32gui.GetClassName(hwnd)
                if title == WECHAT_WINDOW_TITLE or cls == WECHAT_WINDOW_CLASS:
                    matches.append((hwnd, title, cls))
            return True
        win32gui.EnumWindows(enum_cb, None)

        if matches:
            self._hwnd = matches[0][0]
            logger.info(f"Found WeChat window: HWND={self._hwnd}")
            return

        raise RuntimeError("WeChat window not found. Is WeChat running?")

    def _activate(self, polite: bool = True):
        """Restore and focus the WeChat window.

        When polite=True and user is active, skips SetForegroundWindow to
        avoid stealing focus. Screenshots still work on obscured windows.
        """
        if polite and self._is_user_active():
            if DEBUG:
                logger.debug("User active, skipping window activation")
            return
        win32gui.ShowWindow(self._hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(self._hwnd)
        time.sleep(0.3)

    def _get_rect(self) -> tuple:
        """Return the effective capture region in screen coordinates.

        When --select was used this is the user-drawn rectangle.  Otherwise
        it's the full WeChat window rect.
        """
        return self._effective_rect

    @property
    def _chat_list_rect(self) -> tuple:
        """Screen rect for the chat list panel (contacts + previews)."""
        if self._manual_chat_list_rect:
            return self._manual_chat_list_rect
        r = self._get_rect()
        return (r[0], r[1],
                r[0] + int((r[2] - r[0]) * self._divider_x),
                r[3])

    @property
    def _conversation_rect(self) -> tuple:
        """Screen rect for the conversation panel (messages + input)."""
        if self._manual_conversation_rect:
            return self._manual_conversation_rect
        r = self._get_rect()
        return (r[0] + int((r[2] - r[0]) * self._divider_x), r[1],
                r[2], r[3])

    def _capture_region(self, rect: tuple,
                        left_pct: float, top_pct: float,
                        right_pct: float, bottom_pct: float) -> Image.Image:
        """Capture a percentage-defined sub-region of a given screen rect."""
        rx, ry, rr, rb = rect
        rw = rr - rx
        rh = rb - ry
        bbox = (
            rx + int(rw * left_pct),
            ry + int(rh * top_pct),
            rx + int(rw * right_pct),
            ry + int(rh * bottom_pct),
        )
        return self._capture_window(bbox=bbox)

    def _check_user_activity(self):
        """Check if the user has moved the mouse recently.

        Updates internal tracking state. Call this once per poll cycle.
        """
        try:
            pos = pyautogui.position()
        except Exception:
            return

        px, py = pos
        lx, ly = self._last_mouse_pos

        # Initialize on first call
        if lx == 0 and ly == 0:
            self._last_mouse_pos = (px, py)
            return

        dist = ((px - lx) ** 2 + (py - ly) ** 2) ** 0.5
        self._last_mouse_pos = (px, py)

        if dist >= USER_MOUSE_THRESHOLD:
            self._last_mouse_move_time = time.time()
            self._user_active_until = time.time() + USER_IDLE_SECONDS + USER_ACTIVITY_COOLDOWN

    def _is_user_active(self) -> bool:
        """Return True if the user is currently using the mouse/keyboard.

        Bot should NOT steal focus, move the mouse, or type when this is True.
        """
        self._check_user_activity()
        return time.time() < self._user_active_until

    # ------------------------------------------------------------------
    # Layout calibration
    # ------------------------------------------------------------------

    def _calibrate_layout(self):
        """Detect the vertical divider between chat list and conversation panel.

        WeChat 4.x layout (left to right):
          - Icon/tab bar (~5-8% width)
          - Chat list panel (~40-50% width)
          - Conversation panel (remaining ~45-55% width)

        We scan for the divider by looking for a column with minimal text
        (few dark pixels) and consistent background color across the 38-58%
        horizontal range.  Also verifies there is content to the left of
        the candidate divider (the chat list), which prevents matching
        empty space or scrollbars on the right.
        """
        from config import DIVIDER_X as MANUAL_DIVIDER

        if MANUAL_DIVIDER is not None:
            self._divider_x = float(MANUAL_DIVIDER)
            logger.info(f"Layout: using manual divider at {self._divider_x:.1%}")
            return

        self._activate()
        rect = self._get_rect()
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]

        img = self._capture_window()

        best_x = None
        best_score = float('inf')

        # Scan 40-55% — chat-list/conversation divider range for WeChat 4.x.
        # Sample 3-pixel-wide strips to catch thin text.
        STRIP_WIDTH = 3
        for x_pct in range(40, 56):
            x_center = int(w * x_pct / 100)
            strip_samples = []
            for dx in range(-STRIP_WIDTH, STRIP_WIDTH + 1):
                sx = x_center + dx
                if sx < 0 or sx >= w:
                    continue
                for y in range(int(h * 0.10), int(h * 0.90), 3):
                    try:
                        p = img.getpixel((sx, y))
                        strip_samples.append(sum(p) / 3.0)
                    except IndexError:
                        break

            if len(strip_samples) < 40:
                continue

            mean = sum(strip_samples) / len(strip_samples)
            variance = sum((s - mean) ** 2 for s in strip_samples) / len(strip_samples)
            dark_count = sum(1 for s in strip_samples if s < 150)
            light_pct = sum(1 for s in strip_samples if s > 220) / len(strip_samples)

            # A good divider column: high light%, low dark count, low variance
            if light_pct < 0.6:
                continue

            score = variance + dark_count * 15
            if score < best_score:
                best_score = score
                best_x = x_pct / 100.0

        if best_x and best_score < 10000:
            self._divider_x = best_x
            logger.info(f"Layout calibrated: divider at {best_x:.1%} (score={best_score:.0f})")
        else:
            self._divider_x = 0.50
            logger.info("Layout calibration inconclusive, using default divider at 50%")

    # ------------------------------------------------------------------
    # Screenshot + OCR
    # ------------------------------------------------------------------

    # OCR preprocessing thresholds — imported from ocr_engine
    OCR_MIN_DIMENSION = OCR_MIN_DIMENSION
    OCR_SCALE_FACTOR = OCR_SCALE_FACTOR

    def _capture_window(self, bbox: tuple | None = None) -> Image.Image:
        """Capture the WeChat window content using BitBlt from the window DC.

        WeChat 4.x Qt5 renders via DirectComposition which ImageGrab cannot
        capture (it gets a white rectangle from the screen DC).  BitBlt
        from the window's own DC captures the hardware-accelerated surface.

        When _manual_rect is set, captures the full window but only returns
        the user-selected sub-region.  When bbox is given it further crops
        within the effective region.
        """
        # Always BitBlt from the full WeChat window (DX surfaces require it)
        win_x, win_y, win_r, win_b = self._window_rect
        win_w = win_r - win_x
        win_h = win_b - win_y

        hwnd_dc = win32gui.GetWindowDC(self._hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bitmap = win32ui.CreateBitmap()
        save_bitmap.CreateCompatibleBitmap(mfc_dc, win_w, win_h)
        save_dc.SelectObject(save_bitmap)

        save_dc.BitBlt((0, 0), (win_w, win_h), mfc_dc, (0, 0), win32con.SRCCOPY)

        bmp_info = save_bitmap.GetInfo()
        bmp_bits = save_bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            'RGB',
            (bmp_info['bmWidth'], bmp_info['bmHeight']),
            bmp_bits, 'raw', 'BGRX', 0, 1,
        )

        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(self._hwnd, hwnd_dc)

        # Crop to the effective region (manual_rect or window_rect)
        eff_x, eff_y, eff_r, eff_b = self._effective_rect
        img = img.crop((
            eff_x - win_x,
            eff_y - win_y,
            eff_r - win_x,
            eff_b - win_y,
        ))

        if bbox is not None:
            left, top, right, bottom = bbox
            img = img.crop((
                left - eff_x,
                top - eff_y,
                right - eff_x,
                bottom - eff_y,
            ))

        if DEBUG:
            img.save("C:/Users/Trouvailler/wechat-bot/debug_screenshot.png")

        return img

    def _ensure_readable(self, img: Image.Image) -> tuple[Image.Image, float]:
        """Preprocess image for reliable CJK OCR. Delegates to ocr_engine."""
        return ensure_readable(img)

    def _ocr_image(self, img: Image.Image) -> str:
        """Run OCR on a PIL image. Uses RapidOCR with Windows fallback."""
        return ocr_image(img)

    # ------------------------------------------------------------------
    # OCR with bounding boxes
    # ------------------------------------------------------------------

    def _ocr_image_detailed(self, img: Image.Image) -> list[dict]:
        """Run OCR and return per-line text with bounding box info.

        Uses RapidOCR with Windows fallback. Returns list of
        {text, y, height, x} sorted top-to-bottom.
        """
        return ocr_image_detailed(img)

    # ------------------------------------------------------------------
    # Chat list scanning (text-based activity detection)
    # ------------------------------------------------------------------

    def _scan_chat_list_activity(self) -> list[int]:
        """Scan the chat list for any sign of recent activity beyond red dots.

        Looks for "刚刚" and very recent timestamps (within 2 min) on the
        right side of each chat row. Returns sorted list of screen y-positions
        for rows with detected activity.
        """
        import re as _re

        cl = self._chat_list_rect
        cw = cl[2] - cl[0]
        ch = cl[3] - cl[1]

        # Timestamps are right-aligned in the chat list.
        # Scan the rightmost 30% of the chat list.
        region = (
            cl[0] + int(cw * 0.70),
            cl[1] + int(ch * 0.02),
            cl[2],
            cl[3] - int(ch * 0.02),
        )
        try:
            img = self._capture_window(bbox=region)
        except Exception:
            return []

        try:
            lines = self._ocr_image_detailed(img)
        except Exception:
            logger.debug("OCR failed during timestamp scan", exc_info=True)
            return []

        if not lines:
            return []

        now = time.localtime()
        current_minutes = now.tm_hour * 60 + now.tm_min

        active_y_positions = []
        zone_top = region[1]

        for L in lines:
            collapsed = L["text"].replace(" ", "").replace("　", "")

            if "刚刚" in collapsed:
                screen_y = int(zone_top + L["y"] + L["height"] / 2)
                active_y_positions.append(screen_y)
                continue

            m = _re.match(r'^(\d{1,2})[:：](\d{2})$', collapsed)
            if m:
                hh = int(m.group(1))
                mm = int(m.group(2))
                msg_minutes = hh * 60 + mm
                diff = current_minutes - msg_minutes
                if diff < -720:
                    diff += 1440
                elif diff > 720:
                    diff -= 1440
                if 0 <= diff <= 2:
                    screen_y = int(zone_top + L["y"] + L["height"] / 2)
                    active_y_positions.append(screen_y)

        if DEBUG and active_y_positions:
            logger.debug(f"Chat-list timestamp hits at y={active_y_positions}")

        return sorted(set(active_y_positions))

    # ------------------------------------------------------------------
    # Message reading
    # ------------------------------------------------------------------

    def read_last_messages(self) -> str:
        """Read the most recent messages from the currently open chat via OCR.

        Scans the conversation panel, focusing on the message area
        (upper ~80% below the title bar, above the input area).
        """
        self._activate()
        img = self._capture_region(self._conversation_rect, 0.0, 0.0, 1.0, 0.82)
        text = self._ocr_image(img)
        return text

    def read_chat_list(self) -> str:
        """Read the chat list (left panel) — useful for detecting current state."""
        self._activate()
        img = self._capture_region(self._chat_list_rect, 0.0, 0.0, 1.0, 1.0)
        return self._ocr_image(img)

    # ------------------------------------------------------------------
    # Message sending
    # ------------------------------------------------------------------

    def type_and_send(self, text: str) -> bool:
        """Type text into the chat input and press Enter to send.

        Uses clipboard paste instead of typewrite because Chinese characters
        require IME composition which simulated keystrokes cannot provide.

        Returns True if the message was sent, False if skipped (user active).
        """
        if self._is_user_active():
            if DEBUG:
                logger.debug("User active, skipping message send")
            return False

        self._activate(polite=False)  # already checked user activity above

        cv = self._conversation_rect
        input_x = cv[0] + (cv[2] - cv[0]) // 2
        input_y = cv[3] - 20  # ~20px from the bottom

        from wechat_send import paste_text_at
        result = paste_text_at(text, input_x, input_y)

        if DEBUG:
            logger.info("[Sent] %s", text)

        return result

    # ------------------------------------------------------------------
    # Claude integration
    # ------------------------------------------------------------------

    def build_prompt(self, contact: str, incoming_text: str) -> list[dict]:
        history = self.claude.convos.get_history(contact)
        recent_context = self._select_context(history, contact)

        # Look up relationship profile for the person we're talking to
        profile = lookup(contact)

        # Scan for third-party mentions: contact A talking about contact B.
        # Filter out the current conversation partner so we don't tell the
        # model "you know this person" about the person they're talking to.
        mentioned = find_mentioned_contacts(incoming_text)
        mentioned = [m for m in mentioned if m["name"] != contact
                     and m["name"] not in (profile.get("aliases", []) if profile else [])]
        third_party_note = describe_mentioned(mentioned) if mentioned else ""

        if profile:
            relationship_guide = describe(profile, contact)
            parts = [
                f"最近聊天记录：\n{recent_context}",
                f"{contact} 刚发来：{incoming_text}",
                f"─── 你与 {contact} 的关系 ───",
                relationship_guide,
            ]
            if third_party_note:
                parts.append(f"─── 消息中提到的第三人 ───")
                parts.append(third_party_note)
            user_message = "\n\n".join(parts)
        else:
            parts = [
                f"最近聊天记录：\n{recent_context}",
                f"{contact} 刚发来：{incoming_text}",
                f"这是你不熟的人。保持礼貌但有距离，回复简短。",
            ]
            if third_party_note:
                parts.append(f"─── 消息中提到的第三人 ───")
                parts.append(third_party_note)
            parts.append("直接回复你发送的内容，不要加任何前缀、标签或引号。")
            user_message = "\n\n".join(parts)

        return [{"role": "user", "content": user_message}]

    def _select_context(self, history: list[dict], contact: str,
                        char_budget: int = 1000) -> str:
        """Select representative messages within a character budget.

        Always includes the most recent messages. When budget is tight,
        skips ultra-short confirmations from older parts of the history
        to make room for more substantive context.
        """
        if not history:
            return "（无）"

        TRIVIAL_PATTERNS = {'嗯', '好', '行', 'ok', 'OK', 'Ok', '对', '是',
                            '哦', '啊', '哈', '嗯嗯', '好的', '好吧', '行吧',
                            '收到', '知道了', '明白', '懂了', '哦哦', '哈哈',
                            '1', '2', '知道了知道了'}

        # Always keep the most recent 4 messages (immediate context)
        RECENT_KEEP = 4
        recent = history[-RECENT_KEEP:] if len(history) >= RECENT_KEEP else history
        older = history[:-RECENT_KEEP] if len(history) > RECENT_KEEP else []

        def format_msg(m):
            label = "你" if m["role"] == "assistant" else contact
            return f"{label}: {m['content']}"

        # Build recent context (always included)
        recent_lines = [format_msg(m) for m in recent]
        used = sum(len(l) + 1 for l in recent_lines)

        # Add older messages, skipping trivial ones when budget is tight
        older_lines = []
        for m in reversed(older):
            line = format_msg(m)
            collapsed = m['content'].replace(" ", "").replace("　", "").strip()
            is_trivial = collapsed in TRIVIAL_PATTERNS or len(collapsed) <= 2

            if used + len(line) > char_budget:
                if is_trivial:
                    continue  # skip trivial to save budget
                break  # non-trivial but out of budget → stop

            older_lines.insert(0, line)
            used += len(line) + 1

        all_lines = older_lines + recent_lines

        if len(all_lines) < len(history):
            all_lines.insert(0, "…(更早的对话已省略)")

        return "\n".join(all_lines)

    def get_reply(self, contact: str, incoming_text: str) -> Optional[str]:
        messages = self.build_prompt(contact, incoming_text)
        reply = self.claude.get_reply(messages, SYSTEM_PROMPT)
        if reply is None:
            self.disp.api_error()
        return reply

    # ------------------------------------------------------------------
    # Main loop — single chat mode
    # ------------------------------------------------------------------

    def process_current_chat(self, contact_name: str):
        """Read the current chat, generate reply, and send it."""
        # Normalize and get stable contact name from header bar
        header_name = self._get_current_contact()
        contact = self._norm(header_name) if header_name and header_name != "未知联系人" else self._norm(contact_name)

        self.disp.event("🔍", CYAN, "单次模式", f"读取 {contact} 的聊天…")

        text = self.read_last_messages()
        if not text:
            self.disp.event("✗", RED, "OCR", "未在聊天区域检测到文字")
            return

        self.disp.ocr_dump(text)

        incoming_msgs = self._extract_incoming_messages(text)
        if not incoming_msgs:
            self.disp.event("✗", RED, "提取", "未能提取到有效消息")
            return

        for incoming in incoming_msgs:
            # Record user message immediately — context persists even if reply fails
            self.claude.convos.add_message(contact, "user", incoming)

            if self._should_skip_message(contact, incoming):
                self.disp.event("⏭", GRAY, contact, "重复或已回复")
                continue

            self.disp.msg_received(contact, incoming)

            reply = self.get_reply(contact, incoming)
            if not reply:
                self.disp.event("✗", RED, "API", "未生成回复")
                continue

            self.disp.reply_sent(contact, reply)
            self.claude.convos.add_message(contact, "assistant", reply)
            self.claude.convos.increment_reply(contact)
            self._mark_message_handled(contact, incoming, reply)

            delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
            time.sleep(delay)
            if not self.type_and_send(reply):
                continue
            time.sleep(0.8)

        # Prime seen messages so bot doesn't self-reply on next read
        try:
            prime_text = self.read_last_messages()
            if prime_text:
                for prime_msg in self._extract_incoming_messages(prime_text):
                    self._mark_message_handled(contact, prime_msg)
        except Exception:
            pass

    def run_single_chat_mode(self, contact_name: str = "联系人"):
        """Interactive mode: user opens a chat, bot reads + replies.

        Press Ctrl+C to stop.
        """
        self.disp.event("ℹ", CYAN, "单聊模式", f"请打开与 {contact_name} 的微信聊天窗口")
        print(f"  {GRAY}>>> 按 Enter 读取并回复，Ctrl+C 退出{RESET}")

        try:
            while True:
                input(f"  {GRAY}>>> {RESET}")
                self.process_current_chat(contact_name)
        except KeyboardInterrupt:
            self.disp.shutdown()

    def _extract_incoming_messages(self, ocr_text: str) -> list[str]:
        """Extract ALL incoming messages from OCR text (newest first).

        Scans from the bottom of the conversation panel upward, skipping
        timestamps, UI noise, and lines that match the bot's own recently-sent
        replies.  Returns every qualifying message from the contact that hasn't
        been seen before — not just the most recent one.

        When a contact sends multiple messages in quick succession ("打游戏吗"
        then "你在干啥"), all of them appear in the OCR output and all of them
        need replies.
        """
        import re

        def _is_valid_line(collapsed: str) -> bool:
            if not collapsed or len(collapsed) <= 1:
                return False
            if re.match(r'^(\d{1,2}[:：]\d{2}|[上下]午\d{1,2}[:：]\d{2})$', collapsed):
                return False
            if re.match(r'^\d+$', collapsed):
                return False
            if re.match(r'^\d{4}年\d{1,2}月\d{1,2}日$', collapsed):
                return False
            cjk = 0; asc = 0; dig = 0; garb = 0
            for c in collapsed:
                cp = ord(c)
                if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
                    cjk += 1
                elif c.isascii() and c.isalpha():
                    asc += 1
                elif c.isascii() and c.isdigit():
                    dig += 1
                else:
                    garb += 1
            mean = cjk + asc + dig
            if mean == 0 or garb > mean * 5:
                return False
            if cjk == 0 and asc == 0 and dig <= 3:
                return False
            if mean < 2 and len(collapsed) < 8:
                return False
            return True

        lines = ocr_text.strip().split("\n")

        # Build candidate list from bottom up (newest first)
        candidates = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            collapsed = line.replace(" ", "").replace("　", "")
            if _is_valid_line(collapsed):
                candidates.append(line)
            if len(candidates) >= 12:
                break

        if not candidates:
            return []

        # Collect all non-own messages (newest first).
        # Skip lines that match our own recently-sent replies.
        result = []
        for msg in candidates:
            if not self._is_own_message(msg):
                result.append(msg)

        return result

    # Backwards-compatible single-message accessor
    def _extract_incoming_message(self, ocr_text: str) -> Optional[str]:
        msgs = self._extract_incoming_messages(ocr_text)
        return msgs[0] if msgs else None

    def _is_own_message(self, text: str) -> bool:
        """Check if OCR text matches one of our recently-sent replies."""
        fp = self._fingerprint(text)
        if not fp or len(fp) < 2:
            return False
        for last_fp in self._last_sent.values():
            if not last_fp:
                continue
            if fp == last_fp:
                return True
            if len(fp) >= 3 and len(last_fp) >= 3:
                shorter = fp if len(fp) <= len(last_fp) else last_fp
                longer = last_fp if len(fp) <= len(last_fp) else fp
                if len(shorter) / len(longer) >= 0.70 and shorter in longer:
                    return True
            fp_cjk = {c for c in fp if '一' <= c <= '鿿'}
            s_cjk = {c for c in last_fp if '一' <= c <= '鿿'}
            if len(fp_cjk) >= 2 and len(s_cjk) >= 2:
                union = fp_cjk | s_cjk
                if union:
                    jaccard = len(fp_cjk & s_cjk) / len(union)
                    len_ratio = min(len(fp), len(last_fp)) / max(len(fp), len(last_fp))
                    if jaccard >= 0.55 and len_ratio >= 0.65:
                        return True
        return False

    @staticmethod
    def _collapse(text: str) -> str:
        """Remove all spaces for comparison purposes."""
        return text.replace(" ", "").replace("　", "").strip()

    @staticmethod
    def _norm(contact: str) -> str:
        """Normalize contact name for stable dict keys across OCR variance."""
        return normalize_contact(contact)

    @staticmethod
    def _fingerprint(text: str) -> str:
        """OCR-robust fingerprint for deduplication.

        Strips whitespace and punctuation so that minor OCR variations
        (fullwidth vs halfwidth punctuation, extra spaces) don't cause
        the same message to produce different fingerprints.
        """
        import re as _re
        f = text.replace(" ", "").replace("　", "").strip()
        f = f.replace('？', '?').replace('！', '!').replace('，', ',')
        f = f.replace('：', ':').replace('；', ';').replace('（', '(').replace('）', ')')
        f = _re.sub(r'[?!.,:;()（）]+', '', f)
        return f

    # ------------------------------------------------------------------
    # WebSocket bridge helpers
    # ------------------------------------------------------------------

    def _broadcast(self, event: dict) -> None:
        """Send an event to the Electron UI via WebSocket, if connected."""
        if self.ws:
            self.ws.broadcast_sync(event)

    def _process_ws_commands(self) -> None:
        """Check for commands from the Electron UI and apply them."""
        if not self.ws:
            return
        for cmd in self.ws.pop_commands():
            action = cmd.get("command", "")
            if action == "stop":
                self._stop_requested = True
            elif action == "pause":
                self._paused = True
                self._broadcast({"type": "status", "state": "paused",
                                 "uptime": time.time() - self._start_time})
            elif action == "resume":
                self._paused = False
                self._broadcast({"type": "status", "state": "running",
                                 "uptime": time.time() - self._start_time})
            elif action == "config":
                self._apply_ws_config(cmd.get("key", ""), cmd.get("value"))

    def _apply_ws_config(self, key: str, value) -> None:
        """Apply a runtime config change from the UI."""
        import config
        if hasattr(config, key.upper()):
            setattr(config, key.upper(), value)
            logger.info("Config updated via WS: %s = %s", key.upper(), value)
            self._broadcast({"type": "config_updated", "key": key.upper(), "value": str(value)})

    # ------------------------------------------------------------------
    # Red dot detection (unread message indicators)
    # ------------------------------------------------------------------

    def _detect_unread_chats(self) -> list[dict]:
        """Scan the chat list for red unread badges.

        Returns list of {name, screen_y, red_pixel_count} for each chat
        with a visible red dot indicator, sorted top to bottom.
        """
        cl = self._chat_list_rect
        cw = cl[2] - cl[0]
        ch = cl[3] - cl[1]

        # Red dots appear at the far right of each chat row.
        # Scan the rightmost 25% of the chat list panel.
        region = (
            cl[0] + int(cw * 0.75),
            cl[1] + int(ch * 0.02),
            cl[2],
            cl[3] - int(ch * 0.02),
        )
        img = self._capture_window(bbox=region)
        arr = np.array(img.convert("RGB"))

        zone_h = arr.shape[0]
        if zone_h == 0:
            return []

        r, g, b = (arr[:, :, 0].astype(np.int16),
                    arr[:, :, 1].astype(np.int16),
                    arr[:, :, 2].astype(np.int16))

        red_mask = (
            ((r > 200) & (g < 100) & (b < 100))
            | ((r > 170) & (g < 70) & (b < 70))
        )

        if DEBUG and red_mask.any():
            debug_img = Image.fromarray((red_mask * 255).astype(np.uint8))
            debug_img.save("C:/Users/Trouvailler/wechat-bot/debug_red_mask.png")

        row_red_counts = np.sum(red_mask, axis=1)
        badge_rows = np.where(row_red_counts >= RED_DOT_MIN_PIXELS)[0]

        if len(badge_rows) == 0:
            return []

        clusters = []
        cluster_start = badge_rows[0]
        prev = badge_rows[0]
        for r_idx in badge_rows[1:]:
            if r_idx - prev > 3:
                clusters.append((cluster_start, prev))
                cluster_start = r_idx
            prev = r_idx
        clusters.append((cluster_start, prev))

        results = []
        zone_top = region[1]

        for y1, y2 in clusters:
            badge_diameter = y2 - y1
            if badge_diameter < 6 or badge_diameter > 40:
                continue

            center_y = (y1 + y2) / 2.0
            screen_y = int(zone_top + center_y)
            red_count = int(np.sum(row_red_counts[y1:y2 + 1]))

            # Click in the middle of the chat list panel
            row_click_x = cl[0] + cw // 2

            results.append({
                "screen_y": screen_y,
                "click_x": row_click_x,
                "red_pixels": red_count,
            })

        return results

    # ------------------------------------------------------------------
    # Chat switching
    # ------------------------------------------------------------------

    def _click_chat(self, screen_y: int, click_x: int) -> bool:
        """Click on a chat row in the chat list to open it.

        Returns True if the click was performed, False if skipped (user active).
        """
        if self._is_user_active():
            if DEBUG:
                logger.debug("User active, skipping chat click")
            return False
        pyautogui.click(click_x, screen_y)
        time.sleep(CHAT_SWITCH_COOLDOWN)
        return True

    def _deselect_chat(self):
        """Click on empty space in the chat list to deselect current chat.

        This ensures future messages generate red-dot unread indicators.
        Without this, if the bot stays in a chat after replying, new messages
        from that contact wonʼt produce a red dot.
        """
        cl = self._chat_list_rect
        ch = cl[3] - cl[1]
        # Click near the top of the chat list — safe empty zone
        deselect_x = cl[0] + (cl[2] - cl[0]) // 2
        deselect_y = cl[1] + int(ch * 0.10)
        pyautogui.click(deselect_x, deselect_y)
        time.sleep(0.3)

    def _get_current_contact(self) -> str:
        """OCR the chat header bar to identify the currently open contact."""
        # Header is at the very top of the conversation panel
        img = self._capture_region(self._conversation_rect, 0.0, 0.0, 1.0, 0.07)
        text = self._ocr_image(img)
        if text:
            lines = text.strip().split("\n")
            name = lines[0].strip()
            if name and len(name) >= 1:
                return name.replace(" ", "").replace("　", "")
        return "未知联系人"

    def _get_contact_name_at(self, screen_y: int) -> str:
        """Quick OCR of a single chat list row to get contact name.

        Falls back to positional identifier if OCR fails or if the OCR
        result looks like a timestamp / preview text leak.
        """
        import re as _re

        cl = self._chat_list_rect
        cw = cl[2] - cl[0]

        # OCR only the left 55% of the chat list row — contact names and
        # last-message previews live here.  The right 45% holds timestamps,
        # red dots, and muted-chat icons which must not leak into the name.
        strip_height = 25
        region = (
            cl[0] + 5,
            screen_y - strip_height // 2,
            cl[0] + int(cw * 0.55),
            screen_y + strip_height // 2,
        )
        try:
            img = self._capture_window(bbox=region)
            text = self._ocr_image(img)
            if text and len(text) >= 1:
                name = text.split("\n")[0].strip()
                name = name.replace(" ", "").replace("　", "")
                # Reject timestamp-like strings (e.g. "16：56", "9:30")
                if name and not _re.fullmatch(r'\d{1,2}[：:]\d{2,4}', name):
                    return name
        except Exception:
            pass

        return f"行_{screen_y}"

    # ------------------------------------------------------------------
    # Main loop — multi-chat mode with red dot detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_trivial_message(text: str) -> bool:
        """Skip messages that don't warrant a reply.

        Returns True for: single-char confirmations, pure punctuation/emoji,
        obvious UI artifacts, and ultra-short acknowledgements.
        """
        import re as _re
        collapsed = text.replace(" ", "").replace("　", "").strip()
        if len(collapsed) <= 1:
            return True
        # Pure single ack: 嗯, 好, 行, ok, 对, 是, 哦, 啊, 哈, 嗯嗯, 好的
        if _re.fullmatch(r'(嗯|好|行|ok|OK|Ok|对|是|哦|啊|哈|呵|嗯嗯|好的|好吧|行吧|对的|是的|OK|ok|收到|知道了|明白|懂了|哦哦|哈哈){1,2}', collapsed):
            return True
        # Pure punctuation/emoji/symbols (no CJK, no Latin letters)
        meaningful = sum(1 for c in collapsed if c.isalpha() or ('一' <= c <= '鿿'))
        if meaningful == 0 and len(collapsed) <= 6:
            return True
        return False

    def _should_skip_message(self, contact: str, incoming: str) -> bool:
        """OCR-robust dedup and self-reply prevention.

        Three-tier matching to handle OCR corruption:
        1. Exact fingerprint match
        2. Substring match with 80% length-ratio guard
        3. Fuzzy CJK character-set overlap (Jaccard >= 0.55, len ratio >= 0.65)
           — catches cases where OCR corrupts the same text differently each read
        """
        key = self._norm(contact)
        fp = self._fingerprint(incoming)
        if not fp or len(fp) < 2:
            return True
        seen = self._last_messages[key]

        if fp in seen:
            return True

        # CJK char set for fuzzy matching (OCR corruption changes individual
        # chars but often preserves some of the original characters)
        def _cjk_set(s):
            return {c for c in s if '一' <= c <= '鿿'}

        fp_cjk = _cjk_set(fp)

        for s in seen:
            if len(fp) >= 4 and len(s) >= 4:
                # Tier 2: substring with length guard
                shorter = fp if len(fp) <= len(s) else s
                longer = s if len(fp) <= len(s) else fp
                if len(shorter) / len(longer) >= 0.80:
                    if shorter in longer:
                        return True

            # Tier 3: fuzzy CJK character overlap
            s_cjk = _cjk_set(s)
            if len(fp_cjk) >= 2 and len(s_cjk) >= 2:
                union = fp_cjk | s_cjk
                if union:
                    jaccard = len(fp_cjk & s_cjk) / len(union)
                    len_ratio = min(len(fp), len(s)) / max(len(fp), len(s))
                    if jaccard >= 0.55 and len_ratio >= 0.65:
                        return True

        last_sent = self._last_sent.get(key, "")
        if last_sent:
            if fp == last_sent:
                return True
            if len(fp) >= 4 and len(last_sent) >= 4:
                shorter = fp if len(fp) <= len(last_sent) else last_sent
                longer = last_sent if len(fp) <= len(last_sent) else fp
                if len(shorter) / len(longer) >= 0.80:
                    if shorter in longer:
                        return True
        return False

    def _mark_message_handled(self, contact: str, incoming: str, reply: str = ""):
        """Record seen message fingerprint and our reply."""
        key = self._norm(contact)
        fp = self._fingerprint(incoming)
        seen = self._last_messages[key]
        seen.add(fp)
        if reply:
            reply_fp = self._fingerprint(reply)
            seen.add(reply_fp)
            self._last_sent[key] = reply_fp
        if len(seen) > 200:
            recent = list(seen)[-50:]
            seen.clear()
            seen.update(recent)

    def _cleanup_dedup_sets(self):
        """Periodic cleanup of dedup sets to prevent unbounded growth."""
        for contact in list(self._last_messages.keys()):
            if len(self._last_messages[contact]) > 200:
                recent = list(self._last_messages[contact])[-50:]
                self._last_messages[contact].clear()
                self._last_messages[contact].update(recent)
        # Clean last_sent for contacts no longer in _last_messages
        stale = set(self._last_sent.keys()) - set(self._last_messages.keys())
        for c in stale:
            del self._last_sent[c]

    def _merge_nearby_positions(self, red_dots: list[dict], ts_hits: list[int]) -> list[dict]:
        """Merge red-dot and timestamp-based hits, deduplicating nearby y-positions.

        Two positions within 30px are considered the same chat row.
        Red-dot hits are preferred (they carry more info: click_x, red_pixels).
        """
        PROXIMITY = 30  # px — rows are typically ~65px apart

        cl = self._chat_list_rect

        merged: list[dict] = []
        used_ys: set[int] = set()

        for rd in red_dots:
            merged.append(dict(rd))
            used_ys.add(rd["screen_y"])

        for ty in ts_hits:
            too_close = False
            for my in used_ys:
                if abs(ty - my) < PROXIMITY:
                    too_close = True
                    break
            if too_close:
                continue
            merged.append({
                "screen_y": ty,
                "click_x": cl[0] + (cl[2] - cl[0]) // 2,
                "red_pixels": 0,
                "source": "timestamp",
            })

        # Sort top to bottom
        merged.sort(key=lambda c: c["screen_y"])
        return merged

    def run_forever(self, contact_name: str = "当前聊天"):
        """Dual-detection auto-reply — red dots AND chat-list timestamp scanning.

        Runs both red-dot pixel detection (fast, reliable for unselected chats)
        and chat-list timestamp OCR (catches "刚刚", recent times even without
        red dots). Merges results and replies to every detected new message.
        """
        from config import POLL_INTERVAL

        if self._manual_chat_list_rect and self._manual_conversation_rect:
            mode = "双检测模式 ♥+🕐  |  手动框选"
        else:
            mode = f"双检测模式 ♥+🕐  |  分隔线 {self._divider_x:.0%}"
        self.disp.startup(mode)

        self._broadcast({"type": "status", "state": "running",
                         "uptime": 0, "mode": mode.strip()})

        # Seed: mark current messages as seen on startup
        try:
            self._activate()
            seed_text = self.read_last_messages()
            if seed_text:
                contact = self._get_current_contact()
                seed_msgs = self._extract_incoming_messages(seed_text)
                for seed_msg in seed_msgs:
                    self._mark_message_handled(contact, seed_msg)
                if seed_msgs:
                    self.disp.event("✓", GREEN, "启动", f"已标记 [{contact}] 当前消息为已读")
            self._deselect_chat()
        except Exception:
            pass

        while True:
            try:
                # ── Process WebSocket commands ──
                self._process_ws_commands()
                if self._stop_requested:
                    self._broadcast({"type": "status", "state": "stopped",
                                     "uptime": time.time() - self._start_time})
                    self.disp.shutdown()
                    break

                if self._paused:
                    self.disp.heartbeat("已暂停")
                    self._broadcast({"type": "heartbeat", "state": "paused",
                                     "scan_count": self.disp.scan_count})
                    time.sleep(1.0)
                    continue

                now = time.time()

                if now - self._last_unread_scan < UNREAD_SCAN_INTERVAL:
                    self.disp.heartbeat("等待扫描间隔…")
                    time.sleep(0.5)
                    continue

                self._last_unread_scan = now

                if self._is_user_active():
                    self.disp.user_active()
                    self.disp.heartbeat("用户活跃，跳过")
                    self._broadcast({"type": "info", "subtype": "user_active",
                                     "message": "检测到用户活动，暂停自动操作"})
                    time.sleep(POLL_INTERVAL)
                    continue

                self._activate()

                # ── Dual detection ──
                self.disp.scan_start()

                # 1. Red dot pixel scan (fast)
                red_dots = self._detect_unread_chats()

                # 2. Chat-list timestamp OCR ("刚刚", recent times)
                ts_hits = self._scan_chat_list_activity()

                # 3. Merge & deduplicate
                all_hits = self._merge_nearby_positions(red_dots, ts_hits)

                rd_count = len(red_dots)
                ts_only = sum(1 for h in all_hits if h.get("source") == "timestamp")
                self.disp.scan_done(len(all_hits), red_dots=rd_count, ts_hits=ts_only)
                self._broadcast({"type": "scan", "total": len(all_hits),
                                 "red_dots": rd_count, "ts_hits": ts_only})

                if not all_hits:
                    self.disp.heartbeat()
                    self._broadcast({"type": "heartbeat",
                                     "scan_count": self.disp.scan_count})
                    time.sleep(POLL_INTERVAL)
                    continue

                self.disp.unread_found(len(all_hits))
                replied_count = 0

                for i, chat_info in enumerate(all_hits):
                    if self._is_user_active():
                        self.disp.event("⏸", GRAY, "暂停", "用户活动检测，暂停自动回复")
                        break

                    screen_y = chat_info["screen_y"]
                    click_x = chat_info["click_x"]
                    source = chat_info.get("source", "red_dot")

                    # Time-based position cache: positions expire after TTL.
                    # This prevents re-processing the same red dot/timestamp
                    # within TTL seconds, but allows NEW messages at the same
                    # row to be detected once the cache entry expires.
                    cached = self._known_unread_positions.get(screen_y)
                    if cached and (now - cached) < POSITION_CACHE_TTL:
                        continue

                    # Get rough name from chat list row, normalize immediately
                    row_contact = self._norm(self._get_contact_name_at(screen_y))
                    src_label = "🔴" if source == "red_dot" else "🕐"
                    self.disp.chat_switch(i + 1, len(all_hits), f"{row_contact} {src_label}")
                    self._broadcast({"type": "chat_switch", "index": i + 1,
                                     "total": len(all_hits), "contact": row_contact,
                                     "source": source})

                    if not self._click_chat(screen_y, click_x):
                        continue

                    # Get stable contact name from header bar (larger text,
                    # more reliable OCR). Fall back to row name normalized.
                    header_name = self._get_current_contact()
                    if header_name and header_name != "未知联系人":
                        contact = self._norm(header_name)
                    else:
                        contact = self._norm(row_contact)

                    text = self.read_last_messages()
                    # OCR sometimes returns nothing because the conversation
                    # panel hasn't finished rendering after chat switch.
                    # Retry once after a short wait.
                    if not text:
                        time.sleep(0.5)
                        text = self.read_last_messages()
                    if not text:
                        self.disp.skipped(contact, "OCR 无结果")
                        # Skip-penalty: cache position longer so we don't
                        # keep re-opening the same chat on every scan.
                        self._known_unread_positions[screen_y] = now + 20
                        continue

                    self.disp.ocr_dump(text)

                    incoming_msgs = self._extract_incoming_messages(text)
                    if not incoming_msgs:
                        self.disp.skipped(contact, "未能提取消息")
                        self._known_unread_positions[screen_y] = now + 10
                        continue

                    # Process EVERY new message from this contact.
                    # When contact sends "打游戏吗" then "你在干啥" in quick
                    # succession, both appear in the OCR output — reply to each.
                    for incoming in incoming_msgs:
                        # Record user message immediately so context persists
                        # even if we skip the reply (duplicate, rate-limit, trivial).
                        self.claude.convos.add_message(contact, "user", incoming)

                        if self._should_skip_message(contact, incoming):
                            self.disp.skipped(contact, "重复或已回复")
                            self._known_unread_positions[screen_y] = now + 12
                            continue

                        # ── Quality gate: skip trivial acknowledgements ──
                        if self._is_trivial_message(incoming):
                            self.disp.skipped(contact, "无需回复的消息")
                            continue

                        # ── Rate limit: skip if we just replied to this contact ──
                        last_reply = self.claude.convos.get_last_reply_time(contact)
                        if last_reply and (now - last_reply) < REPLY_COOLDOWN:
                            self.disp.skipped(contact, f"冷却中 ({int(now - last_reply)}s)")
                            continue

                        self.disp.msg_received(contact, incoming)
                        self._broadcast({"type": "msg_received", "contact": contact,
                                         "text": incoming[:200]})
                        reply = self.get_reply(contact, incoming)
                        if reply:
                            self.disp.reply_sent(contact, reply)
                            self._broadcast({"type": "reply_sent", "contact": contact,
                                             "text": reply[:200]})
                            self.claude.convos.add_message(contact, "assistant", reply)
                            self.claude.convos.increment_reply(contact)
                            self._mark_message_handled(contact, incoming, reply)
                            delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
                            time.sleep(delay)
                            if not self.type_and_send(reply):
                                continue
                            replied_count += 1
                            time.sleep(0.8)

                    # ── Self-reply guard: re-read and prime ALL visible
                    # messages so the bot doesn't echo itself on the next scan.
                    try:
                        prime_text = self.read_last_messages()
                        if prime_text:
                            for prime_msg in self._extract_incoming_messages(prime_text):
                                self._mark_message_handled(contact, prime_msg)
                    except Exception:
                        pass

                    self._known_unread_positions[screen_y] = now
                    time.sleep(0.5)

                if replied_count > 0:
                    self._deselect_chat()

                # Expire stale position cache entries
                stale_positions = [
                    sy for sy, ts in self._known_unread_positions.items()
                    if now - ts > POSITION_CACHE_TTL * 2
                ]
                for sy in stale_positions:
                    del self._known_unread_positions[sy]

                # ── Periodic maintenance ──
                self._scan_cycle += 1
                if self._scan_cycle % DEDUP_CLEANUP_INTERVAL == 0:
                    self._cleanup_dedup_sets()
                    stale = self.claude.convos.clean_stale(STALE_CONVERSATION_AGE)
                    if stale and DEBUG:
                        logger.debug(f"Cleaned {stale} stale conversations")

                self.disp.heartbeat()
                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                self._broadcast({"type": "status", "state": "stopped",
                                 "uptime": time.time() - self._start_time})
                self.disp.shutdown()
                break
            except Exception:
                logger.exception("Error in main loop")
                self.disp.event("✗", RED, "错误", "主循环异常，5秒后重试")
                self._broadcast({"type": "error", "message": "主循环异常，5秒后重试"})
                time.sleep(5)
