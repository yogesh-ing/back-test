/**
 * Toast notifications (PRD Task 5.3).
 * showToast(message, type)  type: 'success' | 'warning' | 'error'
 */
function showToast(message, type = "success", ms = 3000) {
    const stack = document.getElementById("toast-stack");
    if (!stack) return;
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(() => {
        el.style.opacity = "0";
        el.style.transition = "opacity .3s";
        setTimeout(() => el.remove(), 300);
    }, ms);
}
