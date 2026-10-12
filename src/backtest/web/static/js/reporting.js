/**
 * Consolidated P&L page (PRD-002).
 *
 * The page's job is to make three numbers impossible to confuse: what the
 * trades made gross, what the brokers/statutory charges/tax took, and what is
 * left. Formatting lives in pure "view model" functions so the behaviour that
 * matters (which rows appear, how an estimate is labelled, how a
 * reconciliation difference is described) is testable without a browser —
 * see tests/js/test_reporting.mjs.
 */
const Reporting = (() => {
    const state = { report: null, config: null };

    // ---------------------------------------------------------------- format
    function money(value) {
        const n = Number(value || 0);
        const sign = n < 0 ? "-" : "";
        return sign + "₹" + Math.abs(n).toLocaleString("en-IN", {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
        });
    }

    function pct(value, digits) {
        const n = Number(value || 0);
        return n.toFixed(digits === undefined ? 1 : digits) + "%";
    }

    function escapeHtml(text) {
        return String(text === undefined || text === null ? "" : text)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    // ------------------------------------------------------------ view models
    function summaryModel(report) {
        const s = (report && report.summary) || {};
        const feeRows = (report && report.fee_rows) || [];
        const rows = [{ label: "Gross P&L", amount: Number(s.gross_pnl || 0), kind: "gross" }];
        feeRows.forEach((row) => {
            rows.push({ label: row.label, amount: -Number(row.amount || 0), kind: "fee" });
        });
        if (Number(s.slippage || 0) !== 0) {
            rows.push({ label: "Slippage (recorded)", amount: -Number(s.slippage), kind: "fee" });
        }
        return {
            rows,
            net: Number(s.net_pnl || 0),
            tax: Number(s.tax || 0),
            netAfterTax: Number(s.net_after_tax || 0),
            dragPct: Number(s.cost_drag_pct || 0),
            estimated: ((report && report.cost_basis) || {}).estimated || 0,
            stats: (report && report.stats) || {},
        };
    }

    function brokerModel(report) {
        return ((report && report.by_broker) || []).map((row) => ({
            broker: row.broker,
            trades: row.trades,
            net: Number(row.net_pnl || 0),
            share: Number(row.share_pct || 0),
            modes: (row.modes || []).join("/"),
            estimated: Number(row.estimated_fee_trades || 0),
        }));
    }

    function taxModel(report) {
        return ((report && report.by_category) || []).map((row) => ({
            key: row.category,
            label: row.label,
            treatment: row.treatment,
            lossRule: row.loss_rule,
            schedule: row.schedule,
            net: Number(row.net_pnl || 0),
            rate: Number(row.rate || 0),
            tax: Number(row.estimated_tax || 0),
            trades: row.trades,
        }));
    }

    function warningModel(report) {
        const r = report || {};
        return {
            warnings: r.warnings || [],
            notes: r.data_notes || [],
            sources: r.sources || [],
            costBasis: r.cost_basis || {},
            demo: Boolean(r.demo),
            carryForward: ((r.tax || {}).carry_forward) || [],
            disclaimer: ((r.tax || {}).rules || {}).disclaimer || "",
        };
    }

    function reconcileModel(result) {
        const r = result || {};
        const difference = Number(r.difference || 0);
        const components = Object.keys(r.component_breakdown || {}).map((key) => ({
            key,
            platform: Number((r.component_breakdown[key] || {}).platform || 0),
            broker: Number((r.component_breakdown[key] || {}).broker || 0),
            difference: Number((r.component_breakdown[key] || {}).difference || 0),
        }));
        let headline;
        if (r.status === "PASS") {
            headline = "Within the rounding tolerance — nothing to chase.";
        } else if (r.status === "WARNING") {
            headline = "Small difference — likely a missing or bundled charge component.";
        } else {
            headline = "Material difference — do not file until both sides agree.";
        }
        return {
            status: r.status || "UNKNOWN",
            headline,
            difference,
            differencePct: Number(r.difference_pct || 0),
            platform: Number(r.platform_pnl || 0),
            broker: Number(r.broker_pnl || 0),
            components,
            causes: r.likely_causes || [],
            warnings: r.warnings || [],
            trades: Number(r.trades_compared || 0),
        };
    }

    function ledgerModel(report, limit) {
        const cap = limit || 200;
        const trades = (report && report.trades) || [];
        return {
            rows: trades.slice(0, cap).map((t) => ({
                exit: t.exit_date,
                broker: t.broker,
                mode: t.mode,
                symbol: t.symbol,
                quantity: Number(t.quantity || 0),
                gross: Number(t.gross_pnl || 0),
                fees: Number(t.fees || 0),
                net: Number(t.net_pnl || 0),
                category: t.tax_category_label,
                basis: t.fees_basis,
                tag: t.tag,
            })),
            total: trades.length,
            shown: Math.min(trades.length, cap),
        };
    }

    // --------------------------------------------------------------- renderers
    function card(label, value, hint, tone) {
        const color = tone === "good" ? "var(--success-text)" : tone === "bad" ? "var(--danger-text)" : "var(--text, #e6e6e6)";
        return (
            '<div style="border:1px solid var(--border,#333); border-radius:10px; padding:12px 14px; min-width:190px; flex:1;">' +
            '<div class="muted" style="font-size:12px;">' + escapeHtml(label) + "</div>" +
            '<div style="font-size:20px; font-weight:700; color:' + color + ';">' + escapeHtml(value) + "</div>" +
            (hint ? '<div class="muted" style="font-size:11px;">' + escapeHtml(hint) + "</div>" : "") +
            "</div>"
        );
    }

    function summaryHtml(report) {
        const m = summaryModel(report);
        const ladder = m.rows.map((row) =>
            '<tr><td style="padding:4px 8px;">' + escapeHtml(row.label) + "</td>" +
            '<td style="padding:4px 8px; text-align:right; color:' +
            (row.amount < 0 ? "var(--danger-text)" : "inherit") + ';">' + money(row.amount) + "</td></tr>"
        ).join("");
        const stats = m.stats || {};
        return (
            '<div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px;">' +
            card("Gross P&L", money(m.rows[0] ? m.rows[0].amount : 0), (stats.trade_count || 0) + " closed trades") +
            card("Net P&L", money(m.net), "after all costs") +
            card("Estimated tax", money(m.tax), "slab estimate — see caveats", m.tax > 0 ? "bad" : "") +
            card("Net after tax", money(m.netAfterTax), "the number that matters", m.netAfterTax >= 0 ? "good" : "bad") +
            card("Cost + tax drag", pct(m.dragPct), "share of gross profit", m.dragPct > 30 ? "bad" : "") +
            "</div>" +
            '<div style="border:1px solid var(--border,#333); border-radius:10px; padding:12px;">' +
            '<strong>Gross → net ladder</strong><table style="width:100%; margin-top:8px; font-size:13px;">' +
            ladder +
            '<tr><td style="padding:6px 8px; border-top:1px solid var(--border,#333); font-weight:700;">Net P&L</td>' +
            '<td style="padding:6px 8px; text-align:right; border-top:1px solid var(--border,#333); font-weight:700;">' +
            money(m.net) + "</td></tr></table>" +
            '<div class="muted" style="font-size:11px; margin-top:6px;">' +
            (m.estimated ? "⚠️ " + m.estimated + " trade(s) carry ESTIMATED fee stacks — modelled, not observed."
                         : "All cost stacks are recorded from the sources.") +
            "</div></div>"
        );
    }

    function brokerHtml(report) {
        const rows = brokerModel(report);
        if (!rows.length) return "";
        const body = rows.map((row) =>
            "<tr><td style=\"padding:4px 8px;\">" + escapeHtml(row.broker) + "</td>" +
            '<td style="padding:4px 8px; text-align:right;">' + row.trades + "</td>" +
            '<td style="padding:4px 8px; text-align:right;">' + money(row.net) + "</td>" +
            '<td style="padding:4px 8px; text-align:right;">' + pct(row.share) + "</td>" +
            '<td style="padding:4px 8px;">' + escapeHtml(row.modes) + "</td></tr>"
        ).join("");
        return (
            '<div style="border:1px solid var(--border,#333); border-radius:10px; padding:12px; margin-bottom:14px;">' +
            "<strong>By broker</strong>" +
            '<table style="width:100%; margin-top:8px; font-size:13px;">' +
            '<tr class="muted"><th style="text-align:left; padding:4px 8px;">Broker</th>' +
            '<th style="text-align:right; padding:4px 8px;">Trades</th>' +
            '<th style="text-align:right; padding:4px 8px;">Net P&amp;L</th>' +
            '<th style="text-align:right; padding:4px 8px;">Share</th>' +
            '<th style="text-align:left; padding:4px 8px;">Mode</th></tr>' + body + "</table></div>"
        );
    }

    function taxHtml(report) {
        const rows = taxModel(report);
        if (!rows.length) return "";
        const body = rows.map((row) =>
            "<tr><td style=\"padding:4px 8px;\">" + escapeHtml(row.label) +
            '<div class="muted" style="font-size:11px;">' + escapeHtml(row.treatment) + "</div></td>" +
            '<td style="padding:4px 8px; text-align:right;">' + money(row.net) + "</td>" +
            '<td style="padding:4px 8px; text-align:right;">' + (row.rate ? pct(row.rate * 100) : "—") + "</td>" +
            '<td style="padding:4px 8px; text-align:right;">' + money(row.tax) + "</td>" +
            "<td style=\"padding:4px 8px; font-size:11px;\">" + escapeHtml(row.schedule) + "</td></tr>"
        ).join("");
        const tax = (report && report.tax) || {};
        const carry = (tax.carry_forward || []).map((entry) =>
            '<li style="font-size:12px;">' + escapeHtml(entry.label) + ": " + money(entry.amount) +
            " — " + escapeHtml(entry.rule) + "</li>"
        ).join("");
        return (
            '<div style="border:1px solid var(--border,#333); border-radius:10px; padding:12px; margin-bottom:14px;">' +
            "<strong>Tax categorisation</strong>" +
            '<table style="width:100%; margin-top:8px; font-size:13px;">' +
            '<tr class="muted"><th style="text-align:left; padding:4px 8px;">Category</th>' +
            '<th style="text-align:right; padding:4px 8px;">Net P&amp;L</th>' +
            '<th style="text-align:right; padding:4px 8px;">Rate</th>' +
            '<th style="text-align:right; padding:4px 8px;">Est. tax</th>' +
            '<th style="text-align:left; padding:4px 8px;">Schedule</th></tr>' + body +
            '<tr><td style="padding:6px 8px; font-weight:700;">Total (incl. cess &amp; surcharge)</td>' +
            '<td colspan="2"></td><td style="padding:6px 8px; text-align:right; font-weight:700;">' +
            money(tax.total) + "</td><td></td></tr></table>" +
            (carry ? '<div style="margin-top:8px;"><strong style="font-size:12px;">Losses carried forward</strong><ul style="margin:4px 0 0 18px;">' + carry + "</ul></div>" : "") +
            ((tax.notes || []).map((note) => '<div class="muted" style="font-size:11px; margin-top:4px;">· ' + escapeHtml(note) + "</div>").join("")) +
            "</div>"
        );
    }

    function caveatsHtml(report) {
        const m = warningModel(report);
        const warnings = m.warnings.map((w) => '<li style="color:var(--warning-text); font-size:12px;">' + escapeHtml(w) + "</li>").join("");
        const notes = m.notes.map((w) => '<li class="muted" style="font-size:12px;">' + escapeHtml(w) + "</li>").join("");
        const sources = m.sources.map((s) =>
            '<li style="font-size:12px;">' + escapeHtml(s.name) + " — " + escapeHtml(s.note || "ok") + "</li>"
        ).join("");
        if (!warnings && !notes && !sources) return "";
        return (
            '<div style="border:1px solid var(--border,#333); border-radius:10px; padding:12px; margin-bottom:14px;">' +
            "<strong>What this report does and does not know</strong>" +
            (warnings ? "<ul style=\"margin:6px 0 0 18px;\">" + warnings + "</ul>" : "") +
            (notes ? "<ul style=\"margin:6px 0 0 18px;\">" + notes + "</ul>" : "") +
            '<div class="muted" style="font-size:12px; margin-top:6px;">Sources</div>' +
            "<ul style=\"margin:2px 0 0 18px;\">" + sources + "</ul>" +
            '<div class="muted" style="font-size:11px; margin-top:6px;">' + escapeHtml(m.disclaimer) + "</div>" +
            "</div>"
        );
    }

    function reconcileHtml(result) {
        const m = reconcileModel(result);
        const tone = m.status === "PASS" ? "var(--success-text)" : m.status === "WARNING" ? "var(--warning-text)" : "var(--danger-text)";
        const components = m.components.map((c) =>
            "<tr><td style=\"padding:3px 8px;\">" + escapeHtml(c.key) + "</td>" +
            '<td style="padding:3px 8px; text-align:right;">' + money(c.platform) + "</td>" +
            '<td style="padding:3px 8px; text-align:right;">' + money(c.broker) + "</td>" +
            '<td style="padding:3px 8px; text-align:right; color:' + (c.difference ? "var(--danger-text)" : "inherit") + ';">' +
            money(c.difference) + "</td></tr>"
        ).join("");
        return (
            '<div style="border:1px solid ' + tone + '; border-radius:10px; padding:12px;">' +
            '<strong style="color:' + tone + ';">' + escapeHtml(m.status) + "</strong> — " + escapeHtml(m.headline) +
            '<div class="muted" style="font-size:12px; margin-top:4px;">platform ' + money(m.platform) +
            " vs note " + money(m.broker) + " · difference " + money(m.difference) +
            " (" + pct(m.differencePct) + ") over " + m.trades + " trade(s)</div>" +
            (components ? '<table style="width:100%; margin-top:8px; font-size:12px;">' +
                '<tr class="muted"><th style="text-align:left; padding:3px 8px;">Component</th>' +
                '<th style="text-align:right; padding:3px 8px;">Platform</th>' +
                '<th style="text-align:right; padding:3px 8px;">Note</th>' +
                '<th style="text-align:right; padding:3px 8px;">Δ</th></tr>' + components + "</table>" : "") +
            (m.causes.length ? "<ul style=\"margin:6px 0 0 18px;\">" +
                m.causes.map((c) => '<li style="font-size:12px;">' + escapeHtml(c) + "</li>").join("") + "</ul>" : "") +
            (m.warnings.length ? "<ul style=\"margin:6px 0 0 18px;\">" +
                m.warnings.map((c) => '<li style="font-size:12px; color:var(--warning-text);">' + escapeHtml(c) + "</li>").join("") + "</ul>" : "") +
            "</div>"
        );
    }

    function ledgerHtml(report) {
        const m = ledgerModel(report);
        if (!m.rows.length) {
            return '<div class="muted">No closed trades in this period.</div>';
        }
        const body = m.rows.map((row) =>
            "<tr><td style=\"padding:3px 8px;\">" + escapeHtml(row.exit) + "</td>" +
            "<td style=\"padding:3px 8px;\">" + escapeHtml(row.broker) + "</td>" +
            "<td style=\"padding:3px 8px;\">" + escapeHtml(row.symbol) + "</td>" +
            '<td style="padding:3px 8px; text-align:right;">' + row.quantity + "</td>" +
            '<td style="padding:3px 8px; text-align:right;">' + money(row.fees) + "</td>" +
            '<td style="padding:3px 8px; text-align:right;">' + money(row.net) + "</td>" +
            "<td style=\"padding:3px 8px; font-size:11px;\">" + escapeHtml(row.category) +
            (row.basis === "estimated" ? ' <span title="fee stack estimated">*</span>' : "") +
            (row.tag ? ' <span class="muted">' + escapeHtml(row.tag) + "</span>" : "") + "</td></tr>"
        ).join("");
        return (
            '<div style="border:1px solid var(--border,#333); border-radius:10px; padding:12px;">' +
            "<strong>Trade ledger</strong> <span class=\"muted\" style=\"font-size:12px;\">" +
            m.shown + " of " + m.total + " trades (* = estimated fees)</span>" +
            '<table style="width:100%; margin-top:8px; font-size:12px;">' +
            '<tr class="muted"><th style="text-align:left; padding:3px 8px;">Exit</th>' +
            '<th style="text-align:left; padding:3px 8px;">Broker</th>' +
            '<th style="text-align:left; padding:3px 8px;">Symbol</th>' +
            '<th style="text-align:right; padding:3px 8px;">Qty</th>' +
            '<th style="text-align:right; padding:3px 8px;">Fees</th>' +
            '<th style="text-align:right; padding:3px 8px;">Net</th>' +
            '<th style="text-align:left; padding:3px 8px;">Tax bucket</th></tr>' + body +
            "</table></div>"
        );
    }

    // -------------------------------------------------------------- API calls
    function params() {
        const from = document.getElementById("rp-from");
        const to = document.getElementById("rp-to");
        const paper = document.getElementById("rp-paper");
        const brokers = document.getElementById("rp-brokers");
        const demo = document.getElementById("rp-demo");
        return {
            from_date: from && from.value ? from.value : "",
            to_date: to && to.value ? to.value : "",
            include_paper: paper ? paper.checked : true,
            brokers: brokers && brokers.value ? brokers.value : "",
            demo: !!(demo && demo.checked),
        };
    }

    function queryString(base) {
        const p = params();
        const qs = new URLSearchParams();
        if (p.from_date) qs.set("from_date", p.from_date);
        if (p.to_date) qs.set("to_date", p.to_date);
        qs.set("include_paper", p.include_paper ? "true" : "false");
        if (p.brokers) qs.set("brokers", p.brokers);
        if (p.demo) qs.set("demo", "1");
        return base + "?" + qs.toString();
    }

    function message(text, tone) {
        const host = document.getElementById("rp-message");
        if (!host) return;
        host.innerHTML = text
            ? '<span style="color:' + (tone === "bad" ? "var(--danger-text)" : tone === "warn" ? "var(--warning-text)" : "var(--success-text)") +
              ';">' + escapeHtml(text) + "</span>"
            : "";
    }

    function render(report) {
        state.report = report;
        const set = (id, html) => {
            const host = document.getElementById(id);
            if (host) host.innerHTML = html;
        };
        const banner = document.getElementById("rp-banner");
        if (banner) {
            banner.innerHTML = report && report.demo
                ? '<div style="border:1px solid var(--warning-text); background:var(--warning-tint); border-radius:10px; ' +
                  'padding:10px 12px; margin-bottom:12px; color:var(--warning-text);">DEMO BOOK — sample trades, ' +
                  "clearly labelled, excluded from tax. Not a record of any real account.</div>"
                : "";
        }
        set("rp-summary", summaryHtml(report));
        set("rp-brokers-block", brokerHtml(report));
        set("rp-tax-block", taxHtml(report));
        set("rp-caveats-block", caveatsHtml(report));
        set("rp-ledger-block", ledgerHtml(report));
    }

    function loadReport() {
        message("Loading…", "info");
        return fetch(queryString("/api/reporting/pnl/consolidated"))
            .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
            .then(({ ok, body }) => {
                if (!ok || !body || body.success === false) {
                    message((body && body.error) || "report failed", "bad");
                    return null;
                }
                render(body);
                message("Report generated for " + body.period.label, "good");
                return body;
            })
            .catch((err) => {
                message("report failed: " + err, "bad");
                return null;
            });
    }

    function download(url, payload, filename) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        }).then((response) => {
            if (!response.ok) return response.json().then((b) => { throw new Error((b && b.error) || "export failed"); });
            return response.blob();
        }).then((blob) => {
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            link.remove();
            message("Downloaded " + filename, "good");
        }).catch((err) => message("export failed: " + err, "bad"));
    }

    function bodyPayload(extra) {
        const p = params();
        return Object.assign({
            from_date: p.from_date, to_date: p.to_date,
            include_paper: p.include_paper, brokers: p.brokers, demo: p.demo,
        }, extra || {});
    }

    // ------------------------------------------------------------- reconcile
    function reconcileRow(broker) {
        return (
            '<div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:8px;" data-broker="' +
            escapeHtml(broker) + '">' +
            '<span style="min-width:110px;">' + escapeHtml(broker) + "</span>" +
            '<input class="input rp-note-pnl" type="number" step="0.01" placeholder="contract note net P&L" style="width:210px;">' +
            '<input class="input rp-note-fees" type="number" step="0.01" placeholder="note fees (optional)" style="width:190px;">' +
            '<button class="btn btn-small rp-reconcile" type="button">Reconcile</button>' +
            '<div class="rp-reconcile-result" style="width:100%;"></div></div>'
        );
    }

    function reconcileBlock(report) {
        const brokers = brokerModel(report).map((row) => row.broker);
        if (!brokers.length) return { html: "", brokers: [] };
        return {
            brokers,
            html: '<div style="border:1px solid var(--border,#333); border-radius:10px; padding:12px; margin-bottom:14px;">' +
                "<strong>Broker reconciliation</strong>" +
                '<div class="muted" style="font-size:12px;">Paste the contract note totals — the platform compares them ' +
                "component by component and says what it thinks explains the difference.</div>" +
                brokers.map(reconcileRow).join("") + "</div>",
        };
    }

    function wireReconcile() {
        document.querySelectorAll(".rp-reconcile").forEach((button) => {
            button.addEventListener("click", () => {
                const row = button.closest("[data-broker]");
                const broker = row.getAttribute("data-broker");
                const pnl = row.querySelector(".rp-note-pnl").value;
                const fees = row.querySelector(".rp-note-fees").value;
                const result = row.querySelector(".rp-reconcile-result");
                if (pnl === "") {
                    result.innerHTML = '<span style="color:var(--warning-text); font-size:12px;">Enter the note net P&L first.</span>';
                    return;
                }
                const payload = bodyPayload({ broker: broker, contract_note_pnl: Number(pnl) });
                if (fees !== "") payload.fees_total = Number(fees);
                fetch("/api/reporting/pnl/reconcile", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                }).then((r) => r.json()).then((body) => {
                    if (body.success === false) throw new Error(body.error || "reconcile failed");
                    result.innerHTML = reconcileHtml(body);
                }).catch((err) => {
                    result.innerHTML = '<span style="color:var(--danger-text); font-size:12px;">' + escapeHtml(err.message) + "</span>";
                });
            });
        });
    }

    // ------------------------------------------------------------------ init
    function init() {
        const today = new Date();
        const fyStart = new Date(today.getMonth() >= 3 ? today.getFullYear() : today.getFullYear() - 1, 3, 1);
        const from = document.getElementById("rp-from");
        const to = document.getElementById("rp-to");
        if (from && !from.value) from.value = fyStart.toISOString().slice(0, 10);
        if (to && !to.value) to.value = today.toISOString().slice(0, 10);

        const loadBtn = document.getElementById("rp-load");
        if (loadBtn) loadBtn.addEventListener("click", () => loadReport());
        const pdf = document.getElementById("rp-pdf");
        if (pdf) pdf.addEventListener("click", () => download(
            "/api/reporting/pnl/export/pdf", bodyPayload(), "consolidated_pnl.pdf"));
        const itr = document.getElementById("rp-itr");
        if (itr) itr.addEventListener("click", () => download(
            "/api/reporting/pnl/export/itr", bodyPayload({ format: "xlsx" }), "itr_annexures.xlsx"));
        const trades = document.getElementById("rp-trades");
        if (trades) trades.addEventListener("click", () => download(
            "/api/reporting/pnl/export/trades", bodyPayload(), "trades.csv"));
        const email = document.getElementById("rp-email");
        if (email) email.addEventListener("click", () => {
            fetch("/api/reporting/email", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(bodyPayload({ dry_run: true })),
            }).then((r) => r.json()).then((body) => {
                if (body.success === false) throw new Error(body.error || "email failed");
                message(body.sent ? "Email sent to " + body.to : "Dry run written to " + (body.path || "outbox"), "good");
            }).catch((err) => message("email failed: " + err, "bad"));
        });

        const block = document.getElementById("rp-reconcile-block");
        loadReport().then((report) => {
            if (!block || !report) return;
            const built = reconcileBlock(report);
            block.innerHTML = built.html;
            wireReconcile();
        });
    }

    return {
        init, render, loadReport, money, pct,
        summaryModel, brokerModel, taxModel, warningModel, reconcileModel, ledgerModel,
        summaryHtml, brokerHtml, taxHtml, caveatsHtml, reconcileHtml, ledgerHtml,
    };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Reporting;
