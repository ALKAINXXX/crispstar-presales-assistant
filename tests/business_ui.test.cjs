// Existing pytest/Node command now tests production React/official components.
// Controlled fixtures remain distinct from browser or real-model evidence.
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
test('production React business mapping and official Thread safety', () => {
  const frontend = path.resolve(__dirname, '../frontend');
  const result = spawnSync(process.execPath, [path.join(frontend, 'node_modules/vitest/vitest.mjs'), 'run', 'src/business/BusinessResponse.test.tsx', 'src/business/responseBridge.test.tsx'], { cwd: frontend, encoding: 'utf8' });
  assert.equal(result.status, 0, `${result.error ?? ''}\n${result.stdout}\n${result.stderr}`);
});
