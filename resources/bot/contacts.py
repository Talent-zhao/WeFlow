"""赵有才联系人关系图谱 — 从用户数据目录加载联系人信息。

联系人数据存储在用户数据目录的 contacts.json 中，
不会随应用打包分发，防止个人信息泄漏。

如需添加联系人，请编辑该 JSON 文件，格式参考内置模板。
"""

import json
import os
from typing import Optional

# ── Data loading ──────────────────────────────────────────────────────

def _load_contacts() -> dict:
    """Load contacts from user data, falling back to built-in (empty)."""
    # 1. Check CONTACTS_PATH env var (set by Electron)
    path = os.environ.get("CONTACTS_PATH", "")
    if path and os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    # 2. Look for contacts.json alongside this file (development / standalone)
    local_path = os.path.join(os.path.dirname(__file__), "contacts_data.json")
    if os.path.exists(local_path):
        try:
            with open(local_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    # 3. Fallback: built-in template (intentionally empty — populate via UI or userData)
    return {}


CONTACTS: dict[str, dict] = _load_contacts()


# ── Matching ──────────────────────────────────────────────────────────

def lookup(contact_name: str) -> Optional[dict]:
    """Find a contact profile by name.

    Matches against primary name and all aliases (case-insensitive).
    Uses strict matching for short names (≤2 chars) to avoid false positives
    like "哥" matching "川哥" instead of 李久生.
    """
    clean = contact_name.replace(" ", "").replace("　", "").strip()
    if not clean:
        return None

    is_short = len(clean) <= 2

    for name, profile in CONTACTS.items():
        aliases = profile.get("aliases", [])

        # 1. Exact match always wins
        if name == clean or clean in aliases:
            return profile

        if is_short:
            continue

        # 2. OCR text contains the full alias or primary name
        for alias in aliases:
            if len(alias) >= 2 and alias in clean:
                return profile
        if len(name) >= 2 and name in clean:
            return profile

        # 3. OCR text is contained within alias/name (only for >2 char search)
        for alias in aliases:
            if len(clean) >= 3 and len(alias) >= 3 and clean in alias:
                return profile
        if len(clean) >= 3 and len(name) >= 3 and clean in name:
            return profile

    return None


def find_mentioned_contacts(text: str) -> list[dict]:
    """Scan a message for mentions of known contacts.

    Returns list of {name, profile} for each contact whose name or alias
    appears in the text.  Caller should filter out the current conversation
    partner if needed.

    For 3+ char names: simple substring match.
    For 2-char names (e.g. 田宇, 安阳): only match PRIMARY names (not aliases)
    to avoid false positives from common 2-char combinations appearing in
    regular text.
    """
    clean = text.replace(" ", "").replace("　", "").strip()
    if len(clean) < 2:
        return []

    found: list[dict] = []
    seen_names: set[str] = set()

    for name, profile in CONTACTS.items():
        aliases = profile.get("aliases", [])

        for n in [name] + aliases:
            if len(n) < 3:
                continue
            if n in seen_names:
                continue
            if n in clean:
                found.append({"name": n, "profile": profile})
                seen_names.add(n)
                break

        if len(name) == 2 and name not in seen_names and name in clean:
            found.append({"name": name, "profile": profile})
            seen_names.add(name)

    return found


def describe_mentioned(mentioned: list[dict]) -> str:
    """Build a concise note about third-party contacts mentioned in a message."""
    if not mentioned:
        return ""

    lines = ["对方消息中提到了以下你认识的人："]
    for m in mentioned:
        name = m["name"]
        p = m["profile"]
        role = p["role"]
        mode = p["mode"]
        lines.append(f"- {name}：{role}，你对TA的模式是「{mode}」")
    return "\n".join(lines)


def describe(profile: dict, contact_name: str) -> str:
    """Generate a concise relationship description for the prompt."""
    lines = [
        f"对方是{profile['role']}（{contact_name}）。",
        f"你对TA的模式：{profile['mode']}。",
        profile["dynamic"],
        f"回复风格：tone={profile['tone']}（0=极简省话，10=全功率输出）。",
        "直接回复你发送的内容，不要加任何前缀、标签或引号。就像你在微信里打字一样。",
    ]
    return "\n".join(lines)
