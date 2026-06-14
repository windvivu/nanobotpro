(function () {
    const workspaceInput = document.getElementById("workspace-input");
    if (!workspaceInput) {
        return;
    }

    const buttons = document.querySelectorAll("[data-workspace-prefix]");
    for (const button of buttons) {
        button.addEventListener("click", () => {
            const prefix = button.getAttribute("data-workspace-prefix") || "";
            const current = workspaceInput.value.trim();
            if (!current) {
                workspaceInput.value = prefix;
                workspaceInput.focus();
                return;
            }

            const looksRooted =
                /^[a-zA-Z]:[\\/]/.test(current) ||
                current.startsWith("~/") ||
                current.startsWith("~\\") ||
                current.startsWith("$HOME/") ||
                current.startsWith("$HOME\\") ||
                current.startsWith(".\\") ||
                current.startsWith("./");

            if (!looksRooted) {
                workspaceInput.value = prefix + current.replace(/^[\\/]+/, "");
            } else {
                workspaceInput.value = current;
            }
            workspaceInput.focus();
        });
    }
})();

(function () {
    const form = document.getElementById("create-bot-form");
    if (!form) {
        return;
    }

    const submitBtn = form.querySelector("[data-submit-btn]");
    const submitLabel = submitBtn ? submitBtn.querySelector("[data-label]") : null;
    const errorBox = document.getElementById("create-bot-error");

    function setSubmitting(active) {
        if (!submitBtn) return;
        if (active) {
            submitBtn.setAttribute("aria-busy", "true");
            if (submitLabel) submitLabel.textContent = "Creating…";
            const spinner = document.createElement("span");
            spinner.className = "spinner";
            spinner.setAttribute("data-spinner", "");
            submitBtn.prepend(spinner);
        } else {
            submitBtn.removeAttribute("aria-busy");
            if (submitLabel) submitLabel.textContent = "Create Bot";
            const spinner = submitBtn.querySelector("[data-spinner]");
            if (spinner) spinner.remove();
        }
    }

    function showError(msg) {
        if (!errorBox) return;
        errorBox.textContent = msg;
        errorBox.hidden = false;
    }

    function clearError() {
        if (!errorBox) return;
        errorBox.textContent = "";
        errorBox.hidden = true;
    }

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        clearError();
        setSubmitting(true);

        try {
            const data = new FormData(form);
            const response = await fetch(form.action, {
                method: "POST",
                body: data,
                redirect: "manual",
            });

            if (response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400)) {
                const location = response.headers.get("location") || "/";
                window.location.href = location;
                return;
            }

            if (!response.ok) {
                const text = await response.text();
                const match = text.match(/error=([^&"]+)/);
                if (match) {
                    showError(decodeURIComponent(match[1].replace(/\+/g, " ")));
                } else {
                    showError("An unexpected error occurred. Please try again.");
                }
                setSubmitting(false);
                return;
            }

            window.location.href = "/";
        } catch (err) {
            showError("Request failed. Check your network connection.");
            setSubmitting(false);
        }
    });
})();

(function () {
    function attachModal(modalSelector, openSelector, closeSelector) {
        const modal = document.querySelector(modalSelector);
        const openButton = document.querySelector(openSelector);
        if (!modal || !openButton) {
            return;
        }

        const closeButtons = modal.querySelectorAll(closeSelector);

        function openModal() {
            modal.hidden = false;
            const firstClose = modal.querySelector(closeSelector);
            if (firstClose) firstClose.focus();
        }

        function closeModal() {
            modal.hidden = true;
            openButton.focus();
        }

        openButton.addEventListener("click", openModal);
        for (const button of closeButtons) {
            button.addEventListener("click", closeModal);
        }
        modal.addEventListener("click", (event) => {
            if (event.target === modal) {
                closeModal();
            }
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !modal.hidden) {
                closeModal();
            }
        });
    }

    attachModal("[data-logout-modal]", "[data-logout-open]", "[data-logout-close]");
    attachModal("[data-shutdown-modal]", "[data-shutdown-open]", "[data-shutdown-close]");
    attachModal("[data-delete-bot-modal]", "[data-delete-bot-open]", "[data-delete-bot-close]");
})();

