/**
 * Loading state component (PRD Task 5.4).
 */
function showLoader(containerId, message = "Running…") {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.dataset.prevDisplay = el.style.display;
    el.innerHTML = `<div class="loader"><div class="spinner"></div><div class="muted">${message}</div></div>`;
}
function hideLoader(containerId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.innerHTML = "";
}
