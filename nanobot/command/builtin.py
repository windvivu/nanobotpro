"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import sys

from loguru import logger

from nanobot import __version__
from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.session.goal_state import make_active_goal_state, sustained_goal_active
from nanobot.utils.helpers import build_status_content
from nanobot.utils.restart import set_restart_notice_to_env

# ── Who may run which command (custom, identity hardening) ──────────────────
# Commands that act on the whole bot or its memory are for its admins: the people at its own entry
# points (CLI, Web Chat behind the dashboard login, the fleet API behind its password, internal
# system messages) and chat senders listed in channels.adminFrom as "channel:sender_id". allow_from
# only lets people send messages: anyone allowed could otherwise /restart the bot or read /dream-log.
_ADMIN_CHANNELS = frozenset({"cli", "webchat", "fleet", "system"})


# Teaching from Telegram (customer service): only the chat that receives the escalation
# notices may add knowledge or retire facts. The ban on learning from conversations stays:
# a /teach note goes the same way as a note typed on the dashboard, nothing else is learned.
_TEACH_COMMANDS = ("/teach", "/replace")


def _teacher_of(zalo) -> str:
    from nanobot.config.public_mode import _field
    service = _field(zalo, "customer_service", "customerService", None)
    if not service or not _field(service, "enabled", "enabled", False):
        return ""
    channel, _, target = str(_field(service, "escalation_target", "escalationTarget", "")).strip().partition(":")
    return target.strip() if channel == "telegram" else ""


def teacher_chat(loop) -> str:
    """The Telegram chat_id allowed to teach, "" when customer service is off.

    The saved config decides, as the dashboard changes it while the loop keeps its startup copy
    (review 12 #5): switching the service off or clearing the target revokes the right at once,
    and a config that cannot be read grants nothing. The loop's copy serves only a process that
    runs without a config file.
    """
    try:
        from nanobot.config.loader import get_config_path, load_config
        if not get_config_path().exists():
            return _teacher_of(getattr(getattr(loop, "channels_config", None), "zalo", None))
        return _teacher_of(load_config().channels.zalo)
    except Exception as exc:
        logger.warning("Teaching refused: the saved config could not be read ({})", type(exc).__name__)
        return ""


def _is_teacher(ctx: CommandContext) -> bool:
    target = teacher_chat(ctx.loop)
    return bool(target) and ctx.msg.channel == "telegram" and str(ctx.msg.chat_id) == target


def _teach_command(raw: str) -> bool:
    word = raw.strip().split(maxsplit=1)[0].lower() if raw.strip() else ""
    return word in _TEACH_COMMANDS


def public_command_denial(ctx: CommandContext) -> OutboundMessage | None:
    if (getattr(ctx.loop, "public_customer_mode", False)
            and ctx.msg.channel not in _ADMIN_CHANNELS
            and ctx.raw.strip().startswith("/")
            and ctx.raw.strip().lower() not in {"/help", "/new", "/stop"}
            and not (_teach_command(ctx.raw) and _is_teacher(ctx))):
        return OutboundMessage(channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                               content="This command is not available.")
    return None


def _is_admin(ctx: CommandContext) -> bool:
    if ctx.msg.channel in _ADMIN_CHANNELS:
        return True
    try:
        from nanobot.config.loader import load_config

        admins = {str(entry).strip() for entry in load_config().channels.admin_from}
    except Exception:
        return False
    return f"{ctx.msg.channel}:{ctx.msg.sender_id}" in admins


def _admin_only(handler):
    """Run the command for the bot's admins only; anyone else gets a short refusal."""

    async def guarded(ctx: CommandContext) -> OutboundMessage | None:
        if _is_admin(ctx):
            return await handler(ctx)
        return OutboundMessage(channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                               content="This command is only for the bot's admins.")

    return guarded


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    tasks = loop._active_tasks.pop(msg.session_key, [])
    cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    sub_cancelled = await loop.subagents.cancel_by_session(msg.session_key)
    total = cancelled + sub_cancelled
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


