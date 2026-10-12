/** Shared parameter labels must never bind to another comparison slot. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const source = fileURLToPath(new URL('../../src/backtest/web/static/js/components/params_form.js', import.meta.url));
const context = vm.createContext({ console });
vm.runInContext(fs.readFileSync(source, 'utf8'), context, { filename: 'params_form.js' });
const { renderParamsInto, collectParamsFrom, applyOverridesInto } = context;
let passed = 0;
function test(name, run) {
    run(); passed++;
    console.log(`ok - ${name}`);
}
const parameters = {
    fast: { type: 'int', label: 'Fast period', default: 20, min: 2, max: 100 },
    slow: { type: 'int', label: 'Slow period', default: 50, min: 5, max: 250 },
};
const container = (id = '') => ({ id, innerHTML: '' });
const ids = (node) => [...node.innerHTML.matchAll(/<input id="([^"]+)"/g)].map(m => m[1]);
const targets = (node) => [...node.innerHTML.matchAll(/<label for="([^"]+)"/g)].map(m => m[1]);

test('anonymous comparison slots have distinct control namespaces', () => {
    const first = container(), second = container();
    renderParamsInto(first, parameters);
    renderParamsInto(second, parameters);
    assert.equal(new Set([...ids(first), ...ids(second)]).size, 4);
    assert.deepEqual(targets(first), ids(first));
    assert.deepEqual(targets(second), ids(second));
});
test('re-rendering a container keeps its label targets stable', () => {
    const node = container('params-container');
    renderParamsInto(node, parameters);
    const before = ids(node);
    renderParamsInto(node, parameters, { fast: 30 });
    assert.deepEqual(ids(node), before);
    assert.match(node.innerHTML, /value="30" data-param="fast"/);
});
test('parameter names that sanitize identically still have unique IDs', () => {
    const node = container();
    renderParamsInto(node, {
        'a.b': { type: 'float', default: 1.5 },
        a_b: { type: 'float', default: 2.5 },
    });
    assert.equal(new Set(ids(node)).size, 2);
    assert.deepEqual(targets(node), ids(node));
    assert.match(node.innerHTML, /data-param="a.b"/);
    assert.match(node.innerHTML, /data-param="a_b"/);
});
test('numeric type, bounds and server-owned parameter keys are unchanged', () => {
    const node = container();
    renderParamsInto(node, parameters);
    assert.match(node.innerHTML, /type="number" step="1" min="2" max="100"/);
    assert.match(node.innerHTML, /value="20" data-param="fast"/);
    assert.match(node.innerHTML, /value="50" data-param="slow"/);
});
test('boolean controls retain implicit labels and false overrides', () => {
    const node = container();
    const schema = { enabled: { type: 'bool', label: 'Enabled', default: true } };
    renderParamsInto(node, schema);
    assert.match(node.innerHTML, /<label class="param-check">/);
    assert.match(node.innerHTML, /data-param="enabled" checked/);
    renderParamsInto(node, schema, { enabled: false });
    assert.doesNotMatch(node.innerHTML, /checked/);
});
test('zero overrides, floating steps and text values remain valid', () => {
    const node = container();
    renderParamsInto(node, {
        coefficient: { type: 'float', default: 1.25 },
        name: { type: 'str', default: 'daily' },
    }, { coefficient: 0 });
    assert.match(node.innerHTML, /type="number" step="any"/);
    assert.match(node.innerHTML, /value="0" data-param="coefficient"/);
    assert.match(node.innerHTML, /type="text"/);
    assert.match(node.innerHTML, /value="daily" data-param="name"/);
});
test('empty and missing containers are handled without stale controls', () => {
    const node = container();
    renderParamsInto(node, parameters);
    renderParamsInto(node, {});
    assert.equal(ids(node).length, 0);
    assert.match(node.innerHTML, /No parameters/);
    renderParamsInto(null, parameters);
});
test('collection preserves numeric, boolean, text and cleared values', () => {
    const elements = [
        { type: 'number', value: '1.5', dataset: { param: 'coefficient' } },
        { type: 'number', value: '', dataset: { param: 'missing' } },
        { type: 'checkbox', checked: true, dataset: { param: 'enabled' } },
        { type: 'text', value: 'daily', dataset: { param: 'name' } },
    ];
    assert.deepEqual(JSON.parse(JSON.stringify(collectParamsFrom({ querySelectorAll: () => elements }))), {
        coefficient: 1.5, missing: null, enabled: true, name: 'daily',
    });
    assert.equal(Object.keys(collectParamsFrom(null)).length, 0);
});
test('applying overrides changes only the requested fields', () => {
    const elements = [
        { type: 'number', value: 20, dataset: { param: 'fast' } },
        { type: 'number', value: 50, dataset: { param: 'slow' } },
        { type: 'checkbox', checked: true, dataset: { param: 'enabled' } },
    ];
    applyOverridesInto({ querySelectorAll: () => elements }, { fast: 10, enabled: false });
    assert.equal(elements[0].value, 10);
    assert.equal(elements[1].value, 50);
    assert.equal(elements[2].checked, false);
});
console.log(`${passed} tests passed`);
