"""Core WeChat bot: receives messages, calls Claude with zhaoyoucai persona, replies.
Legacy wxauto4 backend — use bot_image.py for the default image/OCR backend.
"""

import hashlib
import random
import time
import logging
from typing import Optional

from config import (
    REPLY_DELAY_MIN,
    REPLY_DELAY_MAX,
    DEBUG,
)
from claude_client import ClaudeClient
from zhaoyoucai_prompt import SYSTEM_PROMPT

logger = logging.getLogger("wechat-bot")


class ZhaoyoucaiBot:
    """Legacy wxauto4 backend bot — use bot_image.py for primary usage."""

    def __init__(self):
        self.claude = ClaudeClient()
        self._processed_msg_ids: set[str] = set()
        self._wx = None

    # ------------------------------------------------------------------
    # WeChat binding (wxauto4)
    # ------------------------------------------------------------------

    def connect_wechat(self):
        """Attach to the running WeChat window."""
        try:
            from wxauto4 import WeChat
        except ImportError:
            logger.error(
                "wxauto4 not installed. Run: pip install wxauto4"
            )
            raise

        logger.info("Connecting to WeChat...")
        self._wx = WeChat()
        logger.info("WeChat connected OK.")
        return self._wx

    def get_messages(self) -> list:
        """Poll WeChat for new messages. Returns list of message objects."""
        if self._wx is None:
            self.connect_wechat()

        try:
            msgs = self._wx.GetAllMessage()
            return msgs if msgs else []
        except Exception:
            logger.exception("Error getting messages from WeChat")
            return []

    def send_reply(self, contact: str, text: str):
        """Send a text reply to a WeChat contact."""
        if self._wx is None:
            self.connect_wechat()

        if DEBUG:
            logger.info(f"[→ {contact}] {text}")

        try:
            self._wx.SendMsg(text, who=contact)
        except Exception:
            logger.exception(f"Failed to send reply to {contact}")

    # ------------------------------------------------------------------
    # Message dedup
    # ------------------------------------------------------------------

    def _msg_id(self, sender: str, content: str, timestamp) -> str:
        """Build a dedup key. wxauto4 msg objects may have .content .sender .time."""
        ts = getattr(timestamp, "timestamp", lambda: time.time())()
        raw = f"{sender}|{content}|{ts}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _is_duplicate(self, msg_id: str) -> bool:
        if msg_id in self._processed_msg_ids:
            return True
        self._processed_msg_ids.add(msg_id)
        # cap set size
        if len(self._processed_msg_ids) > 5000:
            self._processed_msg_ids.clear()
        return False

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    def handle_message(self, sender: str, content: str) -> Optional[str]:
        """Process one incoming message. Returns reply text or None."""
        if not content or not content.strip():
            return None

        messages = self.claude.build_prompt(sender, content)
        reply = self.claude.get_reply(messages, SYSTEM_PROMPT)

        if reply is None:
            return None

        self.claude.convos.add_message(sender, "user", content)
        self.claude.convos.add_message(sender, "assistant", reply)
        self.claude.convos.increment_reply(sender)

        return reply

    def run_once(self):
        """Single poll iteration: check messages, reply if needed."""
        msgs = self.get_messages()

        for msg in msgs:
            try:
                # wxauto4 returns objects with .content .sender .time
                sender = getattr(msg, "sender", None) or getattr(msg, "who", "")
                content = getattr(msg, "content", None) or str(msg)
                ts = getattr(msg, "time", None)

                if not sender or sender == "Self":
                    continue

                msg_id = self._msg_id(sender, content, ts)
                if self._is_duplicate(msg_id):
                    continue

                if DEBUG:
                    logger.info(f"[{sender} →] {content}")

                reply = self.handle_message(sender, content)

                if reply:
                    # Small random delay to feel natural
                    delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
                    time.sleep(delay)
                    self.send_reply(sender, reply)

            except Exception:
                logger.exception(f"Error processing message from {sender}")

    def run_forever(self):
        """Main bot loop — polls WeChat continuously."""
        from config import POLL_INTERVAL

        logger.info("赵有才微信机器人启动...")
        logger.info("请确保微信PC客户端已登录并保持在前台。")

        while True:
            try:
                self.run_once()
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                logger.info("用户中断，退出中...")
                break
            except Exception:
                logger.exception("Unexpected error in main loop")
                time.sleep(5)
