"""Streaming terminal display for WeChat bot — ANSI colors, heartbeat, VSCode-ready.

Uses stderr for the persistent status line (refreshed via \r) and stdout for
scrolling event lines. VSCode terminal fully supports the ANSI subset we use.
"""

import sys
import time
from datetime import datetime

# ── ANSI escape codes (VSCode-compatible subset) ─────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
GRAY = "\033[90m"

CLEAR_LINE = "\033[2K"
CURSOR_UP = "\033[1A"

# ── Helpers ───────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _uptime(start: float) -> str:
    secs = int(time.time() - start)
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"


# ── Console display ───────────────────────────────────────────────────

class ConsoleDisplay:
    """Streaming console UI for WeChat bot debugging.

    Maintains a persistent status bar on stderr (overwritten each tick via \\r).
    Events print to stdout and scroll normally above it. This avoids cursor
    management fragility while still giving live-updating heartbeat info.
    """

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.scan_count = 0
        self.start_time = time.time()
        self._last_action: str = ""
        self._status_dirty = False  # only redraw if status line was touched

    # ── Internal ──────────────────────────────────────────────────────

    def _write_status(self, text: str):
        """Write/overwrite the persistent status line on stderr."""
        sys.stderr.write(f"\r{CLEAR_LINE}{text}")
        sys.stderr.flush()
        self._status_dirty = True

    def _clear_status(self):
        """Clear the status line before printing an event to stdout."""
        if self._status_dirty:
            sys.stderr.write(f"\r{CLEAR_LINE}")
            sys.stderr.flush()
            self._status_dirty = False

    def _redraw_status(self):
        """Re-print the status line after an event."""
        self._draw_heartbeat()

    def _draw_heartbeat(self):
        """Render the heartbeat status line."""
        action = f" {DIM}|{RESET} {self._last_action}" if self._last_action else ""
        line = (
            f"{GRAY}{_ts()}{RESET} "
            f"{CYAN}♥{RESET} "
            f"#{self.scan_count}  "
            f"{GRAY}↑{_uptime(self.start_time)}{RESET}"
            f"{action}"
        )
        self._write_status(line)

    # ── Public API ────────────────────────────────────────────────────

    def startup(self, mode: str):
        banner = f"{GREEN}{BOLD}赵有才 微信机器人{RESET}  {GRAY}|{RESET}  {mode}"
        print(f"\n{banner}")
        print(f"{GRAY}{'─' * 50}{RESET}")
        self._draw_heartbeat()

    def heartbeat(self, action: str = ""):
        """Call on every poll cycle. Refreshes the status line in place."""
        self.scan_count += 1
        self._last_action = action
        self._draw_heartbeat()

    def event(self, emoji: str, color: str, tag: str, detail: str):
        """Print a scrolled event line to stdout."""
        self._clear_status()
        tag_width = 10
        tag_padded = f"{tag}:"
        print(f" {GRAY}{_ts()}{RESET} {color}{emoji} {tag_padded:<{tag_width}}{RESET} {detail}")
        self._redraw_status()

    def scan_start(self):
        """Show scan is in progress (only in verbose mode)."""
        if self.verbose:
            self._last_action = f"{CYAN}扫描中...{RESET}"

    def scan_done(self, unread_count: int, red_dots: int = 0, ts_hits: int = 0):
        """Called after a scan completes."""
        if unread_count > 0:
            parts = []
            if red_dots:
                parts.append(f"🔴{red_dots}")
            if ts_hits:
                parts.append(f"🕐{ts_hits}")
            detail = " ".join(parts) if parts else f"{unread_count}个"
            self._last_action = f"{YELLOW}{detail} 新消息{RESET}"
            self._draw_heartbeat()
        else:
            self._last_action = f"{DIM}无新消息{RESET}"

    def unread_found(self, count: int):
        self.event("📬", YELLOW, "未读检测", f"发现 {YELLOW}{count}{RESET} 个未读聊天")

    def chat_switch(self, i: int, total: int, contact: str):
        self.event("📌", BLUE, "切换聊天", f"[{i}/{total}] → {CYAN}{contact}{RESET}")

    def msg_received(self, contact: str, text: str):
        preview = text[:100] + ("…" if len(text) > 100 else "")
        self.event("←", BLUE, contact, preview)

    def reply_sent(self, contact: str, text: str):
        preview = text[:100] + ("…" if len(text) > 100 else "")
        self.event("→", GREEN, contact, preview)

    def ocr_dump(self, text: str):
        """Show raw OCR output (verbose mode)."""
        if not self.verbose:
            return
        self._clear_status()
        print(f" {GRAY}{_ts()}{RESET} {MAGENTA}📷 OCR:{RESET}")
        for line in text.strip().split("\n")[:15]:
            print(f" {DIM}│{RESET} {line}")
        print(f" {DIM}─{'─' * 40}{RESET}")
        self._redraw_status()

    def skipped(self, contact: str, reason: str):
        if self.verbose:
            self.event("⏭", GRAY, contact, reason)

    def user_active(self):
        self._last_action = f"{DIM}用户活跃，暂停{RESET}"

    def api_error(self):
        self.event("✗", RED, "API", "调用失败，查看日志")

    def red_dot_detail(self, y_positions: list):
        """Show red dot detection details (verbose mode)."""
        if not self.verbose:
            return
        self._clear_status()
        ys = ", ".join(f"y={y}" for y in y_positions[:10])
        if len(y_positions) > 10:
            ys += f" …+{len(y_positions) - 10}"
        print(f" {GRAY}{_ts()}{RESET} {DIM}🔴 红点像素位置: {ys}{RESET}")
        self._redraw_status()

    def shutdown(self):
        self._clear_status()
        print(f" {GRAY}{_ts()}{RESET} {GREEN}👋 退出{RESET}")


# ── Module-level singleton for convenience ────────────────────────────

_display: ConsoleDisplay | None = None


def get_display(verbose: bool = True) -> ConsoleDisplay:
    global _display
    if _display is None:
        _display = ConsoleDisplay(verbose=verbose)
    return _display
