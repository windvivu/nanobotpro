"""Recognise a reply that tells a customer their request went to a human.

Used by the public customer service runtime: such a reply must be backed by a real
handoff (the escalate tool). A model that writes the sentence without calling the
tool would otherwise leave the customer waiting for a person nobody alerted.

The rules look for the bot's own claim about this request, not for the words alone:
help content about the software ("thông báo sẽ được gửi đến các nhân viên khi bấm
Lưu", "chuyển kho sang chi nhánh"), an offer ("anh/chị có muốn em chuyển...?") or a
refusal ("em chưa thể chuyển tiếp") must not count. A false match costs a needless
alert and a paused customer, so the rules err on the side of not matching.
"""

from __future__ import annotations

import re
import unicodedata

# Who the request went to: people on the seller's side.
_STAFF = (
    r"(?:các\s+)?(?:nhân\s+viên|bộ\s+phận|người\s+(?:hỗ\s+trợ|thật|phụ\s+trách|có\s+thẩm\s+quyền)"
    r"|chuyên\s+viên|tư\s+vấn\s+viên|đội\s+ngũ|quản\s+trị\s+viên|admin|kỹ\s+thuật|kế\s+toán"
    r"|kinh\s+doanh|quản\s+lý|cấp\s+trên|sếp|bên\s+(?:kỹ\s+thuật|kế\s+toán|kinh\s+doanh|hỗ\s+trợ|liên\s+quan))"
)
# The bot speaking about itself, or about the customer's request.
_BOT = r"(?:em|mình|tôi|bên\s+em|chúng\s+em|chúng\s+tôi)"
_REQUEST = (
    r"(?:yêu\s+cầu|vấn\s+đề|câu\s+hỏi|thông\s+tin|trường\s+hợp|phản\s+ánh|khiếu\s+nại|nội\s+dung"
    r"|tình\s+huống|việc)(?:\s+(?:này|của\s+anh(?:/chị)?|của\s+chị|của\s+mình|trên))?"
)
_PASS = r"(?:chuyển|chuyển\s+tiếp|gửi|báo|báo\s+cáo|thông\s+báo|phản\s+ánh)"
_TO = r"(?:cho|đến|tới|sang|lên|với)"

_CLAIMS = [
    # "em đã chuyển (yêu cầu) cho bộ phận ...", "em sẽ báo kỹ thuật", "em đã báo kế toán rồi"
    re.compile(_BOT + r"\s+(?:đã|vừa|sẽ|xin)\s+(?:được\s+)?" + _PASS + r"\b(?:\s+" + _REQUEST + r")?"
               r"(?:\s+" + _TO + r")?\s+" + _STAFF),
    # "yêu cầu của anh/chị đã được chuyển đến nhân viên hỗ trợ" (done, passive; not "sẽ được gửi")
    re.compile(_REQUEST + r"\s+(?:đã|vừa)\s+được\s+" + _PASS + r"\s+" + _TO + r"\s+" + _STAFF),
    # "nhân viên hỗ trợ sẽ liên hệ lại", "bên em sẽ liên hệ lại", "sẽ có người/nhân viên liên hệ"
    re.compile(r"(?:^|\b)(?:" + _STAFF + r"|" + _BOT + r")(?:\s+(?:hỗ\s+trợ|tư\s+vấn|chăm\s+sóc\s+khách\s+hàng"
               r"|phụ\s+trách|bên\s+em|của\s+\w+))?\s+sẽ\s+(?:sớm\s+)?(?:liên\s+hệ|liên\s+lạc|phản\s+hồi"
               r"|gọi\s+lại|nhắn\s+lại|trả\s+lời)(?:\s+lại)?\s+(?:cho\s+|với\s+)?anh"),
    re.compile(r"sẽ\s+có\s+(?:" + _STAFF + r"|người)\b[^.!?\n]{0,30}\b(?:liên\s+hệ|liên\s+lạc|phản\s+hồi|hỗ\s+trợ)"),
    # "yêu cầu của anh/chị đã được chuyển trước đó và đang chờ nhân viên", "em không chuyển lại nữa"
    re.compile(_REQUEST + r"\s+(?:đã|vừa)\s+được\s+" + _PASS + r"\s+(?:trước\s+đó|rồi|từ\s+trước)"),
    re.compile(r"đang\s+chờ\s+" + _STAFF),
    re.compile(_BOT + r"\s+(?:không|chưa)\s+(?:cần\s+)?" + _PASS + r"(?:\s+tiếp)?\s+lại"),
    # English
    re.compile(
        r"\b(?:i|we)(?:'ve|'ll| have| will)?\s+(?:forwarded|transferred|escalated|passed|handed|sent|reported)"
        r"\b[^.!?\n]{0,50}?\b(?:to|on\s+to|over\s+to)\s+(?:a\s+|an\s+|our\s+|the\s+)?"
        r"(?:human|staff|agent|team|support|colleague|specialist|technician|manager)"
    ),
    re.compile(r"\byour\s+(?:request|issue|question|case|complaint)\s+(?:has|was)\s+been\s+(?:forwarded|passed|sent|escalated)"),
    re.compile(
        r"\b(?:a\s+|our\s+|the\s+|someone\s+from\s+our\s+)?(?:human|staff(?:\s+member)?|agent|team(?:\s+member)?|colleague"
        r"|specialist|technician|manager|someone)\s+will\s+(?:soon\s+)?(?:contact|get\s+back|follow\s+up|reach\s+out|call)"
    ),
]

