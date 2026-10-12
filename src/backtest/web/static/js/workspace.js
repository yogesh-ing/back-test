/** Workspace chrome. Presentation/navigation only: no trading API requests. */
(function () {
    "use strict";
    const byId = (id) => document.getElementById(id);
    const root = document.documentElement;
    const sidebar = byId("app-sidebar");
    const workspace = byId("app-workspace");
    const backdrop = byId("sidebar-backdrop");
    const menuButton = byId("sidebar-toggle");
    const themeButton = byId("theme-toggle");
    const mobile = window.matchMedia("(max-width: 1023px)");
    const command = byId("workspace-command");
    const search = byId("command-search");
    const guide = byId("workspace-guide");
    const results = command ? Array.from(command.querySelectorAll(".command-result")) : [];
    let menuOpen = false;
    let selected = 0;
    let previousModal = null;
    let previousFocus = null;

    function focusable(container) {
        return Array.from(container.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]'))
            .filter((el) => !el.closest("[hidden]") && el.getClientRects().length);
    }

    function closeMenu(restoreFocus) {
        menuOpen = false;
        document.body.classList.remove("sidebar-open");
        if (backdrop) backdrop.hidden = true;
        if (menuButton) menuButton.setAttribute("aria-expanded", "false");
        if (workspace) workspace.inert = false;
        if (sidebar) {
            sidebar.inert = mobile.matches;
            if (mobile.matches) sidebar.setAttribute("aria-hidden", "true");
            else sidebar.removeAttribute("aria-hidden");
        }
        if (restoreFocus && menuButton) menuButton.focus();
    }

    function openMenu() {
        if (!mobile.matches || !sidebar) return;
        menuOpen = true;
        sidebar.inert = false;
        sidebar.removeAttribute("aria-hidden");
        document.body.classList.add("sidebar-open");
        if (backdrop) backdrop.hidden = false;
        if (menuButton) menuButton.setAttribute("aria-expanded", "true");
        if (workspace) workspace.inert = true;
        const active = sidebar.querySelector('[aria-current="page"]');
        if (active) active.focus();
    }

    function syncThemeButton() {
        if (!themeButton) return;
        const label = root.dataset.theme === "light" ? "Switch to dark theme" : "Switch to light theme";
        themeButton.setAttribute("aria-label", label);
        themeButton.title = label;
        const meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.content = root.dataset.theme === "light" ? "#f4f6f8" : "#0c1117";
    }

    if (themeButton) themeButton.addEventListener("click", () => {
        root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
        try { localStorage.setItem("trading-workspace.theme", root.dataset.theme); } catch (_) { /* optional */ }
        syncThemeButton();
        document.dispatchEvent(new CustomEvent("workspace:theme", { detail: { theme: root.dataset.theme } }));
    });
    window.addEventListener("storage", (event) => {
        if (event.key !== "trading-workspace.theme" || !["light", "dark"].includes(event.newValue)) return;
        root.dataset.theme = event.newValue;
        syncThemeButton();
        document.dispatchEvent(new CustomEvent("workspace:theme", { detail: { theme: root.dataset.theme } }));
    });
    syncThemeButton();
    closeMenu(false);
    if (menuButton) menuButton.addEventListener("click", () => menuOpen ? closeMenu(true) : openMenu());
    if (byId("sidebar-close")) byId("sidebar-close").addEventListener("click", () => closeMenu(true));
    if (backdrop) backdrop.addEventListener("click", () => closeMenu(true));
    mobile.addEventListener("change", () => closeMenu(false));
    if (sidebar) sidebar.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => closeMenu(false)));

    function visibleResults() { return results.filter((link) => !link.hidden); }
    function selectResult(index) {
        const visible = visibleResults();
        selected = visible.length ? (index + visible.length) % visible.length : 0;
        results.forEach((link) => link.classList.remove("selected"));
        if (visible[selected]) {
            visible[selected].classList.add("selected");
            if (search && search.value) visible[selected].scrollIntoView({ block: "nearest" });
        }
    }
    function filterResults() {
        const query = search.value.toLowerCase().trim();
        results.forEach((link) => { link.hidden = !link.dataset.search.includes(query); });
        byId("command-empty").hidden = visibleResults().length > 0;
        selectResult(0);
    }
    function openDialog(dialog) {
        if (!dialog || dialog.open) return;
        closeMenu(false);
        dialog.showModal();
    }
    function openCommand() {
        if (!command) return;
        search.value = "";
        filterResults();
        openDialog(command);
        search.focus();
    }
    if (byId("command-open")) {
        const key = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ K" : "Ctrl K";
        byId("command-open").querySelector("kbd").textContent = key;
        byId("command-open").addEventListener("click", openCommand);
    }
    if (search) {
        search.addEventListener("input", filterResults);
        search.addEventListener("keydown", (event) => {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                selectResult(selected + (event.key === "ArrowDown" ? 1 : -1));
            } else if (event.key === "Enter") {
                const link = visibleResults()[selected];
                if (link) { event.preventDefault(); link.click(); }
            }
        });
    }
    if (byId("workspace-help")) byId("workspace-help").addEventListener("click", () => openDialog(guide));
    document.querySelectorAll("[data-dialog-close]").forEach((button) => button.addEventListener("click", () => byId(button.dataset.dialogClose).close()));
    [command, guide].filter(Boolean).forEach((dialog) => dialog.addEventListener("click", (event) => {
        if (event.target !== dialog) return;
        const rect = dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    }));
    document.querySelectorAll("[data-trigger]").forEach((button) => button.addEventListener("click", () => {
        const target = byId(button.dataset.trigger);
        if (target && !target.disabled) target.click();
    }));

    // Existing confirmation/auth controllers still own opening and closing.
    // This layer only adds keyboard containment and restores the user's focus.
    function activeOverlay() {
        const overlays = Array.from(document.querySelectorAll(".modal-overlay, .broker-auth-overlay"));
        return overlays.filter((el) => !el.hidden && (el.classList.contains("modal-overlay") || el.classList.contains("open")))
            .sort((a, b) => Number(getComputedStyle(a).zIndex || 0) - Number(getComputedStyle(b).zIndex || 0)).pop() || null;
    }
    function syncModalFocus() {
        const modal = activeOverlay();
        if (modal === previousModal) return;
        if (modal) {
            if (!previousModal) previousFocus = document.activeElement;
            modal.setAttribute("role", "dialog");
            modal.setAttribute("aria-modal", "true");
            const heading = modal.querySelector("h2, h3, .broker-auth-title");
            if (heading && !modal.getAttribute("aria-labelledby")) {
                if (!heading.id) heading.id = modal.id + "-heading";
                modal.setAttribute("aria-labelledby", heading.id);
            }
            const focus = modal.querySelector('input:not([disabled]):not([type="hidden"]), [data-close], .broker-auth-close, .modal-close') || focusable(modal)[0];
            if (focus && focus.getClientRects().length) focus.focus();
        } else if (previousFocus && previousFocus.isConnected) {
            previousFocus.focus();
            previousFocus = null;
        }
        previousModal = modal;
    }
    const modalObserver = new MutationObserver(syncModalFocus);
    document.querySelectorAll(".modal-overlay, .broker-auth-overlay").forEach((el) => {
        modalObserver.observe(el, { attributes: true, attributeFilter: ["hidden", "class"] });
    });

    document.addEventListener("keydown", (event) => {
        const overlay = activeOverlay();
        const nativeDialogOpen = (command && command.open) || (guide && guide.open);
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && !overlay && !nativeDialogOpen) {
            event.preventDefault(); openCommand(); return;
        }
        const editing = event.target.closest && event.target.closest('input, textarea, select, [contenteditable="true"]');
        if (event.key === "?" && !editing && !overlay && !nativeDialogOpen) {
            event.preventDefault(); openDialog(guide); return;
        }
        // Search inputs consume native Escape to clear their value. Closing
        // explicitly gives page search/guide the same one-key dismissal.
        if (event.key === "Escape" && nativeDialogOpen) {
            event.preventDefault();
            (command && command.open ? command : guide).close();
            return;
        }
        if (event.key === "Escape" && menuOpen) { event.preventDefault(); closeMenu(true); return; }
        if (event.key === "Escape" && overlay && !nativeDialogOpen) {
            const close = overlay.querySelector("[data-close], .modal-close, .broker-auth-close");
            if (close) { event.preventDefault(); close.click(); }
        }
        if (event.key === "Tab" && !nativeDialogOpen && (menuOpen || overlay)) {
            const items = focusable(menuOpen ? sidebar : overlay);
            if (!items.length) return;
            const first = items[0], last = items[items.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
    });
})();