RESTART_DELAY_S = 1.0  # the "Restarting..." reply reaches the chat first


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg
    set_restart_notice_to_env(channel=msg.channel, chat_id=msg.chat_id)

    async def _do_restart():
        await asyncio.sleep(RESTART_DELAY_S)
        # The same line as the dashboard's restart: every in-place restart shows in the process log
        # (custom; seen live, a /restart from a chat left no trace and looked like no restart at all)
        logger.info("[Command] Restarting via os.execv... (/restart from {}:{})", msg.channel, msg.chat_id)
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    ctx_est = 0
    try:
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(session)
    except Exception:
        pass
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)
    
    # Fetch web search provider usage (best-effort, never blocks the response)
    search_usage_text: str | None = None
    try:
        from nanobot.utils.searchusage import fetch_search_usage
        web_cfg = getattr(loop, "web_config", None)
        search_cfg = getattr(web_cfg, "search", None) if web_cfg else None
        if search_cfg is not None:
            provider = getattr(search_cfg, "provider", "duckduckgo")
            api_key = getattr(search_cfg, "api_key", "") or None
            usage = await fetch_search_usage(provider=provider, api_key=api_key)
            search_usage_text = usage.format()
    except Exception:
        pass  # Never let usage fetch break /status
    active_tasks = loop._active_tasks.get(ctx.key, [])
    task_count = sum(1 for t in active_tasks if not t.done())
    try:
        task_count += loop.subagents.get_running_count_by_session(ctx.key)
    except Exception:
        pass
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=loop.model,
            start_time=loop._start_time, last_usage=loop._last_usage,
            context_window_tokens=loop.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
            search_usage_text=search_usage_text,
            active_task_count=task_count,
            public_customer_mode=getattr(loop, "public_customer_mode", False),
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Start a fresh session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    snapshot = session.messages[session.last_consolidated:]
    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    if snapshot and not getattr(loop, "public_customer_mode", False):
        loop._schedule_background(loop.consolidator.archive(snapshot))
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata=dict(ctx.msg.metadata or {})
    )


async def cmd_goal(ctx: CommandContext) -> OutboundMessage:
    """Start an explicit sustained goal for the current session."""
    objective = ctx.args.strip()
    if not objective:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /goal <long-running task description>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    session = ctx.loop.sessions.get_or_create(ctx.key)
    current = ctx.loop.sessions.get_goal_state(session)
    if current and sustained_goal_active(session.metadata):
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                "Error: An active goal already exists. Use complete_goal before "
                "starting a new /goal."
            ),
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    state = make_active_goal_state(objective)
    state["started_by"] = "command"
    ctx.loop.sessions.set_goal_state(session, state)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=(
            f"Goal saved: {objective}\n\n"
            "Future messages in this chat will use this goal as context. "
            "Tell me to complete the goal when it is done."
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    import time

    loop = ctx.loop
    msg = ctx.msg

    if getattr(loop, "public_customer_mode", False) or not getattr(loop, "_dream_enabled", False):
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content="Dream is disabled.",
        )

    async def _run_dream():
        t0 = time.monotonic()
        try:
            did_work = await loop.dream.run()
            elapsed = time.monotonic() - t0
            if did_work:
                content = f"Dream completed in {elapsed:.1f}s."
            else:
                content = "Dream: nothing to process."
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"Dream failed after {elapsed:.1f}s: {e}"
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Dreaming...",
    )


