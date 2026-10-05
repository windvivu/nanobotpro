"""Tool roles: which tools the bot gets. Replaces the developer / coder / chatbot / custom presets.

A role decides WHICH tools the bot has. The sandbox and tools.exec.allowedDirs still decide WHERE
file and shell tools may act. Plan: docs/plan/tool_roles.md.

Everything here is a plain function of config values, so AgentLoop, sub-agents, the dashboard and
the settings page all read the same definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Tool groups, by the names the tools register under. "mcp" is a group too: it turns the MCP
# servers on, and MCPServerConfig.roles decides which servers show in which role. `message` is
# always on; `generate_image` follows tools.imageGeneration, not the role.
GROUP_TOOLS: dict[str, tuple[str, ...]] = {
    "file_read": ("read_file", "list_dir", "glob", "grep"),
    "file_write": ("write_file", "edit_file", "apply_patch", "notebook_edit"),
    "shell": ("exec", "write_stdin", "list_exec_sessions"),
    "web": ("web_search", "web_fetch"),
    "spawn": ("spawn",),
    "cron": ("cron",),
    "goal": ("long_task", "complete_goal"),
    "inspect": ("my",),
    "trading": ("tradingview", "market_scanner"),
    "office": ("read_excel", "write_excel", "edit_excel", "read_word", "write_word", "read_pdf"),
}

_TOOL_GROUP = {name: group for group, names in GROUP_TOOLS.items() for name in names}


@dataclass(frozen=True)
class ToolRole:
    id: str
    label: str
    description: str
    groups: frozenset[str]
    subagent_groups: frozenset[str] = frozenset()  # what a sub-agent spawned in this role gets


# Sub-agents never get message, spawn, cron or trading tools
_SUB_RESEARCH = frozenset({"file_read", "file_write", "web"})
_SUB_OFFICE = frozenset({"file_read", "file_write", "office", "web"})
_SUB_WORKER = frozenset({"file_read", "file_write", "shell", "web"})
# Full, and custom before anything was chosen: everything a sub-agent can have. Otherwise a sub-agent
# never gets a group its role lacks (tests/test_tool_roles.py)
_SUB_ALL = _SUB_WORKER | {"office"}

ROLES: dict[str, ToolRole] = {role.id: role for role in (
    ToolRole("chatbot", "Chatbot", "Chỉ trò chuyện và tra cứu web.",
             frozenset({"web", "mcp"})),
    ToolRole("assistant", "Trợ lý",
             "Trợ lý cá nhân: nhắc hẹn, ghi chú, gửi tệp, giao việc nền. Không chạy lệnh trên máy.",
             frozenset({"file_read", "file_write", "web", "spawn", "cron", "inspect", "mcp"}),
             _SUB_RESEARCH),
    ToolRole("office_secretary", "Thư ký văn phòng",
             "Đọc, tạo và cập nhật Word, Excel, PDF trong vùng workspace. Không chạy lệnh.",
             frozenset({"file_read", "file_write", "office", "web", "inspect", "mcp"}),
             _SUB_OFFICE),
    ToolRole("coder", "Coder", "Làm việc trên một dự án: đọc và sửa code, chạy lệnh, git, CodeGraph, /goal.",
             frozenset({"file_read", "file_write", "shell", "web", "goal", "inspect", "mcp"})),
    ToolRole("super_coder", "Super coder",
             "Coder cộng agent phụ chạy song song, nhắc hẹn và mục tiêu dài hạn.",
             frozenset({"file_read", "file_write", "shell", "web", "spawn", "cron", "goal", "inspect", "mcp"}),
             _SUB_WORKER),
    ToolRole("trader", "Trader",
             "Theo dõi thị trường, cảnh báo: TradingView, market scanner (cần Trader Mode). Không chạy lệnh.",
             frozenset({"file_read", "file_write", "web", "spawn", "cron", "inspect", "trading", "mcp"}),
             _SUB_RESEARCH),
    ToolRole("full", "Toàn quyền", "Bật tất cả (trước đây là Developer).",
             frozenset({*GROUP_TOOLS, "mcp"}), _SUB_ALL),
    ToolRole("custom", "Tuỳ chỉnh", "Chọn từng tool.", frozenset()),
)}

CUSTOM = "custom"
LEGACY_ALIASES = {"developer": "full"}


def normalize_role(value: Any) -> str:
    """Role id for a config value. 'developer' (before roles) reads as 'full', and so does an
    unknown value: the old code treated anything it did not know as developer."""
    role = str(value or "").strip().lower()
    role = LEGACY_ALIASES.get(role, role)
    return role if role in ROLES else "full"


def _chosen(custom_tools: Iterable[str] | None) -> set[str] | None:
    """The custom role's chosen tools, or None when none were chosen: then the five group switches
    from before roles apply (enableFileTools, enableWebTools, enableSpawn, enableCron, enableMcp)."""
    chosen = {str(name) for name in (custom_tools or [])}
    return chosen or None


def role_flags(
    role: Any,
    *,
    sandbox_mode: str,
    custom_tools: Iterable[str] | None = None,
    enable_file_tools: bool = True,
    enable_web_tools: bool = True,
    enable_spawn: bool = True,
    enable_cron: bool = True,
    enable_mcp: bool = True,
) -> tuple[bool, bool, bool, bool, bool, bool]:
    """(file, exec, web, spawn, cron, mcp): the groups AgentLoop registers tools by."""
    exec_ok = sandbox_mode != "disabled"
    role = normalize_role(role)
    if role == CUSTOM:
        chosen = _chosen(custom_tools)
        if chosen is None:
            return enable_file_tools, exec_ok, enable_web_tools, enable_spawn, enable_cron, enable_mcp

        def has(group: str) -> bool:
            return bool(chosen & set(GROUP_TOOLS[group]))

        return (has("file_read") or has("file_write") or has("office"),
                exec_ok and "exec" in chosen, has("web"),
                "spawn" in chosen, "cron" in chosen, enable_mcp)
    groups = ROLES[role].groups
    return ("file_read" in groups or "file_write" in groups or "office" in groups,
            exec_ok and "shell" in groups, "web" in groups,
            "spawn" in groups, "cron" in groups, "mcp" in groups)


def custom_tool_filter(role: Any, custom_tools: Iterable[str] | None) -> set[str] | None:
    """The tools a custom role chose one by one, or None when every tool of an enabled group counts
    (all other roles, and custom before anything was chosen)."""
    return _chosen(custom_tools) if normalize_role(role) == CUSTOM else None


def allows_tool(
    role: Any,
    tool_name: str,
    *,
    custom_tools: Iterable[str] | None = None,
    enable_spawn: bool = True,
) -> bool:
    """Whether the role gets this one tool. Used for the tools no group switch covers: /goal, `my`
    and the trading tools. Custom without a chosen list keeps the old rules: /goal comes with the
    spawn switch, the rest is on."""
    role = normalize_role(role)
    if role == CUSTOM:
        chosen = _chosen(custom_tools)
        if chosen is not None:
            return tool_name in chosen
        return bool(enable_spawn) if tool_name in GROUP_TOOLS["goal"] else True
    group = _TOOL_GROUP.get(tool_name)
    return group is None or group in ROLES[role].groups


def subagent_groups(role: Any, *, custom_tools: Iterable[str] | None = None) -> frozenset[str]:
    """The tool groups a sub-agent spawned in this role gets (shell still needs exec enabled and a
    sandbox other than "disabled"). Custom before anything was chosen keeps the old fixed set."""
    role = normalize_role(role)
    if role != CUSTOM:
        return ROLES[role].subagent_groups
    chosen = _chosen(custom_tools)
    if chosen is None:
        return _SUB_ALL
    groups = {group for group in ("file_read", "file_write", "office", "web")
              if chosen & set(GROUP_TOOLS[group])}
    if "exec" in chosen:
        groups.add("shell")
    return frozenset(groups)


def _role_names(names: Iterable[Any]) -> set[str]:
    """Role ids as written in MCPServerConfig.roles: 'developer' reads as 'full', anything else as is
    (so ["none"] matches no role)."""
    keys = (str(name).strip().lower() for name in names)
    return {LEGACY_ALIASES.get(key, key) for key in keys}


def mcp_server_visible(role: Any, server_cfg: Any) -> bool:
    """Whether an MCP server's tools show in this role. A server without `roles` shows in every role
    that has MCP, as all servers did before roles."""
    names = getattr(server_cfg, "roles", None) or []
    return not names or normalize_role(role) in _role_names(names)


def set_custom_visibility(server_cfg: Any, visible: bool) -> None:
    """Show or hide an MCP server in the custom role, leaving every other role as it was."""
    if mcp_server_visible(CUSTOM, server_cfg) == visible:
        return
    names = [str(name) for name in (getattr(server_cfg, "roles", None) or [])]
    if visible:
        server_cfg.roles = [*names, CUSTOM]
        return
    names = names or [role for role in ROLES if role != CUSTOM]  # it showed everywhere: list the others
    names = [name for name in names if _role_names([name]) != {CUSTOM}]
    server_cfg.roles = names or ["none"]  # it showed in custom only: now in no role


def role_tools(
    role: Any,
    *,
    custom_tools: Iterable[str] | None = None,
    enable_file_tools: bool = True,
    enable_web_tools: bool = True,
    enable_spawn: bool = True,
    enable_cron: bool = True,
) -> list[str]:
    """The built-in tools a role gets, before runtime gates (sandbox, exec switch, Trader Mode, cron
    service). The custom dialog starts from these and its counter counts them."""
    role = normalize_role(role)
    if role == CUSTOM:
        chosen = _chosen(custom_tools)
        if chosen is not None:
            return sorted(chosen | {"message"})
        groups = {"shell", "inspect", "trading"}  # the old custom: exec by sandbox, `my` and trading on
        if enable_file_tools:
            groups |= {"file_read", "file_write"}
        if enable_web_tools:
            groups.add("web")
        if enable_spawn:
            groups |= {"spawn", "goal"}
        if enable_cron:
            groups.add("cron")
    else:
        groups = set(ROLES[role].groups)
    return sorted({name for group in groups if group in GROUP_TOOLS for name in GROUP_TOOLS[group]} | {"message"})


# ── For the settings page ─────────────────────────────────────────────────────────────────────────
ALL_TOOLS = frozenset(_TOOL_GROUP) | {"message"}

ROLE_ICONS = {
    "chatbot": "smart_toy",
    "assistant": "support_agent",
    "coder": "terminal",
    "super_coder": "rocket_launch",
    "trader": "candlestick_chart",
    "office_secretary": "business_center",
    "full": "all_inclusive",
    "custom": "tune",
}

_GROUP_LABELS = {
    "file_read": "đọc file",
    "file_write": "ghi file",
    "shell": "chạy lệnh",
    "web": "web",
    "spawn": "agent phụ",
    "cron": "nhắc hẹn",
    "goal": "/goal",
    "trading": "trading",
    "office": "Office",
    "mcp": "MCP",
}

# The custom dialog: (title, note, ((tool, what it does), ...))
TOOL_SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("File · đọc", "", (("read_file", "Đọc file"), ("list_dir", "Xem thư mục"), ("glob", "Tìm file theo tên"),
                        ("grep", "Tìm trong nội dung file"))),
    ("File · ghi", "", (("write_file", "Ghi file"), ("edit_file", "Sửa một đoạn"),
                        ("apply_patch", "Sửa nhiều file một lần"), ("notebook_edit", "Sửa notebook Jupyter"))),
    ("Chạy lệnh", "Chạy ở đâu, được đụng thư mục nào: mục Exec_Sandbox bên dưới.",
     (("exec", "Chạy lệnh"), ("write_stdin", "Gửi phím cho lệnh đang chạy"),
      ("list_exec_sessions", "Xem các lệnh chạy nền"))),
    ("Web", "", (("web_search", "Tìm trên web"), ("web_fetch", "Đọc một trang web"))),
    ("Tự động", "", (("cron", "Nhắc hẹn, việc định kỳ"), ("spawn", "Giao việc cho agent phụ"),
                     ("long_task", "Mục tiêu dài hạn (/goal)"), ("complete_goal", "Đánh dấu xong mục tiêu"))),
    ("Trạng thái bot", "", (("my", "Xem cấu hình và trạng thái của bot"),)),
    ("Trading", "Chỉ có khi Trader Mode bật.", (("tradingview", "Điều khiển chart TradingView"),
                                               ("market_scanner", "Quét thị trường"))),
    ("Office", "Đọc và tạo tài liệu trong vùng workspace.", (
        ("read_excel", "Đọc bảng tính Excel"), ("write_excel", "Tạo bảng tính Excel"),
        ("edit_excel", "Sửa bảng tính Excel"), ("read_word", "Đọc tài liệu Word"),
        ("write_word", "Tạo tài liệu Word"), ("read_pdf", "Đọc PDF"),
    )),
)


def role_summary(role: Any) -> str:
    """One line for a role card: what it has and what it does not."""
    role = normalize_role(role)
    if role == CUSTOM:
        return "Chọn từng tool trong bảng Tuỳ chỉnh chi tiết."
    groups = ROLES[role].groups
    has = [label for group, label in _GROUP_LABELS.items() if group in groups]
    lacks = [label for group, label in _GROUP_LABELS.items() if group not in groups]
    line = "Có: " + ", ".join(has)
    if lacks:
        line += ". Không có: " + ", ".join(lacks)
    return line + ". Luôn có: gửi tin."
