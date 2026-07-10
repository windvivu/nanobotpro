/**
 * Zalo Bridge — Node.js process that communicates with Python via stdin/stdout JSON lines.
 *
 * Protocol:
 *   stdout → Python:
 *     {"event":"qr",          "data":"<base64 or text>"}
 *     {"event":"ready",       "userId":"..."}
 *     {"event":"message",     "threadId":"...", "threadType":"User"|"Group", "content":"...", "senderId":"...", "senderName":"...",
 *                             "mediaUrl":"...", "mediaThumb":"...", "mediaType":"photo"|"video"|"voice"|"gif"|"file"|null}
 *     {"event":"disconnected","reason":"..."}   — only when auto-reconnect gives up
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

// ── Keepalive/idle state ───────────────────────────────────────────────
let lastActivityAt = Date.now();
let keepaliveTimer = null;
const KEEPALIVE_INTERVAL_MS    = 2 * 60 * 1000; // check every 2 min
const IDLE_RESTART_THRESHOLD_MS = 5 * 60 * 1000; // proactive restart after 5 min idle

// Credentials file path (same dir as bridge script)
const CREDS_FILE = path.join(__dirname, "credentials.json");

function saveCredentials(api) {
    try {
        const ctx = api.getContext();
        const cookieJar = api.getCookie();
        const creds = {
            imei: ctx.imei,
            cookie: cookieJar.toJSON(),
            userAgent: ctx.userAgent,
            savedAt: new Date().toISOString(),
        };
        fs.writeFileSync(CREDS_FILE, JSON.stringify(creds, null, 2));
        logErr("Credentials saved to " + CREDS_FILE);
    } catch (err) {
        logErr("Warning: Could not save credentials: " + err.message);
    }
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

function deleteCredentials() {
    try {
        if (fs.existsSync(CREDS_FILE)) {
            fs.unlinkSync(CREDS_FILE);
            logErr("Deleted saved credentials");
        }
    } catch (err) {
        logErr("Warning: Could not delete credentials: " + err.message);
    }
}

// ── Auth error detection ──────────────────────────────────────────────

const AUTH_ERROR_KEYWORDS = [
    "invalid", "expired", "unauthorized", "logged out",
    "kicked", "logged_out", "session", "revoked",
    "another device", "thiết bị khác",
];

function isAuthError(errMsg) {
    const lower = (errMsg || "").toLowerCase();
    return AUTH_ERROR_KEYWORDS.some((kw) => lower.includes(kw));
}

/** Immediately give up reconnection, clear creds, and notify Python. */
function abortWithAuthExpired(detail) {
    logErr(`Auth-related failure detected (${detail}) — aborting reconnect`);
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    deleteCredentials();
    emit({ event: "disconnected", reason: "auth_expired" });
}

// ── Cleanup helper ────────────────────────────────────────────────────

