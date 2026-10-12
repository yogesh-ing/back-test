/**
 * Alert widget — broker-session "Re-login" action tests.
 *
 * The widget normally claims "information only: no action buttons". The one
 * deliberate exception: broker_session_expiring / broker_session_expired
 * alerts carry a Re-login button that opens the existing broker auth popup
 * (window.BrokerAuthUI) so the feed can be revived without leaving the page.
 *
 * Runs under plain Node with a stub DOM; alert_widget.js is loaded with
 * auto-init suppressed and its exported pure render helpers are asserted.
 *
 * Usage: node tests/js/test_alert_widget_relogin.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/alert_widget.js"), "utf8",
);

const sandbox = {
    console,
    setTimeout: () => 0,
    clearTimeout: () => {},
    Date,
    Number,
    Math,
    Promise,
    String,
    Set,
    encodeURIComponent,
    document: {
        getElementById: () => null,
        createElement: () => ({ id: "", className: "", hidden: true, addEventListener: () => {} }),
        addEventListener: () => {},
        body: { classList: { add: () => {} }, dataset: {} },
        hidden: false,
        readyState: "complete",
    },
    window: { __ALERT_WIDGET_NO_AUTOINIT__: true, localStorage: undefined },
};
sandbox.window.showToast = (msg, kind) => { sandbox.__toasts.push([msg, kind]); };
sandbox.__toasts = [];
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "alert_widget.js" });
const AW = vm.runInContext("window.AlertWidget", sandbox);

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

const sessionAlert = {
    alert_id: "a1",
    alert_type: "broker_session_expiring",
    severity: "warning",
    title: "Broker login expiring",
    message: "mStock login expires in under 30 minutes",
    created_at: new Date().toISOString(),
    data: { broker: "mstock", broker_display_name: "mStock", expires_at: "2026-08-25T15:45:00" },
};
const gammaAlert = {
    alert_id: "g1",
    alert_type: "portfolio_gamma_critical",
    severity: "critical",
    title: "Portfolio gamma critical",
    message: "net gamma -500",
    created_at: new Date().toISOString(),
    data: { net_gamma: -500 },
};

test("session alerts are recognised, others are not", () => {
    assert.equal(AW.isSessionAlert(sessionAlert), true);
    assert.equal(AW.isSessionAlert({ ...sessionAlert, alert_type: "broker_session_expired" }), true);
    assert.equal(AW.isSessionAlert(gammaAlert), false);
});

test("list item for a session alert carries a Re-login button (data-broker)", () => {
    const html = AW.renderItem(sessionAlert, Date.now());
    assert.match(html, /data-aw="relogin"/);
    assert.match(html, /data-broker="mstock"/);
    assert.match(html, /Re-login/);
});

test("non-session alerts keep the information-only action row", () => {
    const html = AW.renderItem(gammaAlert, Date.now());
    assert.doesNotMatch(html, /data-aw="relogin"/);
    assert.match(html, /View Details/);
});

test("detail modal footer offers Re-login for a session alert", () => {
    const html = AW.renderDetail({ ...sessionAlert, subscriptions: { subscribed: [], not_subscribed: [] } });
    assert.match(html, /data-aw="relogin"/);
    assert.match(html, /data-broker="mstock"/);
});

test("Re-login opens the broker auth popup targeted at THAT broker", () => {
    const opened = [];
    sandbox.window.BrokerAuthUI = { open: (opts) => opened.push(opts) };
    assert.equal(AW.openRelogin("mstock"), true);
    // Objects cross the vm boundary — compare structurally, not by prototype.
    assert.equal(JSON.stringify(opened), JSON.stringify([{ broker: "mstock" }]));

    assert.equal(AW.openRelogin(""), true);
    assert.equal(JSON.stringify(opened[1]), JSON.stringify({})); // no broker → legacy flow
});

test("without the popup on the page the click toasts instead of failing silently", () => {
    sandbox.window.BrokerAuthUI = undefined;
    sandbox.__toasts.length = 0;
    assert.equal(AW.openRelogin("mstock"), false);
    assert.equal(sandbox.__toasts.length, 1);
    assert.equal(sandbox.__toasts[0][1], "error");
});

console.log(`\n${passed} alert-widget Re-login tests passed`);