(function () {
    const forms = document.querySelectorAll(
        "[data-bot-action-form], form[action$='/start'], form[action$='/stop'], form[action$='/restart']"
    );
    if (!forms.length) {
        return;
    }

    function setBusy(button, active) {
        if (!button) return;
        const label = button.querySelector("[data-label]");
        const baseLabel = button.getAttribute("data-action-label") || (label ? label.textContent : "");
        if (active) {
            button.setAttribute("aria-busy", "true");
            if (label) label.textContent = `${baseLabel}...`;
            const spinner = document.createElement("span");
            spinner.className = "spinner";
            spinner.setAttribute("data-spinner", "");
            button.prepend(spinner);
            return;
        }
        button.removeAttribute("aria-busy");
        if (label) label.textContent = baseLabel;
        const spinner = button.querySelector("[data-spinner]");
        if (spinner) spinner.remove();
    }

    function showActionError(form, message) {
        const container = form.closest(".panel") || form.parentElement;
        if (!container) return;
        let box = container.querySelector("[data-action-error]");
        if (!box) {
            box = document.createElement("div");
            box.className = "form-error action-error";
            box.setAttribute("data-action-error", "");
            container.prepend(box);
        }
        box.textContent = message;
        box.hidden = false;
    }

    function clearActionError(form) {
        const container = form.closest(".panel") || form.parentElement;
        const box = container ? container.querySelector("[data-action-error]") : null;
        if (!box) return;
        box.textContent = "";
        box.hidden = true;
    }

    for (const form of forms) {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            clearActionError(form);
            const button = form.querySelector("button[type='submit']");
            setBusy(button, true);

            try {
                const response = await fetch(form.action, {
                    method: "POST",
                    headers: { Accept: "application/json" },
                });
                const payload = await response.json().catch(() => ({}));
                if (!response.ok || payload.ok === false) {
                    showActionError(form, payload.error || "Action failed. Please try again.");
                    setBusy(button, false);
                    return;
                }
                window.location.reload();
            } catch (err) {
                showActionError(form, "Request failed. Check your network connection.");
                setBusy(button, false);
            }
        });
    }
})();

(function () {
    const logBlock = document.querySelector(".log-block");
    const tailSelect = document.getElementById("tail-select");
    const filterInput = document.getElementById("log-filter");
    const autoRefreshInput = document.getElementById("log-autorefresh");

    if (!logBlock || !tailSelect || !filterInput || !autoRefreshInput) {
        return;
    }

    const botId = tailSelect.getAttribute("data-bot-id") || "";
    const initialStream = tailSelect.getAttribute("data-stream") || "stdout";
    const streamLinks = document.querySelectorAll("[data-log-stream-link]");
    const storageKey = botId ? `adminbot.logs.${botId}.tail` : "";
    let stream = initialStream;
    let timer = null;

    function updateTailState() {
        const tail = tailSelect.value;
        if (storageKey) {
            localStorage.setItem(storageKey, tail);
        }
        const currentUrl = new URL(window.location.href);
        currentUrl.searchParams.set("tail", tail);
        currentUrl.searchParams.set("stream", stream);
        window.history.replaceState(null, "", currentUrl.toString());
        for (const link of streamLinks) {
            const linkUrl = new URL(link.href, window.location.origin);
            linkUrl.searchParams.set("tail", tail);
            link.href = linkUrl.toString();
        }
    }

    if (storageKey) {
        const savedTail = localStorage.getItem(storageKey);
        if (savedTail && [...tailSelect.options].some((option) => option.value === savedTail)) {
            tailSelect.value = savedTail;
        }
    }
    updateTailState();

    const renderLines = (lines) => {
        const term = filterInput.value.trim().toLowerCase();
        const filtered = term
            ? lines.filter((line) => line.toLowerCase().includes(term))
            : lines;
        logBlock.textContent = filtered.length ? filtered.join("\n") : "(no matching log lines)";
    };

    const loadLines = async () => {
        const tail = encodeURIComponent(tailSelect.value);
        const url = `/api/bots/${encodeURIComponent(botId)}/logs?stream=${encodeURIComponent(stream)}&tail=${tail}`;
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        if (!response.ok) {
            return;
        }
        const lines = await response.json();
        if (Array.isArray(lines)) {
            renderLines(lines);
        }
    };

    const schedule = () => {
        if (timer) {
            clearInterval(timer);
            timer = null;
        }
        if (autoRefreshInput.checked) {
            timer = setInterval(loadLines, 3000);
        }
    };

    tailSelect.addEventListener("change", () => {
        updateTailState();
        loadLines();
    });
    filterInput.addEventListener("input", loadLines);
    autoRefreshInput.addEventListener("change", schedule);

    loadLines();
    schedule();
})();
