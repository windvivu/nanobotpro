/**
 * Zalo Bridge — Node.js process that communicates with Python via stdin/stdout JSON lines.
 *
 * Protocol:
 *   stdout → Python:
 *     {"event":"qr",          "data":"<base64 or text>"}
 *     {"event":"ready",       "userId":"..."}
 *     {"event":"message",     "threadId":"...", "threadType":"User"|"Group", "content":"...", "senderId":"...", "senderName":"...",
 *                             "mediaUrl":"...", "mediaThumb":"...", "mediaType":"photo"|"video"|"voice"|"gif"|"file"|null}
 *     {"event":"sent",        "cmdId":"...", "threadId":"..."}                 — ack for a command that carried a cmdId
 *     {"event":"send_error",  "cmdId":"...", "threadId":"...", "message":"..."}
 *     {"event":"disconnected","reason":"max_reconnect_reached"} — retries exhausted, session kept (Python restarts bridge)
 *     {"event":"disconnected","reason":"auth_expired"}          — server rejected the saved session, QR needed (bridge exits)
 *     {"event":"disconnected","reason":"duplicate_connection"}  — account opened in Zalo Web/PC elsewhere (bridge exits)
 *     {"event":"error",       "message":"..."}
 *
 *   stdin ← Python:
 *     {"action":"send",       "threadId":"...", "threadType":"User"|"Group", "content":"..."}
 *     {"action":"send_media", "threadId":"...", "threadType":"User"|"Group", "content":"...", "filePath":"...", "mediaType":"photo"|"video"|"voice"|"gif"|"file"}
 *     {"action":"stop"}
 */

const { Zalo, ThreadType } = require("zca-js");
const readline = require("readline");
const fs = require("fs");
const path = require("path");

// ── Helpers ──────────────────────────────────────────────

function emit(obj) {
    process.stdout.write(JSON.stringify(obj) + "\n");
}

function logErr(msg) {
    // stderr is for debug logs (Python reads stdout only for protocol)
    process.stderr.write(`[zalo_bridge] ${msg}\n`);
}

// ── Main ─────────────────────────────────────────────────

let api = null;

// ── Reconnect state ────────────────────────────────────────────────────
let reconnectTimer = null;
let reconnectAttempts = 0;
let stableResetTimer = null;  // delayed counter-reset (only after connection is proven stable)
const MAX_RECONNECT_ATTEMPTS = 5;
const BASE_RECONNECT_DELAY_MS = 5000; // 5 s, doubles each attempt (max ~2.5 min)
const MIN_STABLE_CONNECTION_MS = 10000; // connection must stay alive 10s before we consider it "real"

// ── Liveness ───────────────────────────────────────────────────────────
// zca-js pings at the application level; on top we send WebSocket pings, which
// the server must answer with a pong (RFC 6455). Any inbound frame, pong or
// successful send counts as activity, so a healthy but quiet chat is never
// restarted. Only a connection silent this long (e.g. dropped by a NAT/router
// without a close) is terminated → "closed" → scheduleReconnect().
let lastActivityAt = Date.now();
let heartbeatTimer = null;
const HEARTBEAT_INTERVAL_MS     = 60 * 1000;     // ping + check every minute
const IDLE_RESTART_THRESHOLD_MS = 5 * 60 * 1000; // no traffic/pong this long = dead

// Close codes (zca-js CloseReason) meaning the account was opened in another
// Zalo Web/PC session — only one web listener per account. Reconnecting at once
// would kick that session back (ping-pong): yield, Python retries much later.
const YIELD_CLOSE_CODES = [3000, 3003]; // DuplicateConnection, KickConnection

// Re-save credentials periodically: every API call can refresh cookies in the
// in-memory jar, and a restart should log in with the freshest ones.
const CREDS_REFRESH_INTERVAL_MS = 30 * 60 * 1000;
let credsRefreshTimer = null;
let lastSavedCreds = "";

// Credentials file path (same dir as bridge script)
const CREDS_FILE = path.join(__dirname, "credentials.json");
// Rejected credentials are moved here instead of being deleted, so a wrong
// "expired" verdict can still be undone by renaming the file back.
const REJECTED_CREDS_FILE = path.join(__dirname, "credentials.rejected.json");

