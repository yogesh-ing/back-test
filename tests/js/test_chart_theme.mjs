import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const script = readFileSync('src/backtest/web/static/js/components/chart_theme.js', 'utf8');
const listeners = {};
let mode = 'dark';
let plugin;
const palettes = {
    dark: { '--text': '#d3dce4', '--muted': '#91a1af', '--primary': '#59d9bb', '--success': '#65d0a5', '--danger': '#f18491', '--warning': '#e6b970', '--info': '#86b8ed', '--violet': '#b6a4e8', '--chart-grid': 'dark-grid', '--chart-fill': 'dark-fill', '--chart-tooltip': '#202d38', '--border': '#273440' },
    light: { '--text': '#344757', '--muted': '#546b7d', '--primary': '#117a66', '--success': '#177a53', '--danger': '#bc3b52', '--warning': '#976017', '--info': '#2a6cae', '--violet': '#7551b0', '--chart-grid': 'light-grid', '--chart-fill': 'light-fill', '--chart-tooltip': '#fff', '--border': '#d7e0e6' },
};
const fakeChart = {
    defaults: { font: {}, animation: {}, plugins: { legend: { labels: {} } }, elements: { point: {}, line: {} } },
    instances: {},
    register(p) { plugin = p; },
};
const sandbox = {
    window: { Chart: fakeChart },
    document: { documentElement: {}, addEventListener(name, handler) { listeners[name] = handler; } },
    getComputedStyle() { return { getPropertyValue(name) { return palettes[mode][name] || ''; } }; },
};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);
let passed = 0;
function test(name, fn) { fn(); passed++; console.log(`ok - ${name}`); }
const callback = (item) => `original-unit:${item.parsed.y}`;
const data = [100, 102, 97];
const instance = {
    data: { labels: ['a', 'b', 'c'], datasets: [
        { label: 'Equity', data, borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,.10)' },
        { label: 'Loss', data: [-1, -2], borderColor: '#e0938f' },
        { label: 'Different series', data: [2, 3], borderColor: ['#3b82f6', '#fff'] },
    ] },
    options: { scales: { x: { ticks: {}, grid: {}, border: {}, title: {} }, y: { min: 0, max: 150, reverse: true, ticks: { callback }, grid: {}, border: {} } }, plugins: { legend: { labels: {} }, tooltip: { callbacks: { label: callback } } } },
};

test('theme has one shared plugin', () => assert.equal(plugin.id, 'workspaceTheme'));
test('self-hosted interface font and compact legends are defaults', () => {
    assert.match(fakeChart.defaults.font.family, /Inter/);
    assert.equal(fakeChart.defaults.plugins.legend.labels.usePointStyle, true);
});
test('axes and tooltip use tokens', () => {
    plugin.beforeUpdate(instance);
    assert.equal(instance.options.scales.x.ticks.color, palettes.dark['--muted']);
    assert.equal(instance.options.scales.x.grid.color, 'dark-grid');
    assert.equal(instance.options.plugins.tooltip.backgroundColor, '#202d38');
});
test('numeric data, labels, domain and unit callbacks are unchanged', () => {
    assert.equal(instance.data.datasets[0].data, data);
    assert.deepEqual(data, [100, 102, 97]);
    assert.equal(instance.data.datasets[0].label, 'Equity');
    assert.equal(instance.options.scales.y.max, 150);
    assert.equal(instance.options.scales.y.reverse, true);
    assert.equal(instance.options.scales.y.ticks.callback, callback);
    assert.equal(instance.options.plugins.tooltip.callbacks.label, callback);
});
test('semantic series colors are consistent', () => {
    assert.equal(instance.data.datasets[0].borderColor, '#59d9bb');
    assert.equal(instance.data.datasets[0].backgroundColor, 'dark-fill');
    assert.equal(instance.data.datasets[1].borderColor, '#f18491');
});
test('custom array palettes retain their meaning', () => {
    assert.deepEqual(instance.data.datasets[2].borderColor, ['#3b82f6', '#fff']);
});
test('theme changes recolor existing charts without changing the data', () => {
    mode = 'light'; plugin.beforeUpdate(instance);
    assert.equal(instance.data.datasets[0].borderColor, '#117a66');
    assert.equal(instance.data.datasets[1].borderColor, '#bc3b52');
    assert.equal(instance.options.scales.x.grid.color, 'light-grid');
    assert.equal(instance.data.datasets[0].data, data);
});
test('theme event updates every existing chart without replaying animations', () => {
    const calls = [];
    fakeChart.instances = { first: { update(arg) { calls.push(arg); } }, second: { update(arg) { calls.push(arg); } } };
    listeners['workspace:theme']();
    assert.deepEqual(calls, ['none', 'none']);
});
test('a missing chart library does not prevent other UI from booting', () => {
    const noChart = { ...sandbox, window: {} };
    vm.createContext(noChart); vm.runInContext(script, noChart);
    assert.equal(typeof noChart.window.ChartTheme.palette, 'function');
});
test('equity tooltips use configured currency rather than hard-coded dollars', () => {
    let configuration;
    const equity = {
        Chart: function (_canvas, config) { configuration = config; },
        Money: { format(value) { return `£${Number(value).toFixed(2)}`; } },
        document: { getElementById() { return {}; } },
    };
    vm.createContext(equity);
    vm.runInContext(readFileSync('src/backtest/web/static/js/charts/equity_chart.js', 'utf8'), equity);
    vm.runInContext('renderEquityChart("equity", {dates:["2026-01-01"], values:[100]});', equity);
    const label = configuration.options.plugins.tooltip.callbacks.label({ dataset: { label: 'Equity' }, parsed: { y: 123.45 } });
    assert.equal(label, 'Equity: £123.45');
});
console.log(`${passed} tests passed`);
