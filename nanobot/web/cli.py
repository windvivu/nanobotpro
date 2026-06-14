"""Entry point for starting the web dashboard server."""

from loguru import logger


async def start_web(config, session_manager=None, agent=None, channel_manager=None, port: int = 8899, host: str = "127.0.0.1"):
    """Start the web dashboard as an async task.

    This function creates the FastAPI app and runs Uvicorn within the
    existing asyncio event loop (used by gateway).

    Args:
        config: Nanobot Config object.
        session_manager: SessionManager instance.
        agent: AgentLoop instance (optional).
        channel_manager: ChannelManager instance (optional, for Zalo setup).
        port: Port to bind the web server to.
        host: Bind address. Use 0.0.0.0 for fleet/remote access.
    """
    import uvicorn

    from nanobot.web.app import create_app

    app = create_app(config, session_manager, agent, channel_manager=channel_manager)

    uvi_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(uvi_config)

    logger.info(f"Web dashboard starting on http://{host}:{port}")
    await server.serve()
