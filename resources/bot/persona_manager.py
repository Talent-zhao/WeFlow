"""Persona loader — reads system prompt from .md files.

Looks up personas in two directories (in order):
  1. PERSONAS_DIR env var — user-created personas (writable)
  2. Built-in personas/ directory — shipped with the app (read-only)

The active persona is selected via the PERSONA env var (default: "zhaoyoucai").
"""

import logging
import os

logger = logging.getLogger("persona-manager")

_BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "personas")


def _find_persona_file(name: str) -> str | None:
    """Find a .md persona file by name. Returns path or None."""
    user_dir = os.environ.get("PERSONAS_DIR", "")
    if user_dir:
        path = os.path.join(user_dir, f"{name}.md")
        if os.path.isfile(path):
            return path
    path = os.path.join(_BUILTIN_DIR, f"{name}.md")
    if os.path.isfile(path):
        return path
    return None


def get_system_prompt(persona_name: str | None = None) -> str:
    """Load the system prompt for the given persona.

    Args:
        persona_name: Name of the persona (without .md extension).
                      Defaults to the PERSONA env var or "zhaoyoucai".

    Returns:
        The persona content as a string.

    Raises:
        FileNotFoundError: If the persona file cannot be found.
    """
    if persona_name is None:
        persona_name = os.environ.get("PERSONA", "zhaoyoucai")

    path = _find_persona_file(persona_name)
    if not path:
        raise FileNotFoundError(
            f"Persona '{persona_name}' not found. "
            f"Checked user dir ({os.environ.get('PERSONAS_DIR', '')}) "
            f"and built-in dir ({_BUILTIN_DIR})."
        )

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    logger.info("Loaded persona '%s' from %s (%d chars)", persona_name, path, len(content))
    return content


def list_personas() -> list[dict]:
    """List all available personas from both directories.

    Returns:
        List of dicts with keys: name, source ("builtin"|"user"), path.
        User personas shadow built-in personas with the same name.
    """
    seen: set[str] = set()
    result: list[dict] = []

    # User personas first (take priority)
    user_dir = os.environ.get("PERSONAS_DIR", "")
    if user_dir and os.path.isdir(user_dir):
        try:
            for entry in sorted(os.listdir(user_dir)):
                if entry.endswith(".md"):
                    name = entry[:-3]
                    seen.add(name)
                    result.append({
                        "name": name,
                        "source": "user",
                        "path": os.path.join(user_dir, entry),
                    })
        except OSError:
            pass

    # Built-in personas (skip if already seen from user dir)
    if os.path.isdir(_BUILTIN_DIR):
        try:
            for entry in sorted(os.listdir(_BUILTIN_DIR)):
                if entry.endswith(".md"):
                    name = entry[:-3]
                    if name not in seen:
                        seen.add(name)
                        result.append({
                            "name": name,
                            "source": "builtin",
                            "path": os.path.join(_BUILTIN_DIR, entry),
                        })
        except OSError:
            pass

    return result
