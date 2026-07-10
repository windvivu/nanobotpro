import asyncio
from loguru import logger
from nanobot.channels.manager import ChannelManager

class SafeChannelManager(ChannelManager):
    """
    Custom subclass of ChannelManager that provides safer production defaults.
    - Gracefully disables misconfigured channels instead of crashing the Gateway.
    - Connects Zalo Personal module to the "zalo" config key via aliases.
    - Applies strict timeouts on shutdown to prevent hanging.
    """
    
    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan + entry_points plugins."""
        from nanobot.channels.registry import discover_all

        # Custom: Module names that need a different config attribute
        _CONFIG_ALIAS: dict[str, str] = {
            "zalo_personal": "zalo",
        }

        for name, cls in discover_all().items():
            config_attr = _CONFIG_ALIAS.get(name, name)
            section = getattr(self.config.channels, config_attr, None)
            if section is None:
                continue
            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            if not enabled:
                continue
            try:
                channel = cls(section, self.bus)
                self._apply_transcription_config(channel)
                # Use config_attr as the channel key (e.g. "zalo" not "zalo_personal")
                self.channels[config_attr] = channel
                logger.info("{} channel enabled", cls.display_name)
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        to_remove: list[str] = []
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                logger.warning(
                    '"{}" has empty allow_from (denies all) — channel disabled. '
                    'Set ["*"] to allow everyone, or add specific user IDs.',
                    name,
                )
                to_remove.append(name)
        for name in to_remove:
            del self.channels[name]

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels (with strict timeouts)...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await asyncio.wait_for(self._dispatch_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Stop all channels (with timeout per channel)
        for name, channel in self.channels.items():
            try:
                await asyncio.wait_for(channel.stop(), timeout=10.0)
                logger.info("Stopped {} channel", name)
            except asyncio.TimeoutError:
                logger.warning("Timeout stopping {} channel — forcing", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)
