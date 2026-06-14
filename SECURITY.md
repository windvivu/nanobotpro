# Nanobot Pro Security Notes

Nanobot Pro can read/write local files, call LLM providers, run tools, start subprocesses, and manage bot processes through Adminbot. Treat it as a privileged local operator tool.

## Secrets And Runtime State

Do not commit runtime state or secrets.

Sensitive files commonly live under:

```text
~/.nanobot/
.adminbot/
```

These locations may contain:

- API keys
- OAuth tokens
- chat history
- memory files
- bot configs
- process logs
- provider errors that may include sensitive text

Recommended permissions on Linux/macOS:

```bash
chmod 700 ~/.nanobot
chmod 600 ~/.nanobot/config.json
```

Adminbot also attempts to protect `.adminbot/auth.json` with `0600` on POSIX systems.

## Adminbot

Adminbot can create, start, stop, restart, delete, and inspect local bot processes.

Security rules:

- First login password is `abc123`.
- Change the password on `127.0.0.1` before exposing Adminbot to another machine.
- Keep Adminbot bound to `127.0.0.1` unless remote access is deliberate.
- If binding `0.0.0.0`, put it behind a VPN, reverse proxy with auth, SSH tunnel, or equivalent access-control layer.
- Adminbot uses plain HTTP by default; without TLS, session cookies can be exposed on the network.
- Login is rate-limited in memory by client IP and username. If a reverse proxy hides client IPs, add rate limiting at the proxy.

## Channels

For chat channels, configure allow-lists where available. Avoid exposing bots to public users unless you explicitly want that behavior.

Examples of sensitive channel data:

- Telegram bot token
- Zalo/WhatsApp session data
- user IDs and phone numbers
- attached files and media

## Tools And MCP

Nanobot tools may access files, run shell commands, call web APIs, or connect to MCP servers.

Operate with these assumptions:

- Do not run as root/admin unless necessary.
- Use a dedicated OS user for production-like use.
- Review enabled MCP servers before running.
- TradingView MCP is a managed external backend; install only from the pinned source you trust.
- MCP/tool outputs can be large or sensitive and may be sent to the model.

## Provider Privacy

LLM providers receive prompts, tool outputs, and conversation context. Use providers and accounts appropriate for the data you send.

For production-like usage:

- Use separate API keys/accounts.
- Set provider spending limits where possible.
- Rotate compromised keys immediately.
- Avoid sending private data to providers that should not see it.

## Incident Response

If you suspect a leak or compromise:

1. Stop affected bots.
2. Revoke or rotate API keys and OAuth tokens.
3. Delete suspicious sessions or runtime files.
4. Review `.adminbot/logs/` and provider logs.
5. Change Adminbot password.
6. Restart Adminbot and the affected bot processes.

## Reporting

Report security issues privately to the project maintainer. Do not publish secrets, tokens, logs, or exploit details in public issues.
