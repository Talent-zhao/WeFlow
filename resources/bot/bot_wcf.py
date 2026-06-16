"""Core WeChat bot using WeChatFerry (wcferry) — process-level hook, no UIA dependency.

Requires: WeChat 3.9.5.81 (not 4.x), wcferry Python package
"""

import random
import time
import logging
from queue import Empty
from typing import Optional

from wcferry import Wcf, WxMsg

from config import (
    REPLY_DELAY_MIN,
    REPLY_DELAY_MAX,
    DEBUG,
)
from claude_client import ClaudeClient
from zhaoyoucai_prompt import SYSTEM_PROMPT

logger = logging.getLogger("wechat-bot-wcf")


class ZhaoyoucaiBot:
    """赵有才微信机器人 — WeChatFerry backend."""

    def __init__(self):
        self.claude = ClaudeClient()

        logger.info("Initializing WeChatFerry SDK...")
        self.wcf = Wcf(debug=DEBUG, block=True)

        self.self_wxid = self.wcf.get_self_wxid()
        logger.info(f"Logged in as: {self.self_wxid}")

        # Build contact lookup: wxid -> {remark, name}
        self._build_contact_cache()

    def _build_contact_cache(self):
        """Load contacts and build wxid -> display name mapping."""
        self.contacts = {}
        for c in self.wcf.get_contacts():
            wxid = c.get("wxid", "")
            remark = c.get("remark", "")
            name = c.get("name", "")
            display = remark or name or wxid
            self.contacts[wxid] = {
                "remark": remark,
                "name": name,
                "display": display,
            }
        logger.info(f"Loaded {len(self.contacts)} contacts")

    def resolve_name(self, wxid: str) -> str:
        """Convert wxid to display name."""
        c = self.contacts.get(wxid)
        return c["display"] if c else wxid

    def reply_to(self, content: str, sender_wxid: str, roomid: str = "") -> str:
        """Generate + send a reply using the zhaoyoucai persona."""
        sender_name = self.resolve_name(sender_wxid)
        receiver = roomid if roomid else sender_wxid

        messages = self.claude.build_prompt(sender_name, content)
        reply = self.claude.get_reply(messages, SYSTEM_PROMPT)
        if not reply:
            return ""

        self.claude.convos.add_message(sender_name, "user", content)
        self.claude.convos.add_message(sender_name, "assistant", reply)

        # Small random delay to feel natural
        delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
        time.sleep(delay)

        ret = self.wcf.send_text(reply, receiver)
        if ret != 0:
            logger.error(f"send_text failed for {sender_name}: ret={ret}")
            return ""

        if DEBUG:
            logger.info(f"[→ {sender_name}] {reply}")

        return reply

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    def handle_message(self, msg: WxMsg) -> bool:
        """Process one incoming WeChat message.

        Returns True if we replied, False otherwise.
        """
        # Skip non-text messages
        if not msg.is_text():
            return False

        # Skip messages from self
        if msg.from_self():
            return False

        content = msg.content.strip()
        if not content:
            return False

        sender_name = self.resolve_name(msg.sender)

        if DEBUG:
            if msg.from_group():
                logger.info(f"[群:{self.resolve_name(msg.roomid)}|{sender_name} →] {content}")
            else:
                logger.info(f"[{sender_name} →] {content}")

        # Generate and send reply
        reply = self.reply_to(content, msg.sender, msg.roomid)
        return bool(reply)

    def run_forever(self):
        """Start the bot — enable message receiving and process in a loop."""
        self.wcf.enable_receiving_msg()
        logger.info("赵有才微信机器人启动！等待消息中...")

        while True:
            try:
                msg = self.wcf.get_msg(block=True)
                self.handle_message(msg)
            except Empty:
                continue
            except KeyboardInterrupt:
                logger.info("用户中断，退出中...")
                break
            except Exception:
                logger.exception("Error processing message")
