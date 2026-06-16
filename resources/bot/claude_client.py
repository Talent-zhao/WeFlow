"""Shared AI API client for all WeChat bot backends.

Auto-detects API format based on whether ANTHROPIC_BASE_URL is configured:
- Empty base URL → Anthropic SDK (native Anthropic API)
- Custom base URL → OpenAI SDK (DeepSeek, OpenAI, and other compatible APIs)
"""

import logging
from typing import Optional

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_BASE_URL, MAX_TOKENS
from conversation_manager import ConversationManager

logger = logging.getLogger("claude-client")


def _normalize_base_url(url: str) -> str:
    """Strip trailing slash."""
    return url.rstrip("/") if url else url


class ClaudeClient:
    """AI API wrapper with conversation history.

    Uses the Anthropic SDK when ANTHROPIC_BASE_URL is empty (native
    Anthropic API), and the OpenAI SDK when a custom base URL is
    configured (DeepSeek, OpenAI, or any OpenAI-compatible provider).
    """

    def __init__(self):
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY not set")

        self._use_openai = bool(ANTHROPIC_BASE_URL)
        self.convos = ConversationManager()
        self.last_error: str | None = None

        if self._use_openai:
            from openai import OpenAI

            base = _normalize_base_url(ANTHROPIC_BASE_URL)
            if not base.endswith("/v1"):
                base = base + "/v1"
            self.client = OpenAI(api_key=ANTHROPIC_API_KEY, base_url=base)
            logger.info("Using OpenAI SDK → %s (model: %s)", base, ANTHROPIC_MODEL)
        else:
            from anthropic import Anthropic

            self.client = Anthropic(api_key=ANTHROPIC_API_KEY)
            logger.info("Using Anthropic SDK → api.anthropic.com (model: %s)", ANTHROPIC_MODEL)

    TRIVIAL = {'嗯', '好', '行', 'ok', 'OK', 'Ok', '对', '是', '哦', '啊',
                '哈', '嗯嗯', '好的', '好吧', '行吧', '收到', '知道了', '明白',
                '懂了', '哦哦', '哈哈', '1', '2'}

    def build_prompt(self, contact: str, incoming_text: str) -> list[dict]:
        """Build messages array with smart context selection.

        Skips ultra-short confirmations when context is long, keeps a
        character budget so the prompt doesn't overflow.
        """
        history = self.convos.get_history(contact)
        CHAR_BUDGET = 800
        RECENT_KEEP = 4

        recent = history[-RECENT_KEEP:] if len(history) >= RECENT_KEEP else history
        older = history[:-RECENT_KEEP] if len(history) > RECENT_KEEP else []

        def fmt(m):
            label = "你" if m["role"] == "assistant" else contact
            return f"{label}: {m['content']}"

        recent_lines = [fmt(m) for m in recent]
        used = sum(len(l) + 1 for l in recent_lines)

        older_lines = []
        for m in reversed(older):
            line = fmt(m)
            collapsed = m['content'].replace(" ", "").replace("　", "").strip()
            if used + len(line) > CHAR_BUDGET:
                if collapsed in self.TRIVIAL or len(collapsed) <= 2:
                    continue
                break
            older_lines.insert(0, line)
            used += len(line) + 1

        all_lines = older_lines + recent_lines
        if len(all_lines) < len(history):
            all_lines.insert(0, "…(更早的对话已省略)")
        recent_context = "\n".join(all_lines) if all_lines else "（无）"

        return [{
            "role": "user",
            "content": (
                f"最近聊天记录：\n{recent_context}\n\n"
                f"对方（{contact}）刚发来：{incoming_text}\n\n"
                f"用赵有才的方式回复。回复要简短自然，像微信聊天一样。"
            ),
        }]

    def get_reply(self, messages: list[dict], system_prompt: str) -> Optional[str]:
        """Call the AI API and return the text reply."""
        self.last_error = None
        try:
            if self._use_openai:
                return self._get_reply_openai(messages, system_prompt)
            else:
                return self._get_reply_anthropic(messages, system_prompt)
        except Exception as e:
            self.last_error = str(e)
            logger.exception("API call failed: %s", e)
            return None

    def _get_reply_openai(self, messages: list[dict], system_prompt: str) -> Optional[str]:
        """OpenAI-compatible chat completions (DeepSeek, OpenAI, etc.)."""
        response = self.client.chat.completions.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                *messages,
            ],
            temperature=0.85,
        )
        choice = response.choices[0]
        if not choice.message or not choice.message.content:
            self.last_error = "API 返回了空响应"
            return None
        reply = choice.message.content.strip()
        if reply.startswith('"') and reply.endswith('"'):
            reply = reply[1:-1]
        return reply

    def _get_reply_anthropic(self, messages: list[dict], system_prompt: str) -> Optional[str]:
        """Native Anthropic Messages API."""
        from anthropic.types import TextBlock

        response = self.client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=messages,
            temperature=0.85,
        )
        text_blocks = [b for b in response.content if isinstance(b, TextBlock)]
        if not text_blocks:
            self.last_error = "API 返回了空响应（无文本块）"
            return None
        reply = text_blocks[0].text.strip()
        if reply.startswith('"') and reply.endswith('"'):
            reply = reply[1:-1]
        return reply
