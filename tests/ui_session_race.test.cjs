// Existing command runs actual ExternalStore adapter timing tests.
// Only fetch timing and storage are controlled; there is no legacy UI clone.
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
test('production ExternalStore session order and stale response protection', () => {
  const frontend = path.resolve(__dirname, '../frontend');
  const result = spawnSync(process.execPath, [path.join(frontend, 'node_modules/vitest/vitest.mjs'), 'run', 'src/business/adapter.test.ts'], { cwd: frontend, encoding: 'utf8' });
  assert.equal(result.status, 0, `${result.error ?? ''}\n${result.stdout}\n${result.stderr}`);
});