def _extract_changed_files(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


def _format_dream_log_content(commit, diff: str, *, requested_sha: str | None = None) -> str:
    files_line = _format_changed_files(diff)
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {files_line}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff to display.",
        ])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.sha}` {c.timestamp} - {c.message.splitlines()[0]}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


async def cmd_dream_log(ctx: CommandContext) -> OutboundMessage:
    """Show what the last Dream changed.

    Default: diff of the latest commit (HEAD~1 vs HEAD).
    With /dream-log <sha>: diff of that specific commit.
    """
    store = ctx.loop.consolidator.store
    git = store.git

    if not git.is_initialized():
        if store.get_last_dream_cursor() == 0:
            msg = "Dream has not run yet. Run `/dream`, or wait for the next scheduled Dream cycle."
        else:
            msg = "Dream history is not available because memory versioning is not initialized."
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=msg, metadata={"render_as": "text"},
        )

    args = ctx.args.strip()

    if args:
        # Show diff of a specific commit
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        if not result:
            content = (
                f"Couldn't find Dream change `{sha}`.\n\n"
                "Use `/dream-restore` to list recent versions, "
                "or `/dream-log` to inspect the latest one."
            )
        else:
            commit, diff = result
            content = _format_dream_log_content(commit, diff, requested_sha=sha)
    else:
        # Default: show the latest commit's diff
        commits = git.log(max_entries=1)
        result = git.show_commit_diff(commits[0].sha) if commits else None
        if result:
            commit, diff = result
            content = _format_dream_log_content(commit, diff)
        else:
            content = "Dream memory has no saved versions yet."

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_dream_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore memory files from a previous dream commit.

    Usage:
        /dream-restore          — list recent commits
        /dream-restore <sha>    — revert a specific commit
    """
    store = ctx.loop.consolidator.store
    git = store.git
    if not git.is_initialized():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Dream history is not available because memory versioning is not initialized.",
        )

    args = ctx.args.strip()
    if not args:
        # Show recent commits for the user to pick
        commits = git.log(max_entries=10)
        if not commits:
            content = "Dream memory has no saved versions to restore yet."
        else:
            content = _format_dream_restore_list(commits)
    else:
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        changed_files = _format_changed_files(result[1]) if result else "the tracked memory files"
        new_sha = git.revert(sha)
        if new_sha:
            content = (
                f"Restored Dream memory to the state before `{sha}`.\n\n"
                f"- New safety commit: `{new_sha}`\n"
                f"- Restored files: {changed_files}\n\n"
                f"Use `/dream-log {new_sha}` to inspect the restore diff."
            )
        else:
            content = (
                f"Couldn't restore Dream change `{sha}`.\n\n"
                "It may not exist, or it may be the first saved version with no earlier state to restore."
            )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


def _reply(ctx: CommandContext, text: str) -> OutboundMessage:
    return OutboundMessage(channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=text,
                           metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"})


_TEACH_USAGE = ("Gõ: /teach <điều bot cần biết>\n"
                "Ví dụ: /teach Gói Cơ bản từ 1/11 là 950.000 đ/năm\n"
                "Sau đó /replace <số> để thu hồi ý cũ cùng chủ đề, ví dụ /replace 1 3")


async def cmd_teach(ctx: CommandContext) -> OutboundMessage:
    """/teach <text>: the note becomes knowledge at once; the facts it may replace are numbered."""
    if not _is_teacher(ctx):
        return _reply(ctx, "This command is not available.")
    text = ctx.raw.strip().split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        return _reply(ctx, _TEACH_USAGE)
    from nanobot.brain_memory.errors import BrainMemoryError
    from nanobot.security.public_output import public_error
    from nanobot.web import kb_catalog
    loop = ctx.loop
    try:
        result = await kb_catalog.add_note(loop.workspace, loop.brain_memory_config,
                                           getattr(loop, "brain_runtime", None), text[1], origin="telegram")
    except (BrainMemoryError, ValueError, OSError) as exc:
        return _reply(ctx, "Chưa dạy được: " + (str(exc) if isinstance(exc, BrainMemoryError) else public_error(exc)))
    related = result["related"]
    pending = getattr(loop, "_teach_related", None)
    if pending is None:
        pending = loop._teach_related = {}
    pending[str(ctx.msg.chat_id)] = [fact["id"] for fact in related]
    lines = ["Đã nhận, bot đang học: " + result["document"]["name"]]
    if related:
        lines.append("")
        lines.append(f"Có {len(related)} ý đã học cùng chủ đề:")
        for index, fact in enumerate(related, 1):
            lines.append(f"{index}. {fact['title']} — {fact['body']}" + (f" ({fact['document']})" if fact["document"] else ""))
        lines.append("")
        lines.append("Ý nào đã sai thì gõ /replace kèm số, ví dụ /replace 1. Không thì bỏ qua.")
    return _reply(ctx, "\n".join(lines))


async def cmd_replace(ctx: CommandContext) -> OutboundMessage:
    """/replace 1 3: retire the facts numbered in the last /teach reply of this chat."""
    if not _is_teacher(ctx):
        return _reply(ctx, "This command is not available.")
    pending = (getattr(ctx.loop, "_teach_related", None) or {}).get(str(ctx.msg.chat_id)) or []
    if not pending:
        return _reply(ctx, "Chưa có danh sách ý cũ. Gõ /teach trước, rồi /replace kèm số.")
    words = ctx.raw.strip().split()[1:]
    if not words or not all(word.isdigit() and 1 <= int(word) <= len(pending) for word in words):
        return _reply(ctx, f"Gõ /replace kèm số từ 1 đến {len(pending)}, ví dụ /replace 1 3.")
    from nanobot.brain_memory.errors import BrainMemoryError
    from nanobot.security.public_output import public_error
    from nanobot.web import kb_catalog
    chosen = list(dict.fromkeys(pending[int(word) - 1] for word in words))
    try:
        result = kb_catalog.retire_kb_facts(ctx.loop.workspace, ctx.loop.brain_memory_config, chosen)
    except (BrainMemoryError, ValueError, OSError) as exc:
        return _reply(ctx, "Chưa thu hồi được: " + (str(exc) if isinstance(exc, BrainMemoryError) else public_error(exc)))
    ctx.loop._teach_related[str(ctx.msg.chat_id)] = [fact_id for fact_id in pending if fact_id not in chosen]
    return _reply(ctx, f"Đã thu hồi {len(result['retired_fact_ids'])} ý.")


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=("/help — Show available commands\n/new — Start a new conversation\n/stop — Stop the current task"
                 + ("\n/teach <nội dung> — Dạy bot một điều cần biết\n/replace <số> — Thu hồi ý cũ sau /teach" if _is_teacher(ctx) else "")
                 if getattr(ctx.loop, "public_customer_mode", False) and ctx.msg.channel not in _ADMIN_CHANNELS
                 else build_help_text()),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = [
        "🐈 nanobot commands:",
        "/new — Start a new conversation",
        "/stop — Stop the current task",
        "/restart — Restart the bot",
        "/status — Show bot status",
        "/goal <task> — Save a long-running goal for this chat",
        "/dream — Manually trigger Dream consolidation",
        "/dream-log — Show what the last Dream changed",
        "/dream-restore — Revert memory to a previous state",
        "/help — Show available commands",
    ]
    return "\n".join(lines)


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    # /stop, /new, /goal and /help act on the sender's own session; the rest only for admins (custom)
    router.priority("/stop", cmd_stop)
    router.priority("/restart", _admin_only(cmd_restart))
    router.priority("/status", _admin_only(cmd_status))
    router.exact("/new", cmd_new)
    router.exact("/status", _admin_only(cmd_status))
    router.exact("/goal", cmd_goal)
    router.prefix("/goal ", cmd_goal)
    router.exact("/teach", cmd_teach)
    router.prefix("/teach ", cmd_teach)
    router.exact("/replace", cmd_replace)
    router.prefix("/replace ", cmd_replace)
    router.exact("/dream", _admin_only(cmd_dream))
    router.exact("/dream-log", _admin_only(cmd_dream_log))
    router.prefix("/dream-log ", _admin_only(cmd_dream_log))
    router.exact("/dream-restore", _admin_only(cmd_dream_restore))
    router.prefix("/dream-restore ", _admin_only(cmd_dream_restore))
    router.exact("/help", cmd_help)
