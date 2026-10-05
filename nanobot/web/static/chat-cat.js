/* Chat focus cat (styles in static/chat-cat.css): draws an SVG cat into #chat-cat and switches
   its state from the Web Chat socket events. chat.html calls chatCat.init() once and
   chatCat.react(event) for each socket event, plus "sent", "open", "closed" and "input".
   static/chat-voice.js adds "mic", "transcribing" and "speaking" (each with on: true or false).
   While idle, the cat now and then plays a short random action (look around, stretch, groom...);
   a click on it plays one right away. */
(function () {
    "use strict";

    // Line art in the spirit of the logo (angular head, circuit lines). The tail is on the left so
    // the right side is free for the "..." bubble, tool icons, "!" and zZ.
    const SVG = `
<svg viewBox="0 0 120 120" aria-hidden="true" focusable="false">
  <g class="cat">
    <path class="ln tail" d="M40 101 Q21 103 20 88 Q19 75 28 70"/>
    <path class="fl body" d="M47 66 Q35 84 40 104 L80 104 Q85 84 73 66 Z"/>
    <path class="ln thin" d="M55 82 Q60 86 65 82"/>
    <g class="paws">
      <path class="fl pl" d="M47 104 Q47 98 52.5 98 Q58 98 58 104"/>
      <path class="fl pr" d="M62 104 Q62 98 67.5 98 Q73 98 73 104"/>
    </g>
    <g class="head">
      <path class="fl" d="M40 46 L38 14 L54 30 L66 30 L82 14 L80 46 L78 58 L68 68 L60 72 L52 68 L42 58 Z"/>
      <path class="acc-ln" d="M42.5 37 L41.5 21 L50.5 29.5 M77.5 37 L78.5 21 L69.5 29.5"/>
      <path class="ln thin" d="M60 30.5 V38 M54.5 30.5 V34.5 L51 38 M65.5 30.5 V34.5 L69 38"/>
      <circle class="ink" cx="60" cy="40" r="1.6"/>
      <circle class="ink" cx="50.5" cy="39" r="1.3"/>
      <circle class="ink" cx="69.5" cy="39" r="1.3"/>
      <g class="eyes">
        <path class="ink" d="M46 50 Q51 45 56 50 Q51 54 46 50 Z"/>
        <path class="ink" d="M64 50 Q69 45 74 50 Q69 54 64 50 Z"/>
      </g>
      <path class="ln opt eyes-happy" d="M46 51 Q51 46 56 51 M64 51 Q69 46 74 51"/>
      <path class="ln opt eyes-sleep" d="M46 50 Q51 53.5 56 50 M64 50 Q69 53.5 74 50"/>
      <path class="ln opt eyes-x" d="M47.5 47 L54.5 53 M54.5 47 L47.5 53 M65.5 47 L72.5 53 M72.5 47 L65.5 53"/>
      <path class="acc" d="M57 58 L63 58 L60 61 Z"/>
      <path class="ln thin mouth" d="M60 61 V63 Q57 66 54 64 M60 63 Q63 66 66 64"/>
      <path class="ln thin opt mouth-sad" d="M55 65 Q60 61.5 65 65"/>
      <path class="ln thin" d="M47 60 L29 57 M47 63 L29 64 M73 60 L91 57 M73 63 L91 64"/>
      <ellipse class="fl opt yawn" cx="60" cy="64" rx="3" ry="3.6"/>
      <ellipse class="acc opt tongue" cx="60" cy="64.6" rx="1.9" ry="1.5"/>
    </g>
    <ellipse class="fl opt paw-up" cx="52.5" cy="100" rx="5.5" ry="3.6"/>
    <ellipse class="fl opt paw-up-r" cx="67.5" cy="100" rx="5.5" ry="3.6"/>
    <g class="opt blush">
      <ellipse class="acc" cx="46" cy="58.5" rx="3.6" ry="1.8"/>
      <ellipse class="acc" cx="74" cy="58.5" rx="3.6" ry="1.8"/>
    </g>
    <path class="acc-ln opt scratch" d="M33 22 L29 19 M31 28 L26 27.5 M33 34 L29 37"/>
    <g class="opt phone">
      <rect class="fl" x="51" y="73" width="18" height="26" rx="3"/>
      <path class="ln thin" d="M55 79 H65 M55 83.5 H63 M55 88 H65"/>
      <ellipse class="fl ph-l" cx="51.5" cy="93" rx="4.5" ry="3.4"/>
      <ellipse class="fl ph-r" cx="68.5" cy="93" rx="4.5" ry="3.4"/>
    </g>
    <g class="opt laptop">
      <path class="fl" d="M26 110 L94 110 L90 103 L30 103 Z"/>
      <path class="ln thin" d="M36 106.5 H42 M46 106.5 H52 M56 106.5 H64 M68 106.5 H74 M78 106.5 H84"/>
      <ellipse class="fl paw-l" cx="51" cy="101" rx="5.5" ry="3.2"/>
      <ellipse class="fl paw-r" cx="69" cy="101" rx="5.5" ry="3.2"/>
    </g>
  </g>
  <g class="opt bubble">
    <circle class="fl thin" cx="88" cy="28" r="1.8"/>
    <circle class="fl thin" cx="93" cy="22" r="2.4"/>
    <ellipse class="fl" cx="104" cy="11" rx="14" ry="8"/>
    <circle class="ink d1" cx="98" cy="11" r="1.6"/>
    <circle class="ink d2" cx="104" cy="11" r="1.6"/>
    <circle class="ink d3" cx="110" cy="11" r="1.6"/>
  </g>
  <g class="opt tool-search">
    <circle class="fl" cx="97" cy="86" r="7"/>
    <path class="ln" d="M102 91 L108 97"/>
  </g>
  <g class="opt tool-file">
    <path class="fl" d="M90 76 H101 L106 81 V98 H90 Z"/>
    <path class="ln thin" d="M93 85 H103 M93 89 H103 M93 93 H100"/>
  </g>
  <g class="opt tool-exec">
    <rect class="fl" x="86" y="78" width="22" height="16" rx="2.5"/>
    <path class="ln thin" d="M90 83 L93 86 L90 89"/>
    <path class="ln cursor" d="M95.5 89.5 H101"/>
  </g>
  <g class="opt tool-other">
    <g class="gear">
      <circle class="fl" cx="97" cy="87" r="5.5"/>
      <path class="ln" d="M97 78.5 V81 M97 93 V95.5 M88.5 87 H91 M103 87 H105.5 M91 81 L92.8 82.8 M101.2 91.2 L103 93 M103 81 L101.2 82.8 M92.8 91.2 L91 93"/>
    </g>
  </g>
  <g class="opt sparkles">
    <path class="acc s1" d="M22 18 L23.6 23.4 L29 25 L23.6 26.6 L22 32 L20.4 26.6 L15 25 L20.4 23.4 Z"/>
    <path class="acc s2" d="M100 20 L101.2 24 L105 25.2 L101.2 26.4 L100 30.4 L98.8 26.4 L95 25.2 L98.8 24 Z"/>
  </g>
  <g class="opt alert">
    <circle class="fl" cx="101" cy="18" r="9"/>
    <path class="ln" d="M101 13 V19"/>
    <circle class="ink" cx="101" cy="23" r="1.4"/>
  </g>
  <g class="opt zz">
    <text class="ink z1" x="86" y="30" font-size="9">z</text>
    <text class="ink z2" x="95" y="20" font-size="13">Z</text>
  </g>
  <g class="opt yarn">
    <circle class="fl" cx="100" cy="102" r="6"/>
    <path class="acc-ln" d="M95 99 Q100 103 105 99 M95 104 Q100 100 105 105 M98 96.5 Q101 102 98 107.5"/>
  </g>
  <g class="opt butterfly">
    <g class="wings">
      <path class="acc" d="M0 0 Q-6 -7 -7 -1.5 Q-6 3 0 0 Z M0 0 Q6 -7 7 -1.5 Q6 3 0 0 Z"/>
      <path class="ln thin" d="M0 -2.5 V2.5"/>
    </g>
  </g>
  <text class="acc opt question" x="94" y="26" font-size="18" font-weight="700">?</text>
  <g class="opt bulb">
    <path class="ln thin rays" d="M100 0.5 V3 M88.5 12 H91 M109 12 H111.5 M91.5 3.5 L93.2 5.2 M108.5 3.5 L106.8 5.2"/>
    <circle class="fl glass" cx="100" cy="12" r="7"/>
    <path class="fl" d="M96.5 18.5 H103.5 V23.5 H96.5 Z"/>
    <path class="ln thin" d="M96.5 21 H103.5"/>
  </g>
  <g class="opt gears">
    <g class="g1">
      <circle class="fl" cx="99" cy="14" r="4.6"/>
      <path class="ln" d="M99 6.5 V8.5 M99 19.5 V21.5 M91.5 14 H93.5 M104.5 14 H106.5 M93.7 8.7 L95.1 10.1 M102.9 17.9 L104.3 19.3 M104.3 8.7 L102.9 10.1 M95.1 17.9 L93.7 19.3"/>
    </g>
    <g class="g2">
      <circle class="fl" cx="110.5" cy="24" r="3"/>
      <path class="ln thin" d="M110.5 18.8 V20.3 M110.5 27.7 V29.2 M105.3 24 H106.8 M114.2 24 H115.7"/>
    </g>
  </g>
  <g class="opt paper">
    <path class="fl" d="M82 92 H106 V111 H82 Z"/>
    <path class="ln thin ink-lines" d="M85 97 H103 M85 101.5 H103 M85 106 H97"/>
    <g class="pencil">
      <path class="fl" d="M95 80 L104 89 L101.5 91.5 L92.5 82.5 Z"/>
      <path class="acc" d="M104 89 L105.5 93 L101.5 91.5 Z"/>
    </g>
  </g>
  <g class="opt compose">
    <path class="fl" d="M85 3 H116 Q118 3 118 5 V21 Q118 23 116 23 H95 L89 28 L90 23 H87 Q85 23 85 21 V5 Q85 3 87 3 Z"/>
    <path class="ln thin c1" d="M89 8.5 H113"/>
    <path class="ln thin c2" d="M89 13 H110"/>
    <path class="ln thin c3" d="M89 17.5 H104"/>
  </g>
  <g class="opt letters">
    <text class="ink l1" x="80" y="98" font-size="10">A</text>
    <text class="ink l2" x="90" y="92" font-size="9">b</text>
    <text class="ink l3" x="99" y="99" font-size="10">c</text>
  </g>
  <g class="opt check">
    <circle class="fl" cx="101" cy="16" r="8.5"/>
    <path class="acc-ln bold" d="M96.8 16 L99.8 19.2 L105.5 12.6"/>
  </g>
  <path class="acc opt heart" d="M100 23 C90 16 91.5 7 96.5 7 C98.5 7 100 8.6 100 10.3 C100 8.6 101.5 7 103.5 7 C108.5 7 110 16 100 23 Z"/>
  <g class="opt confetti">
    <rect class="acc f1" x="20" y="0" width="3" height="5"/>
    <rect class="ink f2" x="34" y="0" width="2.5" height="4.5"/>
    <rect class="acc f3" x="50" y="0" width="3" height="5"/>
    <rect class="ink f4" x="68" y="0" width="2.5" height="4.5"/>
    <rect class="acc f5" x="84" y="0" width="3" height="5"/>
    <rect class="ink f6" x="98" y="0" width="2.5" height="4.5"/>
    <rect class="acc f7" x="108" y="0" width="3" height="5"/>
  </g>
  <g class="opt notes">
    <text class="acc n1" x="88" y="30" font-size="13">♪</text>
    <text class="acc n2" x="18" y="34" font-size="12">♫</text>
  </g>
  <g class="opt sun">
    <path class="acc-ln sun-rays" d="M109.5 15 H112.5 M107 21 L109.1 23.1 M101 23.5 V26.5 M95 21 L92.9 23.1 M92.5 15 H89.5 M95 9 L92.9 6.9 M101 6.5 V3.5 M107 9 L109.1 6.9"/>
    <circle class="acc" cx="101" cy="15" r="5.5"/>
  </g>
  <g class="opt fishbowl">
    <path class="fl" d="M90 89 Q85 96 88.5 103 Q92.5 110.5 100 110.5 Q107.5 110.5 111.5 103 Q115 96 110 89 Z"/>
    <path class="ln thin" d="M88 95.5 Q100 94 112 95.5"/>
    <path class="acc fish" d="M0 0 Q4 -3.2 8 0 Q4 3.2 0 0 Z M8 0 L11.5 -2.8 L11.5 2.8 Z"/>
  </g>
  <g class="opt tea">
    <path class="ln thin steam st1" d="M93 92 Q91 88.5 93 85.5 Q95 82.5 93 79"/>
    <path class="ln thin steam st2" d="M99.5 92 Q97.5 88.5 99.5 85.5 Q101.5 82.5 99.5 79"/>
    <path class="fl" d="M88 95 H105 V102 Q105 108.5 98.5 108.5 H94.5 Q88 108.5 88 102 Z"/>
    <path class="ln thin" d="M105 97.5 Q110.5 97.5 110.5 101 Q110.5 104.5 105 104.5"/>
    <path class="ln" d="M84 111 H109"/>
  </g>
  <g class="opt waves">
    <path class="acc-ln w1" d="M87 21 Q90.5 26 87 31"/>
    <path class="acc-ln w2" d="M91.5 17.5 Q97 26 91.5 34.5"/>
    <path class="acc-ln w3" d="M96 14 Q103.5 26 96 38"/>
  </g>
  <path class="acc-ln opt perk" d="M60 24 V17 M53.5 25 L50 19.5 M66.5 25 L70 19.5"/>
</svg>`;

    const LABELS = {
        error: "Có lỗi rồi…",
        sleeping: "Zzz…",
        offline: "Mất kết nối",
        speaking: "Đang nói…",
    };
    const TRANSCRIBING = "Đang chép lời…";  // over the thinking look, while a recording becomes text
    const TOOL_LABELS = { search: "Đang tìm kiếm…", file: "Đang xem tệp…", exec: "Đang chạy lệnh…", other: "Đang làm việc…" };
    // Idle, listening, thinking, typing and happy have 5 looks each (data-variant 1-5 in
    // chat-cat.css) with these labels; set() picks one at random, never the same look twice in a row.
    const VARIANTS = {
        idle: ["Sẵn sàng", "Lim dim…", "Sưởi nắng…", "Ngắm cá…", "Nhâm nhi trà…"],
        listening: ["Đang nghe…", "Lắng nghe…", "Hóng chuyện…", "Ừ ừ…", "Chăm chú…"],
        thinking: ["Đang suy nghĩ…", "Để xem nào…", "Nảy ra ý tưởng…", "Đang tính toán…", "Gãi đầu suy nghĩ…"],
        typing: ["Đang viết…", "Đang ghi chép…", "Đang soạn tin…", "Gõ gõ gõ…", "Đang nhắn tin…"],
        happy: ["Xong rồi!", "Ngon lành!", "Hoan hô!", "Thương bạn!", "Tuyệt vời!"],
    };
    const lastVariant = {};
    const SETTLE_MS = { listening: 2500, happy: 2500, error: 4000 };  // then back to idle
    const SLEEP_AFTER_MS = 5 * 60 * 1000;
    // While idle, now and then one short action (durations match the animations in chat-cat.css)
    const IDLE_ACTIONS = {
        look: [2400, "Ngó nghiêng…"],
        tilt: [1600, "Hửm?"],
        slowblink: [2200, "Nháy mắt…"],
        stretch: [2400, "Vươn vai…"],
        groom: [2800, "Chải lông…"],
        wave: [2000, "Chào bạn!"],
        tail: [1800, "Vẫy đuôi…"],
        yarn: [3000, "Nghịch cuộn len…"],
        butterfly: [3200, "Đuổi bướm…"],
    };
    const IDLE_GAP_MS = [8000, 22000];

    let box = null;
    let label = null;
    let current = "idle";
    let settleTimer = null;
    let sleepTimer = null;
    let idleTimer = null;
    let lastAction = null;
    let pokeTimer = null;
    let offline = false;  // the socket is closed (the cat shows "Mất kết nối" between tricks)
    let micOpen = false;  // the mic is open: the cat listens until it closes
    let waiting = null;   // what the bot started doing while the mic was open, shown once it closes

    function scheduleIdleAction() {
        clearTimeout(idleTimer);
        const [min, max] = IDLE_GAP_MS;
        idleTimer = setTimeout(() => play(), min + Math.random() * (max - min));
    }

    // One idle action: `name`, or a random one other than the last. Only while idle.
    function play(name) {
        if (!box || current !== "idle") return;
        if (!name && document.hidden) return scheduleIdleAction();  // nobody is watching: later
        if (!IDLE_ACTIONS[name]) {
            const names = Object.keys(IDLE_ACTIONS).filter((n) => n !== lastAction);
            name = names[Math.floor(Math.random() * names.length)];
        }
        const [ms, text] = IDLE_ACTIONS[name];
        lastAction = name;
        box.dataset.idle = name;
        label.textContent = text;
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
            delete box.dataset.idle;
            if (offline) return set("offline");
            label.textContent = VARIANTS.idle[Number(box.dataset.variant) - 1] || VARIANTS.idle[0];
            scheduleIdleAction();
        }, ms);
    }

    // A click on the cat: a trick right away (waking it up if it sleeps or is offline); while it
    // works, only a quick head tilt, so the state it shows stays visible.
    function poke() {
        if (!box) return;
        if (current === "sleeping" || current === "offline") set("idle");
        if (current === "idle") return play();
        box.classList.remove("poked");
        void box.getBoundingClientRect();  // restart the tilt when clicked again
        box.classList.add("poked");
        clearTimeout(pokeTimer);
        pokeTimer = setTimeout(() => box.classList.remove("poked"), 900);
    }

    function pickVariant(state) {
        const count = VARIANTS[state].length;
        let v = 1 + Math.floor(Math.random() * count);
        if (v === lastVariant[state]) v = (v % count) + 1;
        lastVariant[state] = v;
        return v;
    }

    // `variant` (1-5) forces a look for a state in VARIANTS; otherwise one is picked at random.
    function set(state, tool, variant) {
        if (!box) return;
        if (micOpen && state !== "listening") {
            // Kept for when the mic closes, unless it is a state that passes by itself (happy, error)
            waiting = SETTLE_MS[state] ? null : [state, tool, variant];
            return;
        }
        clearTimeout(settleTimer);
        clearTimeout(sleepTimer);
        clearTimeout(idleTimer);
        delete box.dataset.idle;  // any change of state ends an idle action
        current = state;
        box.dataset.state = state === "offline" ? "sleeping" : state;
        if (tool) box.dataset.tool = tool;
        else delete box.dataset.tool;
        if (VARIANTS[state]) {
            const v = VARIANTS[state][variant - 1] ? variant : pickVariant(state);
            box.dataset.variant = String(v);
            label.textContent = VARIANTS[state][v - 1];
        } else {
            delete box.dataset.variant;
            label.textContent = tool ? TOOL_LABELS[tool] : LABELS[state];
        }
        if (SETTLE_MS[state] && !micOpen) settleTimer = setTimeout(() => set("idle"), SETTLE_MS[state]);
        if (state === "idle") {
            sleepTimer = setTimeout(() => set("sleeping"), SLEEP_AFTER_MS);
            scheduleIdleAction();
        }
    }

    // Tool hints arrive as labels starting with an emoji (_TOOL_LABELS in routes/chat.py); any other
    // progress text is the model thinking aloud.
    function toolKind(text) {
        if (/^(🔍|🌐)/u.test(text)) return "search";
        if (/^(📄|📝|📂)/u.test(text)) return "file";
        if (/^⚙/u.test(text)) return "exec";
        return /^\p{Extended_Pictographic}/u.test(text) ? "other" : null;
    }

    function react(event) {
        switch (event && event.type) {
            case "typing":
                return set("thinking");
            case "progress": {
                const kind = toolKind(String(event.content || ""));
                return kind ? set("tool", kind) : set("thinking");
            }
            case "stream_delta":
                if (current !== "typing") set("typing");
                return;
            case "stream_end":
                return set(event.resuming ? "thinking" : "happy");
            case "message":
            case "push":
                return set("happy");
            case "error":
                return set("error");
            case "sent":
                return set("listening");
            case "open":
                offline = false;
                return set("idle");
            case "closed":
                if (offline) return;  // still down: each reconnect attempt closes again
                offline = true;
                return set("offline");
            case "input":  // the user is typing: wake up, restart the nap timer
                if (current === "sleeping") set("idle");
                else if (current === "idle") set("idle", null, Number(box.dataset.variant));  // same look
                return;
            case "mic": {  // the mic opened or closed: listening for as long as it is open
                micOpen = !!event.on;
                if (micOpen) {
                    // What the bot is doing right now comes back too, unless it does something newer meanwhile
                    waiting = box && ["thinking", "typing", "tool"].includes(current)
                        ? [current, box.dataset.tool, Number(box.dataset.variant)] : null;
                    return set("listening");
                }
                const next = waiting;
                waiting = null;
                return next ? set(...next) : set(offline ? "offline" : "idle");
            }
            case "transcribing":  // the recording is being turned into text: the thinking look, own label
                if (event.on) {
                    set("thinking");
                    if (current === "thinking") label.textContent = TRANSCRIBING;
                } else if (label && label.textContent === TRANSCRIBING) {
                    set(offline ? "offline" : "idle");
                }
                return;
            case "speaking":  // the answer is read aloud: the mouth moves until the reading ends
                if (event.on) return set("speaking");
                if (current === "speaking") set(offline ? "offline" : "idle");
                return;
        }
    }

    function init(element) {
        if (!element) return;
        box = element;
        box.innerHTML = SVG + '<div id="chat-cat-label"></div>';
        label = box.querySelector("#chat-cat-label");
        box.title = "Bấm để mèo làm trò";
        box.addEventListener("click", poke);
        set("idle");
    }

    window.chatCat = { init, react, set, play, poke, state: () => current };
})();
