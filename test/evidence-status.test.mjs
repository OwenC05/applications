import assert from 'node:assert/strict';
import test from 'node:test';
import {modelReadinessNotice} from '../backend/ui/status.js';

test('unchecked model readiness does not tell users their cache is missing', () => {
  assert.equal(modelReadinessNotice({models_ready: false, model_readiness: 'not_checked_in_request'}),
    'Model readiness not checked here. Indexing and search validate the local cache.');
});

test('explicit readiness is distinct from absent or invalid diagnostics', () => {
  assert.equal(modelReadinessNotice({models_ready: true}), 'Models cached.');
  assert.equal(modelReadinessNotice({models_ready: false}), 'Model setup required.');
  assert.equal(modelReadinessNotice({}), 'Model readiness unknown.');
  assert.equal(modelReadinessNotice({models_ready: 'false'}), 'Model readiness unknown.');
});
