"""Entry point for starting the Adminbot web UI."""

from __future__ import annotations


def start_web(port: int = 8900, host: str = "127.0.0.1") -> None:
    try:
        import uvicorn

        from adminbot.app.web.app import create_app
    except ImportError as exc:
        raise RuntimeError(
            "Adminbot web UI dependencies are missing. Install the repo with the web extras "
            "before running `adminbot web`."
        ) from exc

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            timeout_graceful_shutdown=3,
        )
    )
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl+C: uvicorn has already shut down cleanly ("Finished server process")
        # and re-raises the signal for its caller; uvicorn.run() swallows it too.
        pass
