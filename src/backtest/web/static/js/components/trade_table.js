/**
 * Trade Table component (PRD Task 2.8).
 * renderTradeTable(containerId, trades) — paginated (20/page), sortable.
 */
const TradeTable = (() => {
    const PAGE_SIZE = 20;
    let state = { rows: [], sortKey: "id", sortDir: 1, page: 1 };

    function sorted() {
        const k = state.sortKey, dir = state.sortDir;
        return [...state.rows].sort((a, b) => {
            let va = a[k], vb = b[k];
            if (k === "id") { va = +va; vb = +vb; }
            if (typeof va === "string") return va.localeCompare(vb) * dir;
            return (va - vb) * dir;
        });
    }

    function render() {
        const wrap = document.getElementById("tradeTable-wrap");
        if (!wrap) return;
        const tbody = wrap.querySelector("tbody");
        const pager = document.getElementById("pagination");
        const rows = sorted();
        const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
        state.page = Math.min(state.page, pages);
        const start = (state.page - 1) * PAGE_SIZE;
        const slice = rows.slice(start, start + PAGE_SIZE);

        tbody.innerHTML = slice.length ? slice.map(t => {
            const pnlCls = t.pnl >= 0 ? "pos" : "neg";
            const resCls = t.result === "Win" ? "tag-win" : "tag-loss";
            const icon = t.result === "Win" ? "✅" : "❌";
            return `<tr>
                <td>${t.id}</td><td>${t.date}</td>
                <td>${t.side}</td><td>${t.entry}</td><td>${t.exit}</td>
                <td class="${pnlCls}">${fmtTradePnl(t.pnl)}</td>
                <td class="${resCls}">${icon} ${t.result}</td>
            </tr>`;
        }).join("") : `<tr><td colspan="7" class="muted">No trades</td></tr>`;

        // pagination controls
        let html = "";
        const btn = (label, p, disabled, active) =>
            `<button ${disabled ? "disabled" : ""} class="${active ? "active" : ""}" data-page="${p}">${label}</button>`;
        html += btn("‹", state.page - 1, state.page === 1, false);
        for (let p = 1; p <= pages; p++) html += btn(String(p), p, false, p === state.page);
        html += btn("›", state.page + 1, state.page === pages, false);
        pager.innerHTML = pages > 1 ? html : "";
    }

    function fmtTradePnl(v) {
        const s = v >= 0 ? "+" : "-";
        return `${s}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    }

    function renderTable(containerId, trades) {
        state = { rows: trades || [], sortKey: "id", sortDir: 1, page: 1 };
        const wrap = document.getElementById(containerId);
        if (!wrap) return;
        // sortable headers
        wrap.querySelectorAll("th[data-sort]").forEach(th => {
            th.onclick = () => {
                const k = th.dataset.sort;
                if (state.sortKey === k) state.sortDir *= -1;
                else { state.sortKey = k; state.sortDir = 1; }
                render();
            };
        });
        const pag = document.getElementById("pagination");
        if (pag) pag.onclick = (e) => {
            if (e.target.dataset.page) { state.page = +e.target.dataset.page; render(); }
        };
        render();
    }

    return { render: renderTable };
})();
