# Adminbot Operator Notes

Adminbot is a local multi-bot manager. It can create, start, stop, restart, and delete local Nanobot gateway processes. Treat it as an operator tool with high local privileges.

## When To Use Adminbot

Use Adminbot when you want to run multiple bot instances from one repo and manage them from one local control surface.

Use a plain `nanobot gateway --web` when you only need one bot.

## Start Adminbot

Recommended wrapper:

```powershell
.\nanobot-launcher.cmd
```

PowerShell variant:

```powershell
.\nanobot-launcher.ps1 -Port 8900
```

Ubuntu/macOS shell variant:

```bash
chmod +x ./nanobot-launcher.sh
./nanobot-launcher.sh 8900
```

Bind to all interfaces only when another machine must connect:

```powershell
.\nanobot-launcher.ps1 -Port 8900 -HostName 0.0.0.0
```

Linux shell equivalent:

```bash
./nanobot-launcher.sh 8900 0.0.0.0
```

First login uses `abc123`. Adminbot requires changing that password before bot operations are enabled.

Direct command:

```powershell
adminbot web --port 8900
```

On Ubuntu/macOS, the direct command is:

```bash
adminbot web --port 8900
```

If the `adminbot` command is not available, reinstall the local package or use the module fallback:

```powershell
.\venv\Scripts\python.exe -m adminbot.app.main web --port 8900
```

On Ubuntu, Adminbot starts child gateways in a separate process group and stops that group with `SIGTERM`. This is safer than killing only the parent PID when a child gateway starts subprocesses.

Linux stop note: Adminbot sends `SIGTERM` to the bot process group and marks the bot stopped. It does not wait and escalate to `SIGKILL` in the current wave. If a process ignores `SIGTERM`, check the OS process list and stop it manually.

Linux verification note: process identity is read from `/proc/<pid>/exe` and `/proc/<pid>/stat` when available. The behavior is covered by mocked tests on Windows; verify manually on Ubuntu once before relying on it for long-running production use.

Open:

```text
http://127.0.0.1:8900
```

## Runtime Files

Adminbot writes runtime state under `.adminbot/`:

```text
.adminbot/
  bots.json
  instances/
  logs/
  run/
```

Meanings:

- `bots.json`: registered bot list.
- `instances/`: per-bot generated config files.
- `logs/`: per-bot stdout/stderr logs.
- `run/`: per-bot process state.

`.adminbot/` is gitignored. Do not commit it. It can contain API keys, OAuth-related config, chat logs, error logs, workspace paths, and process metadata.

## Security Model

Adminbot web requires login:

- First login password is `abc123`.
- Password change is required before bot operations are enabled.
- Login is rate-limited in memory by client and username. Repeated failed attempts are temporarily locked.
- If Adminbot sits behind a reverse proxy, the in-app limiter sees the proxy address unless the deployment preserves real client IPs. Put rate limiting or access control at the proxy for shared/remote deployments.
- Uvicorn binds `127.0.0.1` by default.
- Password hashes are stored in `.adminbot/auth.json`. On POSIX systems Adminbot attempts to set this file to mode `0600`; verify filesystem permissions if you run it on a shared host.

Important remote-access warning:

- `abc123` is public source-code knowledge. If Adminbot is reachable remotely before you change it, another user who can reach the port can log in first and take control.
- Adminbot serves plain HTTP by default. On a remote/LAN bind, the session cookie can travel without TLS unless you put Adminbot behind a secure reverse proxy or tunnel.
- Change the password on `127.0.0.1` first, then bind `0.0.0.0` only behind a deliberate access-control layer.

`--host 0.0.0.0` makes Uvicorn listen on all interfaces. Use it only after changing the default password locally.

Do not expose Adminbot to LAN/public networks unless you add an external access-control layer such as a local VPN, reverse proxy with auth, or another deliberate gate.

Why this matters:

- Adminbot can start and stop bot processes.
- Adminbot can open shell windows.
- Adminbot can read bot stdout/stderr logs.
- Logs may include user messages, provider errors, paths, and secrets if another component logs them.

## Common Operations

List bots:

```powershell
adminbot list
```

Create a bot:

```powershell
adminbot create --workspace .\bots\alpha --name alpha --web-port 8901
```

Start a bot:

```powershell
adminbot start alpha
```

Stop a bot:

```powershell
adminbot stop alpha
```

Restart a bot:

```powershell
adminbot restart alpha
```

Check status:

```powershell
adminbot status alpha
```

## Troubleshooting

### Missing venv

If the launcher says `Missing local venv Python`, create/install the environment:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[web,dev]"
```

### Missing web dependencies

If Adminbot web says dependencies are missing, install with web extras:

```powershell
.\venv\Scripts\python.exe -m pip install -e ".[web]"
```

### Port already busy

Each child bot needs its own web port. Pick a different `--web-port` when creating the bot.

Adminbot itself also needs a free manager port, default `8900`.

### Ubuntu launcher cannot find Python

`nanobot-launcher.sh` looks for:

```text
venv/bin/python
.venv/bin/python
```

Create the venv in one of those locations, or run the module directly with your chosen Python:

```bash
python -m adminbot.app.main web --port 8900
```

### Bot cannot be deleted

Stop the bot first. Adminbot refuses to delete a bot that may still be running.

### Bot shows stale running state after crash

Restart Adminbot. Startup reconciliation refreshes saved state against live processes.

### Logs are too large

Logs rotate when a bot starts and its current stdout/stderr log is roughly `5 MB`; up to 3 archives are kept per stream.

### Adminbot restart vs bot restart

Restarting Adminbot only restarts the manager process. It does not automatically restart every child bot.

Use each bot's Restart action or CLI command to restart a child bot.

## Design Boundaries

Adminbot is not Fleet API.

- Adminbot manages local OS processes.
- Fleet API coordinates already-running bots over HTTP.

Adminbot does not import Nanobot runtime internals. It controls bots through:

```text
python -m nanobot.cli.commands onboard --config <config> --workspace <workspace>
python -m nanobot.cli.commands gateway --web --config <config> --workspace <workspace>
```
