"""Recognise a reply that tells a customer their request went to a human.

Used by the public customer service runtime: such a reply must be backed by a real
handoff (the escalate tool). A model that writes the sentence without calling the
tool would otherwise leave the customer waiting for a person nobody alerted.

The rules look for a claim, not for the words alone: help content about moving
stock, sending notices to the pharmacy's own staff, or an offer such as "do you want
me to pass this to a colleague?" must not count.
"""

from __future__ import annotations

import re
import unicodedata

# Who the request was passed to: people on the seller's side, never things such as
# a branch, a warehouse or a supplier.
_STAFF = (
    r"(?:các\s+)?(?:nhân\s+viên|bộ\s+phận|người\s+(?:hỗ\s+trợ|thật|phụ\s+trách)|chuyên\s+viên"
    r"|tư\s+vấn\s+viên|đội\s+ngũ|quản\s+trị\s+viên|admin)"
)

_CLAIMS = [
    # "đã được chuyển đến nhân viên hỗ trợ", "em đã chuyển yêu cầu cho bộ phận ...", "em sẽ chuyển ..."
    re.compile(
        r"(?:đã|được|vừa|sẽ|xin)\s+(?:được\s+)?(?:chuyển|chuyển\s+tiếp|gửi|báo)\b[^.!?\n]{0,60}?"
        r"\b(?:cho|đến|tới|sang|lên)\s+" + _STAFF
    ),
    # "nhân viên hỗ trợ sẽ liên hệ lại", "sẽ có nhân viên phản hồi"
    re.compile(
        _STAFF + r"(?:\s+(?:hỗ\s+trợ|tư\s+vấn|chăm\s+sóc\s+khách\s+hàng|phụ\s+trách|bên\s+em|của\s+\w+))?"
        r"\s+sẽ\s+(?:sớm\s+)?(?:liên\s+hệ|liên\s+lạc|phản\s+hồi|gọi\s+lại|nhắn\s+lại)"
    ),
    re.compile(r"sẽ\s+có\s+" + _STAFF + r"[^.!?\n]{0,30}\b(?:liên\s+hệ|liên\s+lạc|phản\s+hồi|hỗ\s+trợ)"),
    # English
    re.compile(
        r"\b(?:forwarded|transferred|escalated|passed|handed)\b[^.!?\n]{0,50}?\b(?:to|on\s+to|over\s+to)\s+"
        r"(?:a\s+|an\s+|our\s+|the\s+)?(?:human|staff|agent|team|support|colleague|specialist)"
    ),
    re.compile(
        r"\b(?:a\s+|our\s+|the\s+)?(?:human|staff(?:\s+member)?|agent|team\s+member|colleague|specialist)"
        r"\s+will\s+(?:soon\s+)?(?:contact|get\s+back|follow\s+up|reach\s+out|call)"
    ),
]

# An offer or a condition is not a claim that something happened.
_NOT_A_CLAIM = re.compile(
    r"\?|\bcó\s+muốn\b|\bnếu\b|\bmuốn\s+em\b|\bcần\s+em\b|\bcó\s+cần\b|\bchưa\s+thể\b|\bkhông\s+thể\b"
    r"|\bwould\s+you\b|\bdo\s+you\s+want\b|\bif\s+you\b|\bcannot\b|\bcan't\b"
)
_SENTENCES = re.compile(r"[^.!?\n]+[.!?]*")


def claims_handoff(text: str | None) -> bool:
    """True when some sentence of the reply says the customer was (or will be) passed to staff."""
    if not text:
        return False
    normalized = " ".join(unicodedata.normalize("NFC", text).lower().split())
    for sentence in _SENTENCES.findall(normalized):
        if _NOT_A_CLAIM.search(sentence):
            continue
        if any(rule.search(sentence) for rule in _CLAIMS):
            return True
    return False
