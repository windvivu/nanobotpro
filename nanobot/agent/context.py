"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.brain_memory.context import BrainContextAdapter
from nanobot.brain_memory.index import BrainIndex
from nanobot.brain_memory.store import BrainStore
from nanobot.session.goal_state import goal_state_runtime_lines
from nanobot.utils.helpers import build_assistant_message, current_time_str, detect_image_mime
from nanobot.utils.prompt_templates import render_template

PUBLIC_CUSTOMER_SKILLS = frozenset()


_NOTE_BLOCK = re.compile(r"\[(?:GROUP\b|Quoted:)[^\]]*\]")


QUERY_WORDS = 64      # a message longer than this is searched by its head and its tail (the index takes 64 terms)
QUERY_HEAD, QUERY_TAIL = 20, 44  # the question of a long message is usually at its end
SHORT_MESSAGE_WORDS = 4  # up to three words: "ok", "còn gì nữa": searched together with the previous question


def strip_channel_notes(message: str) -> str:
    """The message without the notes a channel prepends ([GROUP ...], [Quoted: ...])."""
    return " ".join(_NOTE_BLOCK.sub(" ", message).split())


def retrieval_plan(message: Any, history: Sequence[Mapping[str, Any]] | None = None) -> tuple[Any, bool]:
    """(query, cut) for the knowledge index. The notes a channel prepends ([GROUP ...],
    [Quoted: ...]) are dropped: the index takes a query's first 64 terms, and a 39-word note left
    the question itself out (custom, seen live). Other brackets, such as a sub-agent's report
    header, name what the message is about and stay. A long message is searched by its head and
    its tail (cut=True: the caller may ask the model for the question instead); a very short one
    together with the person's previous question."""
    if not isinstance(message, str):
        return message, False
    bare = strip_channel_notes(message)
    if not bare:
        return message, False
    words = bare.split()
    if len(words) > QUERY_WORDS:
        return " ".join(words[:QUERY_HEAD] + words[-QUERY_TAIL:]), True
    if len(words) < SHORT_MESSAGE_WORDS and history:
        for entry in reversed(history):
            content = entry.get("content") if isinstance(entry, Mapping) else None
            if entry.get("role") == "user" and isinstance(content, str):
                previous = strip_channel_notes(content).split()
                if len(previous) >= SHORT_MESSAGE_WORDS:
                    return " ".join(previous[-QUERY_TAIL:] + words), False
                break
    return bare, False


