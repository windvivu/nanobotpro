"""Customer questions the bot could not answer from its knowledge (custom, customer service).

A customer turn counts when the escalate tool handed it over as out of scope, when the reply says
the bot has no information, or when a question found no fact in the knowledge index. The CSKH
page lists them, newest first, so the admin teaches the bot what was missing and marks them
handled. Kept next to the customer service state; at most MAX_ENTRIES, none older than
MAX_AGE_DAYS (the customer records' own retention).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path

from filelock import FileLock, Timeout
from loguru import logger

REASONS = ("out_of_scope", "bot_no_info", "no_knowledge")
MAX_ENTRIES = 500
MAX_AGE_DAYS = 180
QUESTION_MAX_CHARS = 500
REPLY_MAX_CHARS = 300

# The bot saying it does not have the answer: "Em chưa có thông tin về việc này" (the persona's
# words), "em hiện chưa có mức giá cụ thể", or a reply that opens with "Chưa có thông tin".
# Help content about the software ("báo cáo không có dữ liệu thì...") has no bot subject and
# does not count.
_NO_INFO = re.compile(
    r"(?<!của )\b(?:em|mình|tôi|bên\s+em|chúng\s+em|chúng\s+tôi)\b(?:\s+\w+){0,2}?\s+(?:chưa|không)\s+"
    r"(?:có|tìm\s+thấy|nắm\s+được|được\s+cung\s+cấp)\s+(?:\w+\s+){0,2}?"
    r"(?:thông\s+tin|dữ\s+liệu|tài\s+liệu|hướng\s+dẫn|đường\s+dẫn|mức\s+giá|giá)"
    r"|^(?:hiện\s+)?(?:vẫn\s+)?chưa\s+có\s+(?:đủ\s+)?thông\s+tin"
    r"|\b(?:i|we)\s+(?:don't|do\s+not|can't|cannot|couldn't)\s+(?:have|find)\s+(?:any\s+|enough\s+)?"
    r"(?:information|details|data|documentation)\b"
)
# A conditional or an instruction ("Nếu tài khoản chưa có thông tin…") states no missing answer
_CONDITIONAL = re.compile(r"^(?:nếu|khi|trường\s+hợp|lúc|trong\s+trường\s+hợp)\b")
_SENTENCE = re.compile(r"[^.!?\n]+")
# A question or a request for help, as opposed to "ok", "cảm ơn", "chào em"; with or without
# Vietnamese tone marks, as customers often type without them.
_QUESTION_WORDS = re.compile(
    r"\b(?:sao|gì|nào|đâu|bao\s+nhiêu|bao\s+giờ|khi\s+nào|thế\s+nào|làm\s+sao|ra\s+sao|cách|"
    r"hướng\s+dẫn|được\s+không|có\s+(?:\w+\s+){0,3}?không|là\s+gì|ở\s+đâu|giá|báo\s+giá|tư\s+vấn|"
    r"muốn\s+biết|cho\s+(?:tôi|mình|em|hỏi)|xin\s+(?:hỏi|giá|thông\s+tin)|"
    r"gi|nao|dau|bao\s+nhieu|bao\s+gio|khi\s+nao|the\s+nao|lam\s+sao|ra\s+sao|cach|huong\s+dan|"
    r"duoc\s+khong|co\s+(?:\w+\s+){0,3}?khong|la\s+gi|o\s+dau|gia|bao\s+gia|tu\s+van|muon\s+biet|"
    r"cho\s+(?:toi|minh|em|hoi)|xin\s+(?:hoi|gia)|"
    r"how|what|where|when|why|which|can\s+i|could\s+you|is\s+there|do\s+you)\b"
)


def _normalized(text: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).lower().split())


def says_no_information(reply: str | None) -> bool:
    """True when a sentence of the reply tells the customer the bot does not have the answer.
    Conditionals and instructions ("Nếu tài khoản của mình chưa có thông tin…") do not count."""
    for sentence in _SENTENCE.findall(_normalized(reply)):
        sentence = sentence.strip()
        if sentence and not _CONDITIONAL.match(sentence) and _NO_INFO.search(sentence):
            return True
    return False


def looks_like_question(text: str | None) -> bool:
    """True for a question or a request for help of at least two words."""
    normalized = _normalized(text)
    if len(normalized.split()) < 2:
        return False
    return "?" in normalized or bool(_QUESTION_WORDS.search(normalized))


def unanswered_reason(question: str | None, reply: str | None, *, escalated_out_of_scope: bool,
                      knowledge_hits: int | None) -> str | None:
    """Why this turn left the customer without an answer from the knowledge, or None."""
    if escalated_out_of_scope:
        return "out_of_scope"
    if says_no_information(reply):
        return "bot_no_info"
    if knowledge_hits == 0 and looks_like_question(question):
        return "no_knowledge"
    return None


def _well_formed(entry: object) -> bool:
    """An entry the page can show: valid JSON is not enough (review 13 follow-up)."""
    return (isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)) and not isinstance(entry.get("at"), bool)
            and isinstance(entry.get("id"), str) and entry.get("reason") in REASONS
            and all(isinstance(entry.get(key, ""), str) for key in ("customer_id", "name", "chat_id", "question", "reply")))


class UnansweredLog:
    """One JSON file per workspace, written whole under a file lock."""

    LOCK_TIMEOUT_S = 5  # a stuck lock costs this much at most, never the whole loop

    def __init__(self, workspace: Path, *, clock=time.time):
        self.path = Path(workspace) / "zalo_cskh" / "unanswered.json"
        self._lock = FileLock(str(self.path.with_suffix(".lock")), timeout=self.LOCK_TIMEOUT_S)
        self._clock = clock

    def _read(self) -> list[dict]:
        """The entries still within MAX_AGE_DAYS. A file that cannot be read is moved aside, never
        overwritten in place, so nothing is lost silently (review 13)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("not a list")
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            # A unique name: two broken files in the same second must not overwrite each other
            aside = self.path.with_name(f"unanswered.corrupt-{int(self._clock())}-{secrets.token_hex(4)}.json")
            try:
                os.replace(self.path, aside)
            except OSError:
                aside = None
            logger.warning("Unanswered questions could not be read ({}); set aside as {}", type(exc).__name__,
                           aside.name if aside else "-")
            return []
        oldest = self._clock() - MAX_AGE_DAYS * 86400
        kept = [entry for entry in data if _well_formed(entry) and entry["at"] >= oldest]
        if dropped := sum(not _well_formed(entry) for entry in data):
            logger.warning("Unanswered questions: {} malformed entries skipped", dropped)
        return kept

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def record(self, *, customer_id: str, name: str, chat_id: str, question: str, reply: str,
               reason: str) -> dict:
        if reason not in REASONS:
            raise ValueError("Unknown reason")
        started = time.monotonic()
        now = self._clock()
        entry = {
            "id": secrets.token_hex(6), "at": now, "customer_id": str(customer_id), "name": str(name or ""),
            "chat_id": str(chat_id), "question": str(question or "")[:QUESTION_MAX_CHARS],
            "reply": str(reply or "")[:REPLY_MAX_CHARS], "reason": reason, "handled": False,
        }
        try:
            with self._lock:
                entries = self._read()
                entries.append(entry)
                self._write(entries[-MAX_ENTRIES:])
        except Timeout:
            logger.warning("Unanswered question not recorded: the log is locked")
            return entry
        logger.debug("Unanswered question recorded in {:.1f} ms", (time.monotonic() - started) * 1000)
        return entry

    def entries(self, *, include_handled: bool = False) -> list[dict]:
        """Newest first, none older than MAX_AGE_DAYS."""
        try:
            with self._lock:
                items = self._read()
        except Timeout:
            logger.warning("Unanswered questions not listed: the log is locked")
            return []
        return [item for item in reversed(items) if include_handled or not item.get("handled")]

    def mark_handled(self, entry_id: str) -> bool:
        try:
            with self._lock:
                entries = self._read()
                for item in entries:
                    if item.get("id") == entry_id:
                        item["handled"] = True
                        self._write(entries)
                        return True
        except Timeout:
            logger.warning("Unanswered question not marked: the log is locked")
        return False

    def forget(self, identifier: str) -> int | None:
        """Drop a customer's (or a group's) questions when their records are deleted (review 13).
        Handled entries stay hidden until they age out or the cap drops them. None: the log was
        locked and nothing was dropped; the caller keeps the customer's records to try again."""
        try:
            with self._lock:
                entries = self._read()
                kept = [item for item in entries
                        if identifier not in (item.get("customer_id"), item.get("chat_id"))]
                if len(kept) != len(entries):
                    self._write(kept)
                return len(entries) - len(kept)
        except Timeout:
            logger.warning("Unanswered questions not forgotten: the log is locked")
            return None
