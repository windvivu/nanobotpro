"""Structure-aware chunks for explicitly published customer knowledge."""

import bisect
import re

from nanobot.brain_memory.errors import FormatError

KB_CHUNK_BYTES = 6000


def _utf8_prefix(raw: bytes, limit: int) -> bytes:
    return raw[:limit].decode("utf-8", errors="ignore").encode("utf-8")


def kb_content_parts(raw: bytes) -> list[tuple[bytes, bytes]]:
    """Return (repeated context, original body) pairs; bodies join to exact input.

    The common small-document path preserves every byte and thus existing job IDs.
    Extremely long headings are shortened only in repeated context; their original
    bytes remain in the body. Fenced code is not mistaken for document headings.
    """
    if len(raw) <= KB_CHUNK_BYTES:
        return [(b"", raw)]
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormatError("Learning source is not valid UTF-8") from exc
    lines = raw.splitlines(keepends=True)
    # Staged snapshots have YAML metadata before add_document's H1 title.
    title = next((line for line in lines if line.startswith(b"# ")), lines[0]).rstrip(b"\r\n")
    headings, blanks, ends = [], [], []
    heading_text, heading_level = [], []
    offset = 0
    fence = None
    for line in lines:
        stripped = line.lstrip(b" ")
        marker = re.match(rb"(`{3,}|~{3,})", stripped)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[:1] == fence[:1] and len(token) >= len(fence):
                fence = None
        elif fence is None and (heading := re.match(rb" {0,3}(#{1,6})[ \t]+", line)):
            headings.append(offset)
            heading_text.append(line.rstrip(b"\r\n"))
            heading_level.append(len(heading[1]))
        offset += len(line)
        ends.append(offset)
        if not line.strip():
            blanks.append(offset)
    parts = []
    cursor = 0
    while cursor < len(raw):
        context = b""
        if cursor:
            active = bisect.bisect_right(headings, cursor) - 1
            # The enclosing outline, outermost first: a chunk that starts at a
            # sub-section still says which section (article) it belongs to.
            outline, level = [], 7
            for index in range(active, -1, -1):
                if heading_level[index] < level:
                    level = heading_level[index]
                    if heading_text[index] != title:
                        outline.append(heading_text[index])
                    if level == 1:
                        break
            labels = [title, *reversed(outline)]
            # Reserve room for body even for pathological multi-KB headings:
            # the whole context never takes more than a third of the chunk.
            share = min(KB_CHUNK_BYTES // 6, KB_CHUNK_BYTES // 3 // len(labels))
            context = b"".join(_utf8_prefix(label, share) + b"\n" for label in labels)
        end = min(len(raw), cursor + KB_CHUNK_BYTES - len(context))
        if end < len(raw):
            for boundaries in (headings, blanks, ends):
                index = bisect.bisect_right(boundaries, end) - 1
                if index >= 0 and boundaries[index] > cursor:
                    end = boundaries[index]
                    break
            else:
                end = cursor + len(_utf8_prefix(raw[cursor:end], end - cursor))
        parts.append((context, raw[cursor:end]))
        cursor = end
    return parts


def split_kb_content(raw: bytes) -> list[bytes]:
    return [context + body for context, body in kb_content_parts(raw)]
