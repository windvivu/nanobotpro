"""Which turns belong to a group chat (custom).

USER.md is the owner's profile. In a group everyone reads the bot's replies, and the bot cannot tell
which member the profile describes, so a group turn's system prompt leaves USER.md out
(docs/log_fix_codebase/group_chat_user_profile.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Entry points that are always one person: the dashboard's Web Chat, the CLI, the API server
PRIVATE_CHANNELS = frozenset({"webchat", "cli", "api"})
# Entry points that count as a group on every turn. The fleet API brings messages from other bots, and from
# the Fleet Manager's Hall, discussion rooms and peer chats, where other bots read the replies. The other
# side is never the owner talking to the bot directly.
GROUP_CHANNELS = frozenset({"fleet"})
# Session metadata key: this chat is a group. Set when a group message arrives; a turn that answers into
# the chat later without a channel flag of its own (a sub-agent's result, a reminder) reads it.
GROUP_MARK = "group_chat"


def group_flag(metadata: Mapping[str, Any] | None) -> bool | None:
    """True or False when the channel said whether the message came from a group, None when it did not.

    Zalo and Telegram send thread_type "Group" or "User"; Telegram, WhatsApp and Mochat send is_group;
    Feishu ("group" or "p2p") and WeCom ("group" or "single") send chat_type. A turn that debounce
    merged from several people (metadata["senders"]) is a group turn.
    """
    md = metadata or {}
    if md.get("senders"):
        return True
    if isinstance(md.get("is_group"), bool):
        return md["is_group"]
    for key in ("thread_type", "chat_type"):
        value = md.get(key)
        if isinstance(value, str) and value:
            return value.lower() == "group"
    return None
