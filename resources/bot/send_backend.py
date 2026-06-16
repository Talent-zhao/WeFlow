"""Pluggable send backends for WeChat message delivery.

Provides:
  ClipboardBackend — UI automation via clipboard paste (all WeChat versions)
  WcfBackend       — WeChatFerry process-level send (WeChat 3.9.5.81 only)
  create_backend   — auto-detect and return the best available backend
"""

import logging
import random
import time
from typing import Protocol

logger = logging.getLogger("wechat-send-backend")


class SendBackend(Protocol):
    """Abstract send interface — backend-agnostic message delivery."""

    @property
    def name(self) -> str:
        """Human-readable backend name for logging."""
        ...

    def is_available(self) -> bool:
        """Check whether this backend can send messages right now."""
        ...

    def send(self, text: str, contact: str | None = None) -> bool:
        """Send text to a contact. Returns True on success."""
        ...


# ── Clipboard Backend (UI automation) ────────────────────────────────

class ClipboardBackend:
    """Send via clipboard paste + simulated keystrokes.

    Works with all WeChat versions including 4.x.  Requires the WeChat
    window to be visible (not minimized to tray).
    """

    def __init__(self):
        self._hwnd_cache: int | None = None

    @property
    def name(self) -> str:
        return "ClipboardBackend"

    def is_available(self) -> bool:
        try:
            from wechat_send import find_wechat_window
            hwnd = find_wechat_window()
            if hwnd:
                self._hwnd_cache = hwnd
                return True
            return False
        except Exception:
            return False

    def send(self, text: str, contact: str | None = None) -> bool:
        from wechat_send import send_clipboard, release_modifiers, split_for_wechat

        release_modifiers()

        segments = split_for_wechat(text)
        if len(segments) == 1:
            return send_clipboard(text, hwnd=self._hwnd_cache, contact=contact)

        logger.info(
            "Sending long message in %d segments to %s",
            len(segments), contact or "(current)",
        )

        for i, seg in enumerate(segments):
            # Navigate to chat for first segment only; chat stays open after
            target = contact if i == 0 else None
            ok = send_clipboard(seg, hwnd=self._hwnd_cache, contact=target)
            if not ok:
                logger.error("Segment %d/%d failed for %s", i + 1, len(segments), contact)
                return False

            if i < len(segments) - 1:
                # Human-like pause between messages (2-5 seconds)
                delay = random.uniform(2.0, 5.0)
                logger.debug("Segment delay: %.1fs", delay)
                time.sleep(delay)

        return True


# ── WeChatFerry Backend (process injection, zero UI) ─────────────────

