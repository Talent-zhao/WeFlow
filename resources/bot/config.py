"""Configuration for the WeChat bot with Claude + Zhaoyoucai persona."""

import os

# --- Anthropic API ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Model name — adapts to whichever API provider is configured.
# With ANTHROPIC_BASE_URL pointing to DeepSeek, use "deepseek-chat".
# With real Anthropic API, use "claude-sonnet-4-6" or "claude-opus-4-7".
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "deepseek-chat")
# Base URL override — set to e.g. "https://api.deepseek.com" for DeepSeek proxy.
# Empty = use default Anthropic API endpoint.
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "")

# --- Persona ---
# Active persona name (without .md extension).  Looks up the matching .md
# file in PERSONAS_DIR (user personas) first, then in the built-in personas/.
PERSONA = os.environ.get("PERSONA", "zhaoyoucai")
# Directory for user-created persona .md files (writable).
# Empty = only use built-in personas/.
PERSONAS_DIR = os.environ.get("PERSONAS_DIR", "")

# --- Bot Behavior ---
# Max messages to keep as context per conversation
MAX_CONTEXT_MESSAGES = 20
# Max response tokens
MAX_TOKENS = 500
# Delay range between receiving and replying (seconds), to feel natural
REPLY_DELAY_MIN = 1.0
REPLY_DELAY_MAX = 4.0

# --- WeChat ---
# Poll interval for new messages (seconds)
POLL_INTERVAL = 2.0
# Scan interval for unread red-dot detection (seconds). Set to POLL_INTERVAL
# to scan every cycle, or a multiple to scan less frequently.
UNREAD_SCAN_INTERVAL = 3.0
# Minimum consecutive red pixels to count as a red dot (filters noise).
# Lowered from 8 to 4 for small window sizes where red dots are tiny.
RED_DOT_MIN_PIXELS = 4
# Cooldown after switching chats (seconds) — lets the UI render
CHAT_SWITCH_COOLDOWN = 1.2
# Seconds of no mouse movement before bot considers user idle and takes control
USER_IDLE_SECONDS = 3.0
# Mouse must move at least this many pixels to be considered "user activity"
USER_MOUSE_THRESHOLD = 50
# Extra cooldown after user activity ends before bot resumes actions
USER_ACTIVITY_COOLDOWN = 2.0
# Minimum seconds between replies to the same contact (rate limiting)
REPLY_COOLDOWN = 12.0
# Remove conversations idle for longer than this (seconds)
STALE_CONVERSATION_AGE = 3600
# How long a processed chat-row position stays cached (seconds).
# After TTL, new hits at the same position are re-processed —
# this catches new messages from contacts who just replied.
POSITION_CACHE_TTL = 15.0
# How often to run dedup-set cleanup (in number of scan cycles)
DEDUP_CLEANUP_INTERVAL = 20
# Whether to log detailed debug info (screenshots, red-mask images, debug logs)
DEBUG = True
# Whether to suppress verbose terminal display (scan details, OCR dumps)
QUIET_MODE = False
# Manual divider override (None = auto-calibrate).  Set to e.g. 0.45 if the
# auto-detection picks the wrong position.  0.45 = divider at 45% of window width.
DIVIDER_X = None