function cleanup() {
    if (keepaliveTimer) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
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
        // Notify Python: user must reconnect manually via dashboard
        emit({ event: "disconnected", reason: "max_reconnect_reached" });
        return;
    }

    const delay = BASE_RECONNECT_DELAY_MS * Math.pow(2, reconnectAttempts);
    reconnectAttempts++;
    logErr(`Listener closed — retry in ${Math.round(delay / 1000)}s (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);

    reconnectTimer = setTimeout(async () => {
        reconnectTimer = null;
        try {
            logErr(`Reconnecting listener (attempt ${reconnectAttempts})...`);
            api.listener.start();
            logErr("Listener restarted, waiting for 'connected' event...");
            // reconnectAttempts will be reset to 0 on the "connected" event
        } catch (err) {
            logErr(`Reconnect attempt failed: ${err.message}`);
            if (isAuthError(err.message)) {
                abortWithAuthExpired(err.message);
            } else {
                scheduleReconnect(); // doubles the backoff delay
            }
        }
    }, delay);
}

async function main() {
    logErr("Starting Zalo bridge...");

    const zalo = new Zalo();

    // Try login with saved credentials first
    const savedCreds = loadCredentials();
    if (savedCreds) {
        // Retry saved-session login a few times on transient (non-auth) errors
        // before giving up. Without this, a single network/server hiccup would
        // fall straight through to loginQR() and silently hang waiting for a
        // human scan, leaving Python stuck in CONNECTING forever.
        const MAX_SAVED_LOGIN_ATTEMPTS = 4;
        let authExpired = false;
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
                // Only delete credentials on clear auth errors (expired/invalid session).
                // Keep the file for network errors so next start can retry.
                const errMsg = (err.message || "").toLowerCase();
                const isAuthError = errMsg.includes("invalid") || errMsg.includes("expired")
                    || errMsg.includes("unauthorized") || errMsg.includes("logged out");
                api = null;
                if (isAuthError) {
                    logErr("Auth error detected — clearing saved credentials");
                    deleteCredentials();
                    // Notify Python that re-authentication (QR scan) is required
                    emit({ event: "disconnected", reason: "auth_expired" });
                    authExpired = true;
                    break;
                }
                // Non-auth (transient) error — back off and retry.
                logErr("Non-auth error — keeping saved credentials for retry");
                if (attempt < MAX_SAVED_LOGIN_ATTEMPTS) {
                    const delay = BASE_RECONNECT_DELAY_MS * Math.pow(2, attempt - 1);
                    logErr(`Retrying saved-session login in ${Math.round(delay / 1000)}s...`);
                    await new Promise((r) => setTimeout(r, delay));
                }
            }
        }

        // On auth error, credentials were cleared above. Fall through to
        // loginQR() below (same as before) so a fresh QR is generated in this
        // same process — the dashboard shows it without the user re-clicking
        // Connect.
        if (!authExpired && !api) {
            // Exhausted all transient-error retries WITHOUT an auth error. Do NOT
            // silently fall through to loginQR() — that would hang invisibly,
            // leaving Python stuck in CONNECTING. The session is likely still
            // valid, so tell Python to restart the bridge; a fresh process may
            // succeed once the network/server recovers.
            logErr("Saved-session login exhausted all retries — asking Python to restart bridge");
            emit({ event: "disconnected", reason: "max_reconnect_reached" });
            process.exit(0);
        }
    }

    // Fallback to QR login if credential login failed or no saved creds
    if (!api) {
        try {
            logErr("Waiting for QR code scan...");
            api = await zalo.loginQR({
                qrPath: undefined,
            });
            logErr("QR Login successful!");
            // Save credentials for next time
            saveCredentials(api);
        } catch (err) {
            emit({ event: "error", message: `Login failed: ${err.message}` });
            process.exit(1);
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
        logErr(`Listener error: ${err.message}`);
        if (isAuthError(err.message)) {
            // Session invalidated (e.g. user logged out on phone / Zalo kicked)
            // → stop retrying immediately, notify Python to request QR re-scan
            abortWithAuthExpired(err.message);
            try { api.listener.stop(); } catch (_) {}
            return;
        }
        emit({ event: "error", message: err.message });
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

    api.listener.on("closed", () => {
        logErr("Listener closed — scheduling auto-reconnect");
        // Cancel the "stable connection" timer — this connection wasn't real
        if (stableResetTimer) { clearTimeout(stableResetTimer); stableResetTimer = null; }
        // Do NOT emit "disconnected" to Python yet.
        // scheduleReconnect() will only give up (and emit) after MAX_RECONNECT_ATTEMPTS.
        scheduleReconnect();
    });

    // Start listening
    api.listener.start();
    lastActivityAt = Date.now();
    logErr("Listener started, waiting for messages...");

    // ── Keepalive: proactively restart if idle too long ──
    // Some NAT/router setups silently drop idle WebSocket connections.
    // If no activity for IDLE_RESTART_THRESHOLD_MS, stop → "closed" → scheduleReconnect().
    keepaliveTimer = setInterval(() => {
        const idleMs = Date.now() - lastActivityAt;
        if (idleMs > IDLE_RESTART_THRESHOLD_MS) {
            logErr(`Connection idle for ${Math.round(idleMs / 1000)}s — proactive listener restart`);
            try { api.listener.stop(); } catch (_) {}
            // "closed" event fires → scheduleReconnect() handles the rest
        }
    }, KEEPALIVE_INTERVAL_MS);

    // ── Read commands from stdin (Python → Node.js) ──
    const rl = readline.createInterface({ input: process.stdin });

    rl.on("line", async (line) => {
        try {
            const cmd = JSON.parse(line);

            if (cmd.action === "send") {
                if (!api) {
                    logErr("Cannot send: API not ready");
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

            } else if (cmd.action === "send_media") {
                if (!api) {
                    logErr("Cannot send media: API not ready");
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
                } catch (err) {
                    logErr(`Failed to send media (${mType}): ${err.message}`);
                    // Do not re-throw: media send errors should not kill the bridge
                }

            } else if (cmd.action === "stop") {
                logErr("Stop command received, shutting down...");
                cleanup();
                process.exit(0);
            } else {
                logErr(`Unknown action: ${cmd.action}`);
            }
        } catch (err) {
            logErr(`Error handling command: ${err.message}`);
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
    emit({ event: "error", message: err.message });
    logErr(`Fatal error: ${err.stack}`);
    process.exit(1);
});