class WcfBackend:
    """Send via WeChatFerry process injection.

    Calls WeChat's internal SendMsg function directly through spy.dll
    injection — no window focus, no clipboard, no keystroke simulation.

    Requires:
      - WeChat 3.9.5.81 (NOT 4.x)
      - wcferry Python package (pip install wcferry>=39.0.0)
    """

    def __init__(self):
        self._wcf = None
        self._wxid_cache: dict[str, str] = {}  # display_name → wxid
        self._available: bool | None = None

    @property
    def name(self) -> str:
        return "WcfBackend"

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available

        # Pre-check: WeChat 3.9.x uses Software\Tencent\WeChat registry key.
        # WeChat 4.x uses Software\Tencent\Weixin — wcferry only supports 3.9.x.
        # Don't even try to load wcferry if the 3.9.x key is missing, because
        # the C++ SDK calls exit() on failure, crashing the Python process.
        if not self._check_wechat_39x_installed():
            self._available = False
            logger.info(
                "WcfBackend 不可用 "
                "(未检测到微信 3.9.5.81，请安装对应版本以使用非UI发送模式)"
            )
            return False

        try:
            from wcferry import Wcf

            self._wcf = Wcf(debug=False, block=False)

            if not self._wcf.is_login():
                self._available = False
                logger.info("WcfBackend 不可用 — WeChat 3.9.5.81 未登录")
                return False

            self._build_contact_cache()
            self._available = True
            logger.info("WcfBackend 可用 — WeChatFerry 已连接 (非UI模式)")
            return True

        except ImportError:
            self._available = False
            logger.info("WcfBackend 不可用 — wcferry 包未安装")
            return False
        except Exception as e:
            self._available = False
            logger.info(
                "WcfBackend 不可用 "
                "(请安装微信 3.9.5.81 以使用非UI发送模式): %s", e
            )
            return False

    @staticmethod
    def _check_wechat_39x_installed() -> bool:
        """Check Windows registry for WeChat 3.9.x installation.

        WeChat 3.9.x writes to HKCU\\Software\\Tencent\\WeChat.
        WeChat 4.x  writes to HKCU\\Software\\Tencent\\Weixin.
        Only the 3.9.x key indicates wcferry-compatible WeChat.
        """
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"
            )
            install_path, _ = winreg.QueryValueEx(key, "InstallPath")
            winreg.CloseKey(key)
            logger.debug("WcfBackend: 检测到 WeChat 安装路径 %s", install_path)
            return True
        except OSError:
            return False

    def _build_contact_cache(self) -> None:
        """Build display_name → wxid lookup from WeChatFerry contacts."""
        if not self._wcf:
            return
        contacts = self._wcf.get_contacts()
        for c in contacts:
            wxid = c.get("wxid", "")
            if not wxid:
                continue
            remark = c.get("remark", "")
            name = c.get("name", "")
            for key in (remark, name):
                if key and key not in self._wxid_cache:
                    self._wxid_cache[key] = wxid
        logger.info("WcfBackend: 已加载 %d 个联系人映射", len(self._wxid_cache))

    def _resolve_wxid(self, contact_name: str) -> str | None:
        """Map a display name to a wxid for use with wcf.send_text()."""
        if not contact_name:
            return None
        if contact_name in self._wxid_cache:
            return self._wxid_cache[contact_name]
        if contact_name.startswith("wxid_"):
            return contact_name
        # Fuzzy match: "张" inside "张三"
        for display, wxid in self._wxid_cache.items():
            if contact_name in display or display in contact_name:
                return wxid
        return None

    def send(self, text: str, contact: str | None = None) -> bool:
        if not self._wcf:
            logger.error("WcfBackend: 未初始化")
            return False

        receiver = self._resolve_wxid(contact) if contact else None

        if contact and not receiver:
            logger.warning(
                "WcfBackend: 找不到联系人 '%s'，尝试直接作为 wxid 发送", contact
            )
            receiver = contact

        if not receiver:
            logger.error("WcfBackend: 无接收者")
            return False

        ret = self._wcf.send_text(text, receiver)
        if ret == 0:
            logger.info("WcfBackend: 已发送至 %s", contact or receiver)
            return True

        logger.error("WcfBackend: send_text 失败 ret=%d receiver=%s", ret, receiver)
        return False


# ── Factory ───────────────────────────────────────────────────────────

def create_backend(prefer: str = "auto") -> SendBackend:
    """Create the best available send backend.

    Args:
        prefer: "auto" (try wcf first), "wcf" (force WeChatFerry),
                "clipboard" (force UI automation)
    """
    if prefer == "clipboard":
        logger.info("激活 ClipboardBackend (UI 自动化模式) — 强制指定")
        return ClipboardBackend()

    if prefer == "wcf":
        backend = WcfBackend()
        if backend.is_available():
            logger.info("激活 WcfBackend (非UI模式) — 强制指定")
            return backend
        logger.error(
            "WcfBackend 强制指定但不可用 (请确认微信 3.9.5.81 已登录)，"
            "回退到 ClipboardBackend"
        )
        return ClipboardBackend()

    # auto: prefer wcferry, fall back to clipboard
    wcf = WcfBackend()
    if wcf.is_available():
        return wcf

    logger.info("回退到 ClipboardBackend (UI 自动化模式)")
    return ClipboardBackend()
