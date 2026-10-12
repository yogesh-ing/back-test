/**
 * Reusable dynamic param form (shared by Backtest / Compare / Forward pages).
 * renderParamsInto(container, params, overrides)
 * collectParamsFrom(container)
 * applyOverridesInto(container, overrides)
 */
// Compare uses anonymous per-slot containers. Keep a stable, unique namespace
// per element; a shared fallback name would point labels at the wrong slot.
const paramFormNamespaces = new WeakMap();
let paramFormSequence = 0;

function renderParamsInto(container, params, overrides) {
    if (!container) return;
    const keys = Object.keys(params || {});
    if (!keys.length) {
        container.innerHTML = '<p class="muted small">No parameters</p>';
        return;
    }
    if (!paramFormNamespaces.has(container)) {
        paramFormNamespaces.set(container, "form-" + (++paramFormSequence));
    }
    const namespace = paramFormNamespaces.get(container);
    container.innerHTML = keys.map((key, index) => {
        const spec = params[key];
        const label = spec.label || key;
        const inputId = "param-" + namespace + "-" + index + "-" + key.replace(/[^a-zA-Z0-9_-]/g, "_");
        const tooltip = spec.tooltip ? `<div class="hint">${spec.tooltip}</div>` : "";
        if (spec.type === "bool") {
            const def = (overrides && key in overrides) ? overrides[key] : (spec.default === true || spec.default === "true");
            return `<div class="param-row"><label class="param-check">
                <input type="checkbox" data-param="${key}" ${def ? "checked" : ""}> ${label}</label>${tooltip}</div>`;
        }
        const isNum = spec.type === "int" || spec.type === "float";
        const step = spec.type === "float" ? "any" : "1";
        const min = spec.min != null ? `min="${spec.min}"` : "";
        const max = spec.max != null ? `max="${spec.max}"` : "";
        const def = (overrides && key in overrides) ? overrides[key] : (spec.default ?? "");
        return `<div class="param-row"><label for="${inputId}">${label}</label>
            <input id="${inputId}" class="input" type="${isNum ? "number" : "text"}" step="${step}" ${min} ${max}
                   value="${def}" data-param="${key}">${tooltip}</div>`;
    }).join("");
}

function collectParamsFrom(container) {
    const params = {};
    (container && container.querySelectorAll("[data-param]") || []).forEach((el) => {
        if (el.type === "checkbox") params[el.dataset.param] = el.checked;
        else if (el.type === "number") params[el.dataset.param] = el.value === "" ? null : Number(el.value);
        else params[el.dataset.param] = el.value;
    });
    return params;
}

function applyOverridesInto(container, overrides) {
    if (!overrides) return;
    (container && container.querySelectorAll("[data-param]") || []).forEach((el) => {
        const key = el.dataset.param;
        if (!(key in overrides)) return;
        if (el.type === "checkbox") el.checked = !!overrides[key];
        else el.value = overrides[key];
    });
}
