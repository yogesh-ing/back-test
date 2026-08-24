/**
 * Reusable dynamic param form (shared by Backtest / Compare / Forward pages).
 * renderParamsInto(container, params, overrides)
 * collectParamsFrom(container)
 * applyOverridesInto(container, overrides)
 */
function renderParamsInto(container, params, overrides) {
    if (!container) return;
    const keys = Object.keys(params || {});
    if (!keys.length) {
        container.innerHTML = '<p class="muted small">No parameters</p>';
        return;
    }
    container.innerHTML = keys.map((key) => {
        const spec = params[key];
        const label = spec.label || key;
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
        return `<div class="param-row"><label>${label}</label>
            <input class="input" type="${isNum ? "number" : "text"}" step="${step}" ${min} ${max}
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
