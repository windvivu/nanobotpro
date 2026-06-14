# Adminbot

`adminbot` is a local multi-bot launcher and process manager for Nanobot.

This copy was adopted from the sibling `nanobotalone` repo under the MIT license. It is not from the upstream `nanobot_symbolic_link` source.

## Local Status

Current local adoption status: **Wave A core + Wave B local web UI**.

Included:

- CLI entrypoint: `adminbot`
- bot registry in `.adminbot/bots.json`
- per-bot config instances in `.adminbot/instances/`
- per-bot logs in `.adminbot/logs/`
- per-bot state files in `.adminbot/run/`
- background process start/stop/status/restart
- config sync through Nanobot CLI-compatible config files
- local web UI on `127.0.0.1`
- dashboard, create form, bot detail, and log viewer

Deferred to later waves:

- embedded terminal or PTY

## Quick Commands

```powershell
adminbot list
adminbot create --workspace .\bot-a --name bot-a --web-port 8899
adminbot start bot-a
adminbot stop bot-a
adminbot restart bot-a
adminbot status bot-a
adminbot web --port 8900
```

If the `adminbot` command is not available, reinstall the local package or use the module fallback:

```powershell
.\venv\Scripts\python.exe -m adminbot.app.main web --port 8900
```

The web UI is then available at:

```text
http://127.0.0.1:8900
```

## Coupling Contract

Adminbot does not import Nanobot runtime internals. It controls child bots only through CLI subprocesses:

```text
python -m nanobot.cli.commands onboard --config <config> --workspace <workspace>
python -m nanobot.cli.commands gateway --web --config <config> --workspace <workspace>
```

## Runtime Data

Runtime data lives under `.adminbot/` and is ignored by git because it can contain configs, logs, process state, and potentially secrets.

```text
.adminbot/
  bots.json
  instances/
  logs/
  run/
```

## Security Notes

Adminbot can start and stop local bot processes and read bot logs. Treat `.adminbot/` as sensitive runtime state.

The web UI requires login. The bootstrap password is `abc123`, and Adminbot requires changing it before bot operations are enabled. Keep Adminbot bound to `127.0.0.1` unless remote access is deliberate.

For day-to-day operating guidance, see `adminbot/OPERATOR_NOTES.md`.
