/** Shared Chart.js presentation. No data, units or scale callbacks are changed. */
(function (global) {
    "use strict";
    function palette() {
        const styles = getComputedStyle(document.documentElement);
        const value = (name) => styles.getPropertyValue(name).trim();
        return {
            text: value("--text"), muted: value("--muted"), primary: value("--primary"),
            success: value("--success"), danger: value("--danger"), warning: value("--warning"),
            info: value("--info"), violet: value("--violet"), grid: value("--chart-grid"),
            fill: value("--chart-fill"), tooltip: value("--chart-tooltip"), border: value("--border"),
        };
    }
    global.ChartTheme = { palette };
    if (!global.Chart) return;
    const chart = global.Chart;
    chart.defaults.font.family = '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
    chart.defaults.font.size = 11;
    chart.defaults.animation.duration = 200;
    chart.defaults.plugins.legend.align = "start";
    chart.defaults.plugins.legend.labels.usePointStyle = true;
    chart.defaults.plugins.legend.labels.pointStyle = "circle";
    chart.defaults.plugins.legend.labels.boxWidth = 7;
    chart.defaults.plugins.legend.labels.boxHeight = 7;
    chart.defaults.plugins.legend.labels.padding = 18;
    chart.defaults.elements.point.hoverRadius = 4;
    chart.defaults.elements.line.borderWidth = 2;

    const colors = {
        "#3b82f6": "primary", "#4099ff": "primary", "#5b8dbe": "primary",
        "#60a5fa": "info", "#93c5fd": "info", "#38bdf8": "info",
        "#e0938f": "danger", "#ef4444": "danger", "#f87171": "danger", "#be5c58": "danger",
        "#7fc8a0": "success", "#4a8c6a": "success", "#22c55e": "success", "#4ade80": "success",
        "#d4b26a": "warning", "#f59e0b": "warning", "#8b5cf6": "violet",
        "#94a3b8": "muted", "rgba(59,130,246,.10)": "fill", "rgba(59,130,246,0.1)": "fill",
    };
    chart.register({
        id: "workspaceTheme",
        beforeUpdate(instance) {
            const p = palette();
            instance.options.color = p.text;
            for (const axis of Object.values(instance.options.scales || {})) {
                if (axis.ticks) axis.ticks.color = p.muted;
                if (axis.grid) { axis.grid.color = p.grid; axis.grid.drawTicks = false; }
                if (axis.border) axis.border.display = false;
                if (axis.title) axis.title.color = p.muted;
            }
            const plugins = instance.options.plugins || {};
            if (plugins.legend && plugins.legend.labels) plugins.legend.labels.color = p.muted;
            if (plugins.tooltip) {
                Object.assign(plugins.tooltip, {
                    backgroundColor: p.tooltip, titleColor: p.text, bodyColor: p.text,
                    borderColor: p.border, borderWidth: 1, padding: 12, cornerRadius: 8,
                    titleFont: { weight: "600" }, bodyFont: { family: '"JetBrains Mono", monospace', size: 11 },
                });
            }
            for (const dataset of instance.data.datasets) {
                dataset._workspaceColorTokens = dataset._workspaceColorTokens || {};
                for (const field of ["borderColor", "backgroundColor", "pointBackgroundColor", "pointBorderColor"]) {
                    if (!dataset._workspaceColorTokens[field] && typeof dataset[field] === "string" && colors[dataset[field]]) {
                        dataset._workspaceColorTokens[field] = colors[dataset[field]];
                    }
                    const token = dataset._workspaceColorTokens[field];
                    if (token) dataset[field] = p[token];
                }
            }
        },
    });
    document.addEventListener("workspace:theme", () => {
        Object.values(chart.instances || {}).forEach((instance) => instance.update("none"));
    });
})(window);