def retrieval_query(message: Any, history: Sequence[Mapping[str, Any]] | None = None) -> Any:
    return retrieval_plan(message, history)[0]


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"
    _MAX_RECENT_HISTORY = 50

    def __init__(self, workspace: Path, disabled_skills: list[str] | None = None, timezone: str = "UTC",
                 chatbot_config: Any | None = None, brain_memory_config: Any | None = None,
                 public_customer_mode: bool = False):
        self.public_customer_mode = public_customer_mode
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace, public_customer_mode=public_customer_mode)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)
        self._chatbot_config = chatbot_config
        self._brain_memory_config = brain_memory_config
        self._brain_context: BrainContextAdapter | None = None
        # Facts the last system prompt carried from the knowledge index; None when it was not
        # searched. The loop reads it right after build_messages (custom, unanswered questions)
        self.last_knowledge_hits: int | None = None

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        memory_query: str | None = None,
        memory_session: str | None = None,
        include_user_profile: bool = True,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(channel=channel)]

        # A group chat's turns leave USER.md out: it is the owner's profile (custom, agent/group_chat.py)
        bootstrap = self._load_bootstrap_files(skip=() if include_user_profile else ("USER.md",))
        if bootstrap:
            parts.append(bootstrap)

        brain_context = ""
        self.last_knowledge_hits = None
        if memory_query and self._brain_memory_config is not None:
            config = self._brain_memory_config
            if config.reads_session(memory_session):
                try:
                    self._knowledge_adapter()
                    # A long message (a pasted text, a sub-agent's report) is searched by its start: the
                    # index refuses queries over maxQueryChars and only uses their first 64 terms (custom)
                    query = memory_query[:config.max_query_chars]
                    if hasattr(self._brain_context, "retrieve"):
                        retrieved = self._brain_context.retrieve(query)
                        brain_context = retrieved.render()
                        self.last_knowledge_hits = retrieved.hits
                    else:
                        brain_context = self._brain_context.render(query)
                except Exception as exc:
                    if self.public_customer_mode:
                        from nanobot.security.public_output import public_error
                        public_error(exc)  # Log reference locally; never add errors to the prompt.
                    # Brain Memory is an optional cache; legacy context must
                    # continue when its index is absent, corrupt or unavailable.
                    brain_context = ""

        # Brain retrieval is selective. When it finds relevant facts, omit the
        # full legacy MEMORY.md to avoid duplicating the same knowledge in the
        # prompt. An empty result or retrieval failure keeps the legacy memory
        # as a safe fallback.
        if brain_context:
            parts.append("# Brain Memory Reference\n\n" + brain_context)
        elif not self.public_customer_mode:
            memory = self.memory.get_memory_context()
            if memory:
                parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if self.public_customer_mode:
            always_skills = [name for name in always_skills if name in PUBLIC_CUSTOMER_SKILLS]
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = "" if self.public_customer_mode else self.skills.build_skills_summary()
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        # Recent history from Dream — show unconsolidated entries as context
        entries = [] if self.public_customer_mode else self.memory.read_unprocessed_history(since_cursor=self.memory.get_last_dream_cursor())
        if entries:
            capped = entries[-self._MAX_RECENT_HISTORY:]
            parts.append("# Recent History\n\n" + "\n".join(
                f"- [{e['timestamp']}] {e['content']}" for e in capped
            ))

        return "\n\n---\n\n".join(parts)

    def _knowledge_adapter(self) -> BrainContextAdapter:
        if self._brain_context is None:
            config = self._brain_memory_config
            self._brain_context = BrainContextAdapter(
                BrainIndex(BrainStore(self.workspace, config)), config,
                public_customer_mode=self.public_customer_mode,
            )
        return self._brain_context

    def search_knowledge(self, query: str) -> str:
        """The facts matching query alone, rendered, for the search_knowledge tool (custom). Empty
        when nothing matches or the index is unavailable; the reason stays in the server log."""
        config = self._brain_memory_config
        if config is None or not config.reads_session(None):
            return ""
        try:
            adapter = self._knowledge_adapter()
            if not hasattr(adapter, "retrieve"):
                return ""
            return adapter.retrieve(query[:config.max_query_chars], whole=False).render()
        except Exception as exc:
            if self.public_customer_mode:
                from nanobot.security.public_output import public_error
                public_error(exc)
            return ""

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section — uses identity.md template."""
        if self.public_customer_mode:
            return render_template("agent/identity_public.md", channel=channel or "")
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
        thread_type: str | None = None,
        session_summary: str | None = None,
        extra_runtime_lines: Sequence[str] | None = None,
        senders: Sequence[str] | None = None,
        public_customer_mode: bool = False,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines.append(f"Channel: {channel}")
            if not public_customer_mode:
                lines.append(f"Chat ID: {chat_id}")
        if thread_type:
            lines.append(f"Thread Type: {thread_type}")
        if senders:
            # Several people's messages debounced into one turn; each part names its sender (custom)
            lines.append(f"Senders: {', '.join(senders)}")
        else:
            if sender_id:
                lines.append(f"Sender ID: {sender_id}")
            if sender_name:
                lines.append(f"Sender Name: {sender_name}")
        if session_summary and not public_customer_mode:
            lines += ["", "[Resumed Session]", session_summary]
        if extra_runtime_lines and not public_customer_mode:
            lines.extend(extra_runtime_lines)
        return (
            ContextBuilder._RUNTIME_CONTEXT_TAG
            + "\n"
            + "\n".join(lines)
            + "\n"
            + ContextBuilder._RUNTIME_CONTEXT_END
        )

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        """Merge two message content values (str or list-of-blocks) safely."""
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self, skip: tuple[str, ...] = ()) -> str:
        """Load all bootstrap files from workspace, except those in skip."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            if self.public_customer_mode and filename != "SOUL.md":
                continue
            if filename in skip:
                continue
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
        thread_type: str | None = None,
        session_summary: str | None = None,
        current_role: str = "user",
        *,
        session_metadata: Mapping[str, Any] | None = None,
        memory_session: str | None = None,
        senders: Sequence[str] | None = None,
        include_user_profile: bool = True,
        memory_query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call.

        memory_query: what to search the knowledge index with, when the caller knows better
        than the message itself (a question the model distilled from a long one, custom).
        """
        extra_runtime_lines = goal_state_runtime_lines(session_metadata)
        runtime_ctx = self._build_runtime_context(
            channel, chat_id, self.timezone,
            sender_id=sender_id, sender_name=sender_name, thread_type=thread_type,
            session_summary=session_summary,
            extra_runtime_lines=extra_runtime_lines,
            senders=senders,
            public_customer_mode=self.public_customer_mode,
        )
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context goes with user turns only, as upstream's build_current_message does (custom)
        if current_role != "user":
            merged = user_content
        elif isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content

        messages = [
            {"role": "system", "content": self.build_system_prompt(
                skill_names, channel=channel,
                memory_query=memory_query if memory_query is not None else retrieval_query(current_message, history),
                memory_session=memory_session or (f"{channel}:{chat_id}" if channel and chat_id else None),
                include_user_profile=include_user_profile,
            )},
            *history,
        ]
        # Merge if last message has same role (some providers reject consecutive same-role)
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    @staticmethod
    def format_prompt_snapshot(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        """Format messages + tools into readable text for prompt.md snapshot."""
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                img_count = sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
                content = "\n".join(text_parts)
                if img_count:
                    content += f"\n[{img_count} image(s) attached]"
            lines.append(f"{'='*60}\n[{role}]\n{'='*60}\n{content}")

        if tools:
            tool_lines = []
            for t in tools:
                fn = t.get("function", {})
                name = fn.get("name", "?")
                desc = fn.get("description", "")[:80]
                params = fn.get("parameters", {}).get("properties", {})
                param_names = ", ".join(params.keys()) if params else ""
                tool_lines.append(f"  - {name}({param_names}): {desc}")
            lines.append(
                f"{'='*60}\n[TOOLS — {len(tools)} definitions]\n{'='*60}\n"
                + "\n".join(tool_lines)
            )

        return "\n\n".join(lines)


    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: Any,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        messages.append(build_assistant_message(
            content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        ))
        return messages
