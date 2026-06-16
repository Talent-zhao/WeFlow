"""Lightweight reply engine — NO screenshot, NO OCR, NO window detection.

Receives messages from Electron (via WebSocket), runs them through the
existing AI pipeline (ClaudeClient + zhaoyoucai_prompt + contacts),
and sends replies via clipboard paste.

Usage:
    python bot_reply_server.py --ws-port 9877
"""

import argparse
import json
import logging
import random
import time
import sys
import os

from claude_client import ClaudeClient
from zhaoyoucai_prompt import SYSTEM_PROMPT
from contacts import lookup, describe, find_mentioned_contacts, describe_mentioned
from conversation_manager import normalize_contact
from config import (
    ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
    REPLY_DELAY_MIN, REPLY_DELAY_MAX, REPLY_COOLDOWN,
)
from ws_server import BotWebSocketServer
from send_backend import create_backend

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot-reply")


class ReplyEngine:
    """Minimal bot: receives messages, generates AI replies, sends via clipboard."""

    def __init__(self, ws_server: BotWebSocketServer, backend_prefer: str = "auto"):
        self.ws = ws_server
        self.claude = ClaudeClient()
        self._start_time = time.time()
        self._paused = False
        self._stop_requested = False
        self.backend = create_backend(backend_prefer)
        logger.info("Send backend: %s", self.backend.name)

    @staticmethod
    def _norm(contact: str) -> str:
        return normalize_contact(contact)

    def broadcast(self, event: dict):
        if self.ws:
            self.ws.broadcast_sync(event)

    def process_commands(self) -> bool:
        """Process any pending WS commands. Returns False if should stop."""
        if not self.ws:
            return True
        for cmd in self.ws.pop_commands():
            action = cmd.get("command", "")
            if action == "stop":
                self._stop_requested = True
                return False
            elif action == "pause":
                self._paused = True
                self.broadcast({"type": "status", "state": "paused"})
            elif action == "resume":
                self._paused = False
                self.broadcast({"type": "status", "state": "running"})
            elif action == "reply":
                # Handle a reply request from Electron (WCDB)
                self._handle_reply_request(cmd)
            elif action == "send_message":
                self._handle_send_message(cmd)
        return not self._stop_requested

    def _handle_reply_request(self, cmd: dict):
        """Process a reply request: {command:"reply", contact, message, displayName?}"""
        raw_contact = cmd.get("contact", "")
        message = cmd.get("message", "")
        display_name = cmd.get("displayName", "")

        if not raw_contact or not message:
            return

        contact = self._norm(display_name or raw_contact)

        # Rate limit check
        last_reply = self.claude.convos.get_last_reply_time(contact)
        now = time.time()
        if last_reply and (now - last_reply) < REPLY_COOLDOWN:
            logger.info("Rate limited: %s (%.0fs since last reply)", contact, now - last_reply)
            self.broadcast({"type": "info", "subtype": "rate_limited",
                           "contact": contact, "message": f"冷却中 ({int(now - last_reply)}s)"})
            return

        # Skip trivial
        if self._is_trivial(message):
            logger.info("Skipping trivial message from %s", contact)
            self.broadcast({"type": "info", "subtype": "trivial_skipped",
                           "contact": contact, "message": "无需回复的消息"})
            return

        # Record the incoming message
        self.claude.convos.add_message(contact, "user", message)
        self.broadcast({"type": "msg_received", "contact": contact,
                        "text": message[:200]})

        # Build prompt with relationship context
        prompt = self._build_prompt(contact, message)
        reply = self.claude.get_reply(prompt, SYSTEM_PROMPT)

        if not reply:
            err_detail = self.claude.last_error or "未知错误"
            logger.error("Failed to get reply for %s: %s", contact, err_detail)
            self.broadcast({"type": "error", "message": f"AI 回复失败: {contact} — {err_detail}"})
            return

        self.claude.convos.add_message(contact, "assistant", reply)
        self.claude.convos.increment_reply(contact)
        self.broadcast({"type": "reply_sent", "contact": contact,
                        "text": reply[:200]})

        # Send via configured backend
        delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
        time.sleep(delay)
        try:
            self.backend.send(reply, contact=contact)
        except Exception as e:
            logger.exception("Failed to send reply to %s", contact)
            self.broadcast({"type": "error",
                           "message": f"发送失败 ({contact}): {e}"})

    def _handle_send_message(self, cmd: dict):
        """Manual reply via human-like typing: {command:"send_message", contact, message}"""
        contact = cmd.get("contact", "")
        message = cmd.get("message", "")
        if not message or not message.strip():
            logger.warning("send_message: empty message")
            self.broadcast({"type": "error", "message": "消息不能为空"})
            return

        logger.info("Manual send to %s: %s", contact or "(current)", message[:80])

        try:
            success = self.backend.send(message, contact=contact)
            if success:
                self.broadcast({
                    "type": "reply_sent",
                    "contact": contact,
                    "text": message[:200],
                })
                # Record in conversation history for context
                if contact:
                    self.claude.convos.add_message(contact, "assistant", message)
                    self.claude.convos.increment_reply(contact)
            else:
                self.broadcast({
                    "type": "error",
                    "message": f"发送失败: {self.backend.name} 返回失败",
                })
        except Exception as e:
            logger.exception("Manual send failed")
            self.broadcast({
                "type": "error",
                "message": f"发送失败: {e}",
            })

    def _build_prompt(self, contact: str, incoming_text: str) -> list[dict]:
        """Build Claude prompt with conversation history + relationship context."""
        history = self.claude.convos.get_history(contact)
        recent_context = self._select_context(history, contact)

        profile = lookup(contact)
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
        """Select representative messages within a character budget."""
        if not history:
            return "（无）"

        TRIVIAL_PATTERNS = {'嗯', '好', '行', 'ok', 'OK', 'Ok', '对', '是',
                            '哦', '啊', '哈', '嗯嗯', '好的', '好吧', '行吧',
                            '收到', '知道了', '明白', '懂了', '哦哦', '哈哈',
                            '1', '2', '知道了知道了'}

        RECENT_KEEP = 4
        recent = history[-RECENT_KEEP:] if len(history) >= RECENT_KEEP else history
        older = history[:-RECENT_KEEP] if len(history) > RECENT_KEEP else []

        def format_msg(m):
            label = "你" if m["role"] == "assistant" else contact
            return f"{label}: {m['content']}"

        recent_lines = [format_msg(m) for m in recent]
        used = sum(len(l) + 1 for l in recent_lines)

        older_lines = []
        for m in reversed(older):
            line = format_msg(m)
            collapsed = m['content'].replace(" ", "").replace("　", "").strip()
            is_trivial = collapsed in TRIVIAL_PATTERNS or len(collapsed) <= 2
            if used + len(line) > char_budget:
                if is_trivial:
                    continue
                break
            older_lines.insert(0, line)
            used += len(line) + 1

        all_lines = older_lines + recent_lines
        if len(all_lines) < len(history):
            all_lines.insert(0, "…(更早的对话已省略)")
        return "\n".join(all_lines)

    @staticmethod
    def _is_trivial(text: str) -> bool:
        """Skip messages that don't warrant a reply."""
        import re as _re
        collapsed = text.replace(" ", "").replace("　", "").strip()
        if len(collapsed) <= 1:
            return True
        if _re.fullmatch(r'(嗯|好|行|ok|OK|Ok|对|是|哦|啊|哈|呵|嗯嗯|好的|好吧|行吧|对的|是的|OK|ok|收到|知道了|明白|懂了|哦哦|哈哈){1,2}', collapsed):
            return True
        meaningful = sum(1 for c in collapsed if c.isalpha() or ('一' <= c <= '鿿'))
        if meaningful == 0 and len(collapsed) <= 6:
            return True
        return False



def main():
    parser = argparse.ArgumentParser(description="赵有才 微信机器人 — 轻量回复引擎 (WCDB模式)")
    parser.add_argument("--ws-port", type=int, default=9877, help="WebSocket port")
    parser.add_argument("--backend", type=str, default="auto",
                        choices=["auto", "wcf", "clipboard"],
                        help="Send backend: auto (try wcf first), wcf (WeChatFerry 3.9.5.81), clipboard (UI automation)")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)

    logger.info("Starting reply engine (WCDB mode) on ws://127.0.0.1:%d", args.ws_port)
    logger.info("Model: %s", ANTHROPIC_MODEL)

    ws = BotWebSocketServer(port=args.ws_port)
    ws.start()

    engine = ReplyEngine(ws, backend_prefer=args.backend)
    engine.broadcast({
        "type": "status",
        "state": "running",
        "uptime": 0,
        "mode": "WCDB — 数据库消息检测",
    })

    try:
        while True:
            if not engine.process_commands():
                engine.broadcast({"type": "status", "state": "stopped"})
                break
            if engine._paused:
                time.sleep(1.0)
                continue
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        ws.stop()
        logger.info("Reply engine stopped")


if __name__ == "__main__":
    main()
