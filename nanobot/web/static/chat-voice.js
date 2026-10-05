/* Voice in Web Chat, in the normal layout and in Chat focus alike (styles in chat.html): the mic
   button turns speech into a message and sends it; reading aloud, switched in the ⋯ menu, reads the
   bot's answers sentence by sentence while they are written, each sentence in a voice of its own
   language. chat.html calls chatVoice.init(options) once, chatVoice.react(event) for each socket
   event, chatVoice.stopSpeaking() when Stop is pressed and chatVoice.stop() on Esc.

   Speech is recognised by the browser (Web Speech API) or by Groq's Whisper, as chosen in Thiết lập →
   Gateway Configuration (gateway.web.voice): for Groq the page records the mic and posts the audio
   to /chat/transcribe. The mic button carries the choice in data-recognition, data-language and
   data-groq-key (a flag: the key itself never reaches the page). */
(function () {
    "use strict";

    const DOUBLE_SPACE_MS = 400;  // two Space presses this close together open the mic
    const MAX_LISTEN_MS = 60000;  // the mic closes by itself after a minute
    const HANG_MS = 5000;         // Browser: no sound captured by then means no speech service answers
    const QUIET_STOP_MS = 2000;   // Groq: the mic closes this long after the speaker went quiet
    const NO_SPEECH_MS = 8000;    // Groq: ...or when nobody spoke at all
    const HEARD_MS = 200;         // Groq: this much speech means somebody is speaking
    const MIN_SPEECH_MS = 500;    // Groq: a recording with less speech than this is not sent
    const SPEECH_LEVEL = 0.01;    // Groq: loudness (RMS, 0 to 1) below this is never speech
    const TRANSCRIBE_MS = 45000;  // Groq: stop waiting for the text after this long
    const READ_MAX_CHARS = 600;   // a longer answer is read up to about here
    const SETTLE_MS = 400;        // after a reading was cut off, the next one waits this long
    const STREAM_EVERY_MS = 300;  // an answer being written is checked for new sentences this often
    const READ_KEY = "nanobot_chat_voice_read";
    const RECORD_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];

    // Reading aloud picks a voice per sentence, since a voice only speaks its own language. Vietnamese
    // shows in its letters: VI_MARKED are the marked letters it uses, VI_ONLY the ones no other
    // language has. NOT_VIETNAMESE is what no Vietnamese syllable looks like: f, j, w or z; two vowel
    // groups (a written Vietnamese word is one syllable); a doubled letter; "ou", "ea", "ei", "ey";
    // an ending Vietnamese never has.
    const VI_MARKED = /[àáâãèéêìíòóôõùúýăđĩũơư\u1ea0-\u1ef9]/i;
    const VI_ONLY = /[ăđĩũơư\u1ea0-\u1ef9]/i;
    const NOT_VIETNAMESE = /[fjwz]|[aeiouy][^aeiouy]+[aeiouy]|(.)\1|ou|ea|ei|ey|[bdklqrsvx]$|[^n]g$|[^cn]h$/;
    // macOS lists its joke voices and decades-old ones among the English voices, in alphabetical order
    const POOR_VOICE = /^(Albert|Bad News|Bahh|Bells|Boing|Bubbles|Cellos|Good News|Jester|Organ|Superstar|Trinoids|Whisper|Wobble|Zarvox|Fred|Junior|Kathy|Ralph)\b/;
    const LINK = "\uE000";  // stands for a bare web address until its sentence's language is known

    const SAY_AGAIN = "Không nghe rõ, bạn nói lại nhé";
    const USE_GROQ = "chọn Groq trong Thiết lập";
    const MIC_DENIED = "Micro đang bị chặn: hãy cho phép micro cho trang này trong trình duyệt";

    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const synth = window.speechSynthesis;

    let opts = {};
    let micBtn = null;
    let readBtn = null;
    let input = null;
    let noticeEl = null;
    let cat = { react() {} };
    let mode = "browser";      // or "groq"
    let language = "vi-VN";
    let hasGroqKey = false;

    let phase = "idle";        // "idle", "listening" (the mic is open) or "transcribing" (Groq)
    let session = null;        // the mic session in progress: { stop(), cancel() }
    let maxTimer = null;
    let micDenied = false;     // the browser refused the mic: said on the button until it works
    let lastSpace = 0;
    let noticeTimer = null;

    let wantRead = false;      // the speaker button's choice, remembered per browser
    let voices = {};           // language without region -> the installed voice it is read with
    let listed = 0;            // how many voices the browser listed when `voices` was filled
    let settled = false;       // the browser has had time to list its voices
    let speaking = false;
    let queue = [];            // pieces waiting to be read: [{ text, lang }]
    let current = null;        // the one piece the browser has: { utterance, started, stopped }
    let cutAt = -1e9;          // when the browser was last told to stop reading (performance.now())
    let nextTimer = null;
    let speechCheck = null;
    let stream = null;         // the answer being written: { text, taken, chars, lang, full, muted, timer }

    const setting = () => language.toLowerCase().split("-")[0];  // the setting's language, without region
    const join = (draft, heard) => (draft && heard ? `${draft} ${heard}` : draft || heard);

    function notice(text) {
        if (!noticeEl) return;
        clearTimeout(noticeTimer);
        noticeEl.textContent = text;
        noticeEl.classList.toggle("hidden", !text);
        if (text) noticeTimer = setTimeout(() => noticeEl.classList.add("hidden"), 7000);
    }

    function setInput(value) {
        input.value = value;
        input.dispatchEvent(new Event("input", { bubbles: true }));  // the page counts and resizes the box
    }

    // ── Mic ────────────────────────────────────────────────────

    // Why the mic cannot open at all ("" when it can): the button is dimmed and says so
    function micProblem() {
        if (!window.isSecureContext) return "Micro chỉ dùng được khi mở dashboard bằng localhost hoặc HTTPS";
        if (mode === "groq") {
            if (!hasGroqKey) return "Thiếu key Groq: nhập ở Thiết lập → Accounts → Groq";
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
                return "Trình duyệt này không ghi âm được";
            }
            return "";
        }
        // Brave has the API too, so it may try (user decision 2026-10-03): a session that gets no
        // answer from a speech service ends with a notice (browserError, HANG_MS)
        return Recognition ? "" : `Trình duyệt này không nhận dạng giọng nói được, ${USE_GROQ}`;
    }

    function refreshMic() {
        const problem = micProblem() || (micDenied ? MIC_DENIED : "");
        const listening = phase === "listening";
        micBtn.classList.toggle("is-listening", listening);
        micBtn.classList.toggle("is-busy", phase === "transcribing");
        micBtn.classList.toggle("is-off", phase === "idle" && !!problem);
        micBtn.setAttribute("aria-pressed", String(listening));
        micBtn.title = listening ? "Đang nghe: bấm hoặc nhấn Space để gửi"
            : phase === "transcribing" ? "Đang chép lời: bấm để huỷ"
            : problem || "Nói: bấm, hoặc nhấn Space 2 lần khi ô nhập trống";
    }

    function openMic() {
        if (phase !== "idle") return;
        const problem = micProblem();
        if (problem) return notice(problem);
        stopSpeaking();  // the mic would hear the bot's own voice
        notice("");
        phase = "listening";
        cat.react({ type: "mic", on: true });
        maxTimer = setTimeout(closeMic, MAX_LISTEN_MS);
        const started = mode === "groq" ? recordForGroq() : recogniseInBrowser(input.value.trim());
        if (phase === "listening") session = started;
        refreshMic();
    }

    // Done speaking: what was heard is sent
    function closeMic() {
        if (phase === "listening" && session) session.stop();
    }

    // Drop it: nothing is sent
    function cancelMic() {
        if (phase !== "idle" && session) session.cancel();
    }

    // The mic is closed (the cat stops listening); with Groq the text is still on its way
    function micClosed() {
        clearTimeout(maxTimer);
        cat.react({ type: "mic", on: false });
    }

    // The session is over: what was heard goes out like a typed message; with nothing heard, say so.
    // A Groq request that failed is a `problem`: /chat/transcribe says why (HTTP 502).
    function ended({ heard = "", text = "", problem = "", cancelled = false } = {}) {
        if (phase === "transcribing") cat.react({ type: "transcribing", on: false });
        session = null;
        phase = "idle";
        refreshMic();
        if (cancelled) return;
        if (problem) return notice(problem);
        if (!heard) return notice(SAY_AGAIN);
        setInput(text);
        // As if Send was pressed. What cannot go now (no connection, a file still uploading) stays in the box
        if (opts.send) opts.send();
    }

    function browserError(code) {
        switch (code) {
            case "no-speech":  // nothing heard: the usual notice
            case "aborted":    // closed by this page
                return "";
            case "not-allowed":
                micDenied = true;
                return MIC_DENIED;
            case "service-not-allowed":
                return `Trình duyệt không cho dùng dịch vụ nhận dạng giọng nói, hãy ${USE_GROQ}`;
            case "audio-capture":
                return "Không tìm thấy micro";
            case "network":
                // Brave has the API but no speech service behind it: every attempt ends this way
                return navigator.brave
                    ? `Brave không có dịch vụ nhận dạng giọng nói, hãy ${USE_GROQ}`
                    : `Lỗi mạng khi nhận dạng giọng nói; có thể ${USE_GROQ}`;
            case "language-not-supported":
                return `Trình duyệt không nhận dạng được ngôn ngữ ${language}`;
            default:
                return `Không nhận dạng được giọng nói (${code})`;
        }
    }

    // The browser's own recognition: the words show in the box while speaking, and the session ends
    // by itself when the speaker stops (continuous = false). `draft` is what the box already held.
    function recogniseInBrowser(draft) {
        const rec = new Recognition();
        let heard = "";
        let problem = "";
        let cancelled = false;
        let done = false;

        function finish() {
            if (done) return;
            done = true;
            clearTimeout(hang);
            micClosed();
            ended(cancelled ? { cancelled } : { heard, text: join(draft, heard), problem });
        }

        // A browser without a speech service may also just hang (Brave 1.96 on Windows): start() then
        // captures no sound and reports no error. Nothing captured after HANG_MS: give up, also if
        // abort() goes unanswered
        const hang = setTimeout(() => {
            problem = `Trình duyệt không bắt đầu nghe sau ${HANG_MS / 1000} giây, hãy ${USE_GROQ}`;
            try { rec.abort(); } catch (e) { /* not running */ }
            setTimeout(finish, 500);
        }, HANG_MS);

        rec.lang = language;
        rec.interimResults = true;
        rec.continuous = false;
        rec.onaudiostart = () => {
            clearTimeout(hang);
            micDenied = false;
        };
        rec.onresult = (event) => {
            heard = Array.from(event.results, (result) => result[0].transcript).join("").trim();
            setInput(join(draft, heard));
        };
        rec.onerror = (event) => { problem = problem || browserError(event.error); };
        rec.onend = finish;
        try {
            rec.start();
        } catch (e) {
            problem = "Không bật được nhận dạng giọng nói";
            setTimeout(finish, 0);
        }
        return {
            stop() {
                try { rec.stop(); } catch (e) { /* not running */ }
                setTimeout(finish, 3000);  // the final words normally arrive well before
            },
            cancel() {
                cancelled = true;
                try { rec.abort(); } catch (e) { /* not running */ }
                setTimeout(finish, 500);
            },
        };
    }

    function micError(err) {
        const name = err && err.name;
        if (name === "NotAllowedError" || name === "SecurityError") {
            micDenied = true;
            return MIC_DENIED;
        }
        if (name === "NotFoundError" || name === "OverconstrainedError") return "Không tìm thấy micro";
        if (name === "NotReadableError") return "Micro đang được ứng dụng khác dùng";
        return "Không mở được micro";
    }

    // Groq: record the mic, watch the loudness to know when the speaker stopped, then post the
    // recording to /chat/transcribe. The words show once Groq answers (about a second).
    function recordForGroq() {
        let stream = null;
        let recorder = null;
        let audio = null;      // AudioContext of the loudness meter
        let meter = null;
        let upload = null;     // AbortController while the recording is being transcribed
        let chunks = [];
        let speechMs = 0;      // how long somebody spoke
        let metered = false;   // the meter works: without it the recording is sent as it is
        let cancelled = false;
        let closing = false;

        function release() {
            clearInterval(meter);
            if (audio) audio.close().catch(() => {});
            if (stream) stream.getTracks().forEach((track) => track.stop());
            audio = null;
            stream = null;
        }

        function stop() {
            if (closing) return;
            closing = true;  // before the mic opened: handled when it does
            if (recorder && recorder.state !== "inactive") recorder.stop();  // -> recorded()
        }

        function watchLoudness() {
            const Context = window.AudioContext || window.webkitAudioContext;
            if (!Context) return;
            audio = new Context();
            if (audio.state === "suspended") audio.resume().catch(() => {});
            const analyser = audio.createAnalyser();
            analyser.fftSize = 2048;
            audio.createMediaStreamSource(stream).connect(analyser);
            const samples = new Float32Array(analyser.fftSize);
            const recent = [];  // loudness over the last 3 s: its quietest moment is the room's noise
            const startedAt = performance.now();
            let last = startedAt;
            let quietMs = 0;
            meter = setInterval(() => {
                if (!audio || audio.state !== "running") return;
                const now = performance.now();
                const step = Math.min(now - last, 1000);
                last = now;
                analyser.getFloatTimeDomainData(samples);
                let sum = 0;
                for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
                const level = Math.sqrt(sum / samples.length);
                metered = true;
                recent.push(level);
                if (recent.length > 60) recent.shift();
                if (level > Math.max(SPEECH_LEVEL, Math.min(...recent) * 3)) {
                    speechMs += step;
                    quietMs = 0;
                } else {
                    quietMs += step;
                }
                // Somebody spoke and has now been quiet for 2 s, or nobody spoke for 8 s
                if (speechMs >= HEARD_MS ? quietMs >= QUIET_STOP_MS : now - startedAt >= NO_SPEECH_MS) stop();
            }, 50);
        }

        async function transcribe(blob, type) {
            phase = "transcribing";
            cat.react({ type: "transcribing", on: true });
            refreshMic();
            const form = new FormData();
            form.append("audio", blob, `speech.${/ogg/.test(type) ? "ogg" : /mp4/.test(type) ? "m4a" : "webm"}`);
            upload = new AbortController();
            const timeout = setTimeout(() => upload.abort(), TRANSCRIBE_MS);
            try {
                const res = await fetch("/chat/transcribe", { method: "POST", body: form, signal: upload.signal });
                // An expired login is redirected to the /login page
                if (res.redirected) throw new Error("Phiên đăng nhập đã hết, hãy tải lại trang");
                // Templates and static files reload live but Python does not
                if (res.status === 404 || res.status === 405) {
                    throw new Error("Gateway đang chạy bản cũ: khởi động lại gateway để nhận dạng bằng Groq");
                }
                const isJson = (res.headers.get("content-type") || "").includes("application/json");
                const data = isJson ? await res.json() : null;
                if (!res.ok || !data || typeof data.text !== "string") {
                    throw new Error((data && typeof data.error === "string" && data.error) || `Lỗi máy chủ (HTTP ${res.status})`);
                }
                const heard = data.text.trim();
                ended({ heard, text: join(input.value.trim(), heard) });
            } catch (err) {
                if (cancelled) return ended({ cancelled });
                ended({ problem: err.name === "AbortError" ? "Chép lời quá lâu, bạn thử lại nhé" : err.message || "Không chép lời được" });
            } finally {
                clearTimeout(timeout);
            }
        }

        function recorded() {
            const type = recorder.mimeType || (chunks[0] && chunks[0].type) || "audio/webm";
            release();
            micClosed();
            if (cancelled) return ended({ cancelled });
            // Whisper makes up a sentence for a recording without speech (in Vietnamese often "Hãy
            // subscribe cho kênh..."), and what it returns is sent at once. So a recording with no
            // speech, or less than half a second of it, goes nowhere.
            const blob = new Blob(chunks, { type });
            if (!blob.size || (metered && speechMs < MIN_SPEECH_MS)) return ended();
            transcribe(blob, type);
        }

        navigator.mediaDevices.getUserMedia({ audio: true }).then((granted) => {
            stream = granted;
            if (closing) {  // closed while the browser was still asking for the mic
                release();
                micClosed();
                return ended({ cancelled: true });
            }
            micDenied = false;
            const type = RECORD_TYPES.find((t) => MediaRecorder.isTypeSupported(t));
            recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
            recorder.ondataavailable = (event) => { if (event.data && event.data.size) chunks.push(event.data); };
            recorder.onstop = recorded;
            recorder.start();
            watchLoudness();
        }).catch((err) => {
            release();
            micClosed();
            ended(cancelled ? { cancelled } : { problem: micError(err) });
        });

        return {
            stop,
            cancel() {
                cancelled = true;
                if (upload) upload.abort();  // -> the catch in transcribe()
                else stop();
            },
        };
    }

    function onMicClick() {
        if (phase === "listening") closeMic();
        else if (phase === "transcribing") cancelMic();
        else openMic();
        input.focus();  // typing and the Space shortcut go on from the box
    }

    // Space twice within 0.4 s in an empty box opens the mic (the first press types its space as
    // usual, the second takes it back); while the mic is open one press closes it and types nothing
    function onKeydown(event) {
        if (event.key !== " " || event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
        if (event.isComposing || event.keyCode === 229) return;
        const active = document.activeElement;
        if (active !== input && active && active !== document.body) return;  // another control has the key
        if (phase === "listening") {
            event.preventDefault();
            if (!event.repeat) closeMic();
            return;
        }
        if (phase !== "idle" || event.repeat) return;
        const now = performance.now();
        if (input.value.trim() === "" && input.value.length <= 1 && now - lastSpace <= DOUBLE_SPACE_MS) {
            event.preventDefault();
            lastSpace = 0;
            if (input.value) setInput("");
            openMic();
            return;
        }
        lastSpace = input.value === "" ? now : 0;  // counts only in an empty box, so typing never opens the mic
    }

    // ── Read aloud ─────────────────────────────────────────────

    function readProblem() {
        if (!synth || !window.SpeechSynthesisUtterance) return "Trình duyệt này không đọc to được";
        // Until the list has settled, a missing voice says nothing: Chrome lists its own voices
        // first and the system's (the Vietnamese one among them) a moment later
        if (voiceFor(setting()) || !settled) return "";
        return synth.getVoices().length
            ? `Trình duyệt này không có giọng đọc ${setting() === "vi" ? "tiếng Việt" : language}`
            : "Trình duyệt này không có giọng đọc nào";
    }

    // The installed voice for a language ("vi", "en"...), or null. Natural-sounding voices first
    // (Edge: "Microsoft HoaiMy Online (Natural)"), then the browser's default voice, then the region
    // asked for, then a voice on this machine, then any (Windows: "Microsoft An").
    function voiceFor(code) {
        const all = synth ? synth.getVoices() : [];
        if (all.length !== listed) {  // the list changed: choose again
            listed = all.length;
            voices = {};
        }
        if (code in voices) return voices[code];
        if (!all.length) return null;  // not loaded yet
        const lang = (v) => (v.lang || "").replace("_", "-").toLowerCase();
        // The region: the setting's for its own language, else the browser's (en-GB...), else US English
        const region = code === setting() ? language.toLowerCase()
            : (navigator.languages || []).map((l) => l.toLowerCase()).find((l) => l.startsWith(`${code}-`))
            || (code === "en" ? "en-us" : "");
        const score = (v) => (/natural|online/i.test(v.name) ? 16 : 0) + (v.default ? 8 : 0)
            + (lang(v) === region ? 4 : 0) + (v.localService ? 2 : 0) - (POOR_VOICE.test(v.name) ? 32 : 0);
        const best = all.filter((v) => lang(v).split("-")[0] === code).sort((a, b) => score(b) - score(a))[0];
        return (voices[code] = best || null);
    }

    function voicesChanged() {
        listed = -1;  // choose again
        refreshRead();
    }

    function refreshRead() {
        const problem = readProblem();
        const on = wantRead && !problem;
        readBtn.classList.toggle("is-on", on);
        readBtn.classList.toggle("is-off", !!problem);
        readBtn.setAttribute("aria-pressed", String(on));
        if (readBtn.firstElementChild) readBtn.firstElementChild.textContent = on ? "volume_up" : "volume_off";
        readBtn.title = problem || (on ? "Đang đọc to câu trả lời: bấm để tắt" : "Đọc to câu trả lời: bấm để bật");
    }

    function onReadClick() {
        const problem = readProblem();
        if (problem) return notice(problem);
        wantRead = !wantRead;
        try { localStorage.setItem(READ_KEY, wantRead ? "1" : "0"); } catch (e) { /* not remembered */ }
        if (!wantRead) stopSpeaking();
        refreshRead();
        input.focus();
    }

    // Never while the mic is open or its words are on their way: the mic would hear the bot
    const canRead = () => wantRead && !readProblem() && phase === "idle";

    function setSpeaking(on) {
        if (speaking === on) return;
        speaking = on;
        document.documentElement.classList.toggle("voice-speaking", on);  // chat.html shows Stop meanwhile
        cat.react({ type: "speaking", on });
    }

    // Reading goes to the browser one piece at a time, and the browser is only told to stop while a
    // piece is being spoken. Chrome's speech queue otherwise hangs until the page is reloaded: it
    // does when cancel() comes after speak() but before the voice has started (measured on Chrome
    // 154, macOS). A reading stopped in that moment is cut as soon as it starts, and the next piece
    // waits SETTLE_MS after a cut.
    function cutReading() {
        cutAt = performance.now();
        synth.cancel();
    }

    function stopSpeaking() {
        queue = [];
        if (stream) stream.muted = true;  // nor is the rest of the answer being written
        setSpeaking(false);
        if (!current) return;
        if (current.started) cutReading();
        else current.stopped = true;  // readNext cuts it when it starts
    }

    // The piece the browser has is over (read, cut off, or its events were lost): on to the next
    function pieceDone(piece) {
        if (current !== piece) return;
        current = null;
        clearInterval(speechCheck);
        if (!queue.length) setSpeaking(false);
        readNext();
    }

    function readNext() {
        clearTimeout(nextTimer);
        if (current || !queue.length) return;
        const wait = cutAt + SETTLE_MS - performance.now();
        if (wait > 0) {
            nextTimer = setTimeout(readNext, wait);
            return;
        }
        // The voice of the sentence's own language; without one, the setting's reads it
        const voice = voiceFor(queue[0].lang) || voiceFor(setting());
        if (!voice) {
            if (settled) queue = [];  // no voice after all: the button now says so
            else nextTimer = setTimeout(readNext, 300);  // the list is still loading
            return;
        }
        const utterance = new SpeechSynthesisUtterance(queue.shift().text);
        utterance.voice = voice;
        utterance.lang = voice.lang;
        const piece = current = { utterance, started: false, stopped: false };
        utterance.onstart = () => {
            piece.started = true;
            if (piece.stopped) return cutReading();
            if (current === piece) setSpeaking(true);
        };
        utterance.onend = utterance.onerror = () => pieceDone(piece);
        synth.speak(utterance);
        // Browsers sometimes drop the "end" of a reading: if the browser has nothing left to say
        // for a while, take the piece as read
        let idle = 0;
        clearInterval(speechCheck);
        speechCheck = setInterval(() => {
            idle = synth.speaking || synth.pending ? 0 : idle + 1;
            if (idle >= 3) pieceDone(piece);
        }, 500);
    }

    // The language a sentence is read in, or "" when that cannot be told (it then follows the
    // sentence next to it). With Vietnamese as the setting, a sentence of two words or more without
    // a Vietnamese letter is English if one of its words could not be Vietnamese; a single word
    // ("Docker.") stays with its neighbours. With another setting, only what is clearly Vietnamese
    // leaves the setting's language.
    function languageOf(sentence) {
        if (VI_ONLY.test(sentence)) return "vi";
        if (setting() !== "vi") return setting();
        if (VI_MARKED.test(sentence)) return "vi";
        const words = sentence.toLowerCase().match(/\p{L}+/gu) || [];
        return words.length >= 2 && words.some((word) => NOT_VIETNAMESE.test(word)) ? "en" : "";
    }

    // An answer as the sentences to read: what the page shows, without code blocks, tables,
    // formulas, images and emoji. A link is read by its words; a bare address becomes LINK.
    function sentencesOf(markdown) {
        const box = document.createElement("div");
        const source = String(markdown || "").replace(/<think>[\s\S]*?(<\/think>|$)/g, "");
        if (opts.render) box.innerHTML = opts.render(source);  // sanitised by the page, as in its bubbles
        else box.textContent = source;
        box.querySelectorAll("pre, table, .katex, img, svg, hr, .material-symbols-outlined").forEach((el) => el.remove());
        box.querySelectorAll("a").forEach((a) => {
            if (/^(https?:\/\/|www\.)/i.test(a.textContent.trim())) a.textContent = LINK;
        });
        box.querySelectorAll("br").forEach((br) => br.replaceWith("\n"));
        box.querySelectorAll("p, li, ul, ol, h1, h2, h3, h4, h5, h6, blockquote, div").forEach((el) => {
            el.before("\n");
            el.after("\n");
        });
        // Each line (paragraph, heading, list item) ends a sentence, whatever it ends with
        return box.textContent.split("\n")
            .map((line) => line.replace(/[\p{Extended_Pictographic}\u{FE0F}\u{200D}]/gu, "").replace(/\s+/g, " ").trim())
            .filter((line) => /[\p{L}\p{N}\u{E000}]/u.test(line))
            .map((line) => (/[.!?…:;,]$/.test(line) ? line : `${line}.`))  // a pause between lines
            .flatMap((line) => line.match(/\S[\s\S]*?(?:[.!?…]+(?=\s|$)|$)/g) || []);
    }

    // What is read of `sentences`: [{ text, lang }], each piece a few sentences of one language
    // (up to about 200 characters: Chrome cuts a single long reading off after about 15 seconds).
    // `answer` carries what was read of the same answer before: its characters (an answer is read
    // up to about READ_MAX_CHARS, the rest is left to the screen) and its last sentence's language.
    // `ahead` are the sentences that follow but are not read yet (the one still being written).
    function pieces(sentences, answer, ahead = []) {
        const out = [];
        const add = (text, lang) => {
            const last = out[out.length - 1];
            if (last && last.lang === lang && last.text.length + text.length < 200) last.text += ` ${text}`;
            else out.push({ text, lang });
        };
        for (let i = 0; i < sentences.length && !answer.full; i++) {
            let text = sentences[i];
            // Cannot be told: like the sentence before it, else the next one that can. With neither
            // (a one-word opening such as "Great!"), a sentence whose every word could not be
            // Vietnamese is English; anything else is the setting's language
            const words = text.toLowerCase().match(/\p{L}+/gu) || [];
            const lang = languageOf(text) || answer.lang
                || [...sentences.slice(i + 1), ...ahead].map(languageOf).find(Boolean)
                || (setting() === "vi" && words.length && words.every((word) => NOT_VIETNAMESE.test(word)) ? "en" : setting());
            const room = READ_MAX_CHARS - answer.chars;
            if (text.length > room) {
                answer.full = true;
                // Stop before this sentence, unless that leaves under half the limit: then it is cut at a word
                text = answer.chars < READ_MAX_CHARS / 2
                    ? `${text.slice(0, text.lastIndexOf(" ", room)).replace(/[,;:]$/, "")}.` : "";
            }
            if (text) {
                add(text.replaceAll(LINK, lang === "vi" ? "đường link" : "link"), lang);
                answer.chars += text.length + 1;
                answer.lang = lang;
            }
            if (answer.full) {
                const vietnamese = answer.lang === "vi";
                add(vietnamese ? "Phần còn lại xem trên màn hình." : "The rest is on the screen.", vietnamese ? "vi" : "en");
            }
        }
        return out;
    }

    const newAnswer = () => ({ text: "", taken: 0, chars: 0, lang: "", full: false, muted: false, timer: null });
    const readable = (markdown) => pieces(sentencesOf(markdown), newAnswer());
    const speakable = (markdown) => readable(markdown).map((piece) => piece.text).join(" ");

    function speak(markdown) {
        if (!canRead()) return;
        queue.push(...readable(markdown));
        readNext();
    }

    // An answer is read while it is written: every STREAM_EVERY_MS its text so far is turned into
    // sentences and the new ones are queued. All but the last, which may still grow, or turn into a
    // table or a code block once its next line arrives; `done` (the segment is complete) takes it too.
    function readStream(done) {
        const answer = stream;
        if (!answer || answer.full || answer.muted || !canRead()) return;
        const sentences = sentencesOf(answer.text);
        const upto = done ? sentences.length : sentences.length - 1;
        if (upto <= answer.taken) return;
        queue.push(...pieces(sentences.slice(answer.taken, upto), answer, sentences.slice(upto)));
        answer.taken = upto;
        readNext();
    }

    function streamed(delta) {
        const answer = stream = stream || newAnswer();
        answer.text += delta || "";
        if (answer.timer || answer.full || answer.muted || !canRead()) return;
        answer.timer = setTimeout(() => {
            answer.timer = null;
            if (stream === answer) readStream(false);
        }, STREAM_EVERY_MS);
    }

    // What the bot writes is read: an answer sentence by sentence while it streams in (also a
    // lead-in before its tools run), or a whole message at once. Not the history, and not the
    // files it sends ("push").
    function react(event) {
        const type = event && event.type;
        if (type === "stream_delta") {
            streamed(event.content);
        } else if (type === "stream_end") {
            if (event.content) {
                streamed("");
                stream.text = event.content;  // the whole segment, as the page now renders it
                readStream(true);
            }
            if (stream) clearTimeout(stream.timer);
            stream = null;
            // The cat was just set to "done" (or "thinking", before a tool): it is still talking
            if (speaking) cat.react({ type: "speaking", on: true });
        } else if (type === "message" && event.content) {
            speak(event.content);
        } else if (type === "typing" || type === "error") {
            if (stream) clearTimeout(stream.timer);
            stream = null;  // a new turn: the one before may have been stopped in the middle of an answer
        }
    }

    // Esc, or the page is going away: close the mic without sending, stop reading
    function stop() {
        cancelMic();
        stopSpeaking();
    }

    function init(options) {
        opts = options || {};
        micBtn = opts.micBtn;
        readBtn = opts.readBtn;
        input = opts.input;
        noticeEl = opts.noticeEl;
        if (!micBtn || !readBtn || !input) return;
        cat = opts.cat || cat;
        mode = micBtn.dataset.recognition === "groq" ? "groq" : "browser";
        language = micBtn.dataset.language || language;
        hasGroqKey = micBtn.dataset.groqKey === "1";
        try { wantRead = localStorage.getItem(READ_KEY) === "1"; } catch (e) { /* storage blocked: off */ }

        micBtn.addEventListener("click", onMicClick);
        readBtn.addEventListener("click", onReadClick);
        document.addEventListener("keydown", onKeydown);
        window.addEventListener("pagehide", stop);
        if (synth) {
            cutReading();  // Chrome can keep reading the previous page's answer after a reload
            // Voices load after the page in Chrome; Safari has no addEventListener here
            if (synth.addEventListener) synth.addEventListener("voiceschanged", voicesChanged);
            else synth.onvoiceschanged = voicesChanged;
            synth.getVoices();  // the first call starts loading the list
            setTimeout(() => {
                settled = true;
                refreshRead();
            }, 3000);
        }
        refreshMic();
        refreshRead();
    }

    window.chatVoice = { init, react, stop, stopSpeaking, speakable, readable, state: () => phase };
})();