# An offer, a condition, a refusal, or a statement about how the software behaves is not a claim.
_NOT_A_CLAIM = re.compile(
    r"\?|\bcó\s+muốn\b|\bnếu\b|\bmuốn\s+em\b|\bcần\s+em\b|\bcó\s+cần\b|\bchưa\s+thể\b|\bkhông\s+thể\b"
    r"|\bkhi\s+(?:anh|chị|bạn)\b|\bbấm\b|\bnút\b|\btrang\b|\bmenu\b|\bthiết\s+lập\b|\bứng\s+dụng\b"
    r"|\bphần\s+mềm\b|\bhệ\s+thống\b|\bchi\s+nhánh\b|\bkho\b|\bđơn\s+hàng\b|\bphiếu\b|\bgiao\s+hàng\b"
    r"|\bwould\s+you\b|\bdo\s+you\s+want\b|\bif\s+you\b|\bcannot\b|\bcan't\b|\bclick\b|\bbutton\b|\bthe\s+app\b"
)
_SENTENCES = re.compile(r"[^.!?\n]+[.!?]*")


# The customer wants to deal with a person: "cho tôi gặp nhân viên", "tôi muốn nói chuyện với người thật".
# "chuyển cho nhân viên kho" (assigning something to staff in the software) is a help question, not a request.
_PERSON = (
    r"(?:nhân\s+viên(?:\s+(?:hỗ\s+trợ|tư\s+vấn|chăm\s+sóc|thật))?|người\s+thật|người\s+hỗ\s+trợ|tư\s+vấn\s+viên"
    r"|chuyên\s+viên|quản\s+lý|sếp|admin)"
)
_ASKS_FOR_HUMAN = re.compile(
    r"(?:^|\b)(?:(?:tôi|em|mình|anh|chị)\s+)?(?:muốn|cần|xin|làm\s+ơn|hãy|cho\s+(?:tôi|em|mình))\s+(?:được\s+)?"
    r"(?:gặp|nói\s+chuyện\s+với|trao\s+đổi\s+với|kết\s+nối\s+(?:với|tới))\s+(?:trực\s+tiếp\s+)?" + _PERSON
    + r"|^(?:gặp|nói\s+chuyện\s+với)\s+" + _PERSON + r"\b"
    + r"|\bescalate\b|\b(?:talk|speak)\s+to\s+(?:a\s+)?(?:human|real\s+person|person|agent|staff)\b"
)
_NOT_A_REQUEST = re.compile(
    r"\bkhông\s+(?:cần|muốn)\b|\bđừng\b|\bchưa\s+cần\b|\bdon't\b|\bno\s+need\b"
    r"|\bphần\s+mềm\b|\bứng\s+dụng\b|\bphiếu\b|\bđơn\s+hàng\b|\bkho\b|\bquyền\b|\btài\s+khoản\b|\bđăng\s+nhập\b"
    r"|\bchi\s+nhánh\b|\btrang\b|\bmenu\b|\bnút\b|\bbấm\b|\bthiết\s+lập\b|\bcách\b|\blàm\s+sao\b|\bthế\s+nào\b|\bhow\s+(?:do|to)\b"
)


def asks_for_human(text: str | None) -> bool:
    """True when the customer plainly asks for a person (or to escalate)."""
    if not text:
        return False
    normalized = " ".join(unicodedata.normalize("NFC", text).lower().split())
    return bool(_ASKS_FOR_HUMAN.search(normalized)) and not _NOT_A_REQUEST.search(normalized)


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
