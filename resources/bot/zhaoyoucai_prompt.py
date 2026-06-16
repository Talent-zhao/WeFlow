"""System prompt loader — thin wrapper around persona_manager.

The full persona definitions now live as .md files in personas/.
This module exists for backward compatibility so all existing imports
(sys.path in bot.py, bot_wcf.py, bot_image.py, bot_reply_server.py)
continue to work without changes.
"""

from persona_manager import get_system_prompt

SYSTEM_PROMPT = get_system_prompt()
