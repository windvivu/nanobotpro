"""Startup checks for public customer instances (WP-01).

These checks deliberately do not implement the runtime gates in WP-02..04.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

from nanobot.agent.tool_roles import normalize_role
from nanobot.config.schema import Config


def _field(value: Any, snake: str, camel: str, default: Any = None) -> Any:
    """Channel configs are currently dictionaries; also support typed configs."""
    if isinstance(value, dict):
        return value.get(camel, value.get(snake, default))
    return getattr(value, snake, getattr(value, camel, default))


def public_mode_violations(config: Config) -> list[str]:
    """Return all startup violations, without changing config or runtime state."""
    errors: list[str] = []
    zalo = getattr(config.channels, "zalo", None)
    service = _field(zalo, "customer_service", "customerService")
    if _field(service, "enabled", "enabled", False) and not config.public_customer_mode:
        errors.append("channels.zalo.customerService.enabled requires publicCustomerMode=true")

    if not config.public_customer_mode:
        return errors

    if _field(service, "enabled", "enabled", False):
        from types import SimpleNamespace

        from nanobot.channels.zalo_personal import customer_service_target_error
        target = _field(service, "escalation_target", "escalationTarget", "")
        error = customer_service_target_error(
            SimpleNamespace(enabled=True, escalation_target=target if isinstance(target, str) else ""), config,
        )
        if error:
            errors.append(error)

    if normalize_role(config.tools.tool_preset) != "chatbot":
        errors.append("tools.toolPreset must be chatbot")
    if config.agents.defaults.unified_session:
        errors.append("agents.defaults.unifiedSession must be false")
    if config.brain_memory.learning_sessions:
        errors.append("brainMemory.learningSessions must be empty")
    if any(not source.startswith("kb/") for source in config.brain_memory.migration_sources):
        errors.append("brainMemory.migrationSources must contain only kb/ sources")
    if config.agents.defaults.dream.enabled:
        errors.append("agents.defaults.dream.enabled must be false")
    heartbeat = config.gateway.heartbeat
    channel, sep, target = heartbeat.target.strip().partition(":")
    if heartbeat.enabled and (not sep or channel not in {"webchat", "telegram"} or not target.strip()):
        errors.append("gateway.heartbeat.target must be webchat:<id> or telegram:<id> when enabled")
    if any(entry.strip().lower().startswith(("telegram:", "zalo:")) for entry in config.channels.admin_from):
        errors.append("channels.adminFrom must not grant admin to telegram or zalo senders")
    for name, server in config.tools.mcp_servers.items():
        if server.enabled and "chatbot" not in server.roles:
            errors.append(f"tools.mcpServers.{name}.roles must explicitly contain chatbot")
    return errors


# Only these policy fields may be restored by the dashboard's enable undo.
CUSTOMER_SERVICE_FIELDS = {
    "publicCustomerMode": True,
    "tools.toolPreset": "chatbot",
    "agents.defaults.unifiedSession": False,
    "agents.defaults.dream.enabled": False,
    "gateway.heartbeat.enabled": False,
    "brainMemory.enabled": True,
    "brainMemory.retrievalEnabled": True,
    "brainMemory.learningEnabled": True,
    "brainMemory.migrationEnabled": True,
    "brainMemory.learningSessions": [],
    "brainMemory.migrationDeleteSources": False,
    "channels.zalo.customerService.enabled": True,
}


def customer_service_policy_key(key: str) -> bool:
    return key in CUSTOMER_SERVICE_FIELDS or key in {
        "brainMemory.migrationSources", "channels.adminFrom",
        "channels.zalo.customerService.escalationTarget",
    } or (key.startswith("tools.mcpServers.") and key.endswith(".enabled")
          and bool(key[len("tools.mcpServers."):-len(".enabled")]))


def policy_value(data: dict, key: str, *value):
    """Read/write a policy leaf; MCP names may themselves contain dots."""
    if not customer_service_policy_key(key):
        raise ValueError("Not a customer service policy key")
    parts = (['tools', 'mcpServers', key[len('tools.mcpServers.'):-len('.enabled')], 'enabled']
             if key.startswith('tools.mcpServers.') else key.split('.'))
    target = data
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    if value:
        target[parts[-1]] = value[0]
    return target.get(parts[-1])


@dataclass
class CustomerServiceEnablePlan:
    config: Config
    changes: list[dict]
    blockers: list[str]
    warnings: list[str]
    persona: str

    def public_dict(self):
        return {"changes": self.changes, "blockers": self.blockers,
                "warnings": self.warnings, "persona": self.persona}


def customer_service_enable_plan(config: Config, telegram_chat_id: str, *,
                                 session_count: int = 0, persona: str = "install") -> CustomerServiceEnablePlan:
    """Pure proposal. Filesystem observations are supplied by the caller; no I/O."""
    from nanobot.brain_memory.config import is_public_kb_source
    from nanobot.channels.zalo_personal import ZaloCustomerServiceConfig

    data = config.model_dump(mode="json", by_alias=True)
    zalo = data["channels"].get("zalo") or {}
    data["channels"]["zalo"] = zalo
    service = _field(zalo, "customer_service", "customerService", {}) or {}
    zalo.pop("customer_service", None)
    zalo["customerService"] = ZaloCustomerServiceConfig.model_validate(service).model_dump(by_alias=True)
    target = str(telegram_chat_id).strip()
    blockers = []
    telegram = getattr(config.channels, "telegram", {})
    token = _field(telegram, "token", "token", "")
    if not _field(telegram, "enabled", "enabled", False) or not isinstance(token, str) or not token.strip():
        blockers.append("cskh.mode.blocker_telegram")
    if not _field(zalo, "enabled", "enabled", False):
        blockers.append("cskh.mode.blocker_zalo")
    if len(target) > 64 or not re.fullmatch(r"-?[0-9]+", target):
        blockers.append("cskh.mode.blocker_chat_id")
    else:
        target = str(int(target))
    updates = {**CUSTOMER_SERVICE_FIELDS,
        "brainMemory.migrationSources": [ref for ref in config.brain_memory.migration_sources if is_public_kb_source(ref)],
        "channels.adminFrom": [ref for ref in config.channels.admin_from
                               if not ref.strip().lower().startswith(("telegram:", "zalo:"))],
        "channels.zalo.customerService.escalationTarget": "telegram:" + target,
    }
    for name, server in config.tools.mcp_servers.items():
        if server.enabled and "chatbot" not in server.roles:
            updates[f"tools.mcpServers.{name}.enabled"] = False
    changes = []
    for key, new in updates.items():
        old = policy_value(data, key)
        if old != new:
            changes.append({"key": key, "old": old, "new": new})
            policy_value(data, key, new)
    proposed = Config.model_validate(data)
    blockers.extend(public_mode_violations(proposed))
    warnings = []
    if session_count:
        warnings.append("cskh.mode.warning_sessions")
    try:
        loopback = config.gateway.web.host.lower() == "localhost" or ipaddress.ip_address(config.gateway.web.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        warnings.append("cskh.mode.warning_host")
    if persona == "keep":
        warnings.append("cskh.mode.warning_soul")
    return CustomerServiceEnablePlan(proposed, changes, blockers, warnings, persona)
