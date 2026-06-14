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
    const logBlock = document.querySelector(".log-block");
    const tailSelect = document.getElementById("tail-select");
    const filterInput = document.getElementById("log-filter");
    const autoRefreshInput = document.getElementById("log-autorefresh");

    if (!logBlock || !tailSelect || !filterInput || !autoRefreshInput) {
        return;
    }

    const botId = tailSelect.getAttribute("data-bot-id") || "";
    const initialStream = tailSelect.getAttribute("data-stream") || "stdout";
    let stream = initialStream;
    let timer = null;

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

    tailSelect.addEventListener("change", loadLines);
    filterInput.addEventListener("input", loadLines);
    autoRefreshInput.addEventListener("change", schedule);

    schedule();
})();
