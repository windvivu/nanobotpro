"""Knowledge search the customer service model calls with its own keywords (custom).

The facts in the prompt are picked from the customer's own words, which is where it fails: a
follow-up such as "mình chỉ là nhà thuốc nhỏ thôi" holds none of the words of the price facts.
When the prompt does not answer, the model searches again with the knowledge's words ("giá gói
Cơ bản") instead of telling the customer the information is missing. Read-only, and it reads only
what retrieval reads for the customer: facts learned from published KB documents.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobot.agent.tools.base import Tool

QUERY_MAX_CHARS = 200
NOTHING_FOUND = "No matching information in the knowledge base."


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "Search the approved knowledge base with your own keywords. Use it when the reference "
        "information already given does not answer the customer's question, and always before "
        "telling the customer that some information is not available. Write the words the "
        "knowledge would use, short and specific (for example 'giá gói Cơ bản', 'bảng giá', "
        "'gia hạn'), not the customer's whole sentence; try other words if nothing matches. "
        "Returns the matching facts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to search for", "maxLength": QUERY_MAX_CHARS},
        },
        "required": ["query"],
    }

    def __init__(self, context: Any):
        self._context = context  # ContextBuilder: it owns the index and the public KB filter

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, query: str, **kwargs: Any) -> str:
        query = " ".join(str(query or "").split())[:QUERY_MAX_CHARS]
        if not query:
            return NOTHING_FOUND
        from nanobot.agent.hold import signal_slow
        signal_slow()  # one more model round is certain: the customer gets a hold message now
        found = await asyncio.to_thread(self._context.search_knowledge, query)
        return found or NOTHING_FOUND