function saveCredentials(api) {
    try {
        const ctx = api.getContext();
        const cookieJar = api.getCookie();
        const creds = {
            imei: ctx.imei,
            cookie: cookieJar.toJSON(),
            userAgent: ctx.userAgent,
        };
        const snapshot = JSON.stringify(creds);
        if (snapshot === lastSavedCreds) return; // nothing new to persist
        const body = JSON.stringify({ ...creds, savedAt: new Date().toISOString() }, null, 2);
        // Write-then-rename, so a crash or power cut mid-write never leaves a torn file.
        const tmpFile = CREDS_FILE + ".tmp";
        fs.writeFileSync(tmpFile, body);
        try {
            fs.renameSync(tmpFile, CREDS_FILE);
        } catch (_) {
            fs.writeFileSync(CREDS_FILE, body); // e.g. target briefly locked on Windows
            try { fs.unlinkSync(tmpFile); } catch (_) {}
        }
        lastSavedCreds = snapshot;
        logErr("Credentials saved to " + CREDS_FILE);
    } catch (err) {
        logErr("Warning: Could not save credentials: " + err.message);
    }
}

function markActivity() {
    lastActivityAt = Date.now();
}

/** Start (or restart) the listener and watch its socket for liveness signals. */
function startListener() {
    api.listener.start();
    markActivity();
    const ws = api.listener.ws; // the `ws` WebSocket zca-js just created
    if (ws && typeof ws.on === "function") {
        ws.on("message", markActivity);
        ws.on("pong", markActivity);
    }
}

function heartbeat() {
    const ws = api && api.listener && api.listener.ws;
    // Handshake/closing, or a reconnect already pending: the reconnect logic owns
    // this phase — do not pile a restart on top of it.
    if (!ws || ws.readyState !== ws.OPEN || reconnectTimer) return;
    const idleMs = Date.now() - lastActivityAt;
    if (idleMs > IDLE_RESTART_THRESHOLD_MS) {
        logErr(`Idle watchdog: no traffic or pong for ${Math.round(idleMs / 1000)}s — terminating dead connection`);
        ws.terminate(); // → "closed" → scheduleReconnect()
        return;
    }
    try { ws.ping(); } catch (_) {}
}

function loadCredentials() {
    try {
        if (!fs.existsSync(CREDS_FILE)) return null;
        const raw = fs.readFileSync(CREDS_FILE, "utf-8");
        const creds = JSON.parse(raw);
        if (creds.imei && creds.cookie && creds.userAgent) {
            logErr("Found saved credentials (saved: " + (creds.savedAt || "unknown") + ")");
            return creds;
        }
        return null;
    } catch (err) {
        logErr("Warning: Could not load credentials: " + err.message);
        return null;
    }
}

function quarantineCredentials() {
    try {
        if (fs.existsSync(CREDS_FILE)) {
            fs.renameSync(CREDS_FILE, REJECTED_CREDS_FILE);
            logErr("Moved rejected credentials to " + REJECTED_CREDS_FILE);
        }
    } catch (err) {
        logErr("Warning: Could not move rejected credentials: " + err.message);
        // Never leave a rejected session in place: it would be retried forever.
        try { fs.unlinkSync(CREDS_FILE); } catch (_) {}
    }
}

/** Emit a final event, then exit once it is flushed (pipe writes can be async on Windows). */
function emitAndExit(obj, code) {
    process.stdout.write(JSON.stringify(obj) + "\n", () => process.exit(code));
    setTimeout(() => process.exit(code), 2000); // safety net if the write never completes
}

// ── Session rejection detection ───────────────────────────────────────

// zca-js reports a cookie login refused by the Zalo server as ZaloApiError
// "Đăng nhập thất bại" (no session data returned). The English words cover other
// zca-js versions and HTTP 401. Network failures ("fetch failed", timeouts, 5xx)
// never match, so they are retried instead of costing the saved session.
const SESSION_REJECTED_PATTERNS = [
    "đăng nhập thất bại", "khởi tạo ngữ cảnh thất bại",
    "invalid", "expired", "unauthorized", "logged out",
];

function isSessionRejected(err) {
    const lower = ((err && err.message) || "").toLowerCase();
    return SESSION_REJECTED_PATTERNS.some((p) => lower.includes(p));
}

// ── Cleanup helper ────────────────────────────────────────────────────

