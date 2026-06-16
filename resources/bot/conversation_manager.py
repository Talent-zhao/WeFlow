"""Manages per-contact conversation history for context-aware replies."""

import time
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict


def normalize_contact(name: str) -> str:
    """Normalize OCR contact names for stable dictionary keys.

    Strips whitespace, collapses internal spaces/ideographic spaces,
    removes OCR noise characters and CJK punctuation artifacts that
    frequently corrupt contact name recognition.

    For names containing ASCII letters, the ASCII portion is preferred
    since Windows OCR handles Latin text far more reliably than CJK.
    """
    if not name:
        return "未知联系人"

    # Collapse all whitespace (including ideographic space U+3000)
    name = re.sub(r'[\s　]+', '', name)

    # Remove common OCR noise symbols throughout the string.
    # OCR frequently prepends/appends garbage like 劬, 韃, 》, 《, |, etc.
    noise_chars = r'|[]{}()（）<>《》「」『』·．.。,::;；!！?？\'\"\\/#@$%^&*=+~`'
    name = re.sub(f'[{re.escape(noise_chars)}]', '', name)

    # Extract ASCII word if present — OCR handles Latin text reliably.
    # E.g. "劬2Trouvaille." -> "Trouvaille", "韃是Trouvaille." -> "Trouvaille"
    ascii_match = re.search(r'[A-Za-z][A-Za-z0-9._\-]*[A-Za-z]', name)
    if ascii_match:
        return ascii_match.group(0)

    # Remove leading single-char OCR fragments that are common CJK radicals
    # misrecognized as standalone characters. These are NOT valid surname chars.
    # Examples: 亻(人 radical), 氵(水 radical), 刂(刀 radical), 讠(言 radical),
    #           冫(冰 radical), 忄(心 radical), 纟(糸 radical), 钅(金 radical)
    _OCR_RADICAL_FRAGMENTS = set('亻氵刂讠冫忄纟钅辶艹宀扌牜衤')
    if len(name) >= 3 and name[0] in _OCR_RADICAL_FRAGMENTS:
        rest = name[1:]
        cjk_count = sum(1 for c in rest if '一' <= c <= '鿿')
        if cjk_count >= 2:
            name = rest

    return name if name else "未知联系人"


@dataclass
class Conversation:
    messages: deque = field(default_factory=lambda: deque(maxlen=20))
    last_reply_time: float = 0.0
    reply_count: int = 0
    last_activity: float = 0.0  # last time any message was added


class ConversationManager:
    def __init__(self, max_context: int = 20):
        self.max_context = max_context
        self._convos: Dict[str, Conversation] = defaultdict(
            lambda: Conversation(
                messages=deque(maxlen=max_context),
                last_activity=time.time(),
            )
        )

    def _key(self, contact: str) -> str:
        return normalize_contact(contact)

    def add_message(self, contact: str, role: str, content: str):
        """Add a message to the conversation history."""
        key = self._key(contact)
        conv = self._convos[key]
        conv.messages.append({"role": role, "content": content})
        conv.last_activity = time.time()

    def get_history(self, contact: str) -> list[dict]:
        """Get the conversation history for a contact."""
        return list(self._convos[self._key(contact)].messages)

    def get_reply_count(self, contact: str) -> int:
        return self._convos[self._key(contact)].reply_count

    def get_last_reply_time(self, contact: str) -> float:
        return self._convos[self._key(contact)].last_reply_time

    def get_last_activity(self, contact: str) -> float:
        return self._convos[self._key(contact)].last_activity

    def increment_reply(self, contact: str):
        key = self._key(contact)
        self._convos[key].reply_count += 1
        self._convos[key].last_reply_time = time.time()

    def clear(self, contact: str):
        """Reset conversation for a contact."""
        self._convos[self._key(contact)] = Conversation(
            messages=deque(maxlen=self.max_context)
        )

    def clean_stale(self, max_age_seconds: float = 3600):
        """Remove conversations idle for longer than max_age_seconds."""
        now = time.time()
        stale_keys = [
            k for k, v in self._convos.items()
            if now - v.last_activity > max_age_seconds
        ]
        for k in stale_keys:
            del self._convos[k]
        if stale_keys:
            return len(stale_keys)
        return 0
