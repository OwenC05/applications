import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {EditBuffers} from '../backend/ui/workspace/client.js';

const source = () => readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');

test('root mounts owner-scoped budget settings with shared edits and no generic refresh wrapper', async () => {
  const app = await source();
  assert.match(app, /import \{budgetPane\} from '\.\/budget-pane\.js'/);
  const settings = app.slice(app.indexOf('function settings('), app.indexOf('async function render('));
  const start = settings.indexOf('budget = budgetPane(');
  assert.ok(start >= 0);
  const mount = settings.slice(start, settings.indexOf('append(budget.element)', start));
  assert.match(mount, /api, owner: state.owner, edits/);
  assert.match(mount, /onConfigurationChanged: \(\) => \{ disclosure\?\.invalidate\(\); review\?\.invalidate\(\); \}/);
  assert.doesNotMatch(mount, /operation\(|refreshAfter|reloadWithMessage|edits\.clear|edits\.acknowledge/);
  assert.equal((app.match(/budget = budgetPane\(/g) || []).length, 1);
});

test('actual root budget retirement clears only the owned pane once and runs on context replacement', async () => {
  const app = await source();
  const definition = app.slice(app.indexOf('function retireBudget('), app.indexOf('function retireActivity('));
  assert.match(definition, /budget\?\.retire\(\); budget = null/);
  let retired = 0;
  const helper = new Function('start', `let budget = start; ${definition}; return {retire: retireBudget, current: () => budget};`)(
    {retire() { retired++; }});
  helper.retire(); helper.retire();
  assert.equal(retired, 1); assert.equal(helper.current(), null);
  const replacement = app.slice(app.indexOf('function retireActivity('), app.indexOf('const $ ='));
  assert.match(replacement, /retireBudget\(\)/);
  const context = app.slice(app.indexOf('function context('), app.indexOf('function busy('));
  assert.ok(context.indexOf('retireActivity()') < context.indexOf('replaceChildren'));
});

test('budget and other settings generations remain independent through root dirty guards', async () => {
  const edits = new EditBuffers(), budget = {}, preferences = {};
  edits.changed(budget); const submitted = edits.capture(budget);
  edits.changed(preferences); edits.changed(budget);
  assert.equal(edits.acknowledge(submitted), false);
  assert.equal(edits.forms.get(preferences).dirty, true);
  edits.forget(budget);
  assert.equal(edits.dirty, true);
  const app = await source();
  assert.match(app, /get dirty\(\) \{ return edits.dirty; \}/);
  assert.match(app, /if \(state.dirty && !confirm\('Discard unsaved changes and change views/);
  assert.match(app, /if \(state.dirty && !confirm\('Discard unsaved changes and switch profiles/);
});