function cleanup() {
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
    if (credsRefreshTimer) { clearInterval(credsRefreshTimer); credsRefreshTimer = null; }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (stableResetTimer) { clearTimeout(stableResetTimer); stableResetTimer = null; }
    if (api && api.listener) {
        try { api.listener.stop(); } catch (_) {}
    }
}

// ── Auto-reconnect helper ─────────────────────────────────────────────

function scheduleReconnect() {
    if (reconnectTimer) return; // already queued

    if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        logErr(`Max reconnect attempts (${MAX_RECONNECT_ATTEMPTS}) reached — giving up`);
        // Python restarts the bridge process (session kept, no QR); this one is done.
        cleanup();
        emitAndExit({ event: "disconnected", reason: "max_reconnect_reached" }, 0);
        return;
    }

    const delay = BASE_RECONNECT_DELAY_MS * Math.pow(2, reconnectAttempts);
    reconnectAttempts++;
    logErr(`Listener closed — retry in ${Math.round(delay / 1000)}s (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);

    reconnectTimer = setTimeout(async () => {
        reconnectTimer = null;
        try {
            logErr(`Reconnecting listener (attempt ${reconnectAttempts})...`);
            startListener();
            logErr("Listener restarted, waiting for 'connected' event...");
            // reconnectAttempts will be reset to 0 on the "connected" event
        } catch (err) {
            logErr(`Reconnect attempt failed: ${err.message}`);
            scheduleReconnect(); // doubles the backoff delay
        }
    }, delay);
}

async function main() {
    logErr("Starting Zalo bridge...");

    // Read commands before logging in, so a "stop" command or a closed stdin
    // (Python gone) ends this process in every phase — never an orphan bridge.
    startCommandReader();

    const zalo = new Zalo();

    // Try login with saved credentials first
    const savedCreds = loadCredentials();
    if (!savedCreds && fs.existsSync(CREDS_FILE)) {
        // Unusable session file (e.g. torn write on power loss): handle it like a
        // rejected session instead of silently starting an unattended QR login.
        logErr("Saved credentials unusable — QR re-login required");
        quarantineCredentials();
        emitAndExit({ event: "disconnected", reason: "auth_expired" }, 0);
        return;
    }
    if (savedCreds) {
        // Retry the saved-session login: right after a reboot the network or the
        // Zalo server is often not ready yet. Only a login the server refuses
        // several times IN A ROW counts as an expired session — one odd response
        // must never cost the saved session.
        const MAX_SAVED_LOGIN_ATTEMPTS = 4;
        const REJECTIONS_TO_EXPIRE = 3;
        // zca-js also reports a server-side refusal with HTTP 200 (rate limit,
        // anti-bot, maintenance) as "Đăng nhập thất bại", so rejections are spread
        // over ~2 minutes before the session is declared dead. Python's connect
        // watchdog (240s) outlasts this sequence.
        const REJECTION_RETRY_DELAYS_MS = [30000, 90000];
        let rejections = 0;
        for (let attempt = 1; attempt <= MAX_SAVED_LOGIN_ATTEMPTS; attempt++) {
            try {
                logErr(`Attempting login with saved credentials (attempt ${attempt}/${MAX_SAVED_LOGIN_ATTEMPTS})...`);
                api = await zalo.login({
                    imei: savedCreds.imei,
                    cookie: savedCreds.cookie,
                    userAgent: savedCreds.userAgent,
                });
                logErr("Login with saved credentials successful!");
                // Re-save to refresh cookie expiry
                saveCredentials(api);
                break;
            } catch (err) {
                logErr("Saved credentials login failed: " + err.message);
                api = null;
                if (isSessionRejected(err)) {
                    rejections++;
                    if (rejections >= REJECTIONS_TO_EXPIRE) {
                        // The session is really dead. Set the file aside and exit:
                        // the QR flow must only start from an explicit Connect —
                        // zca-js would otherwise regenerate an unattended QR every
                        // 100s forever.
                        logErr(`Session rejected ${rejections} times in a row — QR re-login required`);
                        quarantineCredentials();
                        emitAndExit({ event: "disconnected", reason: "auth_expired" }, 0);
                        return;
                    }
                } else {
                    rejections = 0; // a transient error breaks the streak
                }
                if (attempt < MAX_SAVED_LOGIN_ATTEMPTS) {
                    const delay = rejections > 0
                        ? REJECTION_RETRY_DELAYS_MS[rejections - 1]
                        : BASE_RECONNECT_DELAY_MS * Math.pow(2, attempt - 1);
                    logErr(`Retrying saved-session login in ${Math.round(delay / 1000)}s...`);
                    await new Promise((r) => setTimeout(r, delay));
                }
            }
        }

        if (!api) {
            // Retries exhausted without a consistent rejection — most likely the
            // network/server is down. Keep the session and let Python restart the
            // bridge with backoff. Do NOT fall through to loginQR(): it would hang
            // invisibly waiting for a human scan.
            logErr("Saved-session login exhausted all retries — asking Python to restart bridge");
            emitAndExit({ event: "disconnected", reason: "max_reconnect_reached" }, 0);
            return;
        }
    }

    // No saved session: QR login (needs a human to scan)
    if (!api) {
        try {
            logErr("Waiting for QR code scan...");
            api = await zalo.loginQR({
                qrPath: undefined,
            });
            logErr("QR Login successful!");
            // Save credentials for next time; a previously rejected session is obsolete now
            saveCredentials(api);
            try { fs.unlinkSync(REJECTED_CREDS_FILE); } catch (_) {}
        } catch (err) {
            emitAndExit({ event: "error", message: `Login failed: ${err.message}` }, 1);
            return;
        }
    }

    // Notify Python that we're ready
    const selfId = api.getOwnId ? String(api.getOwnId()) : "unknown";
    emit({ event: "ready", userId: selfId });

    // ── Listen for incoming messages ──
    api.listener.on("message", (message) => {
        lastActivityAt = Date.now(); // reset idle timer on any incoming message
        try {
            const content = message.data.content;

            // Skip self-sent messages (double-check: isSelf flag + senderId match)
            const senderId = String(message.data.uidFrom || message.threadId);
            if (message.isSelf || senderId === selfId) {
                return;
            }

            // ── Extract text and media info ──
            let textContent = "";
            let mediaUrl = null;
            let mediaThumb = null;
            let mediaType = null;

            if (typeof content === "string") {
                textContent = content;
            } else if (content && typeof content === "object") {
                // content is TAttachmentContent: { href, thumb, title, description, ... }
                mediaUrl   = content.href  || null;
                mediaThumb = content.thumb || null;
                textContent = content.title || content.description || "";

                // Map msgType → mediaType
                const msgType = message.data.msgType || "";
                const typeMap = {
                    "chat.photo":   "photo",
                    "chat.video":   "video",
                    "chat.voice":   "voice",
                    "chat.gif":     "gif",
                    "chat.sticker": "sticker",
                };
                mediaType = typeMap[msgType] || "file";
                logErr(`Media message: type=${mediaType}, url=${mediaUrl ? mediaUrl.slice(0, 60) + "..." : "none"}`);
            } else {
                // Unknown content type — skip
                logErr(`Unknown content type from ${message.threadId}: ${typeof content}`);
                return;
            }

            // Skip if neither text nor media
            if (!textContent && !mediaUrl) return;

            const threadTypeStr =
                message.type === ThreadType.Group ? "Group" : "User";

            // Extract mentioned user IDs — zca-js can return:
            //   { userId: displayName }  (object)
            //   [{ uid: "...", ... }]    (array of objects)
            //   ["uid1", "uid2"]         (array of strings)
            const rawMentions = message.data.mentions;
            let mentionedIds = [];
            try {
                if (rawMentions) {
                    if (Array.isArray(rawMentions)) {
                        mentionedIds = rawMentions.map((m) => {
                            try {
                                // object form: { uid, userId, id, ... }
                                const uid = m && typeof m === "object"
                                    ? (m.uid || m.userId || m.id || null)
                                    : m;
                                return uid != null ? String(uid) : null;
                            } catch (_) { return null; }
                        }).filter(Boolean);
                    } else if (typeof rawMentions === "object") {
                        // { userId: displayName } — keys are the IDs
                        mentionedIds = Object.keys(rawMentions);
                    }
                }
            } catch (_) {}

            // Extract quote/reply info — all conversions wrapped defensively
            // zca-js quoteData fields: ownerId, msg, attach, fromD, cliMsgId, ts, ttl, ...
            const quoteData = message.data.quote;
            let quotedSenderId = null;
            let quotedContent = null;
            if (quoteData) {
                // ownerId: ID of the person whose message was quoted
                try {
                    const oid = quoteData.ownerId;
                    if (oid != null) quotedSenderId = String(oid);
                } catch (_) {}

                // msg: the actual text content of the quoted message
                try {
                    if (typeof quoteData.msg === "string" && quoteData.msg) {
                        quotedContent = quoteData.msg;
                    }
                } catch (_) {}

                // attach: JSON string fallback (for older message types)
                if (!quotedContent) {
                    try {
                        const attach = quoteData.attach;
                        if (typeof attach === "string" && attach) {
                            const parsed = JSON.parse(attach);
                            const c = parsed.content || parsed.msg || parsed.text;
                            if (typeof c === "string") quotedContent = c;
                        } else if (attach && typeof attach === "object") {
                            const c = attach.content || attach.msg || attach.text;
                            if (typeof c === "string") quotedContent = c;
                        }
                    } catch (_) {}
                }
            }

            emit({
                event: "message",
                threadId: String(message.threadId),
                threadType: threadTypeStr,
                content: textContent,
                senderId: String(message.data.uidFrom || message.threadId),
                senderName: message.data.dName || "",
                mentionedIds: mentionedIds,
                quotedSenderId: quotedSenderId,
                quotedContent: quotedContent,
                // ── Media fields (null for text-only messages) ──
                mediaUrl:   mediaUrl,
                mediaThumb: mediaThumb,
                mediaType:  mediaType,
            });
        } catch (err) {
            logErr(`Error processing message: ${err.message}`);
        }
    });

    // Handle listener events
    api.listener.on("error", (err) => {
        // Never judge the session from listener errors: zca-js also reports
        // per-message decode failures here (e.g. "Invalid time value"). A revoked
        // session closes the socket instead; reconnects then run out and the
        // restarted bridge's saved-session login gives the real verdict.
        const message = (err && err.message) || String(err);
        logErr(`Listener error: ${message}`);
        emit({ event: "error", message });
    });

    api.listener.on("connected", () => {
        logErr("Listener connected");
        lastActivityAt = Date.now();
        // Do NOT reset reconnectAttempts immediately.
        // Schedule a delayed reset: if the connection stays alive for
        // MIN_STABLE_CONNECTION_MS, THEN it's a real connection and we reset.
        // If "closed" fires before that, we cancel this timer.
        if (stableResetTimer) clearTimeout(stableResetTimer);
        stableResetTimer = setTimeout(() => {
            stableResetTimer = null;
            if (reconnectAttempts > 0) {
                logErr(`Connection stable for ${MIN_STABLE_CONNECTION_MS / 1000}s — resetting reconnect counter`);
                reconnectAttempts = 0;
            }
        }, MIN_STABLE_CONNECTION_MS);
    });

    api.listener.on("closed", (code, reason) => {
        // Cancel the "stable connection" timer — this connection wasn't real
        if (stableResetTimer) { clearTimeout(stableResetTimer); stableResetTimer = null; }
        if (YIELD_CLOSE_CODES.includes(code)) {
            logErr(`Listener closed (code ${code}): account opened in another Zalo Web/PC session — yielding`);
            cleanup();
            emitAndExit({ event: "disconnected", reason: "duplicate_connection", code }, 0);
            return;
        }
        logErr(`Listener closed (code ${code}${reason ? `, ${reason}` : ""}) — scheduling auto-reconnect`);
        // Do NOT emit "disconnected" to Python yet.
        // scheduleReconnect() will only give up (and emit) after MAX_RECONNECT_ATTEMPTS.
        scheduleReconnect();
    });

    // Start listening
    startListener();
    logErr("Listener started, waiting for messages...");

    heartbeatTimer = setInterval(heartbeat, HEARTBEAT_INTERVAL_MS);
    credsRefreshTimer = setInterval(() => saveCredentials(api), CREDS_REFRESH_INTERVAL_MS);
}

/** Tell Python how a send command ended (only commands that asked for an ack). */
function ack(cmd, errorMessage) {
    if (!cmd || !cmd.cmdId) return; // fire-and-forget command, e.g. the progress hint
    if (errorMessage) {
        emit({ event: "send_error", cmdId: cmd.cmdId, threadId: cmd.threadId || "", message: errorMessage });
    } else {
        emit({ event: "sent", cmdId: cmd.cmdId, threadId: cmd.threadId });
    }
}

// ── Read commands from stdin (Python → Node.js) ──
// Started at the top of main(), before login, and kept for the process lifetime.
function startCommandReader() {
    const rl = readline.createInterface({ input: process.stdin });

    rl.on("line", async (line) => {
        let cmd = null;
        try {
            cmd = JSON.parse(line);

            if (cmd.action === "send") {
                if (!api) {
                    logErr("Cannot send: API not ready");
                    ack(cmd, "API not ready");
                    return;
                }
                const threadType =
                    cmd.threadType === "Group" ? ThreadType.Group : ThreadType.User;

                await api.sendMessage(
                    { msg: cmd.content },
                    cmd.threadId,
                    threadType
                );
                logErr(`Sent message to ${cmd.threadId}`);
                markActivity();
                ack(cmd);

            } else if (cmd.action === "send_media") {
                if (!api) {
                    logErr("Cannot send media: API not ready");
                    ack(cmd, "API not ready");
                    return;
                }
                const threadType = cmd.threadType === "Group" ? ThreadType.Group : ThreadType.User;
                const filePath = cmd.filePath;
                const caption  = cmd.content || "";
                const mType    = cmd.mediaType || "file";

                try {
                    if (mType === "video") {
                        // Video: must upload first to get URL
                        logErr(`Uploading video: ${filePath}`);
                        const uploaded = await api.uploadAttachment(filePath, cmd.threadId, threadType);
                        const item = uploaded[0];
                        if (!item || item.fileType !== "video") throw new Error("Upload video failed or wrong type");
                        await api.sendVideo(
                            {
                                msg: caption,
                                videoUrl: item.fileUrl,
                                thumbnailUrl: item.fileUrl, // zca-js requires thumbnailUrl; no separate thumb from uploadAttachment — fallback to same URL
                            },
                            cmd.threadId,
                            threadType
                        );
                        logErr(`Sent video to ${cmd.threadId}`);

                    } else if (mType === "voice") {
                        // Voice: must upload first to get URL
                        logErr(`Uploading voice: ${filePath}`);
                        const uploaded = await api.uploadAttachment(filePath, cmd.threadId, threadType);
                        const item = uploaded[0];
                        if (!item) throw new Error("Upload voice failed");
                        await api.sendVoice(
                            { voiceUrl: item.fileUrl },
                            cmd.threadId,
                            threadType
                        );
                        logErr(`Sent voice to ${cmd.threadId}`);

                    } else {
                        // photo, gif, file — send via Buffer (no need for sharp/imageMetadataGetter)
                        logErr(`Sending attachment (${mType}): ${filePath}`);
                        const data = await fs.promises.readFile(filePath);
                        const filename = path.basename(filePath);
                        await api.sendMessage(
                            {
                                msg: caption,
                                attachments: {
                                    data: data,
                                    filename: filename,
                                    metadata: { totalSize: data.length },
                                },
                            },
                            cmd.threadId,
                            threadType
                        );
                        logErr(`Sent ${mType} to ${cmd.threadId}`);
                    }
                    markActivity();
                    ack(cmd);
                } catch (err) {
                    logErr(`Failed to send media (${mType}): ${err.message}`);
                    ack(cmd, err.message || `Failed to send media (${mType})`);
                    // Do not re-throw: media send errors should not kill the bridge
                }

            } else if (cmd.action === "stop") {
                logErr("Stop command received, shutting down...");
                if (api) saveCredentials(api); // freshest cookies for the next start
                cleanup();
                process.exit(0);
            } else {
                logErr(`Unknown action: ${cmd.action}`);
            }
        } catch (err) {
            logErr(`Error handling command: ${err.message}`);
            ack(cmd, err.message || "Error handling command");
        }
    });

    rl.on("close", () => {
        logErr("stdin closed, shutting down...");
        cleanup();
        process.exit(0);
    });
}

// Handle graceful shutdown
process.on("SIGTERM", () => {
    logErr("SIGTERM received");
    cleanup();
    process.exit(0);
});

process.on("SIGINT", () => {
    logErr("SIGINT received");
    cleanup();
    process.exit(0);
});

main().catch((err) => {
    logErr(`Fatal error: ${err.stack}`);
    emitAndExit({ event: "error", message: err.message }, 1);
});
