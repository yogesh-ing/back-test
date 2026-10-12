#!/usr/bin/env python3
"""Optional real-browser gate for the trading workspace.

Install Playwright separately; it is not an application dependency:
    pip install playwright && python -m playwright install chromium

Start the app, then run:
    python scripts/check_workspace_ui.py --url http://127.0.0.1:5000

--synthetic-backtest additionally runs the existing stateless backtest API,
only when /health confirms the synthetic source. No runner is created, no
broker is authenticated, and no money-moving action is confirmed.
Screenshots, if requested, should be written to ignored local tooling output.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROUTES = [
    "/", "/backtest", "/portfolio", "/portfolio/paper", "/portfolio/live",
    "/analytics", "/compare", "/optimize", "/strategy-builder", "/forward",
    "/risk", "/reporting", "/settings", "/data", "/optimize/runs/design-check",
]
VIEWPORTS = [(1440, 1000), (768, 1024), (390, 844)]


def audit_layout(page, url, screenshots, theme, width):
    errors = []

    def on_error(error):
        errors.append(str(error))

    page.on("pageerror", on_error)
    response = page.goto(url, wait_until="domcontentloaded")
    page.evaluate("document.fonts.ready")
    page.wait_for_function(
        "document.getElementById('halt-pill-text').textContent !== 'Checking'"
    )
    if url.endswith("/compare"):
        page.wait_for_function("""() => {
            const slots = [...document.querySelectorAll('.slot-card .slot-params')];
            return slots.length >= 2 && slots.every(el => el.children.length > 0);
        }""")
    elif url.endswith("/optimize"):
        page.locator("#optParamTable tbody tr[data-i]").first.wait_for(state="attached")
    elif url.endswith("/risk"):
        page.locator("#cfg-daily-loss").wait_for(state="attached")
    if url.endswith("/settings"):
        page.locator('[data-group="in_use"]').wait_for(state="visible")
        page.wait_for_function("document.getElementById('segments-list').textContent.length > 0")
        assert "[object " not in page.locator("#segments-list").inner_text()
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    assert response.status == 200, f"{url}: HTTP {response.status}"
    assert scroll_width <= width, f"{url}: {scroll_width}px content at {width}px viewport"
    assert not errors, f"{url}: {errors}"
    assert page.locator('a[aria-current="page"]').count() == 1
    assert page.evaluate("""() => {
        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
        return ids.length === new Set(ids).size;
    }"""), f"{url}: duplicate dynamic controller IDs"
    assert not page.locator("#data-fetch-indicator").is_visible()
    assert page.locator("html").get_attribute("data-theme") == theme
    if screenshots:
        slug = url.rsplit("/", 1)[-1] or "home"
        if "/portfolio/" in url:
            slug = "portfolio-" + slug
        elif "/optimize/runs/" in url:
            slug = "optimization-run"
        page.screenshot(path=str(screenshots / f"{slug}-{theme}-{width}.png"))
    page.remove_listener("pageerror", on_error)


def audit_accessibility(page, axe_path, selector=None):
    page.add_script_tag(path=str(axe_path))
    violations = page.evaluate("""async selector => {
        const result = await axe.run(selector || document, {
            runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa']}
        });
        return result.violations.map(v => ({
            id: v.id, impact: v.impact,
            nodes: v.nodes.map(n => ({target: n.target, summary: n.failureSummary}))
        }));
    }""", selector)
    assert not violations, json.dumps(violations, indent=2)


def contain_focus(page, selector, count=20):
    for _ in range(count):
        page.keyboard.press("Tab")
        assert page.locator(selector).evaluate("el => el.contains(document.activeElement)")


def audit_interactions(page, base, axe_path=None):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(base + "/backtest", wait_until="domcontentloaded")
    page.locator("#theme-toggle").click()
    expected = page.locator("html").get_attribute("data-theme")
    page.reload(wait_until="domcontentloaded")
    assert page.locator("html").get_attribute("data-theme") == expected
    page.keyboard.press("Control+k")
    page.locator("#command-search").fill("risk")
    assert page.locator(".command-result:visible").count() == 1
    assert page.locator(".command-result:visible").get_attribute("href") == "/risk"
    page.keyboard.press("Escape")
    assert not page.locator("#workspace-command").is_visible()
    page.locator("#workspace-help").click()
    assert page.locator("#workspace-guide").is_visible()
    page.keyboard.press("Escape")

    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#sidebar-toggle").click()
    page.wait_for_function("document.getElementById('sidebar-toggle').ariaExpanded === 'true'")
    page.wait_for_function(
        "getComputedStyle(document.getElementById('app-sidebar')).visibility === 'visible'"
    )
    contain_focus(page, "#app-sidebar", 25)
    page.keyboard.press("Escape")
    assert page.locator("#sidebar-toggle").get_attribute("aria-expanded") == "false"
    assert page.evaluate("document.activeElement.id") == "sidebar-toggle"

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(base + "/portfolio/paper", wait_until="domcontentloaded")
    page.locator("#btn-add").click()
    page.locator("#spawn-modal").wait_for(state="visible")
    page.wait_for_function(
        "document.getElementById('spawn-modal').getAttribute('role') === 'dialog'"
    )
    assert page.locator("#spawn-mode").input_value() == "paper"
    page.locator('#spawn-strategy option[value="sma_crossover"]').wait_for(state="attached")
    page.select_option("#spawn-strategy", "sma_crossover")
    page.locator("#spawn-params input").first.wait_for(state="visible")
    contain_focus(page, "#spawn-modal")
    if axe_path:
        audit_accessibility(page, axe_path, "#spawn-modal")
    page.set_viewport_size({"width": 390, "height": 844})
    footer = page.locator("#spawn-modal .modal-foot").bounding_box()
    assert footer["y"] >= 0 and footer["y"] + footer["height"] <= 844
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    page.keyboard.press("Escape")
    assert not page.locator("#spawn-modal").is_visible()
    page.wait_for_function("document.activeElement.id === 'btn-add'")
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#broker-status").click()
    page.locator("#broker-board-overlay").wait_for(state="visible")
    contain_focus(page, "#broker-board-overlay")
    if axe_path:
        audit_accessibility(page, axe_path, "#broker-board-overlay")
    page.keyboard.press("Escape")
    assert not page.locator("#broker-board-overlay").is_visible()
    page.locator("#risk-strip-flatten").click()
    page.locator("#risk-flatten-modal").wait_for(state="visible")
    contain_focus(page, "#risk-flatten-modal")
    if axe_path:
        audit_accessibility(page, axe_path, "#risk-flatten-modal")
    page.locator('[data-close="risk-flatten-modal"]').last.click()
    assert not page.locator("#risk-flatten-modal").is_visible()
    page.goto(base + "/settings", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('killswitch-state').textContent.includes('OFF')"
    )
    mutations = []

    def capture_gate_request(request):
        if request.url.endswith("/api/settings/live-kill-switch") and request.method == "PUT":
            mutations.append(request.url)

    page.on("request", capture_gate_request)
    with page.expect_event("dialog") as proposal:
        page.locator("#killswitch-toggle").click()
    assert "real broker orders" in proposal.value.message
    assert not mutations, "Cancelling the proposal must not arm live execution"
    page.remove_listener("request", capture_gate_request)
    # Controlled failure of a read only: unknown must not be painted as OFF.

    def fail_gate_read(route):
        assert route.request.method == "GET"
        route.fulfill(status=503, content_type="application/json", body='{"error":"unavailable"}')

    pattern = "**/api/settings/live-kill-switch"
    page.route(pattern, fail_gate_read)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('killswitch-state').textContent.includes('Unavailable')"
    )
    assert page.locator("#killswitch-toggle").is_disabled()
    page.unroute(pattern, fail_gate_read)


def audit_synthetic_backtest(page, base, screenshots, axe_path=None):
    health = page.request.get(base + "/health").json()
    assert health["source"] == "synthetic", "Generated backtest requires the synthetic source"
    page.goto(base + "/backtest", wait_until="domcontentloaded")
    page.locator('#strategy option[value="sma_crossover"]').wait_for(state="attached")
    with page.expect_response(lambda r: "/api/strategies/sma_crossover/params" in r.url):
        page.select_option("#strategy", "sma_crossover")
    page.locator('#params-container input[data-param="fast"]').wait_for(state="visible")
    page.locator("#useSyntheticDemo").click()
    assert page.locator("#symbol").input_value() == "DEMO"
    assert page.locator("#timeframe").input_value() == "1day"
    assert not page.locator("#results").is_visible()
    page.locator("#capital").fill("100000")
    held = []

    def hold_run(route):
        held.append(route)

    pattern = "**/api/backtest/run"
    page.route(pattern, hold_run)
    with page.expect_response(lambda r: r.url.endswith("/api/backtest/run"), timeout=120000) as run:
        with page.expect_request(lambda r: r.url.endswith("/api/backtest/run")):
            page.locator("#runBtn").click()
        assert page.locator("#runBtn").is_disabled()
        page.evaluate("document.getElementById('runBtn').click()")
        assert len(held) == 1, "A duplicate click must not launch a second run"
        held[0].continue_()
    page.unroute(pattern, hold_run)
    assert run.value.status == 200, run.value.text()
    page.locator("#metricsCards .metric-card").first.wait_for(state="visible")
    page.wait_for_function("!document.getElementById('runBtn').disabled")
    assert page.locator("#results").is_visible()
    assert "Synthetic" in page.locator("#resultProvenance").inner_text()
    snapshot = page.evaluate("""() => {
        const chart = Chart.getChart('equityChart');
        return {data: chart.data.datasets[0].data.slice(),
                color: chart.options.scales.x.ticks.color,
                currency: chart.options.plugins.tooltip.callbacks.label({
                    dataset: {label: 'Equity'}, parsed: {y: 1234.56}
                })};
    }""")
    symbol = page.locator("body").get_attribute("data-currency-symbol")
    assert symbol in snapshot["currency"]
    if axe_path:
        audit_accessibility(page, axe_path)
    page.locator("#theme-toggle").click()
    changed = page.evaluate("""() => {
        const chart = Chart.getChart('equityChart');
        return {data: chart.data.datasets[0].data.slice(),
                color: chart.options.scales.x.ticks.color};
    }""")
    assert snapshot["data"] == changed["data"]
    assert snapshot["color"] != changed["color"]
    if axe_path:
        audit_accessibility(page, axe_path)
    page.locator('.tab[data-tab="drawdown"]').click()
    assert page.evaluate("!!Chart.getChart('drawdownChart')")
    page.locator('.tab[data-tab="signals"]').click()
    assert page.evaluate("!!Chart.getChart('signalsChart')")
    page.locator('.tab[data-tab="equity"]').click()
    if screenshots:
        page.screenshot(path=str(screenshots / "synthetic-backtest-result.png"), full_page=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5000")
    parser.add_argument("--browser", help="Optional Chromium executable path")
    parser.add_argument("--browser-config", type=Path, help="JSON with executablePath and args")
    parser.add_argument("--screenshots", type=Path, help="Use an ignored directory, e.g. .cache/ui")
    parser.add_argument("--axe", type=Path, help="Optional local axe.min.js for WCAG checks")
    parser.add_argument("--synthetic-backtest", action="store_true")
    args = parser.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        parser.error("Install optional test tooling: pip install playwright")
    launch = {"headless": True}
    if args.browser:
        launch["executable_path"] = args.browser
    if args.browser_config:
        config = json.loads(args.browser_config.read_text())
        launch.update(executable_path=config["executablePath"], args=config.get("args", []))
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    base = args.url.rstrip("/")
    with sync_playwright() as tool:
        browser = tool.chromium.launch(**launch)
        failures = []
        context = browser.new_context()
        context.add_init_script(
            "try { if (!localStorage.getItem('trading-workspace.theme')) "
            "localStorage.setItem('trading-workspace.theme', 'dark'); } catch (_) {}"
        )
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.dismiss())
        for theme in ["dark", "light"]:
            if page.url.startswith(base):
                page.evaluate(
                    "theme => localStorage.setItem('trading-workspace.theme', theme)", theme
                )
            for width, height in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": height})
                for route in ROUTES:
                    try:
                        audit_layout(page, base + route, args.screenshots, theme, width)
                        if args.axe and width == 1440:
                            audit_accessibility(page, args.axe)
                    except AssertionError as error:
                        failures.append(f"{route} · {theme} · {width}px: {error}")
                print(f"AUDITED {theme} · {width}px · {len(ROUTES)} screens", flush=True)
        if args.screenshots:
            (args.screenshots / "validation.json").write_text(json.dumps({
                "layouts_checked": len(ROUTES) * len(VIEWPORTS) * 2,
                "wcag_checked": len(ROUTES) * 2 if args.axe else 0,
                "failures": failures,
            }, indent=2))
        assert not failures, "\n".join(failures)
        audit_interactions(page, base, args.axe)
        print("PASS theme persistence, page search, mobile focus and confirmation/auth dialogs")
        if args.synthetic_backtest:
            audit_synthetic_backtest(page, base, args.screenshots, args.axe)
            print("PASS real generated-data backtest, all chart tabs and currency-aware tooltips")
        context.close()
        browser.close()
    print("Workspace browser gate passed. No trading action was confirmed.")


if __name__ == "__main__":
    main()
